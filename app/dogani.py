"""
dogani - 볼트(폴더) 단위 할일·일정 오버레이 + 사내망 팀 볼트

- 화면: 네비게이션 바 하나 + 할일 창 + 달력 창. 바의 이름을 누르면 공간(팀 볼트/내 볼트)을 고른다
- 내 볼트: 이 PC 의 폴더 하나 (볼트 열기로 바꿈, 남에게 공유 안 함). vault.py 참고
- 팀 볼트: 팀 호스트 PC 의 볼트 (팀원 모두 편집). team.py 참고
- 시스템 트레이(Win32 기본 메뉴)로 표시 / 이름 / 팀 / 볼트 / 자동 실행 / 종료
"""
import argparse
import ctypes
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import winreg
from ctypes import wintypes
from urllib.parse import quote

import pystray
from PIL import Image, ImageDraw
from PySide6.QtCore import QBuffer, QByteArray, QFile, QIODevice, QObject, QPoint, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QGuiApplication, QPalette, QRegion
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineScript,
                                      QWebEngineUrlRequestInterceptor, QWebEngineUrlRequestJob, QWebEngineUrlScheme,
                                      QWebEngineUrlSchemeHandler)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QInputDialog,
                               QLabel, QLineEdit, QMessageBox, QVBoxLayout, QWidget)

import liveupdate
import vault
from server import CLIENT_KEY, HOST_HEADER, KEY_HEADER, TOKEN_HEADER, USER_HEADER, ShareServer
from team import NAME_MAX, TeamError, TeamHost, clean_name

APP_NAME = "dogani"
CONFIG_DIR = os.environ.get("DOGANI_HOME") or os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "dogani")
CONFIG_PATH = os.path.join(CONFIG_DIR, "dogani.json")
TEAM_DIR = os.path.join(CONFIG_DIR, "team")   # 팀 호스트의 서명 키·초대장 (볼트 id 별). 예전엔 팀 데이터 전부
LEGACY_TEAM_VAULT = os.path.join(TEAM_DIR, "notes")   # v26.11.10 이전 공용 노트 폴더 = 기본 팀 볼트
RECENT_MAX = 10
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # exe 면 런처(launcher.py)가 고른 코드 폴더
UI_DIR = os.path.join(BASE_DIR, "ui")
DEFAULT_PORT = 8765
OPACITY_LEVELS = [100, 90, 80, 70, 60]
ME = "me"      # 내 노트 공간 키
TEAM = "team"  # 팀 볼트 공간 키
TEAM_POLL_MS = 20_000

TEAM_STATUS = {"online": "연결됨", "offline": "호스트 연결 끊김", "logged_out": "다시 로그인 필요"}

EMPTY_TEAM = {"host": "", "token": "", "id": "", "pub": "", "team": "", "members": []}
DEFAULT_CONFIG = {
    "port": DEFAULT_PORT,
    "vault": "",          # 지금 연 내 볼트 (폴더)
    "vaults": [],         # 최근 연 볼트 (최근 것 먼저)
    "name": "",           # 내 이름 (팀원에게 보이는 이름)
    "team": dict(EMPTY_TEAM),
    "team_host": False,   # 이 PC 가 팀 호스트
    "team_vault": "",     # 팀 호스트일 때 팀 볼트 폴더
    "visible": True,
    "space": ME,          # 지금 보고 있는 공간
    "windows": {},        # nav / note / cal -> {"pos", "panel", "open"}
    "on_top": False,      # False: 바탕화면에 고정 (Win+D 에도 남아 있음)
    "opacity": 100,
    "theme": "light",
}


# ---------------------------------------------------------------- config
def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    saved = load_json(CONFIG_PATH)
    if saved is None:
        saved = migrate_old_config()
    cfg.update(saved)
    cfg["team"] = {**EMPTY_TEAM, **(cfg.get("team") or {})}
    cfg["team"].pop("secret", None)   # 예전 HMAC 비밀키 (위조에 쓰일 수 있어 폐기)
    migrate_windows(cfg)
    # v26.11.10 이전: todo 폴더 + 내 노트 공유(일시 중지·차단·팀원 전용·주소 추가) → 볼트, 공유 없음
    old_dir = cfg.pop("todo_dir", "")
    if old_dir and not cfg["vault"]:
        cfg["vault"] = old_dir
    for k in ("paused", "team_only", "blocked", "servers"):
        cfg.pop(k, None)
    if cfg["team_host"] and not cfg["team_vault"]:
        cfg["team_vault"] = LEGACY_TEAM_VAULT
    if cfg["space"] not in (ME, TEAM):
        cfg["space"] = ME
    if cfg["vault"] and cfg["vault"] not in cfg["vaults"]:
        cfg["vaults"].insert(0, cfg["vault"])
    if not cfg["name"]:
        cfg["name"] = clean_name(socket.gethostname() or os.environ.get("USERNAME"))   # 처음엔 PC 이름
    return cfg


def migrate_windows(cfg):
    """v26.11.06 이전: 사람마다 네비게이션 바(overlays) → 이제 바 하나(windows)"""
    old = cfg.pop("overlays", None)
    if cfg["windows"] or not old:
        return
    me = old.get(ME) or {}
    cfg["windows"] = {"nav": {"pos": me.get("pos")}}
    for mode in ("note", "cal"):
        if isinstance(me.get(mode), dict):
            cfg["windows"][mode] = {k: me[mode].get(k) for k in ("pos", "panel", "open")}


def migrate_old_config():
    """v26.11.04.01 이전(서버/클라이언트 분리) 설정을 가져온다."""
    cfg = {}
    old_server = load_json(os.path.join(CONFIG_DIR, "server.json")) or {}
    old_client = load_json(os.path.join(CONFIG_DIR, "client.json")) or {}
    old_owner = load_json(os.path.join(CONFIG_DIR, "owner.json")) or {}
    for k in ("port", "todo_dir", "paused", "blocked"):
        if k in old_server:
            cfg[k] = old_server[k]
    for k in ("on_top", "opacity", "theme"):
        if k in old_client:
            cfg[k] = old_client[k]
    if old_owner.get("pos"):
        cfg["windows"] = {"nav": {"pos": old_owner["pos"]}}
    if old_client.get("server"):
        cfg["servers"] = [old_client["server"]]
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def remember_vault(cfg, path):
    cfg["vaults"] = ([path] + [v for v in cfg["vaults"] if os.path.normcase(v) != os.path.normcase(path)])[:RECENT_MAX]


def normalize_server(text):
    text = text.strip().removeprefix("http://").rstrip("/")
    if not text:
        return ""
    if ":" not in text:
        text = f"{text}:{DEFAULT_PORT}"
    return text


# ---------------------------------------------------------------- http (팀 호스트와 통신)
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 사내 프록시를 타지 않게


def http_json(server, path, body=None, token="", name="", timeout=4):
    """→ (HTTP 상태, dict). 연결 실패면 상태 0"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"http://{server}{path}", data=data, method="POST" if data else "GET")
    req.add_header(KEY_HEADER, CLIENT_KEY)
    req.add_header(HOST_HEADER, socket.gethostname()[:64])
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header(TOKEN_HEADER, token)
    if name:
        req.add_header(USER_HEADER, quote(name))
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}


# ---------------------------------------------------------------- autostart
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
OLD_RUN_NAMES = ("DoganiServer", "DoganiClient")


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def is_autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except OSError:
                pass


def migrate_autostart():
    """예전 DoganiServer/DoganiClient 자동 실행 항목이 있으면 지우고 dogani 로 바꾼다."""
    found = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as key:
            for name in OLD_RUN_NAMES:
                try:
                    winreg.QueryValueEx(key, name)
                    winreg.DeleteValue(key, name)
                    found = True
                except OSError:
                    pass
    except OSError:
        return
    if found:
        set_autostart(True)


# ---------------------------------------------------------------- desktop pin
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
GWLP_HWNDPARENT = -8
GW_OWNER = 4


def find_desktop_window():
    """바탕화면 아이콘(SHELLDLL_DefView)을 가진 창. Win11 24H2+ 는 Progman, 그 이전은 WorkerW 인 경우가 있다."""
    progman = user32.FindWindowW("Progman", None)
    if progman and user32.FindWindowExW(progman, None, "SHELLDLL_DefView", None):
        return progman
    found = []

    def callback(hwnd, _):
        if user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found[0] if found else progman


def set_owner(hwnd, owner):
    """창의 소유자(owner)를 바꾼다. 바탕화면 창이 소유자면 '바탕화면 보기(Win+D)' 때도 바탕화면 위에 남는다."""
    user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, owner or 0)


def get_owner(hwnd):
    return user32.GetWindow(hwnd, GW_OWNER)


# ---------------------------------------------------------------- tray
ICON_COLORS = {"on": (46, 160, 67), "off": (150, 150, 150)}


def make_icon_image(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=12, fill=color)
    d.line((18, 33, 28, 44, 47, 21), fill="white", width=7, joint="curve")
    return img


class FreshMenuIcon(pystray.Icon):
    """우클릭 시점에 메뉴를 다시 만들어 항상 최신 상태를 보여준다(트레이 스레드에서 실행)."""

    def _on_notify(self, wparam, lparam):
        if lparam == 0x0205:  # WM_RBUTTONUP
            self._update_menu()
        return super()._on_notify(wparam, lparam)


class Tray(QObject):
    """pystray 는 별도 스레드에서 돈다. 메뉴 클릭은 시그널로 Qt 메인 스레드에 넘긴다."""

    command = Signal(str, object)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.icon = FreshMenuIcon(APP_NAME, make_icon_image(ICON_COLORS["off"]), APP_NAME)
        self.icon.menu = pystray.Menu(lambda: self.build_menu())

    def emit(self, name, arg=None):
        # pystray 는 콜백 인자 개수(0~2개)를 검사하므로 기본값 인자를 쓰면 안 된다
        return lambda icon, item: self.command.emit(name, arg)

    def vault_items(self):
        Item = pystray.MenuItem
        cfg = self.app.cfg
        items = [Item(f"{vault.vault_name(v)}  ({v})", self.emit("switch_vault", v),
                      checked=lambda i, v=v: v == cfg["vault"], radio=True)
                 for v in cfg["vaults"]]
        if items:
            items.append(pystray.Menu.SEPARATOR)
        items += [Item("폴더 열기 (탐색기)", self.emit("open_dir"), enabled=bool(cfg["vault"])),
                  Item("볼트 열기...", self.emit("browse_dir"))]
        if len(cfg["vaults"]) > 1:
            items.append(Item("최근 목록 정리 (지금 볼트만 남김)", self.emit("clear_vaults")))
        return items

    def member_items(self):
        Item = pystray.MenuItem
        team = self.app.share.team if self.app.share else None
        if not team:
            return [Item("(없음)", None, enabled=False)]
        items = []
        for m in team.members():
            if m["admin"]:
                items.append(Item(f"{m['name']}  - 관리자(이 PC)", None, enabled=False))
                continue
            state = "접속 중" if m["online"] else "꺼짐"
            items.append(Item(f"{m['name']}  - {state}", pystray.Menu(
                Item("다시 초대 (PC 바꿈, 초대장 복사)", self.emit("reinvite", m["id"])),
                Item("팀에서 내보내기", self.emit("kick", m["id"])),
            )))
        return items

    def invite_items(self):
        Item = pystray.MenuItem
        team = self.app.share.team if self.app.share else None
        invites = team.invites() if team else []
        if not invites:
            return [Item("(없음)", None, enabled=False)]
        return [Item(f"{i['code']}  - {i['label']}, {max(1, i['left'] // 60)}분 남음", pystray.Menu(
                    Item("복사", self.emit("copy_invite", i["code"])),
                    Item("폐기", self.emit("cancel_invite", i["code"])),
                )) for i in invites]

    def team_menu(self):
        Item = pystray.MenuItem
        app = self.app
        t = app.cfg["team"]
        if app.share and app.share.team:
            team = app.share.team
            return pystray.Menu(
                Item("새 팀원 초대장 만들기 (15분, 복사됨)", self.emit("new_invite")),
                Item("초대장 관리", pystray.Menu(lambda: self.invite_items())),
                Item(f"호스트 주소 {app.share.address}  (클릭 시 복사)", self.emit("copy_address")),
                pystray.Menu.SEPARATOR,
                Item("팀원", pystray.Menu(lambda: self.member_items())),
                Item("팀 이름 바꾸기...", self.emit("rename_team")),
                Item(f"팀 볼트 폴더 열기 ({team.root})", self.emit("open_team_dir")),
                pystray.Menu.SEPARATOR,
                Item("팀 호스트 끄기", self.emit("stop_host")),
            )
        if t.get("token"):
            return pystray.Menu(
                Item(f"호스트 {t['host']}  - {TEAM_STATUS.get(app.team_status, '')}", None, enabled=False),
                Item("초대장으로 다시 참여...", self.emit("join_team")),
                pystray.Menu.SEPARATOR,
                Item("팀 나가기 (이 PC 에서 로그아웃)", self.emit("leave_team")),
            )
        return pystray.Menu(
            Item("팀 참여... (초대장)", self.emit("join_team")),
            pystray.Menu.SEPARATOR,
            Item("이 PC 를 팀 호스트로 (팀 볼트 폴더 고르기)...", self.emit("start_host"), enabled=bool(app.share)),
        )

    def build_menu(self):
        Item = pystray.MenuItem
        app, cfg = self.app, self.app.cfg
        share = app.share
        vault_label = f"볼트: {vault.vault_name(cfg['vault'])}" if cfg["vault"] else "볼트: (안 열림)"
        if share and share.team:
            team_label = f"팀스페이스: {share.team.name} (호스트)"
        elif cfg["team"].get("token"):
            team_label = f"팀스페이스: {cfg['team'].get('team') or '팀'} ({TEAM_STATUS.get(app.team_status, '')})"
        else:
            team_label = "팀스페이스: 참여 안 함"
        return [
            Item("dogani 표시", self.emit("toggle_visible"), checked=lambda i: cfg["visible"], default=True),
            Item(f"내 이름: {cfg['name']}", pystray.Menu(Item("이름 바꾸기...", self.emit("rename")))),
            Item(team_label, self.team_menu()),
            pystray.Menu.SEPARATOR,
            Item(vault_label, pystray.Menu(lambda: self.vault_items())),
            pystray.Menu.SEPARATOR,
            Item("바탕화면에 고정", self.emit("set_on_top", False), checked=lambda i: not cfg["on_top"], radio=True),
            Item("항상 위에 표시", self.emit("set_on_top", True), checked=lambda i: cfg["on_top"], radio=True),
            Item("투명도", pystray.Menu(*[
                Item(f"{level}%", self.emit("opacity", level),
                     checked=lambda i, lv=level: cfg["opacity"] == lv, radio=True)
                for level in OPACITY_LEVELS
            ])),
            Item("다크 테마", self.emit("toggle_theme"), checked=lambda i: cfg["theme"] == "dark"),
            Item("Windows 시작 시 자동 실행", self.emit("toggle_autostart"), checked=lambda i: is_autostart()),
            pystray.Menu.SEPARATOR,
            Item(lambda i: liveupdate.version_text(), None, enabled=False),
            Item("종료", self.emit("quit")),
        ]

    def update(self):
        share = self.app.share
        self.icon.icon = make_icon_image(ICON_COLORS["on" if share else "off"])
        if share:
            self.icon.title = f"dogani {liveupdate.version_text()} - {vault.vault_name(self.app.cfg['vault'])}"
        else:
            self.icon.title = f"dogani - 포트 {self.app.cfg['port']} 사용 중 (볼트를 열 수 없음)"

    def notify(self, text):
        try:
            self.icon.notify(text, APP_NAME)
        except Exception:
            pass

    def start(self):
        self.icon.run_detached()
        self.icon.visible = True

    def stop(self):
        self.icon.stop()


# ---------------------------------------------------------------- dialogs (Windows 기본 위젯)
class TeamDialog(QDialog):
    """팀 참여 (호스트 주소 + 초대장 코드). 이름은 '내 이름'을 그대로 보냄"""

    def __init__(self, host="", name=""):
        super().__init__(None, Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("팀 참여")
        form = QFormLayout()
        self.host = QLineEdit(host, placeholderText="예) 192.168.0.10  (관리자에게 받기)")
        self.code = QLineEdit(placeholderText="XXXX-XXXX-XXXX", maxLength=20)
        form.addRow("호스트 주소", self.host)
        form.addRow("초대장 코드", self.code)
        name_label = QLabel(f"{name}   (트레이 > 내 이름 에서 바꿈)")
        name_label.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow("내 이름", name_label)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("초대장은 관리자가 사람마다 따로 만들어 줍니다. (15분 안에 한 번만 쓸 수 있음)\n"
                                "PC 를 바꿨으면 관리자에게 '다시 초대'를 부탁하세요."))
        self.error = QLabel()
        self.error.setStyleSheet("color: #c0392b")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.check)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.handler = None
        self.setMinimumWidth(380)

    def check(self):
        if not normalize_server(self.host.text()):
            return self.error.setText("호스트 주소를 입력하세요.")
        if not self.code.text().strip():
            return self.error.setText("초대장 코드를 입력하세요.")
        msg = self.handler(self) if self.handler else None
        if msg:
            self.error.setText(msg)
        else:
            self.accept()


# ---------------------------------------------------------------- overlay web view
class Bridge(QObject):
    """JS(app.js) ↔ 파이썬 연결 객체 (창마다 하나)."""

    def __init__(self, win):
        super().__init__()
        self.win = win

    @Slot(result=str)
    def getConfig(self):
        return json.dumps(self.win.js_config())

    @Slot()
    def startMove(self):
        self.win.windowHandle().startSystemMove()

    @Slot()
    def startResize(self):
        self.win.start_resize()

    @Slot(int, int)
    def setNavSize(self, w, h):
        self.win.set_nav_size(w, h)

    @Slot(bool)
    def setExpanded(self, expanded):
        self.win.set_expanded(expanded)

    @Slot(str, str)
    def setStatus(self, key, status):
        if status in ("online", "offline", "blocked"):
            self.win.app.on_status(key, status)

    @Slot(str, str)
    def setName(self, key, name):
        self.win.app.on_peer_name(key, name)

    @Slot(str)
    def togglePanel(self, mode):
        if mode in ("note", "cal"):
            self.win.app.toggle_panel(mode)

    @Slot()
    def closePanel(self):
        self.win.app.close_panel(self.win.mode)

    @Slot(str)
    def openNote(self, date):
        self.win.app.open_note(date)

    @Slot(str, str, bool)
    def noteDateChanged(self, date, title, is_today):
        self.win.app.note_date_changed(date, title, is_today)

    @Slot(int)
    def stepNote(self, step):
        self.win.app.step_note(step)

    @Slot(str)
    def selectSpace(self, key):
        self.win.app.select_space(key)

    @Slot(str)
    def action(self, name):
        if name in ("join_team", "rename", "browse_dir", "start_host"):
            self.win.app.on_command(name, None)

    @Slot(str)
    def openUrl(self, url):
        if url.startswith(("http://", "https://", "mailto:")):
            QDesktopServices.openUrl(QUrl(url))


# file:// 로 열면 서버(http) 접근이 막히고, LocalContentCanAccessRemoteUrls 를 켜면 로컬 css/js 로딩이 깨진다.
# 그래서 UI 파일은 전용 스킴(dogani://ui/...)으로 제공한다.
UI_SCHEME = b"dogani"
MIME_TYPES = {".html": b"text/html", ".js": b"text/javascript", ".css": b"text/css",
              ".svg": b"image/svg+xml", ".png": b"image/png"}


def register_ui_scheme():
    """QApplication 생성 전에 호출해야 한다."""
    scheme = QWebEngineUrlScheme(UI_SCHEME)
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    scheme.setFlags(QWebEngineUrlScheme.Flag.CorsEnabled)
    QWebEngineUrlScheme.registerScheme(scheme)


class UiSchemeHandler(QWebEngineUrlSchemeHandler):
    def requestStarted(self, job):
        rel = job.requestUrl().path().lstrip("/") or "index.html"
        path = os.path.normpath(os.path.join(UI_DIR, rel))
        mime = MIME_TYPES.get(os.path.splitext(path)[1].lower())
        if not path.startswith(UI_DIR + os.sep) or not mime or not os.path.isfile(path):
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        with open(path, "rb") as f:
            data = f.read()
        buf = QBuffer(job)
        buf.setData(QByteArray(data))
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        job.reply(mime, buf)


class ClientHeaders(QWebEngineUrlRequestInterceptor):
    """서버로 가는 모든 요청(fetch, SSE)에 dogani 식별 헤더 + 내 이름 + 팀원 토큰을 붙인다."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.host = socket.gethostname().encode("utf-8", "ignore")[:64]

    def interceptRequest(self, info):
        if info.requestUrl().scheme() in ("http", "https"):
            cfg = self.app.cfg
            info.setHttpHeader(KEY_HEADER.encode(), CLIENT_KEY.encode())
            info.setHttpHeader(HOST_HEADER.encode(), self.host)
            info.setHttpHeader(USER_HEADER.encode(), quote(cfg["name"]).encode())
            if cfg["team"].get("token"):
                info.setHttpHeader(TOKEN_HEADER.encode(), cfg["team"]["token"].encode())


class Page(QWebEnginePage):
    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        # 오버레이 안에서 다른 페이지로 이동하지 않도록 막는다
        if url.scheme() == UI_SCHEME.decode():
            return True
        if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)
        return False


def qwebchannel_js():
    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError("qwebchannel.js 를 찾을 수 없습니다")
    data = bytes(f.readAll()).decode("utf-8")
    f.close()
    return data


def setup_profile(app):
    """모든 창이 같이 쓰는 기본 프로필에 UI 스킴과 헤더를 한 번만 설치."""
    profile = QWebEngineProfile.defaultProfile()
    app.scheme_handler = UiSchemeHandler(app)
    profile.installUrlSchemeHandler(UI_SCHEME, app.scheme_handler)
    app.headers = ClientHeaders(app)
    profile.setUrlRequestInterceptor(app.headers)
    script = QWebEngineScript()
    script.setName("qwebchannel")
    script.setSourceCode(qwebchannel_js())
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    profile.scripts().insert(script)


PANEL_DEFAULT_SIZE = {"note": (440, 640), "cal": (400, 440)}
PANEL_MIN_SIZE = {"note": (300, 240), "cal": (340, 300)}


class ResizeGhost(QWidget):
    """크기 조절 중에 보여주는 외곽선.
    반투명 창 + 웹뷰는 크기가 바뀔 때마다 화면 전체를 다시 그려서 끌 때 버벅이므로,
    끄는 동안은 이 선만 움직이고 마우스를 놓을 때 한 번만 실제 창 크기를 바꾼다."""

    BORDER = 2

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor("#2f80ed"))
        self.setPalette(pal)
        self.setAutoFillBackground(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        b = self.BORDER
        r = self.rect()
        self.setMask(QRegion(r).subtracted(QRegion(r.adjusted(b, b, -b, -b))))


VK_LBUTTON = 0x01


class Overlay(QWidget):
    """바탕화면 창. mode = nav(네비게이션 바, 하나뿐) | note(할일 창) | cal(달력 창)"""

    def __init__(self, app, mode):
        super().__init__()
        self.app = app
        self.mode = mode
        self.expanded = False
        self.nav_size = QSize(200, 60)

        self.setWindowTitle(APP_NAME + {"note": " 할일", "cal": " 달력"}.get(mode, ""))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.apply_flags()

        self.view = QWebEngineView(self)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        page = Page(self.view)
        page.setBackgroundColor(Qt.GlobalColor.transparent)
        self.view.setPage(page)

        self.bridge = Bridge(self)
        self.channel = QWebChannel(page)
        self.channel.registerObject("bridge", self.bridge)
        page.setWebChannel(self.channel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

        self.setWindowOpacity(app.cfg["opacity"] / 100)
        self.resize(self.nav_size)
        self.restore_position()
        # 탐색기 재시작 등으로 바탕화면 창이 바뀌면 다시 고정
        self.pin_timer = QTimer(self, interval=5000, timeout=self.apply_pin)
        self.pin_timer.start()
        self.view.load(QUrl("dogani://ui/index.html"))

    @property
    def wcfg(self):
        """이 창의 위치·크기 설정"""
        return self.app.win_cfg(self.mode)

    def js_config(self):
        cfg = self.app.js_common()
        cfg["mode"] = self.mode
        if self.mode == "nav":
            cfg.update(self.app.js_nav())
        elif self.mode == "note":
            cfg["date"] = self.wcfg.pop("date", None)
        return cfg

    # -- window flags
    def apply_flags(self):
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self.app.cfg["on_top"]:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        visible = self.isVisible()
        self.setWindowFlags(flags)
        if visible:
            self.show()

    def showEvent(self, event):
        super().showEvent(event)
        # Qt 가 표시 직후 소유자를 다시 설정하므로 조금 뒤에 적용
        QTimer.singleShot(300, self.apply_pin)

    def apply_pin(self):
        """바탕화면 고정 모드면 바탕화면 창을 소유자로 지정, 항상 위 모드면 해제."""
        if not self.isVisible():
            return
        hwnd = int(self.winId())
        target = None if self.app.cfg["on_top"] else find_desktop_window()
        if (get_owner(hwnd) or None) != (target or None):
            set_owner(hwnd, target)

    def place_on_saved_screen(self):
        """모니터별로 기억한 위치(작업 영역 기준 상대 좌표)로 옮김. 기억한 모니터가 없으면 주 모니터의 같은 상대 위치."""
        rel = self.wcfg.get("screens") or {}
        key = self.wcfg.get("screen")
        if not key or key not in rel:
            return False
        screens = {screen_key(s): s for s in QGuiApplication.screens()}
        screen = screens.get(key) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        rx, ry = rel[key] if key in screens else rel.get(screen_key(screen), rel[key])
        self._placing = True
        try:
            self.move(geo.left() + rx, geo.top() + ry)
            self.keep_on_screen(QPoint(geo.left() + 1, geo.top() + 1) if self._off_screen() else None)
        finally:
            self._placing = False
        return True

    def _off_screen(self):
        return QGuiApplication.screenAt(self.pos() + QPoint(30, 20)) is None

    def restore_position(self):
        if self.place_on_saved_screen():
            return
        pos = self.wcfg.get("pos")
        if pos and QGuiApplication.screenAt(QPoint(pos[0] + 30, pos[1] + 20)):
            self.move(pos[0], pos[1])
            return
        nav = self.app.nav
        if self.mode != "nav" and nav:
            # 처음 여는 창은 네비게이션 바 바로 아래: 할일 창은 그대로, 달력 창은 할일 창 자리 왼쪽
            w = (self.wcfg.get("panel") or PANEL_DEFAULT_SIZE[self.mode])[0]
            x = nav.x() - (w - 10 if self.mode == "cal" else 0)
            self.move(x, nav.y() + nav.nav_size.height() - 10)
            self.keep_on_screen(nav.pos())
            return
        geo = QGuiApplication.primaryScreen().availableGeometry()
        self.move(geo.right() - 420, geo.top() + 20)

    # -- size
    def set_nav_size(self, w, h):
        self.nav_size = QSize(w, h)
        if not self.expanded:
            self.setMinimumSize(0, 0)
            self.resize(self.nav_size)

    def set_expanded(self, expanded):
        self.expanded = expanded
        if expanded:
            w, h = self.wcfg.get("panel") or PANEL_DEFAULT_SIZE.get(self.mode, (440, 640))
            mw, mh = PANEL_MIN_SIZE.get(self.mode, (300, 240))
            self.setMinimumSize(mw, mh)
            self.resize(max(w, mw), max(h, mh))
            self.keep_on_screen()
        else:
            self.setMinimumSize(0, 0)
            self.resize(self.nav_size)

    def start_resize(self):
        """오른쪽 아래 손잡이로 크기 조절 시작 (외곽선만 따라다님)"""
        if getattr(self, "_ghost", None) is None:
            self._ghost = ResizeGhost()
            self._resize_timer = QTimer(self, interval=15, timeout=self._resize_tick)
        self._resize_from = (QCursor.pos(), self.size())
        self._resize_size = self.size()
        self._ghost.setGeometry(self.geometry().adjusted(*self._margins()))
        self._ghost.show()
        self._resize_timer.start()

    def _margins(self):
        m = 10  # 그림자용 투명 여백 (style.css #root padding) 안쪽 = 보이는 창
        return m, m, -m, -m

    def _resize_tick(self):
        start, size = self._resize_from
        d = QCursor.pos() - start
        mn = self.minimumSize()
        w = max(mn.width(), size.width() + d.x(), 120)
        h = max(mn.height(), size.height() + d.y(), 80)
        self._resize_size = QSize(w, h)
        geo = self.geometry()
        geo.setSize(self._resize_size)
        self._ghost.setGeometry(geo.adjusted(*self._margins()))
        if not ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
            self._resize_timer.stop()
            self._ghost.hide()
            self.resize(self._resize_size)

    def keep_on_screen(self, anchor=None):
        anchor = anchor if anchor is not None else self.pos()
        screen = QGuiApplication.screenAt(anchor + QPoint(30, 20)) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = min(self.x(), geo.right() - self.width() + 1)
        y = min(self.y(), geo.bottom() - self.height() + 1)
        self.move(max(geo.left(), x), max(geo.top(), y))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.expanded:
            self.wcfg["panel"] = [self.width(), self.height()]
            self.app.save_later()

    def moveEvent(self, event):
        super().moveEvent(event)
        if getattr(self, "_placing", False) or self.app.screens_changing:
            return  # 모니터 연결/해제로 밀려난 이동은 기억하지 않음
        self.wcfg["pos"] = [self.x(), self.y()]
        screen = QGuiApplication.screenAt(self.pos() + QPoint(30, 20))
        if screen:
            geo = screen.availableGeometry()
            key = screen_key(screen)
            self.wcfg.setdefault("screens", {})[key] = [self.x() - geo.left(), self.y() - geo.top()]
            self.wcfg["screen"] = key
        self.app.save_later()

    def push_config(self, **values):
        self.view.page().runJavaScript(f"window.App && App.configure({json.dumps(values)})")

    def call(self, fn, *args):
        self.view.page().runJavaScript(f"window.App && App.{fn}({', '.join(json.dumps(a) for a in args)})")


# ---------------------------------------------------------------- app
def overlaps(a, b):
    """두 폴더가 같거나 한쪽이 다른 쪽 안에 있음"""
    a, b = os.path.normcase(os.path.abspath(a)), os.path.normcase(os.path.abspath(b))
    try:
        common = os.path.commonpath([a, b])
    except ValueError:   # 다른 드라이브
        return False
    return common in (a, b)


def screen_key(screen):
    """모니터 식별자: 연결 순서에 따라 바뀌는 DISPLAYn 대신 제조사·모델·일련번호 우선."""
    key = " ".join(p for p in (screen.manufacturer(), screen.model(), screen.serialNumber()) if p).strip()
    return key or screen.name()


class doganiApp(QObject):
    team_result = Signal(int, object)   # 팀 호스트 응답 (작업 스레드 → 메인 스레드)

    def __init__(self, qapp, cfg):
        super().__init__()
        self.qapp = qapp
        self.cfg = cfg
        self.status = {}     # 공간 키 -> 창에서 알려 준 연결 상태
        self.nav = None      # 네비게이션 바 (하나)
        self.panels = {}     # "note" | "cal" -> Overlay
        self.note_label = None   # 할일 창이 보여주는 날짜 {date, title, today}
        self.team_status = "offline"
        self.team_polling = False
        self.save_timer = QTimer(self, singleShot=True, interval=800, timeout=lambda: save_config(self.cfg))
        # 모니터 연결/해제: Windows 가 창을 밀어 놓은 뒤 각 창을 기억한 모니터 기준 위치로 되돌림
        self.screens_changing = False
        self.screen_timer = QTimer(self, singleShot=True, interval=1500, timeout=self.on_screens_settled)
        qapp.screenAdded.connect(self.on_screens_changed)
        qapp.screenRemoved.connect(self.on_screens_changed)
        qapp.primaryScreenChanged.connect(self.on_screens_changed)
        setup_profile(self)

        self.open_vault_meta(cfg["vault"])
        self.share = ShareServer(cfg["port"], cfg["vault"], cfg["name"])
        try:
            self.share.start()
        except OSError:
            self.share = None
        if self.share and cfg["team_host"]:
            self.start_host(quiet=True)
        self.apply_team_auth()

        self.tray = Tray(self)
        self.tray.command.connect(self.on_command)
        liveupdate.install(self)
        self.team_result.connect(self.on_team_result)
        self.team_timer = QTimer(self, interval=TEAM_POLL_MS, timeout=self.poll_team)

    def save_later(self):
        self.save_timer.start()

    def on_screens_changed(self, *_):
        self.screens_changing = True
        self.screen_timer.start()

    def on_screens_settled(self):
        for ov in self.windows():
            ov.place_on_saved_screen()
        self.screens_changing = False

    def win_cfg(self, mode):
        return self.cfg["windows"].setdefault(mode, {"pos": None, "panel": None, "open": False})

    def windows(self):
        return ([self.nav] if self.nav else []) + list(self.panels.values())

    def in_team(self):
        return bool(self.cfg["team"].get("token"))

    # ------------------------------------------------------------ spaces (공용 / 개인)
    def spaces(self):
        cfg, t = self.cfg, self.cfg["team"]
        out = []
        if self.in_team():
            out.append({"key": TEAM, "group": "team", "name": t.get("team") or "팀 공용", "server": t["host"],
                        "prefix": "/api/team", "kind": "team",
                        "status": (self.status.get(TEAM) or "online") if self.team_status == "online" else
                        ("blocked" if self.team_status == "logged_out" else "offline")})
        out.append({"key": ME, "group": "me", "name": vault.vault_name(cfg["vault"]) or cfg["name"],
                    "server": f"127.0.0.1:{cfg['port']}",
                    "prefix": "/api", "kind": "me", "status": "online" if self.share else "offline",
                    "sub": "" if self.share else "포트 사용 중"})
        for s in out:
            s["status"] = s.get("status") or "offline"
        return out

    def current_space(self):
        spaces = self.spaces()
        cur = next((s for s in spaces if s["key"] == self.cfg["space"] and not s.get("disabled")), None)
        return cur or next(s for s in spaces if s["key"] == ME)

    def team_space(self):
        """개인 공간 달력에 같이 보여줄 공용 일정 출처"""
        if not self.in_team() or self.team_status == "logged_out":
            return None
        return {"server": self.cfg["team"]["host"], "prefix": "/api/team"}

    def js_common(self):
        return {"theme": self.cfg["theme"], "space": self.current_space(), "team": self.team_space()}

    def js_nav(self):
        return {"spaces": self.spaces(), "open": self.open_panels(), "noteLabel": self.note_label,
                "me": self.cfg["name"], "teamState": "host" if self.share and self.share.team else
                ("joined" if self.in_team() else "none")}

    def push_spaces(self):
        common = self.js_common()
        for win in self.panels.values():
            win.push_config(**common)
        if self.nav:
            self.nav.push_config(**common, **self.js_nav())

    def select_space(self, key):
        if key == self.cfg["space"]:
            return
        self.cfg["space"] = key
        self.save_later()
        self.push_spaces()

    def on_status(self, key, status):
        if self.status.get(key) == status:
            return
        self.status[key] = status
        if self.nav:
            self.nav.push_config(spaces=self.spaces())

    def on_peer_name(self, key, name):
        pass   # 예전 '주소로 추가한 노트' 이름 표시용 (지금은 쓰지 않음)

    # ------------------------------------------------------------ team (client)
    def apply_team_auth(self):
        if self.share:
            self.share.name = self.cfg["name"]

    def poll_team(self):
        t = self.cfg["team"]
        if not t.get("token") or self.team_polling:
            return
        self.team_polling = True
        host, token, name = t["host"], t["token"], self.cfg["name"]

        def work():
            status, res = http_json(host, "/api/team/me", token=token, name=name)
            self.team_result.emit(status, res)

        threading.Thread(target=work, daemon=True).start()

    def on_team_result(self, status, res):
        self.team_polling = False
        t = self.cfg["team"]
        if not t.get("token"):
            return
        prev = self.team_status
        if status == 200:
            self.team_status = "online"
            t["members"] = res.get("members") or []
            t["pub"] = res.get("pub") or t.get("pub", "")
            t["team"] = res.get("team") or t["team"]
            me = res.get("me")
            if me and me.get("name") and me["name"] != self.cfg["name"]:
                self.cfg["name"] = me["name"]   # 다른 PC 에서 바꾼 이름
        elif status == 401:
            self.team_status = "logged_out"
            if prev != "logged_out":
                self.tray.notify("팀 로그인이 풀렸습니다. (내보내졌거나 다른 PC 로 다시 초대됨)\n관리자에게 초대장을 받아 다시 참여하세요.")
        else:
            self.team_status = "offline"
        self.apply_team_auth()
        self.save_later()
        self.push_spaces()

    def team_request(self, host, path, body):
        """대화상자에서 쓰는 동기 요청 → (성공 여부, 응답 또는 오류 문구)"""
        status, res = http_json(host, path, body, token=self.cfg["team"].get("token", ""), name=self.cfg["name"])
        return self.team_request_result(host, status, res)

    @staticmethod
    def team_request_result(host, status, res):
        if status == 200:
            return True, res
        if status == 0:
            return False, f"{host} 에 연결할 수 없습니다. 주소와 호스트 PC 의 dogani 가 켜져 있는지 확인하세요."
        if res.get("error") == "no_team":
            return False, "그 PC 는 팀 호스트가 아닙니다."
        return False, res.get("message") or f"실패했습니다. (HTTP {status})"

    def set_session(self, host, res):
        self.cfg["team"] = {**EMPTY_TEAM, "host": host, "token": res["token"], "id": res["id"],
                            "pub": res.get("pub", ""), "team": res.get("team", "")}
        self.cfg["name"] = res.get("name") or self.cfg["name"]
        self.team_status = "online"
        self.apply_team_auth()
        save_config(self.cfg)
        self.poll_team()
        self.push_spaces()
        if hasattr(self, "tray"):   # 시작할 때(트레이 만들기 전) 호스트를 여는 경우
            self.tray.update()

    def join_team(self):
        t = self.cfg["team"]
        dlg = TeamDialog("" if t.get("host", "").startswith("127.") else t.get("host", ""), self.cfg["name"])

        def handler(d):
            host = normalize_server(d.host.text())
            name = self.cfg["name"]
            while True:
                status, res = http_json(host, "/api/team/join", {"code": d.code.text(), "name": name})
                if status != 409 or res.get("error") != "name_taken":
                    break
                # 이름이 겹치면 그 자리에서 다른 이름을 물어 같은 초대장으로 다시 시도
                name, ok = QInputDialog.getText(d, "이름이 겹침", res.get("message", "") + "\n\n팀에서 쓸 다른 이름:",
                                                QLineEdit.EchoMode.Normal, name)
                name = clean_name(name) if ok else ""
                if not name:
                    return "취소했습니다. (초대장은 15분 안에 다시 쓸 수 있음)"
            ok, res = self.team_request_result(host, status, res)
            if not ok:
                return res
            self.set_session(host, res)
            return None

        dlg.handler = handler
        if dlg.exec():
            self.tray.notify(f"{self.cfg['team'].get('team') or '팀'}에 {self.cfg['name']}(으)로 들어왔습니다.")

    def leave_team(self):
        if QMessageBox.question(None, APP_NAME, "이 PC 에서 팀 로그아웃할까요?\n(팀원 목록에서 지워지지는 않습니다. "
                                "다시 들어오려면 관리자에게 초대장을 받으세요.)") != QMessageBox.StandardButton.Yes:
            return
        self.cfg["team"] = dict(EMPTY_TEAM)
        self.cfg["space"] = ME
        self.apply_team_auth()
        self.push_spaces()

    # ------------------------------------------------------------ team (host)
    def pick_team_vault(self):
        """팀 볼트로 쓸 폴더 고르기 → 경로 또는 "" (내 볼트와 겹치면 안 됨: 개인 노트가 팀에 공개됨)"""
        start = self.cfg["team_vault"] or (LEGACY_TEAM_VAULT if os.path.isdir(LEGACY_TEAM_VAULT) else os.path.expanduser("~"))
        while True:
            path = QFileDialog.getExistingDirectory(None, "팀 볼트 폴더 선택 (팀원 모두에게 공개·편집됨)", start)
            if not path:
                return ""
            path = os.path.normpath(path)
            mine = self.cfg["vault"]
            if mine and overlaps(path, mine):
                QMessageBox.warning(None, APP_NAME, "내 볼트와 겹치는 폴더는 팀 볼트로 쓸 수 없습니다.\n"
                                    "(내 볼트 노트가 팀원에게 공개됨) 따로 빈 폴더를 만들어 고르세요.")
                start = path
                continue
            return path

    def start_host(self, quiet=False):
        if not self.share:
            return
        root = self.cfg["team_vault"]
        if not quiet:
            if self.in_team() and QMessageBox.question(
                    None, APP_NAME, "지금 들어가 있는 팀에서 로그아웃하고 이 PC 를 새 팀의 호스트로 만들까요?"
            ) != QMessageBox.StandardButton.Yes:
                return
            root = self.pick_team_vault()
            if not root:
                return
        if self.cfg["vault"] and overlaps(root, self.cfg["vault"]):
            QMessageBox.warning(None, APP_NAME, "내 볼트와 겹치는 폴더는 팀 볼트로 열 수 없습니다.\n"
                                f"팀 볼트: {root}\n내 볼트: {self.cfg['vault']}")
            return
        if quiet and not os.path.isdir(root):
            # 폴더가 옮겨졌거나 드라이브가 아직 안 붙음 → 새로 만들면 새 볼트 id·새 키 = 팀원 모두 로그아웃. 켜지 않고 알림
            QMessageBox.warning(None, APP_NAME, f"팀 볼트 폴더가 없어 팀 호스트를 켜지 않았습니다.\n{root}\n\n"
                                "폴더를 되돌린 뒤 다시 실행하거나, 트레이 > 팀스페이스 에서 다시 고르세요.")
            return
        # 예전 팀 데이터(team\team.json)는 예전 공용 노트 폴더를 팀 볼트로 열 때만 가져옴 (노트와 팀원이 갈라지지 않게)
        legacy = TEAM_DIR if os.path.normcase(os.path.abspath(root)) == os.path.normcase(os.path.abspath(LEGACY_TEAM_VAULT)) else None
        try:
            os.makedirs(root, exist_ok=True)
            host = TeamHost(root, TEAM_DIR, self.cfg["name"], legacy_dir=legacy)
        except OSError as e:
            QMessageBox.warning(None, APP_NAME, f"팀 볼트를 열 수 없습니다.\n{e}")
            return
        self.share.team = host
        self.cfg["team_host"] = True
        self.cfg["team_vault"] = root
        owner = next(m for m in host.members() if m["id"] == host.owner_id)
        self.cfg["name"] = owner["name"]
        self.set_session(f"127.0.0.1:{self.cfg['port']}",
                         {"token": host.owner_token(), "id": host.owner_id, "pub": host.pub,
                          "team": host.name, "name": owner["name"]})
        if not quiet:
            QMessageBox.information(
                None, APP_NAME,
                f"팀 호스트를 열었습니다.\n\n팀원 초대: 트레이 > 팀스페이스 > 새 팀원 초대장 만들기\n"
                f"  → 호스트 주소({self.share.address})와 초대장 코드가 복사되니 그 사람에게 보내 주세요.\n"
                "  (사람마다 하나씩, 15분 안에 한 번만 쓸 수 있음)\n\n"
                "이 PC 가 꺼져 있으면 공용 공간을 볼 수 없으니, 켜 두는 시간이 긴 PC 를 추천합니다.")

    def stop_host(self):
        if QMessageBox.question(None, APP_NAME, "팀 호스트를 끌까요?\n팀원들은 공용 공간을 볼 수 없게 됩니다.\n"
                                f"(팀 볼트 {self.cfg['team_vault']} 와 키는 남아 있어서 다시 켜면 그대로 돌아옵니다.)"
                                ) != QMessageBox.StandardButton.Yes:
            return
        if self.share:
            self.share.team = None
        self.cfg["team_host"] = False
        self.cfg["team"] = dict(EMPTY_TEAM)
        self.cfg["space"] = ME
        self.apply_team_auth()
        self.push_spaces()

    def rename(self):
        name, ok = QInputDialog.getText(None, "내 이름", "팀원에게 보이는 이름 (최대 20자)",
                                        QLineEdit.EchoMode.Normal, self.cfg["name"])
        name = clean_name(name) if ok else ""
        if not name or name == self.cfg["name"]:
            return
        if self.in_team() and self.team_status != "logged_out":   # 로그인 풀렸으면 이 PC 이름만 바꿈
            ok, res = self.team_request(self.cfg["team"]["host"], "/api/team/profile", {"name": name})
            if not ok:
                QMessageBox.warning(None, APP_NAME, res)
                return
            name = res.get("name", name)
        self.cfg["name"] = name
        self.apply_team_auth()
        self.poll_team()
        self.push_spaces()
        self.tray.update()

    # ------------------------------------------------------------ windows
    def open_panels(self):
        return {m: m in self.panels for m in ("note", "cal")}

    def show_panel(self, mode):
        self.win_cfg(mode)["open"] = True
        win = self.panels.get(mode)
        if not win:
            win = Overlay(self, mode)
            self.panels[mode] = win
        win.show()
        win.raise_()
        self.push_open()
        self.save_later()
        return win

    def close_panel(self, mode, forget=True):
        win = self.panels.pop(mode, None)
        if forget:
            self.win_cfg(mode)["open"] = False
        if win:
            win.close()
            win.deleteLater()
        if mode == "note":
            self.note_label = None
        self.push_open()
        self.save_later()

    def toggle_panel(self, mode):
        if mode in self.panels:
            self.close_panel(mode)
        else:
            self.show_panel(mode)

    def push_open(self):
        if self.nav:
            self.nav.push_config(open=self.open_panels(), noteLabel=self.note_label)

    def open_note(self, date):
        """달력에서 날짜 클릭 → 할일 창을 열고(이미 열려 있으면 그대로) 그 날짜로"""
        win = self.panels.get("note")
        if win:
            win.show()
            win.raise_()
            win.call("showDate", date)
        else:
            self.win_cfg("note")["date"] = date
            self.show_panel("note")

    def note_date_changed(self, date, title, is_today):
        self.note_label = {"date": date, "title": title, "today": is_today}
        if self.nav:
            self.nav.push_config(noteLabel=self.note_label)
        cal = self.panels.get("cal")
        if cal:
            cal.push_config(noteDate=date)

    def step_note(self, step):
        win = self.panels.get("note")
        if win:
            win.call("step", step)

    def set_visible(self, visible):
        self.cfg["visible"] = visible
        if visible:
            if not self.nav:
                self.nav = Overlay(self, "nav")
            self.nav.show()
            for mode in ("note", "cal"):
                if self.win_cfg(mode).get("open"):
                    self.show_panel(mode)
        else:
            if self.nav:
                self.nav.hide()
            for mode in ("note", "cal"):
                self.close_panel(mode, forget=False)  # 다시 표시하면 열려 있던 창도 다시 연다

    def start(self):
        self.tray.update()
        self.tray.start()
        if self.cfg["visible"]:
            self.set_visible(True)
        self.team_timer.start()
        self.poll_team()
        if not self.share:
            self.tray.notify(f"포트 {self.cfg['port']}이(가) 사용 중이라 볼트를 열 수 없습니다.\n"
                             "예전 DoganiServer 가 켜져 있다면 종료하고 다시 실행하세요.")
        elif not self.cfg["vault"]:
            QTimer.singleShot(500, self.browse_dir)

    # ------------------------------------------------------------ actions
    def open_vault_meta(self, path):
        """볼트 표시(.dogani/vault.json) 만들기. 폴더가 없으면(USB·네트워크 드라이브 등) 조용히 넘어감"""
        try:
            return vault.open_vault(path) if path else None
        except OSError:
            return None

    def browse_dir(self):
        path = QFileDialog.getExistingDirectory(
            None, "볼트 열기 (노트 폴더)  예) 폴더/2026.11/261102 월.md",
            self.cfg["vault"] or os.path.expanduser("~"))
        if path:
            self.switch_vault(os.path.normpath(path))

    def switch_vault(self, path):
        team_root = self.share.team.root if self.share and self.share.team else ""
        if team_root and overlaps(path, team_root):
            QMessageBox.warning(None, APP_NAME, "팀 볼트와 겹치는 폴더는 내 볼트로 열 수 없습니다.\n"
                                "(팀 볼트는 바의 공간 선택에서 '공용'으로 보세요)")
            return
        if not os.path.isdir(path):
            if QMessageBox.question(None, APP_NAME, f"폴더가 없습니다.\n{path}\n\n최근 목록에서 뺄까요?"
                                    ) == QMessageBox.StandardButton.Yes:
                self.cfg["vaults"] = [v for v in self.cfg["vaults"] if os.path.normcase(v) != os.path.normcase(path)]
                save_config(self.cfg)
            return
        self.open_vault_meta(path)
        remember_vault(self.cfg, path)
        if path != self.cfg["vault"]:
            self.cfg["vault"] = path
            self.cfg["space"] = ME
            if self.share:
                self.share.set_root(path)
            self.push_spaces()
            self.tray.update()
        save_config(self.cfg)

    def on_command(self, name, arg):
        cfg = self.cfg
        share = self.share
        team = share.team if share else None
        if name == "toggle_visible":
            self.set_visible(not cfg["visible"])
        elif name == "select_space":
            if not cfg["visible"]:
                self.set_visible(True)
            self.select_space(arg)
        elif name == "rename":
            self.rename()
        elif name == "join_team":
            self.join_team()
        elif name == "leave_team":
            self.leave_team()
        elif name == "start_host":
            self.start_host()
        elif name == "stop_host":
            self.stop_host()
        elif name in ("new_invite", "reinvite", "copy_invite") and team:
            try:
                code = arg if name == "copy_invite" else team.create_invite(arg if name == "reinvite" else None)
            except TeamError as e:
                return self.tray.notify(e.message)
            if all(i["code"] != code for i in team.invites()):
                return self.tray.notify("만료됐거나 폐기된 초대장입니다. 새로 만드세요.")
            QGuiApplication.clipboard().setText(f"dogani 팀 참여\n호스트 주소: {self.share.address}\n초대장 코드: {code}")
            if name != "copy_invite":
                self.tray.notify(f"초대장 {code} 를 복사했습니다. (15분 안에 한 번만 사용)\n메신저로 그 사람에게 붙여 넣어 보내세요.")
        elif name == "cancel_invite" and team:
            team.cancel_invite(arg)
        elif name == "rename_team" and team:
            text, ok = QInputDialog.getText(None, "팀 이름", "공용 공간 이름", QLineEdit.EchoMode.Normal, team.name)
            if ok and clean_name(text):
                team.rename_team(text)
                self.poll_team()
        elif name == "open_team_dir" and team:
            os.startfile(team.notes_dir)
        elif name == "kick" and team:
            m = next((m for m in team.members() if m["id"] == arg), None)
            if m and QMessageBox.question(None, APP_NAME, f"{m['name']} 님을 팀에서 내보낼까요?") == QMessageBox.StandardButton.Yes:
                team.kick(arg)
                self.poll_team()
        elif name == "copy_address" and share:
            QGuiApplication.clipboard().setText(share.address)
        elif name == "open_dir":
            if cfg["vault"] and os.path.isdir(cfg["vault"]):
                os.startfile(cfg["vault"])
        elif name == "browse_dir":
            self.browse_dir()
        elif name == "switch_vault":
            self.switch_vault(arg)
        elif name == "clear_vaults":
            cfg["vaults"] = [cfg["vault"]] if cfg["vault"] else []
        elif name == "set_on_top":
            cfg["on_top"] = arg
            for ov in self.windows():
                ov.apply_flags()
        elif name == "opacity":
            cfg["opacity"] = arg
            for ov in self.windows():
                ov.setWindowOpacity(arg / 100)
        elif name == "toggle_theme":
            cfg["theme"] = "light" if cfg["theme"] == "dark" else "dark"
            for ov in self.windows():
                ov.push_config(theme=cfg["theme"])
        elif name == "toggle_autostart":
            set_autostart(not is_autostart())
        elif name == "quit":
            save_config(cfg)
            if share:
                share.stop()
            self.tray.stop()
            self.qapp.quit()
            return
        save_config(cfg)


def single_instance(name="DoganiMutex"):
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\" + name)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def main():
    parser = argparse.ArgumentParser(description="dogani - 볼트 할일·일정 오버레이 + 팀 볼트")
    parser.add_argument("--dir", help="열 볼트(노트 폴더) 경로")
    parser.add_argument("--port", type=int, help=f"포트 (기본 {DEFAULT_PORT}, 팀 호스트 주소에 씀)")
    parser.add_argument("--name", help="내 이름")
    args = parser.parse_args()

    register_ui_scheme()
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)

    mutex = liveupdate.MUTEX = single_instance()
    if mutex is None:
        QMessageBox.information(None, APP_NAME, "dogani 가 이미 실행 중입니다.\n시스템 트레이를 확인하세요.")
        return

    cfg = load_config()
    if args.dir:
        cfg["vault"] = os.path.abspath(args.dir)
        remember_vault(cfg, cfg["vault"])
    if args.port:
        cfg["port"] = args.port
    if args.name and clean_name(args.name):
        cfg["name"] = clean_name(args.name)
    save_config(cfg)
    migrate_autostart()

    app = doganiApp(qapp, cfg)
    app.start()
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()

"""
dogani - 사내망 todo 실시간 공유 (서버 + 뷰어 통합)

- 화면: 네비게이션 바 하나 + 할일 창 + 달력 창. 바의 이름을 누르면 공간(공용/개인)을 고른다
- 개인 공간: 내 노트(todo 폴더를 공유 서버로 열고 바로 편집) / 팀원·다른 사람 노트(읽기 전용)
- 공용 공간: 팀 호스트 PC 에 있는 공용 노트·공용 일정 (팀원 모두 편집). team.py 참고
- 시스템 트레이(Win32 기본 메뉴)로 표시 / 이름 / 팀 / 공유 / 접속자 / 경로 / 자동 실행 / 종료
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
from server import CLIENT_KEY, HOST_HEADER, KEY_HEADER, TOKEN_HEADER, USER_HEADER, ShareServer
from team import NAME_MAX, TeamHost, clean_name, valid_pin

APP_NAME = "Dogani"
CONFIG_DIR = os.environ.get("DOGANI_HOME") or os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Dogani")
CONFIG_PATH = os.path.join(CONFIG_DIR, "dogani.json")
TEAM_DIR = os.path.join(CONFIG_DIR, "team")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # exe 면 런처(launcher.py)가 고른 코드 폴더
UI_DIR = os.path.join(BASE_DIR, "ui")
DEFAULT_PORT = 8765
OPACITY_LEVELS = [100, 90, 80, 70, 60]
ME = "me"      # 내 노트 공간 키
TEAM = "team"  # 공용 공간 키 (팀원은 "m:팀원ID", 주소로 추가한 사람은 "ip:port")
TEAM_POLL_MS = 20_000

STATUS_LABELS = {
    "online": "접속 중",
    "offline": "연결 끊김",
    "paused": "공유 일시 중지됨",
    "blocked": "차단됨",
    "team_only": "팀원만 볼 수 있음",
}
TEAM_STATUS = {"online": "연결됨", "offline": "호스트 연결 끊김", "logged_out": "다시 로그인 필요"}

EMPTY_TEAM = {"host": "", "token": "", "id": "", "pub": "", "team": "", "members": []}
DEFAULT_CONFIG = {
    "port": DEFAULT_PORT,
    "todo_dir": "",
    "name": "",           # 내 이름 (팀원·동료에게 보이는 이름)
    "paused": False,      # 내 노트 공유 일시 중지
    "team_only": True,    # 팀에 들어 있으면 팀원에게만 내 노트 공유
    "blocked": [],
    "servers": [],        # 주소로 직접 추가한 사람 ["192.168.0.10:8765", ...]
    "team": dict(EMPTY_TEAM),
    "team_host": False,   # 이 PC 가 팀 호스트
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
    if not cfg["name"]:
        cfg["name"] = clean_name(os.environ.get("USERNAME") or socket.gethostname())
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
    """예전 DoganiServer/DoganiClient 자동 실행 항목이 있으면 지우고 Dogani 로 바꾼다."""
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
ICON_COLORS = {"sharing": (46, 160, 67), "paused": (230, 145, 30), "off": (150, 150, 150)}


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

    def client_items(self):
        Item = pystray.MenuItem
        share = self.app.share
        if not share:
            return [Item("(공유 서버 꺼짐)", None, enabled=False)]
        clients, blocked = share.hub.snapshot()
        items = []
        order = sorted(clients.items(), key=lambda kv: (kv[1]["active"] == 0, -kv[1]["last_seen"]))
        for ip, c in order:
            if ip in blocked:
                continue
            who = c.get("user") or c["host"]
            name = f"{who} ({ip})" if who else ip
            if not c["active"]:
                name += f"  - 마지막 {time.strftime('%m/%d %H:%M', time.localtime(c['last_seen']))}"
            items.append(Item(name, pystray.Menu(Item("연결 끊고 차단", self.emit("block", ip))),
                              checked=lambda i, a=c["active"]: a > 0, radio=True))
        if blocked:
            if items:
                items.append(pystray.Menu.SEPARATOR)
            for ip in sorted(blocked):
                c = clients.get(ip, {})
                who = c.get("user") or c.get("host", "")
                items.append(Item(f"[차단됨] {ip}" + (f" ({who})" if who else ""),
                                  pystray.Menu(Item("차단 해제", self.emit("unblock", ip)))))
        return items or [Item("(접속 기록 없음)", None, enabled=False)]

    def server_items(self):
        Item = pystray.MenuItem
        app = self.app
        items = []
        for addr in app.cfg["servers"]:
            name = app.names.get(addr)
            label = f"{name} ({addr})" if name else addr
            label += f"  - {STATUS_LABELS.get(app.status.get(addr, 'offline'), '')}"
            items.append(Item(label, pystray.Menu(
                Item("보기", self.emit("select_space", addr)),
                Item("삭제", self.emit("remove_server", addr)),
            )))
        if items:
            items.append(pystray.Menu.SEPARATOR)
        items.append(Item("추가...", self.emit("add_server")))
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
            state = "PIN 초기화됨 (다시 참여 대기)" if m["pending"] else "접속 중" if m["online"] else "꺼짐"
            items.append(Item(f"{m['name']}  - {state}", pystray.Menu(
                Item("PIN 초기화 (잊어버렸을 때)", self.emit("reset_pin", m["id"])),
                Item("팀에서 내보내기", self.emit("kick", m["id"])),
            )))
        return items

    def team_menu(self):
        Item = pystray.MenuItem
        app = self.app
        t = app.cfg["team"]
        if app.share and app.share.team:
            team = app.share.team
            return pystray.Menu(
                Item(f"참여 코드 {team.code}  (클릭 시 복사)", self.emit("copy_code")),
                Item(f"호스트 주소 {app.share.address}  (클릭 시 복사)", self.emit("copy_address")),
                Item("참여 코드 새로 만들기", self.emit("renew_code")),
                pystray.Menu.SEPARATOR,
                Item("팀원", pystray.Menu(lambda: self.member_items())),
                Item("팀 이름 바꾸기...", self.emit("rename_team")),
                Item("공용 폴더 열기", self.emit("open_team_dir")),
                pystray.Menu.SEPARATOR,
                Item("팀 호스트 끄기", self.emit("stop_host")),
            )
        if t.get("token"):
            return pystray.Menu(
                Item(f"호스트 {t['host']}  - {TEAM_STATUS.get(app.team_status, '')}", None, enabled=False),
                Item("PIN 바꾸기...", self.emit("change_pin")),
                Item("다시 로그인...", self.emit("login_team")),
                pystray.Menu.SEPARATOR,
                Item("팀 나가기 (이 PC 에서 로그아웃)", self.emit("leave_team")),
            )
        return pystray.Menu(
            Item("팀 참여... (참여 코드)", self.emit("join_team")),
            Item("로그인... (다른 PC 에서 참여했던 계정)", self.emit("login_team")),
            pystray.Menu.SEPARATOR,
            Item("이 PC 를 팀 호스트로 (새 팀 만들기)", self.emit("start_host"), enabled=bool(app.share)),
        )

    def build_menu(self):
        Item = pystray.MenuItem
        app, cfg = self.app, self.app.cfg
        share = app.share
        if share:
            share_label = "공유 일시 중지됨" if share.paused else "공유 중"
            share_menu = pystray.Menu(
                Item(f"{share.address}  (클릭 시 주소 복사)", self.emit("copy_address")),
                pystray.Menu.SEPARATOR,
                Item("공유 일시 중지", self.emit("toggle_pause"), checked=lambda i: share.paused),
                Item("팀원에게만 공유", self.emit("toggle_team_only"), checked=lambda i: cfg["team_only"],
                     enabled=app.in_team()),
                Item("접속자", pystray.Menu(lambda: self.client_items())),
            )
        else:
            share_label = "공유 꺼짐 (포트 사용 중)"
            share_menu = pystray.Menu(Item(f"포트 {cfg['port']}을(를) 열 수 없습니다", None, enabled=False))
        todo_dir = cfg["todo_dir"] or "(경로 미지정)"
        if share and share.team:
            team_label = f"팀스페이스: {share.team.name} (호스트)"
        elif cfg["team"].get("token"):
            team_label = f"팀스페이스: {cfg['team'].get('team') or '팀'} ({TEAM_STATUS.get(app.team_status, '')})"
        else:
            team_label = "팀스페이스: 참여 안 함"
        return [
            Item("Dogani 표시", self.emit("toggle_visible"), checked=lambda i: cfg["visible"], default=True),
            Item(f"내 이름: {cfg['name']}", pystray.Menu(Item("이름 바꾸기...", self.emit("rename")))),
            Item(team_label, self.team_menu()),
            pystray.Menu.SEPARATOR,
            Item(f"내 노트 {share_label}", share_menu, checked=lambda i: bool(share) and not share.paused, radio=True),
            Item("todo 경로", pystray.Menu(
                Item(todo_dir, self.emit("open_dir")),
                pystray.Menu.SEPARATOR,
                Item("찾아보기...", self.emit("browse_dir")),
            )),
            Item("주소로 추가한 노트", pystray.Menu(lambda: self.server_items())),
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
        key = "off" if not share else "paused" if share.paused else "sharing"
        self.icon.icon = make_icon_image(ICON_COLORS[key])
        if share:
            state = "공유 일시 중지" if share.paused else "공유 중"
            self.icon.title = f"Dogani {liveupdate.version_text()} - {self.app.cfg['name']} {share.address} ({state})"
        else:
            self.icon.title = "Dogani - 내 노트 공유 꺼짐"

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
    """팀 참여(join) / 로그인(login) / PIN 바꾸기(pin)"""

    def __init__(self, mode, host="", name=""):
        super().__init__(None, Qt.WindowType.WindowStaysOnTopHint)
        self.mode = mode
        self.setWindowTitle({"join": "팀 참여", "login": "팀 로그인", "pin": "PIN 바꾸기"}[mode])
        form = QFormLayout()
        self.host = QLineEdit(host, placeholderText="예) 192.168.0.10  (호스트 트레이 > 팀스페이스에서 확인)")
        self.code = QLineEdit(placeholderText="숫자 6자리", maxLength=6)
        self.name = QLineEdit(name, maxLength=NAME_MAX)
        self.old_pin = QLineEdit(echoMode=QLineEdit.EchoMode.Password, maxLength=8)
        self.pin = QLineEdit(echoMode=QLineEdit.EchoMode.Password, maxLength=8, placeholderText="숫자 4~8자리")
        self.pin2 = QLineEdit(echoMode=QLineEdit.EchoMode.Password, maxLength=8)
        if mode in ("join", "login"):
            form.addRow("호스트 주소", self.host)
        if mode == "join":
            form.addRow("참여 코드", self.code)
        if mode in ("join", "login"):
            form.addRow("내 이름", self.name)
        if mode == "pin":
            form.addRow("지금 PIN", self.old_pin)
        form.addRow("PIN" if mode != "pin" else "새 PIN", self.pin)
        if mode != "login":
            form.addRow("PIN 확인", self.pin2)
        hint = {
            "join": "참여 코드는 팀 호스트(관리자) PC 트레이에 있습니다.\n"
                    "PIN 은 다른 PC 에서 로그인할 때만 다시 묻습니다.",
            "login": "다른 PC 에서 참여할 때 정한 이름과 PIN 을 입력하세요.\n"
                     "PIN 을 잊었으면 관리자에게 'PIN 초기화'를 부탁한 뒤 '팀 참여'로 다시 들어오세요.",
            "pin": "",
        }[mode]
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        if hint:
            layout.addWidget(QLabel(hint))
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
        if self.mode in ("join", "login") and not normalize_server(self.host.text()):
            return self.error.setText("호스트 주소를 입력하세요.")
        if self.mode == "join" and not self.code.text().strip():
            return self.error.setText("참여 코드를 입력하세요.")
        if self.mode in ("join", "login") and not clean_name(self.name.text()):
            return self.error.setText("이름을 입력하세요.")
        if not valid_pin(self.pin.text()):
            return self.error.setText("PIN 은 숫자 4~8자리입니다.")
        if self.mode != "login" and self.pin.text() != self.pin2.text():
            return self.error.setText("PIN 확인이 맞지 않습니다.")
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
        if status in STATUS_LABELS:
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
        if name in ("join_team", "login_team", "rename", "add_server", "start_host"):
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
    """서버로 가는 모든 요청(fetch, SSE)에 Dogani 식별 헤더 + 내 이름 + 팀원 토큰을 붙인다."""

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

    def restore_position(self):
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
        self.wcfg["pos"] = [self.x(), self.y()]
        self.app.save_later()

    def push_config(self, **values):
        self.view.page().runJavaScript(f"window.App && App.configure({json.dumps(values)})")

    def call(self, fn, *args):
        self.view.page().runJavaScript(f"window.App && App.{fn}({', '.join(json.dumps(a) for a in args)})")


# ---------------------------------------------------------------- app
class DoganiApp(QObject):
    team_result = Signal(int, object)   # 팀 호스트 응답 (작업 스레드 → 메인 스레드)

    def __init__(self, qapp, cfg):
        super().__init__()
        self.qapp = qapp
        self.cfg = cfg
        self.status = {}     # 공간 키 -> 창에서 알려 준 연결 상태
        self.names = {}      # 주소로 추가한 사람 -> 상대가 알려 준 이름
        self.nav = None      # 네비게이션 바 (하나)
        self.panels = {}     # "note" | "cal" -> Overlay
        self.note_label = None   # 할일 창이 보여주는 날짜 {date, title, today}
        self.team_status = "offline"
        self.team_polling = False
        self.save_timer = QTimer(self, singleShot=True, interval=800, timeout=lambda: save_config(self.cfg))
        setup_profile(self)

        self.share = ShareServer(cfg["port"], cfg["todo_dir"], cfg["paused"], cfg["blocked"], cfg["name"])
        self.share.team_only = cfg["team_only"]
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
        out.append({"key": ME, "group": "me", "name": cfg["name"], "server": f"127.0.0.1:{cfg['port']}",
                    "prefix": "/api", "kind": "me", "status": "online" if self.share else "offline",
                    "sub": "" if self.share else "공유 꺼짐"})
        seen = set()
        for m in t.get("members") or []:
            if m["id"] == t.get("id"):
                continue
            addr = m.get("addr", "")
            seen.add(addr)
            key = "m:" + m["id"]
            st = self.status.get(key) or ("online" if m.get("online") and addr else "offline")
            out.append({"key": key, "group": "member", "name": m["name"], "server": addr, "prefix": "/api",
                        "kind": "peer", "status": st if m.get("online") else "offline",
                        "sub": "" if addr else "노트 공유 안 함", "disabled": not addr})
        for addr in cfg["servers"]:
            if addr in seen:
                continue
            out.append({"key": addr, "group": "other", "name": self.names.get(addr) or addr.removesuffix(f":{DEFAULT_PORT}"),
                        "server": addr, "prefix": "/api", "kind": "peer",
                        "status": self.status.get(addr, "offline"), "sub": addr})
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
        if name and key in self.cfg["servers"] and self.names.get(key) != name:
            self.names[key] = name
            self.push_spaces()

    # ------------------------------------------------------------ team (client)
    def apply_team_auth(self):
        if self.share:
            t = self.cfg["team"]
            self.share.auth.update(t.get("pub") if t.get("token") else "", t.get("members"), joined=bool(t.get("token")))
            self.share.team_only = self.cfg["team_only"]
            self.share.name = self.cfg["name"]

    def poll_team(self):
        t = self.cfg["team"]
        if not t.get("token") or self.team_polling:
            return
        self.team_polling = True
        host, token, name = t["host"], t["token"], self.cfg["name"]
        port = self.cfg["port"] if self.share else 0

        def work():
            status, res = http_json(host, f"/api/team/me?port={port}", token=token, name=name)
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
                self.tray.notify("팀 로그인이 풀렸습니다. (내보내졌거나 PIN 이 초기화됨)\n트레이 > 팀스페이스에서 다시 로그인하세요.")
        else:
            self.team_status = "offline"
        self.apply_team_auth()
        self.save_later()
        self.push_spaces()

    def team_request(self, host, path, body):
        """대화상자에서 쓰는 동기 요청 → (성공 여부, 응답 또는 오류 문구)"""
        status, res = http_json(host, path, body, token=self.cfg["team"].get("token", ""), name=self.cfg["name"])
        if status == 200:
            return True, res
        if status == 0:
            return False, f"{host} 에 연결할 수 없습니다. 주소와 호스트 PC 의 Dogani 가 켜져 있는지 확인하세요."
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

    def join_team(self, mode):
        t = self.cfg["team"]
        dlg = TeamDialog(mode, "" if t.get("host", "").startswith("127.") else t.get("host", ""), self.cfg["name"])

        def handler(d):
            host = normalize_server(d.host.text())
            if mode == "join":
                ok, res = self.team_request(host, "/api/team/join",
                                            {"code": d.code.text().strip(), "name": d.name.text(), "pin": d.pin.text()})
            else:
                ok, res = self.team_request(host, "/api/team/login", {"name": d.name.text(), "pin": d.pin.text()})
            if not ok:
                return res
            self.set_session(host, res)
            return None

        dlg.handler = handler
        if dlg.exec():
            self.tray.notify(f"{self.cfg['team'].get('team') or '팀'}에 {self.cfg['name']}(으)로 들어왔습니다.")

    def change_pin(self):
        dlg = TeamDialog("pin")

        def handler(d):
            ok, res = self.team_request(self.cfg["team"]["host"], "/api/team/profile",
                                        {"pin": d.pin.text(), "old_pin": d.old_pin.text()})
            return None if ok else res

        dlg.handler = handler
        if dlg.exec():
            self.tray.notify("PIN 을 바꿨습니다.")

    def leave_team(self):
        if QMessageBox.question(None, APP_NAME, "이 PC 에서 팀 로그아웃할까요?\n(팀원 목록에서 지워지지는 않습니다. "
                                "나중에 이름 + PIN 으로 다시 로그인할 수 있습니다.)") != QMessageBox.StandardButton.Yes:
            return
        self.cfg["team"] = dict(EMPTY_TEAM)
        if self.cfg["space"] == TEAM or self.cfg["space"].startswith("m:"):
            self.cfg["space"] = ME
        self.apply_team_auth()
        self.push_spaces()

    # ------------------------------------------------------------ team (host)
    def start_host(self, quiet=False):
        if not self.share:
            return
        if not quiet:
            if self.in_team() and QMessageBox.question(
                    None, APP_NAME, "지금 들어가 있는 팀에서 로그아웃하고 이 PC 를 새 팀의 호스트로 만들까요?"
            ) != QMessageBox.StandardButton.Yes:
                return
        try:
            host = TeamHost(TEAM_DIR, self.cfg["name"])
        except OSError as e:
            QMessageBox.warning(None, APP_NAME, f"팀 폴더를 만들 수 없습니다.\n{e}")
            return
        self.share.team = host
        self.cfg["team_host"] = True
        owner = next(m for m in host.members() if m["id"] == host.owner_id)
        self.cfg["name"] = owner["name"]
        self.set_session(f"127.0.0.1:{self.cfg['port']}",
                         {"token": host.owner_token(), "id": host.owner_id, "pub": host.pub,
                          "team": host.name, "name": owner["name"]})
        if not quiet:
            QMessageBox.information(
                None, APP_NAME,
                f"팀 호스트를 열었습니다.\n\n팀원에게 알려 줄 것:\n  호스트 주소  {self.share.ip}\n  참여 코드  {host.code}\n\n"
                "팀원은 트레이 > 팀스페이스 > 팀 참여 에서 입력합니다.\n"
                "이 PC 가 꺼져 있으면 공용 공간을 볼 수 없으니, 켜 두는 시간이 긴 PC 를 추천합니다.")

    def stop_host(self):
        if QMessageBox.question(None, APP_NAME, "팀 호스트를 끌까요?\n팀원들은 공용 공간을 볼 수 없게 됩니다.\n"
                                f"(데이터는 {TEAM_DIR} 에 남아 있어서 다시 켜면 그대로 돌아옵니다.)"
                                ) != QMessageBox.StandardButton.Yes:
            return
        if self.share:
            self.share.team = None
        self.cfg["team_host"] = False
        self.cfg["team"] = dict(EMPTY_TEAM)
        if self.cfg["space"] != ME:
            self.cfg["space"] = ME
        self.apply_team_auth()
        self.push_spaces()

    def rename(self):
        name, ok = QInputDialog.getText(None, "내 이름", "팀원·동료에게 보이는 이름 (최대 20자)",
                                        QLineEdit.EchoMode.Normal, self.cfg["name"])
        name = clean_name(name) if ok else ""
        if not name or name == self.cfg["name"]:
            return
        if self.in_team():
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
            self.tray.notify(f"포트 {self.cfg['port']}이(가) 사용 중이라 내 노트를 공유할 수 없습니다.\n"
                             "예전 DoganiServer 가 켜져 있다면 종료하고 다시 실행하세요.")
        elif not self.cfg["todo_dir"]:
            QTimer.singleShot(500, self.browse_dir)

    # ------------------------------------------------------------ actions
    def browse_dir(self):
        path = QFileDialog.getExistingDirectory(
            None, "todo(데일리 노트) 폴더 선택  예) 선택폴더/2026.11/261102 월.md",
            self.cfg["todo_dir"] or os.path.expanduser("~"))
        if path:
            path = os.path.normpath(path)
            if path != self.cfg["todo_dir"]:
                self.cfg["todo_dir"] = path
                if self.share:
                    self.share.set_root(path)

    def add_server(self):
        text, ok = QInputDialog.getText(
            None, "주소로 노트 추가",
            f"팀원이 아닌 사람의 Dogani 주소 (IP 또는 IP:포트, 기본 포트 {DEFAULT_PORT})\n"
            "팀원은 팀에 들어오면 자동으로 목록에 나옵니다.",
            QLineEdit.EchoMode.Normal, "")
        addr = normalize_server(text) if ok else ""
        if not addr:
            return
        if addr not in self.cfg["servers"]:
            self.cfg["servers"].append(addr)
        self.cfg["space"] = addr
        self.push_spaces()

    def remove_server(self, addr):
        if QMessageBox.question(None, APP_NAME, f"{addr} 노트를 목록에서 삭제할까요?") != QMessageBox.StandardButton.Yes:
            return
        if addr in self.cfg["servers"]:
            self.cfg["servers"].remove(addr)
        self.status.pop(addr, None)
        self.names.pop(addr, None)
        if self.cfg["space"] == addr:
            self.cfg["space"] = ME
        self.push_spaces()

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
        elif name in ("join_team", "login_team"):
            self.join_team("join" if name == "join_team" else "login")
        elif name == "change_pin":
            self.change_pin()
        elif name == "leave_team":
            self.leave_team()
        elif name == "start_host":
            self.start_host()
        elif name == "stop_host":
            self.stop_host()
        elif name == "copy_code" and team:
            QGuiApplication.clipboard().setText(team.code)
        elif name == "renew_code" and team:
            team.renew_code()
            self.tray.notify(f"새 참여 코드: {team.code}\n(이미 들어온 팀원에게는 영향 없음)")
        elif name == "rename_team" and team:
            text, ok = QInputDialog.getText(None, "팀 이름", "공용 공간 이름", QLineEdit.EchoMode.Normal, team.name)
            if ok and clean_name(text):
                team.rename_team(text)
                self.poll_team()
        elif name == "open_team_dir" and team:
            os.startfile(team.notes_dir)
        elif name == "reset_pin" and team:
            team.reset_pin(arg)
            self.tray.notify(f"PIN 을 초기화했습니다. 그 사람은 참여 코드({team.code}) + 같은 이름 + 새 PIN 으로 다시 참여하면 됩니다.")
        elif name == "kick" and team:
            m = next((m for m in team.members() if m["id"] == arg), None)
            if m and QMessageBox.question(None, APP_NAME, f"{m['name']} 님을 팀에서 내보낼까요?") == QMessageBox.StandardButton.Yes:
                team.kick(arg)
                self.poll_team()
        elif name == "add_server":
            self.add_server()
        elif name == "remove_server":
            self.remove_server(arg)
        elif name == "copy_address" and share:
            QGuiApplication.clipboard().setText(share.address)
        elif name == "toggle_pause" and share:
            share.set_paused(not share.paused)
            cfg["paused"] = share.paused
            self.tray.update()
        elif name == "toggle_team_only":
            cfg["team_only"] = not cfg["team_only"]
            self.apply_team_auth()
        elif name in ("block", "unblock") and share:
            (share.hub.block if name == "block" else share.hub.unblock)(arg)
            cfg["blocked"] = sorted(share.hub.snapshot()[1])
        elif name == "open_dir":
            if cfg["todo_dir"] and os.path.isdir(cfg["todo_dir"]):
                os.startfile(cfg["todo_dir"])
        elif name == "browse_dir":
            self.browse_dir()
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
    parser = argparse.ArgumentParser(description="Dogani - 옵시디언 todo 실시간 공유")
    parser.add_argument("--dir", help="내 todo(데일리 노트) 폴더 경로")
    parser.add_argument("--port", type=int, help=f"내 노트 공유 포트 (기본 {DEFAULT_PORT})")
    parser.add_argument("--add", metavar="ADDR", action="append", help="다른 사람 주소 추가 (IP 또는 IP:포트)")
    parser.add_argument("--name", help="내 이름")
    args = parser.parse_args()

    register_ui_scheme()
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)

    mutex = liveupdate.MUTEX = single_instance()
    if mutex is None:
        QMessageBox.information(None, APP_NAME, "Dogani 가 이미 실행 중입니다.\n시스템 트레이를 확인하세요.")
        return

    cfg = load_config()
    if args.dir:
        cfg["todo_dir"] = os.path.abspath(args.dir)
    if args.port:
        cfg["port"] = args.port
    if args.name and clean_name(args.name):
        cfg["name"] = clean_name(args.name)
    for addr in args.add or []:
        addr = normalize_server(addr)
        if addr and addr not in cfg["servers"]:
            cfg["servers"].append(addr)
    save_config(cfg)
    migrate_autostart()

    app = DoganiApp(qapp, cfg)
    app.start()
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()

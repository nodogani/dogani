"""
Dogani - 사내망 옵시디언 todo 실시간 공유 (서버 + 뷰어 통합)

- 내 노트: todo 폴더를 공유 서버로 열고(server.py), 바탕화면 오버레이에서 보고 바로 편집
- 다른 사람 노트: 동료의 Dogani 주소를 추가하면 각각 오버레이(네비게이션 바)가 하나씩 생김 (읽기 전용)
- 시스템 트레이(Win32 기본 메뉴)로 표시 / 공유 / 접속자 / 경로 / 자동 실행 / 종료
"""
import argparse
import ctypes
import json
import os
import socket
import sys
import time
import winreg
from ctypes import wintypes

import pystray
from PIL import Image, ImageDraw
from PySide6.QtCore import QBuffer, QByteArray, QFile, QIODevice, QObject, QPoint, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineScript,
                                      QWebEngineUrlRequestInterceptor, QWebEngineUrlRequestJob, QWebEngineUrlScheme,
                                      QWebEngineUrlSchemeHandler)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QLineEdit, QMessageBox, QVBoxLayout, QWidget

from server import CLIENT_KEY, HOST_HEADER, KEY_HEADER, ShareServer

APP_NAME = "Dogani"
CONFIG_DIR = os.environ.get("DOGANI_HOME") or os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Dogani")
CONFIG_PATH = os.path.join(CONFIG_DIR, "dogani.json")
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))  # exe 패키징 시 _MEIPASS
UI_DIR = os.path.join(BASE_DIR, "ui")
DEFAULT_PORT = 8765
OPACITY_LEVELS = [100, 90, 80, 70, 60]
ME = "me"  # 내 노트 오버레이 키 (다른 사람은 "ip:port")

STATUS_LABELS = {
    "online": "접속 중",
    "offline": "연결 끊김",
    "paused": "공유 일시 중지됨",
    "blocked": "차단됨",
}

DEFAULT_CONFIG = {
    "port": DEFAULT_PORT,
    "todo_dir": "",
    "paused": False,      # 내 노트 공유 일시 중지
    "blocked": [],
    "servers": [],        # 다른 사람 주소 목록 ["192.168.0.10:8765", ...]
    "overlays": {},       # 키(me / 주소) -> {"visible", "pos", "panel"}
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
    return cfg


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
    overlays = {}
    if old_owner or old_server:
        overlays[ME] = {"visible": old_server.get("overlay", True), "pos": old_owner.get("pos"),
                        "panel": old_owner.get("panel")}
    if old_client.get("server"):
        cfg["servers"] = [old_client["server"]]
        overlays[old_client["server"]] = {"visible": old_client.get("visible", True), "pos": old_client.get("pos"),
                                          "panel": old_client.get("panel")}
    cfg["overlays"] = overlays
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
            name = f"{ip} ({c['host']})" if c["host"] else ip
            if not c["active"]:
                name += f"  - 마지막 {time.strftime('%m/%d %H:%M', time.localtime(c['last_seen']))}"
            items.append(Item(name, pystray.Menu(Item("연결 끊고 차단", self.emit("block", ip))),
                              checked=lambda i, a=c["active"]: a > 0, radio=True))
        if blocked:
            if items:
                items.append(pystray.Menu.SEPARATOR)
            for ip in sorted(blocked):
                host = clients.get(ip, {}).get("host", "")
                items.append(Item(f"[차단됨] {ip}" + (f" ({host})" if host else ""),
                                  pystray.Menu(Item("차단 해제", self.emit("unblock", ip)))))
        return items or [Item("(접속 기록 없음)", None, enabled=False)]

    def server_items(self):
        Item = pystray.MenuItem
        app = self.app
        items = []
        for addr in app.cfg["servers"]:
            ov = app.overlays.get(addr)
            label = ov.label if ov and ov.label != addr else addr
            if ov and ov.label != addr:
                label = f"{ov.label} ({addr})"
            label += f"  - {STATUS_LABELS[app.status.get(addr, 'offline')]}"
            items.append(Item(label, pystray.Menu(
                Item("표시", self.emit("toggle_visible", addr), checked=lambda i, a=addr: app.is_visible(a)),
                Item("삭제", self.emit("remove_server", addr)),
            ), checked=lambda i, a=addr: app.is_visible(a)))
        if items:
            items.append(pystray.Menu.SEPARATOR)
        items.append(Item("추가...", self.emit("add_server")))
        return items

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
                Item("접속 IP", pystray.Menu(lambda: self.client_items())),
            )
        else:
            share_label = "공유 꺼짐 (포트 사용 중)"
            share_menu = pystray.Menu(Item(f"포트 {cfg['port']}을(를) 열 수 없습니다", None, enabled=False))
        todo_dir = cfg["todo_dir"] or "(경로 미지정)"
        return [
            Item("내 노트 표시", self.emit("toggle_visible", ME), checked=lambda i: app.is_visible(ME), default=True),
            Item(f"내 노트 {share_label}", share_menu, checked=lambda i: bool(share) and not share.paused, radio=True),
            Item("todo 경로", pystray.Menu(
                Item(todo_dir, self.emit("open_dir")),
                pystray.Menu.SEPARATOR,
                Item("찾아보기...", self.emit("browse_dir")),
            )),
            pystray.Menu.SEPARATOR,
            Item("다른 사람 노트", pystray.Menu(lambda: self.server_items())),
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
            Item("종료", self.emit("quit")),
        ]

    def update(self):
        share = self.app.share
        key = "off" if not share else "paused" if share.paused else "sharing"
        self.icon.icon = make_icon_image(ICON_COLORS[key])
        if share:
            state = "공유 일시 중지" if share.paused else "공유 중"
            self.icon.title = f"Dogani - 내 노트 {share.address} ({state})"
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


# ---------------------------------------------------------------- overlay web view
class Bridge(QObject):
    """JS(app.js) ↔ 파이썬 연결 객체 (오버레이 창마다 하나)."""

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
        self.win.windowHandle().startSystemResize(Qt.Edge.RightEdge | Qt.Edge.BottomEdge)

    @Slot(int, int)
    def setNavSize(self, w, h):
        self.win.set_nav_size(w, h)

    @Slot(bool)
    def setExpanded(self, expanded):
        self.win.set_expanded(expanded)

    @Slot(str)
    def setStatus(self, status):
        if status in STATUS_LABELS:
            self.win.status_changed.emit(self.win.key, status)

    @Slot(str)
    def setName(self, name):
        self.win.label = name or self.win.server

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
    """서버로 가는 모든 요청(fetch, SSE)에 Dogani 식별 헤더를 붙인다."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.host = socket.gethostname().encode("utf-8", "ignore")[:64]

    def interceptRequest(self, info):
        if info.requestUrl().scheme() in ("http", "https"):
            info.setHttpHeader(KEY_HEADER.encode(), CLIENT_KEY.encode())
            info.setHttpHeader(HOST_HEADER.encode(), self.host)


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


def setup_profile(parent):
    """모든 오버레이가 같이 쓰는 기본 프로필에 UI 스킴과 헤더를 한 번만 설치."""
    profile = QWebEngineProfile.defaultProfile()
    parent.scheme_handler = UiSchemeHandler(parent)
    profile.installUrlSchemeHandler(UI_SCHEME, parent.scheme_handler)
    parent.headers = ClientHeaders(parent)
    profile.setUrlRequestInterceptor(parent.headers)
    script = QWebEngineScript()
    script.setName("qwebchannel")
    script.setSourceCode(qwebchannel_js())
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    profile.scripts().insert(script)


class Overlay(QWidget):
    """노트 하나(내 노트 또는 다른 사람)를 보여주는 바탕화면 오버레이 창."""

    status_changed = Signal(str, str)

    def __init__(self, app, key, server, index):
        super().__init__()
        self.app = app
        self.key = key
        self.server = server
        self.label = "내 노트" if key == ME else server
        self.index = index
        self.expanded = False
        self.nav_size = QSize(200, 60)

        self.setWindowTitle(f"Dogani - {self.label}")
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
    def ocfg(self):
        return self.app.overlay_cfg(self.key)

    def js_config(self):
        return {"server": self.server, "theme": self.app.cfg["theme"], "mine": self.key == ME,
                "label": "내 노트" if self.key == ME else ""}

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
        pos = self.ocfg.get("pos")
        if pos and QGuiApplication.screenAt(QPoint(pos[0] + 30, pos[1] + 20)):
            self.move(pos[0], pos[1])
            return
        # 처음 뜨는 창은 화면 오른쪽 위부터 아래로 차례대로
        geo = QGuiApplication.primaryScreen().availableGeometry()
        self.move(geo.right() - 280, geo.top() + 20 + self.index * 56)

    # -- size
    def set_nav_size(self, w, h):
        self.nav_size = QSize(w, h)
        if not self.expanded:
            self.setMinimumSize(0, 0)
            self.resize(self.nav_size)

    def set_expanded(self, expanded):
        self.expanded = expanded
        if expanded:
            w, h = self.ocfg.get("panel") or [440, 640]
            self.setMinimumSize(max(400, self.nav_size.width()), 300)  # 네비게이션 바에 날짜 이동이 들어가므로
            self.resize(max(w, self.nav_size.width()), h)
            self.keep_on_screen()
        else:
            self.setMinimumSize(0, 0)
            self.resize(self.nav_size)

    def keep_on_screen(self):
        screen = QGuiApplication.screenAt(self.pos() + QPoint(30, 20)) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = min(self.x(), geo.right() - self.width() + 1)
        y = min(self.y(), geo.bottom() - self.height() + 1)
        self.move(max(geo.left(), x), max(geo.top(), y))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.expanded:
            self.ocfg["panel"] = [self.width(), self.height()]
            self.app.save_later()

    def moveEvent(self, event):
        super().moveEvent(event)
        self.ocfg["pos"] = [self.x(), self.y()]
        self.app.save_later()

    def push_config(self, **values):
        self.view.page().runJavaScript(f"window.App && App.configure({json.dumps(values)})")


# ---------------------------------------------------------------- app
class DoganiApp(QObject):
    def __init__(self, qapp, cfg):
        super().__init__()
        self.qapp = qapp
        self.cfg = cfg
        self.status = {}    # 키 -> online/offline/...
        self.overlays = {}  # 키 -> Overlay
        self.save_timer = QTimer(self, singleShot=True, interval=800, timeout=lambda: save_config(self.cfg))
        setup_profile(self)

        self.share = ShareServer(cfg["port"], cfg["todo_dir"], cfg["paused"], cfg["blocked"])
        try:
            self.share.start()
        except OSError:
            self.share = None

        self.tray = Tray(self)
        self.tray.command.connect(self.on_command)

    def save_later(self):
        self.save_timer.start()

    def overlay_cfg(self, key):
        return self.cfg["overlays"].setdefault(key, {"visible": True, "pos": None, "panel": None})

    def is_visible(self, key):
        return self.overlay_cfg(key).get("visible", True)

    def server_of(self, key):
        return f"127.0.0.1:{self.cfg['port']}" if key == ME else key

    def keys(self):
        return [ME] + list(self.cfg["servers"])

    def ensure_overlay(self, key):
        ov = self.overlays.get(key)
        if not ov:
            ov = Overlay(self, key, self.server_of(key), self.keys().index(key))
            ov.status_changed.connect(self.on_status)
            self.overlays[key] = ov
        return ov

    def set_visible(self, key, visible):
        self.overlay_cfg(key)["visible"] = visible
        if visible:
            self.ensure_overlay(key).show()
        elif key in self.overlays:
            self.overlays[key].hide()

    def start(self):
        self.tray.update()
        self.tray.start()
        for key in self.keys():
            if self.is_visible(key):
                self.set_visible(key, True)
        if not self.share:
            self.tray.notify(f"포트 {self.cfg['port']}이(가) 사용 중이라 내 노트를 공유할 수 없습니다.\n"
                             "예전 DoganiServer 가 켜져 있다면 종료하고 다시 실행하세요.")
        elif not self.cfg["todo_dir"]:
            QTimer.singleShot(500, self.browse_dir)

    def on_status(self, key, status):
        self.status[key] = status

    # -- actions
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
            None, "다른 사람 노트 추가",
            f"상대방 Dogani 주소를 입력하세요 (IP 또는 IP:포트, 기본 포트 {DEFAULT_PORT})\n"
            "상대방 트레이 > 내 노트 공유 중 > 주소 클릭으로 복사할 수 있습니다.",
            QLineEdit.EchoMode.Normal, "")
        addr = normalize_server(text) if ok else ""
        if not addr or addr == ME:
            return
        if addr not in self.cfg["servers"]:
            self.cfg["servers"].append(addr)
        self.set_visible(addr, True)

    def remove_server(self, addr):
        if QMessageBox.question(None, APP_NAME, f"{addr} 노트를 목록에서 삭제할까요?") != QMessageBox.StandardButton.Yes:
            return
        if addr in self.cfg["servers"]:
            self.cfg["servers"].remove(addr)
        self.cfg["overlays"].pop(addr, None)
        self.status.pop(addr, None)
        ov = self.overlays.pop(addr, None)
        if ov:
            ov.close()
            ov.deleteLater()

    def on_command(self, name, arg):
        cfg = self.cfg
        share = self.share
        if name == "toggle_visible":
            self.set_visible(arg, not self.is_visible(arg))
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
            for ov in self.overlays.values():
                ov.apply_flags()
        elif name == "opacity":
            cfg["opacity"] = arg
            for ov in self.overlays.values():
                ov.setWindowOpacity(arg / 100)
        elif name == "toggle_theme":
            cfg["theme"] = "light" if cfg["theme"] == "dark" else "dark"
            for ov in self.overlays.values():
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
    args = parser.parse_args()

    register_ui_scheme()
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)

    mutex = single_instance()
    if mutex is None:
        QMessageBox.information(None, APP_NAME, "Dogani 가 이미 실행 중입니다.\n시스템 트레이를 확인하세요.")
        return

    cfg = load_config()
    if args.dir:
        cfg["todo_dir"] = os.path.abspath(args.dir)
    if args.port:
        cfg["port"] = args.port
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

"""
todo 공유 클라이언트 (읽기 전용)

- 바탕화면에 반투명 네비게이션 오버레이를 띄우고, 오늘의 노트 / 달력을 펼쳐 볼 수 있다
- 서버의 변경 알림(SSE)을 받아 실시간 갱신
- 시스템 트레이(Win32 기본 메뉴)로 접속 서버 변경 / 표시 옵션 / 자동 실행 / 종료
"""
import argparse
import ctypes
import json
import os
import socket
import sys
import threading
import winreg
from ctypes import wintypes

import pystray
from PIL import Image, ImageDraw
from PySide6.QtCore import QBuffer, QByteArray, QFile, QIODevice, QObject, QPoint, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineScript, QWebEngineUrlRequestInterceptor,
                                      QWebEngineUrlRequestJob, QWebEngineUrlScheme, QWebEngineUrlSchemeHandler)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox, QVBoxLayout, QWidget

APP_NAME = "DoganiClient"
CONFIG_DIR = os.environ.get("DOGANI_HOME") or os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Dogani")
CONFIG_PATH = os.path.join(CONFIG_DIR, "client.json")
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))  # exe 패키징 시 _MEIPASS
UI_DIR = os.path.join(BASE_DIR, "ui")
DEFAULT_PORT = 8765
OPACITY_LEVELS = [100, 90, 80, 70, 60]

# 서버가 클라이언트 프로그램인지 확인하는 헤더 (서버의 CLIENT_KEY 와 같아야 함)
KEY_HEADER = b"X-Dogani-Key"
HOST_HEADER = b"X-Dogani-Host"
CLIENT_KEY = b"dogani-client-v1"

STATUS_LABELS = {
    "online": "서버 접속 중",
    "offline": "서버 연결 끊김",
    "paused": "서버 공유 일시 중지됨",
    "blocked": "서버에서 차단됨",
}

DEFAULT_CONFIG = {
    "server": "",
    "pos": None,          # [x, y]
    "panel": [440, 640],  # 펼쳤을 때 창 크기
    "on_top": False,      # False: 바탕화면에 고정 (Win+D 에도 남아 있음)
    "opacity": 100,
    "theme": "light",
    "visible": True,
}


# ---------------------------------------------------------------- config
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def normalize_server(text):
    text = text.strip().removeprefix("http://").rstrip("/")
    if not text:
        return ""
    if ":" not in text:
        text = f"{text}:{DEFAULT_PORT}"
    return text


# ---------------------------------------------------------------- autostart
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


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
def make_icon_image(online):
    color = (47, 128, 237) if online else (150, 150, 150)
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
        self.icon = FreshMenuIcon(APP_NAME, make_icon_image(False), "todo 클라이언트")
        self.icon.menu = pystray.Menu(lambda: self.build_menu())

    def emit(self, name, arg=None):
        return lambda icon, item: self.command.emit(name, arg)

    def build_menu(self):
        Item = pystray.MenuItem
        cfg = self.app.cfg
        status = self.app.status
        return [
            Item(STATUS_LABELS[status], pystray.Menu(
                Item(cfg["server"] or "(서버 미지정)", None, enabled=False),
                pystray.Menu.SEPARATOR,
                Item("접속 서버 변경...", self.emit("change_server")),
            ), checked=lambda i: status == "online", radio=True),
            Item("오버레이 표시", self.emit("toggle_visible"), checked=lambda i: cfg["visible"], default=True),
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

    def set_status(self, status):
        self.icon.icon = make_icon_image(status == "online")
        self.icon.title = f"todo 클라이언트 - {self.app.cfg['server']} ({STATUS_LABELS[status]})"

    def start(self):
        self.icon.run_detached()
        self.icon.visible = True

    def stop(self):
        self.icon.stop()


# ---------------------------------------------------------------- overlay
class Bridge(QObject):
    """JS(app.js) ↔ 파이썬 연결 객체."""

    def __init__(self, win):
        super().__init__()
        self.win = win

    @Slot(result=str)
    def getConfig(self):
        return json.dumps({"server": self.win.cfg["server"], "theme": self.win.cfg["theme"]})

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
            self.win.status_changed.emit(status)

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
    """서버로 가는 모든 요청(fetch, SSE)에 클라이언트 식별 헤더를 붙인다."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.host = socket.gethostname().encode("utf-8", "ignore")[:64]

    def interceptRequest(self, info):
        if info.requestUrl().scheme() in ("http", "https"):
            info.setHttpHeader(KEY_HEADER, CLIENT_KEY)
            info.setHttpHeader(HOST_HEADER, self.host)


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


class Overlay(QWidget):
    status_changed = Signal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.expanded = False
        self.nav_size = QSize(200, 48)
        self.save_timer = QTimer(self, singleShot=True, interval=800, timeout=lambda: save_config(self.cfg))

        self.setWindowTitle("Dogani")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.apply_flags()

        self.view = QWebEngineView(self)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        page = Page(self.view)
        page.setBackgroundColor(Qt.GlobalColor.transparent)
        self.view.setPage(page)
        self.headers = ClientHeaders(self)
        page.setUrlRequestInterceptor(self.headers)
        self.scheme_handler = UiSchemeHandler(self)
        profile = page.profile()
        if not profile.urlSchemeHandler(UI_SCHEME):
            profile.installUrlSchemeHandler(UI_SCHEME, self.scheme_handler)

        script = QWebEngineScript()
        script.setName("qwebchannel")
        script.setSourceCode(qwebchannel_js())
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        page.scripts().insert(script)

        self.bridge = Bridge(self)
        self.channel = QWebChannel(page)
        self.channel.registerObject("bridge", self.bridge)
        page.setWebChannel(self.channel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

        self.setWindowOpacity(cfg["opacity"] / 100)
        self.resize(self.nav_size)
        self.restore_position()
        # 탐색기 재시작 등으로 바탕화면 창이 바뀌면 다시 고정
        self.pin_timer = QTimer(self, interval=5000, timeout=self.apply_pin)
        self.pin_timer.start()
        self.view.load(QUrl("dogani://ui/index.html"))

    # -- window flags
    def apply_flags(self):
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self.cfg["on_top"]:
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
        target = None if self.cfg["on_top"] else find_desktop_window()
        if (get_owner(hwnd) or None) != (target or None):
            set_owner(hwnd, target)

    def restore_position(self):
        pos = self.cfg.get("pos")
        if pos and QGuiApplication.screenAt(QPoint(pos[0] + 20, pos[1] + 10)):
            self.move(pos[0], pos[1])
            return
        geo = QGuiApplication.primaryScreen().availableGeometry()
        self.move(geo.right() - 260, geo.top() + 20)

    # -- size
    def set_nav_size(self, w, h):
        self.nav_size = QSize(w, h)
        if not self.expanded:
            self.setMinimumSize(0, 0)
            self.resize(self.nav_size)

    def set_expanded(self, expanded):
        self.expanded = expanded
        if expanded:
            w, h = self.cfg["panel"]
            self.setMinimumSize(max(300, self.nav_size.width()), 280)
            self.resize(max(w, self.nav_size.width()), h)
            self.keep_on_screen()
        else:
            self.setMinimumSize(0, 0)
            self.resize(self.nav_size)

    def keep_on_screen(self):
        screen = QGuiApplication.screenAt(self.pos() + QPoint(20, 10)) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = min(self.x(), geo.right() - self.width() + 1)
        y = min(self.y(), geo.bottom() - self.height() + 1)
        self.move(max(geo.left(), x), max(geo.top(), y))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.expanded:
            self.cfg["panel"] = [self.width(), self.height()]
            self.save_timer.start()

    def moveEvent(self, event):
        super().moveEvent(event)
        self.cfg["pos"] = [self.x(), self.y()]
        self.save_timer.start()

    # -- push config to JS
    def push_config(self, **values):
        self.view.page().runJavaScript(f"window.App && App.configure({json.dumps(values)})")


# ---------------------------------------------------------------- app
class ClientApp(QObject):
    def __init__(self, qapp, cfg):
        super().__init__()
        self.qapp = qapp
        self.cfg = cfg
        self.status = "offline"

        self.overlay = Overlay(cfg)
        self.overlay.status_changed.connect(self.on_status)
        self.tray = Tray(self)
        self.tray.command.connect(self.on_command)

    def start(self):
        self.tray.start()
        if self.cfg["visible"]:
            self.overlay.show()
        if not self.cfg["server"]:
            QTimer.singleShot(300, self.change_server)

    def on_status(self, status):
        self.status = status
        self.tray.set_status(status)

    def change_server(self):
        text, ok = QInputDialog.getText(
            None, "접속 서버 변경",
            f"서버 주소를 입력하세요 (IP 또는 IP:포트, 기본 포트 {DEFAULT_PORT})",
            QLineEdit.EchoMode.Normal, self.cfg["server"])
        if ok:
            self.cfg["server"] = normalize_server(text)
            save_config(self.cfg)
            self.overlay.push_config(server=self.cfg["server"])

    def on_command(self, name, arg):
        cfg = self.cfg
        if name == "change_server":
            self.change_server()
        elif name == "toggle_visible":
            cfg["visible"] = not cfg["visible"]
            self.overlay.setVisible(cfg["visible"])
        elif name == "set_on_top":
            cfg["on_top"] = arg
            self.overlay.apply_flags()
        elif name == "opacity":
            cfg["opacity"] = arg
            self.overlay.setWindowOpacity(arg / 100)
        elif name == "toggle_theme":
            cfg["theme"] = "light" if cfg["theme"] == "dark" else "dark"
            self.overlay.push_config(theme=cfg["theme"])
        elif name == "toggle_autostart":
            set_autostart(not is_autostart())
        elif name == "quit":
            save_config(cfg)
            self.tray.stop()
            self.qapp.quit()
            return
        save_config(cfg)


def single_instance():
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\DoganiClientMutex")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def main():
    parser = argparse.ArgumentParser(description="todo 공유 클라이언트")
    parser.add_argument("--server", help=f"서버 주소 (IP 또는 IP:포트, 기본 포트 {DEFAULT_PORT})")
    args = parser.parse_args()

    register_ui_scheme()
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)

    mutex = single_instance()
    if mutex is None:
        QMessageBox.information(None, APP_NAME, "클라이언트가 이미 실행 중입니다.\n시스템 트레이를 확인하세요.")
        return

    cfg = load_config()
    if args.server:
        cfg["server"] = normalize_server(args.server)
        save_config(cfg)

    app = ClientApp(qapp, cfg)
    app.start()
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()

"""
todo 공유 서버

- 옵시디언 데일리 노트 폴더(YYMMDD 요일.md)를 감시하다가 변경되면 SSE로 클라이언트에 푸시
- HTTP API 제공 (사내망 전용). 동료는 읽기 전용, 서버 PC 에서 접속한 클라이언트(서버 주인)는 편집 가능
- 시스템 트레이(Win32 기본 메뉴)로 상태 확인 / 경로 변경 / 자동 실행 / 종료
"""
import argparse
import ctypes
import datetime as dt
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import winreg
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pystray
from PIL import Image, ImageDraw

APP_NAME = "DoganiServer"
CONFIG_DIR = os.environ.get("DOGANI_HOME") or os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Dogani")
CONFIG_PATH = os.path.join(CONFIG_DIR, "server.json")
DEFAULT_PORT = 8765
POLL_INTERVAL = 1.0      # 파일 변경 감시 주기(초)
HEARTBEAT_INTERVAL = 15  # SSE keep-alive 주기(초)

# 파일명 규칙: "261030 금.md" (YYMMDD) 또는 "2026-10-30.md" (옵시디언 기본 데일리노트)
FILE_PATTERNS = [
    (re.compile(r"^(\d{2})(\d{2})(\d{2})(?:[\s_].*)?\.md$", re.I), lambda m: (2000 + int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[\s_].*)?\.md$", re.I), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
]
# 클라이언트 프로그램 식별용 헤더 (보안 인증이 아니라 "브라우저로 그냥 열람"을 막는 용도)
KEY_HEADER = "X-Dogani-Key"
HOST_HEADER = "X-Dogani-Host"
CLIENT_KEY = "dogani-client-v1"
TASK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[(.)\]")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
WEEKDAYS = "월화수목금토일"
MAX_NOTE_BYTES = 2 * 1024 * 1024


# ---------------------------------------------------------------- config
def load_config():
    cfg = {"port": DEFAULT_PORT, "todo_dir": "", "paused": False, "blocked": [], "overlay": True}
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


def overlay_command():
    """서버 주인용 오버레이(클라이언트 --owner 모드) 실행 명령. 못 찾으면 None."""
    frozen = getattr(sys, "frozen", False)
    here = os.path.dirname(os.path.abspath(sys.executable if frozen else __file__))
    if frozen:  # dist\DoganiServer\ 옆의 dist\DoganiClient\
        exe = os.path.normpath(os.path.join(here, "..", "DoganiClient", "DoganiClient.exe"))
        return [exe] if os.path.exists(exe) else None
    script = os.path.normpath(os.path.join(here, "..", "client", "todo_client.py"))
    if not os.path.exists(script):
        return None
    py = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return [py if os.path.exists(py) else sys.executable, script]


# ---------------------------------------------------------------- notes
def parse_note_date(filename):
    for pattern, conv in FILE_PATTERNS:
        m = pattern.match(filename)
        if m:
            try:
                return dt.date(*conv(m))
            except ValueError:
                return None
    return None


def read_text(path):
    with open(path, "rb") as f:
        data = f.read()
    return data.decode("utf-8-sig", errors="replace")


def count_tasks(text):
    """체크박스 항목 수(하위 항목 포함). 공백이 아닌 상태 문자는 모두 완료로 본다(옵시디언 동작)."""
    done = total = 0
    in_fence = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = TASK_RE.match(line)
        if m:
            total += 1
            if m[1] != " ":
                done += 1
    return done, total


class NoteStore:
    """todo 폴더를 주기적으로 스캔해서 날짜→파일 매핑과 완료 통계를 유지."""

    def __init__(self):
        self.lock = threading.Lock()
        self.root = ""
        self.files = {}    # date -> (path, mtime_ns, size)
        self.summary = {}  # date -> (done, total)

    def set_root(self, root):
        with self.lock:
            self.root = root
            self.files = {}
            self.summary = {}

    def _scan(self, root):
        found = {}
        if not root or not os.path.isdir(root):
            return found
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                d = parse_note_date(name)
                if d is None:
                    continue
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                # 같은 날짜 파일이 여러 개면 최근 수정된 것을 사용
                if d not in found or st.st_mtime_ns > found[d][1]:
                    found[d] = (path, st.st_mtime_ns, st.st_size)
        return found

    def refresh(self):
        """변경된 날짜 목록을 반환."""
        with self.lock:
            root = self.root
        found = self._scan(root)
        changed = []
        with self.lock:
            if root != self.root:  # 스캔 도중 경로가 바뀜
                return []
            for d, info in found.items():
                if self.files.get(d) != info:
                    changed.append(d)
                    try:
                        self.summary[d] = count_tasks(read_text(info[0]))
                    except OSError:
                        self.summary[d] = (0, 0)
            for d in list(self.files):
                if d not in found:
                    changed.append(d)
                    self.summary.pop(d, None)
            self.files = found
        return changed

    def latest_before(self, d):
        with self.lock:
            past = [x for x in self.files if x < d]
        return max(past) if past else None

    def note(self, d, fallback=False):
        """fallback=True 이면 해당 날짜 노트가 없을 때 그 이전의 가장 최근 노트를 돌려준다."""
        with self.lock:
            info = self.files.get(d)
        if not info and fallback:
            prev = self.latest_before(d)
            if prev:
                result = self.note(prev)
                result["fallback_for"] = d.isoformat()
                return result
        if not info:
            return {"date": d.isoformat(), "exists": False}
        path, mtime_ns, _ = info
        try:
            text = read_text(path)
        except OSError:
            return {"date": d.isoformat(), "exists": False}
        done, total = count_tasks(text)
        return {
            "date": d.isoformat(),
            "exists": True,
            "title": os.path.splitext(os.path.basename(path))[0],
            "mtime": mtime_ns // 1_000_000,
            "content": text,
            "done": done,
            "total": total,
        }

    def write(self, d, content, base_mtime=None):
        """노트 저장. 파일이 없으면 선택폴더/yyyy.mm/YYMMDD 요일.md 로 새로 만든다.
        base_mtime 이 현재 파일과 다르면(그 사이 옵시디언 등에서 수정됨) 저장하지 않고 None."""
        with self.lock:
            info = self.files.get(d)
            root = self.root
        if info:
            path = info[0]
            if base_mtime is not None and os.stat(path).st_mtime_ns // 1_000_000 != base_mtime:
                return None
        else:
            if not root or not os.path.isdir(root):
                raise OSError("todo 경로가 지정되지 않았습니다")
            folder = os.path.join(root, f"{d.year:04d}.{d.month:02d}")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"{d:%y%m%d} {WEEKDAYS[d.weekday()]}.md")
        tmp = path + ".dogani.tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        os.replace(tmp, path)
        self.refresh()
        return self.note(d)

    def month(self, year, month):
        with self.lock:
            return {
                d.isoformat(): {"done": s[0], "total": s[1]}
                for d, s in self.summary.items()
                if d.year == year and d.month == month
            }


# ---------------------------------------------------------------- clients / SSE
class Hub:
    """SSE 구독자 관리 + 접속 IP 기록 + 차단 목록."""

    def __init__(self, blocked=()):
        self.lock = threading.Lock()
        self.subscribers = []  # (ip, queue)
        self.clients = {}      # ip -> {"active": int, "last_seen": float, "host": str}
        self.blocked = set(blocked)

    def _client(self, ip):
        return self.clients.setdefault(ip, {"active": 0, "last_seen": 0, "host": ""})

    def subscribe(self, ip, host=""):
        q = queue.Queue()
        with self.lock:
            self.subscribers.append((ip, q))
            c = self._client(ip)
            c["active"] += 1
            c["last_seen"] = time.time()
            if host:
                c["host"] = host
        return q

    def unsubscribe(self, ip, q):
        with self.lock:
            self.subscribers = [s for s in self.subscribers if s[1] is not q]
            c = self.clients.get(ip)
            if c:
                c["active"] = max(0, c["active"] - 1)
                c["last_seen"] = time.time()

    def touch(self, ip, host=""):
        with self.lock:
            c = self._client(ip)
            c["last_seen"] = time.time()
            if host:
                c["host"] = host

    def is_blocked(self, ip):
        with self.lock:
            return ip in self.blocked

    def block(self, ip):
        """차단 목록에 넣고 현재 연결을 끊는다."""
        with self.lock:
            self.blocked.add(ip)
            targets = [q for i, q in self.subscribers if i == ip]
        for q in targets:
            q.put(None)

    def unblock(self, ip):
        with self.lock:
            self.blocked.discard(ip)

    def broadcast(self, event, data):
        msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self.lock:
            targets = [q for _, q in self.subscribers]
        for q in targets:
            q.put(msg)

    def snapshot(self):
        with self.lock:
            return {ip: dict(c) for ip, c in self.clients.items()}, set(self.blocked)


class Handler(BaseHTTPRequestHandler):
    server_version = "Dogani/1.0"
    app: "App" = None

    def log_message(self, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", f"{KEY_HEADER}, {HOST_HEADER}, Content-Type")
        self.end_headers()

    def _check(self):
        """공통 접속 검사. 통과하면 (ip, host), 아니면 응답을 보내고 None."""
        ip = self.client_address[0]
        # 클라이언트 프로그램만 접속 허용 (웹브라우저로 주소만 쳐서 보는 것을 막음)
        if self.headers.get(KEY_HEADER) != CLIENT_KEY:
            self._send_json({"error": "client_only", "message": "Dogani 클라이언트로만 볼 수 있습니다."}, 403)
            return None
        if self.app.hub.is_blocked(ip):
            self._send_json({"error": "blocked"}, 403)
            return None
        host = self.headers.get(HOST_HEADER, "")[:64]
        self.app.hub.touch(ip, host)
        return ip, host

    def _is_owner(self, ip):
        """서버 PC 자신에서 온 요청 = 서버 주인 (편집 허용)."""
        return ip in ("127.0.0.1", "::1", self.app.ip)

    def do_POST(self):
        checked = self._check()
        if not checked:
            return
        ip, _ = checked
        url = urlparse(self.path)
        if url.path != "/api/note":
            self._send_json({"error": "not found"}, 404)
            return
        if not self._is_owner(ip):
            self._send_json({"error": "read_only"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_NOTE_BYTES:
                self._send_json({"error": "too_large"}, 413)
                return
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            d = dt.date.fromisoformat(body["date"])
            content = body["content"]
            if not isinstance(content, str):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            self._send_json({"error": "bad request"}, 400)
            return
        try:
            note = self.app.store.write(d, content, body.get("base_mtime"))
        except OSError as e:
            self._send_json({"error": "write_failed", "message": str(e)}, 500)
            return
        if note is None:
            self._send_json({"error": "conflict"}, 409)
            return
        self.app.hub.broadcast("changed", {"dates": [d.isoformat()]})
        self._send_json(note)

    def do_GET(self):
        checked = self._check()
        if not checked:
            return
        ip, host = checked
        app = self.app

        url = urlparse(self.path)
        qs = parse_qs(url.query)
        try:
            if url.path == "/api/info":
                self._send_json({"app": APP_NAME, "today": dt.date.today().isoformat(), "paused": app.paused,
                                 "writable": self._is_owner(ip)})
            elif url.path == "/api/events":
                self._stream(ip, host)
            elif app.paused:
                self._send_json({"error": "paused"}, 503)
            elif url.path == "/api/note":
                d = dt.date.fromisoformat(qs.get("date", [dt.date.today().isoformat()])[0])
                self._send_json(app.store.note(d, fallback=qs.get("fallback", ["0"])[0] == "1"))
            elif url.path == "/api/month":
                y, m = map(int, qs["ym"][0].split("-"))
                self._send_json({"ym": f"{y:04d}-{m:02d}", "days": app.store.month(y, m)})
            else:
                self._send_json({"error": "not found"}, 404)
        except (KeyError, ValueError):
            self._send_json({"error": "bad request"}, 400)

    def _stream(self, ip, host):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = self.app.hub.subscribe(ip, host)
        try:
            hello = json.dumps({"paused": self.app.paused})
            self.wfile.write(f"retry: 3000\nevent: hello\ndata: {hello}\n\n".encode("utf-8"))
            self.wfile.flush()
            while not self.server.stopping:
                try:
                    msg = q.get(timeout=HEARTBEAT_INTERVAL)
                except queue.Empty:
                    msg = ": ping\n\n"
                if msg is None:  # 차단됨
                    break
                self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
        except (OSError, ConnectionError):
            pass
        finally:
            self.app.hub.unsubscribe(ip, q)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    stopping = False


# ---------------------------------------------------------------- helpers
def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # 실제 패킷은 나가지 않음
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def message_box(text, title=APP_NAME, error=False):
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)


def ask_directory(initial):
    """윈도우 기본 폴더 선택 대화상자 (SHBrowseForFolder). tkinter 없이 동작해서 exe 에서도 문제 없음."""
    from ctypes import wintypes

    ole32, shell32, user32 = ctypes.windll.ole32, ctypes.windll.shell32, ctypes.windll.user32
    BFFCALLBACK = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, ctypes.c_uint, wintypes.LPARAM, wintypes.LPARAM)

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [("hwndOwner", wintypes.HWND), ("pidlRoot", ctypes.c_void_p),
                    ("pszDisplayName", wintypes.LPWSTR), ("lpszTitle", wintypes.LPCWSTR),
                    ("ulFlags", ctypes.c_uint), ("lpfn", BFFCALLBACK),
                    ("lParam", wintypes.LPARAM), ("iImage", ctypes.c_int)]

    BIF_RETURNONLYFSDIRS, BIF_NEWDIALOGSTYLE = 0x1, 0x40
    BFFM_INITIALIZED, BFFM_SETSELECTIONW = 1, 0x467
    user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, ctypes.c_wchar_p]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    start = initial if initial and os.path.isdir(initial) else os.path.expanduser("~")

    def on_event(hwnd, msg, lparam, data):
        if msg == BFFM_INITIALIZED:
            user32.SendMessageW(hwnd, BFFM_SETSELECTIONW, 1, start)
            user32.SetForegroundWindow(hwnd)
        return 0

    callback = BFFCALLBACK(on_event)
    display = ctypes.create_unicode_buffer(260)
    info = BROWSEINFOW(None, None, ctypes.cast(display, wintypes.LPWSTR), "todo(데일리 노트) 폴더 선택  예) 선택폴더/2026.11/261102 월.md",
                       BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE, callback, 0, 0)
    ole32.CoInitializeEx(None, 0x2)  # 이 스레드에서 COM 사용 (apartment threaded)
    try:
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(info))
        if not pidl:
            return ""
        path = ctypes.create_unicode_buffer(32768)
        ok = shell32.SHGetPathFromIDListW(pidl, path)
        ole32.CoTaskMemFree(pidl)
        return os.path.normpath(path.value) if ok else ""
    finally:
        ole32.CoUninitialize()


ICON_RUNNING = (46, 160, 67)
ICON_PAUSED = (230, 145, 30)


def make_icon_image(color=ICON_RUNNING):
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


# ---------------------------------------------------------------- app
class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = NoteStore()
        self.store.set_root(cfg["todo_dir"])
        self.hub = Hub(cfg["blocked"])
        self.paused = cfg["paused"]
        self.ip = local_ip()
        self.running = True
        self.icon = None
        self.overlay_proc = None

        Handler.app = self
        self.httpd = Server(("0.0.0.0", cfg["port"]), Handler)

    # -- background
    def watch_loop(self):
        while self.running:
            changed = self.store.refresh()
            if changed:
                self.hub.broadcast("changed", {"dates": sorted(d.isoformat() for d in changed)})
            time.sleep(POLL_INTERVAL)

    # -- tray actions
    def copy_address(self, icon, item):
        subprocess.run("clip", input=f"{self.ip}:{self.cfg['port']}".encode(), shell=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)

    def open_dir(self, icon, item):
        if self.cfg["todo_dir"] and os.path.isdir(self.cfg["todo_dir"]):
            os.startfile(self.cfg["todo_dir"])

    def browse_dir(self, icon=None, item=None):
        def run():
            path = ask_directory(self.cfg["todo_dir"])
            if path and path != self.cfg["todo_dir"]:
                self.cfg["todo_dir"] = path
                save_config(self.cfg)
                self.store.set_root(path)
                self.hub.broadcast("reload", {})
        threading.Thread(target=run, daemon=True).start()

    def toggle_pause(self, icon, item):
        self.paused = not self.paused
        self.cfg["paused"] = self.paused
        save_config(self.cfg)
        self.hub.broadcast("status", {"paused": self.paused})
        self.update_icon()

    def block(self, ip):
        self.hub.block(ip)
        self.cfg["blocked"] = sorted(self.hub.snapshot()[1])
        save_config(self.cfg)

    def unblock(self, ip):
        self.hub.unblock(ip)
        self.cfg["blocked"] = sorted(self.hub.snapshot()[1])
        save_config(self.cfg)

    def update_icon(self):
        if self.icon:
            self.icon.icon = make_icon_image(ICON_PAUSED if self.paused else ICON_RUNNING)
            self.icon.title = self.tooltip()

    def tooltip(self):
        state = "공유 일시 중지" if self.paused else "실행 중"
        return f"todo 서버 - {self.ip}:{self.cfg['port']} ({state})"

    # -- 내 오버레이 (서버 주인은 클라이언트를 따로 안 켜도 같은 화면을 보고 편집)
    def start_overlay(self):
        if self.overlay_proc and self.overlay_proc.poll() is None:
            return
        cmd = overlay_command()
        if not cmd:
            if self.icon:
                self.icon.notify("클라이언트(DoganiClient)를 찾을 수 없어 오버레이를 띄우지 못했습니다.", APP_NAME)
            return
        self.overlay_proc = subprocess.Popen(cmd + ["--owner", str(self.cfg["port"]), "--parent", str(os.getpid())],
                                             creationflags=subprocess.CREATE_NO_WINDOW)

    def stop_overlay(self):
        if self.overlay_proc and self.overlay_proc.poll() is None:
            self.overlay_proc.terminate()
        self.overlay_proc = None

    def toggle_overlay(self, icon, item):
        self.cfg["overlay"] = not self.cfg["overlay"]
        save_config(self.cfg)
        if self.cfg["overlay"]:
            self.start_overlay()
        else:
            self.stop_overlay()

    def toggle_autostart(self, icon, item):
        set_autostart(not is_autostart())

    def quit(self, icon, item):
        self.running = False
        self.stop_overlay()
        self.httpd.stopping = True
        threading.Thread(target=self.httpd.shutdown, daemon=True).start()
        icon.stop()

    # -- menu
    @staticmethod
    def ip_action(func, ip):
        # pystray 는 메뉴 콜백의 인자 개수(0~2개)를 검사하므로 기본값 인자(ip=ip)를 쓰면 안 된다
        return lambda icon, item: func(ip)

    def client_items(self):
        Item = pystray.MenuItem
        clients, blocked = self.hub.snapshot()
        items = []
        order = sorted(clients.items(), key=lambda kv: (kv[1]["active"] == 0, -kv[1]["last_seen"]))
        for ip, c in order:
            if ip in blocked:
                continue
            name = f"{ip} ({c['host']})" if c["host"] else ip
            if not c["active"]:
                name += f"  - 마지막 {time.strftime('%m/%d %H:%M', time.localtime(c['last_seen']))}"
            items.append(Item(name, pystray.Menu(
                Item("연결 끊고 차단", self.ip_action(self.block, ip)),
            ), checked=lambda i, a=c["active"]: a > 0, radio=True))
        if blocked:
            if items:
                items.append(pystray.Menu.SEPARATOR)
            for ip in sorted(blocked):
                host = clients.get(ip, {}).get("host", "")
                items.append(Item(f"[차단됨] {ip}" + (f" ({host})" if host else ""), pystray.Menu(
                    Item("차단 해제", self.ip_action(self.unblock, ip)),
                )))
        return items or [Item("(접속 기록 없음)", None, enabled=False)]

    def build_menu(self):
        Item = pystray.MenuItem
        addr = f"{self.ip}:{self.cfg['port']}"
        todo_dir = self.cfg["todo_dir"] or "(경로 미지정)"
        return pystray.Menu(
            Item("서버 일시 중지됨" if self.paused else "서버 실행 중", pystray.Menu(
                Item(f"{addr}  (클릭 시 복사)", self.copy_address),
            ), checked=lambda i: not self.paused, radio=True),
            Item("공유 일시 중지", self.toggle_pause, checked=lambda i: self.paused),
            Item("내 오버레이 표시 (편집 가능)", self.toggle_overlay, checked=lambda i: self.cfg["overlay"]),
            Item("접속 IP", pystray.Menu(lambda: self.client_items())),
            Item("todo 경로", pystray.Menu(
                Item(todo_dir, self.open_dir),
                pystray.Menu.SEPARATOR,
                Item("찾아보기...", self.browse_dir),
            )),
            Item("Windows 시작 시 자동 실행", self.toggle_autostart, checked=lambda i: is_autostart()),
            pystray.Menu.SEPARATOR,
            Item("서버 종료", self.quit),
        )

    def run(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self.watch_loop, daemon=True).start()

        self.icon = FreshMenuIcon(APP_NAME, make_icon_image(ICON_PAUSED if self.paused else ICON_RUNNING), self.tooltip())
        self.icon.menu = pystray.Menu(lambda: list(self.build_menu().items))

        def setup(icon):
            icon.visible = True
            if self.cfg["overlay"]:
                self.start_overlay()
            if not self.cfg["todo_dir"]:
                icon.notify("todo 폴더를 선택해주세요.", APP_NAME)
                self.browse_dir()

        self.icon.run(setup)


def main():
    parser = argparse.ArgumentParser(description="todo 공유 서버")
    parser.add_argument("--dir", help="todo(데일리 노트) 폴더 경로")
    parser.add_argument("--port", type=int, help=f"포트 (기본 {DEFAULT_PORT})")
    args = parser.parse_args()

    cfg = load_config()
    if args.dir:
        cfg["todo_dir"] = os.path.abspath(args.dir)
    if args.port:
        cfg["port"] = args.port
    save_config(cfg)

    try:
        app = App(cfg)
    except OSError as e:
        message_box(f"포트 {cfg['port']}을(를) 열 수 없습니다.\n이미 서버가 실행 중인지 확인하세요.\n\n{e}", error=True)
        sys.exit(1)
    app.run()


if __name__ == "__main__":
    main()

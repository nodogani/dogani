"""
내 노트 공유 서버 (dogani.py 안에서 스레드로 실행)

- 옵시디언 데일리 노트 폴더(YYMMDD 요일.md)를 감시하다가 변경되면 SSE로 접속자에게 푸시
- HTTP API (사내망 전용). 다른 사람은 읽기 전용, 같은 PC(나)에서 온 요청만 편집 가능
- 공유 일시 중지 중에도 나는 계속 보고 편집할 수 있다
"""
import datetime as dt
import json
import os
import queue
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import schedule

POLL_INTERVAL = 1.0      # 파일 변경 감시 주기(초)
HEARTBEAT_INTERVAL = 15  # SSE keep-alive 주기(초)

# 파일명 규칙: "261030 금.md" (YYMMDD) 또는 "2026-10-30.md" (옵시디언 기본 데일리노트)
FILE_PATTERNS = [
    (re.compile(r"^(\d{2})(\d{2})(\d{2})(?:[\s_].*)?\.md$", re.I), lambda m: (2000 + int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[\s_].*)?\.md$", re.I), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
]
# Dogani 프로그램 식별용 헤더 (보안 인증이 아니라 "브라우저로 그냥 열람"을 막는 용도)
KEY_HEADER = "X-Dogani-Key"
HOST_HEADER = "X-Dogani-Host"
CLIENT_KEY = "dogani-client-v1"
TASK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[(.)\]")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
WEEKDAYS = "월화수목금토일"
MAX_NOTE_BYTES = 2 * 1024 * 1024


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
        self.blank = set()       # 내용이 빈 노트 날짜 (어제 노트 복사 대상에서 뺌)
        self.sched_files = {}    # path -> (mtime_ns, size)
        self.events = []         # 일정 파일에서 읽은 일정
        self.sched_changed = False

    def set_root(self, root):
        with self.lock:
            self.root = root
            self.files = {}
            self.summary = {}
            self.blank = set()
            self.sched_files = {}
            self.events = []

    def has_root(self):
        with self.lock:
            return bool(self.root) and os.path.isdir(self.root)

    def _scan(self, root):
        found, sched = {}, {}
        if not root or not os.path.isdir(root):
            return found, sched
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if schedule.is_schedule_file(name):
                    path = os.path.join(dirpath, name)
                    try:
                        st = os.stat(path)
                        sched[path] = (st.st_mtime_ns, st.st_size)
                    except OSError:
                        pass
                    continue
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
        return found, sched

    def refresh(self):
        """변경된 날짜 목록을 반환."""
        with self.lock:
            root = self.root
        found, sched = self._scan(root)
        events = None
        if sched != self.sched_files:
            events = []
            for path in sorted(sched):
                try:
                    events += schedule.parse(read_text(path))
                except OSError:
                    pass
            events.sort(key=schedule.sort_key)
        changed = []
        with self.lock:
            if root != self.root:  # 스캔 도중 경로가 바뀜
                return []
            for d, info in found.items():
                if self.files.get(d) != info:
                    changed.append(d)
                    try:
                        text = read_text(info[0])
                        self.summary[d] = count_tasks(text)
                        self.blank.discard(d) if text.strip() else self.blank.add(d)
                    except OSError:
                        self.summary[d] = (0, 0)
            for d in list(self.files):
                if d not in found:
                    changed.append(d)
                    self.summary.pop(d, None)
                    self.blank.discard(d)
            self.files = found
            if events is not None:
                self.sched_files = sched
                self.events = events
                self.sched_changed = True
        return changed

    def pop_schedule_changed(self):
        with self.lock:
            changed, self.sched_changed = self.sched_changed, False
            return changed

    def schedule(self):
        with self.lock:
            return list(self.events)

    def prev_note(self, d):
        """d 이전에 있는 가장 최근 노트(빈 노트 제외) 날짜 (어제 노트 복사용)"""
        with self.lock:
            earlier = [x for x in self.files if x < d and x not in self.blank]
        return max(earlier).isoformat() if earlier else None

    def note(self, d):
        with self.lock:
            info = self.files.get(d)
        if not info:
            return {"date": d.isoformat(), "exists": False, "no_dir": not self.has_root(), "prev": self.prev_note(d)}
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
            "prev": self.prev_note(d) if not text.strip() else None,
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
        self.clients = {}      # ip -> {"active": int, "last_seen": float, "host": str}  (나 자신은 기록 안 함)
        self.blocked = set(blocked)

    def _client(self, ip):
        return self.clients.setdefault(ip, {"active": 0, "last_seen": 0, "host": ""})

    def subscribe(self, ip, host="", record=True):
        q = queue.Queue()
        with self.lock:
            self.subscribers.append((ip, q))
            if record:
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

    def broadcast(self, event, data_for):
        """data_for: dict 또는 ip -> dict 함수 (접속자마다 다른 내용을 보낼 때)."""
        with self.lock:
            targets = list(self.subscribers)
        for ip, q in targets:
            data = data_for(ip) if callable(data_for) else data_for
            q.put(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")

    def snapshot(self):
        with self.lock:
            return {ip: dict(c) for ip, c in self.clients.items()}, set(self.blocked)


class Handler(BaseHTTPRequestHandler):
    server_version = "Dogani/1.0"
    share: "ShareServer" = None

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
        """공통 접속 검사. 통과하면 (ip, host, owner), 아니면 응답을 보내고 None."""
        ip = self.client_address[0]
        # Dogani 프로그램만 접속 허용 (웹브라우저로 주소만 쳐서 보는 것을 막음)
        if self.headers.get(KEY_HEADER) != CLIENT_KEY:
            self._send_json({"error": "client_only", "message": "Dogani 로만 볼 수 있습니다."}, 403)
            return None
        owner = self.share.is_owner(ip)
        if not owner and self.share.hub.is_blocked(ip):
            self._send_json({"error": "blocked"}, 403)
            return None
        host = self.headers.get(HOST_HEADER, "")[:64]
        if not owner:
            self.share.hub.touch(ip, host)
        return ip, host, owner

    def do_POST(self):
        checked = self._check()
        if not checked:
            return
        _, _, owner = checked
        url = urlparse(self.path)
        if url.path != "/api/note":
            self._send_json({"error": "not found"}, 404)
            return
        if not owner:
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
        share = self.share
        try:
            note = share.store.write(d, content, body.get("base_mtime"))
        except OSError as e:
            self._send_json({"error": "write_failed", "message": str(e)}, 500)
            return
        if note is None:
            self._send_json({"error": "conflict"}, 409)
            return
        share.hub.broadcast("changed", {"dates": [d.isoformat()]})
        self._send_json(note)

    def do_GET(self):
        checked = self._check()
        if not checked:
            return
        ip, host, owner = checked
        share = self.share
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        try:
            if url.path == "/api/info":
                self._send_json({"app": "Dogani", "name": share.name, "today": dt.date.today().isoformat(),
                                 "paused": share.paused and not owner, "writable": owner})
            elif url.path == "/api/events":
                self._stream(ip, host, owner)
            elif share.paused and not owner:
                self._send_json({"error": "paused"}, 503)
            elif url.path == "/api/note":
                d = dt.date.fromisoformat(qs.get("date", [dt.date.today().isoformat()])[0])
                self._send_json(share.store.note(d))
            elif url.path == "/api/schedule":
                self._send_json({"events": share.store.schedule()})
            elif url.path == "/api/month":
                y, m = map(int, qs["ym"][0].split("-"))
                self._send_json({"ym": f"{y:04d}-{m:02d}", "days": share.store.month(y, m)})
            else:
                self._send_json({"error": "not found"}, 404)
        except (KeyError, ValueError):
            self._send_json({"error": "bad request"}, 400)

    def _stream(self, ip, host, owner):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        share = self.share
        q = share.hub.subscribe(ip, host, record=not owner)
        try:
            hello = json.dumps({"paused": share.paused and not owner})
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
            share.hub.unsubscribe(ip, q)


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    stopping = False


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # 실제 패킷은 나가지 않음
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class ShareServer:
    """내 노트 HTTP 서버 + 폴더 감시. start() 가 포트를 못 열면 OSError."""

    def __init__(self, port, todo_dir, paused=False, blocked=()):
        self.port = port
        self.store = NoteStore()
        self.store.set_root(todo_dir)
        self.hub = Hub(blocked)
        self.paused = paused
        self.ip = local_ip()
        self.name = socket.gethostname()
        self.running = False
        self.httpd = None

    @property
    def address(self):
        return f"{self.ip}:{self.port}"

    def is_owner(self, ip):
        """같은 PC 에서 온 요청 = 나 (편집 허용, 공유 일시 중지·차단 무시)."""
        return ip in ("127.0.0.1", "::1", self.ip)

    def start(self):
        Handler.share = self
        self.httpd = _HTTPServer(("0.0.0.0", self.port), Handler)
        self.running = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self._watch_loop, daemon=True).start()

    def stop(self):
        self.running = False
        if self.httpd:
            self.httpd.stopping = True
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    def _watch_loop(self):
        while self.running:
            changed = self.store.refresh()
            if changed:
                self.hub.broadcast("changed", {"dates": sorted(d.isoformat() for d in changed)})
            if self.store.pop_schedule_changed():
                self.hub.broadcast("schedule", {})
            time.sleep(POLL_INTERVAL)

    def set_root(self, path):
        self.store.set_root(path)
        self.hub.broadcast("reload", {})

    def set_paused(self, paused):
        self.paused = paused
        self.hub.broadcast("status", lambda ip: {"paused": paused and not self.is_owner(ip)})

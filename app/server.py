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
from urllib.parse import parse_qs, unquote, urlparse

import schedule

from team import NAME_MAX, TeamAuth, TeamError

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
TOKEN_HEADER = "X-Dogani-Token"   # 팀원 토큰 (team.py)
USER_HEADER = "X-Dogani-User"     # 표시 이름 (URL 인코딩)
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

    def sched_doc_path(self):
        """편집할 일정 파일: 있으면 첫 번째(올해 것 우선), 없으면 선택폴더/일정.md"""
        with self.lock:
            paths = sorted(self.sched_files)
            root = self.root
        year = f"{dt.date.today().year}"
        for p in paths:
            if os.path.basename(p).startswith(year):
                return p
        if paths:
            return paths[0]
        return os.path.join(root, "일정.md") if root and os.path.isdir(root) else ""

    def sched_doc(self):
        path = self.sched_doc_path()
        if not path:
            return {"exists": False, "no_dir": True}
        if not os.path.exists(path):
            return {"exists": False, "title": os.path.splitext(os.path.basename(path))[0]}
        st = os.stat(path)
        return {"exists": True, "title": os.path.splitext(os.path.basename(path))[0],
                "mtime": st.st_mtime_ns // 1_000_000, "content": read_text(path)}

    def write_sched_doc(self, content, base_mtime=None):
        path = self.sched_doc_path()
        if not path:
            raise OSError("todo 경로가 지정되지 않았습니다")
        if os.path.exists(path) and base_mtime is not None and os.stat(path).st_mtime_ns // 1_000_000 != base_mtime:
            return None
        tmp = path + ".dogani.tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        os.replace(tmp, path)
        return self.sched_doc()

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
        self.clients = {}      # ip -> {"active", "last_seen", "host", "user"}  (나 자신은 기록 안 함)
        self.blocked = set(blocked)

    def _client(self, ip):
        return self.clients.setdefault(ip, {"active": 0, "last_seen": 0, "host": "", "user": ""})

    def subscribe(self, ip, host="", record=True, user=""):
        q = queue.Queue()
        with self.lock:
            self.subscribers.append((ip, q))
            if record:
                c = self._client(ip)
                c["active"] += 1
                c["last_seen"] = time.time()
                if host:
                    c["host"] = host
                if user:
                    c["user"] = user
        return q

    def unsubscribe(self, ip, q):
        with self.lock:
            self.subscribers = [s for s in self.subscribers if s[1] is not q]
            c = self.clients.get(ip)
            if c:
                c["active"] = max(0, c["active"] - 1)
                c["last_seen"] = time.time()

    def touch(self, ip, host="", user=""):
        with self.lock:
            c = self._client(ip)
            c["last_seen"] = time.time()
            if host:
                c["host"] = host
            if user:
                c["user"] = user

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


class Ctx:
    """요청 하나의 접속자 정보"""

    def __init__(self, ip, host, owner, user, member):
        self.ip = ip
        self.host = host        # 상대 PC 이름
        self.owner = owner      # 같은 PC (= 나)
        self.user = user        # 상대가 보낸 표시 이름
        self.member = member    # 토큰으로 확인된 팀원 (id, 이름) 또는 None


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
        self.send_header("Access-Control-Allow-Headers",
                         f"{KEY_HEADER}, {HOST_HEADER}, {TOKEN_HEADER}, {USER_HEADER}, Content-Type")
        self.end_headers()

    def _check(self):
        """공통 접속 검사. 통과하면 Ctx, 아니면 응답을 보내고 None."""
        ip = self.client_address[0]
        # Dogani 프로그램만 접속 허용 (웹브라우저로 주소만 쳐서 보는 것을 막음)
        if self.headers.get(KEY_HEADER) != CLIENT_KEY:
            self._send_json({"error": "client_only", "message": "Dogani 로만 볼 수 있습니다."}, 403)
            return None
        share = self.share
        owner = share.is_owner(ip)
        host = self.headers.get(HOST_HEADER, "")[:64]
        user = unquote(self.headers.get(USER_HEADER, ""))[:NAME_MAX]
        token = self.headers.get(TOKEN_HEADER, "")
        member = None
        if token:
            if share.team:   # 내가 팀 호스트면 직접 확인 (내보낸 사람·다시 초대도 바로 반영)
                mid = share.team.member(token)
                if mid:
                    member = (mid, next((m["name"] for m in share.team.members() if m["id"] == mid), user))
            else:
                member = share.auth.verify(token)
            if member and not member[1]:
                member = (member[0], user)
        if not owner and share.hub.is_blocked(ip):
            self._send_json({"error": "blocked"}, 403)
            return None
        return Ctx(ip, host, owner, user, member)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_NOTE_BYTES:
            raise OverflowError
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    # ------------------------------------------------------------ routing
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        ctx = self._check()
        if not ctx:
            return
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        try:
            if url.path.startswith("/api/team/"):
                self._team(method, url.path[len("/api/team"):], qs, ctx)
            elif url.path.startswith("/api/"):
                self._personal(method, url.path[len("/api"):], qs, ctx)
            else:
                self._send_json({"error": "not found"}, 404)
        except TeamError as e:
            self._send_json({"error": e.code, "message": e.message}, e.status)
        except OverflowError:
            self._send_json({"error": "too_large"}, 413)
        except (KeyError, ValueError, TypeError, AttributeError):
            self._send_json({"error": "bad request"}, 400)

    # ------------------------------------------------------------ 개인 공간 (내 노트)
    def _personal(self, method, path, qs, ctx):
        share = self.share
        if not ctx.owner:
            share.hub.touch(ctx.ip, ctx.host, ctx.member[1] if ctx.member else "")
        if path == "/info":
            return self._send_json({"app": "Dogani", "name": share.name, "pc": share.pc_name,
                                    "today": dt.date.today().isoformat(), "kind": "personal",
                                    "paused": share.paused and not ctx.owner, "writable": ctx.owner})
        # 팀원에게만 공유: 팀에 들어 있고 설정이 켜져 있으면 팀원 토큰이 있어야 볼 수 있다
        if not ctx.owner and share.team_only and (share.auth.active or share.team) and not ctx.member:
            return self._send_json({"error": "team_only"}, 403)
        if path == "/events":
            return self._stream(share.hub, ctx, lambda: share.paused and not ctx.owner, record=not ctx.owner)
        if share.paused and not ctx.owner:
            return self._send_json({"error": "paused"}, 503)
        self._space(method, path, qs, share.store, share.hub, writable=ctx.owner)

    # ------------------------------------------------------------ 팀 공간
    def _team(self, method, path, qs, ctx):
        team = self.share.team
        if not team:
            return self._send_json({"error": "no_team", "message": "이 PC 는 팀 호스트가 아닙니다."}, 404)
        if method == "POST" and path == "/join":
            body = self._body()
            res = team.join(ctx.ip, body.get("code"), body.get("name"))
            res.update(pub=team.pub, team=team.name)
            return self._send_json(res)
        if path == "/hello":   # 참여 전에 주소가 맞는지 확인용
            return self._send_json({"app": "Dogani", "team": team.name, "host": self.share.name})
        mid = ctx.member[0] if ctx.member else (team.owner_id if ctx.owner else None)
        if not mid:
            return self._send_json({"error": "not_member", "message": "팀원만 볼 수 있습니다."}, 401)
        if path == "/me":
            # 팀원 PC 가 주기적으로 보내는 신호: 내 노트 주소를 알리고 팀원 목록·공개키를 받는다
            port = int(qs.get("port", ["0"])[0] or 0)
            ip = self.share.ip if ctx.owner else ctx.ip
            team.touch(mid, f"{ip}:{port}" if port else "")
            members = team.members()
            me = next((m for m in members if m["id"] == mid), None)
            return self._send_json({"me": me, "members": members, "pub": team.pub, "team": team.name})
        if path == "/profile" and method == "POST":
            body = self._body()
            name = team.update_profile(mid, body.get("name"))
            return self._send_json({"name": name})
        if path == "/info":
            return self._send_json({"app": "Dogani", "name": team.name, "kind": "team", "paused": False,
                                    "writable": True, "today": dt.date.today().isoformat()})
        if path == "/events":
            return self._stream(team.hub, ctx, lambda: False, record=False)
        self._space(method, path, qs, team.store, team.hub, writable=True)

    # ------------------------------------------------------------ 노트 공간 공통 (개인·팀)
    def _space(self, method, path, qs, store, hub, writable):
        if method == "POST":
            if not writable:
                return self._send_json({"error": "read_only"}, 403)
            body = self._body()
            content = body["content"]
            if not isinstance(content, str):
                raise ValueError
            try:
                if path == "/note":
                    d = dt.date.fromisoformat(body["date"])
                    note = store.write(d, content, body.get("base_mtime"))
                    event = ("changed", {"dates": [d.isoformat()]})
                elif path == "/sched-doc":
                    note = store.write_sched_doc(content, body.get("base_mtime"))
                    event = ("schedule", {})
                else:
                    return self._send_json({"error": "not found"}, 404)
            except OSError as e:
                return self._send_json({"error": "write_failed", "message": str(e)}, 500)
            if note is None:
                return self._send_json({"error": "conflict"}, 409)
            if event[0] == "schedule":
                store.refresh()
                store.pop_schedule_changed()
            hub.broadcast(*event)
            return self._send_json(note)
        if path == "/note":
            d = dt.date.fromisoformat(qs.get("date", [dt.date.today().isoformat()])[0])
            self._send_json(store.note(d))
        elif path == "/schedule":
            self._send_json({"events": store.schedule()})
        elif path == "/sched-doc":
            self._send_json(store.sched_doc())
        elif path == "/month":
            y, m = map(int, qs["ym"][0].split("-"))
            self._send_json({"ym": f"{y:04d}-{m:02d}", "days": store.month(y, m)})
        else:
            self._send_json({"error": "not found"}, 404)

    def _stream(self, hub, ctx, paused, record):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = hub.subscribe(ctx.ip, ctx.host, record=record, user=ctx.member[1] if ctx.member else "")
        try:
            hello = json.dumps({"paused": paused()})
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
            hub.unsubscribe(ctx.ip, q)


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

    def __init__(self, port, todo_dir, paused=False, blocked=(), name=""):
        self.port = port
        self.store = NoteStore()
        self.store.set_root(todo_dir)
        self.hub = Hub(blocked)
        self.paused = paused
        self.ip = local_ip()
        self.pc_name = socket.gethostname()
        self.name = name or self.pc_name   # 표시 이름 (트레이 > 내 이름)
        self.team = None          # 팀 호스트면 TeamHost
        self.auth = TeamAuth()    # 팀원이면 접속자 토큰 검증용
        self.team_only = True     # 팀에 들어 있으면 팀원에게만 공유
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
            team = self.team
            if team:
                changed = team.store.refresh()
                if changed:
                    team.hub.broadcast("changed", {"dates": sorted(d.isoformat() for d in changed)})
                if team.store.pop_schedule_changed():
                    team.hub.broadcast("schedule", {})
            time.sleep(POLL_INTERVAL)

    def set_root(self, path):
        self.store.set_root(path)
        self.hub.broadcast("reload", {})

    def set_paused(self, paused):
        self.paused = paused
        self.hub.broadcast("status", lambda ip: {"paused": paused and not self.is_owner(ip)})

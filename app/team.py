"""
팀스페이스 (공용 공간 + 팀원 식별)

- 팀 호스트: 팀원 중 한 사람의 Dogani 가 맡는다 (켜 두는 시간이 긴 PC 추천).
  공용 노트·공용 일정은 호스트 PC 의 폴더(기본 %APPDATA%\\Dogani\\team\\notes)에 평범한 md 파일로 저장
  → 옵시디언으로 열 수 있고, 백업은 폴더 복사
- 인증 (IT 회사가 아닌 사무실 수준):
  * 팀 참여 = 호스트 주소 + 참여 코드(6자리, 호스트 트레이에 표시) + 이름 + PIN(숫자 4~8자리)
  * 한 번 참여하면 PC 에 토큰을 저장해서 다시 묻지 않음. 다른 PC 에서는 이름 + PIN 으로 로그인
  * PIN 은 솔트+PBKDF2 해시로만 저장, 5번 틀리면 그 IP 는 1분 동안 로그인 금지
  * 토큰 = 팀원ID.발급시각.HMAC(팀 비밀키). 팀원 PC 들은 비밀키로 토큰을 직접 검증해서
    호스트가 꺼져 있어도 "팀원에게만 공유" 가 동작한다 (팀원끼리는 신뢰한다는 전제)
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

PIN_ITER = 120_000
FAIL_LIMIT = 5
FAIL_LOCK = 60          # 초
ONLINE_WINDOW = 75      # 마지막 신호가 이 안이면 접속 중 (클라이언트는 20초마다 보냄)
NAME_MAX = 20

SCHEDULE_SEED = """# 공용 일정

> 한 줄 = 일정 하나:  `날짜, 제목, #태그, 장소, 메모`
> 날짜 **굵게** = 중요 · ==하이라이트== = 쉬는 날(회사 휴무) · *기울임* = 기간을 화살표로
> 예) `**261119** 15:00, 정기 점검, #전산, 서버실` · `261124-261126, 출장, #외근` · `==261225==, 성탄절`

"""


# ---------------------------------------------------------------- token
def sign(secret_hex, mid, iat):
    msg = f"{mid}.{iat}".encode()
    return hmac.new(bytes.fromhex(secret_hex), msg, hashlib.sha256).hexdigest()[:32]


def make_token(secret_hex, mid, iat=None):
    iat = int(iat if iat is not None else time.time())
    return f"{mid}.{iat}.{sign(secret_hex, mid, iat)}"


def parse_token(secret_hex, token):
    """서명이 맞으면 (팀원ID, 발급시각), 아니면 None"""
    try:
        mid, iat, sig = (token or "").split(".")
        iat = int(iat)
    except ValueError:
        return None
    if not secret_hex or not hmac.compare_digest(sign(secret_hex, mid, iat), sig):
        return None
    return mid, iat


class TeamAuth:
    """팀원 PC(호스트 아님)에서 접속자 토큰 검증용. 비밀키·팀원 목록은 호스트에서 받아 온다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.secret = ""
        self.members = {}   # id -> name (모르면 빈 dict = 목록 확인 안 함)

    def update(self, secret, members):
        with self.lock:
            self.secret = secret or ""
            self.members = {m["id"]: m["name"] for m in members or []}

    @property
    def active(self):
        return bool(self.secret)

    def verify(self, token):
        """토큰 → (id, 이름) 또는 None. 팀에서 내보낸 사람(목록에 없음)은 거부"""
        with self.lock:
            secret, members = self.secret, dict(self.members)
        parsed = parse_token(secret, token)
        if not parsed:
            return None
        mid = parsed[0]
        if members and mid not in members:
            return None
        return mid, members.get(mid, "")


# ---------------------------------------------------------------- pin
def hash_pin(pin, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), PIN_ITER).hex()
    return {"salt": salt, "hash": h}


def check_pin(pin, rec):
    if not rec:
        return False
    return hmac.compare_digest(hash_pin(pin, rec["salt"])["hash"], rec["hash"])


def valid_pin(pin):
    return isinstance(pin, str) and pin.isdigit() and 4 <= len(pin) <= 8


def clean_name(name):
    name = " ".join(str(name or "").split())[:NAME_MAX]
    return name


class TeamError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ---------------------------------------------------------------- host
class TeamHost:
    """팀 호스트 데이터: team.json(팀원·비밀키·참여 코드) + notes 폴더(공용 노트·일정)"""

    def __init__(self, folder, owner_name):
        from server import Hub, NoteStore  # 순환 import 방지
        self.lock = threading.Lock()
        self.folder = folder
        self.path = os.path.join(folder, "team.json")
        self.notes_dir = os.path.join(folder, "notes")
        os.makedirs(self.notes_dir, exist_ok=True)
        self.data = self._load()
        self.fails = {}   # ip -> [횟수, 잠금 해제 시각]
        seed = os.path.join(self.notes_dir, "일정.md")
        if not os.path.exists(seed):
            with open(seed, "w", encoding="utf-8") as f:
                f.write(SCHEDULE_SEED)
        self.store = NoteStore()
        self.store.set_root(self.notes_dir)
        self.store.refresh()
        self.store.pop_schedule_changed()
        self.hub = Hub()
        self.owner_id = self._ensure_owner(owner_name)

    # -- storage
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data.setdefault("name", "팀 공용")
        data.setdefault("secret", secrets.token_hex(32))
        data.setdefault("code", self._new_code())
        data.setdefault("members", {})
        return data

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @staticmethod
    def _new_code():
        return f"{secrets.randbelow(1_000_000):06d}"

    # -- properties
    @property
    def secret(self):
        return self.data["secret"]

    @property
    def code(self):
        return self.data["code"]

    @property
    def name(self):
        return self.data["name"]

    def renew_code(self):
        with self.lock:
            self.data["code"] = self._new_code()
            self._save()
            return self.data["code"]

    def rename_team(self, name):
        name = clean_name(name)
        if name:
            with self.lock:
                self.data["name"] = name
                self._save()

    # -- members
    def _find_name(self, name):
        key = name.casefold()
        for mid, m in self.data["members"].items():
            if m["name"].casefold() == key:
                return mid
        return None

    def _ensure_owner(self, name):
        """호스트 PC 주인은 PIN 없이 자동으로 관리자 팀원"""
        with self.lock:
            for mid, m in self.data["members"].items():
                if m.get("admin"):
                    return mid
            name = clean_name(name) or "관리자"
            if self._find_name(name):
                name += "(관리자)"
            mid = secrets.token_hex(4)
            self.data["members"][mid] = {"name": name, "pin": None, "admin": True, "min_iat": 0,
                                         "joined": dt.datetime.now().isoformat(timespec="seconds"),
                                         "addr": "", "last_seen": 0}
            self._save()
            return mid

    def owner_token(self):
        return make_token(self.secret, self.owner_id)

    def _limit(self, ip):
        f = self.fails.get(ip)
        if f and f[0] >= FAIL_LIMIT and time.time() < f[1]:
            raise TeamError("locked", f"여러 번 틀려서 {FAIL_LOCK}초 동안 잠겼습니다.", 429)

    def _fail(self, ip, code, message):
        f = self.fails.setdefault(ip, [0, 0])
        if time.time() > f[1]:
            f[0] = 0 if f[0] >= FAIL_LIMIT else f[0]
        f[0] += 1
        f[1] = time.time() + FAIL_LOCK
        raise TeamError(code, message, 401)

    def join(self, ip, code, name, pin):
        """참여 코드 + 이름 + PIN → 토큰. PIN 이 초기화된 기존 이름이면 새 PIN 으로 다시 참여"""
        self._limit(ip)
        name = clean_name(name)
        if not name:
            raise TeamError("bad_name", "이름을 입력하세요.")
        if not valid_pin(pin):
            raise TeamError("bad_pin", "PIN 은 숫자 4~8자리입니다.")
        with self.lock:
            if not hmac.compare_digest(str(code or "").strip(), self.code):
                pass
            else:
                mid = self._find_name(name)
                if mid:
                    m = self.data["members"][mid]
                    if m.get("pin") or m.get("admin"):
                        raise TeamError("name_taken", "이미 있는 이름입니다. 본인이면 '로그인'을 쓰세요.", 409)
                else:
                    mid = secrets.token_hex(4)
                    m = self.data["members"][mid] = {
                        "name": name, "admin": False, "min_iat": 0, "addr": "", "last_seen": 0,
                        "joined": dt.datetime.now().isoformat(timespec="seconds")}
                m["pin"] = hash_pin(pin)
                self._save()
                self.fails.pop(ip, None)
                return self._session(mid)
        self._fail(ip, "bad_code", "참여 코드가 맞지 않습니다.")

    def login(self, ip, name, pin):
        self._limit(ip)
        with self.lock:
            mid = self._find_name(clean_name(name))
            m = self.data["members"].get(mid) if mid else None
            ok = m is not None and check_pin(str(pin or ""), m.get("pin"))
        if not ok:
            self._fail(ip, "bad_login", "이름 또는 PIN 이 맞지 않습니다.")
        self.fails.pop(ip, None)
        return self._session(mid)

    def _session(self, mid):
        token = make_token(self.secret, mid)
        return {"token": token, "id": mid, "name": self.data["members"][mid]["name"]}

    def member(self, token):
        """토큰 → 팀원 id (내보냈거나 PIN 초기화 이전 토큰이면 None)"""
        parsed = parse_token(self.secret, token)
        if not parsed:
            return None
        mid, iat = parsed
        with self.lock:
            m = self.data["members"].get(mid)
            if not m or iat < m.get("min_iat", 0):
                return None
        return mid

    def touch(self, mid, addr):
        with self.lock:
            m = self.data["members"].get(mid)
            if m:
                m["last_seen"] = time.time()
                if m.get("addr") != addr:
                    m["addr"] = addr
                    self._save()

    def update_profile(self, mid, name=None, pin=None, old_pin=None):
        with self.lock:
            m = self.data["members"].get(mid)
            if not m:
                raise TeamError("not_member", "팀원이 아닙니다.", 401)
            if name is not None:
                name = clean_name(name)
                if not name:
                    raise TeamError("bad_name", "이름을 입력하세요.")
                other = self._find_name(name)
                if other and other != mid:
                    raise TeamError("name_taken", "다른 팀원이 쓰는 이름입니다.", 409)
                m["name"] = name
            if pin is not None:
                if not valid_pin(pin):
                    raise TeamError("bad_pin", "PIN 은 숫자 4~8자리입니다.")
                if m.get("pin") and not check_pin(str(old_pin or ""), m["pin"]):
                    raise TeamError("bad_login", "지금 PIN 이 맞지 않습니다.", 401)
                m["pin"] = hash_pin(pin)
            self._save()
            return m["name"]

    def kick(self, mid):
        with self.lock:
            m = self.data["members"].get(mid)
            if m and not m.get("admin"):
                del self.data["members"][mid]
                self._save()

    def reset_pin(self, mid):
        """PIN 을 지우고 기존 토큰을 무효화. 그 사람은 참여 코드 + 같은 이름 + 새 PIN 으로 다시 참여"""
        with self.lock:
            m = self.data["members"].get(mid)
            if m and not m.get("admin"):
                m["pin"] = None
                m["min_iat"] = int(time.time()) + 1
                self._save()

    def members(self):
        now = time.time()
        with self.lock:
            out = [{"id": mid, "name": m["name"], "addr": m.get("addr", ""), "admin": bool(m.get("admin")),
                    "online": now - m.get("last_seen", 0) < ONLINE_WINDOW or bool(m.get("admin")),
                    "pending": not m.get("pin") and not m.get("admin"),
                    "last_seen": m.get("last_seen", 0)}
                   for mid, m in self.data["members"].items()]
        return sorted(out, key=lambda m: (not m["admin"], m["name"]))

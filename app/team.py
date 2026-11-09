"""
팀스페이스 (공용 공간 + 팀원 식별)

- 팀 호스트: 팀원 중 한 사람의 Dogani 가 맡는다 (켜 두는 시간이 긴 PC 추천).
  공용 노트·공용 일정은 호스트 PC 의 폴더(기본 %APPDATA%\\Dogani\\team\\notes)에 평범한 md 파일로 저장
  → 옵시디언으로 열 수 있고, 백업은 폴더 복사
- 인증 (IT 회사가 아닌 사무실 수준):
  * 팀 참여 = 호스트 주소 + 초대장 코드(사람마다 다름, 12자리, 한 번 쓰면 폐기, 15분 뒤 만료) + 이름
  * 한 번 참여하면 PC 에 토큰을 저장해서 다시 묻지 않음. PC 를 바꾸면 호스트가 그 사람 자리로 다시 초대
    (다시 초대로 들어오면 예전 PC 의 토큰은 무효)
  * 초대장 코드를 5번 틀리면 그 IP 는 1분 동안 참여 금지
  * 토큰 = 팀원ID.발급시각.Ed25519 서명. 서명 키(개인키)는 호스트 PC 에만 있고, 팀원 PC 들은
    공개키만 받아서 토큰을 직접 검증한다 → 호스트가 꺼져 있어도 "팀원에게만 공유" 가 동작하고,
    팀원(또는 내보낸 사람)이 공개키로 토큰을 위조할 수는 없다
  * 내보내기 = 팀원 목록에서 삭제, 다시 초대로 참여 = min_iat 올림. 둘 다 팀원 목록과 함께 각 PC 로 전파
"""
import datetime as dt
import json
import os
import secrets
import threading
import time
from functools import lru_cache

import ed25519

INVITE_TTL = 15 * 60    # 초대장 유효 시간(초)
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 헷갈리는 0/O, 1/I 제외
INVITE_LEN = 12         # 32^12 ≈ 60비트
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
def public_key(sign_key_hex):
    return ed25519.public_key(bytes.fromhex(sign_key_hex)).hex()


def make_token(sign_key_hex, mid, iat=None):
    iat = int(iat if iat is not None else time.time())
    return f"{mid}.{iat}.{ed25519.sign(bytes.fromhex(sign_key_hex), f'{mid}.{iat}'.encode()).hex()}"


def _shape_ok(token, members=None):
    """비싼 서명 검증 전에 싼 검사: 형식·서명 길이·팀원 목록에 있는 ID 인지 (아무 토큰으로 CPU 를 태우지 못하게)"""
    parts = (token or "").split(".")
    return len(parts) == 3 and len(parts[2]) == 128 and (not members or parts[0] in members)


@lru_cache(maxsize=256)   # 같은 토큰이 요청마다 오므로 서명 검증은 한 번만
def parse_token(pub_hex, token):
    """서명이 맞으면 (팀원ID, 발급시각), 아니면 None"""
    try:
        mid, iat, sig = (token or "").split(".")
        iat = int(iat)
        ok = ed25519.verify(bytes.fromhex(pub_hex), f"{mid}.{iat}".encode(), bytes.fromhex(sig))
    except ValueError:
        return None
    return (mid, iat) if ok else None


class TeamAuth:
    """팀원 PC(호스트 아님)에서 접속자 토큰 검증용. 공개키·팀원 목록은 호스트에서 받아 온다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pub = ""
        self.joined = False
        self.members = {}   # id -> (이름, min_iat) (모르면 빈 dict = 목록 확인 안 함)

    def update(self, pub, members, joined=None):
        with self.lock:
            self.pub = pub or ""
            # 팀에 들어 있는데 공개키가 아직 없으면(업데이트 직후·로그인 풀림) 아무 토큰도 통과 못 함 = 닫힌 쪽
            self.joined = bool(pub) if joined is None else bool(joined)
            self.members = {m["id"]: (m["name"], m.get("min_iat", 0)) for m in members or []}

    @property
    def active(self):
        return self.joined

    def verify(self, token):
        """토큰 → (id, 이름) 또는 None. 내보낸 사람(목록에 없음)·다시 초대 이전 토큰은 거부"""
        with self.lock:
            pub, members = self.pub, dict(self.members)
        parsed = parse_token(pub, token) if pub and _shape_ok(token, members) else None
        if not parsed:
            return None
        mid, iat = parsed
        if members and (mid not in members or iat < members[mid][1]):
            return None
        return mid, members.get(mid, ("", 0))[0]


# ---------------------------------------------------------------- invite
def normalize_invite(code):
    """입력한 초대장 코드 → 대문자·구분자 제거 (XXXX-XXXX-XXXX 로 보여 주고, 띄어쓰기·소문자도 받음)"""
    return "".join(c for c in str(code or "").upper() if c.isalnum())


def format_invite(code):
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


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
    """팀 호스트 데이터: team.json(팀원·서명 키·초대장) + notes 폴더(공용 노트·일정)"""

    def __init__(self, folder, owner_name):
        from server import Hub, NoteStore  # 순환 import 방지
        self.lock = threading.Lock()
        self.folder = folder
        self.path = os.path.join(folder, "team.json")
        self.notes_dir = os.path.join(folder, "notes")
        os.makedirs(self.notes_dir, exist_ok=True)
        self.data = self._load()
        if self.data.pop("_dirty", False):   # 새 서명 키는 바로 저장 (안 그러면 재시작마다 키가 바뀜)
            self._save()
        self.pub = public_key(self.data["sign_key"])
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
        # 예전 HMAC 비밀키는 팀원 PC 에 퍼져 있어서 위조에 쓰일 수 있음 → 버리고 서명 키로 교체
        # (예전 토큰은 모두 무효, 팀원은 초대장으로 한 번 다시 참여)
        if "secret" in data or "sign_key" not in data:
            data.pop("secret", None)
            data.setdefault("sign_key", secrets.token_hex(32))
            data["_dirty"] = True
        # 예전 공용 참여 코드·PIN 은 초대장으로 바뀜 → 지움 (이미 들어온 팀원 토큰은 그대로 유효)
        if data.pop("code", None) is not None:
            data["_dirty"] = True
        data.setdefault("members", {})
        for m in data["members"].values():
            if m.pop("pin", 0) != 0:
                data["_dirty"] = True
        data.setdefault("invites", {})
        return data

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @property
    def name(self):
        return self.data["name"]

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
        """호스트 PC 주인은 초대장 없이 자동으로 관리자 팀원"""
        with self.lock:
            for mid, m in self.data["members"].items():
                if m.get("admin"):
                    return mid
            name = clean_name(name) or "관리자"
            if self._find_name(name):
                name += "(관리자)"
            mid = secrets.token_hex(4)
            self.data["members"][mid] = {"name": name, "admin": True, "min_iat": 0,
                                         "joined": dt.datetime.now().isoformat(timespec="seconds"),
                                         "addr": "", "last_seen": 0}
            self._save()
            return mid

    def owner_token(self):
        return make_token(self.data["sign_key"], self.owner_id)

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

    # -- invites
    def _purge(self):
        """만료된 초대장 정리 (lock 안에서 부름) → 바뀌었으면 True"""
        now = time.time()
        old = [c for c, v in self.data["invites"].items() if v.get("exp", 0) <= now]
        for c in old:
            del self.data["invites"][c]
        return bool(old)

    def _drop_invites(self, mid):
        """그 팀원 자리로 만든 초대장 모두 삭제 (lock 안에서 부름)"""
        for c in [c for c, v in self.data["invites"].items() if v.get("mid") == mid]:
            del self.data["invites"][c]

    def create_invite(self, mid=None):
        """일회용 초대장 → 코드(XXXX-XXXX-XXXX). mid 를 주면 그 팀원 자리로 다시 초대(PC 바꿈)"""
        code = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_LEN))
        with self.lock:
            m = self.data["members"].get(mid) if mid else None
            if mid and (not m or m.get("admin")):
                raise TeamError("not_member", "팀원이 아닙니다.", 404)
            self._purge()
            if mid:
                self._drop_invites(mid)   # 다시 초대는 한 사람당 한 장만 살아 있게
            self.data["invites"][code] = {"exp": int(time.time()) + INVITE_TTL, "mid": mid or ""}
            self._save()
        return format_invite(code)

    def cancel_invite(self, code):
        with self.lock:
            if self.data["invites"].pop(normalize_invite(code), None) is not None:
                self._save()

    def invites(self):
        """아직 안 쓴 초대장 [{code, label, left(초)}], 남은 시간 짧은 순"""
        now = time.time()
        with self.lock:
            if self._purge():
                self._save()
            out = []
            for c, v in self.data["invites"].items():
                m = self.data["members"].get(v.get("mid") or "")
                out.append({"code": format_invite(c), "left": int(v["exp"] - now),
                            "label": f"{m['name']} 다시 초대" if m else "새 팀원"})
        return sorted(out, key=lambda i: i["left"])

    def join(self, ip, code, name):
        """초대장 코드 (+ 새 팀원이면 이름) → 토큰. 성공하면 초대장은 폐기"""
        self._limit(ip)
        code = normalize_invite(code)
        with self.lock:
            purged = self._purge()
            inv = self.data["invites"].get(code) if len(code) == INVITE_LEN else None
            if inv is not None:
                mid = inv.get("mid") or ""
                if mid:
                    m = self.data["members"].get(mid)
                    if not m:   # 그 사이 내보냄 (보통은 kick 이 초대장도 지움)
                        del self.data["invites"][code]
                        self._save()
                        raise TeamError("bad_code", "초대장 대상이 팀에 없습니다. 관리자에게 새 초대장을 받으세요.", 404)
                    self._drop_invites(mid)
                    iat = int(time.time()) + 1
                    m["min_iat"] = iat   # 예전 PC 의 토큰(같은 1초 것 포함)은 무효, 새 토큰만 통과
                else:
                    name = clean_name(name)
                    if not name:
                        raise TeamError("bad_name", "이름을 입력하세요.")
                    if self._find_name(name):
                        raise TeamError("name_taken", "이미 있는 이름입니다. 본인이면 관리자에게 '다시 초대'를 부탁하세요.", 409)
                    mid = secrets.token_hex(4)
                    self.data["members"][mid] = {
                        "name": name, "admin": False, "min_iat": 0, "addr": "", "last_seen": 0,
                        "joined": dt.datetime.now().isoformat(timespec="seconds")}
                    iat = None
                self.data["invites"].pop(code, None)
                self._save()
                self.fails.pop(ip, None)
                return self._session(mid, iat)
            if purged:
                self._save()
        self._fail(ip, "bad_code", "초대장 코드가 맞지 않거나 만료됐습니다. (15분 안에 한 번만 쓸 수 있음)")

    def _session(self, mid, iat=None):
        token = make_token(self.data["sign_key"], mid, iat)
        return {"token": token, "id": mid, "name": self.data["members"][mid]["name"]}

    def member(self, token):
        """토큰 → 팀원 id (내보냈거나 다시 초대 이전 토큰이면 None)"""
        with self.lock:
            known = set(self.data["members"])
        if not _shape_ok(token, known):
            return None
        parsed = parse_token(self.pub, token)
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

    def update_profile(self, mid, name=None):
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
            self._save()
            return m["name"]

    def kick(self, mid):
        with self.lock:
            m = self.data["members"].get(mid)
            if m and not m.get("admin"):
                del self.data["members"][mid]
                self._drop_invites(mid)
                self._save()

    def members(self):
        now = time.time()
        with self.lock:
            out = [{"id": mid, "name": m["name"], "addr": m.get("addr", ""), "admin": bool(m.get("admin")),
                    "min_iat": m.get("min_iat", 0),
                    "online": now - m.get("last_seen", 0) < ONLINE_WINDOW or bool(m.get("admin")),
                    "last_seen": m.get("last_seen", 0)}
                   for mid, m in self.data["members"].items()]
        return sorted(out, key=lambda m: (not m["admin"], m["name"]))

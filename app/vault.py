"""
볼트 = dogani 가 여는 폴더 하나 (옵시디언 볼트와 같은 개념)

- 볼트 설정은 그 폴더의 `.dogani/vault.json` 에 둔다 → 폴더를 옮기거나 복사해도 따라감
- 개인 볼트와 팀 볼트는 같은 형식. 팀 볼트는 `.dogani/team.json` 이 하나 더 있을 뿐 (team.py)
- 비밀값(팀 서명 키·초대장)과 PC 마다 다른 값(창 위치 등)은 볼트에 두지 않고 %APPDATA%\\dogani 에 둔다
- 노트 스캔은 점(.)으로 시작하는 폴더를 건너뛰므로 .dogani / .obsidian 은 노트로 읽지 않음
"""
import datetime as dt
import json
import os
import re
import secrets

META_DIR = ".dogani"
VAULT_FILE = "vault.json"


def meta_dir(root):
    return os.path.join(root, META_DIR)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def open_vault(root):
    """폴더를 볼트로 연다. .dogani/vault.json 이 없으면 만든다 → 볼트 설정 dict (id 포함). 폴더가 없으면 OSError"""
    if not root or not os.path.isdir(root):
        raise OSError(f"폴더가 없습니다: {root}")
    path = os.path.join(meta_dir(root), VAULT_FILE)
    data = _read(path)
    if not re.fullmatch(r"[0-9a-f]{16}", str(data.get("id", ""))):   # 키 파일 이름에 쓰임 → 경로 문자 금지
        data = {"created": dt.datetime.now().isoformat(timespec="seconds"), **data, "id": secrets.token_hex(8)}
        write_json(path, data)
    return data


def vault_name(root):
    return os.path.basename(os.path.normpath(root)) if root else ""

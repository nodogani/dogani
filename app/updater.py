"""
자동 업데이트 (디스코드식: 켤 때만 적용).

- 배포 = GitHub 에 버전 태그(v26.xx.xx.xx) push. 커밋·push 만으로는 안 나감.
- 받는 쪽(exe, 나·동료 동일): 런처가 켤 때 GitHub 의 최신 태그를 확인해 새 버전이면 받아서 바로 적용.
  실행 중에는 확인하지 않음.
- 소스 실행(개발 PC)은 받지 않음.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

REPO = "nodogani/dogani"
CODE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(CODE_DIR)
CONFIG_DIR = os.environ.get("DOGANI_HOME") or os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Dogani")
CACHE_DIR = os.path.join(CONFIG_DIR, "app")
STAGED_DIR = CACHE_DIR + ".new"
FROZEN = getattr(sys, "frozen", False)
NO_WINDOW = 0x08000000
TAG_RE = re.compile(r"^v\d{2}\.\d{2}\.\d{2}\.\d{2}$")
UA = {"User-Agent": "dogani-updater"}


# ---------------------------------------------------------------- 내 버전
def current_version():
    """지금 실행 중인 코드의 버전 {"id": 태그, "label": 태그}"""
    try:
        with open(os.path.join(CODE_DIR, "version.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"id": "", "label": "개발 중" if not FROZEN else ""}


def version_label():
    """exe = 받은/빌드한 버전, 소스 실행 = 마지막 태그 + 개발 중"""
    if FROZEN:
        return current_version().get("label", "")
    try:
        tag = subprocess.run(["git", "-C", REPO_DIR, "describe", "--tags", "--abbrev=0"], capture_output=True,
                             check=True, creationflags=NO_WINDOW).stdout.decode().strip()
        return f"{tag} 개발 중"
    except (OSError, subprocess.CalledProcessError):
        return "개발 중"


# ---------------------------------------------------------------- 받는 쪽 (GitHub)
def _get(url, timeout):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def latest_tag(timeout=5):
    """GitHub 의 가장 최신 버전 태그. 확인 못 하면 None"""
    try:
        tags = json.loads(_get(f"https://api.github.com/repos/{REPO}/tags?per_page=100", timeout))
    except (OSError, ValueError):
        return None
    names = [t.get("name", "") for t in tags if TAG_RE.match(t.get("name", ""))]
    return max(names) if names else None  # vYY.MM.DD.NN 은 문자열 정렬 = 시간순


def stage(timeout=20):
    """최신 태그의 app/ 을 STAGED_DIR 에 받아 둔다. 받았으면 True"""
    tag = latest_tag(5)
    if not tag or tag <= current_version().get("id", ""):
        return False
    try:
        data = _get(f"https://codeload.github.com/{REPO}/zip/refs/tags/{tag}", timeout)
        tmp = STAGED_DIR + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(STAGED_DIR, ignore_errors=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            prefix = z.namelist()[0].split("/")[0] + "/app/"  # dogani-v26.../app/...
            names = [n for n in z.namelist() if n.startswith(prefix) and not n.endswith("/")
                     and not n[len(prefix):].startswith("editor/")]
            if prefix + "dogani.py" not in names:
                return False
            for n in names:
                rel = n[len(prefix):]
                if ".." in rel.split("/"):
                    return False
                dest = os.path.join(tmp, *rel.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(z.read(n))
        # version.json 은 마지막에 (다 받았다는 표시)
        with open(os.path.join(tmp, "version.json"), "w", encoding="utf-8") as f:
            json.dump({"id": tag, "label": tag}, f, ensure_ascii=False)
        os.replace(tmp, STAGED_DIR)
        return True
    except (OSError, ValueError, zipfile.BadZipFile, IndexError):
        return False

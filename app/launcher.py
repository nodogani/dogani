"""
dogani.exe 진입점 (런처). 이 파일만 exe 에 고정되고, 실제 프로그램 코드(dogani.py, ui/ ...)는
%APPDATA%\\Dogani\\app 에 받아 둔 최신본을 실행한다. 받은 게 없거나 깨졌으면 exe 에 같이 넣은 기본 코드로 실행.

켤 때마다 (디스코드식)
  1) 지금 코드의 updater.py 로 GitHub 최신 태그를 확인해 새 버전이면 app.new 에 받고
  2) app.new 를 app 으로 바꿔 끼운 뒤
  3) 실행할 코드 폴더를 고른다.  → 받는 방법은 updater.py 에 있어서 런처는 거의 바뀔 일이 없다.
"""
import ctypes
import importlib
import importlib.util
import json
import os
import shutil
import sys
import traceback

import deps  # noqa: F401  PyInstaller 가 PySide6 등 의존성을 exe 에 넣도록

CONFIG_DIR = os.environ.get("DOGANI_HOME") or os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Dogani")
CACHE_DIR = os.path.join(CONFIG_DIR, "app")
STAGED_DIR = CACHE_DIR + ".new"
BUNDLED_DIR = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "app")
CODE_MODULES = ("dogani", "server", "schedule", "team", "updater", "devreload", "liveupdate")


def already_running():
    """dogani.single_instance() 와 같은 mutex. 이미 떠 있으면 그 프로세스가 쓰는 코드 폴더를 건드리면 안 됨"""
    handle = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, "Local\\DoganiMutex")  # SYNCHRONIZE
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
    return bool(handle)


def version_id(folder):
    try:
        with open(os.path.join(folder, "version.json"), encoding="utf-8") as f:
            return json.load(f).get("id", "")
    except (OSError, ValueError):
        return ""


def apply_staged():
    if not os.path.isfile(os.path.join(STAGED_DIR, "version.json")):  # 다 받은 것만 (version.json 은 마지막에 씀)
        return
    try:
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        os.replace(STAGED_DIR, CACHE_DIR)
    except OSError:
        pass


def code_dir():
    """받은 코드와 exe 기본 코드 중 새 버전 (exe 를 새로 빌드해 나눠 주면 기본 코드가 더 새것일 수 있음)"""
    if os.path.isfile(os.path.join(CACHE_DIR, "dogani.py")) and version_id(CACHE_DIR) >= version_id(BUNDLED_DIR):
        return CACHE_DIR
    return BUNDLED_DIR


def fetch_update():
    """실행할 코드의 updater.stage() 로 새 버전을 받아 둔다. 실패해도 그냥 지금 버전으로 실행"""
    try:
        spec = importlib.util.spec_from_file_location("_boot_updater", os.path.join(code_dir(), "updater.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.stage()
    except Exception:
        traceback.print_exc()


def run(code_dir):
    sys.path.insert(0, code_dir)
    os.environ["DOGANI_CODE_DIR"] = code_dir
    # import dogani 로 쓰면 PyInstaller 가 dogani 를 exe 안에 굳혀 넣어서 받은 코드보다 먼저 잡힌다
    return importlib.import_module("dogani").main


def main():
    if not already_running():  # 두 번째 실행은 dogani 가 알아서 종료하므로 업데이트 안 건드림
        apply_staged()  # 지난번 실행 중에 받아 둔 것
        fetch_update()
        apply_staged()
    entry = None
    if code_dir() == CACHE_DIR:
        try:
            entry = run(CACHE_DIR)
        except Exception:  # 받은 코드가 깨졌으면 지우고 기본 코드로
            traceback.print_exc()
            for name in CODE_MODULES:
                sys.modules.pop(name, None)
            sys.path.remove(CACHE_DIR)
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
    if entry is None:
        entry = run(BUNDLED_DIR)
    entry()


if __name__ == "__main__":
    main()

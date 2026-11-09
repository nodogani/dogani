"""
dogani.py 에 붙이는 라이브 업데이트 (dogani.py 는 install(app) 한 줄만 부름)

- exe: 업데이트는 켤 때 런처가 적용 (디스코드식). 실행 중에는 확인하지 않음 (켤 때 런처가 확인·적용 한 번으로 끝)
- 소스 실행(개발 PC): ui 파일 저장 → 창 새로고침, .py 저장 → 재시작
"""
import ctypes
import subprocess
import sys

from PySide6.QtCore import QObject

import devreload
import updater

CREATE_NO_WINDOW = 0x08000000
VERSION = updater.version_label()  # 업데이트되면 재시작하므로 한 번만 계산
MUTEX = None  # dogani.main 이 single_instance() 핸들을 넣어 둔다 (재시작 전에 놓아야 새 프로세스가 뜸)


def version_text():
    """트레이 표시용: exe = v26.11.06.02, 소스 실행 = 버전 (최신)"""
    state = getattr(INSTANCE, "state", "") if INSTANCE else ""
    return f"{VERSION} ({state})" if state else VERSION


INSTANCE = None


class LiveUpdate(QObject):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.state = "" if getattr(sys, "frozen", False) else "최신"  # 개발 PC 는 배포하는 쪽이라 늘 최신
        if not getattr(sys, "frozen", False):
            self.dev = devreload.DevReloader(self)
            self.dev.ui_changed.connect(self.reload_ui)
            self.dev.py_changed.connect(self.restart)

    def reload_ui(self):
        for ov in self.app.windows():
            ov.view.reload()

    def restart(self):
        # run.bat 으로 실행하면 dogani 는 __main__ 이라 import dogani 하면 한 번 더 로드됨
        dogani = sys.modules.get("dogani") or sys.modules["__main__"]
        app = self.app
        dogani.save_config(app.cfg)
        if app.share:
            app.share.stop()
        app.tray.stop()
        if MUTEX:
            ctypes.windll.kernel32.CloseHandle(MUTEX)
        subprocess.Popen(dogani.launch_command(), creationflags=CREATE_NO_WINDOW)
        app.qapp.quit()


def install(app):
    global INSTANCE
    INSTANCE = app.live_update = LiveUpdate(app)

"""
dogani.py 에 붙이는 라이브 업데이트 (dogani.py 는 install(app) 한 줄만 부름)

- exe: 업데이트는 켤 때 런처가 적용 (디스코드식). 실행 중에는 30초 뒤 + 1시간마다 GitHub 에 확인만 해서
  트레이에 "재시작하면 적용" 표시. 알아서 재시작하지 않음
- 소스 실행(개발 PC): ui 파일 저장 → 창 새로고침, .py 저장 → 재시작
"""
import ctypes
import subprocess
import sys
import threading

from PySide6.QtCore import QObject, QTimer, Signal

import devreload
import updater

FIRST_CHECK_MS = 30 * 1000
INTERVAL_MS = 60 * 60 * 1000  # GitHub API 비로그인 한도(IP당 60회/시) 여유 있게
CREATE_NO_WINDOW = 0x08000000
VERSION = updater.version_label()  # 업데이트되면 재시작하므로 한 번만 계산
MUTEX = None  # dogani.main 이 single_instance() 핸들을 넣어 둔다 (재시작 전에 놓아야 새 프로세스가 뜸)


def version_text():
    """트레이 표시용: v26.11.06.02 (최신) / (재시작하면 적용)"""
    state = getattr(INSTANCE, "state", "") if INSTANCE else ""
    return f"{VERSION} ({state})" if state else VERSION


INSTANCE = None


class LiveUpdate(QObject):
    checked = Signal(str)

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.state = "" if getattr(sys, "frozen", False) else "최신"  # 개발 PC 는 배포하는 쪽이라 늘 최신
        if getattr(sys, "frozen", False):
            self.checked.connect(self.on_checked)
            QTimer(self, interval=INTERVAL_MS, timeout=self.check).start()
            QTimer.singleShot(FIRST_CHECK_MS, self.check)
        else:
            self.dev = devreload.DevReloader(self)
            self.dev.ui_changed.connect(self.reload_ui)
            self.dev.py_changed.connect(self.restart)

    def check(self):
        def work():
            result = updater.check()
            if result:
                self.checked.emit(result)
        threading.Thread(target=work, daemon=True).start()

    def on_checked(self, result):
        self.state = "재시작하면 적용" if result == "new" else "최신"
        self.app.tray.update()

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

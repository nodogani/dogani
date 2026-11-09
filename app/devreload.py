"""개발 모드(소스로 실행)에서 파일을 저장하면 바로 반영.
  ui/ 파일 → 열린 창 새로고침,  .py 파일 → Dogani 재시작."""
import os

from PySide6.QtCore import QObject, QTimer, Signal

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_EXT = (".html", ".js", ".css", ".svg", ".png")


def snapshot():
    out = {}
    for root, dirs, files in os.walk(CODE_DIR):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "editor", "node_modules")]
        for name in files:
            if name.endswith(".py") or name.endswith(UI_EXT):
                path = os.path.join(root, name)
                try:
                    out[path] = os.stat(path).st_mtime_ns
                except OSError:
                    pass
    return out


def compiles(path):
    try:
        with open(path, "rb") as f:
            compile(f.read(), path, "exec")
        return True
    except (OSError, SyntaxError, ValueError):
        return False


class DevReloader(QObject):
    ui_changed = Signal()
    py_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = snapshot()
        self.timer = QTimer(self, interval=700, timeout=self.tick)
        self.timer.start()

    def tick(self):
        new = snapshot()
        changed = {p for p in new.keys() | self.state.keys() if new.get(p) != self.state.get(p)}
        self.state = new
        if any(p.endswith(".py") for p in changed):
            if all(compiles(p) for p in new if p.endswith(".py")):  # 문법 오류 상태로 재시작하면 죽으니까
                self.py_changed.emit()
        elif changed:
            self.ui_changed.emit()

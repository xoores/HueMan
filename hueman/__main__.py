"""Entry point: ``python -m hueman``."""

import signal
import sys

from PySide6.QtWidgets import QApplication

from .config import AppConfig
from .ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("hueman")
    app.setApplicationDisplayName("Hueman")
    app.setDesktopFileName("hueman")
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    win = MainWindow(AppConfig.load())
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

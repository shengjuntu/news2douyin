from __future__ import annotations
import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QSettings
from .simple_window import SimpleWindow

def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("news2douyin")
    app.setOrganizationName("news2douyin")
    settings = QSettings("news2douyin", "news2douyin")
    win = SimpleWindow(settings=settings)
    win.show()
    sys.exit(app.exec())

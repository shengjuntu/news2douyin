from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from .main_window import MainWindow


def main() -> None:
    # Ensure relative paths resolve from project root when run from anywhere
    # (best effort; does not affect installed package use)
    try:
        repo_root = Path(__file__).resolve().parents[3]
        if (repo_root / "pyproject.toml").exists():
            os.chdir(str(repo_root))
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("news2douyin")
    app.setOrganizationName("news2douyin")

    # Persist window settings
    settings = QSettings()
    win = MainWindow(settings=settings)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

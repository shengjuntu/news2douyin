from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from .v7_client_window import V7ClientWindow


def main() -> None:
    try:
        repo_root = Path(__file__).resolve().parents[3]
        if (repo_root / 'pyproject.toml').exists():
            import os
            os.chdir(str(repo_root))
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName('news2douyin-v7-client')
    app.setOrganizationName('news2douyin')
    settings = QSettings('news2douyin', 'news2douyin-v7-client')
    win = V7ClientWindow(settings=settings)
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

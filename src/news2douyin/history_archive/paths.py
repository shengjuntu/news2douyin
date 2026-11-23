from os import getenv
from pathlib import Path


def archive_root(storage_root):
    configured = getenv('NEWS2DOUYIN_HISTORY_ARCHIVE_DIR', '').strip()
    return Path(configured).expanduser().resolve() if configured else Path(storage_root).resolve() / 'history_archive'

from __future__ import annotations
from pathlib import Path
from typing import Any
import os
from dotenv import find_dotenv, load_dotenv
import yaml


def load_environment() -> None:
    """Load the working-directory .env consistently; real env vars win."""
    explicit = os.getenv('NEWS2DOUYIN_ENV_FILE')
    path = explicit if explicit else find_dotenv(usecwd=True)
    if path:
        load_dotenv(path, override=False)

def load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    return yaml.safe_load(p.read_text(encoding="utf-8"))

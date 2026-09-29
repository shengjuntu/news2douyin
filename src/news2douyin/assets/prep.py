from __future__ import annotations
from pathlib import Path
from typing import Optional
import subprocess
import sys

def prep_assets(run_dir: Path, assets_dir: Path | None = None, *, llm: str = "none") -> Path:
    """Run the packaged legacy asset worker in an isolated subprocess."""
    run_dir = Path(run_dir)
    if assets_dir is None:
        assets_dir = Path(run_dir) / "assets"
    assets_dir = Path(assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, '-m', 'news2douyin.assets.prepare', "--run-dir", str(run_dir), "--out", str(assets_dir), "--llm", llm]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"prep_assets failed (code {p.returncode})\n{p.stdout}")
    return assets_dir

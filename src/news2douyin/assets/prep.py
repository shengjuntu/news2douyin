from __future__ import annotations
from pathlib import Path
from typing import Optional
import subprocess
import sys

def prep_assets(run_dir: Path, assets_dir: Path | None = None, *, llm: str = "none") -> Path:
    """Prepare per-event asset folders under out_dir from a given run directory.

    This is a lightweight wrapper around the existing tools/prep_assets.py script so the
    CLI and GUI can call it via Python without relying on an external entrypoint.
    """
    run_dir = Path(run_dir)
    if assets_dir is None:
        assets_dir = Path(run_dir) / "assets"
    assets_dir = Path(assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)

    # Call the existing script for now (keeps behavior identical).
    # You can later move implementation fully into this module.
    script = (Path(__file__).resolve().parents[3] / "tools" / "prep_assets.py").resolve()
    if not script.exists():
        raise FileNotFoundError(f"prep_assets script not found: {script}")

    cmd = [sys.executable, str(script), "--run-dir", str(run_dir), "--out", str(assets_dir), "--llm", llm]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"prep_assets failed (code {p.returncode})\n{p.stdout}")
    return assets_dir

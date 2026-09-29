from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from loguru import logger


def render_markdown(template_path: str | Path, script_pack: dict) -> str:
    template_path = Path(template_path)
    env = Environment(loader=FileSystemLoader(str(template_path.parent)))
    tmpl = env.get_template(template_path.name)
    return tmpl.render(script=script_pack)


def save_script_pack(
    out_dir: str | Path,
    script_pack: dict,
    template_path: str | Path,
    *,
    write_json: bool = True,
    write_md: bool = True,
) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        if write_json:
            (out_dir / "script.json").write_text(
                json.dumps(script_pack, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (out_dir / "assets_plan.json").write_text(
                json.dumps(script_pack.get("assets_plan", {}), ensure_ascii=False, indent=2), encoding="utf-8"
            )

        if write_md:
            md = render_markdown(template_path, script_pack)
            (out_dir / "script.md").write_text(md, encoding="utf-8")
    except Exception:
        logger.exception(f"[EXPORT] save_script_pack failed out_dir={out_dir}")
        raise

    logger.info(f"[EXPORT] saved out_dir={out_dir}")

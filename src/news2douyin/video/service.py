from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlmodel import Session

from ..editorial.service import build_editorial_pack
from ..storage.models import ScriptPackage, utc_now_iso
from ..utils.hashing import stable_hash


def build_script_text(pack: dict[str, Any], profile_name: str = 'douyin_market_60s') -> str:
    title = pack['event_title']
    sources = pack.get('sources') or []
    summary = (sources[0].get('excerpt') or '')[:400] if sources else ''
    parts = [f'这条新闻关注的是：{title}。']
    if summary and summary != title:
        parts.append(f'收录资料摘要：{summary}')
    parts.append('后续关注相关事件的进一步信息。')
    return '\n'.join(parts)


def build_script_package(session: Session, event_key: str, *, profile_name: str = 'douyin_market_60s', output_root: str | Path = 'runs_v7/packages') -> ScriptPackage:
    editorial = build_editorial_pack(session, event_key)
    script_text = build_script_text(editorial, profile_name)
    package_key = 'pkg_' + stable_hash(event_key, profile_name, utc_now_iso(), uuid4().hex, length=20)
    out_dir = Path(output_root) / package_key
    out_dir.mkdir(parents=True, exist_ok=False)
    row = ScriptPackage(package_key=package_key, event_key=event_key, profile_name=profile_name,
                        script_text=script_text, output_dir=str(out_dir), tts_status='pending')
    from .workbench import initialize_script
    import shutil
    try:
        initialize_script(session, row, editorial)
        session.commit()
    except Exception:
        session.rollback()
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    session.refresh(row)
    return row

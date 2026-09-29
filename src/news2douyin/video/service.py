from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from ..editorial.service import build_editorial_pack
from ..storage.models import ScriptPackage, utc_now_iso
from ..storage.utils import dumps
from ..utils.hashing import stable_hash


def build_script_text(pack: dict[str, Any], profile_name: str = 'douyin_market_60s') -> str:
    sectors = '、'.join(pack.get('sectors') or []) or '市场主线'
    symbols = '、'.join(pack.get('symbols') or []) or '相关龙头'
    headlines = pack.get('supporting_headlines') or []
    lead = headlines[0] if headlines else pack['event_title']
    return (
        f"今天看一个和{sectors}有关的市场线索。\n"
        f"核心事件是：{lead}。\n"
        f"我的理解是：{pack['market_view']}\n"
        f"如果你要落到交易上，优先观察{symbols}以及板块联动强度。\n"
        f"最后记住，这类新闻更适合做盘前印象，不适合脱离盘面单独下结论。"
    )


def build_script_package(session: Session, event_key: str, *, profile_name: str = 'douyin_market_60s', output_root: str | Path = 'runs_v7/packages') -> ScriptPackage:
    editorial = build_editorial_pack(session, event_key)
    script_text = build_script_text(editorial, profile_name)
    package_key = 'pkg_' + stable_hash(event_key, profile_name, utc_now_iso(), uuid4().hex, length=20)
    out_dir = Path(output_root) / package_key
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {'editorial': editorial, 'script_text': script_text, 'profile_name': profile_name}
    (out_dir / 'script.txt').write_text(script_text, encoding='utf-8')
    (out_dir / 'script.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    (out_dir / 'assets_manifest.json').write_text(json.dumps({'images': [], 'video_clips': [], 'notes': ['fill assets later']}, ensure_ascii=False, indent=2), encoding='utf-8')
    row = ScriptPackage(package_key=package_key, event_key=event_key, profile_name=profile_name, script_text=script_text, script_json=dumps(payload), output_dir=str(out_dir), tts_status='pending')
    session.add(row)
    session.commit(); session.refresh(row)
    return row

from __future__ import annotations

import json
import re
import time
from typing import Any

from loguru import logger

from ..llm.client_factory import create_client_with_config
from ..prompts.manager import PromptManager


_JSON_RE = re.compile(r"\{.*\}", re.S)


def _extract_json(text: str) -> dict[str, Any]:
    t = (text or "").strip()
    if t.startswith("{") and t.endswith("}"):
        return json.loads(t)
    m = _JSON_RE.search(t)
    if not m:
        raise ValueError("No JSON found in LLM output")
    return json.loads(m.group(0))


def build_script_from_story_llm(
    story: dict,
    *,
    llm_cfg: dict,
    prompts_cfg: dict | None,
    script_cfg: dict | None,
    export_cfg: dict,
) -> dict:
    """LLM-based script pack generator.

    Produces a *template-ready* script_pack JSON consumed by templates/douyin/*.j2.
    """

    client_cfg_path = llm_cfg.get("client_config_path")
    if not client_cfg_path:
        raise ValueError("llm.client_config_path is required")
    client = create_client_with_config(client_cfg_path)

    # Optional overrides for this stage
    stage_cfg = (script_cfg or {})
    llm_over = (stage_cfg.get("llm") or {})

    temperature = float(llm_over.get("temperature", llm_cfg.get("temperature", 0.2)))
    max_tokens = int(llm_over.get("max_tokens", llm_cfg.get("max_tokens", 1200)))
    retry_cfg = (llm_over.get("retry") or llm_cfg.get("retry") or {})
    max_attempts = int(retry_cfg.get("max_attempts", 2))
    backoff = float(retry_cfg.get("backoff_sec", 1.0))

    pm = PromptManager(prompts_cfg, project_root=".")
    tmpl = pm.get("script")

    style = story.get("recommended_style") or "fast_news"
    templates_map = export_cfg.get("templates") or {}
    target_seconds = int((templates_map.get(style, {}) or {}).get("target_seconds", 45))
    hashtags_default = export_cfg.get("hashtags_default") or ["#新闻"]
    safe_disclaimer = bool(export_cfg.get("safe_disclaimer", True))

    variables = {
        "STORYPACK_JSON": json.dumps(story, ensure_ascii=False),
        "STYLE": style,
        "TARGET_SECONDS": target_seconds,
        "HASHTAGS_DEFAULT_JSON": json.dumps(hashtags_default, ensure_ascii=False),
        "SAFE_DISCLAIMER": "true" if safe_disclaimer else "false",
    }
    prompt = PromptManager.render(tmpl, variables)

    ev = story.get("event_id") or "event_unknown"
    log = logger.bind(event_id=str(ev), style=str(style))

    err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            t0 = time.perf_counter()
            resp = client.generate(prompt, max_tokens=max_tokens)
            obj = _extract_json(resp)

            # Minimal guards so templates never crash
            obj.setdefault("event_id", ev)
            obj.setdefault("topic", story.get("topic") or "")
            obj.setdefault("thesis", story.get("thesis") or "")
            obj.setdefault("tone", story.get("tone") or "冷静客观")
            obj.setdefault("recommended_style", style)
            obj.setdefault("video_title_candidates", obj.get("titles") or [])
            obj.setdefault("hook", {"spoken": "", "on_screen": ""})
            obj.setdefault("segments", [])
            obj.setdefault("closing", {"spoken": "", "on_screen": ""})
            obj.setdefault("hashtags", hashtags_default)
            if safe_disclaimer:
                obj.setdefault("disclaimer", "本内容为新闻信息梳理，不构成任何建议；请以权威来源为准。")
            else:
                obj.setdefault("disclaimer", "")

            log.info(f"[SCRIPT_LLM] ok in {(time.perf_counter()-t0):.2f}s segments={len(obj.get('segments') or [])}")
            return obj
        except Exception as e:
            err = e
            log.warning(f"[SCRIPT_LLM] attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                time.sleep(backoff * (2 ** (attempt - 1)))

    raise RuntimeError(f"SCRIPT_LLM failed: {err}")

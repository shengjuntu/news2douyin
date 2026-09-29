"""LLM-based news filter with rule-based fallback.

Calls the configured OpenAI-compatible endpoint (OPENAI_BASE_URL / MODEL from .env)
directly over HTTP with thinking disabled (chat_template_kwargs).
If the endpoint is down or a batch fails, falls back to the rule filter so that
scheduled collection never blocks or empties out.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from loguru import logger

from .filters import filter_items, hard_filter_items
from ..llm.settings import get_settings, probe_endpoint

BATCH_SIZE = 12
MAX_CONTENT_CHARS = 300
MAX_TOKENS = 1200
VALID_CATEGORIES = {'business', 'technology', 'politics', 'economy', 'military'}
VALID_SENTIMENTS = {'positive', 'negative', 'neutral'}
PROBE_TIMEOUT = 3.0


def endpoint_alive(timeout: float = PROBE_TIMEOUT) -> bool:
    return probe_endpoint(timeout)


def _llm_chat(messages: list[dict[str, str]], max_tokens: int = MAX_TOKENS, timeout: int = 90) -> str:
    """One chat completion with thinking disabled. Raises on failure."""
    import requests
    settings = get_settings()
    url = settings.base_url + '/chat/completions'
    payload = {
        'model': settings.model,
        'messages': messages,
        'temperature': 0.0,
        'max_tokens': max_tokens,
        'chat_template_kwargs': {'enable_thinking': False},
    }
    r = requests.post(url, json=payload, headers=settings.headers, timeout=timeout)
    r.raise_for_status()
    return r.json()['choices'][0]['message']['content'] or ''


def _generate_with_retry(prompt: str) -> str:
    last_err = None
    for attempt in range(2):
        try:
            return _llm_chat([{'role': 'user', 'content': prompt}])
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise last_err


def _build_prompt(profile: dict[str, Any], batch: list[dict[str, Any]]) -> str:
    cats = ', '.join(profile.get('categories') or ['business', 'technology', 'politics', 'economy', 'military'])
    kws = ', '.join(profile.get('keywords_include') or [])
    excl = ', '.join(profile.get('keywords_exclude') or '')
    country = profile.get('country', 'us')
    lines = []
    for i, item in enumerate(batch, start=1):
        content = re.sub(r'\s+', ' ', (item.get('content') or '')).strip()[:MAX_CONTENT_CHARS]
        lines.append(f"[{i}] 标题: {item.get('title','')}\n    摘要: {content}")
    return f"""你是抖音财经资讯频道的新闻筛选器。
目标领域: {cats}
关注关键词: {kws}
排除主题: {excl}
新闻来源国家: {country}

对下面每条新闻判断:
1) keep: 是否属于目标领域、或与关注关键词直接相关、且有传播价值 (true/false)
2) categories: 命中的目标领域列表, 可为空数组
3) sentiment: positive / negative / neutral

禁止输出任何思考过程或解释, 只输出严格 JSON, 格式:
{{"items": [{{"id": 1, "keep": true, "categories": ["military"], "sentiment": "negative"}}]}}

新闻列表:
{chr(10).join(lines)}"""


def _extract_json(text: str) -> Any:
    """Best-effort extraction of a JSON object (with 'items') or list from LLM output."""
    if not text:
        return None
    t = text.strip()
    candidates = [t]
    candidates.append(re.sub(r'```(?:json)?\s*|\s*```', '', t).strip())
    # balanced-brace candidates, left to right
    start = t.find('{')
    while start != -1:
        depth = 0
        for i in range(start, len(t)):
            ch = t[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    candidates.append(t[start:i + 1])
                    break
        start = t.find('{', start + 1)
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict) and isinstance(obj.get('items'), list):
            return obj
        if isinstance(obj, list) and obj:
            return {'items': obj}
    return None


def _parse_decisions(text: str, batch: list[dict[str, Any]]) -> dict[int, dict[str, Any]] | None:
    data = _extract_json(text)
    if data is None:
        return None
    entries = data.get('items') if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return None
    by_id: dict[int, dict[str, Any]] = {}
    for e in entries:
        if not isinstance(e, dict):
            return None
        iid = e.get('id')
        if type(iid) is not int or not 1 <= iid <= len(batch) or iid in by_id:
            return None
        if type(e.get('keep')) is not bool:
            return None
        cats = e.get('categories')
        if not isinstance(cats, list) or any(not isinstance(c, str) or c not in VALID_CATEGORIES for c in cats):
            return None
        if not isinstance(e.get('sentiment'), str) or e['sentiment'] not in VALID_SENTIMENTS:
            return None
        by_id[iid] = e
    return by_id or None


def _apply_decision(item: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    return dict(item, category_tags=decision['categories'], sentiment=decision['sentiment'])


def _parse_response(text: str, batch: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Compatibility helper: incomplete/invalid batches require fallback."""
    by_id = _parse_decisions(text, batch)
    if by_id is None or len(by_id) != len(batch):
        return None
    return [_apply_decision(item, by_id[i]) for i, item in enumerate(batch, 1) if by_id[i]['keep']]


def llm_filter_items(items: list[dict[str, Any]], profile: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Filter items via LLM. Returns (kept_items, mode) with mode in {'llm','mixed','rules'}."""
    items = hard_filter_items(items, profile)
    if not items:
        return [], 'rules'
    if not endpoint_alive():
        logger.info('[LLM-FILTER] endpoint unavailable, using rule filter')
        return filter_items(items, profile), 'rules'

    kept: list[dict[str, Any]] = []
    llm_batches = 0
    partial_fallback = False
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        try:
            text = _generate_with_retry(_build_prompt(profile, batch))
            res = _parse_decisions(text, batch)
        except Exception as e:
            logger.warning(f'[LLM-FILTER] batch failed: {e}')
            res = None
        if res is None:
            logger.warning('[LLM-FILTER] falling back to rule filter for remaining items')
            kept.extend(filter_items(items[i:], profile))
            return kept, 'mixed' if llm_batches else 'rules'
        for iid, item in enumerate(batch, 1):
            decision = res.get(iid)
            if decision is None:
                kept.extend(filter_items([item], profile))
                partial_fallback = True
            elif decision['keep']:
                kept.append(_apply_decision(item, decision))
        llm_batches += 1

    # enforce exclude keywords as a cheap safety net on top of LLM decisions
    exclude = [k.lower() for k in profile.get('keywords_exclude', [])]
    if exclude:
        kept = [
            it for it in kept
            if not any(k in f"{it.get('title','')}\n{it.get('content','')}".lower() for k in exclude)
        ]
    logger.info(f'[LLM-FILTER] model={get_settings().model} kept={len(kept)}/{len(items)} batches={llm_batches}')
    return kept, 'mixed' if partial_fallback else 'llm'

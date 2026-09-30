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

from ..tasks.control import TaskControlError
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


def _generate_with_retry(prompt: str, check_cancel=None) -> str:
    last_err = None
    for attempt in range(2):
        if check_cancel:
            check_cancel()
        try:
            result = _llm_chat([{'role': 'user', 'content': prompt}])
            if check_cancel:
                check_cancel()
            return result
        except TaskControlError:
            raise
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


def llm_filter_items(items: list[dict[str, Any]], profile: dict[str, Any], *, check_cancel=None, diagnostics=None) -> tuple[list[dict[str, Any]], str]:
    """Use the same decisions for filtering and diagnostic counts; no second inference."""
    from collections import Counter
    from .filters import hard_filter_reason
    if check_cancel:
        check_cancel()
    indexed = [dict(item, _filter_index=i) for i, item in enumerate(items)]
    decisions, candidates, fallbacks = {}, [], []
    def decide(item, reason):
        decisions[item['_filter_index']] = reason
    for item in indexed:
        reason = hard_filter_reason(item, profile)
        if reason:
            decide(item, reason)
        else:
            candidates.append(item)
    def rules(batch):
        kept = filter_items(batch, profile)
        keys = {item['_filter_index'] for item in kept}
        for item in batch:
            decide(item, 'kept_rule' if item['_filter_index'] in keys else 'not_relevant_rule')
        return kept
    def finish(kept, mode):
        if diagnostics is not None:
            counts = dict(Counter(decisions.values()))
            diagnostics.update(schema_version=1, fetched=len(indexed), after_hard_filter=len(candidates),
                kept=len(kept), filter_mode=mode, counts=counts, fallbacks=list(dict.fromkeys(fallbacks)),
                sample_limit=50, samples=[{'title': item.get('title', ''), 'url': item.get('url', ''),
                    'reason': decisions[i], 'kept': decisions[i].startswith('kept_')}
                    for i, item in enumerate(indexed[:50])])
        return [{k: v for k, v in item.items() if k != '_filter_index'} for item in kept], mode
    if not candidates:
        return finish([], 'rules')
    if profile.get('filter_mode') == 'rules':
        fallbacks.append('configured_rules')
        return finish(rules(candidates), 'rules')
    if not endpoint_alive():
        fallbacks.append('model_unavailable')
        return finish(rules(candidates), 'rules')
    kept, llm_batches, partial_fallback = [], 0, False
    for i in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[i:i + BATCH_SIZE]
        failure = 'invalid_response'
        try:
            if check_cancel:
                check_cancel()
            prompt = _build_prompt(profile, batch)
            text = _generate_with_retry(prompt, check_cancel=check_cancel) if check_cancel else _generate_with_retry(prompt)
            res = _parse_decisions(text, batch)
        except TaskControlError:
            raise
        except Exception:
            failure, res = 'request_failed', None
        if res is None:
            fallbacks.append(failure)
            kept.extend(rules(candidates[i:]))
            return finish(kept, 'mixed' if llm_batches else 'rules')
        for iid, item in enumerate(batch, 1):
            decision = res.get(iid)
            if decision is None:
                kept.extend(rules([item]))
                partial_fallback = True
                fallbacks.append('partial_response')
            else:
                decide(item, 'kept_ai' if decision['keep'] else 'not_relevant_ai')
                if decision['keep']:
                    kept.append(_apply_decision(item, decision))
        llm_batches += 1
    return finish(kept, 'mixed' if partial_fallback else 'llm')

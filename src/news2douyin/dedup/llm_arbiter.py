"""LLM arbiter for gray-zone dedup decisions.

For article pairs whose heuristic similarity falls in the ambiguous band
(GRAY_ZONE_LOW <= score < NEAR_DUP_THRESHOLD), ask the configured
OpenAI-compatible endpoint (MiniCPM5-2B via vLLM, same endpoint as the
collect LLM filter) whether the two articles report the same specific event.

Fail-safe design (mirrors collect/llm_filter.py):
- endpoint probe result is cached (TTL) to avoid a probe per call
- endpoint down / request error / unparseable JSON -> returns None,
  the caller keeps the heuristic decision (no merge)
- pairwise verdicts are cached in-memory for the life of the process
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request

from loguru import logger

OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL', 'http://127.0.0.1:19993/v1')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', 'vllm')
MODEL = os.getenv('MODEL', 'MiniCPM5-2B')
DEDUP_LLM_TIMEOUT = int(os.getenv('DEDUP_LLM_TIMEOUT', '30'))
PROBE_TTL_SECONDS = 60.0
MAX_SNIPPET_CHARS = 200
MAX_TOKENS = 80

_probe_at = 0.0
_probe_alive = False
_verdict_cache: dict[tuple[str, str], bool] = {}


def endpoint_alive(force: bool = False) -> bool:
    global _probe_at, _probe_alive
    if force or (time.time() - _probe_at) > PROBE_TTL_SECONDS:
        try:
            with urllib.request.urlopen(OPENAI_BASE_URL.rstrip('/') + '/models', timeout=3.0) as r:
                _probe_alive = 200 <= r.status < 300
        except Exception:
            _probe_alive = False
        _probe_at = time.time()
    return _probe_alive


def _pair_key(a_title: str, b_title: str) -> tuple[str, str]:
    ha = hashlib.sha1((a_title or '').encode('utf-8')).hexdigest()[:16]
    hb = hashlib.sha1((b_title or '').encode('utf-8')).hexdigest()[:16]
    return (ha, hb) if ha <= hb else (hb, ha)


def _extract_same_event(text: str) -> bool | None:
    if not text:
        return None
    t = text.strip()
    candidates = [t, re.sub(r'```(?:json)?\s*|\s*```', '', t).strip()]
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
        if isinstance(obj, dict) and isinstance(obj.get('same_event'), bool):
            return obj['same_event']
    return None


def _prompt(a_title: str, a_snippet: str, b_title: str, b_snippet: str) -> str:
    return f"""你是新闻去重判断器。判断下面两条新闻是否报道"同一具体事件"。
核心原则: 两条新闻的"核心事实"(谁、做了什么、发生了什么事)必须完全一致, 才算同一事件。
同一事件:
- 不同媒体对同一事实的不同措辞报道
- 标题的栏目前缀/媒体后缀/"[视频]"等差异
不是同一事件(务必严格区分):
1) 同一活动的不同阶段/场次/年份
2) 同一主题下的不同篇目: 如"XX开幕"新闻与"XX观察"评论; "A游戏提示"与"B游戏提示"
3) 对某事件的反应/批评/评价, 与该事件本身: 如"特朗普抨击美联储加息"是反应文章, 与"美联储加息"不是同一事件
4) 同一主体的不同活动: 如"出席会议"与"结束出席回到北京"、"发表讲话"与"签署协议"
5) 不同主体发表的不同言论
6) 信息不足以判断时, 输出 false

示例:
A"2026年国家网络安全宣传周开幕" vs B"2026年国家网络安全宣传周观察" => {{"same_event": false, "reason": "开幕新闻与观察评论是不同篇目"}}
A"[视频]中美经贸磋商在美国纽约举行" vs B"中美经贸磋商在美国纽约举行" => {{"same_event": true, "reason": "同一磋商, 仅前缀不同"}}
A"特朗普抨击美联储加息" vs B"美联储三年来首次加息" => {{"same_event": false, "reason": "反应文章与加息事件本身"}}
A"习近平结束出席金砖国家领导人第十八次会晤回到北京" vs B"习近平出席金砖国家领导人第十八次会晤第一阶段会议" => {{"same_event": false, "reason": "回到北京与出席会议是不同活动"}}

新闻A 标题: {a_title}
新闻A 摘要: {a_snippet}
新闻B 标题: {b_title}
新闻B 摘要: {b_snippet}

禁止输出思考过程或解释, 只输出严格 JSON: {{"same_event": true, "reason": "不超过15个字"}}"""


def llm_same_event(a_title: str, a_content: str, b_title: str, b_content: str) -> bool | None:
    """Ask the LLM whether two articles cover the same specific event.

    Returns True/False on a confident parseable verdict, or None when the
    endpoint is unavailable or the response is unparseable (caller falls
    back to the heuristic decision).
    """
    if not endpoint_alive():
        return None
    key = _pair_key(a_title, b_title)
    if key in _verdict_cache:
        return _verdict_cache[key]
    a_snip = re.sub(r'\s+', ' ', a_content or '').strip()[:MAX_SNIPPET_CHARS]
    b_snip = re.sub(r'\s+', ' ', b_content or '').strip()[:MAX_SNIPPET_CHARS]
    try:
        import requests
        url = OPENAI_BASE_URL.rstrip('/') + '/chat/completions'
        payload = {
            'model': MODEL,
            'messages': [{'role': 'user', 'content': _prompt(a_title, a_snip, b_title, b_snip)}],
            'temperature': 0.0,
            'max_tokens': MAX_TOKENS,
            'chat_template_kwargs': {'enable_thinking': False},
        }
        headers = {'Content-Type': 'application/json'}
        if OPENAI_API_KEY:
            headers['Authorization'] = f'Bearer {OPENAI_API_KEY}'
        r = requests.post(url, json=payload, headers=headers, timeout=DEDUP_LLM_TIMEOUT)
        r.raise_for_status()
        text = r.json()['choices'][0]['message']['content'] or ''
    except Exception as e:
        logger.warning(f'[DEDUP-LLM] request failed: {e}')
        return None
    verdict = _extract_same_event(text)
    if verdict is None:
        logger.warning(f'[DEDUP-LLM] unparseable response: {text[:120]!r}')
        return None
    _verdict_cache[key] = verdict
    return verdict

from __future__ import annotations

import re
from typing import Any

SECTOR_KEYWORDS = {
    'semiconductor': ['semiconductor', 'chip', 'chips', 'foundry', 'wafer', '晶圆', '半导体', '芯片'],
    'ai': ['artificial intelligence', 'ai', 'gpu', '算力', '大模型', '推理'],
    'ev': ['electric vehicle', 'ev', 'battery', '锂电', '新能源车'],
    'biotech': ['drug', 'biotech', 'pharma', 'clinical', '医药', '创新药'],
    'macro': ['fed', 'cpi', 'ppi', 'rate', 'yield', 'tariff', '关税', '利率', '通胀'],
    'energy': ['oil', 'gas', 'lng', 'coal', '原油', '天然气', '煤炭'],
}

SYMBOL_PATTERNS = [r'\b[A-Z]{2,5}\b', r'\b\d{6}\b']

POSITIVE = ['surge', 'beat', 'record high', 'rally', 'gain', 'rise', 'jump', '增长', '上涨', '利好']
NEGATIVE = ['fall', 'drop', 'miss', 'cut', 'warning', 'slump', '跌', '下滑', '利空', '裁员']


def _contains_any(text: str, keywords: list[str]) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in keywords)


def classify_categories(text: str, requested: list[str]) -> list[str]:
    out = []
    lt = text.lower()
    mapping = {
        'business': ['company', 'business', 'earnings', 'revenue', 'market', 'stock', '企业', '业绩', '股市'],
        'technology': ['technology', 'ai', 'chip', 'software', 'semiconductor', '半导体', '科技', '芯片'],
        'politics': ['government', 'policy', 'president', 'minister', 'congress', 'senate', '选举', '政策', '政府', '政治'],
        'economy': ['fed', 'inflation', 'rate', 'cpi', 'ppi', 'economy', 'macro', '宏观', '经济'],
        'military': ['military', 'defense', 'defence', 'troop', 'missile', 'army', 'navy', 'air force',
                     'militar', 'strike', 'weapons', 'militia', '基地', '导弹', '军事', '国防', '军演'],
    }
    for cat in requested:
        kws = mapping.get(cat.lower(), [cat.lower()])
        if any(k in lt for k in kws):
            out.append(cat)
    return out


def extract_sectors(text: str) -> list[str]:
    out = []
    lt = text.lower()
    for sector, keywords in SECTOR_KEYWORDS.items():
        if any(k.lower() in lt for k in keywords):
            out.append(sector)
    return out


def extract_symbols(text: str) -> list[str]:
    found = []
    for pat in SYMBOL_PATTERNS:
        found.extend(re.findall(pat, text or ''))
    clean = []
    for token in found:
        if token not in {'USD', 'CPI', 'PPI', 'CEO'}:
            clean.append(token)
    return sorted(set(clean))[:12]


def score_market_relevance(title: str, content: str, include_keywords: list[str]) -> float:
    text = f'{title}\n{content}'.lower()
    score = 0.0
    if 'stock' in text or 'market' in text or 'shares' in text or '股' in text:
        score += 2.0
    if 'fed' in text or 'cpi' in text or 'tariff' in text or '利率' in text or '关税' in text:
        score += 2.5
    if 'earnings' in text or 'guidance' in text or '业绩' in text:
        score += 2.0
    if 'semiconductor' in text or 'chip' in text or '半导体' in text:
        score += 2.0
    for kw in include_keywords:
        if kw.lower() in text:
            score += 0.6
    return round(score, 2)


def detect_sentiment(title: str, content: str) -> str:
    text = f'{title}\n{content}'.lower()
    pos = sum(1 for w in POSITIVE if w.lower() in text)
    neg = sum(1 for w in NEGATIVE if w.lower() in text)
    if pos > neg:
        return 'positive'
    if neg > pos:
        return 'negative'
    return 'neutral'


def enrich_item(item: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    text = f"{item.get('title','')}\n{item.get('content','')}"
    categories = classify_categories(text, profile.get('categories', []))
    sectors = extract_sectors(text)
    symbols = extract_symbols(text)
    include_keywords = profile.get('keywords_include', [])
    # Keep LLM-provided tags if present (llm_filter sets them before enrich runs);
    # otherwise fall back to rule-based classification.
    item['category_tags'] = item.get('category_tags') or categories
    item['sector_tags'] = sectors
    item['symbols'] = symbols
    item['market_relevance_score'] = score_market_relevance(item.get('title',''), item.get('content',''), include_keywords)
    item['sentiment'] = item.get('sentiment') or detect_sentiment(item.get('title',''), item.get('content',''))
    return item

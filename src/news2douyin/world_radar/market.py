"""Create bounded, source-backed market research leads from existing observations."""
from collections import defaultdict
from datetime import timedelta, timezone
import re
from urllib.parse import urlencode

from sqlalchemy import and_, or_
from sqlmodel import select

from ..candidates.models import SourceReport
from ..dedup.normalize import canonicalize_url
from ..selection.source_content import source_url, text, usable_title
from .events import CATEGORIES, classify as event_category


def _pattern(expression):
    return re.compile(expression, re.IGNORECASE)


_PRODUCTS = (
    ('copper', '铜', _pattern(
        r'铜(?:矿|精矿|冶炼|金属|期货|库存|供应|需求|产量)?(?!像|牌|鼓|钱|币|器|雕|陵|川|仁)|'
        r'\bcopper\b')),
    ('natural-gas', '天然气', _pattern(r'天然气|液化天然气|\bnatural[ -]gas\b|\bLNG\b')),
)
_SIGNALS = (
    ('policy', '政策与监管', _pattern(
        r'出口.{0,10}(?:禁令|禁运|关税|管制|配额|许可|限制)|'
        r'(?:进口|出口)税|(?:贸易|进口)制裁|'
        r'\b(?:export|import)[ -]+(?:ban|tariff|dut(?:y|ies)|controls?|quotas?|licen[cs](?:e|es|ing)|restrict\w*|embargo)\b|'
        r'\b(?:ban|restrict\w*|suspend\w*|tax\w*)\b.{0,55}\b(?:exports?|imports?)\b'),
     '可能通过贸易限制或成本变化作用于供给。',
     '核对正式文件的适用物项、地区、公布与生效日期。',
     ('分地区进出口量', '现货报价或地区价差')),
    ('industry', '产业运营', _pattern(
        r'(?:停产|关闭|停工|复产|重启|减产|增产|暂停生产|恢复生产)|'
        r'\b(?:shut(?:s|ting)?[ -]?down|shut\w*|clos(?:e[sd]?|ing|ure)|restart\w*|'
        r'halt\w*|suspend\w*|production cuts?|cuts? (?:output|production)|'
        r'(?:reduce|resume|curtail|increase|boost)\w* (?:output|production|operations))\b'),
     '可能通过设施产量或开工率改变可供货量。',
     '核对受影响的设施、产能和中断期限。',
     ('产量或开工率', '现货库存', '现货报价或升贴水')),
    ('transport', '运输与物流', _pattern(
        r'封锁|关闭|封闭|停运|停航|受损|中断|绕行|改道|恢复通航|'
        r'\b(?:clos(?:e[sd]?|ing|ure)|shut\w*|block\w*|damag\w*|disrupt\w*|suspend\w*|'
        r'rerout\w*|reopen\w*)\b'),
     '运输条件变化可能改变交付时间或运费。',
     '核对路线、承运货物、中断期限及可用的替代路线。',
     ('受影响线路的运量', '运费', '地区价差')),
    ('supply', '供需与库存', _pattern(
        r'(?:库存|储气量).{0,20}(?:升|降|增|减|超预期|低于预期|低位|高位)|'
        r'去库|累库|需求.{0,14}(?:升|降|增|减|萎缩)|'
        r'\b(?:inventor(?:y|ies)|[a-z-]+ stockpiles?|storage levels?|demand)\b.{0,30}\b'
        r'(?:ris\w*|fell|fall\w*|declin\w*|drop\w*|draw\w*|build\w*|'
        r'up|down|surge\w*|jump\w*|grow\w*|shrink\w*)\b'),
     '可能影响供需平衡或短期可交付量。',
     '核对库存、需求或储气的原始统计、统计期和比较基准。',
     ('库存或储气量', '消费量或订单', '现货报价')),
)
_FACILITY = _pattern(
    r'冶炼|矿山|铜矿|矿场|气田|产量|产能|生产|开工|'
    r'\b(?:mines?|mining|smelt\w*|plants?|gasfields?|production|output|facilit\w*)\b')
_ROUTE = _pattern(
    r'港口|港区|海峡|运河|航道|航线|铁路|刻赤(?:海峡)?大桥|'
    r'\b(?:ports?|straits?|canals?|shipping lanes?|railway|railroad|Kerch Bridge)\b')
_NEGATION = _pattern(
    r'尚未.{0,8}(?:关闭|停产|禁运)|(?:并未|没有|否认).{0,14}(?:停运|关闭|停产|中断|限制)|'
    r'\b(?:den(?:y|ies|ied)|ruled out|no plans? to|no reports? of|not planning)\b|'
    r'\b(?:shares?|stocks?|futures)\b.{0,16}\b(?:close|clos(?:es|ed|ing) higher|closing price)\b')
_SENTENCES = re.compile(r'[。！？!?；;\n]+|\.(?=\s|$)')
_RECALL_TERMS = (
    '铜', 'copper', '天然气', 'natural gas', 'natural-gas', 'LNG',
    '港口', '港区', '海峡', '运河', '航道', '航线', '铁路', '刻赤',
    'port', 'strait', 'canal', 'shipping lane', 'railway', 'railroad', 'kerch bridge',
)
_REPORT_LIMIT = 480


def _segments(title, excerpt):
    if title:
        yield title
    yield from (part.strip() for part in _SENTENCES.split(excerpt) if part.strip())


def classify(title, excerpt=''):
    """Return a hypothesis only if a visible passage connects the relevant terms."""
    for segment in _segments(title, excerpt):
        if _NEGATION.search(segment):
            continue
        products = [{'id': product_id, 'label': label} for product_id, label, pattern in _PRODUCTS
                    if pattern.search(segment)]
        for category, label, pattern, rationale, next_check, indicators in _SIGNALS:
            if not pattern.search(segment):
                continue
            if category == 'transport':
                if not _ROUTE.search(segment):
                    continue
                if not products:
                    indicators = indicators[:2]
            elif not products or (category == 'industry' and not _FACILITY.search(segment)):
                continue
            first = products[0] if products else {}
            return dict(state='hypothesis', category=category, channel=label,
                        products=products, product_id=first.get('id'), product_label=first.get('label'),
                        rationale=rationale, next_check=next_check, indicators=list(indicators),
                        evidence=text(segment, 520))
    return None


def _report_card(row):
    url = source_url(row.url)
    if not url or '\\' in url or not usable_title(row.title):
        return None
    match = classify(row.title, row.excerpt)
    if not match:
        return None
    category = event_category(row.title)
    if category == 'other':
        category = match['category']
    query = urlencode({'candidate': row.key})
    return dict(key=row.key, candidate_key=row.key, event_key='', moment_key='', title=row.title, excerpt=row.excerpt,
                category=category, category_label=CATEGORIES[category], stage='待核线索',
                observed_at=row.observed_at, occurred_on='', published_at=row.published_at or '',
                country='', place='', latitude=None, longitude=None, precision='unknown', geography_basis='',
                status='reported_lead',
                source_url=url, source_title=row.title, source_kind='report', source_path=url,
                event_path='/organize?' + query, market=match)


def _reports(session, start, end):
    # Only select small indexed observations. A report's country identifies its
    # media source and is deliberately excluded from geographic placement.
    since = _bound(start)
    cutoff = _bound(end)
    row = SourceReport
    possible = or_(*(column.icontains(term, autoescape=True)
                     for term in _RECALL_TERMS for column in (row.title, row.excerpt)))
    statement = (select(row.key, row.url, row.title, row.excerpt, row.observed_at, row.published_at)
                 .where(row.observed_at >= since, row.observed_at < cutoff,
                        or_(row.published_at == '', and_(row.published_at >= since, row.published_at < cutoff)),
                        possible)
                 .order_by(row.observed_at.desc(), row.key.desc()).limit(_REPORT_LIMIT + 1)
                 .execution_options(n2d_query_name='world.market.reports'))
    return session.exec(statement).all()


def _bound(moment):
    # SourceReport observations are stored at whole-second UTC precision.
    rounded = moment.replace(microsecond=0) + timedelta(seconds=bool(moment.microsecond))
    return rounded.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _references(card):
    title = ' '.join(card['title'].casefold().split())
    url = canonicalize_url(card['source_url']) if card.get('source_url') else ''
    day = (card.get('occurred_on') or card.get('published_at') or '')[:10]
    return [(value, day) for value in (title, url) if value]


def _already_seen(seen, references):
    # A definite distinct date is an eligible new development. With unknown
    # publication/occurrence, a recrawl alone does not establish a new action.
    return any(value in seen and (not day or '' in seen[value] or day in seen[value])
               for value, day in references)


def _remember(seen, references):
    for value, day in references:
        seen[value].add(day)


def _ranking(card):
    # Review concrete exposure first, then preserve the newest observation.
    match = card['market']
    return (bool(match['products']), card['observed_at'] or '',
            card.get('source_kind') == 'local', card['key'])


def leads(session, cards, *, start, end, limit=16, country='', category=''):
    """Use the shared event cards plus a prefilter pass independent of hotspot thresholds."""
    items = []
    seen = defaultdict(set)
    for card in cards:
        if country and (card.get('country') or '').lower() != country.lower():
            continue
        match = classify(card.get('source_title') or card['title'], card.get('excerpt') or '')
        if not match or (category and category not in (match['category'], card['category'])):
            continue
        items.append(dict(card, market=match))
        _remember(seen, _references(card))

    # A raw observation has no trusted action country for these filters.
    rows = [] if country else _reports(session, start, end)
    for row in rows[:_REPORT_LIMIT]:
        card = _report_card(row)
        if not card or (category and category not in (card['category'], card['market']['category'])):
            continue
        references = _references(card)
        if _already_seen(seen, references):
            continue
        items.append(card)
        _remember(seen, references)
    items.sort(key=_ranking, reverse=True)
    return dict(items=items[:limit], sampled=len(cards) + min(len(rows), _REPORT_LIMIT),
                truncated=len(rows) > _REPORT_LIMIT or len(items) > limit)

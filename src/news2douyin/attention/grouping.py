"""Conservative concrete-issue grouping and a compact SQL projection.

``prepare_batch`` reads bounded, narrow SQL candidates without a write lock.
The caller then computes lake.metrics for each prepared topic outside its SQL
transaction. ``apply_prepared`` publishes only the prepared latest state in a
short transaction. Neither phase calls a model, network service or DuckDB.

These are groups of source *leads*, not assertions that the headlines are true.
Similarity rules deliberately prefer a false split over merging two claims.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import hashlib
import json
import math
import re
import unicodedata
from urllib.parse import urlparse
from uuid import uuid4

from sqlmodel import Session, select

from ..dedup.normalize import canonicalize_url
from ..desk.service import enqueue_graph
from ..storage.articles import lock_news
from ..storage.models import utc_now_iso
from .models import AttentionItem, AttentionTopic


ALGORITHM = 'attention-group-v1'
WINDOW_HOURS = 72
MAX_TOPICS = 500
MAX_CANDIDATE_ITEMS = 5000
# Match the archive validator's 128 sources x 10,000 items. A valid manifest
# must not become a permanently failing inbox merely because its raw lists are
# larger than the collector's separately filtered article output. Archive byte
# limits are enforced by the lake; candidate/metric reads stay bounded below.
MAX_BATCH_ITEMS = 128 * 10000
MAX_METRIC_KEYS = 200

# These are lexical safeguards, not an entity/event extraction claim. Unknown
# names can still group through a high title overlap; a known person alone
# never provides sufficient evidence.
_ENTITIES = {
    'trump': ('特朗普', '川普', 'donald trump', 'trump'),
    'putin': ('普京', 'putin'), 'zelensky': ('泽连斯基', 'zelensky'),
    'biden': ('拜登', 'biden'), 'musk': ('马斯克', 'elon musk', 'musk'),
    'china': ('中国', 'china', 'chinese'), 'us': ('美国', 'united states', 'u.s.'),
    'russia': ('俄罗斯', '俄方', 'russia', 'russian'),
    'ukraine': ('乌克兰', '乌方', 'ukraine', 'ukrainian'),
    'france': ('法国', 'france', 'french'), 'poland': ('波兰', 'poland', 'polish'),
    'canada': ('加拿大', 'canada', 'canadian'), 'mexico': ('墨西哥', 'mexico'),
    'iran': ('伊朗', 'iran'), 'israel': ('以色列', 'israel'),
    'japan': ('日本', 'japan', 'japanese'), 'eu': ('欧盟', 'european union'),
    'india': ('印度', 'india', 'indian'), 'germany': ('德国', 'germany', 'german'),
}
_ACTIONS = {
    'tariff': ('关税', '进口税', 'tariff'),
    'ceasefire': ('停火', '休战', 'ceasefire', 'truce'),
    'sanction': ('制裁', 'sanction'), 'attack': ('袭击', '轰炸', '空袭', 'attack', 'bombing'),
    'election': ('选举', '大选', 'election'), 'visit': ('访问', '会晤', '会谈', 'visit', 'meeting'),
    'strike': ('罢工', 'strike'), 'coup': ('政变', 'coup'),
    'protest': ('抗议', '示威', '骚乱', 'protest', 'riot'),
    'quake': ('地震', 'earthquake'), 'flood': ('洪灾', '洪水', 'flood'),
    'launch': ('发射', 'launch'), 'resign': ('辞职', '下台', 'resign'),
    'arrest': ('逮捕', '拘捕', 'arrest'), 'acquire': ('收购', '并购', 'acquisition'),
}
_OBJECTS = {
    'steel': ('钢', 'steel'), 'aluminium': ('铝', 'aluminium', 'aluminum'),
    'car': ('汽车', '电动车', 'automobile', 'vehicle'),
    'chip': ('芯片', '半导体', 'semiconductor', 'chip'),
    'rare_earth': ('稀土', 'rare earth'), 'oil': ('石油', '原油', 'crude oil'),
}
_NEGATION = re.compile(r'否认|辟谣|并未|并非|并不|不会|没有|未曾|未对|未向|未能|不再|不对|不向|拒绝|取消|撤销|不实|虚假|(?<![a-z])(?:not|never|denies|denied|denial|false|refuses?|cancelled|canceled)(?![a-z])')
_PROPOSAL = re.compile(r'计划|拟对|拟将|考虑|提议|尚未|(?<![a-z])(?:plans?|proposes?|considering)(?![a-z])')
_ACTUAL = re.compile(r'宣布|签署|生效|实施|达成|(?<![a-z])(?:announces?|signed|enacted|agreed)(?![a-z])')
_NUMBERS = re.compile(r'\d+(?:[.,]\d+)*(?:\s*(?:%|％|万|亿|年|月|日|天|小时|人|美元|元|次))?')
_CN_NUMBERS = re.compile(r'百分之[零一二三四五六七八九十百千万亿两]+|[零一二三四五六七八九十百千万亿两]+(?:年|月|日|天|小时|人|美元|元|次|个百分点)')
_RELATIVE_TIME = re.compile(r'今日|今天|昨日|昨天|明天|明日|本周|下周|上周|周[一二三四五六日天]|星期[一二三四五六日天]|yesterday|today|tomorrow')
_CJK = re.compile(r'[\u3400-\u9fff]+')
_WORDS = re.compile(r'[a-z]+|\d+(?:\.\d+)?')
_GENERIC = {'最新消息', '突发消息', '重大消息', '突发重大消息', '重磅消息', '关注国际局势', '最新进展', '突发', 'breakingnews', 'latestnews'}


class PreparationConflict(RuntimeError):
    """Another publisher changed a topic after the unlocked preparation read."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _loads(value, default):
    try:
        result = json.loads(value)
        return result if isinstance(result, type(default)) else default
    except (TypeError, ValueError):
        return default


def _hash(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def _time(value):
    if not value:
        return ''
    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    except (TypeError, ValueError, OverflowError):
        return ''


def normalize_title(value):
    """Preserve words, numbers and negation; remove cosmetic punctuation only."""
    value = unicodedata.normalize('NFKC', str(value or '')).lower()
    # Punctuation is separated rather than deleted so English tokens cannot join.
    return re.sub(r'\s+', ' ', ''.join(c if c.isalnum() or c == '%' else ' ' for c in value)).strip()


def _has(text, alias):
    return bool(re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', text)) if alias.isascii() else alias in text


def _lexicon(text, table):
    return sorted(key for key, aliases in table.items() if any(_has(text, word) for word in aliases))


def _tokens(text):
    tokens = set(_WORDS.findall(text))
    for run in _CJK.findall(text):
        tokens.update(run[index:index + 2] for index in range(len(run)-1))
    return tokens


def title_signature(title):
    normalized = normalize_title(title)
    compact = normalized.replace(' ', '')
    entities = _lexicon(normalized, _ENTITIES)
    actions = _lexicon(normalized, _ACTIONS)
    # A headline containing only a name is a subject, not a concrete issue.
    remaining = normalized
    for aliases in _ENTITIES.values():
        for alias in aliases:
            remaining = remaining.replace(alias, '')
    generic = compact in _GENERIC or len(compact) < 6 or (entities and not actions and len(remaining.strip()) < 5)
    # A small visible-text subject guard catches unknown people/organisations
    # too: 张三/李四 or Alice/Alise cannot merge merely because their action is
    # identical. This is a conservative hint, not a resolved entity identifier.
    subject_hint = ''
    for entity, aliases in _ENTITIES.items():
        if any(normalized.startswith(alias) for alias in aliases):
            subject_hint = 'known:' + entity
            break
    if not subject_hint:
        prefix = re.split(r'宣布|表示|声称|计划|考虑|回应|否认|发布|将|拟|与|对|向|称|(?<![a-z])(?:announces?|denies|discusses?|plans?|visits?)(?![a-z])', normalized, maxsplit=1)[0].strip()
        if 1 < len(prefix) <= 12 and prefix != normalized:
            subject_hint = prefix
    return dict(normalized_title=normalized, entities=entities, actions=actions, subject_hint=subject_hint,
        objects=_lexicon(normalized, _OBJECTS), numbers=sorted(set(re.sub(r'\s+', '', value) for value in _NUMBERS.findall(normalized) + _CN_NUMBERS.findall(normalized))),
        relative_times=sorted(set(_RELATIVE_TIME.findall(normalized))),
        negated=bool(_NEGATION.search(normalized)), proposed=bool(_PROPOSAL.search(normalized)),
        actual=bool(_ACTUAL.search(normalized)), generic=bool(generic))


def _compatible(left, right):
    # Apply these even for reused URLs: a source may update a live story URL to
    # a different claim. Exact title is otherwise safe within the bounded window.
    if left['negated'] != right['negated']:
        return False
    if left['numbers'] != right['numbers']:
        return False
    for name in ('actions', 'objects', 'entities'):
        if left[name] and right[name] and left[name] != right[name]:
            return False
    for name in ('subject_hint', 'relative_times'):
        if left[name] and right[name] and left[name] != right[name]:
            return False
    if (left['proposed'] and right['actual'] and not right['proposed']) or (right['proposed'] and left['actual'] and not left['proposed']):
        return False
    return True


def _article_url(value):
    try:
        parsed = urlparse(value)
        return parsed.scheme in ('http', 'https') and bool(parsed.netloc) and parsed.path.lower().rstrip('/') not in ('', '/index', '/index.html', '/index.htm', '/home', '/news')
    except ValueError:
        return False


def _match(signature, candidate, url, observed):
    other = candidate['signature']
    left_time, right_time = _time(observed), candidate['last_seen']
    if not left_time or not right_time:
        return None
    difference = abs((datetime.fromisoformat(left_time.replace('Z', '+00:00')) - datetime.fromisoformat(right_time.replace('Z', '+00:00'))).total_seconds())
    if difference > WINDOW_HOURS * 3600 or not _compatible(signature, other):
        return None
    same_url = bool(url and _article_url(url) and url in candidate['urls'])
    if signature['generic'] or other['generic']:
        # Generic headlines only share an article within one day, never by name.
        first = _time(candidate['first_seen'])
        span = abs((datetime.fromisoformat(left_time.replace('Z', '+00:00')) - datetime.fromisoformat(first.replace('Z', '+00:00'))).total_seconds()) if first else float('inf')
        if same_url and max(difference, span) <= 24 * 3600:
            return ('same_url', 1.0, ['同一规范化文章链接；通用标题仅在24小时内归并'])
        return None
    if signature['normalized_title'] == other['normalized_title']:
        return ('same_title', 1.0, ['规范化标题完全一致；关键数字、否定和行为检查一致'])
    left, right = signature['normalized_title'], other['normalized_title']
    tokens_left, tokens_right = _tokens(left), _tokens(right)
    overlap = len(tokens_left & tokens_right) / max(1, len(tokens_left | tokens_right))
    similarity = SequenceMatcher(None, left, right, autojunk=False).ratio()
    # URL anchors are stronger than a near title but still require shared text
    # context, preventing a rolling URL from swallowing unrelated reports.
    if same_url and similarity >= .6 and overlap >= .35:
        return ('same_url', round(similarity, 4), ['同一规范化文章链接，且标题上下文相近', '关键数字、否定、主体和行为无冲突'])
    if not tokens_left or not tokens_right or min(len(left), len(right)) < 10:
        return None
    context = bool(signature['actions'] and other['actions'])
    subject = bool(set(signature['entities']) & set(other['entities'])) or left[:2] == right[:2]
    if context and subject and similarity >= .84 and overlap >= .58:
        return ('conservative_title', round(similarity, 4), ['标题高度相似，且主体、具体行为和关键数字一致', f'字符相似度{similarity:.2f}；词/字对重合度{overlap:.2f}'])
    if not signature['actions'] and not other['actions'] and subject and similarity >= .94 and overlap >= .8:
        return ('conservative_title', round(similarity, 4), ['标题几乎一致，关键主体、数字和否定无冲突', f'字符相似度{similarity:.2f}；词/字对重合度{overlap:.2f}'])
    return None


def _incoming(batch):
    incoming = {}
    sources = set()
    for source in batch.get('sources', []):
        if not isinstance(source, dict):
            continue
        source_id = str(source.get('source_id', ''))[:128]
        if source_id and source.get('status') != 'not_attempted':
            sources.add(source_id)
        if source.get('kind', source.get('source_kind')) == 'non_news':
            continue
        for raw in source.get('items', []):
            if not isinstance(raw, dict) or raw.get('source_kind', source.get('kind')) == 'non_news':
                continue
            title = str(raw.get('title', ''))[:4000]
            observed = _time(raw.get('observed_at') or source.get('observed_at') or batch.get('observed_at'))
            if not title.strip() or not observed:
                continue
            url = canonicalize_url(str(raw.get('canonical_url') or raw.get('url') or '')[:16000])
            lake_key = str(raw.get('key') or _hash(_json([source_id, raw.get('native_id') or url, normalize_title(title)])))
            if len(lake_key) > 128:
                raise ValueError('Attention lake item key exceeds limit')
            signature = title_signature(title)
            stamp = datetime.fromisoformat(observed.replace('Z', '+00:00')).timestamp()
            episode = int(stamp // ((24 if signature['generic'] else WINDOW_HOURS) * 3600))
            item_key = 'ati_' + _hash(_json([lake_key, episode]))[:40]
            value = dict(key=item_key, lake_key=lake_key, episode=episode, observed_at=observed,
                source_id=str(raw.get('source_id') or source_id)[:128], platform=str(raw.get('platform') or source.get('platform') or source_id)[:128],
                source_kind=str(raw.get('source_kind') or source.get('kind') or 'unknown')[:32],
                audience_scope=str(raw.get('audience_scope') or source.get('audience_scope') or 'unknown')[:32],
                native_id=str(raw.get('native_id', ''))[:512], title=title, signature=signature,
                normalized_title=signature['normalized_title'], url=str(raw.get('url') or url)[:16000],
                canonical_url=url, canonical_hash=_hash(url) if url else '', excerpt=str(raw.get('excerpt', ''))[:1000],
                list_position=raw.get('observed_list_position'), snapshot_ref=dict(
                    archive_ref=raw.get('archive_ref') or source.get('archive_ref', ''),
                    archive_sha256=raw.get('archive_sha256') or source.get('archive_sha256', ''),
                    observed_at=observed, snapshot_reported_at=_time(raw.get('snapshot_reported_at') or source.get('snapshot_reported_at')),
                    content_key=raw.get('content_key') or source.get('content_key', ''),
                    raw_metric_text=str(raw.get('raw_metric_text', ''))[:256], metric_raw=raw.get('metric_raw', {}),
                    position_kind='returned_list_position'))
            previous = incoming.get(item_key)
            if previous is None or (observed, _json(value)) > (previous['observed_at'], _json(previous)):
                incoming[item_key] = value
            if len(incoming) > MAX_BATCH_ITEMS:
                raise ValueError('Attention normalized batch exceeds grouping item limit')
    return sorted(incoming.values(), key=lambda row: (row['observed_at'], row['source_id'], row['key'])), sources


_TOPIC_COLUMNS = (AttentionTopic.key, AttentionTopic.title, AttentionTopic.normalized_title,
    AttentionTopic.normalized_hash, AttentionTopic.signature_json, AttentionTopic.first_seen,
    AttentionTopic.last_seen, AttentionTopic.generation)
_ITEM_COLUMNS = (AttentionItem.key, AttentionItem.topic, AttentionItem.source_id,
    AttentionItem.canonical_url, AttentionItem.canonical_hash, AttentionItem.title,
    AttentionItem.first_seen, AttentionItem.last_seen, AttentionItem.grouping_json)


def _candidate(row):
    key, title, normalized, normalized_hash, signature_json, first, last, generation = row
    # Recompute from visible text rather than trusting an older algorithm's shape.
    return dict(key=key, title=title, normalized_title=normalized, normalized_hash=normalized_hash,
        signature=title_signature(title), first_seen=first, last_seen=last, generation=generation, urls=set())


def prepare_batch(session: Session, batch: dict) -> dict:
    """Prepare JSON-safe upserts using bounded read-only SQL candidate queries.

    For every ``prepared['topics'][key]`` compute
    ``lake.metrics(data['item_keys'], now=prepared['observed_at'])`` outside the
    SQL transaction, then pass that mapping to ``apply_prepared``.
    """
    incoming, sources = _incoming(batch)
    observed = _time(batch.get('observed_at')) or max((row['observed_at'] for row in incoming), default='')
    if not observed:
        return dict(grouping_version=ALGORITHM, observed_at='', topics={})
    moment = datetime.fromisoformat(observed.replace('Z', '+00:00'))
    cutoff = (moment - timedelta(hours=WINDOW_HOURS)).isoformat(timespec='seconds').replace('+00:00', 'Z')
    ceiling = (moment + timedelta(hours=WINDOW_HOURS)).isoformat(timespec='seconds').replace('+00:00', 'Z')
    recent = session.exec(select(*_TOPIC_COLUMNS).where(AttentionTopic.last_seen >= cutoff,
        AttentionTopic.first_seen <= ceiling).order_by(AttentionTopic.last_seen.desc(), AttentionTopic.key).limit(MAX_TOPICS)).all()
    candidates = {row[0]: _candidate(row) for row in recent}
    hashes = sorted({_hash(item['normalized_title']) for item in incoming})
    # Indexed exact-title recovery prevents a busy 500-topic window hiding an
    # existing exact match. Verify actual text later rather than trust the hash.
    for offset in range(0, len(hashes), 500):
        rows = session.exec(select(*_TOPIC_COLUMNS).where(AttentionTopic.normalized_hash.in_(hashes[offset:offset+500]),
            AttentionTopic.last_seen >= cutoff, AttentionTopic.first_seen <= ceiling)
            .order_by(AttentionTopic.last_seen.desc(), AttentionTopic.key).limit(MAX_TOPICS)).all()
        for row in rows:
            if len(candidates) < MAX_TOPICS * 2 or row[0] in candidates:
                candidates[row[0]] = _candidate(row)
    member_rows = []
    if candidates:
        member_rows = session.exec(select(*_ITEM_COLUMNS).where(AttentionItem.topic.in_(list(candidates)),
            AttentionItem.last_seen >= cutoff).order_by(AttentionItem.last_seen.desc(), AttentionItem.key)
            .limit(MAX_CANDIDATE_ITEMS)).all()
    # Retry/identity lookup is bounded by this batch, not a full historical scan.
    exact_members = {}
    keys = [item['key'] for item in incoming]
    for offset in range(0, len(keys), 500):
        rows = session.exec(select(*_ITEM_COLUMNS).where(AttentionItem.key.in_(keys[offset:offset+500]))).all()
        for row in rows:
            exact_members[row[0]] = row
    url_rows = []
    url_hashes = sorted({item['canonical_hash'] for item in incoming if item['canonical_hash']})
    for offset in range(0, len(url_hashes), 500):
        remaining = MAX_CANDIDATE_ITEMS - len(url_rows)
        if remaining <= 0:
            break
        url_rows.extend(session.exec(select(*_ITEM_COLUMNS).where(
            AttentionItem.canonical_hash.in_(url_hashes[offset:offset+500]), AttentionItem.last_seen >= cutoff,
            AttentionItem.last_seen <= ceiling).order_by(AttentionItem.last_seen.desc(), AttentionItem.key)
            .limit(remaining)).all())
    # Hashes keep indexed keys small; _match verifies the actual canonical URL.
    missing = sorted({row[1] for row in list(exact_members.values()) + url_rows} - candidates.keys())
    if missing:
        for offset in range(0, len(missing), 500):
            for row in session.exec(select(*_TOPIC_COLUMNS).where(AttentionTopic.key.in_(missing[offset:offset+500]))):
                candidates[row[0]] = _candidate(row)
        remaining = max(0, MAX_CANDIDATE_ITEMS-len(member_rows))
        if remaining:
            member_rows += session.exec(select(*_ITEM_COLUMNS).where(AttentionItem.topic.in_(missing),
                AttentionItem.last_seen >= cutoff).order_by(AttentionItem.last_seen.desc(), AttentionItem.key)
                .limit(remaining)).all()
    members = defaultdict(list)
    members_by_lake = defaultdict(list)
    refresh = set()
    seen_members = set()
    for row in member_rows + list(exact_members.values()) + url_rows:
        key, topic, source, url, _url_hash, _title, first, last, grouping = row
        if key in seen_members:
            continue
        seen_members.add(key)
        detail = _loads(grouping, {})
        lake_key = detail.get('lake_key')
        if lake_key:
            members[topic].append((last, lake_key))
            members_by_lake[lake_key].append(row)
        if topic in candidates and url:
            candidates[topic]['urls'].add(url)
        if source in sources and cutoff <= last <= ceiling:
            refresh.add(topic)
    inverted = defaultdict(set)
    title_index = defaultdict(set)
    url_index = defaultdict(set)

    def index(candidate):
        title_index[candidate['normalized_title']].add(candidate['key'])
        for token in _tokens(candidate['normalized_title']):
            inverted[token].add(candidate['key'])
        for url in candidate['urls']:
            url_index[url].add(candidate['key'])

    for candidate in candidates.values():
        index(candidate)
    prepared = {}

    def touch(candidate):
        key = candidate['key']
        if key not in prepared:
            prepared[key] = dict(key=key, title=candidate['title'], normalized_title=candidate['normalized_title'],
                normalized_hash=_hash(candidate['normalized_title']), signature=candidate['signature'],
                is_new=candidate.get('is_new', False), base_generation=candidate.get('generation'),
                first_seen=candidate['first_seen'], last_seen=candidate['last_seen'], items=[])
        return prepared[key]

    for item in incoming:
        prior = exact_members.get(item['key'])
        if prior is None:
            # An episode bucket bounds *new* identities after an inactivity
            # gap. It must not duplicate a still-active source item every third
            # midnight. Reuse its membership only after the same concrete-issue
            # checks (including the 24h generic-title span) have passed.
            for member in sorted(members_by_lake.get(item['lake_key'], ()), key=lambda row: (row[7], row[0]), reverse=True):
                candidate = candidates.get(member[1])
                if candidate and _match(item['signature'], candidate, item['canonical_url'], item['observed_at']):
                    prior = member
                    item = dict(item, key=member[0])
                    break
        chosen = candidates.get(prior[1]) if prior else None
        decision = None
        if chosen:
            # Identity lookup also verifies the original canonical title, since
            # neither a truncated hash nor a reused native ID is semantic proof.
            old = _loads(prior[-1], {})
            if old.get('lake_key') == item['lake_key']:
                decision = old
            else:
                chosen = None
        if chosen is None:
            overlapping = Counter()
            for token in _tokens(item['normalized_title']):
                overlapping.update(inverted.get(token, ()))
            shortlist = set(key for key, _ in overlapping.most_common(25))
            shortlist.update(title_index.get(item['normalized_title'], ()))
            shortlist.update(url_index.get(item['canonical_url'], ()))
            matches = []
            for key in sorted(shortlist):
                result = _match(item['signature'], candidates[key], item['canonical_url'], item['observed_at'])
                if result:
                    matches.append((result[1], key, result))
            if matches:
                _score, key, result = sorted(matches, key=lambda value: (-value[0], value[1]))[0]
                chosen = candidates[key]
                decision = dict(algorithm=ALGORITHM, method=result[0], score=result[1], reasons=result[2])
            else:
                key = 'att_' + _hash(_json([item['lake_key'], item['episode']]))[:40]
                chosen = dict(key=key, title=item['title'], normalized_title=item['normalized_title'],
                    normalized_hash=_hash(item['normalized_title']), signature=item['signature'],
                    first_seen=item['observed_at'], last_seen=item['observed_at'], generation=None,
                    is_new=True, urls=set())
                candidates[key] = chosen
                decision = dict(algorithm=ALGORITHM, method='new_topic', score=None,
                    reasons=['未找到可验证的同一具体议题；不因共享人物或国家而合并'])
            if item['canonical_url']:
                chosen['urls'].add(item['canonical_url'])
            index(chosen)
        detail = dict(decision or {}, lake_key=item['lake_key'], episode=item['episode'])
        value = touch(chosen)
        value['first_seen'] = min(value['first_seen'], item['observed_at'])
        value['last_seen'] = max(value['last_seen'], item['observed_at'])
        row = {key: field for key, field in item.items() if key not in ('signature', 'lake_key', 'episode', 'observed_at', 'snapshot_ref')}
        row.update(topic=chosen['key'], first_seen=item['observed_at'], last_seen=item['observed_at'],
            snapshot_ref_json=_json(item['snapshot_ref']), grouping_json=_json(detail))
        value['items'].append(row)
        members[chosen['key']].append((item['observed_at'], item['lake_key']))
    # An empty/failed/changed source still refreshes previously seen issues so
    # the UI can distinguish a source outage or absence from measured zero.
    for key in sorted(refresh):
        if key in candidates:
            touch(candidates[key])
    for key, topic in prepared.items():
        all_keys = list(dict.fromkeys(lake_key for _when, lake_key in sorted(members[key], reverse=True)))
        topic['item_keys'] = all_keys[:MAX_METRIC_KEYS]
        topic['member_keys_truncated'] = len(all_keys) > MAX_METRIC_KEYS or len(member_rows) >= MAX_CANDIDATE_ITEMS
    return dict(grouping_version=ALGORITHM, observed_at=observed, topics=prepared)


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _metrics(value, truncated):
    """Preserve the lake contract, bounded for the operational SQL row."""
    value = dict(value or {})
    value['attention_score'] = _number(value.get('attention_score'))
    value['rise_score'] = _number(value.get('rise_score'))
    value['platform_count'] = max(0, int(value.get('platform_count') or 0))
    value['item_count'] = max(0, int(value.get('item_count') or 0))
    value['baseline_state'] = str(value.get('baseline_state') or 'insufficient')[:80]
    if value['baseline_state'] == 'insufficient':
        value['rise_score'] = None
    value['platforms'] = list(value.get('platforms') or [])[:128]
    value['series'] = list(value.get('series') or [])[-48:]
    value['notes'] = list(value.get('notes') or [])[:20]
    value['grouping'] = dict(algorithm=ALGORITHM, member_keys_truncated=bool(truncated), material_kind='newsnow_metadata')
    if truncated:
        value['notes'].append('本议题仅汇总最近200个来源条目；原始观察完整保留。')
    return value


def apply_prepared(session: Session, prepared: dict, metrics_by_topic: dict) -> list[str]:
    """Publish prepared rows atomically; caller commits or rolls back.

    Reprepare on ``PreparationConflict``. Generation checks fence an unlocked
    read from a concurrent publisher. Exact replays do not mint new generations.
    """
    if prepared.get('grouping_version') != ALGORITHM:
        raise ValueError('Unsupported attention grouping preparation')
    topics = prepared.get('topics') or {}
    if not topics:
        return []
    lock_news(session, 'attention:publish', *('attention:' + key for key in sorted(topics)))
    current = {}
    topic_keys = sorted(topics)
    for offset in range(0, len(topic_keys), 500):
        current.update({row.key: row for row in session.exec(select(AttentionTopic).where(
            AttentionTopic.key.in_(topic_keys[offset:offset+500]))).all()})
    # Check every generation before adding any projection rows.
    for key, value in topics.items():
        row = current.get(key)
        if (row is None) != bool(value['is_new']) or (row and row.generation != value['base_generation']):
            raise PreparationConflict('Attention topic changed during preparation; reprepare the batch')
    item_keys = sorted({item['key'] for value in topics.values() for item in value['items']})
    current_items = {}
    for offset in range(0, len(item_keys), 500):
        current_items.update({row.key: row for row in session.exec(select(AttentionItem).where(
            AttentionItem.key.in_(item_keys[offset:offset+500]))).all()})
    touched = []
    for key, value in topics.items():
        topic = current.get(key)
        new = topic is None
        changed = new
        if new:
            topic = AttentionTopic(key=key, title=value['title'], normalized_title=value['normalized_title'],
                normalized_hash=value['normalized_hash'], signature_json=_json(value['signature']),
                first_seen=value['first_seen'], last_seen=value['last_seen'])
        for item in value['items']:
            existing = current_items.get(item['key'])
            if existing is None:
                existing = AttentionItem(**item)
                session.add(existing)
                current_items[item['key']] = existing
                changed = True
                continue
            if existing.topic != key:
                raise PreparationConflict('Attention item membership changed during preparation')
            first = min(existing.first_seen, item['first_seen'])
            if existing.first_seen != first:
                existing.first_seen = first
                changed = True
            if item['last_seen'] >= existing.last_seen:
                for field, field_value in item.items():
                    if field in ('key', 'topic', 'first_seen', 'grouping_json'):
                        continue
                    if getattr(existing, field) != field_value:
                        setattr(existing, field, field_value)
                        changed = True
        first, last = min(topic.first_seen, value['first_seen']), max(topic.last_seen, value['last_seen'])
        if (first, last) != (topic.first_seen, topic.last_seen):
            topic.first_seen, topic.last_seen = first, last
            changed = True
        metrics = _metrics(metrics_by_topic.get(key), value['member_keys_truncated'])
        metrics_time = _time(metrics.get('observed_at')) or prepared.get('observed_at', '')
        old_summary = _loads(topic.summary_json, {})
        old_time = _time(old_summary.get('observed_at'))
        if 'matching' in old_summary:
            # The match worker owns this small boundary/reasons object. Keeping
            # it also makes an unchanged replay independent of matcher timing.
            metrics['matching'] = old_summary['matching']
        # Absence of metrics is unknown, not an instruction to erase a summary.
        if key in metrics_by_topic and metrics_time >= old_time:
            metrics['observed_at'] = metrics_time
            summary, chart = _json(metrics), _json(metrics['series'])
            if (topic.summary_json, topic.chart_json) != (summary, chart):
                topic.summary_json, topic.chart_json = summary, chart
                for field in ('attention_score', 'rise_score', 'platform_count', 'item_count', 'baseline_state'):
                    setattr(topic, field, metrics[field])
                changed = True
        if changed:
            topic.generation = uuid4().hex
            topic.updated_at = utc_now_iso()
            topic.match_next_at = 0
            topic.match_error = ''
            session.add(topic)
            enqueue_graph(session, 'attention/' + key)
            touched.append(key)
    return touched

"""Bounded, explainable *candidate* links from observed attention to source events.

An official event is a coded source record, not our researched event. A shared
document is therefore evidence of a related report, never proof of identity or
truth. Rule scores order the review queue; they are not probabilities. No model,
HTTP, archive scan, or accepted WatchEvent membership is created here.
"""
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from sqlalchemy import delete, func, or_
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from .models import AttentionEventMatch, AttentionItem, AttentionTopic, AttentionWatchMatch
from ..dedup.normalize import canonicalize_url
from ..desk.focus_lifecycle import is_deleted, visible_watch
from ..desk.models import Watch
from ..desk.rules import ALIASES, hits, norm, terms
from ..desk.service import enqueue_graph
from ..intake.official_models import OfficialEvent, OfficialMention
from ..selection.service import CAMEO_LABELS
from ..storage.articles import lock_news

OBSERVATION_DAYS = 7
EVENT_LIMIT = 200
MENTION_LIMIT = 1000
ITEM_LIMIT = 12
MATCH_LIMIT = 12
WATCH_LIMIT = 200
REFRESH_SECONDS = 300
ERROR_RETRY_SECONDS = 900

# These are deliberately broad CAMEO root-code hints. They do not validate a
# specific treaty, amount, attack, sanction, or claim in the source headline.
_ACTIONS = {
    '援助': (('07',), ('援助', '援乌', '军援', 'aid', 'assistance', 'military aid')),
    '磋商': (('04',), ('谈判', '会谈', '磋商', '会晤', '谈话', 'negotiation', 'negotiations', 'talks', 'meeting', 'meet')),
    '合作': (('05', '06'), ('合作', 'cooperation', 'cooperate')),
    '调查': (('09',), ('调查', 'investigate', 'investigation', 'probe')),
    '拒绝': (('12',), ('拒绝', 'reject', 'rejected', 'refuse', 'refused')),
    '威胁': (('13',), ('威胁', 'threaten', 'threatened', 'threat')),
    '抗议': (('14',), ('抗议', '示威', '罢工', 'protest', 'protests', 'demonstration', 'on strike')),
    '军事姿态': (('15',), ('军演', '军事演习', 'military exercise', 'military exercises', 'military drill', 'naval drill')),
    '减少关系': (('16',), ('断交', '召回大使', 'sever ties', 'recall ambassador')),
    '强制措施': (('17',), ('制裁', '关税', 'sanction', 'sanctions', 'tariff', 'tariffs', '逮捕', 'arrest')),
    '武装行动': (('18', '19'), ('袭击', '空袭', '轰炸', '交火', 'attack', 'attacks', 'airstrike', 'airstrikes', 'bombing', 'clash', 'clashes')),
}
_PEOPLE = {
    '特朗普': ('donald trump', 'trump', '川普'),
    '拜登': ('joe biden', 'biden'),
    '普京': ('vladimir putin', 'putin'),
    '泽连斯基': ('volodymyr zelensky', 'zelensky', 'zelenskyy'),
    '马克龙': ('emmanuel macron', 'macron'),
    '联合国': ('united nations',),
    '北约': ('nato', 'north atlantic treaty organization'),
}
_NEGATIVE = re.compile(r'否认|辟谣|并未|没有|尚未|未曾|未发生|取消|未获证实|不实|'
                       r'\b(?:denies?|denied|not|never|cancelled|canceled|false|debunked)\b', re.I)
_TENTATIVE = re.compile(r'是否发生|会不会|传闻|据传|可能会|尚待证实|尚未证实|'
                        r'\b(?:rumou?rs?|alleged|might|could|unconfirmed)\b', re.I)
_QUANTITY = re.compile(r'\d+(?:[.,]\d+)?\s*(?:%|％|亿|万|美元|欧元|元|吨|枚|架|艘|人|亿美元|billion|million|percent|dollars)')
_EVENT_FIELDS = ('event_id', 'event_date', 'last_seen', 'actor1_name', 'actor2_name',
    'actor1_country_iso2', 'actor2_country_iso2', 'event_root_code', 'action_country_iso2',
    'action_place', 'source_url')


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _loads(value, fallback):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return fallback


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _contains(text, phrase):
    phrase = norm(phrase)
    return bool(phrase and (re.search(r'(?<![a-z0-9])' + re.escape(phrase) + r'(?![a-z0-9])', text)
                           if phrase.isascii() else phrase in text))


def _safe_url(value):
    try:
        p = urlsplit(value or '')
        if p.scheme.lower() not in ('https', 'http') or not p.hostname or p.username or p.password:
            return ''
        return canonicalize_url(value)
    except ValueError:
        return ''


@lru_cache(maxsize=1)
def _entities():
    rows = json.loads((Path(__file__).resolve().parents[1] / 'server/data/countries.json').read_text(encoding='utf-8'))
    result = []
    for row in rows:
        # Never substring-match two/three-letter country codes: US also occurs
        # inside Russia, and IN is an ordinary English word.
        names = [row['zh'], row['en']]
        names.extend(ALIASES.get(row['zh'], ()))
        result.append(dict(key='country:' + row['a2'].lower(), label=row['zh'],
                           iso=row['a2'].lower(), names=tuple(dict.fromkeys(norm(x) for x in names))))
    for label, names in _PEOPLE.items():
        result.append(dict(key='actor:' + label, label=label, iso='',
                           names=tuple(dict.fromkeys(norm(x) for x in (label, *names, *ALIASES.get(label, ()))))))
    return tuple(result)


def _explicit_dates(text, observed):
    dates = []
    for year, month, day in re.findall(r'(?<!\d)(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?', text):
        try:
            dates.append(date(int(year), int(month), int(day)))
        except ValueError:
            pass
    for month, day in re.findall(r'(?<![\d年])(\d{1,2})月(\d{1,2})日', text):
        try:
            dates.append(date(observed.year, int(month), int(day)))
        except ValueError:
            pass
    years = [int(y) for y in re.findall(r'(?<!\d)(20\d{2})年', text)]
    return list(dict.fromkeys(dates)), years


def _signals(text, observed):
    text = norm(text or '')[:2500]
    entities = [e for e in _entities() if any(_contains(text, name) for name in e['names'])]
    actions = [dict(label=label, roots=roots, matched=[p for p in phrases if _contains(text, p)])
               for label, (roots, phrases) in _ACTIONS.items() if any(_contains(text, p) for p in phrases)]
    dates, years = _explicit_dates(text, observed)
    return dict(text=text, entities=entities, actions=actions, dates=dates, years=years,
                negative=bool(_NEGATIVE.search(text)), tentative=bool(_TENTATIVE.search(text)),
                quantity=bool(_QUANTITY.search(text)))


def _date_issue(event, signals, observed):
    try:
        occurred = date.fromisoformat(event['event_date'][:10])
    except ValueError:
        return '来源事件缺少可核对的发生日期'
    if signals['dates'] and min(abs((occurred - d).days) for d in signals['dates']) > 2:
        return '标题明确日期与来源事件发生日期相差超过2天'
    if signals['years'] and occurred.year not in signals['years']:
        return '标题明确年份与来源事件发生年份不一致'
    if not signals['dates'] and not signals['years'] and abs((observed.date() - occurred).days) > OBSERVATION_DAYS:
        return '最近被观测不等于最近发生：来源事件发生日期与本次热点相隔超过7天'
    return ''


def _entity_fields(entity, event):
    fields = []
    for name in ('actor1_name', 'actor2_name'):
        if any(_contains(norm(event[name]), word) for word in entity['names']):
            fields.append(name)
    if entity['iso']:
        for name in ('actor1_country_iso2', 'actor2_country_iso2', 'action_country_iso2'):
            if (event[name] or '').lower() == entity['iso']:
                fields.append(name)
        if any(_contains(norm(event['action_place']), word) for word in entity['names']):
            fields.append('action_place')
    return fields


def _semantic(event, signals, observed):
    if signals['negative'] or signals['tentative'] or signals['quantity']:
        return None
    if _date_issue(event, signals, observed):
        return None
    found = [(e, _entity_fields(e, event)) for e in signals['entities']]
    found = [(e, fields) for e, fields in found if fields]
    actors = [(e, fields) for e, fields in found if any(f.startswith('actor') for f in fields)]
    actions = [a for a in signals['actions'] if event['event_root_code'] in a['roots']]
    if not actors or not actions:
        return None
    # One person and a broad action code do not distinguish the issue. Require
    # another entity/country, or an explicit compatible calendar date.
    if len(found) < 2 and not signals['dates']:
        return None
    reasons = [f"主体/地区命中 {e['label']}：" + '、'.join(fields) for e, fields in found]
    reasons.append('行为线索 CAMEO ' + event['event_root_code'] + ' ' +
                   CAMEO_LABELS.get(event['event_root_code'], '') + '；标题词：' +
                   '、'.join(p for a in actions for p in a['matched']))
    if signals['dates']:
        reasons.append('标题日期与 event_date 相容（容差2天）')
    reasons.extend(('event_date=' + event['event_date'] + '；最近观测=' + event['last_seen'],
                    '仅主体、宽泛行为及地区/日期相容；待核原文，不能据此认定为同一事实'))
    return dict(method='structured_candidate', score=min(79, 45 + len(found) * 8 + (6 if signals['dates'] else 0)), reasons=reasons)


def _anchor_conditions(signals):
    entities = {e['key']: e for signal in signals for e in signal['entities']}
    conditions = []
    for entity in list(entities.values())[:12]:
        if entity['iso']:
            conditions.extend(column == entity['iso'] for column in
                              (OfficialEvent.actor1_country_iso2, OfficialEvent.actor2_country_iso2,
                               OfficialEvent.action_country_iso2))
        # One canonical English/surname alias plus the native label suffices
        # for bounded recall; all aliases remain available for field checking.
        names = list(entity['names'])[:4]
        conditions.extend(func.lower(column).contains(word, autoescape=True)
                          for column in (OfficialEvent.actor1_name, OfficialEvent.actor2_name)
                          for word in names)
    return conditions


def prepare_match(session, topic_key, now=None):
    """Read a bounded candidate snapshot. Caller can close it before writing."""
    observed = _now(now)
    topic = session.get(AttentionTopic, topic_key)
    if not topic:
        raise KeyError('热点议题不存在')
    items = session.exec(select(AttentionItem.title, AttentionItem.excerpt, AttentionItem.url,
                               AttentionItem.canonical_url, AttentionItem.last_seen)
                        .where(AttentionItem.topic == topic_key)
                        .order_by(AttentionItem.last_seen.desc(), AttentionItem.key).limit(ITEM_LIMIT)).all()
    # Each headline is evaluated separately: facts from unrelated members may
    # not be spliced together to satisfy actor/action conditions.
    texts = list(dict.fromkeys([topic.title, *(row[0] for row in items)]))
    signals = [_signals(text, observed) for text in texts if text]
    urls = {_safe_url(value) for row in items for value in (row[2], row[3])} - {''}
    raw_urls = {value for row in items for value in (row[2], row[3]) if _safe_url(value)} | urls
    cutoff = _iso(observed - timedelta(days=OBSERVATION_DAYS))
    ceiling = _iso(observed + timedelta(minutes=15))
    statement = select(*(getattr(OfficialEvent, field) for field in _EVENT_FIELDS)).where(
        OfficialEvent.last_seen >= cutoff, OfficialEvent.last_seen <= ceiling)
    anchors = _anchor_conditions(signals)
    if anchors:
        # source_url also stays inside this recent-observation window. Mentions
        # are never looked up by their unindexed document field globally.
        statement = statement.where(or_(*anchors, OfficialEvent.source_url.in_(sorted(raw_urls))))
    rows = session.exec(statement.order_by(OfficialEvent.last_seen.desc(), OfficialEvent.event_id)
                        .limit(EVENT_LIMIT + 1)).all()
    truncated = len(rows) > EVENT_LIMIT
    events = [dict(zip(_EVENT_FIELDS, row)) for row in rows[:EVENT_LIMIT]]
    mentions = []
    if urls and events:
        mentions = session.exec(select(OfficialMention.event_id, OfficialMention.doc_identifier)
                    .where(OfficialMention.event_id.in_([row['event_id'] for row in events]),
                           OfficialMention.last_seen >= cutoff, OfficialMention.last_seen <= ceiling)
                    .order_by(OfficialMention.last_seen.desc(), OfficialMention.key)
                    .limit(MENTION_LIMIT + 1)).all()
    mention_truncated = len(mentions) > MENTION_LIMIT
    mention_urls = {}
    for event_id, doc in mentions[:MENTION_LIMIT]:
        if _safe_url(doc) in urls:
            mention_urls.setdefault(event_id, []).append(_safe_url(doc))
    matches = []
    rejected_dates = 0
    for event in events:
        method = ''
        doc = _safe_url(event['source_url'])
        if doc and doc in urls:
            method = 'source_document_url'
        elif event['event_id'] in mention_urls:
            method, doc = 'mention_document_url', mention_urls[event['event_id']][0]
        if method:
            reasons = [('source_url' if method == 'source_document_url' else 'Mentions.doc_identifier') + ' 同文档链接：' + doc,
                       '同一报道可能提及多个事件；只形成相关报道候选，不证明同一事实']
            issues = list(dict.fromkeys(_date_issue(event, signal, observed) for signal in signals))
            reasons.extend(issue for issue in issues if issue)
            if any(signal['negative'] or signal['tentative'] for signal in signals):
                reasons.append('标题含否定/未证实线索，须核查报道立场')
            if any(signal['quantity'] for signal in signals):
                reasons.append('标题具体数值未由事件元数据核验')
            match = dict(method=method, score=95 if method == 'source_document_url' else 90, reasons=reasons)
        else:
            options = [result for signal in signals if (result := _semantic(event, signal, observed))]
            match = max(options, key=lambda result: result['score']) if options else None
            rejected_dates += int(bool(signals) and all(_date_issue(event, signal, observed) for signal in signals))
        if match:
            matches.append(dict(event_id=event['event_id'], **match))
    matches.sort(key=lambda row: (-row['score'], row['event_id']))
    watches = session.exec(select(Watch.key, Watch.name, Watch.scope, Watch.dimensions,
                                  Watch.exclude, Watch.version, Watch.enabled)
                    .where(visible_watch()).order_by(Watch.key).limit(WATCH_LIMIT + 1)).all()
    watch_matches = []
    for key, name, scope, dimensions, exclude, version, enabled in watches[:WATCH_LIMIT]:
        for signal in signals:
            text = signal['text']
            anchor = hits(terms(scope), text)
            dims = hits(terms(dimensions), text)
            if not anchor or hits(terms(exclude), text) or (terms(dimensions) and not dims):
                continue
            watch_matches.append(dict(watch=key, scope_version=version, reasons=[
                '仅关键词范围召回：' + '、'.join(anchor + dims),
                '当前专题版本：' + str(version),
                '专题已暂停；此处仅供范围查询，不表示持续跟踪' if not enabled else '候选关联；尚未归集到专题，不触发研究']))
            break
    reasons = []
    if not matches:
        reasons.append('本轮有界检索未形成可核查候选；不代表没有相关事件或关注度为零')
    if not any(signal['actions'] for signal in signals):
        reasons.append('缺少可辨识的具体行为；人物名本身不足以认定同一议题')
    if any(signal['quantity'] for signal in signals):
        reasons.append('具体数值无法由官方事件元数据核验，已跳过纯语义匹配')
    if any(signal['negative'] or signal['tentative'] for signal in signals):
        reasons.append('否定/未证实表述已跳过纯语义匹配')
    if rejected_dates:
        reasons.append(f'{rejected_dates}条来源记录发生时间明显不相容，未作语义关联')
    if truncated or mention_truncated:
        reasons.append('召回达到上限；相关记录可能在本轮检索范围之外')
    return dict(topic=topic_key, generation=topic.generation, checked_at=_iso(observed),
                checked_epoch=observed.timestamp(), matches=matches[:MATCH_LIMIT],
                watches=watch_matches[:MATCH_LIMIT], scope=dict(observation_days=OBSERVATION_DAYS,
                    observed_after=cutoff, observed_before=ceiling, entity_filtered=bool(anchors),
                    event_limit=EVENT_LIMIT, events_read=len(events), event_truncated=truncated,
                    mention_limit=MENTION_LIMIT, mentions_read=min(len(mentions), MENTION_LIMIT),
                    mention_truncated=mention_truncated, item_limit=ITEM_LIMIT, match_limit=MATCH_LIMIT,
                    watches_read=min(len(watches), WATCH_LIMIT), watches_truncated=len(watches) > WATCH_LIMIT,
                    score_type='rule_score_not_probability', comparison='recent_observation_not_event_date'),
                unmatched_reasons=reasons)


def _key(topic, target):
    return sha256((topic + '\0' + target).encode()).hexdigest()[:40]


def apply_match(session, prepared, now=None):
    """Publish only a current snapshot in a short transaction; caller commits."""
    topic_key = prepared['topic']
    lock_news(session, 'attention:' + topic_key,
              *('watch:' + row['watch'] for row in prepared['watches']))
    topic = session.get(AttentionTopic, topic_key)
    if (not topic or topic.generation != prepared['generation'] or
            topic.match_checked_at > prepared['checked_at']):
        return dict(changed=False, stale=True, topic=topic_key)
    when = _now(now) if now is not None else _now(prepared['checked_epoch'])
    stamp = _iso(when)
    # Historical manual decisions can accumulate over a long-lived topic. Only
    # the <=12 proposed IDs need to be loaded; old human decisions stay intact.
    proposed_ids = [row['event_id'] for row in prepared['matches']]
    old = {row.event_id: row for row in session.exec(select(AttentionEventMatch)
                   .where(AttentionEventMatch.topic == topic_key,
                          AttentionEventMatch.event_id.in_(proposed_ids)).limit(MATCH_LIMIT)).all()} if proposed_ids else {}
    present = set()
    for match in prepared['matches']:
        event_id = match['event_id']
        present.add(event_id)
        row = old.get(event_id) or AttentionEventMatch(key=_key(topic_key, event_id), topic=topic_key, event_id=event_id)
        # Human decisions are durable. New evidence can make the same link
        # current again, but may not silently reverse confirmed/rejected.
        row.method, row.score, row.reasons_json = match['method'], match['score'], _json(match['reasons'])
        row.generation, row.updated_at = topic.generation, stamp
        session.add(row)
    session.execute(delete(AttentionEventMatch).where(AttentionEventMatch.topic == topic_key,
        AttentionEventMatch.status == 'candidate', AttentionEventMatch.event_id.not_in(present))
        .execution_options(synchronize_session=False))
    proposed_watches = [row['watch'] for row in prepared['watches']]
    old_watches = {row.watch: row for row in session.exec(select(AttentionWatchMatch)
                        .where(AttentionWatchMatch.topic == topic_key,
                               AttentionWatchMatch.watch.in_(proposed_watches)).limit(MATCH_LIMIT)).all()} if proposed_watches else {}
    accepted_watches = set()
    for match in prepared['watches']:
        watch = session.get(Watch, match['watch'])
        if not watch or watch.version != match['scope_version'] or is_deleted(session, watch.key):
            continue
        accepted_watches.add(watch.key)
        row = old_watches.get(watch.key) or AttentionWatchMatch(key=_key(topic_key, watch.key), topic=topic_key, watch=watch.key)
        row.scope_version, row.reasons_json, row.updated_at = watch.version, _json(match['reasons']), stamp
        session.add(row)
    session.execute(delete(AttentionWatchMatch).where(AttentionWatchMatch.topic == topic_key,
                    AttentionWatchMatch.watch.not_in(accepted_watches)).execution_options(synchronize_session=False))
    summary = _loads(topic.summary_json, {})
    if not isinstance(summary, dict):
        summary = {}
    summary['matching'] = dict(scope=prepared['scope'], unmatched_reasons=prepared['unmatched_reasons'],
                               checked_at=prepared['checked_at'], event_candidates=len(prepared['matches']),
                               watch_candidates=len(accepted_watches))
    topic.summary_json = _json(summary)
    topic.match_checked_at, topic.match_error = stamp, ''
    topic.match_next_at = when.timestamp() + REFRESH_SECONDS
    topic.updated_at = stamp
    session.add(topic)
    enqueue_graph(session, 'attention/' + topic_key)
    return dict(changed=True, stale=False, topic=topic_key, events=len(prepared['matches']), watches=len(accepted_watches))


def refresh_topic(engine, topic_key, now=None):
    """Close the read transaction before any publication lock is acquired."""
    observed = _now(now)
    with Session(engine) as session:
        prepared = prepare_match(session, topic_key, now=observed)
    with Session(engine) as session:
        result = apply_match(session, prepared, now=observed)
        session.commit()
    return result


def step(engine):
    """Process at most one due recent topic; failure has a readable backoff."""
    now = _now()
    with Session(engine) as session:
        key = session.exec(select(AttentionTopic.key).where(AttentionTopic.match_next_at <= now.timestamp(),
                    AttentionTopic.last_seen >= _iso(now - timedelta(days=OBSERVATION_DAYS)))
                    .order_by(AttentionTopic.match_next_at, AttentionTopic.key).limit(1)).first()
    if not key:
        return False
    try:
        return bool(refresh_topic(engine, key, now=now)['changed'])
    except SQLAlchemyError:
        # A failed lock/connection must not trigger another database write.
        raise
    except Exception as exc:
        # One bounded retry-state write; if the database itself is unavailable,
        # propagate to the worker's outer backoff instead of a lock/error loop.
        with Session(engine) as session:
            lock_news(session, 'attention:' + key)
            topic = session.get(AttentionTopic, key)
            if topic:
                topic.match_error = (type(exc).__name__ + ': ' + str(exc))[:500]
                topic.match_next_at = now.timestamp() + ERROR_RETRY_SECONDS
                session.add(topic)
                session.commit()
        return True

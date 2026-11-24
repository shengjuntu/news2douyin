"""Explain one published daily card from the evidence available to that edition."""
from datetime import date, datetime, timezone
import json

from sqlmodel import select

from .sources import edition_memberships
from ..desk.focus_models import FocusCandidate
from ..desk.models import Watch, WatchEvent
from ..extraction.models import FocusPoint
from ..knowledge.models import PointMaterial
from ..selection.source_content import source_url
from ..storage.sql import time_value


def _materials(card):
    # Ranker samples can include the representative, or omit it on older issues.
    result = []
    seen = set()
    entries = [dict(url=card.get('source_url'), title=card.get('title'),
                    source_kind=card.get('source_kind'), source_key=card.get('source_key'))]
    entries.extend(card.get('sources') or ())
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = source_url(entry.get('url'))
        if url and '\\' not in url and url not in seen:
            result.append(dict(url=url, title=str(entry.get('title') or url), source_kind=entry.get('source_kind') or '',
                               source_key=entry.get('source_key') or ''))
            seen.add(url)
    return result


def _date(value):
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return ''


def _time(value):
    if isinstance(value, str):
        try:
            instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if instant.tzinfo is not None:
                return instant.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        except (ValueError, OverflowError):
            pass
    return ''


def _member_details(session, events, end):
    # Bound work to local events whose sources are actually accessible on the selected cards.
    # The membership edge suffices for relevance. A question requires a dated decision
    # for this version AND an actual material-to-outline link for the same local event.
    if not events:
        return {}
    memberships = {}
    for pos in range(0, len(events), 100):
        keys = events[pos:pos + 100]
        for event, name, key in session.exec(edition_memberships(keys, end)):
            memberships[event, name] = dict(watch_key=key, event=event)

        query = (select(PointMaterial.event, Watch.name, FocusPoint.text, FocusCandidate.questions_json)
                 .select_from(PointMaterial)
                 .join(FocusPoint, FocusPoint.key == PointMaterial.point)
                 .join(Watch, Watch.key == FocusPoint.watch)
                 .join(WatchEvent, (WatchEvent.watch == Watch.key) & (WatchEvent.event == PointMaterial.event))
                 .join(FocusCandidate, (FocusCandidate.watch == Watch.key) & (FocusCandidate.event == PointMaterial.event)
                       & (FocusCandidate.scope_version == Watch.version))
                 .where(PointMaterial.event.in_(keys), time_value(FocusPoint.created) < time_value(end),
                        WatchEvent.choice == 'auto', WatchEvent.branch == FocusPoint.text,
                        FocusCandidate.status == 'done', FocusCandidate.decision.in_(('core', 'background')),
                        time_value(FocusCandidate.updated) < time_value(end))
                 .order_by(FocusCandidate.updated.desc(), FocusCandidate.key))
        for event, name, question, encoded in session.exec(query):
            entry = memberships.get((event, name))
            if entry and 'question' not in entry:
                try:
                    supported = json.loads(encoded)
                except (ValueError, TypeError):
                    continue
                if isinstance(supported, list) and question in supported:
                    entry['question'] = question[:400]
    return memberships


def attach_focus(items, session, matcher, cutoff):
    """Snapshot evidence for accepted memberships and unreviewed title matches."""
    end = cutoff.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    memberships = set()
    material = {}
    for card in items:
        if card.get('watches') or card.get('focus_hints'):
            entries = material[card['key']] = _materials(card)
            if card.get('watches'):
                memberships.update(s['source_key'][6:] for s in entries if s['source_key'].startswith('local:'))
    links = _member_details(session, sorted(memberships), end)
    for card in items:
        entries = material.get(card['key'], ())
        evidence = {}
        for sample in entries:
            key = sample['source_key']
            if key.startswith('local:'):
                for name in card.get('watches', ()):
                    if name not in evidence and (entry := links.get((key[6:], name))):
                        evidence[name] = dict(entry, kind='membership', url=sample['url'])
            elif matcher and card.get('focus_hints'):
                hints = matcher.explain(sample['title'], card['focus_hints'])
                for name, hint in hints.items():
                    if name not in evidence:
                        evidence[name] = dict(hint, kind='scope_hint', url=sample['url'])
        if evidence:
            card['focus_evidence'] = evidence


def _focused(card):
    evidence = card.get('focus_evidence') or {}
    rows = []
    names = set()
    for source, kind in (('watches', 'membership'), ('focus_hints', 'scope_hint')):
        for name in card.get(source) or ():
            if not isinstance(name, str) or name in names:
                continue
            names.add(name)
            entry = evidence.get(name) or {}
            if entry.get('kind') != kind:
                entry = {}
            terms = entry.get('terms') or []
            if kind == 'membership':
                question = entry.get('question') or ''
                detail = ('关联纲要问题「' + question + '」，可继续核查。' if question else '同组本地事件已纳入该专题。')
            else:
                detail = ('来源标题命中关注词「' + '、'.join(terms) + '」，等待专题判读。' if terms else
                          '同组标题命中专题范围，等待专题判读。')
            rows.append(dict(kind=kind, label=name, detail=detail, watch_key=entry.get('watch_key') or '',
                             terms=terms, source_url=source_url(entry.get('url')), event=entry.get('event') or '',
                             question=entry.get('question') or ''))
    return rows


def _change(card, day):
    action = card.get('action_evidence') or {}
    if not source_url(card.get('source_url')) or action.get('url') != card.get('source_url'):
        action = {}
    coverage = card.get('official_coverage') or {}
    quote = action.get('quote') or ''
    if card.get('reconstruction'):
        kind, label, detail = 'reconstructed', '片段重建 · 待核对原文', '线索来自归档重建片段，仍需取得报道原文。'
    elif action.get('kind') == 'dated_action_report':
        from .signals import _dates_in
        dates = _dates_in(quote)
        if day and dates and dates != {day}:
            return dict(kind='action_lead', label='报道提到其他日期和行动',
                        detail='报道原句中的日期与本期日期不同，需按原文核查行动时间。', quote=quote)
        kind, label, detail = 'dated_action', '报道提到本期日期和行动', '同一条报道的句子里出现日期和行动表述，可核对引文。'
    elif action.get('kind') == 'stage_report':
        kind, label, detail = 'action_lead', '报道提到行动 · 日期待核', '来源标题有行动阶段表述，发生日期需要核对。'
    elif card.get('coverage_growth_only') or _count(coverage.get('new_documents')) > 0:
        kind = 'coverage_growth'
        label = '报道增加 · 行动进展待核' if card.get('coverage_growth_only') else '新增关联报道'
        detail = '本期出现该官方事件编号的新增关联文档；材料中的行动变化待核。'
    elif card.get('source_kind') == 'newsnow':
        kind, label, detail = 'attention', '平台关注线索', '本期在中文平台收录，行动进展需要查看原始报道。'
    else:
        kind, label, detail = 'new_material', '本期收录', '这份材料进入了本期清单，行动是否首次发生仍待核对。'
    return dict(kind=kind, label=label, detail=detail, quote=quote)


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def _evidence(card, links):
    counts = []
    limits = []
    for data, key, cap, unit in (
        (card, 'candidate_documents', 'candidate_documents_capped', '个去重候选链接'),
        (card, 'domain_count', 'domains_capped', '个报道域名'),
        (card.get('publisher_breadth') or {}, 'units', 'units_capped',
         '个媒体来源组' if card.get('selection_provider') == 'plugin' else '个排序来源组'),
    ):
        if type(data.get(key)) is int and data[key] >= 0:
            counts.append(f'{"至少 " if data.get(cap) else ""}{data[key]} {unit}')
    counts.append(f'保存 {len(links)} 个可查来源链接')
    if type(card.get('candidate_documents')) is not int or card['candidate_documents'] < 0:
        limits.append('本版只保存了来源样本，候选链接总数未知。')
    elif card['candidate_documents'] > len(links) or card.get('candidate_documents_capped'):
        limits.append('链接列表仅列出当期保存的来源样本。')

    coverage = card.get('official_coverage') or {}
    def known(field):
        return type(coverage.get(field)) is int and coverage[field] >= 0

    counts = ' · '.join(counts)
    official = []
    if known('documents'):
        official.append(f"共 {coverage['documents']} 篇去重文档")
    prefix = '' if coverage.get('history_complete') is True else '至少 '
    if known('new_documents'):
        official.append(f"本期首次关联 {prefix}{coverage['new_documents']} 篇报道")
    if known('new_domains'):
        official.append(f"首次出现 {prefix}{coverage['new_domains']} 个媒体域名")
    if official:
        limits.append('该官方事件的本地 Mentions 索引' + '，'.join(official) + '。')
    if coverage and coverage.get('history_complete') is not True:
        limits.append('部分首次观测资料缺失，新增数量只列已能确认的部分。')
    if card.get('publisher_breadth') or known('new_domains'):
        limits.append('多个域名可能转载同一稿件。来源组数量不能证明独立采访或交叉核实。')
    return dict(summary=counts, limits=limits, links=links)


def _timing(card, issue, change):
    kind = card.get('source_kind')
    clock = card.get('source_clock') or {}
    stamp = _time(card.get('source_observed_at'))
    latest = stamp or _time(card.get('observed_at'))
    first = ''
    first_basis, latest_basis = '', '清单内最近观测'
    local = _time(clock.get('local_received_at'))
    local_basis = '本机收到该版官方记录'
    day = _date(clock.get('event_day'))
    basis = 'GDELT 编码事件日期' if day else '发生日期待核'
    if clock.get('clock_kind') == 'official':
        first, latest = _time(clock.get('upstream_first')), _time(clock.get('upstream_last'))
        first_basis, latest_basis = 'GDELT 首次观测', 'GDELT 最近观测'
    elif clock.get('clock_kind') == 'reconstruction':
        local_basis = '本机取得重建片段'
    elif clock.get('clock_kind') == 'version':
        first, first_basis, latest_basis = _time(clock.get('first_local')), '该事件本期最早可读版本', '该事件本期最近可读版本'
    elif clock.get('clock_kind') == 'attention':
        first, first_basis, latest_basis = _time(clock.get('first_local')), '本地首次观测该热榜议题', '本地最近观测该热榜议题'
    elif kind == 'gkg' and stamp:
        first, first_basis = _time(card.get('earliest_gkg_receipt')), '本期同组 GKG 材料最早本地收录'
        local, local_basis = stamp, '本机收到代表报道所在批次'
        latest = ''
    key = (card.get('source_key') or '').split(':')
    restored = ''
    if len(key) == 4 and key[0] == 'gkg':
        sources = (issue.get('stats') or {}).get('sources') or {}
        restored = _time((sources.get('gkg_restored_batches') or {}).get(':'.join(key[1:3])))
    if not day and change['kind'] in ('dated_action', 'action_lead') and change['quote']:
        # Replays can bring in old titles today. Use only the actual date found
        # in the quoted source sentence, never the digest day or arrival date.
        from .signals import _dates_in
        dates = _dates_in(change['quote'])
        if len(dates) == 1 and (date_in_quote := _date(next(iter(dates)))):
            day, basis = date_in_quote, '报道原句中与行动同现的日期，待核'
    return dict(event_day=day, event_basis=basis, first_observed_at=first, first_basis=first_basis,
                latest_observed_at=latest, latest_basis=latest_basis, local_received_at=local,
                local_basis=local_basis if local else '', restored_at=restored)


def describe(card, issue):
    """No I/O. Old editions only expose the evidence retained in that revision."""
    links = _materials(card)
    change = _change(card, _date(issue.get('day')))
    context = card.get('actor_context')
    change['context'] = context if isinstance(context, dict) else None
    focus = _focused(card)
    return dict(change=change, evidence=_evidence(card, links), timing=_timing(card, issue, change),
                focus=focus, focus_note='' if focus else '本版未记录关联专题或范围命中。')

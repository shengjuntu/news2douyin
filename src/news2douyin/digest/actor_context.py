"""Freeze a bounded, source-indexed comparison of GDELT-coded recipients."""
from collections import defaultdict
import re
from urllib.parse import urlencode

from sqlalchemy import union_all
from sqlmodel import select

from ..intake.official_models import OfficialEvent as Event
from ..selection.source_content import source_url
from .calendar import day_bounds_utc, days_before

WINDOW_DAYS = 7
PER_COUNTRY = 256
COUNTRIES_PER_QUERY = 16
COLUMNS = (Event.event_id, Event.actor1_name, Event.actor2_name, Event.actor1_country, Event.event_code,
           Event.action_country_iso2, Event.action_place, Event.added_at, Event.last_seen, Event.ingested_at,
           Event.feed, Event.stamp, Event.row_index, Event.source_url)


def _norm(value):
    return ' '.join((value or '').casefold().split())


def _group(row):
    actor = _norm(row.actor1_name)
    target = _norm(row.actor2_name)
    place = _norm(row.action_place)
    unknown = {'', 'unknown', 'unspecified'}
    if (actor in unknown or target in unknown or place in unknown or not row.action_country_iso2
            or not re.fullmatch(r'[0-9]{2,4}', row.event_code)):
        return None
    # Compare official names as reported. This never claims to resolve identity/aliases.
    return (actor, _norm(row.actor1_country), row.event_code, row.action_country_iso2, place)


def _instant(moment):
    return moment.isoformat(timespec='seconds').replace('+00:00', 'Z')


def _country_samples(session, countries, start, end):
    """Each LIMIT runs inside its own indexed country/time range before role comparison."""
    result = defaultdict(list)
    for pos in range(0, len(countries), COUNTRIES_PER_QUERY):
        probes = []
        for country in countries[pos:pos + COUNTRIES_PER_QUERY]:
            recent = (select(*COLUMNS).where(Event.action_country_iso2 == country,
                        Event.last_seen >= start, Event.last_seen < end,
                        Event.added_at >= start, Event.added_at < end,
                        Event.ingested_at != '', Event.ingested_at < end)
                      .order_by(Event.last_seen.desc(), Event.event_id.desc()).limit(PER_COUNTRY + 1).subquery())
            probes.append(select(recent))
        stmt = union_all(*probes) if len(probes) > 1 else probes[0]
        for row in session.execute(stmt):
            result[row.action_country_iso2].append(row)
    # UNION ALL does not promise output order. Make the sentinel and example deterministic.
    for rows in result.values():
        rows.sort(key=lambda r: (r.last_seen, r.event_id), reverse=True)
    return result


def attach_actor_context(items, session, day, ceiling):
    """Only representative official records from this publication can be annotated.

    Retrieve selected primary keys in batches, then <=257 rows per represented action country.
    The compact index represents its latest local version. Backfilled or changed rows
    that cannot be established by the relevant cutoff never qualify as comparisons.
    """
    targets = {}
    for card in items:
        if card.get('source_kind') != 'gdelt':
            continue
        source = re.fullmatch(r'gdelt:([0-9]{1,30})', card.get('source_key') or '')
        if source:
            targets.setdefault(source[1], []).append(card)
    if not targets:
        return
    initial_day = days_before(day, WINDOW_DAYS)
    last_day = days_before(day, 1)
    start = _instant(day_bounds_utc(initial_day)[0])
    end = _instant(day_bounds_utc(day)[0])
    cutoff = _instant(ceiling)
    # Only use rows actually visible at this issue cutoff; the positional pointer
    # prevents a second read substituting a different Events export with similar values.
    anchors = {}
    event_ids = list(targets)
    for pos in range(0, len(event_ids), 500):
        query = select(*COLUMNS).where(Event.event_id.in_(event_ids[pos:pos + 500]))
        for row in session.execute(query):
            anchors[row.event_id] = row
    groups = {}
    for event_id, cards in targets.items():
        row = anchors.get(event_id)
        for card in cards:
            clock = card.get('source_clock') or {}
            valid = (row is not None and clock.get('clock_kind') == 'official'
                     and clock.get('event_ref') == [row.feed, row.stamp, row.row_index]
                     and clock.get('local_received_at') == row.ingested_at
                     and clock.get('upstream_last') == row.last_seen
                     and '' < row.ingested_at < cutoff and end <= row.last_seen < cutoff
                     and '' < row.added_at < cutoff)
            if valid and (group := _group(row)):
                groups.setdefault(group, []).append((card, row))
            else:
                card['actor_context'] = dict(status='unknown', summary='缺少可核对的官方行动双方、编码、地点或当期版本，无法比较受事。')
    if not groups:
        return

    countries = sorted({group[3] for group in groups})
    samples = _country_samples(session, countries, start, end)
    matches = defaultdict(list)
    for rows in samples.values():
        for past in rows[:PER_COUNTRY]:
            # added_at comes from the persisted Events export. A later mention
            # does not re-date the underlying action or change its participants.
            if (group := _group(past)) in groups:
                matches[group].append(past)

    for group, active in groups.items():
        country_pool = samples[group[3]]
        truncated = len(country_pool) > PER_COUNTRY
        sample_count = min(len(country_pool), PER_COUNTRY)
        related = matches[group]
        for card, now in active:
            current_recipient = _norm(now.actor2_name)
            same = next((past for past in related if _norm(past.actor2_name) == current_recipient), None)
            window = f'{initial_day} 至 {last_day}'
            scope = (f'对照 {window}（北京日期）的本地官方记录，施事「{now.actor1_name}」、'
                     f'CAMEO {now.event_code}、行动地「{now.action_place}」相同的记录')
            if same:
                status, reference = 'seen', same
                detail = f'共 {len(related)} 条，其中已有受事「{now.actor2_name}」。'
            elif related:
                status, reference = 'not_seen_in_sample', related[0]
                detail = f'共 {len(related)} 条，仅在这组样本中未见本条受事「{now.actor2_name}」，曾见「{reference.actor2_name}」。'
            else:
                status, reference = 'unknown', None
                detail = '有 0 条，受事变化待核。'
            note = (f'已截断到该行动国家最近 {sample_count} 条。' if truncated
                    else f'本地索引该国窗口取样 {sample_count} 条。')
            context = dict(status=status, summary=scope + detail + note, window_start=str(initial_day),
                           window_end=str(last_day), comparable_count=len(related), sample_count=sample_count,
                           truncated=truncated)
            if reference:
                params = urlencode({'source':'gdelt', 'event':reference.event_id, 'evidence_day':str(last_day)})
                context['example'] = dict(event_id=reference.event_id, recipient=reference.actor2_name,
                                          source_url=source_url(reference.source_url), detail_url='/radar?'+params,
                                          source_observed_at=reference.added_at, local_received_at=reference.ingested_at)
            card['actor_context'] = context

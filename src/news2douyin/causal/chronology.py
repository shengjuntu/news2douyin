"""Conservative temporal intervals for occurrence and public evidence."""
from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from ..search.dates import timezone_name

UTC = timezone.utc
DATE = re.compile(r'\d{4}-\d{2}-\d{2}\Z')
LITERAL = re.compile(r'(?<!\d)\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)?(?!\d)')
CHINESE_DATE = re.compile(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})[日号]')
RELATIVE = re.compile(r'今天|今日|昨天|昨日|前天')


def interval(value):
    if not value or not isinstance(value, str):
        return None
    try:
        if DATE.fullmatch(value):
            zone = ZoneInfo(timezone_name())
            day = date.fromisoformat(value)
            return (datetime.combine(day, time.min, zone).astimezone(UTC),
                    datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC))
        if 'T' not in value and ' ' not in value:
            return None
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name()))
        point = parsed.astimezone(UTC)
        return point, point
    except (ValueError, OverflowError):
        return None


def inferred(text, now):
    values = {match.group() for match in LITERAL.finditer(text)}
    for match in CHINESE_DATE.finditer(text):
        try:
            values.add(date(*(int(part) for part in match.groups())).isoformat())
        except ValueError:
            continue
    zone = ZoneInfo(timezone_name())
    today = now.astimezone(zone).date()
    offsets = {'今天': 0, '今日': 0, '昨天': 1, '昨日': 1, '前天': 2}
    values.update((today - timedelta(days=offsets[value])).isoformat() for value in RELATIVE.findall(text))
    value = next(iter(values)) if len(values) == 1 else ''
    return value if interval(value) else ''


def sqlite_utc_bound(value):
    # Suffix-free UTC seconds apply equally to stored UTC offsets and Z values.
    return value.astimezone(UTC).replace(tzinfo=None).isoformat(timespec='seconds')


def excerpt_time(source):
    """Date by which this version of the cited text was publicly available."""
    published = source.get('published_at', '')
    if 'modified_at' not in source:
        return published
    revision = source['modified_at']
    first, latest = interval(published), interval(revision)
    if not first or not latest or latest[1] < first[0]:
        return ''
    return revision if latest[1] >= first[1] else published


def eligible(candidate, anchor, as_of):
    latest = min(anchor[1], as_of) if anchor else as_of
    earliest = min(anchor[0], latest) if anchor else latest
    occurrence = interval(candidate.get('occurred_at', ''))
    if occurrence and occurrence[0] >= latest:
        return None

    early_sources, uncertain_sources = [], []
    for source in candidate.get('sources', []):
        stamp = interval(excerpt_time(source))
        if stamp is None:
            uncertain_sources.append(source)
        elif stamp[1] <= earliest and stamp[0] < earliest:
            early_sources.append((stamp[0], source))
        elif stamp[0] < latest:
            uncertain_sources.append(source)

    if not early_sources and not uncertain_sources:
        return None
    early_sources.sort(key=lambda item: item[0])
    timely = anchor is not None and (
        early_sources and (occurrence is None or (occurrence[0] < earliest and occurrence[1] <= earliest))
    )
    sources = [source for _, source in early_sources] + uncertain_sources
    return {**candidate, 'time_status': 'earlier' if timely else 'unknown',
            'published_at': next((stamp for source in sources if (stamp := excerpt_time(source))), ''),
            'sources': sources[:4]}


def bind_citation(candidate, decision, anchor, as_of):
    """Recheck chronology using only the source whose wording supports this edge."""
    source_id = decision.get('evidence_source')
    quote = decision.get('evidence_quote')
    sources = candidate['sources']
    for number, source in enumerate(sources, start=1):
        if source_id == f'S{number}' and quote and quote in source['quote']:
            cited = {**source, 'cited_quote': quote}
            dated = eligible({**candidate, 'sources': [cited]}, anchor, as_of)
            earlier = bool(dated and dated['time_status'] == 'earlier' and cited.get('kind') != 'local_headline')
            break
    else:
        raise ValueError('归因决定与原文来源不一致。')
    relation = decision.get('relation', 'context')
    return {**candidate, 'reason': decision['reason'], 'score': float(decision['score']),
            'relation': 'context' if relation == 'possible_cause' and not earlier else relation,
            'published_at': excerpt_time(cited), 'time_status': 'earlier' if earlier else 'unknown',
            'sources': [cited, *(other for other in sources if other is not source)]}

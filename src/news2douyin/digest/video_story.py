"""Ground cross-day video-card follow-ups in the saved companion editions."""

from collections import defaultdict
from datetime import date, datetime, timezone
from itertools import islice
import re
import unicodedata
from urllib.parse import urlencode, urlsplit

from ..dedup.normalize import canonicalize_url
from ..selection.source_content import source_url, usable_title


HISTORY_DAYS = 14
HISTORY_LIMIT = 3
EDITION_CARD_LIMIT = 100

_LOCAL = re.compile(r'local:[A-Za-z0-9_-]{1,120}\Z')
_GDELT = re.compile(r'gdelt:[0-9]{1,24}\Z')
# A full court reference can connect developments under differing headlines
# (filing, hearing, judgment). Preserve the year and court identifier. A case
# number lacking the court code could be shared by unrelated jurisdictions.
_COURT_CASE = re.compile(
    r'\((?:19|20)[0-9]{2}\)[\u3400-\u9fff][0-9]{2,6}'
    r'(?:民初|民终|刑初|刑终|行初|行终|执异|执复)[0-9]{2,8}号'
)
_SECTION_SLUGS = {'news', 'index', 'home', 'world', 'latest', 'live', 'update', 'updates', 'breaking'}
_MATCH_REASON = {
    'local': '同一已整理事件',
    'gdelt': '同一 GDELT 编码事件',
    'document': '同一来源网址的较早标题',
    'case': '报道引用了相同完整案号',
}


def _day(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return None
        if parsed.isoformat() == value:
            return parsed
    return None


def _instant(value):
    if isinstance(value, str) and value:
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if isinstance(value, datetime) and value.utcoffset() is not None:
        return value.astimezone(timezone.utc)
    return None


def _headline(card):
    value = card.get('title')
    if not isinstance(value, str):
        return ''
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split()).rstrip('。.!！')


def _report_url(card):
    value = card.get('source_url')
    return source_url(value) if isinstance(value, str) and '\\' not in value else ''


def _anchors(card, url, headline):
    key = card.get('source_key')
    kind = card.get('source_kind')
    if isinstance(key, str):
        if kind == 'local' and _LOCAL.fullmatch(key):
            yield 'local', key
        elif kind == 'gdelt' and _GDELT.fullmatch(key):
            yield 'gdelt', key
    path = urlsplit(url).path.strip('/')
    if path and not set(path.casefold().split('/')).issubset(_SECTION_SLUGS):
        yield 'document', canonicalize_url(url)
    if not card.get('reconstruction'):
        cases = set(_COURT_CASE.findall(headline))
        if len(cases) == 1:
            yield 'case', next(iter(cases))


def _history_index(previous_editions, day, ceiling):
    index = defaultdict(list)
    for edition in previous_editions:
        date_value = _day(edition.get('day'))
        if not date_value or not 1 <= (day - date_value).days <= HISTORY_DAYS:
            continue
        old_cutoff = _instant(edition.get('cutoff_at'))
        if ceiling and old_cutoff and old_cutoff > ceiling:
            continue
        for card in islice(edition.get('items') or (), EDITION_CARD_LIMIT):
            title, key = card.get('title'), card.get('key')
            url = _report_url(card)
            seen = _instant(card.get('source_observed_at') or card.get('observed_at'))
            if not key or not url or not isinstance(title, str) or not usable_title(title):
                continue
            if ceiling and (not seen or seen >= ceiling):
                continue
            headline = _headline(card)
            entry = dict(day=date_value.isoformat(), key=key, title=title, url=url)
            for anchor in _anchors(card, url, headline):
                index[anchor].append((entry, headline, seen))
    return index


def attach_history(items, previous_editions, *, day, cutoff=None):
    """Add up to three first-hand links from earlier saved video editions.

    The entries are evidence for revisiting the story and have no ranking or
    event-adoption effect. Report or case identity does not assert causality.
    """
    current_day = _day(day)
    if current_day is None:
        raise ValueError('视频候选日期无效')
    ceiling = _instant(cutoff)
    if cutoff is not None and ceiling is None:
        raise ValueError('视频候选的截止时间须包含时区')
    index = _history_index(previous_editions, current_day, ceiling)
    for item in items:
        item.pop('story_history', None)
        url, headline = _report_url(item), _headline(item)
        if not url or not headline:
            continue
        seen = _instant(item.get('source_observed_at') or item.get('observed_at'))
        if ceiling and (not seen or seen >= ceiling):
            continue
        choices = {}
        for priority, anchor in enumerate(_anchors(item, url, headline)):
            for entry, old_title, old_seen in index.get(anchor, ()):
                if old_title == headline or entry['key'] == item.get('key'):
                    continue
                if seen and (not old_seen or old_seen >= seen):
                    continue
                identity = (entry['day'], entry['key'])
                choices.setdefault(identity, (entry, _MATCH_REASON[anchor[0]], old_title, priority))
        dates = set()
        titles = set()
        history = []
        for entry, reason, old_title, _ in sorted(choices.values(), key=lambda found: (
            found[0]['day'], -found[3], found[0]['key'], found[0]['url'],
        ), reverse=True):
            if entry['day'] in dates or old_title in titles:
                continue
            dates.add(entry['day'])
            titles.add(old_title)
            history.append(dict(entry, reason=reason))
            if len(history) == HISTORY_LIMIT:
                break
        if history:
            item['story_history'] = history
    return items


def focus_url(card, *, day):
    """Open a new, unsaved Focus draft from this dated publication card."""
    selected = _day(day)
    key = card.get('key')
    if not selected or not isinstance(key, str) or not 1 <= len(key) <= 160:
        return ''
    return '/focus?' + urlencode({'new': 1, 'video_day': selected.isoformat(), 'video_key': key})

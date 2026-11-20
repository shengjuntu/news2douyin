"""A bounded, read-only board for recently observed news topics."""
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from urllib.parse import quote, urlencode, urlsplit

from fastapi import HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from ..desk.focus_lifecycle import visible_watch
from ..desk.models import Watch
from ..hotspots.detect import detect_hotspots
from ..hotspots.sources import read_window
from ..selection.source_content import source_url
from .daily_radar import SOURCE_LABELS


WINDOW_HOURS = 36
CACHE_SECONDS = 60
TOPICS_PER_CATEGORY = 8
LABELS = {'conflict': '冲突', 'diplomacy': '重要会议', 'disaster': '自然灾害', 'disruption': '广泛影响'}
CANDIDATE_KINDS = {'gkg', 'gdelt', 'local', 'newsnow', 'report'}
SOURCE_NAMES = {**SOURCE_LABELS, 'daily': '每日精选', 'official': 'GDELT Events + Mentions',
                'working': '已整理事件', 'attention': '中文平台关注', 'report': '已收录报道', 'reports': '已收录报道'}
PROVIDER_NAMES = {'worldnewsapi': 'WorldNewsAPI', 'newsapi': 'NewsAPI'}


class HotspotBoard:
    def __init__(self):
        self._lock = Lock()
        self._snapshot = None
        self._expires = 0.0

    def read(self, session):
        with self._lock:
            if monotonic() < self._expires:
                return self._snapshot
            now = datetime.now(timezone.utc)
            data = read_window(session, as_of=now, recent_hours=WINDOW_HOURS, previous_hours=WINDOW_HOURS)
            groups = detect_hotspots(data['items'], as_of=now, limit=TOPICS_PER_CATEGORY * len(LABELS),
                                     per_category=TOPICS_PER_CATEGORY,
                                     recent_hours=WINDOW_HOURS, previous_hours=WINDOW_HOURS)
            self._snapshot = dict(topics=groups, as_of=now.isoformat(), sources=data['sources'])
            self._expires = monotonic() + CACHE_SECONDS
            return self._snapshot

    def get(self, session, key):
        return next((topic for topic in self.read(session)['topics'] if topic['id'] == key), None)


def _report_context(row):
    address = source_url(row.get('url') or row.get('source_url') or '')
    if not address:
        return None
    key = row.get('source_key') or row.get('key') or ''
    kind = row.get('source_kind') or ''
    scheme, _, ident = key.partition(':')
    task_url = ''
    if scheme in CANDIDATE_KINDS and ident and len(key) <= 240:
        task_url = '/workbench/new?' + urlencode({'candidate': key})
    elif event := row.get('local_event_key'):
        task_url = '/workbench/new?' + urlencode({'event': event})
    if row.get('official_only'):
        label = 'GDELT 事件编码线索'
    elif row.get('attention_only'):
        label = '中文平台观察'
    else:
        provider = row.get('provider') or ''
        label = PROVIDER_NAMES.get(provider) or SOURCE_NAMES.get(kind) or provider
    return dict(title=row.get('title'), url=address, domain=(urlsplit(address).hostname or '').removeprefix('www.'),
                label=label, observed_at=row.get('observed_at'), excerpt=row.get('excerpt'), task_url=task_url)


def _cards(session, snapshot, category):
    groups = [topic for topic in snapshot['topics'] if not category or category == topic['category']]
    names = {topic['title'][:80] for topic in groups}
    stored = dict(session.exec(select(Watch.name, Watch.key).where(Watch.name.in_(names), visible_watch())).all()) if names else {}
    cards = []
    for topic in groups:
        key = stored.get(topic['title'][:80])
        url = '/focus?' + urlencode({'new': 1, 'hotspot': topic['id']})
        reports = [_report_context(source) for source in topic['sources']]
        cards.append(dict(topic, reports=[row for row in reports if row],
                          focus_url='/focus/' + quote(key, safe='') if key else url,
                          existing_focus=bool(key), category_name=LABELS[topic['category']]))
    return cards


def register(app, engine, render):
    app.state.hotspot_board = HotspotBoard()

    @app.get('/radar/hotspots', response_class=HTMLResponse)
    def hotspots(category: str = Query('', max_length=12)):
        if category and category not in LABELS:
            raise HTTPException(422, '热点分类无效')
        with Session(engine) as session:
            snapshot = app.state.hotspot_board.read(session)
            cards = _cards(session, snapshot, category)
        return render('hotspots.html', active='radar', category=category, categories=LABELS,
                      cards=cards, as_of=snapshot['as_of'], hours=WINDOW_HOURS,
                      sampled_sources=[(SOURCE_NAMES[kind], info['count'], info.get('limited'))
                                       for kind, info in snapshot['sources'].items() if kind in SOURCE_NAMES])

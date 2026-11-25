"""World radar: bounded local event readings, regional signals and market leads."""
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import HTTPException, Query
from fastapi.responses import HTMLResponse

from ..desk.places import countries
from ..digest.calendar import BEIJING
from ..georisk import config as risk_config
from ..georisk.service import dashboard
from ..storage.db import session_scope
from ..world_radar.events import CATEGORIES, developments
from ..world_radar.market import leads
from .georisk import analysis_status

PATH = '/radar/world'
DEFAULT_DAYS = 3
EVENTS_LIMIT = 60
MARKET_LIMIT = 20
SIGNAL_DAYS = 14
Layer = Literal['events', 'signals', 'market']


def _filters(now, day, days, category, country, signal, layer):
    today = now.astimezone(BEIJING).date()
    try:
        selected = date.fromisoformat(day) if day else today
        if (day and day != selected.isoformat()) or not date(1979, 1, 1) <= selected <= today:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(422, '日期须在 1979-01-01 至今天之间。') from exc
    if days not in (1, 3, 7):
        raise HTTPException(422, '请选择 1、3 或 7 天。')
    if category and category not in CATEGORIES:
        raise HTTPException(422, '未知事件类别。')
    country = country.lower()
    if country and country not in countries():
        raise HTTPException(422, '未知国家或地区。')
    return dict(day=selected.isoformat(), days=days, country=country,
                category=category, signal=signal, layer=layer)


def _window(filters, now):
    selected = date.fromisoformat(filters['day'])
    first = selected - timedelta(days=filters['days'] - 1)
    start = datetime.combine(first, time.min, BEIJING).astimezone(timezone.utc)
    end = datetime.combine(selected + timedelta(days=1), time.min, BEIJING).astimezone(timezone.utc)
    return first, start, min(end, now)


def _signals(session, worker, filters, now):
    try:
        cfg = risk_config.settings()
    except ValueError:
        return dict(enabled=False, state='config_error', category='', category_options={}, items=[])
    options = {key: risk_config.CATEGORY_LABELS.get(key, key) for key in cfg.categories}
    category = filters['signal'] or ('conflict' if 'conflict' in options else next(iter(options)))
    if category not in options:
        raise HTTPException(422, '未知报道升温主题。')
    items = []
    for scope, codes in cfg.scopes.items():
        if filters['country'] and codes and filters['country'] not in codes:
            continue
        data = dashboard(session, scope=scope, category=category, days=SIGNAL_DAYS, end_day=filters['day'], now=now)
        latest = data['items'][-1]
        items.append({**latest, 'scope': scope, 'label': data['scopes'][scope], 'countries': sorted(codes),
                      'analysis_status': analysis_status(worker, data),
                      'path': '/radar/georisk?' + urlencode(dict(
                          scope=scope, category=category, end_day=filters['day'], day=filters['day'], days=SIGNAL_DAYS)),
                      'points': data['items']})
    state = items[0]['analysis_status'] if items else 'unchecked'
    return dict(enabled=risk_config.enabled(), state=state, category=category,
                category_options=options, items=items)


def _hotspots(session, board, filters, now):
    today = now.astimezone(BEIJING).date().isoformat()
    if filters['day'] != today or filters['country'] or filters['category'] or filters['layer'] != 'signals':
        return dict(items=[], window_hours=36, as_of=None)
    snapshot = board.read(session)
    items = []
    for row in snapshot['topics'][:12]:
        fields = ('id', 'title', 'category', 'status_label', 'recent_reports', 'previous_reports', 'location', 'sources')
        items.append({**{key: row[key] for key in fields if key in row},
                      'focus_url': '/focus?' + urlencode({'new': 1, 'hotspot': row['id']})})
    return dict(items=items, window_hours=36, as_of=snapshot['as_of'], sampled_sources=snapshot['sources'])


def _add_links(result, back):
    for row in result['items']:
        if (path := row.get('event_path')) and path.startswith('/events/'):
            row['event_path'] = path + ('&' if '?' in path else '?') + urlencode({'back': back})
        if event := row.get('event_key'):
            params = {'event_key': event}
            if moment := row.get('moment_key'):
                params['moment_key'] = moment
        elif candidate := row.get('candidate_key'):
            params = {'candidate_key': candidate}
        else:
            continue
        row['research_path'] = '/research/market-impact?' + urlencode(params)


def register(app, engine, render):
    Day = Annotated[str, Query(max_length=10)]
    Kind = Annotated[str, Query(max_length=32)]
    Country = Annotated[str, Query(max_length=2)]

    @app.get(PATH, response_class=HTMLResponse)
    def world_page(day: Day = '', days: int = DEFAULT_DAYS, country: Country = '',
                   category: Kind = '', signal: Kind = '', layer: Layer = 'events'):
        now = datetime.now(timezone.utc)
        filters = _filters(now, day, days, category, country, signal, layer)
        response = render('world_radar.html', active='radar', world_day=now.astimezone(BEIJING).date().isoformat(),
                          world_filters=filters, world_categories=CATEGORIES, world_countries=countries())
        response.set_cookie('n2d_desk', 'radar', max_age=31536000, httponly=True, samesite='lax')
        return response

    @app.get('/api/radar/world')
    def world_data(day: Day = '', days: int = DEFAULT_DAYS, country: Country = '',
                   category: Kind = '', signal: Kind = '', layer: Layer = 'events'):
        now = datetime.now(timezone.utc)
        filters = _filters(now, day, days, category, country, signal, layer)
        first, start, end = _window(filters, now)
        area = {key: filters[key] for key in ('country', 'category')}
        with session_scope(engine) as session:
            events = developments(session, start=start, end=end, limit=EVENTS_LIMIT, **area)
            regional = _signals(session, app.state.georisk, filters, now)
            market_inputs = events
            if filters['category'] and layer == 'market':
                market_inputs = developments(session, start=start, end=end, limit=EVENTS_LIMIT, country=filters['country'])
            market = leads(session, market_inputs['items'], start=start, end=end, limit=MARKET_LIMIT, **area)
            market['truncated'] = market['truncated'] or market_inputs['truncated']
            hotspots = _hotspots(session, app.state.hotspot_board, filters, now)
        back = PATH + '?' + urlencode({key: value for key, value in filters.items() if value})
        for result in (events, market):
            _add_links(result, back)
        return {**filters, 'start_day': first.isoformat(), 'window_end': end.isoformat(), 'timezone': str(BEIJING),
                'time_basis': 'current_records',
                'countries': countries(), 'categories': CATEGORIES,
                'events': events, 'signals': regional, 'market': market, 'hotspots': hotspots}

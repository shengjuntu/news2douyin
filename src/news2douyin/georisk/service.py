"""Read summaries and compare each day only to earlier covered days."""
from datetime import date, datetime, timedelta, timezone
from math import log1p
from statistics import fmean, stdev

from sqlmodel import select

from ..digest.calendar import BEIJING, day_bounds_utc, days_before
from ..storage.utils import loads
from .config import CATEGORY_LABELS, SCOPE_LABELS, enabled, settings
from .models import GeoriskCoverage, GeoriskDay

BASELINE_WINDOW = 30
MIN_BASELINE_DAYS = 14
MIN_COVERAGE = 0.95
SPIKE_Z = 3.0
MAX_QUERY_DAYS = 120
MAX_EVIDENCE = 6


def stamp(moment):
    return moment.strftime('%Y%m%d%H%M%S')


def expected_through(now):
    """Exclusive due-slot boundary, including the current upstream quarter-hour."""
    instant = now.astimezone(timezone.utc)
    return instant.replace(minute=instant.minute // 15 * 15, second=0, microsecond=0) + timedelta(minutes=15)


def _expected(day, now, feed_count):
    first, end = day_bounds_utc(day)
    slots = max(0, int((min(end, expected_through(now)) - first).total_seconds()) // 900)
    return slots * feed_count


def _scope(cfg, scope, category):
    if scope not in cfg.scopes or category not in cfg.categories:
        raise ValueError('地缘风险范围或分类无效')


def _date(value, today):
    if not value:
        return today
    try:
        day = date.fromisoformat(value)
        if day.isoformat() == value and day <= today:
            return day
    except (ValueError, TypeError):
        pass
    raise ValueError('请选择今天或更早的日期（YYYY-MM-DD）')


def _group(row, scope, category):
    return loads(row.groups_json, {}).get(scope, {}).get(category, {}) if row else {}


def _point(row, archived_count, day, now, feeds, scope, category):
    group = _group(row, scope, category)
    expected = _expected(day, now, len(feeds))
    available = row.available_batches if row else 0
    archived = max(archived_count, row.archived_batches if row else 0)
    coverage = min(1., available / expected) if expected else None
    docs, negative = group.get('doc_count', 0), group.get('negative_tone')
    score = negative * log1p(docs) if negative is not None else None
    if available and not docs:
        score = 0.
    if not available:
        state = 'pending' if archived else 'missing'
    elif day == now.astimezone(BEIJING).date():
        state = 'live'
    elif available >= expected:
        state = 'complete'
    else:
        state = 'partial'
    return dict(day=day.isoformat(), doc_count=docs, source_count=group.get('source_count', 0),
                tone_doc_count=group.get('tone_doc_count', 0), negative_tone=negative, score=score,
                expected_batches=expected, archived_batches=archived, available_batches=available,
                missing_batches=max(0, expected - archived), pending_batches=max(0, archived - available),
                rejected_rows=row.rejected_rows if row else 0, coverage=coverage, state=state)


def dashboard(session, *, scope='global', category='conflict', days=30, end_day='', now=None):
    cfg = settings()
    _scope(cfg, scope, category)
    if not isinstance(days, int) or not 1 <= days <= MAX_QUERY_DAYS:
        raise ValueError(f'可查看 1–{MAX_QUERY_DAYS} 天')
    now = now or datetime.now(timezone.utc)
    end = _date(end_day, now.astimezone(BEIJING).date())
    first = days_before(end, days - 1)
    history_start = days_before(first, BASELINE_WINDOW)
    rows = {row.day: row for row in session.exec(
        select(GeoriskDay.day, GeoriskDay.available_batches, GeoriskDay.archived_batches,
               GeoriskDay.rejected_rows, GeoriskDay.groups_json)
        .where(GeoriskDay.config_key == cfg.key,
               GeoriskDay.day.between(history_start.isoformat(), end.isoformat()))
        .execution_options(n2d_query_name='georisk.dashboard')
    )}
    snapshot = session.get(GeoriskCoverage, cfg.key)
    archived = loads(snapshot.archived_by_day_json, {}) if snapshot else {}
    accepted = []
    items = []
    for index in range((end - history_start).days + 1):
        day = history_start + timedelta(days=index)
        key = day.isoformat()
        point = _point(rows.get(key), archived.get(key, 0), day, now, cfg.feeds, scope, category)
        earlier = [score for past, score in accepted if (day - past).days <= BASELINE_WINDOW]
        eligible = (point['state'] in ('partial', 'complete') and point['coverage'] is not None
                    and point['coverage'] >= MIN_COVERAGE and point['score'] is not None)
        mean = std = z = None
        if len(earlier) >= MIN_BASELINE_DAYS:
            mean, std = fmean(earlier), stdev(earlier)
            if eligible and std > 1e-12:
                z = (point['score'] - mean) / std
        if day >= first:
            items.append(dict(point, baseline_mean=mean, baseline_std=std, baseline_days=len(earlier),
                              zscore=z, spike=z is not None and z >= SPIKE_Z))
        if eligible:
            accepted.append((day, point['score']))
    return dict(scope=scope, category=category, start=first.isoformat(), end=end.isoformat(),
                timezone=str(BEIJING), enabled=enabled(), feeds=list(cfg.feeds),
                scopes={key: SCOPE_LABELS.get(key, key) for key in cfg.scopes},
                categories={key: CATEGORY_LABELS.get(key, key) for key in cfg.categories},
                baseline_window=BASELINE_WINDOW, min_baseline_days=MIN_BASELINE_DAYS,
                min_coverage=MIN_COVERAGE, spike_z=SPIKE_Z, items=items)


def evidence_for_day(session, scope, category, day):
    cfg = settings()
    _scope(cfg, scope, category)
    if not day:
        raise ValueError('请选择日期（YYYY-MM-DD）')
    day = _date(day, datetime.now(BEIJING).date()).isoformat()
    evidence = session.exec(select(GeoriskDay.evidence_json).where(
        GeoriskDay.config_key == cfg.key, GeoriskDay.day == day)
        .execution_options(n2d_query_name='georisk.evidence')).first()
    return loads(evidence, {}).get(scope, {}).get(category, [])[:MAX_EVIDENCE]

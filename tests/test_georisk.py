from datetime import date, datetime, timedelta, timezone
from math import log1p
from zipfile import ZipFile

import pytest
from sqlmodel import Session

from news2douyin.georisk.config import settings
from news2douyin.georisk.models import GeoriskDay
from news2douyin.georisk.runtime import GeoriskWorker, MAX_BATCHES_PER_TICK
from news2douyin.georisk.service import dashboard, evidence_for_day
from news2douyin.intake.feeds import FEEDS
from news2douyin.intake.lake import Lake
from news2douyin.storage.utils import dumps


NOW = datetime(2026, 11, 20, 2, 0, tzinfo=timezone.utc)
SIGNAL_DAY = '2026-11-19'


def archive(root, feed, stamp, *observations):
    data = []
    for index, (url, title, theme, country, tone) in enumerate(observations, 1):
        fields = [''] * 27
        fields[0] = f'{stamp}-{index}'
        fields[1] = stamp
        fields[4] = url
        fields[7] = theme
        fields[9] = f'1#place#{country}#0#0#0#'
        fields[15] = '1,2,' + str(tone) + ',3,4,5,100'
        fields[26] = f'<PAGE_TITLE>{title}</PAGE_TITLE>' if title else ''
        data.append('\t'.join(fields))
    suffix = FEEDS[feed]['suffix']
    target = root / (stamp + suffix)
    with ZipFile(target, 'w') as package:
        package.writestr(stamp + suffix[:-4], '\n'.join(data) + '\n' if data else '')
    return Lake(root).ingest_archive(feed, stamp, target)


def test_lake_projection_replays_late_batches_and_sql_failure(engine, tmp_path, monkeypatch):
    monkeypatch.delenv('NEWS2DOUYIN_GEORISK_CONFIG', raising=False)
    monkeypatch.setenv('NEWS2DOUYIN_GEORISK_REPLAY_DAYS', '2')
    archive(tmp_path, 'gkg_english', '20261119010000',
            ('https://news.example/conflict?utm_source=a', 'Officials reported armed clashes', 'ARMEDCONFLICT', 'UP', 5),
            ('https://unsupported.example/unreadable', '', 'ARMEDCONFLICT', 'UP', 3),
            ('https://news.example/sanctions', 'New export sanctions reported', 'ECON_TRADE_DISPUTE', 'IR', 4),
            ('https://news.example/other', 'Airport passenger totals increased', 'TRANSPORT', 'IR', 9),
            ('https://news.example/trade', 'Trade delegations opened talks', 'ECON_TRADE_DISPUTE', 'IR', 9))
    archive(tmp_path, 'gkg_translation', '20261119010000',
            ('https://news.example/conflict', 'Translated report of armed clashes', 'ARMEDCONFLICT', 'UP', 7))
    archive(tmp_path, 'gkg_english', '20261117150000',
            ('https://olds.example/old', 'Old clash report', 'ARMEDCONFLICT', 'UP', 50))
    worker = GeoriskWorker(engine, tmp_path)
    assert worker.step(NOW)
    with Session(engine) as session:
        result = dashboard(session, days=1, end_day=SIGNAL_DAY, now=NOW, scope='europe')['items'][0]
        assert result['doc_count'] == result['source_count'] == 2
        assert result['negative_tone'] == 4
        assert result['score'] == pytest.approx(4 * log1p(2))
        assert result['available_batches'] == result['archived_batches'] == 2
        assert result['missing_batches'] == 190 and result['state'] == 'partial'
        assert result['zscore'] is None and not result['spike']
        assert evidence_for_day(session, 'europe', 'conflict', SIGNAL_DAY) == [
            {'title': 'Officials reported armed clashes', 'url': 'https://news.example/conflict?utm_source=a',
             'domain': 'news.example'},
        ]
        sanctions = dashboard(session, days=1, end_day=SIGNAL_DAY, now=NOW,
                              scope='middle-east', category='sanctions')['items'][0]
        assert sanctions['doc_count'] == 1 and sanctions['negative_tone'] == 4

    archive(tmp_path, 'gkg_english', '20261118163000',
            ('https://news.example/conflict', 'Earlier evidence of armed clashes', 'ARMEDCONFLICT', 'UP', 1),
            ('https://new.example/conflict', 'Fresh evidence of armed clashes', 'ARMEDCONFLICT', 'UP', 'NaN'))
    original_publish = worker._publish

    def fail_publication(*_):
        raise RuntimeError('simulated interruption between DuckDB and SQL')

    monkeypatch.setattr(worker, '_publish', fail_publication)
    with pytest.raises(RuntimeError, match='simulated interruption'):
        worker.step(NOW, refresh=True)
    monkeypatch.setattr(worker, '_publish', original_publish)
    retry = GeoriskWorker(engine, tmp_path)
    assert retry.step(NOW)
    assert not retry.step(NOW, refresh=True)
    with Session(engine) as session:
        result = dashboard(session, days=1, end_day=SIGNAL_DAY, now=NOW)['items'][0]
        assert result['doc_count'] == result['source_count'] == 3
        assert result['tone_doc_count'] == 2 and result['negative_tone'] == 2
        assert result['available_batches'] == 3
        titles = [item['title'] for item in evidence_for_day(session, 'global', 'conflict', SIGNAL_DAY)]
        assert titles == ['Earlier evidence of armed clashes', 'Fresh evidence of armed clashes']


def test_spike_uses_only_covered_past_days_and_marks_live_gaps(engine):
    key = settings().key
    signal_day = date.fromisoformat(SIGNAL_DAY)

    def summary(day, negative, available=192):
        group = {'doc_count': 1, 'source_count': 1, 'tone_doc_count': 1, 'negative_tone': negative}
        return GeoriskDay(config_key=key, day=day.isoformat(), available_batches=available,
                          archived_batches=available, groups_json=dumps({'global': {'conflict': group}}))

    with Session(engine) as session:
        session.add_all([summary(signal_day - timedelta(days=i), 1 + (i % 2), 80 if i == 10 else 192)
                         for i in range(1, 16)])
        session.add(summary(signal_day, 40, 184))
        session.add(summary(signal_day + timedelta(days=1), 80, 12))
        session.commit()
        items = dashboard(session, days=2, now=NOW)['items']
        spike = items[0]
        assert spike['day'] == SIGNAL_DAY and spike['state'] == 'partial'
        assert spike['baseline_days'] == 14 and spike['baseline_mean'] == pytest.approx((22 / 14) * log1p(1))
        assert spike['spike'] and spike['zscore'] > 3
        live = items[1]
        assert live['state'] == 'live' and live['zscore'] is None and not live['spike']
        assert live['expected_batches'] == 82 and live['missing_batches'] == 70
        missing = dashboard(session, days=1, end_day='2026-11-01', now=NOW)['items'][0]
        assert missing['state'] == 'missing' and missing['score'] is None


def test_archive_backlog_is_bounded_and_no_theme_hit_is_distinct_from_missing(engine, tmp_path):
    archive_count = MAX_BATCHES_PER_TICK + 1
    first = datetime(2026, 11, 19, 1, 0, tzinfo=timezone.utc)
    for index in range(archive_count):
        batch_stamp = (first + timedelta(minutes=15 * index)).strftime('%Y%m%d%H%M%S')
        archive(tmp_path, 'gkg_english', batch_stamp)
    archive(tmp_path, 'gkg_english', '20261118150000')
    worker = GeoriskWorker(engine, tmp_path)
    assert worker.step(NOW)
    with Session(engine) as session:
        result = dashboard(session, days=1, end_day=SIGNAL_DAY, now=NOW)['items'][0]
        assert result['available_batches'] == MAX_BATCHES_PER_TICK
        assert result['archived_batches'] == archive_count
        assert result['pending_batches'] == 1 and result['score'] == 0
        pending = dashboard(session, days=1, end_day='2026-11-18', now=NOW)['items'][0]
        assert pending['state'] == 'pending' and pending['score'] is None
        assert pending['archived_batches'] == pending['pending_batches'] == 1
    assert worker.step(NOW)
    with Session(engine) as session:
        result = dashboard(session, days=1, end_day=SIGNAL_DAY, now=NOW)['items'][0]
        assert result['available_batches'] == archive_count and result['pending_batches'] == 0

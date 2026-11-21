"""Offline smoke of the installed release, including one GKG-only published item."""
from importlib import metadata
from pathlib import Path
from unittest.mock import patch
import os
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from news2douyin.digest.daily_review import score_daily
from news2douyin.digest.review_cli import main as review
from news2douyin.digest.service import build_issue, last_closed_day
from news2douyin.intake.lake import Lake
from news2douyin.server.app import create_app


def main():
    assert metadata.version('news2douyin') == '0.48.0'
    day = last_closed_day()
    # 09:00 UTC is 17:00 Beijing on the same date.
    observed = day.isoformat() + 'T09:00:00Z'
    feed = 'gkg_english'
    stamp = day.strftime('%Y%m%d') + '090000'
    headline = 'Port authority publishes replacement terminal timetable'
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(db_url='sqlite:///' + str(root / 'work.db'), storage_root=str(root))
        try:
            with Lake(root).connection(create=True) as db:
                db.execute('INSERT INTO intake_archives (feed,stamp,rows,completed_at) VALUES (?,?,1,?)',
                           [feed, stamp, observed])
                db.execute('INSERT INTO intake_records_v2 (feed,stamp,row_index,title,url,valid) VALUES (?,?,1,?,?,true)',
                           [feed, stamp, headline, 'https://news.example/timetable'])
            report = build_issue(app.state.engine, day, storage_root=root)
            assert report['status'] == 'ready' and report['items'][0]['title'] == headline, report
            client = TestClient(app)
            page = client.get('/radar', params={'day': day.isoformat()})
            assert page.status_code == 200 and headline in page.text and '形成选题' in page.text
            data = client.get('/api/radar/daily', params={'day': day.isoformat()}).json()
            assert data['items'][0]['source_kind'] == 'gkg'
            assert data['stats']['sources']['gkg_seen'] == 1
            assert client.get('/research-assets/daily-radar.js').status_code == 200
            packet = root / 'blind-review'
            review(['export-daily', '--start', str(day), '--end', str(day), '--out', str(packet),
                    '--db-url', 'sqlite:///' + str(root / 'work.db'), '--storage-root', str(root),
                    '--outside', '0'])
            assert headline in (packet / 'review.html').read_text(encoding='utf-8')
            rating = score_daily(packet)['days'][day.isoformat()]['selected']
            assert rating['total'] == 1 and rating['unjudged'] == 1
            publisher_diff = root / 'source-counts.html'
            with patch.dict(os.environ, NEWS2DOUYIN_STORAGE_ROOT=str(root)):
                review(['compare-publishers', '--start', str(day), '--end', str(day),
                        '--db-url', 'sqlite:///' + str(root / 'work.db'), '--warmup-days', '0',
                        '--out', str(publisher_diff)])
            assert '两榜共有 1 条' in publisher_diff.read_text(encoding='utf-8')
            client.close()
        finally:
            app.state.engine.dispose()
    print('Installed daily radar, GKG-only edition, JSON, HTML, packaged blind review and scoring passed.')


if __name__ == '__main__':
    main()

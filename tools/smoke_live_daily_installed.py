"""Exercise the installed live daily reader and management routes with a temporary local archive."""
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient

from news2douyin.digest.service import default_day
from news2douyin.intake.lake import Lake
from news2douyin.server.app import create_app
from news2douyin.storage.models import utc_now_iso


def main():
    assert version('news2douyin') == '0.40.0'
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        app = create_app(db_url=f'sqlite:///{root}/db', storage_root=temp)
        client = TestClient(app)
        try:
            day = default_day()
            history = (day - timedelta(days=4)).isoformat()
            daily = '/api/radar/daily'
            for number, title in enumerate(('City council approved the subway expansion schedule',
                                            'Airport terminal opened for traffic under new safety rules'), 1):
                feed = 'gkg_english'
                stamp = day.strftime('%Y%m%d') + ('000000' if number == 1 else '001500')
                with Lake(root).connection(create=True) as db:
                    db.execute('INSERT INTO intake_archives (feed,stamp,rows,completed_at) VALUES (?,?,1,?)',
                               [feed, stamp, utc_now_iso()])
                    db.execute('INSERT INTO intake_records_v2 (feed,stamp,row_index,title,url,valid) '
                               'VALUES (?,?,1,?,?,true)', [feed, stamp, title, f'https://paper.example/{number}'])
                accepted = client.post(daily, params={'day': str(day)})
                assert accepted.status_code == 202, accepted.text
                old = client.get(daily).json()
                assert len(old['items']) == number - 1
                assert app.state.digest_worker.step()
                latest = client.get(daily).json()
                assert latest['day'] == str(day) and latest['revision'] == number
                assert latest['edition_kind'] == 'intraday' and len(latest['items']) == number
                page = client.get('/radar', params={'group_by':'country', 'sort_by':'recent'})
                assert page.status_code == 200 and title in page.text
                assert 'data-reading-ordinal' in page.text and '译成中文' in page.text
            assert '为什么值得跟' in page.text
            shortlist_path = f'/api/radar/daily/{day}/shortlist.md'
            assert f'{shortlist_path}?revision=2' in page.text and 'Markdown（2 条）' in page.text
            shortlist = client.get(shortlist_path, params={'revision': 2})
            assert shortlist.status_code == 200 and '.md' in shortlist.headers['content-disposition']
            assert str(day) in shortlist.text
            assert all(item['title'] in shortlist.text and item['source_url'] in shortlist.text
                       for item in latest['items'])
            assert client.get(shortlist_path, params={'revision': 1}).status_code == 409
            assert client.get(f'/api/radar/daily/{day}/brief.md', params={'revision': 2}).status_code == 200
            from news2douyin.digest.cli import main as export
            md = root / 'daily-shortlist.md'
            assert export(['export','--day',str(day),'--db-url',f'sqlite:///{root}/db','--out',str(md)]) == 0
            assert md.read_bytes() == shortlist.content
            from news2douyin.digest.review_cli import main as review
            import json
            review_dir = root / 'shortlist-review'
            review(['compare-shortlist', '--day', str(day), '--db-url', f'sqlite:///{root}/db',
                    '--storage-root', temp, '--out', str(review_dir)])
            comparison = json.loads((review_dir / 'comparison.json').read_text(encoding='utf-8'))
            assert comparison['revision'] == 2 and set(comparison['arms']) == {'baseline','classification','merge','combined'}
            assert comparison['arms']['combined']['stats']['selected'] == len(latest['items'])
            assert (review_dir / 'review.html').is_file() and (review_dir / 'shortlist.md').is_file()
            assert client.get(shortlist_path, params={'revision': 2}).content == shortlist.content
            summary = client.get(daily, params={'summary':'true'}).json()
            assert summary['revision'] == 2 and 'items' not in summary
            assert client.get('/admin/daily-radar', params={'day':history}).status_code == 200
            result = client.post(f'/api/admin/daily-radar/{history}/backfill')
            assert result.status_code == 202, result.text
            assert result.json()['job']['total'] == 6 * 96
            for asset in ('daily-radar.css', 'daily-radar.js', 'daily-translation.js', 'daily-management.js'):
                assert client.get('/research-assets/' + asset).status_code == 200
        finally:
            client.close()
            app.state.engine.dispose()
    print('Installed live editions 1 and 2, group/sort/reading/translation controls, history repair queue and static files passed.')


if __name__ == '__main__':
    main()

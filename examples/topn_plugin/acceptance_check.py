"""Exercise the installed external strategy against the real daily worker and CLI."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from importlib.metadata import entry_points, version
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from sqlmodel import Session

from news2douyin.digest.models import DailyDigestIssue
from news2douyin.digest.selection import Candidate, select_top_n
from news2douyin.digest.service import default_day
from news2douyin.intake.lake import Lake
from news2douyin.server.app import create_app


TITLE = 'Bayport council approved the expansion of the waterfront transit network'
UPDATED = 'Bayport officials released the final timetable for local transit services'
LATE_URL = 'https://late.example/report/late'
CONFIG = 'NEWS2DOUYIN_SELECTION_CONFIG'


def seed(root, day):
    reports = [(TITLE, f'https://paper-{i % 3}.example/report/{i}') for i in range(120)]
    updates = [(UPDATED, reports[0][1]),
               ('Officials released a revised public transport budget', 'https://paper-0.example/report/budget'),
               ('Transit board revealed its new schedule for drivers', 'https://paper-1.example/report/drivers')]
    utc_day = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    batches = [('001500', utc_day + timedelta(minutes=15), reports),
               ('091500', utc_day + timedelta(hours=9, minutes=15), updates),
               ('093000', utc_day + timedelta(hours=16), [('Material completed at the exclusive cutoff', LATE_URL)])]
    with Lake(root).connection(create=True) as db:
        for stamp_suffix, received, rows in batches:
            stamp = day.strftime('%Y%m%d') + stamp_suffix
            db.execute('INSERT INTO intake_archives (feed,stamp,rows,completed_at) VALUES (?,?,?,?)',
                       ['gkg_english', stamp, len(rows), received.isoformat().replace('+00:00', 'Z')])
            db.executemany('INSERT INTO intake_records_v2 (feed,stamp,row_index,title,url,valid) '
                           'VALUES (?,?,?,?,?,true)',
                           [('gkg_english', stamp, i, title, url) for i, (title, url) in enumerate(rows, 1)])
    return reports[0][1]


def configure(path, limit, cap):
    path.write_text(f'[selection]\nstrategy="latest-diverse"\nlimit={limit}\n'
                    f'[selection.options]\nmax_per_domain={cap}\n', encoding='utf-8')
    os.environ[CONFIG] = str(path)


def run_cli(root, day, action, *extra):
    args = [sys.executable, '-I', '-m', 'news2douyin.digest.cli', action, '--day', str(day),
            '--db-url', f'sqlite:///{root}/db', '--storage-root', str(root), *extra]
    result = subprocess.run(args, cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'{action} failed: {result.stderr[-2000:]} {result.stdout[-2000:]}')
    return result.stdout


def check_edition(client, day, issue, size):
    assert issue['status'] == 'ready', issue.get('error')
    items = issue['items']
    assert len(items) == size, (len(items), size)
    assert len(set(i['key'] for i in items)) == size
    assert all(i.get('source_url') != LATE_URL for i in items)
    web = client.get('/radar', params={'day': str(day)})
    assert web.status_code == 200 and f'Markdown（{size} 条）' in web.text
    route = f'/api/radar/daily/{day}/shortlist.md'
    md = client.get(route, params={'revision': issue['revision']})
    assert md.status_code == 200, md.text[:300]
    assert len(re.findall(r'^## \d+\. ', md.text, re.MULTILINE)) == size
    return md.content


def check_sdk_order(day):
    cutoff = datetime(day.year, day.month, day.day, 16, tzinfo=timezone.utc)
    opts = dict(day=day, cutoff=cutoff, n=3, strategy='latest-diverse', options={'max_per_domain': 3})
    inputs = [Candidate(f'candidate-{i}', 'Same reported development', cutoff.replace(hour=9).isoformat(),
                        f'https://example.com/reports/{i}', 'gkg') for i in range(6)]
    winning = select_top_n(iter(inputs), **opts)['items']
    reversed_order = select_top_n(reversed(inputs), **opts)['items']
    assert [i['key'] for i in winning] == [i['key'] for i in reversed_order]
    omit = winning[0]['key']
    skipped = select_top_n(iter(inputs), prior_keys={omit}, **opts)['items']
    assert len(skipped) == 3 and all(i['key'] != omit for i in skipped)


def check_metadata(issue, limit, cap):
    assert issue['stats']['scanned'] == 123
    assert issue['stats']['selection'] == dict(api_version=1, strategy='latest-diverse',
                                              version='1.0.0', limit=limit,
                                              options={'max_per_domain': cap})


def main():
    plugins = entry_points(group='news2douyin.selection', name='latest-diverse')
    (plugin,) = plugins
    assert plugin.dist.name == 'news2douyin-latest-docs-example'
    for key in list(os.environ):
        if key.startswith('NEWS2DOUYIN_SELECTION_'):
            del os.environ[key]
    os.environ['NEWS2DOUYIN_DIGEST_ENABLED'] = '1'
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / 'selection.toml'
        day = default_day() - timedelta(days=4)
        check_sdk_order(day)
        old_day = day - timedelta(days=1)
        updated_url = seed(root, day)
        app = create_app(db_url=f'sqlite:///{root}/db', storage_root=tmp)
        client = TestClient(app)
        try:
            with Session(app.state.engine) as session:
                # Missing provenance and selection metadata are valid historical records.
                session.add(DailyDigestIssue(day=str(old_day), status='ready', items_json=json.dumps([
                    {'key': 'archived-card', 'title': 'Original unlinked headline'}])))
                session.commit()
            published = json.loads(run_cli(root, day, 'build'))
            assert 0 < len(published['items']) < 100
            original_keys = [item['key'] for item in published['items']]
            check_edition(client, day, published, len(original_keys))
            configure(config, 3, 1)
            start = client.post(f'/api/admin/daily-radar/{day}/rebuild')
            assert start.status_code == 202, start.text
            assert app.state.digest_worker.step(), client.get('/api/radar/daily', params={'day': str(day)}).text
            small = client.get('/api/radar/daily', params={'day': str(day)}).json()
            check_edition(client, day, small, 3)
            check_metadata(small, 3, 1)
            assert all(item['key'].startswith('doc_') for item in small['items'])
            hosts = Counter(urlsplit(i['source_url']).hostname for i in small['items'])
            assert len(hosts) == 3 and set(hosts.values()) == {1}
            configure(config, 110, 200)
            large = json.loads(run_cli(root, day, 'rebuild'))
            md = check_edition(client, day, large, 110)
            check_metadata(large, 110, 200)
            ordered_urls = [item['source_url'] for item in large['items']]
            assert len(set(ordered_urls)) == 110
            assert any(i['source_url'] == updated_url and i['title'] == UPDATED for i in large['items'])
            export_path = root / 'published.md'
            run_cli(root, day, 'export', '--out', str(export_path))
            assert export_path.read_bytes() == md
            configure(config, 200, 200)
            partial = json.loads(run_cli(root, day, 'rebuild'))
            check_edition(client, day, partial, 122)
            check_metadata(partial, 200, 200)
            assert len({i['source_url'] for i in partial['items']}) == 122
            assert ordered_urls == [i['source_url'] for i in partial['items'][:110]]
            legacy = client.get(f'/api/radar/daily/{old_day}/shortlist.md', params={'revision': 1})
            assert legacy.status_code == 200 and 'Original unlinked headline' in legacy.text
            old_md = root / 'legacy.md'
            run_cli(root, old_day, 'export', '--out', str(old_md))
            assert old_md.read_bytes() == legacy.content
            del os.environ[CONFIG]
            restored = json.loads(run_cli(root, day, 'rebuild'))
            assert [i['key'] for i in restored['items']] == original_keys
            print(json.dumps(dict(core_version=version('news2douyin'), plugin_distribution=plugin.dist.name,
                                  plugin_version=plugin.dist.version, eligible_observations=small['stats']['scanned'],
                                  saved_selection=large['stats']['selection'],
                                  default_items=len(original_keys), web_worker_items=len(small['items']),
                                  web_worker_hosts=len(hosts), cli_items=len(large['items']),
                                  partial_items=len(partial['items']), partial_limit=200,
                                  updated_duplicate_selected=True, excluded_at_cutoff=True,
                                  tie_order_independent=True, prior_pick_filter_verified=True,
                                  web_cli_markdown_identical=True, legacy_unlinked_export=True,
                                  default_rebuild_identical=True), indent=2))
        finally:
            client.close()
            app.state.engine.dispose()


if __name__ == '__main__':
    main()

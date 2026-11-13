"""Smoke-test benchmark isolation and workload shape without large CI data."""
import json
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys


SOURCE = Path(__file__).resolve().parents[1]
TOOL = SOURCE / 'tools/benchmark_read_queries.py'


def call(*arguments):
    return subprocess.run([sys.executable, str(TOOL), *map(str, arguments)],
        cwd=SOURCE, capture_output=True, text=True, timeout=90)


def test_benchmark_fixture_preserves_exact_versions_and_uncompressed_payloads(tmp_path):
    db = tmp_path / 'fixture.db'
    generated = call('generate', '--db', db, '--rows', 17, '--versions-per-article', 3,
        '--payload-bytes', 8192, '--article-body-bytes', 2048)
    assert generated.returncode == 0, generated.stderr
    metadata = json.loads(Path(str(db) + '.fixture.json').read_text())
    assert metadata['counts']['articleversion'] == 17
    assert metadata['counts']['article'] == metadata['counts']['event'] == 6
    assert metadata['counts']['textmention'] == 2
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT min(length(payload_json)) FROM articleversion').fetchone()[0] >= 8192
        assert connection.execute('SELECT min(length(content)) FROM article').fetchone()[0] == 2048
        assert json.loads(connection.execute('SELECT payload_json FROM articleversion LIMIT 1').fetchone()[0])['synthetic_revision'] == 1
        assert connection.execute('SELECT max(revision) FROM articleversion WHERE article_key=?',
            ('bench_a_000000005',)).fetchone()[0] == 2
        assert {row[0] for row in connection.execute("SELECT json_extract(data,'$.kind') FROM textmention")} == {'attack', 'meeting'}
    before = db.stat().st_mtime_ns, db.stat().st_size
    refused = call('generate', '--db', db, '--rows', 1)
    assert refused.returncode != 0 and 'refusing to replace' in refused.stderr
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before


def test_benchmark_compares_fresh_processes_without_changing_fixture(tmp_path):
    db = tmp_path / 'fixture.db'
    generated = call('generate', '--db', db, '--rows', 12)
    assert generated.returncode == 0, generated.stderr
    before = db.read_bytes()
    output = tmp_path / 'comparison.json'
    result = call('compare', '--db', db, '--source', SOURCE, '--baseline-source', SOURCE,
        '--paths', '/api/system/status', '--repeats', 1, '--output', output)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report['fixture_unchanged'] and db.read_bytes() == before
    assert [row['label'] for row in report['results']] == ['baseline', 'current']
    assert len({row['worker_pid'] for row in report['results']}) == 2
    for row in report['results']:
        assert not row.get('error'), row
        assert row['median']['sql_count'] > 0
        assert row['runs'][0]['status_code'] == 200
        assert row['runs'][0]['wall_ms'] >= row['runs'][0]['sql_execute_ms']
        assert row['runs'][0]['response_json']['events'] == 12
        assert row['explain'] and all('plan' in plan for plan in row['explain'])
    assert list(tmp_path.glob('n2d-read-bench-*')) == []


def test_version_scan_detection_does_not_mislabel_event_facet_grouping():
    spec = importlib.util.spec_from_file_location('benchmark_read_queries', TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.sql_flags('SELECT max(articleversion.id) FROM articleversion GROUP BY articleversion.article_key')['version_group_by']
    assert not module.sql_flags('SELECT event_key, (SELECT id FROM articleversion WHERE article_key=a.key LIMIT 1) FROM articleeventlink GROUP BY event_key')['version_group_by']

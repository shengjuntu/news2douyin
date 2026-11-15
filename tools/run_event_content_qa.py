"""Reproduce the source-preview release matrix using disposable databases only.

The PostgreSQL phases start their own loopback clusters. HTTP fixtures are
synthetic and loopback-only, and model calls are deterministic test doubles.
The phases intentionally overlap; their pass totals must not be added together.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


TESTS = [
    'tests/test_source_content_runtime.py',
    'tests/test_source_preview_fetch.py',
    'tests/test_source_fetch_budget.py',
    'tests/test_source_preview_translation.py',
    'tests/test_event_report_cards.py',
    'tests/test_event_report_ui.py',
    'tests/test_event_content_journey.py',
    'tests/test_event_source_query_budget.py',
    'tests/test_selection_backend.py',
    'tests/test_event_metadata_handoff.py',
    'tests/test_reading_translations.py',
    'tests/test_attention_journey.py',
]


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def hashes(root, tests=TESTS):
    paths = sorted((root/'src/news2douyin').rglob('*'))
    paths += [root/p for p in tests]
    paths += [root/'tests/event_content_test_support.py', Path(__file__).resolve()]
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths if p.is_file() and '__pycache__' not in p.parts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--output', type=Path, default=Path('docs/validation/0.30.0/event-content-matrix.json'))
    parser.add_argument('--modes', nargs='+', choices=['sqlite', 'postgres-c', 'postgres-en-us'],
                        default=['sqlite', 'postgres-c', 'postgres-en-us'])
    parser.add_argument('--tests', nargs='+', default=TESTS)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output if args.output.is_absolute() else root/args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    report = dict(schema=1, release='0.30.0', started_at=utcnow(), synthetic_fixtures=True,
        production_access=False, external_news_access=False, real_llm_calls=False,
        scope='Independent focused source-content matrix; not the whole repository suite.',
        count_note='Each phase is standalone and overlaps other phases. Do not sum the phase totals.',
        transport='Controlled loopback HTTP with synthetic public-DNS/IP pin mapping for source-preview journeys.',
        tests=args.tests, phases=[], source_sha256_before=hashes(root, args.tests))
    env = dict(os.environ)
    env.pop('NEWS2DOUYIN_TEST_POSTGRES_URL', None)
    env.pop('NEWS2DOUYIN_TEST_POSTGRES_LOCALE', None)
    env['PYTHONUNBUFFERED'] = '1'
    for mode in args.modes:
        command = [args.python, '-m', 'pytest', '-q', '--disable-warnings', *args.tests]
        if mode != 'sqlite':
            locale = 'C' if mode == 'postgres-c' else 'en_US.utf8'
            command = [args.python, 'tools/run_postgres_test_server.py', '--locale', locale, '--', *command]
        start = time.monotonic()
        print('Running ' + mode, flush=True)
        result = subprocess.run(command, cwd=root, env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=900)
        log = output.parent/(output.stem+'-'+mode+'.log')
        log.write_text(result.stdout)
        found = re.findall(r'(\d+) (passed|failed|skipped|deselected|error|errors)\b', result.stdout)
        phase = dict(mode=mode, command=command, returncode=result.returncode,
            duration_seconds=round(time.monotonic()-start, 3),
            counts={name: int(number) for number, name in found},
            log=str(log.relative_to(root)), finished_at=utcnow())
        if mode != 'sqlite':
            try:
                pg = next(json.loads(line) for line in result.stdout.splitlines()
                          if line.startswith('{') and '"qa_only"' in line)
                phase['postgres'] = {key: pg[key] for key in ('version', 'locale', 'collation', 'host', 'qa_only')}
            except (ValueError, KeyError, StopIteration):
                phase['postgres_metadata_error'] = 'No valid cluster metadata in test launcher output'
        report['phases'].append(phase)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
        print(json.dumps(phase, ensure_ascii=False), flush=True)
    report['finished_at'] = utcnow()
    report['source_sha256_after'] = hashes(root, args.tests)
    report['source_changed_during_run'] = report['source_sha256_before'] != report['source_sha256_after']
    report['passed'] = all(p['returncode'] == 0 for p in report['phases'])
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

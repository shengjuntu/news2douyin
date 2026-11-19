"""Replay the nine public ZIPs listed in public-replay-inputs.json in a new local directory.

Run with the project's research dependencies installed. With --archives the run
is entirely offline. With --download the official HTTPS archives are fetched.
The resulting SQLite working DB, DuckDB archive and editor review CSV stay local.
"""
import argparse
import csv
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from urllib.request import urlopen

from news2douyin.digest.service import build_issue, read_issue
from news2douyin.intake.lake import Lake
from news2douyin.storage.db import init_db, make_engine
from sqlmodel import Session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--archives', type=Path, help='Directory containing the nine ZIP files')
    source.add_argument('--download', action='store_true', help='Fetch official ZIPs')
    parser.add_argument('--work-dir', type=Path, required=True, help='New directory for isolated review')
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    inputs = here.parent / 'docs/validation/0.33.0/public-replay-inputs.json'
    manifest = json.loads(inputs.read_text(encoding='utf-8'))
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=False)
    archives = args.archives or work / 'zips'
    if args.download:
        archives.mkdir()
    lake = Lake(work)
    days = set()
    for entry in manifest['archives']:
        path = archives / entry['name']
        if args.download:
            with urlopen(entry['url'], timeout=90) as response:
                raw = response.read(entry['bytes'] + 1)
            if len(raw) != entry['bytes']:
                raise ValueError(f'Archived byte length mismatch: {path.name}')
            path.write_bytes(raw)
        with path.open('rb') as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != entry['sha256']:
            raise ValueError(f'Archive hash mismatch: {path.name}')
        stamp = entry['name'].split('.')[0]
        start = datetime.strptime(stamp, '%Y%m%d%H%M%S')
        days.add(start.date())
        # Simulated historical local availability, not the machine's real receipt
        # time. Only this temporary archive sees the clock substitution.
        available = (start + timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ')
        with patch('news2douyin.intake.lake.utc_now_iso', return_value=available):
            result = lake.ingest_archive(manifest['feed'], stamp, path, expected_bytes=entry['bytes'])
        if result['rows'] != entry['lines']:
            raise ValueError(f'Row count mismatch: {path.name}')
    db_url = 'sqlite:///' + str(work / 'work.db')
    engine = make_engine(db_url)
    try:
        init_db(engine)
        for day in sorted(days):
            issue = build_issue(engine, day, storage_root=work)
            with Session(engine) as session:
                if issue['status'] != 'ready' or read_issue(session, day) != issue:
                    raise RuntimeError(f'Issue was not published: {day}')
            print(day, json.dumps(issue['stats'], ensure_ascii=False), flush=True)
    finally:
        engine.dispose()
    review = [sys.executable, '-m', 'news2douyin.digest.review_cli']
    subprocess.run([*review, 'export-daily', '--start', str(min(days)), '--end',
                    str(max(days)), '--db-url', db_url, '--storage-root', str(work),
                    '--out', str(work / 'review'), '--outside', '8', '--seed', 'public-replay-033'],
                   check=True)
    saved_labels = inputs.with_name('public-replay-ratings.json')
    labels = json.loads(saved_labels.read_text(encoding='utf-8'))
    ratings = work / 'review/public-sample-ratings.csv'
    with ratings.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=('id', 'day', 'important', 'fact_group', 'note'))
        writer.writeheader()
        writer.writerows(labels)
    subprocess.run([*review, 'score-daily', str(work / 'review'),
                    '--ratings', str(ratings)], check=True)


if __name__ == '__main__':
    main()

"""Reproduce the v0.34 source-breadth study with two installed release environments.

Run this script under each release with the same verified archives and separate
output directories. Compare the results to obtain unlabelled review.csv and
technical counts. Local outputs contain source headlines; share only as needed.
"""
import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
from importlib import metadata
import json
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

STUDY = Path(__file__).resolve().parents[1] / 'docs/validation/0.34.0'
# Key previously published at position 100 in v0.33's March 18 regression case.
CASE_KEY = 'progress_943666fdf6f6267f7d498793bb986272ea07757f'


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def ordered_hash(cards):
    return digest(json.dumps([[c['title'], c['source_url']] for c in cards],
                             ensure_ascii=False, separators=(',', ':')))


def get_zip(entry, archives, download):
    path = archives / entry['name']
    if not path.exists() and download:
        with urlopen(entry['url'], timeout=90) as response:
            data = response.read(entry['bytes'] + 1)
        if len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError(f'Unexpected archive: {path.name}')
        path.write_bytes(data)
    if path.stat().st_size != entry['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
        raise ValueError(f'Archive differs from manifest: {path.name}')
    return path


def run(args):
    from news2douyin.digest.archive import iter_gkg
    from news2douyin.digest.ranking import grouped_candidates
    from news2douyin.digest.service import build_issue, read_issue
    from news2douyin.intake.lake import Lake
    from news2douyin.storage.db import init_db, make_engine
    from sqlmodel import Session

    manifest = load(STUDY / 'public-archives.json')
    protocol = load(STUDY / 'public-protocol.json')
    work = args.out.resolve()
    work.mkdir(parents=True, exist_ok=False)
    archives = args.archives.resolve()
    if args.download:
        archives.mkdir(exist_ok=True, parents=True)
    lake = Lake(work)
    days = set()
    for entry in manifest['archives']:
        path = get_zip(entry, archives, args.download)
        stamp = entry['name'].split('.')[0]
        dt = datetime.strptime(stamp, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        received = (dt + timedelta(minutes=15)).isoformat().replace('+00:00', 'Z')
        days.add(dt.date())
        with patch('news2douyin.intake.lake.utc_now_iso', return_value=received):
            archive = lake.ingest_archive(manifest['feed'], stamp, path, expected_bytes=entry['bytes'])
        if archive['rows'] != entry['lines'] or archive['completed_at'] != received:
            raise RuntimeError(f'Ingest verification failed: {stamp}')
    version = metadata.version('news2douyin')
    result = {'version': version, 'days': {}}
    engine = make_engine('sqlite:///' + str(work / 'work.db'))
    try:
        init_db(engine)
        for day in sorted(days):
            issue = build_issue(engine, day, storage_root=work)
            with Session(engine) as session:
                if issue['status'] != 'ready' or issue != read_issue(session, day):
                    raise RuntimeError(f'Could not freeze edition: {day}')
            result['days'][day.isoformat()] = {'cards': issue['items'], 'stats': issue['stats']}
            if day.isoformat() == protocol['known_regression_date']:
                with grouped_candidates(iter_gkg(work, day), keep_baseline=False) as pool:
                    case = next(c for c in pool.iter_cards() if c['key'] == CASE_KEY)
                result['case'] = {k:case[k] for k in
                                  ('key', 'domains', 'domain_count', 'publisher_breadth') if k in case}
                result['case']['links'] = [s['url'] for s in case['sources']]
                result['case']['selected_rank'] = next(
                    (i for i, c in enumerate(issue['items'], 1) if c['key'] == CASE_KEY), None)
    finally:
        engine.dispose()
    dump(work / 'observations.json', result)


def compare(args):
    baseline, new = (load(path / 'observations.json') for path in (args.baseline, args.current))
    if baseline['version'] != '0.33.0' or new['version'] != '0.34.0':
        raise ValueError('Run against the actual installed 0.33.0 and 0.34.0 packages')
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    protocol = load(STUDY / 'public-protocol.json')
    answer, draw = {}, []
    report = {'baseline':baseline['version'], 'current':new['version'],
              'case':{'baseline':baseline['case'], 'current':new['case']}, 'days':{}}
    for day, left in sorted(baseline['days'].items()):
        right = new['days'][day]
        old, now = left['cards'], right['cards']
        a, b = {c['key']:c for c in old}, {c['key']:c for c in now}
        if len(a) != len(old) or len(b) != len(now):
            raise ValueError(f'Duplicate selected key: {day}')
        report['days'][day] = {
            'valid_candidates':right['stats']['scanned'], 'groups':right['stats']['groups'],
            'previous_seats':len(old), 'current_seats':len(now), 'common_keys':len(a.keys() & b.keys()),
            'same_position':sum(x['key']==y['key'] for x,y in zip(old,now)),
            'previous_fingerprints':ordered_hash(old), 'current_fingerprints':ordered_hash(now),
            'exited':len(a.keys()-b.keys()), 'entered':len(b.keys()-a.keys()),
        }
        if day not in protocol['held_out_dates']:
            continue
        for group, candidates, others in [('previous_only',a,b),('current_only',b,a)]:
            options = []
            for k in candidates.keys() - others.keys():
                blind = digest(f'publisher-breadth-034|{day}|{k}')[:24]
                options.append((digest(blind),blind,candidates[k]))
            for _, blind, card in sorted(options)[:20]:
                answer[blind] = {'day':day, 'cohort':group, 'progress_key':card['key']}
                draw.append({'id':blind,'day':day,'title':card['title']})
    dump(out / 'result.json', report)
    dump(out / 'answer.json', answer)
    with (out / 'review.csv').open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=('id', 'day', 'title', 'worthwhile', 'note'))
        writer.writeheader()
        for row in sorted(draw, key=lambda r:r['id']):
            title=row['title']
            if title.lstrip().startswith(('=', '+', '-', '@')) or title.startswith(('\t','\r','\n')):
                row['title']="'"+title
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    replay = sub.add_parser('run', help='Generate seven frozen editions under the installed version')
    replay.add_argument('--archives', required=True, type=Path)
    replay.add_argument('--download', action='store_true', help='Download missing official archives')
    replay.add_argument('--out', required=True, type=Path, help='New work directory')
    comparison = sub.add_parser('compare', help='Export observations and blinded sample for the three new dates')
    comparison.add_argument('--baseline', required=True, type=Path)
    comparison.add_argument('--current', required=True, type=Path)
    comparison.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    (run if args.command == 'run' else compare)(args)


if __name__ == '__main__':
    main()

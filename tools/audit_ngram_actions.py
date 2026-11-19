"""Inspect one local English GDELT Events/Mentions batch against a Web NGrams minute.

By default stdout contains hashes, counts and decisions only. With --private-review,
the optional file also contains reconstructed text and original URLs for local review.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from zipfile import BadZipFile, ZipFile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from news2douyin.intake import ngrams
from news2douyin.intake.official_parser import decoded_row
from news2douyin.selection.ngram_actions import REASON_LABELS, diagnose_excerpt
from news2douyin.selection.source_content import source_url


def digest(value):
    return sha256(value.encode('utf-8')).hexdigest()


def file_digest(path):
    hasher = sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def archive_time(path, ending):
    if not path.name.endswith(ending):
        raise ValueError(f'Expected a filename ending in {ending}')
    stamp = path.name[:-len(ending)]
    if len(stamp) != 14 or not stamp.isascii() or not stamp.isdigit() or not stamp.endswith('00'):
        raise ValueError(f'Expected a UTC minute prefix for {path.name}')
    return datetime.strptime(stamp, '%Y%m%d%H%M%S')


def rows(path, feed, stamp, counts):
    with ZipFile(path) as archive:
        members = [entry for entry in archive.infolist() if not entry.is_dir()]
        if len(members) != 1 or members[0].file_size > 1024**3:
            raise ValueError('Expected a single GDELT table no larger than 1 GiB')
        with archive.open(members[0]) as table:
            for index, raw in enumerate(table, 1):
                parsed = decoded_row(raw, feed, stamp, index, '')
                if parsed['valid']:
                    counts['valid'] += 1
                    yield parsed['metadata']
                else:
                    counts['invalid'] += 1


def audit(events, mentions, minute_file, documents=40, sample=25, private=False):
    official_time = archive_time(events, '.export.CSV.zip')
    if official_time != archive_time(mentions, '.mentions.CSV.zip'):
        raise ValueError('Choose Events and Mentions files from the same UTC batch')
    minute = archive_time(minute_file, '.webngrams.json.gz')
    if not timedelta() <= official_time - minute < timedelta(minutes=15):
        raise ValueError('Choose one of the 15 minutes ending with the Events/Mentions batch')
    if not (1 <= documents <= ngrams.MAX_URLS) or sample < 0:
        raise ValueError(f'--documents must be 1 to {ngrams.MAX_URLS}; --sample must be nonnegative')
    stamp = official_time.strftime('%Y%m%d%H%M%S')
    minute_stamp = minute.strftime('%Y%m%d%H%M%S')
    event_rows, mention_rows = Counter(), Counter()
    events_by_id = {r['event_id']: r for r in rows(events, 'events_english', stamp, event_rows)}
    mentions_by_url = defaultdict(set)
    for row in rows(mentions, 'mentions_english', stamp, mention_rows):
        mentions_by_url[row['doc_identifier']].add(row['event_id'])
    urls = sorted(mentions_by_url, key=digest)[:documents]
    results = ngrams.reconstruct_local_archive(minute_file, minute_stamp, [u for u in urls if source_url(u)])
    states = Counter(r.status for r in results.values())
    if len(urls) > len(results):
        states['ineligible_identifier'] = len(urls) - len(results)
    reasons = Counter()
    records = []
    for url, result in results.items():
        if result.status != 'ready':
            continue
        for event_id in mentions_by_url[url]:
            event = events_by_id.get(event_id)
            if event is None:
                continue
            left, right, code = event['actor1_name'], event['actor2_name'], event['event_root_code']
            decision = diagnose_excerpt(result.text, result.language, left, right, code)
            records.append({'pair_sha256': digest(event_id+'\n'+url), 'event_id': event_id,
                            'doc_sha256': digest(url), 'gate_reason': decision.reason,
                            'cameo_code': event['event_code'], 'event_date': event['event_date']})
            if private:
                records[-1].update(url=url, actor1=left, actor2=right, language=result.language,
                                   reconstructed_text=result.text, assessment='')
            reasons[decision.reason] += 1
    records.sort(key=lambda r: r['pair_sha256'])
    selected = records[:sample]
    public_keys = ('pair_sha256', 'event_id', 'doc_sha256', 'gate_reason', 'cameo_code', 'event_date')
    public = dict(official_stamp_utc=stamp, ngrams_minute_utc=minute_stamp,
                  archive_sha256={p.name: file_digest(p) for p in (events, mentions, minute_file)},
                  event_rows=dict(event_rows), mention_rows=dict(mention_rows), distinct_documents=len(mentions_by_url),
                  sampled_documents=len(urls), reconstruction=dict(sorted(states.items())), paired_records=len(records),
                  passed=reasons['matched'], gate_reasons=dict(sorted(reasons.items())),
                  reason_labels={k: REASON_LABELS[k] for k in sorted(reasons)},
                  pair_sample=[{k: row[k] for k in public_keys} for row in selected])
    return public, selected if private else []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('events', 'mentions', 'ngrams'):
        parser.add_argument('--'+name, required=True, type=Path)
    parser.add_argument('--documents', type=int, default=40, help='number of SHA-256 ordered exact document identifiers')
    parser.add_argument('--sample', type=int, default=25, help='number of SHA-256 ordered joined pairs to list')
    parser.add_argument('--private-review', type=Path, help='new local JSON file with text and blank assessments')
    args = parser.parse_args()
    try:
        summary, review = audit(args.events, args.mentions, args.ngrams, args.documents, args.sample, bool(args.private_review))
        if args.private_review:
            with open(args.private_review, 'x', encoding='utf-8', opener=lambda path, flags: os.open(path, flags, 0o600)) as out:
                json.dump(review, out, ensure_ascii=False, indent=2)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except (OSError, BadZipFile, EOFError, ValueError, zlib.error) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    main()

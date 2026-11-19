"""Check source-family scoring on synthetic GKG articles, entirely offline.

Run from the release checkout with its Python dependencies installed.
With --postgres also publish the same archive into a disposable local PG cluster
(pgserver is needed only for that check). Existing databases are never opened.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from sqlmodel import Session

from news2douyin.digest.archive import iter_gkg
from news2douyin.digest.publisher_families import IHEART_HOSTS, NBC_LOCAL_HOSTS
from news2douyin.digest.ranking import MAX_DOMAIN_SAMPLE, MAX_SOURCES, grouped_candidates
from news2douyin.digest.service import build_issue, read_issue
from news2douyin.intake.lake import Lake
from news2douyin.storage.db import make_engine, init_db


DAY = date(2025, 3, 1)
OBSERVED = '2025-03-01T08:00:00Z'


def make_archive(root):
    network = sorted(IHEART_HOSTS)[:80]
    if len(network) != 80:
        raise ValueError('The diagnostic fixture requires at least 80 verified station hosts')
    # Deliberately sort unknown names after the known network, testing whether
    # host truncation would hide the other source units in the mixed groups.
    unknown = [f'zzzz-unknown-{n:03}.example' for n in range(80)]
    other_family = sorted(NBC_LOCAL_HOSTS)[:3]
    rows, stories = [], {}
    kinds = (['network'] * 50 + ['two_hosts'] * 75 + ['two_families'] * 15 + ['mixed'] * 9 + ['overflow'])
    for sequence, kind in enumerate(kinds):
        hosts = {'network': network, 'two_hosts': unknown[:2], 'two_families': network[:3] + other_family,
                 'mixed': network + unknown[:3],
                 'overflow': network + unknown}[kind]
        title = f'District reports on regional transport survey {sequence:04}'
        urls = [f'https://{host}/synthetic-survey/{sequence:04}' for host in hosts]
        stories[title] = (kind, set(urls))
        rows.extend((title, url) for url in urls)
    titles, urls = zip(*rows)
    with Lake(root).connection(create=True) as db:
        db.execute("""INSERT INTO intake_records_v2 (feed,stamp,row_index,title,url,valid)
            SELECT 'gkg_english','20250301080000',unnest(?),unnest(?),unnest(?),true""",
                   [list(range(1, len(rows)+1)), list(titles), list(urls)])
        db.execute("""INSERT INTO intake_archives (feed,stamp,rows,valid_rows,invalid_rows,completed_at)
            VALUES ('gkg_english','20250301080000',?,?,0,?)""", [len(rows), len(rows), OBSERVED])
        assert db.execute('SELECT count(DISTINCT url) FROM intake_records_v2').fetchone()[0] == len(rows)
    return stories


def inspect_cards(pool, stories):
    current = pool.select(mode='balanced')['items']
    previous = pool.select(mode='balanced_v033')['items']
    examples = {}
    all_cards = list(pool.iter_cards())
    assert len(all_cards) == len(stories)
    for card in all_cards:
        kind, urls = stories[card['title']]
        breadth = card['publisher_breadth']
        total_units = {'network': 1, 'two_hosts': 2, 'two_families': 2, 'mixed': 4, 'overflow': 81}[kind]
        assert breadth['units'] == min(total_units, MAX_DOMAIN_SAMPLE), (kind, breadth)
        assert breadth['units_capped'] == (total_units > MAX_DOMAIN_SAMPLE)
        assert breadth['folded'] == (kind != 'two_hosts')
        if kind == 'two_families':
            assert len(breadth['mapped_families']) == 2 and breadth['unmatched_hosts'] == 0
        assert card['observations'] == len(urls)
        assert card['domain_count'] == min(len(urls), MAX_DOMAIN_SAMPLE)
        assert card['domains_capped'] == (len(urls) > MAX_DOMAIN_SAMPLE)
        samples = {s['url'] for s in card['sources']}
        assert len(samples) == min(len(urls), MAX_SOURCES) and samples <= urls
        assert card['source_url'] in samples
        examples[kind] = {'archived_urls':len(urls), 'displayed_domain_count':card['domain_count'],
                          'domains_capped':card['domains_capped'], 'units':breadth['units'],
                          'units_capped':breadth['units_capped'], 'folded':breadth['folded'], 'shown_links':len(samples)}
    expected = {'mixed':9, 'overflow':1, 'two_hosts':75, 'two_families':15}
    current_counts = Counter(stories[c['title']][0] for c in current)
    previous_counts = Counter(stories[c['title']][0] for c in previous)
    assert current_counts == expected and previous_counts['network'] == 50
    return current, previous_counts, current_counts, examples, all_cards


def publish(root, url, selected):
    engine = make_engine(url)
    try:
        init_db(engine)
        issue = build_issue(engine, DAY, storage_root=root)
        assert issue['status'] == 'ready' and issue['items'] == selected
        with Session(engine) as session:
            assert issue == read_issue(session, DAY)
    finally:
        engine.dispose()


def run(pg):
    with TemporaryDirectory(prefix='n2d-syndication-qa-') as tmp:
        root = Path(tmp)
        stories = make_archive(root)
        observed = list(iter_gkg(root, DAY))
        with grouped_candidates(observed) as pool:
            selected, old, new, examples, cards = inspect_cards(pool, stories)
        with grouped_candidates(reversed(observed)) as reverse:
            assert list(reverse.iter_cards()) == cards
            assert reverse.select(mode='balanced')['items'] == selected
        # Local/official observations can already contain a list of domains.
        # Folding must see the unknown hosts even if the raw sample trims them.
        mixed = next((row, stories[row.title][1]) for row in observed if stories[row.title][0] == 'mixed')
        representative, mixed_urls = mixed
        grouped = replace(representative, domains=tuple(sorted(urlsplit(url).hostname for url in mixed_urls)))
        with grouped_candidates([grouped]) as one:
            aggregate, = one.iter_cards()
            assert aggregate['domains_capped'] and aggregate['publisher_breadth']['units'] == 4
        publish(root, 'sqlite:///' + (root/'edition.db').as_posix(), selected)
        databases = ['SQLite']
        if pg:
            from benchmark_topic_projection import isolated_postgres
            with isolated_postgres(root, 'en_US.utf8') as (urls, meta):
                publish(root, urls['chunked'], selected)
                databases.append(meta['version'])
        snapshot = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        return dict(synthetic=True, input_documents=len(observed), distinct_titles=len(stories),
                    archived_urls=len({item.source_url for item in observed}),
                    baseline_top100=dict(old), current_top100=dict(new), source_link_checks=examples,
                    candidate_order_reversed_checks=['all group evidence','ordered top 100'],
                    single_candidate_domain_overflow={'input_hosts':len(mixed_urls), 'effective_units':4},
                    frozen_backends=databases, ordered_card_sha256=hashlib.sha256(snapshot).hexdigest())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.postgres), ensure_ascii=False))

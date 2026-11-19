"""Blind evaluation of daily radar editions against the same observed input."""
from __future__ import annotations

import csv
import hashlib
import heapq
import json
import re
import tempfile
from collections import Counter
from datetime import timedelta
from pathlib import Path

from .calendar import days_before

MODES = ('breadth', 'editorial', 'balanced_v031', 'balanced_v032', 'balanced_v033', 'balanced')
LABELS = ('important', 'deep', 'repeat')
HISTORY_DAYS = 7
CSV_FIELDS = ('id', 'day', 'title', 'excerpt', 'observed_at', 'source_url', 'material_origin', *LABELS, 'fact_group')
LEGACY_SCOPE = ('Coverage is relative to judged progress in the pooled lists and sampled omissions. '
         'The sample favors distinct country/attention strata. Absolute global recall is unknown. '
         'balanced_v032 replays the 0.32 ranking and grouping on the stored candidate snapshot. '
         'balanced_v031 reconstructs the prior policy on that snapshot and excludes newly recovered '
         'NGrams-only candidates; it is not the archived 0.31 edition. The editorial method is a rule-based proxy. '
         'Mutable SQL projections may have changed '
         'since original observation; use a contemporaneous database and archive snapshot when available.')
SCOPE = (LEGACY_SCOPE + ' balanced_v033 retains 0.33 headline grouping and raw media-domain breadth. '
         'balanced uses the current publisher-group weighting. The breadth and editorial controls '
         'retain their original raw-domain policy.')


def _digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _cell(text):
    value = str(text or '')
    if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
        return "'" + value
    return value


def _outside_sample(cards, selected, budget, salt, *, previously_selected=()):
    """Pick one per country/attention stratum first, then fill with a seeded sample.

    Keep the best representative per stratum and a global bottom-k sample.
    Strata are deliberately sampled across rather than weighted by media volume.
    """
    first = {}
    top = []
    total = 0
    prior_excluded = 0
    for card in cards:
        key = card['key']
        if key in selected:
            continue
        if key in previously_selected:
            prior_excluded += 1
            continue
        total += 1
        if not budget:
            continue
        country = str(card.get('country') or '').upper()
        country = country if re.fullmatch('[A-Z]{2}', country) else 'unknown'
        has_attention = card.get('attention') is not None or card.get('source_kind') == 'newsnow'
        stratum = (country, has_attention)
        priority = int(_digest(salt, 'sample', key), 16)
        if stratum not in first or priority < first[stratum][0]:
            first[stratum] = (priority, card)
        if len(top) < budget:
            heapq.heappush(top, (-priority, key, card))
        elif priority < -top[0][0]:
            heapq.heapreplace(top, (-priority, key, card))
    strata = sorted(first, key=lambda s: _digest(salt, 'stratum', *s))
    chosen = {first[s][1]['key']: first[s][1] for s in strata[:budget]}
    for _, key, card in sorted(top, reverse=True):
        if len(chosen) == budget:
            break
        chosen[key] = card
    return list(chosen.values()), {'outside_groups': total, 'occupied_strata': len(first),
                                   'prior_read_excluded': prior_excluded}


def export_review(days, candidates_for_day, output: Path, *, limit=100, outside=20, seed='31', warmup=0):
    """Publish a new review bundle atomically. Never replace completed reviewer labels."""
    from .strategies import STRATEGIES

    days = list(days)
    if not days or any(b - a != timedelta(days=1) for a, b in zip(days, days[1:])):
        raise ValueError('Supply consecutive comparison days in chronological order')
    if type(limit) is not int or limit < 1 or not 0 <= outside <= 100 or not 0 <= warmup <= HISTORY_DAYS:
        raise ValueError('limit must be a positive integer; outside 0..100; warmup 0..7')
    output = Path(output)
    if output.exists():
        raise FileExistsError(f'Choose a new review directory: {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    start = days_before(days[0], warmup)
    manifest = {'schema': 'news2douyin.daily-radar-review.v4', 'scope': SCOPE, 'limit': limit,
                'outside_per_day': outside, 'seed': seed, 'warmup_days': (days[0] - start).days, 'days': {}}
    history = {mode: [] for mode in MODES}
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        stage = Path(temp) / 'bundle'
        stage.mkdir()
        with (stage / 'review.csv').open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for offset in range((days[-1] - start).days + 1):
                day = start + timedelta(days=offset)
                stamp = day.isoformat()
                salt = _digest(seed, stamp)
                selected = {}
                selected_progress = set()
                lists = {}

                def add_card(card):
                    # A formerly empty excerpt and the newly available sentence
                    # are different reading experiences even with the same headline.
                    origin = '重建片段，待核原文' if card.get('reconstruction') else '关联来源材料'
                    key = _digest(salt, 'blind', card['key'], card['title'], card['source_url'],
                                  card['excerpt'], card['observed_at'], origin)[:24]
                    selected.setdefault(key, dict(card, material_origin=origin))
                    return key

                source_counts = {}
                selection_stats = {}
                with STRATEGIES.prepare(candidates_for_day(day, source_counts), MODES) as pool:
                    for mode in MODES:
                        prior = {key for previous in history[mode] for key in previous}
                        edition = STRATEGIES.select(pool, mode, limit=limit, prior_keys=prior)
                        cards = edition['items']
                        selection_stats[mode] = edition['stats']
                        keys = [c['key'] for c in cards]
                        history[mode].append(keys)
                        history[mode] = history[mode][-HISTORY_DAYS:]
                        selected_progress.update(keys)
                        lists[mode] = [add_card(card) for card in cards]
                    if day < days[0]:
                        continue
                    selected_progress = pool.current_keys(selected_progress)
                    current_keys = pool.current_key_map(c['key'] for c in selected.values())
                    sampled, stats = [], {}
                    if outside:
                        sampled, stats = _outside_sample(pool.iter_cards(), selected_progress, outside, salt)
                omissions = [add_card(card) for card in sampled]
                manifest['days'][stamp] = {
                    'lists': lists, 'audit_sample': omissions,
                    'progress': {key: _digest(salt, 'progress', current_keys.get(card['key'], card['key']))[:24] for key, card in selected.items()},
                    'legacy_progress': {key: _digest(salt, 'progress', card['key'])[:24] for key, card in selected.items()},
                    'sources': source_counts, 'ranking': selection_stats, **stats}
                for key, item in sorted(selected.items()):
                    writer.writerow(dict(id=key, day=stamp, **{
                        f: _cell(item.get(f)) for f in ('title', 'excerpt', 'observed_at', 'source_url', 'material_origin')}))
        (stage / 'answer-key.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                               encoding='utf-8')
        stage.rename(output)
    return manifest


def score_review(bundle: Path, ratings: Path | None = None):
    """Score only explicit binary judgments; leave unjudged denominators visible."""
    bundle = Path(bundle)
    manifest = json.loads((bundle / 'answer-key.json').read_text(encoding='utf-8'))
    known = {}
    progress = {}
    legacy_progress = {}
    days = manifest['days']
    # Existing review bundles contain three modes, and may already be labeled.
    modes = tuple(next(iter(days.values()))['lists']) if days else ()
    pools = {mode: [] for mode in (*modes, 'audit_sample')}
    for day, group in manifest['days'].items():
        progress.update(group.get('progress', {}))
        legacy_progress.update(group.get('legacy_progress', group.get('progress', {})))
        for mode in modes:
            pools[mode].extend(group['lists'][mode])
        pools['audit_sample'].extend(group['audit_sample'])
        for ids in [*group['lists'].values(), group['audit_sample']]:
            known.update(dict.fromkeys(ids, day))

    # The offline HTML worksheet covers importance and fact groups. Other binary
    # labels remain unjudged unless the editor supplies the full CSV worksheet.
    shortlist = manifest.get('schema') == 'news2douyin.shortlist-review.v1'
    required = {'id', 'day', 'important', 'fact_group'} if shortlist else {'id', 'day', *LABELS}
    values = {}
    facts = {}
    with (ratings or bundle / 'review.csv').open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError('Rating columns must include ' + ', '.join(sorted(required)))
        for number, row in enumerate(reader, 2):
            key = row['id']
            if key not in known or known[key] != row['day'] or key in values:
                raise ValueError(f'Unrecognized, duplicate or misdated id on row {number}')
            judgments = {}
            for label in LABELS:
                val = (row.get(label) or '').strip()
                if val not in ('', '0', '1'):
                    raise ValueError(f'Use 1, 0 or blank for {label} on row {number}')
                if val:
                    judgments[label] = int(val)
            values[key] = judgments
            # An editor may link two different cards to the same real-world
            # development even when the grouping algorithm gave them distinct IDs.
            group = (row.get('fact_group') or '').strip()
            if shortlist and group == '-':
                facts[key] = (known[key], 'id', key)
            elif group:
                facts[key] = (known[key], 'group', group)

    def fact(key):
        return facts.get(key, (known[key], 'id', progress.get(key, key)))

    important_facts = {fact(key) for key, judgment in values.items() if judgment.get('important') == 1}
    results = {}
    for mode, keys in pools.items():
        summary = {}
        for label in LABELS:
            labels = Counter(values.get(key, {}).get(label) for key in keys)
            judged = labels[0] + labels[1]
            summary[label] = dict(positive=labels[1], judged=judged, unjudged=labels[None],
                                  rate=labels[1] / judged if judged else None)
        tagged = [facts[key] for key in keys if key in facts]
        results[mode] = {'cards': len(keys), **summary,
                         'manually_grouped_cards': len(tagged),
                         'confirmed_duplicate_slots': len(tagged) - len(set(tagged))}
        if mode != 'audit_sample':
            hits = len(important_facts.intersection(fact(key) for key in keys))
            results[mode]['judged_important_coverage'] = {
                'hits': hits, 'pool_and_sample_positives': len(important_facts),
                'ratio': hits / len(important_facts) if important_facts else None}
    default_scope = SCOPE if 'balanced_v033' in pools else LEGACY_SCOPE
    report = {'scope': manifest.get('scope', default_scope), 'days': len(days), 'outcomes': results}
    # In an older bundle 'balanced' always denotes the last policy shipped at
    # export time. Compare consecutive saved versions without relabeling it.
    versions = [('balanced_v031', 'balanced_v032' if 'balanced_v032' in pools else 'balanced', '031', '032'),
                ('balanced_v032', 'balanced_v033' if 'balanced_v033' in pools else 'balanced', '032', '033'),
                ('balanced_v033', 'balanced', '033', '034')]
    for older, newer, old_name, new_name in versions:
        if older not in pools or newer not in pools:
            continue
        before, after = set(pools[older]), set(pools[newer])
        comp_progress = legacy_progress if new_name == '032' else progress
        old_progress = {comp_progress.get(k, k) for k in before}
        new_progress = {comp_progress.get(k, k) for k in after}
        same_progress = old_progress & new_progress
        same_cards = {comp_progress.get(k, k) for k in before & after}
        comparison = {'shared_progress': len(same_progress), 'identical_cards': len(before & after),
                      'updated_material_in_shared_progress': len(same_progress - same_cards)}
        if older == 'balanced_v033':
            same_rank = 0
            paired_slots = 0
            for group in days.values():
                old_slots = group['lists'][older]
                new_slots = group['lists'][newer]
                paired_slots += min(len(old_slots), len(new_slots))
                same_rank += sum(a == b for a, b in zip(old_slots, new_slots))
            comparison.update(paired_slots=paired_slots, identical_cards_at_same_rank=same_rank,
                              identical_cards_at_changed_rank=len(before & after)-same_rank)
        for label, keys in ((f'entering_v{new_name}', {k for k in after if comp_progress.get(k, k) not in old_progress}),
                            (f'displaced_v{old_name}', {k for k in before if comp_progress.get(k, k) not in new_progress})):
            comparison[label] = {'cards': len(keys)}
            for metric in LABELS:
                judged = [values[k][metric] for k in keys if metric in values.get(k, {})]
                comparison[label][metric] = {'positive': sum(judged), 'judged': len(judged), 'unjudged': len(keys)-len(judged)}
        report[f'comparison_{old_name}_{new_name}'] = comparison
    return report

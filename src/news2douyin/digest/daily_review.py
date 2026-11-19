"""Blind, read-only audit of one actual or simulated reading list per day."""
from __future__ import annotations

import csv
import json
import tempfile
from collections import Counter
from datetime import timedelta
from pathlib import Path

from .calendar import days_before
from .evaluation import HISTORY_DAYS, _cell, _digest, _outside_sample
from .ranking import card_progress_key
from .strategies import DEFAULT_STRATEGY, STRATEGIES

FIELDS = ('id', 'day', 'title', 'excerpt', 'observed_at', 'source_url', 'material_origin',
          'important', 'fact_group', 'note')
SCHEMA = 'news2douyin.daily-editor-review.v1'


def _material(card):
    return {
        'title': card.get('title', ''), 'excerpt': card.get('excerpt', ''),
        'observed_at': card.get('observed_at', ''), 'source_url': card.get('source_url', ''),
        'material_origin': '重建片段，待核原文' if card.get('reconstruction') else '关联来源材料',
    }


def export_daily(days, candidates_for_day, out, *, edition='published', issue_for_day=None,
                 outside=20, seed='daily-33', limit=100, warmup=7):
    """Export frozen slots verbatim, or explicitly request a chronological replay.

    The outside sample is stratified over today's *observable, groupable* candidates.
    For a published issue the outside population is still a reconstruction.
    """
    from .review_page import write_review_page

    days = list(days)
    if not days or any(b - a != timedelta(days=1) for a, b in zip(days, days[1:])):
        raise ValueError('Choose consecutive Beijing days in order')
    if edition not in ('published', 'replay') or edition == 'published' and not issue_for_day:
        raise ValueError('Published editions require a read-only issue reader')
    if type(limit) is not int or limit < 1 or not 0 <= outside <= 100 or not 0 <= warmup <= HISTORY_DAYS:
        raise ValueError('limit must be a positive integer, outside 0..100, warmup 0..7')
    out = Path(out)
    if out.exists():
        raise FileExistsError(f'Choose a new review directory: {out}')
    out.parent.mkdir(parents=True, exist_ok=True)
    start = days_before(days[0], warmup) if edition == 'replay' else days[0]
    manifest = dict(schema=SCHEMA, edition=edition, seed=seed, outside_per_day=outside,
                    warmup_days=(days[0] - start).days, days={})
    history = []

    def remember(slots):
        # Normalized and saved identifiers cover both old and newly merged headlines.
        history.append({key for c in slots for key in (c['key'], card_progress_key(c))})
        del history[:-HISTORY_DAYS]

    if edition == 'published':
        previous = days_before(days[0], HISTORY_DAYS)
        while previous < days[0]:
            prior_row = issue_for_day(previous)
            remember(prior_row.get('items', ()) if prior_row['status'] == 'ready' else ())
            previous += timedelta(days=1)

    with tempfile.TemporaryDirectory(dir=out.parent) as temp:
        stage = Path(temp) / 'bundle'
        stage.mkdir()
        with (stage/'review.csv').open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            for offset in range((days[-1] - start).days + 1):
                day = start + timedelta(days=offset)
                stamp = day.isoformat()
                info = {}
                if edition == 'published':
                    row = issue_for_day(day)
                    info = {'status': row['status'], 'generated_at': row.get('generated_at', '')}
                    info.update({k: row[k] for k in ('edition_kind', 'revision', 'cutoff_at') if k in row})
                    if row['status'] != 'ready':
                        manifest['days'][stamp] = info
                        remember(())
                        continue
                    selected = row['items']
                counts = {}
                with STRATEGIES.prepare(candidates_for_day(day, counts), DEFAULT_STRATEGY) as pool:
                    prior = {key for previous in history for key in previous}
                    if edition == 'replay':
                        result = STRATEGIES.select(pool, DEFAULT_STRATEGY, prior_keys=prior, limit=limit)
                        selected = result['items']
                        if day < days[0]:
                            remember(selected)
                            continue
                        info = {'status': 'replayed', 'generated_at': ''}
                    selected_keys = {c['key'] for c in selected}
                    excluded = pool.current_keys(selected_keys) | {card_progress_key(c) for c in selected}
                    sampled, stats = _outside_sample(pool.iter_cards(), excluded, outside, _digest(seed, stamp),
                                                     previously_selected=pool.current_keys(prior))
                    remember(selected)
                blind_rows = {}

                def blind(card):
                    data = _material(card)
                    key = _digest(seed, stamp, 'daily-review', card['key'], data)[:24]
                    if key in blind_rows:
                        raise ValueError(f'Duplicate review card on {stamp}')
                    blind_rows[key] = {name: _cell(value) for name, value in data.items()}
                    return key

                # Keep exact slot order only in the answer key.
                selected_ids = [blind(c) for c in selected]
                sampled_ids = [blind(c) for c in sampled]
                examples = {key: {'title': card['title'], 'source_url': card['source_url']}
                            for key, card in zip(sampled_ids, sampled)}
                info.update(selected=selected_ids, sampled=sampled_ids, sampled_sources=examples,
                            sources=counts, **stats)
                manifest['days'][stamp] = info
                for key, data in sorted(blind_rows.items()):
                    writer.writerow(dict(id=key, day=stamp, **data))
        (stage/'daily-key.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        write_review_page(stage)
        stage.rename(out)
    return manifest


def _load_ratings(path, known):
    ratings = {}
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {'id', 'day', 'important', 'fact_group'} <= set(reader.fieldnames):
            raise ValueError('Ratings require id, day, important, fact_group')
        for line, row in enumerate(reader, 2):
            key = row['id']
            if key not in known or row['day'] != known[key] or key in ratings:
                raise ValueError(f'Unrecognized, duplicate or misdated id on row {line}')
            importance = (row['important'] or '').strip()
            if importance not in ('', '0', '1'):
                raise ValueError(f'Use 1, 0 or blank for important on row {line}')
            ratings[key] = (importance, (row.get('fact_group') or '').strip(), (row.get('note') or '').strip())
    return ratings


def _importance(keys, rating):
    counts = Counter(rating(k)[0] for k in keys)
    return dict(total=len(keys), worthwhile=counts['1'], judged=counts['1']+counts['0'], unjudged=counts[''])


def _score_day(info, ratings):
    if 'selected' not in info:
        return dict(status=info['status'], generated_at=info.get('generated_at', ''))

    def rating(key):
        return ratings.get(key, ('', '', ''))

    picked, sampled = info['selected'], info['sampled']
    selected = _importance(picked, rating)
    outside = _importance(sampled, rating)
    picked_groups = Counter(rating(k)[1] for k in picked)
    checked = len(picked) - picked_groups['']
    duplicates = sum(n - 1 for group, n in picked_groups.items() if group not in ('', '-') and n > 1)
    worthwhile = [key for key in picked if rating(key)[0] == '1']
    distinct = {(('single', key) if group == '-' else ('group', group))
                for key in worthwhile if (group := rating(key)[1])}
    selected.update(duplicate_slots=duplicates, grouping_checked=checked, grouping_unjudged=len(picked)-checked,
                    confirmed_worthwhile_groups=len(distinct),
                    worthwhile_grouping_pending=sum(not rating(key)[1] for key in worthwhile))

    misses = {}
    covered, unresolved = 0, 0
    for key in sampled:
        imp, group, note = rating(key)
        if imp != '1':
            continue
        if group and group != '-' and group in picked_groups:
            covered += 1
            continue
        # A '-' is an explicit editor assertion of uniqueness across this day's packet.
        # For a named group, incompletely grouped selected slots could cover it.
        if picked and (not group or (group != '-' and checked < len(picked))):
            unresolved += 1
            continue
        identity = ('group', group) if group not in ('', '-') else ('card', key)
        item = info['sampled_sources'][key]
        misses.setdefault(identity, []).append(dict(id=key, title=item['title'], source_url=item['source_url'], note=note))

    outside.update(population=info['outside_groups'], prior_read_excluded=info['prior_read_excluded'],
                   gkg_archive_present=info['sources'].get('gkg_archive_present'),
                   confirmed_missed_cards=sum(map(len, misses.values())),
                   confirmed_missed_groups=sum(bool(rating(rows[0]['id'])[1]) for rows in misses.values()),
                   confirmed_missed_ungrouped_cards=sum(not rating(rows[0]['id'])[1] for rows in misses.values()),
                   covered_worthwhile_cards=covered, unresolved_worthwhile_cards=unresolved)
    return dict(status=info['status'], generated_at=info['generated_at'], selected=selected,
                outside_sample=outside, missed_examples=[example for group in misses.values() for example in group])


def score_daily(bundle, ratings=None):
    bundle = Path(bundle)
    manifest = json.loads((bundle/'daily-key.json').read_text(encoding='utf-8'))
    if manifest['schema'] != SCHEMA:
        raise ValueError('Use a daily review bundle created by export-daily')
    known = {key: day for day, info in manifest['days'].items()
             for key in (*info.get('selected', []), *info.get('sampled', []))}
    values = _load_ratings(ratings or bundle/'review.csv', known)
    results = {day: _score_day(info, values) for day, info in manifest['days'].items()}
    return {'schema': SCHEMA, 'edition': manifest['edition'], 'days': results}


def format_daily(report):
    mode = '已发布日报' if report['edition'] == 'published' else '按现有数据回放'
    lines = [f'评审对象：{mode}。数字仅统计明确填写的标签。']
    for day, item in report['days'].items():
        if 'selected' not in item:
            lines.append(f'{day} ｜ 无已发布日报（{item["status"]}），未评审。')
            continue
        sel, out = item['selected'], item['outside_sample']
        lines.append(f"{day} ｜ 入选 {sel['total']} 条，值得读 {sel['worthwhile']}/{sel['judged']} 已评，"
                     f"{sel['unjudged']} 条未评。重复占位 {sel['duplicate_slots']}，"
                     f"其中 {sel['grouping_checked']}/{sel['total']} 条已核对同日进展。"
                     f"已确认值得读的不同进展 {sel['confirmed_worthwhile_groups']} 项，"
                     f"另有 {sel['worthwhile_grouping_pending']} 条值得读且归组待核。")
        lines.append(f"  另剔除近7天已选 {out['prior_read_excluded']} 组，落选候选组 {out['population']}，抽看 {out['total']} 条，"
                     f"重要性已评 {out['judged']}，未评 {out['unjudged']}。"
                     f"确认遗漏 {out['confirmed_missed_cards']} 条，"
                     f"值得读且已覆盖 {out['covered_worthwhile_cards']} 条，"
                     f"值得读但覆盖待核 {out['unresolved_worthwhile_cards']} 条。")
        if out['gkg_archive_present'] is False:
            lines.append('  本次没有找到 GKG 归档，落选总体只包含事务库中的可读候选。')
        for example in item['missed_examples']:
            lines.append(f"    {example['title']}  {example['source_url']}")
    return '\n'.join(lines)

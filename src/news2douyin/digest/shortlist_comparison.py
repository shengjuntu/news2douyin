"""Review a published shortlist and three independent replay arms on the same evidence."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from .calendar import day_bounds_utc
from .evaluation import CSV_FIELDS, _cell, _digest, _outside_sample
from .handoff import card_token
from .review_page import write_review_page
from .shortlist import render_shortlist
from .strategies import DEFAULT_STRATEGY, STRATEGIES

TRIAL_ARMS = {name: 'shortlist_' + name for name in ('classification', 'merge', 'combined')}
STRATEGY_IDS = (DEFAULT_STRATEGY, *TRIAL_ARMS.values())
SCHEMA = 'news2douyin.shortlist-review.v1'
SCOPE = ('四组使用同一期资料截止时间、可读候选和此前七日的已读排除名单。'
         '基线已核对入选顺序、来源材料与可用的候选计数。'
         '换入或换出按独占一个名额的代表条目计。合并条目仍可作为代表的关联材料阅读。'
         '榜外抽查从四组的代表条目以外取样，可能包含已附为关联材料的报道。'
         '盲评按当时已有来源判断具体进展，覆盖率的分母是获标注的材料；全球漏报率未知。')


def _published_window(issue, day):
    if issue.get('status') != 'ready':
        raise ValueError('这一天没有已发布日报可供对照')
    _, end = day_bounds_utc(day)
    if issue.get('edition_kind') == 'intraday':
        stamp = issue.get('cutoff_at')
        if not stamp:
            raise ValueError('滚动版缺少资料截止时间，无法按同一版本对照')
        end = min(end, datetime.fromisoformat(stamp.replace('Z', '+00:00')) + timedelta(seconds=1))
    restored = issue['stats'].get('sources', {}).get('gkg_restored_batches', {})
    return end, [tuple(key.split(':', 1)) for key in restored]


def _published_comparison_limit(issue):
    """Compare a frozen balanced list at its original size and strategy version."""
    record = issue['stats'].get('selection')
    if record is None:
        return 100  # Editions published before strategy provenance was recorded.
    baseline = STRATEGIES.get(DEFAULT_STRATEGY)
    if (record.get('strategy') != baseline.id or record.get('version') != baseline.version
            or record.get('options') != {}):
        raise ValueError(f'该期由 {record.get("strategy")}（{record.get("version")}）生成，'
                         f'此对照需要 {baseline.id}（{baseline.version}）的默认参数。')
    limit = record.get('limit')
    if type(limit) is not int or limit < 1:
        raise ValueError('已发布日报缺少可回放的入选名额')
    return limit


def _verify_published_baseline(issue, replay):
    stats = issue.get('stats') or {}
    count_fields = ('scanned', 'groups', 'prior_excluded', 'eligible', 'scanned_by_source')
    changed = any(stats[k] != replay['stats'].get(k) for k in count_fields if k in stats)
    if changed or [card_token(c) for c in issue['items']] != [card_token(c) for c in replay['items']]:
        raise ValueError('以当期截止时间回放的名次或证据与已发布版本有差异；请用发布时的数据库与归档快照对照')


def _write_blind_csv(path, materials, stamp):
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for key, card in sorted(materials.items()):
            visible = {name: _cell(card.get(name)) for name in ('title','excerpt','observed_at','source_url')}
            origin = '重建片段，待核原文' if card.get('reconstruction') else '关联来源材料'
            writer.writerow(dict(id=key, day=stamp, material_origin=origin, **visible))



def _changes(baseline, items):
    old = {c['key'] for c in baseline}
    new = {c['key'] for c in items}
    return dict(shared=len(old & new),
                same_rank=sum(a['key'] == b['key'] for a, b in zip(baseline, items)),
                entering=[c['key'] for c in items if c['key'] not in old],
                displaced=[c['key'] for c in baseline if c['key'] not in new])


def _write_merge_review(stage, arms):
    representatives = {variant: {c['key']: c for c in arm['items']} for variant, arm in arms.items()}
    fields = ('variant', 'representative_id', 'member_id', 'representative_title', 'member_title', 'representative_url',
              'member_url', 'reason', 'same_development', 'note')
    with (stage/'merges.jsonl').open(encoding='utf-8') as source, (stage/'merge-review.csv').open(
            'w', encoding='utf-8-sig', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for line in source:
            pair = json.loads(line)
            variant, member = pair['variant'], pair['member']
            card = representatives[variant][pair['representative_key']]
            row = dict(variant=variant, representative_id=card['key'], member_id=member['key'],
                       representative_title=card['title'], member_title=member['title'],
                       representative_url=card['source_url'], member_url=member['source_url'], reason=member['reason'])
            writer.writerow({key: _cell(value) for key, value in row.items()})


def export_comparison(day, issue, candidates, prior_keys, output, *, outside=20, source_stats=None):
    """Stage a self-contained bundle after exact control-list verification. No production writes."""
    output = Path(output)
    if output.exists():
        raise FileExistsError(f'请选择新的输出目录：{output}')
    if not 0 <= outside <= 100:
        raise ValueError('每期落选抽样数必须在 0 到 100 之间')
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as temp:
        stage = Path(temp) / 'bundle'
        stage.mkdir()
        report = _stage_comparison(day, issue, candidates, prior_keys, stage, outside, source_stats)
        stage.rename(output)
    return report


def _stage_comparison(day, issue, candidates, prior_keys, stage, outside, source_stats):
    stamp = day.isoformat()
    if issue.get('day', stamp) != stamp:
        raise ValueError('已发布日报与回放日期不同')
    ceiling, _ = _published_window(issue, day)
    salt = _digest(SCHEMA, stamp, issue['revision'])
    limit = _published_comparison_limit(issue)
    with STRATEGIES.prepare(candidates, STRATEGY_IDS) as pool:
        control = STRATEGIES.select(pool, DEFAULT_STRATEGY, prior_keys=prior_keys, limit=limit)
        _verify_published_baseline(issue, control)
        # Stable titles and order alone can conceal an edited source group, topic or score.
        ranked_fields = ('score', 'observed_at', 'source_clock', 'attention', 'impact', 'is_primary',
                         'watches', 'focus_hints', 'publisher_breadth', 'domain_count', 'observations')
        if any(saved[name] != replayed.get(name) for saved, replayed in zip(issue['items'], control['items'])
               for name in ranked_fields if name in saved):
            raise ValueError('以当期截止时间回放的排序依据与已发布版本有差异，请使用发布时的快照')
        arms = {'baseline': dict(control, items=issue['items'])}
        with (stage/'merges.jsonl').open('w', encoding='utf-8') as stream:
            def on_merge(row):
                stream.write(json.dumps(row, ensure_ascii=False, separators=(',', ':'))+'\n')

            for variant, strategy_id in TRIAL_ARMS.items():
                arms[variant] = STRATEGIES.select(pool, strategy_id, limit=limit, prior_keys=prior_keys,
                                                  on_merge=on_merge)
        chosen = {c['key'] for arm in arms.values() for c in arm['items']}
        sample, counts = _outside_sample(pool.iter_cards(), chosen, outside, salt,
                                         previously_selected=pool.current_keys(prior_keys))

    basis = {c['key']: c for c in issue['items']}
    materials = {}
    def blind_id(card):
        blind = _digest(salt, card['key'])[:24]
        materials.setdefault(blind, card)
        return blind

    lists = {}
    for name, arm in arms.items():
        arm['changes'] = _changes(issue['items'], arm['items'])
        lists[name] = [blind_id(card) for card in arm['items']]
        if name != 'baseline':
            # Restore the attributed details of already-published representative cards.
            # New entrants retain only what the same-cutoff replay actually recovered.
            for card in arm['items']:
                saved = basis.get(card['key'], {})
                for field in ('geography','category','actor_context','focus_evidence'):
                    if field in saved:
                        card[field] = saved[field]
    ids = [blind_id(card) for card in sample]
    manifest = dict(schema=SCHEMA, scope=SCOPE,
                    days={stamp: dict(lists=lists, audit_sample=ids, revision=issue['revision'],
                                      cutoff_exclusive=ceiling.isoformat(), **counts)})
    report = dict(schema='news2douyin.shortlist-comparison.v1', scope=SCOPE, day=stamp, revision=issue['revision'],
                  edition_kind=issue.get('edition_kind'), cutoff_at=issue.get('cutoff_at'),
                  cutoff_exclusive=ceiling.isoformat(), source_stats=source_stats or {},
                  audit_sample_size=len(ids), arms=arms)
    _write_blind_csv(stage/'review.csv', materials, stamp)
    write_review_page(stage)
    _write_merge_review(stage, arms)
    for filename, data in [('answer-key.json', manifest), ('comparison.json', report)]:
        (stage/filename).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    combined = dict(issue, day=stamp, items=arms['combined']['items'])
    (stage/'shortlist.md').write_text(render_shortlist(combined, trial=True), encoding='utf-8')
    return report


def compare_local(engine, storage_root, day, output, *, outside=20):
    from sqlalchemy import inspect
    from sqlmodel import Session
    from .review_cli import observed_candidates
    from .service import prior_keys, read_issue

    inspector = inspect(engine)
    if not inspector.has_table('dailydigestissue'):
        raise ValueError('工作库中尚无已发布日报可供对照')
    with Session(engine) as session:
        connection = session.connection()
        if connection.dialect.name == 'postgresql':
            connection.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        else:
            connection.exec_driver_sql('BEGIN')
        issue = read_issue(session, day)
        history = prior_keys(session, day)
    ceiling, restored = _published_window(issue, day)
    stats = {}
    candidates = observed_candidates(engine, storage_root, day, stats, cutoff=ceiling,
                                     include_recovered=inspector.has_table('ngramtarget'), restored_stamps=restored,
                                     coarse=STRATEGIES.requirements(STRATEGY_IDS).coarse)
    return export_comparison(day, issue, candidates, history, output, outside=outside, source_stats=stats)

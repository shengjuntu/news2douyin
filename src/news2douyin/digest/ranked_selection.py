"""Static ranking policies sharing the prepared pool and the original score ordering."""
BALANCED_MODES = ('balanced', 'balanced_v031', 'balanced_v032', 'balanced_v033')
MODES = (*BALANCED_MODES, 'breadth', 'editorial')


def check_legacy_limit(limit):
    if type(limit) is not int or not 0 <= limit <= 100:
        raise ValueError('limit must be an integer between 0 and 100')


def selection_reasons(item, mode, within_lane):
    n = item['domain_count']
    count = f'{n}+' if item['domains_capped'] else str(n)
    reasons = [f'关联报道域名 {count} 个']
    if mode == 'balanced' and (publishers := item.get('publisher_breadth')):
        units = f"{publishers['units']}+" if publishers['units_capped'] else str(publishers['units'])
        reasons.append(f'按 {units} 个来源组计算报道广度')
    if mode != 'breadth':
        if item['impact'] > 0:
            reasons.append('标题变化信号或来源事件编码参与排序')
        if item['is_primary']:
            reasons.append('发布机构原始文件')
        if item['excerpt']:
            reasons.append('有可读摘录')
    if mode in ('balanced', 'balanced_v032', 'balanced_v033'):
        if item.get('coverage_growth_only'):
            reasons.append('已有线索的报道增加，所选材料的行动进展待核')
        if coverage := item.get('official_coverage'):
            if coverage.get('history_complete') and coverage['new_documents']:
                reasons.append(f"GDELT 在本期首次提及的关联文档 {coverage['new_documents']} 篇，新增报道域名 {coverage['new_domains']} 个")
        if action := item.get('action_evidence'):
            reasons.append(action['basis'])
    if mode in BALANCED_MODES:
        if item['watches']:
            reasons.append('已关联关注专题')
        if item['focus_hints']:
            reasons.append('标题与专题范围匹配（待核）：' + '、'.join(item['focus_hints'][:3]))
        if item['attention'] is not None:
            reasons.append('有中文平台热度观测')
        if within_lane > 1:
            reasons.append(f'本期同一主题方向第 {within_lane} 条，已降低重复占位权重')
    return reasons


def select_ranked(pool, *, mode, policy, ranking, prior_keys, limit):
    """Select from a prepared grouping policy in the requested ordering.

    Registry entries bind policy (the grouping generation) independently of
    ranking (the score and order). SQL identifiers below are internal constants.
    """
    db = pool.db
    table, stats = pool.group_index(policy)
    if policy in ('current', 'v033'):
        prior_keys = pool.current_keys(prior_keys)
    db.execute('DELETE FROM prior')
    db.executemany('INSERT OR IGNORE INTO prior VALUES (?)', ((key,) for key in prior_keys))
    excluded = db.execute(f'SELECT count(*) FROM prior JOIN {table} USING (key)').fetchone()[0]
    available = f'NOT EXISTS (SELECT 1 FROM prior WHERE prior.key = {table}.key)'
    if ranking == 'balanced':
        # Within each coverage lane the i-th pick has a diminishing return.
        # Because the penalty increases and the base score decreases, this
        # one window sort gives the same first K as repeatedly choosing the
        # highest marginal benefit. No arbitrary shortlist can hide a lane.
        query = f"""
            WITH scores AS (
                SELECT key, observed, lane, balanced FROM {table} WHERE {available}
            ), ordered AS (
                SELECT key, observed, balanced, row_number() OVER (
                    PARTITION BY lane ORDER BY balanced DESC, observed DESC, key
                ) AS position FROM scores
            )
            SELECT key, balanced - 24.0*(1.0 - 1.0/position) AS score, position
            FROM ordered ORDER BY score DESC, balanced DESC, observed DESC, key LIMIT ?
        """
    else:
        column = {'breadth': 'breadth', 'editorial': 'editorial'}[ranking]
        query = f"""SELECT key, {column} AS score, 1 FROM {table} WHERE {available}
                   ORDER BY score DESC, observed DESC, key LIMIT ?"""
    # SQLite binds a signed 64-bit integer; N larger than this pool means all rows.
    selected = list(db.execute(query, (min(limit, stats['groups']),)))
    keys = [key for key, _, _ in selected]
    cards = pool.cards_for(keys, policy=policy)
    items = []
    for key, score, lane_position in selected:
        item = cards[key]
        item.update(score=round(score, 3), reasons=selection_reasons(item, mode, lane_position))
        items.append(item)
    stats = dict(stats, prior_excluded=excluded, eligible=stats['groups']-excluded,
                 selected=len(items), mode=mode)
    return dict(items=items, stats=stats)

"""Read-only shortlist experiments over a shared day of grouped observations.

The temporary disk index orders every eligible group. Within a (category, country)
bucket all remaining candidates receive exactly the same coverage bonus, so its
head suffices for exact greedy selection. Only known attributes earn a bonus.
"""
from collections import Counter
from heapq import heappop, heappush
import json

from .ranked_selection import selection_reasons

VARIANTS = ('classification', 'merge', 'combined')
# Small experimental priors, not learned estimates of importance or source reliability.
GENRE_POINTS = {'promotion': -36.0, 'review': -30.0, 'service': -14.0,
                'forecast': -8.0, 'reported_action': 12.0}
GENRE_LABEL = {'promotion': '促销', 'review': '评测或评论', 'service': '实用指南',
               'forecast': '预测性表述', 'reported_action': '具体行动表述'}
CATEGORY_BONUS = 4.0
COUNTRY_BONUS = 2.0
MAX_SAVED_MEMBERS = 24


def create_features(db):
    db.execute('CREATE TABLE shortlist_features (key TEXT PRIMARY KEY, beat TEXT, country TEXT, '
               'genre TEXT, points REAL, evidence TEXT)')


def features_row(key, coarse):
    genre = coarse.get('genre') or ''
    return (key, coarse.get('category') or '', coarse.get('mention_country') or '',
            genre, GENRE_POINTS.get(genre, 0), json.dumps(coarse, ensure_ascii=False, separators=(',', ':')))


def _order(db, use_classification):
    """Static quality and existing within-lane ordering; the two coverage bonuses are dynamic."""
    if use_classification:
        feature_join = 'LEFT JOIN shortlist_features f ON f.key = p.key'
        features = "COALESCE(f.beat, ''), COALESCE(f.country, ''), balanced + COALESCE(f.points, 0)"
    else:
        feature_join, features = '', "'', '', balanced"
    db.execute('DROP TABLE IF EXISTS shortlist_order')
    db.execute('''CREATE TABLE shortlist_order (
        beat TEXT, country TEXT, slot INTEGER, key TEXT, base REAL, tie INTEGER, lane_pos INTEGER,
        PRIMARY KEY(beat, country, slot)) WITHOUT ROWID''')
    db.execute(f'''INSERT INTO shortlist_order
        WITH scored(key, observed, lane, beat, country, quality) AS (
          SELECT p.key, p.observed, p.lane, {features}
          FROM progress p {feature_join}
          WHERE NOT EXISTS (SELECT 1 FROM prior WHERE prior.key=p.key)
        ), numbered AS (
          SELECT *, row_number() OVER (
            PARTITION BY lane ORDER BY quality DESC, observed DESC, key) AS lane_pos FROM scored
        ), valued AS (
          SELECT *, quality - 24.0 * (1.0 - 1.0/lane_pos) AS base FROM numbered
        ), ordered AS (
          SELECT *, row_number() OVER (
            ORDER BY base DESC, quality DESC, observed DESC, key) AS tie FROM valued
        )
        SELECT beat, country, row_number() OVER (PARTITION BY beat,country ORDER BY tie),
               key,base,tie,lane_pos FROM ordered''')


def _contribution(value, used, weight):
    return weight / (1 + used[value]) if value else 0.0


def _coverage(beat, country, used_beats, used_countries):
    return (_contribution(beat, used_beats, CATEGORY_BONUS)
            + _contribution(country, used_countries, COUNTRY_BONUS))


def _priority(head, beats, countries):
    beat, country, _, _, base, tie, _ = head
    return -(base + _coverage(beat, country, beats, countries)), tie


def _describe(coarse, classification, beats, countries):
    notes = []
    if classification:
        if (genre := coarse.get('genre')) in GENRE_POINTS:
            notes.append(f"标题体裁线索「{GENRE_LABEL[genre]}」{GENRE_POINTS[genre]:+g} 分，匹配「{coarse.get('genre_basis') or '未保存'}」")
        if beat := coarse.get('category'):
            bonus = _contribution(beat, beats, CATEGORY_BONUS)
            notes.append(f'粗分类「{beat}」此前已选 {beats[beat]} 席，本席覆盖加分 {bonus:.2f}')
        if country := coarse.get('mention_country'):
            bonus = _contribution(country, countries, COUNTRY_BONUS)
            notes.append(f'报道主要提及 {country}，此前已选 {countries[country]} 席，本席覆盖加分 {bonus:.2f}')
    return notes


def _attach(representative, card, reason, on_merge):
    explanation = representative['shortlist_trial']
    explanation['merged_count'] += 1
    member = {field: card[field] for field in ('key', 'title', 'source_url', 'source_kind',
              'source_key', 'excerpt', 'observed_at', 'source_clock', 'sources') if field in card}
    member['reason'] = reason
    if on_merge is not None:
        on_merge(dict(variant=explanation['variant'], representative_key=representative['key'], member=member))
    if len(explanation['merged_members']) >= MAX_SAVED_MEMBERS:
        explanation['merged_members_truncated'] = True
        return
    explanation['merged_members'].append(member)


def select_trial(db, day_stats, prior_keys, *, variant, limit, on_merge=None):
    if type(limit) is not int or limit < 0:
        raise ValueError('limit must be a nonnegative integer')
    if variant not in VARIANTS:
        raise ValueError(f'unknown shortlist trial variant {variant!r}')
    classification = variant in ('classification', 'combined')
    db.execute('DELETE FROM prior')
    db.executemany('INSERT OR IGNORE INTO prior VALUES (?)', ((key,) for key in prior_keys))
    _order(db, classification)
    eligible = db.execute('SELECT count(*) FROM shortlist_order').fetchone()[0]
    index = None
    if variant in ('merge', 'combined'):
        from .action_groups import ActionSeatIndex
        index = ActionSeatIndex()

    selected = {}
    beats, countries = Counter(), Counter()
    heap = []
    for head in db.execute('SELECT * FROM shortlist_order WHERE slot=1'):
        heappush(heap, (*_priority(head, beats, countries), head))
    examined = 0
    while heap and len(selected) < limit:
        old_score, tie, head = heappop(heap)
        score, _ = _priority(head, beats, countries)
        # A coverage bonus can only decrease. Unrefreshed heads remain upper bounds.
        if score > old_score and heap and (score, tie) > heap[0][:2]:
            heappush(heap, (score, tie, head))
            continue
        beat, country, slot, key, _, _, lane_position = head
        encoded, saved = db.execute('''SELECT p.card, f.evidence FROM progress p
            LEFT JOIN shortlist_features f ON f.key=p.key WHERE p.key=?''', (key,)).fetchone()
        card = json.loads(encoded)
        coarse = json.loads(saved) if saved else {}
        examined += 1
        match = index.find(card) if index is not None else None
        if match:
            representative, reason = match
            _attach(selected[representative], card, reason, on_merge)
        else:
            notes = _describe(coarse, classification, beats, countries)
            card.update(score=round(-score, 3), reasons=[*selection_reasons(card, 'balanced', lane_position), *notes],
                        shortlist_trial=dict(variant=variant, coarse=coarse if classification else {},
                                            adjustments=notes, merged_count=0, merged_members=[],
                                            merged_members_truncated=False))
            selected[key] = card
            if index is not None:
                index.add(card)
            if beat:
                beats[beat] += 1
            if country:
                countries[country] += 1
        next_head = db.execute('SELECT * FROM shortlist_order WHERE beat=? AND country=? AND slot=?',
                               (beat, country, slot + 1)).fetchone()
        if next_head:
            heappush(heap, (*_priority(next_head, beats, countries), next_head))

    stats = dict(day_stats, mode='shortlist_trial', variant=variant,
                 prior_excluded=day_stats['groups'] - eligible, eligible=eligible,
                 candidates_examined=examined, selected=len(selected), merged_count=examined - len(selected),
                 unexamined=eligible - examined, max_saved_members_per_seat=MAX_SAVED_MEMBERS,
                 coverage_scope='逐席选择，计数限于本次实际比较的候选组')
    return dict(items=list(selected.values()), stats=stats)

"""Attach sourced reading groups after selection without changing how a card ranked."""
from collections import defaultdict
import zlib

from ..collect.providers.gkg import FIPS
from ..desk.places import countries
from ..intake.lake import Lake, SUMMARY_COLUMNS
from .handoff import gkg_ref


# Exact GKG themes. Generic person/occupation/language tags do not identify a beat.
THEMES = {
    '外交与冲突': ('ARMEDCONFLICT', 'MILITARY', 'WB_2473_DIPLOMACY_AND_NEGOTIATIONS',
                'SOC_DIPLOMRECOG', 'TERROR', 'PEACEKEEPING'),
    '政治与治理': ('ELECTION', 'LEGISLATION', 'IMPEACHMENT', 'CONSTITUTIONAL',
                'PROTEST', 'DEMOCRACY', 'DEMOCRATIZATION', 'EPU_POLICY_REGULATION'),
    '经济与贸易': ('ECON_INFLATION', 'ECON_STOCKMARKET', 'ECON_TAXATION', 'ECON_FREETRADE',
                'ECON_TRADE_DISPUTE', 'ECON_FOREIGNINVEST', 'ECON_INTEREST_RATES',
                'ECON_HOUSING_PRICES', 'ECON_DEBT', 'ECON_IPO', 'ECON_COST_OF_LIVING',
                'WB_698_TRADE', 'EPU_CATS_TRADE_POLICY', 'EPU_CATS_MONETARY_POLICY',
                'EPU_CATS_FISCAL_POLICY', 'EPU_POLICY_BUDGET'),
    '健康与医疗': ('MEDICAL', 'GENERAL_HEALTH', 'UNGP_HEALTHCARE', 'WB_1406_DISEASES',
                'WB_1350_PHARMACEUTICALS', 'WB_2165_HEALTH_EMERGENCIES'),
    '公共安全与司法': ('ARREST', 'TRIAL', 'SOC_GENERALCRIME', 'WB_1014_CRIMINAL_JUSTICE'),
    '环境与灾害': ('NATURAL_DISASTER', 'ENV_CLIMATECHANGE', 'WB_567_CLIMATE_CHANGE',
                'ENV_DEFORESTATION', 'ENV_SPECIESENDANGERED', 'UNGP_CLIMATE_CHANGE_ACTION'),
    '教育与科技': ('EDUCATION', 'SCIENCE', 'SOC_TECHNOLOGYSECTOR',
               'WB_376_INNOVATION_TECHNOLOGY_AND_ENTREPRENEURSHIP'),
    '能源与农业': ('AGRICULTURE', 'ENV_OIL', 'ENV_NATURALGAS', 'ENV_COAL', 'ENV_WINDPOWER',
               'ENV_SOLAR', 'WB_507_ENERGY_AND_EXTRACTIVES', 'WB_525_RENEWABLE_ENERGY'),
}
THEME_CATEGORY = {term: label for label, terms in THEMES.items() for term in terms}


def _iso(value):
    iso = value.strip().lower() if isinstance(value, str) else ''
    return iso.upper() if iso in countries() else ''


def _terms(metadata, field):
    values = metadata.get(field)
    return (term for term in values if isinstance(term, str)) if isinstance(values, list) else ()


def _enhanced(metadata, field):
    value = metadata.get(field)
    return value.split(';') if isinstance(value, str) else ()


def _place_group(metadata):
    observed = set()
    for value in _terms(metadata, 'mention_iso'):
        if iso := _iso(value):
            observed.add(iso)
    offsets = defaultdict(set)
    for mention in _enhanced(metadata, 'enhanced_locations'):
        fields = mention.split('#')
        if len(fields) != 9:
            continue
        iso = _iso(FIPS.get(fields[2].upper()))
        if not iso:
            continue
        observed.add(iso)
        if fields[-1].isascii() and fields[-1].isdecimal() and len(fields[-1]) <= 12:
            offsets[iso].add(int(fields[-1]))
    if not observed:
        return {}

    def priority(iso):
        positions = offsets[iso]
        return -len(positions), min(positions, default=float('inf'))

    ordered = sorted(observed, key=lambda code: (priority(code), code))
    # Legacy/unordered locations can support a country set without a lead country.
    primary = ordered[0] if len(ordered) == 1 or priority(ordered[0]) < priority(ordered[1]) else ''
    return dict(countries=ordered, primary=primary, basis='gkg_mentions')


def _category(metadata):
    offsets = defaultdict(set)
    terms = {t for t in _terms(metadata, 'themes') if t in THEME_CATEGORY}
    for pair in _enhanced(metadata, 'enhanced_themes'):
        theme, sep, pos = pair.rpartition(',')
        if sep and theme in THEME_CATEGORY and pos.isascii() and pos.isdecimal() and len(pos) <= 12:
            terms.add(theme)
            offsets[theme].add(int(pos))
    if not terms:
        return {}
    beat_positions = defaultdict(set)
    for term in terms:
        beat_positions[THEME_CATEGORY[term]].update(offsets[term])

    def priority(values):
        return -len(values), min(values, default=float('inf'))

    labels = sorted(beat_positions, key=lambda label: (priority(beat_positions[label]), label))
    if len(labels) > 1 and priority(beat_positions[labels[0]]) == priority(beat_positions[labels[1]]):
        return {}
    label = labels[0]
    chosen = min((t for t in terms if THEME_CATEGORY[t] == label), key=lambda t: (priority(offsets[t]), t))
    return dict(label=label, basis='gkg_theme', theme=chosen)


def _read_batch(lake, keys):
    """At most one lookup per table for this chunk; decompress after releasing the lake lock."""
    placeholders = ','.join(['(?,?,?)'] * len(keys))
    stamps = [stamp for _, stamp, _ in keys]
    params = [min(stamps), max(stamps), *(part for key in keys for part in key)]
    rows = []
    with lake.connection(read_only=True) as db:
        if db is None:
            return {}
        tables = lake.tables(db)
        # Request only metadata from legacy snapshots; raw_line can be very large.
        projections = {
            'intake_records_v2': SUMMARY_COLUMNS + ',payload,raw_bytes,raw_sha256,codec,parser_version,metadata_overlay_json',
            'intake_records': 'feed,stamp,row_index,url,valid,metadata_json',
        }
        for table, columns in projections.items():
            if table not in tables:
                continue
            query = (f'SELECT {columns} FROM {table} WHERE stamp BETWEEN ? AND ? '
                     f'AND (feed,stamp,row_index) IN (VALUES {placeholders})')
            result = db.execute(query, params)
            names = [c[0] for c in result.description]
            rows.extend(dict(zip(names, r)) for r in result.fetchall())
    material = {}
    for row in rows:
        key = row['feed'], row['stamp'], row['row_index']
        if key in material:
            continue
        material[key] = {}
        if not row['valid'] or ('payload' in row and row['payload'] is None):
            continue
        try:
            record = lake.material(row)
            if isinstance(record.get('metadata'), dict):
                material[key] = record
        except (ValueError, TypeError, KeyError, AttributeError, zlib.error):
            # A malformed optional document must not displace the selected headline.
            continue
    return material


def enrich_selected(items, *, storage_root):
    """Enrich newly generated cards from their exact article; frozen reads need no lake."""
    by_key = defaultdict(list)
    for card in items:
        source_key = card.get('source_key') or ''
        if card.get('source_kind') == 'gdelt':
            if iso := _iso(card.get('country')):
                card['geography'] = dict(countries=[iso], primary=iso, basis='official_action', source_key=source_key)
        ref = gkg_ref(source_key)
        if ref and card.get('source_url'):
            by_key[(ref['feed'], ref['stamp'], ref['row_index'])].append(card)

    if storage_root is None or not by_key:
        return
    keys = sorted(by_key, key=lambda key: (key[1], key[0], key[2]))
    lake = Lake(storage_root)
    for start in range(0, len(keys), 24):
        part = keys[start:start + 24]
        for key, record in _read_batch(lake, part).items():
            if not record:
                continue
            geo = _place_group(record['metadata'])
            category = _category(record['metadata'])
            for card in by_key[key]:
                if record['url'] != card['source_url']:
                    continue
                if geo:
                    card['geography'] = dict(geo, source_key=card['source_key'])
                if category:
                    card['category'] = dict(category, source_key=card['source_key'])

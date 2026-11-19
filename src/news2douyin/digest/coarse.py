"""Source-scoped GKG hints for the optional offline shortlist trial.

Country and entity fields describe mentions in an article. They cannot establish
the event location or its participants. Genre cues describe the headline wording.
"""
import json
import re
import zlib

from ..collect.providers.gkg import FIPS
from ..intake.lake import PARSER_VERSION, unpack_payload
from .geography import _category, _place_group


# Bound the per-page memory held after releasing the DuckDB intake lock. Unknown
# or oversized material can still compete on its original headline and evidence.
MAX_ROW_BYTES = 256 * 1024
MAX_OVERLAY_CHARS = 32 * 1024
PAGE_SIZE = 256

_GENRES = [
    ('promotion', re.compile(
        r'^(?:sponsored|advertorial|广告推广|商业推广)\s*[:：]|\b(?:coupon code|promo code)\b|'
        r'\b(?:black friday|cyber monday|early access)\b.{0,90}(?:[$£€]\s*[\d,.]+\s*off|\d+%\s*off)|'
        r'\b(?:shoppers?|shopping|skirt|dress|headphones|handbag|smartwatch)\b.{0,80}'
        r'\b(?:drops? to|now only|slashed to|on sale for)\s*[$£€]\s*\d+', re.I)),
    ('review', re.compile(
        r'^(?:(?:film|movie|book|album|game|TV|theatre)\s+review|影评|剧评|书评)\s*[:：]|'
        r'\b(?:film|movie|series)\b.{0,100}\brotten tomatoes\s+(?:rating|score)\b|'
        r'\bspoilers?\b.{0,55}\b(?:elimination|episode|leaked)\b|\b(?:film|movie|book)\s+review\s*:', re.I)),
    ('service', re.compile(
        r'^(?:how to (?:watch|stream|buy|book|grow|cook|clean|choose)|where to (?:watch|stream|shop)|'
        r'\d+ (?:best (?:gifts?|deals?|picks?)|signs (?:you|that you)))\b|'
        r'\b(?:gardeners?|homeowners?)\s+(?:are )?(?:urged|advised) to (?:spray|prune|plant|trim)\b|'
        r'^(?:购物攻略|观赛指南|种植指南)\s*[:：]', re.I)),
    ('forecast', re.compile(
        r"\b(?:may|might|could) (?:cause|delay|disrupt|push|raise|trigger|force|hit|face|lead to)\b|"
        r'或将导致|可能引发|预计将推迟', re.I)),
]

# Only paired actions/objects get the 'reported_action' label. The guard
# avoids converting a plan, negation, question, quoted assertion or retrospective
# into a report that the action happened. This remains a lead to check.
_GUARD = re.compile(
    r"\b(?:not|never|could|may|might|would|will|plans?|planned|proposes?|proposed|proposal|"
    r"expects?|expected|scheduled|hopes?|intends?|set to|prepares?|preparing|should|likely|"
    r"aims?|anniversary|last year|last month|years? ago|recalls?|remembering|how to|claims?|"
    r"accuses?|denies|denied|rumou?rs?|says?|said|allegedly|reportedly|if|whether|urges?|calls? for)\b|"
    r"n['’]t\b|[?？“”\"]|可能|有望|拟议|计划|预计|尚未|否认|去年|周年|呼吁|传闻", re.I)
_ACTION = re.compile(
    r'\b(?:signs?|signed|ratifies|ratified)\s+(?:a |an |the )?(?:(?:[\w-]+)\s+){0,3}'
    r'(?:agreement|pact|treaty|accord|bill into law)\b|'
    r'\b(?:parliament|senate|assembly|cabinet|congress|council)\s+(?:has |formally )?'
    r'(?:approved?|approves|passed?|passes|adopts?|adopted|ratifies|ratified)\b|'
    r'\b(?:summit|negotiations|peace talks|ministerial meeting)\s+'
    r'(?:open(?:s|ed)?|begin(?:s)?|began|resume[ds]?|kick(?:s|ed)? off)\b|'
    r'\b(?:open(?:s|ed)?|convene[ds]?|resum(?:e[ds]?))\s+'
    r'(?:the |a |an )?(?:annual |regional |bilateral |peace )?(?:summit|talks|negotiations)\b|'
    r'\b(?:rail|train|flight|ferry|water|power|internet)\s+services?\s+(?:resume[ds]?|restored)\b|'
    r'\b(?:lifts?|lifted|ends?|ended)\s+(?:the |a |an )?(?:national |nationwide )?'
    r'(?:evacuation(?: order)?|ground stop|ban|curfew|embargo)\b|'
    r'\b(?:evacuation order|ground stop|curfew)\s+(?:(?:has been|is|was) )?lifted\b|'
    r'峰会(?:开幕|召开)|签署(?:合作)?(?:协议|条约)|(?:议会|内阁)表决通过|正式恢复供(?:电|水)', re.I)


def headline_genre(title):
    for label, pattern in _GENRES:
        if match := pattern.search(title):
            return {'genre': label, 'genre_basis': match.group()[:160]}
    if not _GUARD.search(title) and (match := _ACTION.search(title)):
        return {'genre': 'reported_action', 'genre_basis': match.group()[:160]}
    return {}


def projection(table):
    if table == 'intake_records':
        return f', CASE WHEN valid AND length(metadata_json)<={MAX_ROW_BYTES} THEN metadata_json END'
    return (f', CASE WHEN valid AND raw_bytes BETWEEN 1 AND {MAX_ROW_BYTES} AND '
            f'octet_length(payload)<={MAX_ROW_BYTES} AND parser_version=\'{PARSER_VERSION}\' AND '
            f'coalesce(length(metadata_overlay_json),0)<={MAX_OVERLAY_CHARS} THEN payload END, '
            'codec, raw_sha256, raw_bytes, '
            f'CASE WHEN length(metadata_overlay_json)<={MAX_OVERLAY_CHARS} THEN metadata_overlay_json END')


def _metadata(columns):
    # Archives migrated from the old table store JSON. In v2, verify the raw
    # bytes using the normal decoder but read only the narrow GKG fields used
    # here, avoiding extraction of XML, GCAM and other large unused properties.
    if columns[0] is None:
        return None
    if len(columns) == 1:
        return json.loads(columns[0])
    payload, codec, sha, size, overlay = columns
    raw = unpack_payload(dict(payload=payload, codec=codec, raw_sha256=sha, raw_bytes=size))
    if raw.count(b'\t') != 26:
        return None
    fields = raw.split(b'\t', 15)

    def terms(index):
        return list(dict.fromkeys(v.strip() for v in fields[index].decode('utf-8').split(';') if v.strip()))

    countries = set()
    for value in terms(9):
        parts = value.split('#')
        if len(parts) > 2 and (iso := FIPS.get(parts[2].upper())):
            countries.add(iso)
    result = dict(themes=terms(7), enhanced_themes=fields[8].decode('utf-8'), mention_iso=sorted(countries),
                  enhanced_locations=fields[10].decode('utf-8'), persons=terms(11), orgs=terms(13))
    if overlay and overlay != '{}':
        delta = json.loads(overlay)
        for key in delta.get('remove', []):
            result.pop(key, None)
        result.update(delta.get('set', {}))
    return result


def classify(title, columns, counts):
    hints = headline_genre(title)
    if hints:
        counts['gkg_coarse_genre'] += 1
    try:
        metadata = _metadata(columns)
    except (ValueError, TypeError, KeyError, AttributeError, zlib.error, RecursionError):
        metadata = None
    if not isinstance(metadata, dict):
        counts['gkg_coarse_metadata_unavailable'] += 1
        return hints or None
    counts['gkg_coarse_metadata_read'] += 1
    if category := _category(metadata):
        hints.update(category=category['label'], theme=category['theme'])
        counts['gkg_coarse_category'] += 1
    country = _place_group(metadata).get('primary')
    if country:
        hints['mention_country'] = country
        counts['gkg_coarse_mention_country'] += 1
    # Only source-supplied GKG names; no role, alias, or entity resolution is implied.
    names = []
    for field in ('persons', 'orgs'):
        values = metadata.get(field)
        if isinstance(values, list):
            names.extend(name for name in values if isinstance(name, str) and 1 < len(name) <= 120)
    if names:
        hints['entities'] = list(dict.fromkeys(names))[:8]
    return hints or None

"""Identify event leads and ongoing subjects from bounded, dated source reports."""
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from hashlib import sha256
from itertools import islice
import re
import unicodedata
from urllib.parse import urlsplit

from ..dedup.normalize import canonicalize_url
from ..digest.publisher_families import publisher_unit
from ..selection.source_content import source_url, text, usable_title
from ..server.countries import directory_context


CATEGORIES = {
    'conflict': '战争与冲突', 'diplomacy': '重要会晤',
    'disaster': '自然灾害', 'disruption': '大范围影响',
}
MAX_OBSERVATIONS = 12_000
MAX_SOURCES = 8


def _patterns(entries):
    return [(value, label, re.compile(phrases, re.I | re.ASCII)) for value, label, phrases in entries]


_DISASTERS = _patterns((
    ('earthquake', '地震', r'地震|强震|余震|\b(?:earthquakes?|aftershocks?|temblor|terremoto|sismo|séisme|erdbeben)\b'),
    ('tsunami', '海啸', r'海啸|\btsunamis?\b'),
    ('flood', '洪灾', r'洪水|洪灾|洪涝|山洪|\b(?:floods?|flooding|inundaci[oó]n|inondations?|hochwasser)\b'),
    ('storm', '风暴', r'台风|飓风|强风暴|热带气旋|龙卷风|\b(?:typhoons?|hurricanes?|cyclones?|tornado(?:es|s)?)\b'),
    ('wildfire', '山火', r'山火|野火|林火|森林火灾|\b(?:wildfires?|forest fires?|incendios? forestales?)\b'),
    ('volcano', '火山喷发', r'火山喷发|火山爆发|\b(?:volcanic eruption|volcano erupts?)\b'),
    ('landslide', '山体滑坡', r'山体滑坡|泥石流|\b(?:landslides?|mudslides?)\b'),
))
_DISRUPTIONS = _patterns((
    ('power', '停电影响', r'停电|电力中断|电网瘫痪|\b(?:power (?:outages?|cuts?|failures?)|blackouts?|without power)\b'),
    ('network', '网络服务中断', r'断网|网络中断|网络故障|通信中断|系统中断|服务瘫痪|\b(?:internet|network|communications?|service|systems?|cloud)\s+(?:outages?|disruptions?|shutdowns?)\b'),
    ('transport', '交通中断', r'停航|停运|交通中断|铁路中断|航班取消|空域关闭|机场关闭|港口关闭|\b(?:flights? cancel(?:led|ed)|cancellations?|airspace clos(?:ed|ure)|airports? (?:close[ds]?|shutdowns?)|transport disruptions?|rail (?:outage|shutdown|disruption)|ports? (?:close[ds]?|closures?))\b'),
    ('supply', '供应中断', r'供应中断|供应链中断|大面积停水|断供|\b(?:(?:water|gas|fuel) (?:shortages?|cuts?|supply disruptions?)|supply chain disruptions?|supply disruptions?)\b'),
))
_MEETINGS = _patterns((
    ('apec', 'APEC', r'亚太经合组织|\bapec\b'), ('g20', '二十国集团（G20）', r'二十国集团|\bg\s*20\b'),
    ('g7', '七国集团（G7）', r'七国集团|\bg\s*7\b'), ('nato', '北约', r'北约|\bnato\b'),
    ('asean', '东盟', r'东南亚国家联盟|东盟|\basean\b'), ('brics', '金砖国家', r'金砖国家|\bbrics\b'),
    ('cop', '联合国气候大会', r'联合国气候大会|气候缔约方大会|\bcop\s*\d{2}\b|\bun climate (?:change )?(?:summit|conference)\b'),
    ('un', '联合国', r'联合国|\bunited nations\b|\bun (?:security council|general assembly|summit)\b|\bunga\b'),
    ('eu', '欧盟', r'欧盟|欧洲理事会|\beuropean (?:union|council)\b|\beu\b'),
    ('sco', '上海合作组织', r'上海合作组织|上合组织|\bshanghai cooperation organi[sz]ation\b|\bsco\b'),
))

_CONFLICT = re.compile(r'战争|战事|战况|交战|交火|空袭|炮击|轰炸|停火|武装冲突|军事冲突|军事打击|导弹袭击|空中打击|抵御.{0,6}入侵|入侵|\b(?:wars?|armed conflicts?|air\s?strikes?|shells?|shelling|missile strikes?|military strikes?|clashes|invasion|ceasefires?|hostilities|combat operations?|battlefield|combats? armés?|guerra|alto el fuego)\b', re.I)
_NONMILITARY = re.compile(r'贸易战|价格战|口水战|网络入侵|\b(?:trade wars?|war of words|price wars?|cyber intrusions?|war on (?:drugs|cancer|poverty))\b', re.I)
_MEETING = re.compile(r'峰会|首脑会议|领导人(?:会晤|会谈|会议)|高层会谈|联合国大会|安理会(?:会议|开会)|部长级(?:会议|会谈)|\b(?:summits?|high.level (?:talks|meetings)|leaders? (?:meet|meeting)|ministerial (?:meetings?|talks)|general assembly|security council meeting|presidents? (?:meet|talk)|climate conference)\b', re.I)
_HIGH_LEVEL = re.compile(r'主席|总理|首相|总统|首脑|外交部长|领导人|国防部长|外交大臣|\b(?:presidents?|prime ministers?|leaders?|heads of state|foreign ministers?|secretary of state)\b', re.I)
_HISTORICAL = re.compile(r'周年纪念|周年回顾|[0-9一二两三四五六七八九十]+周年|历史回顾|历史上的|战争史|史话|旧闻|演习|演练|电影上映|谣言|虚假消息|辟谣|\b(?:anniversary|retrospective|earthquake drills?|disaster drills?|simulation|revisited|historical overview|decades ago|film premiere|false reports?|hoax)\b', re.I)
_BROAD = re.compile(r'大面积|大范围|大规模|大批|数十万|上百万|数百万|全国|多地|多省|多城|多个地区|多个国家|数百(?:次|班)|\b(?:widespread|nationwide|countrywide|global|large.scale|massive|hundreds of thousands|millions|across (?:the |multiple |several )?(?:country|cities|regions|states|world)|statewide|affects? \d{4,})\b', re.I)
_SEVERE = re.compile(r'紧急撤离|紧急疏散|紧急避险|紧急状态|海啸警报|多人(?:死亡|遇难)|大规模(?:撤离|疏散)|严重人员伤亡|\b(?:state of emergency|tsunami warning|mass evacuation|thousands evacuated|dozens killed|hundreds injured)\b', re.I)
_MAGNITUDE = re.compile(r'(?:\b(?:magnitude|magnitud|m)\s*(\d(?:\.\d+)?)\b|(\d(?:\.\d+)?)\s*(?:级|[- ]magnitude\b))', re.I)

_PAIRS = {
    ('RU', 'UA'): ('俄乌战争', ('俄乌战争', '俄乌战事', '俄乌冲突', '乌克兰战争', '俄罗斯入侵乌克兰', 'Russia-Ukraine war', 'Russia’s war in Ukraine', 'Ukraine war', 'war in Ukraine'), r'俄乌|乌俄|乌克兰战争|\bukraine war\b|\bwar in ukraine\b'),
    ('IQ', 'IR'): ('伊朗与伊拉克冲突', ('两伊战争', '两伊冲突', '伊朗与伊拉克冲突', '伊朗-伊拉克战争', 'Iran–Iraq war', 'Iran-Iraq war', 'Iran-Iraq conflict'), r'两伊战争|两伊冲突|\biran\s*[-–—]\s*iraq war\b'),
    ('IL', 'IR'): ('以色列与伊朗冲突', ('以伊冲突', '伊以冲突', '伊朗与以色列冲突', 'Israel-Iran conflict'), r'以伊冲突|伊以冲突|以伊交火|伊以交火'),
    ('IL', 'PS'): ('巴以冲突', ('巴以冲突', '以巴冲突', '加沙战争', 'Gaza war', 'Israel–Palestine conflict', 'Israel-Palestine conflict'), r'巴以|以巴|加沙战争|\bgaza war\b'),
    ('IN', 'PK'): ('印度与巴基斯坦冲突', ('印巴冲突', '印巴交火', '印度与巴基斯坦冲突', 'India-Pakistan conflict'), r'印巴冲突|印巴交火'),
}
_PAIR_PATTERNS = tuple((pair, re.compile(pattern, re.I)) for pair, (_, _, pattern) in _PAIRS.items())
_COUNTRY_ALIASES = {
    'US': ('美国', '美国的', '美方', 'United States', 'United States of America', 'U.S.', 'American'),
    'GB': ('英国', '英方', 'United Kingdom', 'Britain', 'British'), 'RU': ('俄罗斯', '俄方', '俄军', 'Russia', 'Russian'),
    'UA': ('乌克兰', '乌方', '乌军', 'Ukraine', 'Ukrainian'), 'JP': ('日本', '日方', 'Japan', 'Japanese'),
    'CN': ('中国', '中方', 'China', 'Chinese'), 'KR': ('韩国', '韩方', 'South Korea', 'South Korean'),
    'KP': ('朝鲜', '朝方', 'North Korea', 'North Korean'), 'IQ': ('伊拉克', 'Iraq', 'Iraqi'),
    'IR': ('伊朗', 'Iran', 'Iranian'), 'IL': ('以色列', '以方', 'Israel', 'Israeli'),
    'TR': ('土耳其', 'Türkiye', 'Turkish', 'Turkey'), 'PS': ('巴勒斯坦', '加沙', 'Palestine', 'Palestinian', 'Gaza'),
    'TW': ('台湾', '臺灣', 'Taiwan'), 'MM': ('缅甸', 'Myanmar', 'Burmese'),
    'IN': ('印度', '印方', 'India', 'Indian'), 'PK': ('巴基斯坦', '巴方', 'Pakistan', 'Pakistani'),
}
# Country capitals are loaded from the geographic directory. Regional names
# supplement it where a country alone would be too coarse for a local event.
_REGIONS = (
    ('JP', '北海道', 'Hokkaido'), ('JP', '石川县', 'Ishikawa'), ('JP', '能登半岛', 'Noto Peninsula'),
    ('JP', '千叶', 'Chiba'), ('JP', '冲绳', 'Okinawa'), ('JP', '青森', 'Aomori'),
    ('US', '加利福尼亚', 'California'), ('US', '阿拉斯加', 'Alaska'), ('US', '得克萨斯', 'Texas'),
    ('TW', '花莲', 'Hualien'), ('TW', '台东', 'Taitung'), ('HK', '香港', 'Hong Kong'),
    ('CN', '四川', 'Sichuan'), ('CN', '云南', 'Yunnan'), ('CN', '西藏', 'Tibet'),
    ('CN', '新疆', 'Xinjiang'), ('CN', '甘肃', 'Gansu'), ('CN', '青海', 'Qinghai'),
    ('ID', '苏门答腊', 'Sumatra'), ('ID', '爪哇', 'Java'), ('ID', '巴厘岛', 'Bali'),
    ('TR', '加济安泰普', 'Gaziantep'), ('TR', '哈塔伊', 'Hatay'), ('GR', '克里特岛', 'Crete'),
    ('ES', '马德里', 'Madrid'), ('ES', '加泰罗尼亚', 'Catalonia'), ('ES', '巴伦西亚', 'Valencia'),
    ('PS', '加沙', 'Gaza'), ('MY', '沙巴', 'Sabah'), ('NZ', '南岛', 'South Island'),
)
_AMBIGUOUS_NAMES = {'georgia', 'chad', 'jordan', 'jersey', 'turkey', 'congo', 'virgin islands', 'sao tome'}


def _flat(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def _literal(alias):
    escaped = re.escape(_flat(alias))
    return escaped if re.search(r'[\u3400-\u9fff]', alias) else rf'(?<!\w){escaped}(?!\w)'


def _lexicon(aliases):
    names = {key: value for key, value in sorted(aliases.items(), key=lambda pair: -len(pair[0])) if key}
    return re.compile('|'.join(_literal(key) for key in names)), names


@lru_cache(maxsize=1)
def _geography():
    country_rows = directory_context()['countries']
    countries = {row['a2']: (row['zh'], row['en']) for row in country_rows}
    country_aliases = {}
    places = {}
    for row in country_rows:
        code = row['a2']
        for value in [row['zh'], row['en'], *_COUNTRY_ALIASES.get(code, ())]:
            key = _flat(value)
            if key not in _AMBIGUOUS_NAMES:
                country_aliases[key] = code
        for zh, en, *_ in row['capitals']:
            if not zh or not en or _flat(zh) == _flat(row['zh']) or _flat(en) == _flat(row['en']):
                continue
            for alias in (zh, en):
                places.setdefault(_flat(alias), (code, zh, en))
    for code, zh, en in _REGIONS:
        for alias in (zh, en):
            places[_flat(alias)] = (code, zh, en)
    return countries, _lexicon(country_aliases), _lexicon(places)


def _observed(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return None


def _iso(value):
    candidate = str(value or '').strip().upper()
    return candidate if candidate in _geography()[0] else ''


def _names(value, lexicon):
    pattern, labels = lexicon
    return list(dict.fromkeys(labels[match.group()] for match in pattern.finditer(value)))


def _scopes(row, title):
    country_names, country_lexicon, place_lexicon = _geography()
    countries = _names(title, country_lexicon)
    source_country = _iso(row.get('country'))
    raw_place = text(row.get('place'), 120).split(',')[0].strip()
    input_place = _names(_flat(raw_place), place_lexicon) if raw_place else []
    places = input_place or _names(title, place_lexicon)
    if source_country:
        places = [place for place in places if place[0] == source_country]
    if len(places) == 1:
        return places[0][0], places[0][1], places[0][2], countries
    # A country's summary label doesn't count as a specific action place.
    country = source_country or (countries[0] if len(countries) == 1 else '')
    if raw_place and country and _flat(raw_place) not in {_flat(name) for name in country_names[country]}:
        return country, raw_place, raw_place, countries
    return country, '', '', countries


@dataclass(frozen=True)
class _Signal:
    category: str
    kind: str
    subject: tuple[str, ...]
    name: str
    terms: tuple[str, ...]
    place: str = ''
    country: str = ''
    severe: bool = False
    magnitude: float = 0.0


def _pair(row, title, countries, *, war=False):
    if war:
        for pair, pattern in _PAIR_PATTERNS:
            if pattern.search(title):
                return pair
    participants = row.get('actor_countries') or ()
    if isinstance(participants, str):
        participants = [participants]
    captured = tuple(sorted({code for value in participants if (code := _iso(value))}))
    if len(captured) == 2:
        return captured
    return tuple(sorted(countries)) if len(countries) == 2 else ()


def _country_terms(pair, label):
    names = _geography()[0]
    (left, left_en), (right, right_en) = (names[code] for code in pair)
    suffix = '冲突' if label == 'conflict' else '会晤'
    english = 'conflict' if label == 'conflict' else 'summit'
    return f'{left}与{right}{suffix}', (f'{left}与{right}{suffix}', f'{right}与{left}{suffix}', f'{left_en}-{right_en} {english}', f'{right_en}-{left_en} {english}')


def _impact_signal(category, match, title, country, place, place_en):
    if match is None or not country:
        return None
    kind, label = match
    country_name, country_en = _geography()[0][country]
    scope = place or country_name
    label_en = {'power': 'power outage', 'network': 'network outage', 'transport': 'transport disruption',
                'supply': 'supply disruption'}.get(kind, kind)
    magnitude = 0.0
    if kind == 'earthquake' and (matched := _MAGNITUDE.search(title)):
        magnitude = float(matched[1] or matched[2])
    # A magnitude cue or a claimed impact only describes that report.
    severe = bool(magnitude >= 6 or _SEVERE.search(title) or category == 'disruption')
    return _Signal(category, kind, (country,), scope + label,
                   (scope + label, f'{place_en or country_en} {label_en}'), place, country, severe, magnitude)


def _incident(patterns, title):
    return next(((kind, label) for kind, label, pattern in patterns if pattern.search(title)), None)


def _classify(row, headline):
    title = _flat(headline)
    if _HISTORICAL.search(title):
        return None
    official = bool(row.get('official_only'))
    code = str(row.get('event_root_code') or row.get('event_code') or '')[:2]
    war = (code in {'18', '19', '20'} if official else bool(_CONFLICT.search(title) and not _NONMILITARY.search(title)))
    disaster = _incident(_DISASTERS, title) if not official else None
    meeting = not official and (_MEETING.search(title) or (_HIGH_LEVEL.search(title) and re.search(r'会晤|会谈|\b(?:hold talks|meet with)\b', title)))
    disruption = _incident(_DISRUPTIONS, title) if not official and _BROAD.search(title) else None
    if not (war or disaster or meeting or disruption):
        return None
    country, place, place_en, countries = _scopes(row, title)
    if disaster_signal := _impact_signal('disaster', disaster, title, country, place, place_en):
        return disaster_signal
    if war and (pair := _pair(row, title, countries, war=True)):
        name, terms = _PAIRS.get(pair)[:2] if pair in _PAIRS else _country_terms(pair, 'conflict')
        return _Signal('conflict', 'armed', pair, name, terms)
    if official:
        return None
    if meeting:
        for kind, label, pattern in _MEETINGS:
            if pattern.search(title):
                term = '' if kind in {'cop', 'un', 'eu', 'sco'} else kind.upper() + ' summit'
                name = label if kind == 'cop' else label + '重要会晤'
                terms = ((label + '峰会', label + '部长级会议', term) if kind != 'cop'
                         else (label, 'UN climate summit', 'UN climate change conference', 'COP climate summit'))
                return _Signal('diplomacy', kind, (kind,), name, terms)
        if pair := _pair(row, title, countries):
            name, terms = _country_terms(pair, 'diplomacy')
            return _Signal('diplomacy', 'bilateral', pair, name, terms)
    return _impact_signal('disruption', disruption, title, country, place, place_en)


@dataclass(frozen=True)
class _Observation:
    signal: _Signal
    at: datetime
    source: dict
    canonical: str
    domain: str
    event: str
    supporting: bool


@dataclass
class _Episode:
    rows: list = field(default_factory=list)
    places: set = field(default_factory=set)
    magnitudes: set = field(default_factory=set)
    events: set = field(default_factory=set)
    titles: set = field(default_factory=set)
    url_titles: set = field(default_factory=set)

    def add(self, row):
        self.rows.append(row)
        if row.signal.place:
            self.places.add(_flat(row.signal.place))
        if row.signal.magnitude:
            self.magnitudes.add(row.signal.magnitude)
        if row.event:
            self.events.add(row.event)
        title = _flat(row.source['title'])
        self.titles.add(title)
        self.url_titles.add((row.canonical, title))


def _observation(row, first, as_of):
    if not isinstance(row, Mapping):
        return None
    observed = _observed(row.get('observed_at'))
    published = _observed(row.get('published_at'))
    if not observed or not first <= observed <= as_of:
        return None
    if published is not None and not first <= published <= as_of:
        return None
    at = published or observed
    url = source_url(row.get('url') or row.get('source_url'))
    headline = text(row.get('title'), 500)
    if not url or '\\' in url or not usable_title(headline) or not (signal := _classify(row, headline)):
        return None
    canonical = canonicalize_url(url)
    domain = (urlsplit(canonical).hostname or '').removeprefix('www.').rstrip('.')
    event = text(row.get('event_key') or row.get('event_id'), 180)
    supporting = bool(row.get('official_only') or row.get('attention_only'))
    source = dict(title=headline, url=url, source_url=url, domain=domain,
                  source_kind=text(row.get('source_kind'), 50), source_key=text(row.get('source_key') or row.get('key'), 180),
                  event_key=event, excerpt=text(row.get('excerpt'), 1200), observed_at=observed.isoformat(),
                  published_at=published.isoformat() if published else '', official_only=bool(row.get('official_only')),
                  attention_only=bool(row.get('attention_only')), provider=text(row.get('provider'), 80),
                  language=text(row.get('language'), 40), local_event_key=text(row.get('local_event_key'), 160),
                  official_event_id=text(row.get('official_event_id'), 40), adapter=text(row.get('adapter'), 32),
                  clock_kind=text(row.get('clock_kind'), 32))
    return _Observation(signal, at, source, canonical, domain, event, supporting)


def _join(group, observation):
    own = observation.signal
    if own.category in {'conflict', 'diplomacy'}:
        return True
    first, recent = group.rows[0], group.rows[-1]
    place = _flat(own.place)
    if place and group.places and place not in group.places:
        return False
    title = _flat(observation.source['title'])
    if (observation.event in group.events or
            (observation.canonical, title) in group.url_titles):
        return True
    if observation.at - first.at > timedelta(hours=24) or observation.at - recent.at > timedelta(hours=12):
        return False
    if own.kind == 'earthquake':
        magnitudes = group.magnitudes
        if own.magnitude and magnitudes and any(abs(own.magnitude - prior) > 0.21 for prior in magnitudes):
            return False
        return bool(place in group.places and (own.magnitude or magnitudes) or
                    own.magnitude and magnitudes and observation.at - first.at <= timedelta(hours=10) or
                    title in group.titles)
    if own.category == 'disruption':
        return True
    return place in group.places or title in group.titles


def _focus_terms(signal, primary):
    terms = [*signal.terms, primary.source['title']]
    return list(dict.fromkeys(value for raw in terms if 2 <= len(value := text(raw, 160)) <= 160))[:12]


def _build(group, recent_start, recent_hours, previous_hours):
    def evidence_priority(row):
        return (row.supporting, row.source['clock_kind'] == 'edition_group', row.at,
                not bool(row.source['excerpt']), row.source['source_key'])

    sources = {}
    for row in group:
        current = sources.get(row.canonical)
        if not current or evidence_priority(row) < evidence_priority(current):
            sources[row.canonical] = row
    usable = [row for row in sources.values() if not row.supporting]
    active = [row for row in usable if row.at >= recent_start]
    if not active:
        return None
    recent_domains = {row.domain for row in active}
    units = {publisher_unit(domain)[0] for domain in recent_domains} - {''}
    previous_reports = len(usable) - len(active)
    severe = any(row.signal.severe for row in active)
    if len(units) < 2 and not severe:
        return None
    confidence = 'multi_source' if len(units) >= 2 else 'single_source'
    status = 'check'
    status_label = '单源待核'
    if confidence == 'multi_source':
        if len(active) >= 3 and len(active) / recent_hours >= 2 * max(1, previous_reports) / previous_hours:
            status, status_label = 'rising', '报道升温'
        elif previous_reports:
            status, status_label = 'ongoing', '持续关注'
        else:
            status, status_label = 'new', '近期出现'
    ranked = sorted(active, key=lambda row: (row.at, row.signal.severe, bool(row.source['excerpt']), row.canonical), reverse=True)
    primary = ranked[0]
    # For mixed descriptions of one disaster, show the most specific place.
    anchor = next((row for row in ranked if row.signal.place), primary)
    signal = anchor.signal
    selected, seen_domains = [primary.source], {primary.domain}
    for row in ranked[1:]:
        if row.domain not in seen_domains and len(selected) < MAX_SOURCES:
            selected.append(row.source)
            seen_domains.add(row.domain)
    selected_keys = {source['url'] for source in selected}
    supplemental = sorted((row for row in sources.values() if row.source['url'] not in selected_keys),
                          key=lambda row: (row.supporting, -row.at.timestamp(), row.canonical))
    selected.extend(row.source for row in supplemental[: MAX_SOURCES - len(selected)])

    identity = (signal.category, signal.kind, *signal.subject)
    if signal.category in {'disaster', 'disruption'}:
        identity += (_flat(signal.place), group[0].at.date().isoformat(), min(row.canonical for row in group))
    key = 'hot_' + sha256('\0'.join(identity).encode()).hexdigest()[:20]
    summary = (f'最近 {recent_hours} 小时找到 {len(active)} 篇报道，来自 {len(recent_domains)} 个域名。'
               f'此前 {previous_hours} 小时找到 {previous_reports} 篇。')
    reasons = [f'报道标题出现{CATEGORIES[signal.category]}线索', f'按地名或事件主体归并，域名可能刊发转载']
    if severe:
        reasons.append('标题报告较大震级、紧急行动或广泛影响，细节需查原文')
    if confidence == 'single_source':
        reasons.append('来源覆盖仍有限，待进一步核对')
    if any(row.source['official_only'] for row in sources.values()):
        reasons.append('补充 GDELT 事件编码线索')
    if any(row.source['attention_only'] for row in sources.values()):
        reasons.append('补充中文平台关注线索')
    return dict(id=key, title=signal.name, category=signal.category, label=CATEGORIES[signal.category],
                status=status, status_label=status_label, confidence=confidence, summary=summary, reasons=reasons,
                recent_reports=len(active), previous_reports=previous_reports, domains=len(recent_domains),
                first_seen=min(row.at for row in usable).isoformat(), last_seen=primary.at.isoformat(), sources=selected,
                location=signal.place or (_geography()[0][signal.country][0] if signal.country else ''),
                focus_terms=_focus_terms(signal, primary),
                focus_description=f'持续跟踪{signal.name}的已报道进展、实际影响和后续回应，核对传播中的具体出处。',
                severity=severe)


def detect_hotspots(observations, *, as_of, limit=24, per_category=None, recent_hours=36, previous_hours=36):
    """Return ranked hotspot briefs from a fixed and explicitly bounded time window."""
    as_of = _observed(as_of)
    if as_of is None:
        raise ValueError('as_of must include a timezone')
    if not all(isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 168 for value in (recent_hours, previous_hours)):
        raise ValueError('hotspot windows must each be between 1 and 168 hours')
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError('hotspot limit must be between 1 and 100')
    if per_category is not None and (type(per_category) is not int or not 1 <= per_category <= 100):
        raise ValueError('hotspot per-category limit must be between 1 and 100')
    recent_start = as_of - timedelta(hours=recent_hours)
    first = recent_start - timedelta(hours=previous_hours)
    records = [observation for row in islice(observations, MAX_OBSERVATIONS)
               if (observation := _observation(row, first, as_of))]
    records.sort(key=lambda row: (row.at, row.supporting, row.canonical, row.source['source_key'], row.source['title']))
    grouped = defaultdict(list)
    for row in records:
        signal = row.signal
        candidates = grouped[(signal.category, signal.kind, signal.subject)]
        for group in reversed(candidates[-32:]):
            if _join(group, row):
                group.add(row)
                break
        else:
            new_group = _Episode()
            new_group.add(row)
            candidates.append(new_group)

    topics = [topic for candidates in grouped.values() for group in candidates
              if (topic := _build(group.rows, recent_start, recent_hours, previous_hours))]
    topics.sort(key=lambda row: (row['confidence'] == 'multi_source', row['status'] == 'rising', row['severity'],
                                 min(row['domains'], 8), min(row['recent_reports'], 12), row['last_seen'], row['id']), reverse=True)
    if per_category is None:
        return topics[:limit]
    counts = Counter()
    limited = []
    for topic in topics:
        category = topic['category']
        if counts[category] < per_category:
            limited.append(topic)
            counts[category] += 1
            if len(limited) == limit:
                break
    return limited

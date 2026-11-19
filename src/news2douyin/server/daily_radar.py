"""Read a published Beijing-calendar edition; never run a build in a request."""
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlsplit

from fastapi import HTTPException
from ..digest import service
from ..digest.handoff import can_select_card, card_token
from ..digest.why_follow import describe
from ..selection.source_content import source_url
from ..desk.places import countries
from ..translations.daily import daily_card_descriptor


def resolve_day(raw):
    latest = service.default_day()
    if raw is None:
        return latest, latest
    try:
        day = date.fromisoformat(raw)
        if day.isoformat() != raw or day > latest:
            raise ValueError
    except ValueError:
        raise HTTPException(422, '请选择今天或之前的北京时间日期，格式为 YYYY-MM-DD')
    return day, latest


def issue_data(session, raw_day, *, summary=False):
    day, _ = resolve_day(raw_day)
    if summary:
        return service.read_issue_state(session, day)
    return service.read_issue(session, day)


def _record_url(source_key, day):
    source, _, key = source_key.partition(':')
    if not key:
        return ''
    if source == 'gkg':
        from ..digest.handoff import gkg_ref
        ref = gkg_ref(source_key)
        if ref:
            return '/api/intake/records/{feed}/{stamp}/{row_index}'.format(**ref)
    if source == 'gdelt' and key.isdigit():
        return '/radar?' + urlencode({'source': 'gdelt', 'event': key, 'evidence_day': day})
    if source == 'local':
        return '/events/' + quote(key, safe='') + '?' + urlencode({'back': '/radar?day=' + day})
    if source == 'newsnow':
        return '/radar?' + urlencode({'source': 'attention', 'attention_topic': key})
    return ''


def _title_label(card):
    origin = card.get('reconstruction') or {}
    if origin and origin.get('title_origin') != 'publisher':
        return '重建片段'
    if card.get('source_kind') == 'newsnow':
        return '平台条目标题'
    return '来源标题' if card.get('source_url') else '归档标题 · 未保存标题来源'


def _publisher_details(card):
    breadth = card.get('publisher_breadth')
    if not isinstance(breadth, dict):
        return None
    counts = (card.get('domain_count'), breadth.get('units'))
    if any(type(value) is not int or value < 0 for value in counts):
        return None
    return breadth


def _focus_reason(reason):
    if reason == '已关联关注专题':
        return '已关联焦点'
    if isinstance(reason, str):
        for old, label in (('标题与专题范围匹配（待核）：', '命中焦点范围（待核）：'),
                           ('专题相关：', '焦点相关：')):
            if reason.startswith(old):
                return label + reason[len(old):]
    return reason


def _focus_evidence_label(match):
    if match['kind'] == 'membership':
        return '纲要问题「' + match['question'] + '」' if match['question'] else '已收录'
    if match['terms']:
        return '标题命中「' + '、'.join(match['terms']) + '」 · 待核'
    return '命中焦点范围 · 待核'


GROUP_OPTIONS = {'none': '不分组', 'country': '按国家或地区', 'category': '按分类', 'source': '按数据来源'}
SORT_OPTIONS = {'recommended': '推荐顺序', 'recent': '最近观测优先'}
SOURCE_LABELS = {'gdelt': 'GDELT Events', 'gkg': 'GDELT GKG', 'local': '已整理事件', 'newsnow': 'NewsNow',
                 'collected': '采集报道', 'report': '采集报道'}


def _geography(card, names):
    geo = card.get('geography') or {}
    # GKG describes places mentioned in a document. Do not infer event location
    # from its publisher, domain or language.
    codes = [str(code).lower() for code in (geo.get('countries') or []) if str(code).lower() in names]
    primary = str(geo.get('primary') or '').lower()
    if not geo:
        primary = str(card.get('country') or '').lower()
    if primary not in names:
        primary = ''
    if primary:
        codes = [primary, *[code for code in codes if code != primary]]
    labels = list(dict.fromkeys(names[code] for code in codes))
    group = names[primary] if primary else ('多国或多地区' if len(labels) > 1 else next(iter(labels), ''))
    basis = {'gkg_mentions': '报道提及', 'official_action': '行动地', 'local_place': '事件地点'}.get(geo.get('basis'), '')
    return group, (' · '.join(filter(None, (basis, '、'.join(labels)))) if labels else '')


def _observed_order(card):
    stamp = card.get('observed_at')
    if isinstance(stamp, str):
        try:
            moment = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def _reading_groups(cards, group_by, sort_by):
    fields = {'country': 'country_group', 'category': 'category_label', 'source': 'source_label'}
    unknown = {'country': '国家或地区未标注', 'category': '未分类', 'source': '来源未标注'}
    grouped = {}
    if sort_by == 'recent':
        cards = sorted(cards, key=_observed_order, reverse=True)
    for card in cards:
        label = card[fields[group_by]] if group_by in fields else ''
        grouped.setdefault(label, []).append(card)
    result = []
    ordinal = 0
    for label in sorted(grouped, key=lambda name: (not name, name)):
        members = grouped[label]
        for card in members:
            ordinal += 1
            card['ordinal'] = ordinal
        result.append(dict(id=f'daily-group-{len(result)+1}', label=label or unknown.get(group_by, ''), members=members))
    return result


def page_context(session, raw_day, group_by='none', sort_by='recommended'):
    if group_by not in GROUP_OPTIONS or sort_by not in SORT_OPTIONS:
        raise HTTPException(422, '日报分组或排序方式无效')
    day, latest = resolve_day(raw_day)
    issue = service.read_issue(session, day)
    names = countries()
    cards = []
    for rank, original in enumerate(issue['items'], 1):
        why = describe(original, issue)
        for link in why['evidence']['links']:
            link['domain'] = urlsplit(link['url']).hostname or ''
        for match in why['focus']:
            match['detail'] = _focus_evidence_label(match)
        card = dict(original, source_url=source_url(original.get('source_url')), sources=why['evidence']['links'])
        card['reasons'] = [_focus_reason(reason) for reason in (original.get('reasons') or ())]
        source_key = card.get('source_key') or ''
        source = source_key.partition(':')[0]
        organize_url = '/organize?' + urlencode({'candidate': source_key}) if source in SOURCE_LABELS else ''
        source_label = SOURCE_LABELS.get(source or card.get('source_kind'), '')
        if source == 'report':
            provider = (card.get('source_ref') or {}).get('provider', '')
            source_label = 'WorldNewsAPI' if provider == 'worldnewsapi' else source_label
        select_url = ''
        if can_select_card(card):
            select_url = '/workbench/new?' + urlencode({'digest_day': day.isoformat(), 'progress': card['key'],
                                                        'digest_token': card_token(original)})
        country_group, country_text = _geography(card, names)
        category = (card.get('category') or {}).get('label') or card.get('channel') or ''
        domain = urlsplit(card['source_url']).hostname or ''
        cards.append(dict(card, why=why, title_label=_title_label(card), publisher=_publisher_details(card),
                          rank=rank, country_group=country_group, country_text=country_text, category_label=category,
                          detail_url=_record_url(source_key, day.isoformat()), domain=domain,
                          select_url=select_url, organize_url=organize_url, source_label=source_label,
                          translation=daily_card_descriptor(issue['day'], original)))

    def day_link(target):
        params = {'day': target.isoformat()}
        if group_by != 'none':
            params['group_by'] = group_by
        if sort_by != 'recommended':
            params['sort_by'] = sort_by
        return '/radar?' + urlencode(params)

    return dict(issue=issue, cards=cards, reading_groups=_reading_groups(cards, group_by, sort_by),
                source_labels=SOURCE_LABELS, group_by=group_by, sort_by=sort_by,
                group_options=GROUP_OPTIONS, sort_options=SORT_OPTIONS, latest_day=latest,
                today_url=day_link(latest), is_today=day==latest,
                prev_url=day_link(day-timedelta(days=1)) if day>date.min else '',
                next_url=day_link(day+timedelta(days=1)) if day<latest else '')

"""Export an entire published radar issue, in its saved recommendation order."""
from collections import Counter
from datetime import datetime
import re
from urllib.parse import urlsplit

from ..desk.places import countries
from ..translations.daily import daily_card_descriptor
from ..translations.language import describe_language
from .calendar import BEIJING
from .markdown_text import _md, _md_link, _plain, _url, selection_note
from .why_follow import describe

SOURCES = {'gdelt': 'GDELT Events', 'gkg': 'GDELT GKG', 'local': '已整理事件', 'newsnow': 'NewsNow'}
GEO_BASIS = {'gkg_mentions': '报道提及', 'official_action': '行动地', 'local_place': '事件地点'}


def _local_time(value):
    if isinstance(value, str) and value:
        try:
            instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if instant.tzinfo:
                return instant.astimezone(BEIJING).strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, OverflowError):
            pass
    return ''


def _identifier(value):
    # A raw stable key in a Markdown code span is convenient to copy back into Codex.
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_./:-]+', value):
        return '`' + value + '`'
    return _md(value, None)


def _classification(card):
    fields = []
    category = (card.get('category') or {}).get('label') or card.get('channel')
    if category:
        fields.append('分类 ' + _md(category))
    geo = card.get('geography') or {}
    codes = list(geo.get('countries') or ())
    if not codes and card.get('country'):
        codes = [card['country']]
    if codes:
        basis = GEO_BASIS.get(geo.get('basis'), '归档地区')
        names = countries()
        labels = [names.get(str(code).lower(), str(code)) for code in codes]
        fields.append(basis + ' ' + _md('、'.join(labels), None))
    if card.get('actors'):
        fields.append('同组参与方 ' + _md('、'.join(card['actors']), None))
    return ' · '.join(fields)


def _coverage(evidence, card):
    # The publisher-group caveat applies to the whole issue and is printed once.
    notes = [text for text in evidence['limits'] if not text.startswith('多个域名可能转载')]
    coverage = [evidence['summary'], *notes]
    breadth = card.get('publisher_breadth') or {}
    if breadth.get('mapped_families'):
        coverage.append('媒体网络 ' + '、'.join(breadth['mapped_families']))
    return ' '.join(_md(part, None) for part in coverage)


def _source_lines(card, why):
    original = _url(card.get('source_url'))
    sources = why['evidence']['links']
    if original:
        yield '- 代表来源　' + _md_link(original)
    else:
        yield '- 代表来源　本版未保存可用原文链接。'
    for source in sources:
        link = _url(source['url'])
        if not link or link == original:
            continue
        title = source['title']
        kind = SOURCES.get(source.get('source_kind'), source.get('source_kind'))
        label = title if title not in (card.get('title'), link) else urlsplit(link).hostname
        yield '- 关联来源　' + _md_link(link, label) + (' · ' + _md(kind) if kind else '')


def _card_lines(rank, card, issue, translations):
    why = describe(card, issue)
    change, timing = why['change'], why['timing']
    title = _plain(card.get('title'), None)
    excerpt = _plain(card.get('excerpt'), None)
    yield f"## {rank:03d}. {_md(title, None) if title else '本版未保存标题'}"
    yield ''
    desc = daily_card_descriptor(issue['day'], card)
    key = card.get('key') or (desc or {}).get('key')
    source = card.get('source_kind') or ''
    identity = [_md(SOURCES.get(source, source) or '来源类型未保存')]
    if key:
        identity.append('条目 ID ' + _identifier(key))
    if card.get('source_key'):
        identity.append('代表记录 ' + _identifier(card['source_key']))
    yield '- 来源与标识　' + ' · '.join(identity)
    classification = _classification(card)
    if classification:
        yield '- 分类与地点　' + classification
    reconstruction = card.get('reconstruction') or {}
    language = describe_language(title, card.get('language') or reconstruction.get('language'))
    material = ('归档重建片段' if reconstruction else '标题和摘录' if excerpt and excerpt != title
                else '平台条目标题' if source == 'newsnow' else '仅标题' if title else '标题缺失')
    yield '- 当期材料　' + material + ' · ' + language['label']
    if excerpt and excerpt != title:
        yield '- 保存的摘录　' + _md(excerpt, None)
    quote = _plain(change.get('quote'), None)
    if quote and quote not in (excerpt, title):
        yield '- 报道中的行动表述　' + _md(quote, None)
    if translations.get(key):
        yield '- 已有中文译读　' + _md(translations[key], None)
    yield '- 本期变化　' + _md(change['label'])
    clocks = [f"{timing['event_basis']} {timing['event_day']}" if timing['event_day'] else '行动发生日期待核']
    for name, label in (('first_observed_at', timing['first_basis']), ('latest_observed_at', timing['latest_basis']),
                        ('local_received_at', timing['local_basis']), ('restored_at', '历史补录时间')):
        if value := _local_time(timing[name]):
            clocks.append(label + ' ' + value)
    yield '- 时间　' + ' · '.join(clocks)
    yield '- 报道覆盖　' + _coverage(why['evidence'], card)
    yield from _source_lines(card, why)
    for match in why['focus']:
        note = _md(match['label']) + ' · ' + _md(match['detail'], None)
        link = _url(match.get('source_url'))
        if link and link != card.get('source_url'):
            note += ' · ' + _md_link(link)
        yield '- 专题　' + note
    if context := change.get('context'):
        note = _md(context.get('summary'), None)
        example = context.get('example') or {}
        if ref := example.get('event_id'):
            note += ' · 对照记录 ' + _identifier(ref)
        if link := _url(example.get('source_url')):
            note += ' · ' + _md_link(link)
        yield '- 参与方对照　' + note
    if card.get('reasons'):
        yield '- 排序依据　' + ' · '.join(_md(reason, None) for reason in card['reasons'])


def _trial_sources(card, *, merge_log=False):
    evidence = card.get('shortlist_trial') or {}
    coarse = evidence.get('coarse') or {}
    if coarse.get('entities'):
        yield '- 代表报道提及的实体　' + _md('、'.join(coarse['entities']), None)
    members = evidence.get('merged_members') or []
    if members:
        count = evidence['merged_count']
        yield f'- 同一行动候选　选席时并入 {count} 组，附 {len(members)} 组材料。'
    for member in members:
        link = _url(member['source_url'])
        title = _md_link(link, member['title']) if link else _md(member['title'], None)
        yield '  - ' + title + ' · ' + _identifier(member['key']) + ' · ' + _md(member['reason'], None)
        if excerpt := member.get('excerpt'):
            yield '    - 成员摘录　' + _md(excerpt, None)
        other_links = []
        seen = {link}
        for item in member.get('sources') or ():
            address = _url(item.get('url'))
            if address and address not in seen:
                seen.add(address)
                other_links.append(_md_link(address, item.get('title')))
        if other_links:
            yield '    - 成员关联来源　' + ' · '.join(other_links)
    if evidence.get('merged_members_truncated'):
        note = '  - 本次并入材料过多，卡内达到保存上限。'
        if merge_log:
            note += '全部成员及其来源见同包 [merges.jsonl](merges.jsonl)，可按本条代表 ID 查找。'
        yield note


def render_shortlist(issue, *, translations=None, trial=False):
    """No selection or I/O on the event stream. Old cards retain their published seat."""
    if issue.get('status') != 'ready':
        raise ValueError('该日日报尚未发布')
    items = issue['items']
    kind = '当日滚动版' if issue.get('edition_kind') == 'intraday' else '收盘版'
    cutoff = _local_time(issue.get('cutoff_at')) or '旧版未保存'
    heading = '每日精选 · 全部候选'
    if trial:
        budget = (issue.get('stats') or {}).get('selection', {}).get('limit', 100)
        budget_text = '百条' if budget == 100 else f'Top {budget} '
        heading = f'{budget_text}选题试验 · 粗分类与行动归并'
    edition_label = '对照已发布日报' if trial else '日报'
    order = '试验推荐' if trial else '当期推荐'
    lines = [f"# {_md(issue['day'])} {heading}", '',
             f"{edition_label}第 {issue['revision']} 版 · {kind} · 资料截至 {cutoff}（北京时间）",
             f"完整导出本期 {len(items)} 条，按{order}顺序编号。文内时刻均为北京时间。"]
    if note := selection_note(issue):
        lines.append(note)
    if trial:
        lines.append('本次离线试排保留代表条目的标题和关联材料。并入材料可能另含细节，须逐条核对原文。')
    if items:
        sources = Counter(SOURCES.get(card.get('source_kind'), card.get('source_kind') or '类型未知') for card in items)
        lines.append('按代表材料统计：' + '，'.join(f'{_md(name)} {count} 条' for name, count in sources.items()) + '。')
        lines.append('报道覆盖仅统计当期可见材料，多个域名可能转载同一稿件。行动日期待核的条目保留其观测时间。')
    if issue.get('backfill_id') or (issue.get('stats') or {}).get('sources', {}).get('backfill_id'):
        lines.append('本期经过历史补采，部分材料为事后取得。')
    for rank, card in enumerate(items, 1):
        lines.append('')
        lines.extend(_card_lines(rank, card, issue, translations or {}))
        if card.get('shortlist_trial'):
            lines.extend(_trial_sources(card, merge_log=trial))
    return '\n'.join(lines) + '\n'

"""Build a ten-story video editor's brief from a single published issue."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import re
import unicodedata
from urllib.parse import urlsplit

from ..dedup.normalize import canonicalize_url
from ..selection.source_content import usable_title
from ..translations.daily import daily_card_descriptor
from .calendar import BEIJING
from .headline_identity import progress_keys
from .markdown_text import _md, _md_link, _plain, _url, selection_note
from .signals import headline_signals
from .why_follow import describe


LIMIT = 10
# Only spelling, attribution and articles can disappear from the initial headline key.
# Actors, negations, word order, changes of action and every number are retained.
_ARTICLES = {'a', 'an', 'the'}
_WIRE = re.compile(r'\s+(?:[-–—|]\s*(?:Reuters|Associated Press|AP News|AFP|新华社|路透社))\s*$', re.I)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_BACKGROUND = re.compile(
    r'^(?:opinion|commentary|sponsored|advertorial|how to|where to watch|review:)\b|'
    r'^(?:review|spoiler)\s*:|\bspoilers?\b.{0,60}\b(?:elimination|episode|leaked)\b|^\d+ signs (?:you|that you)\b|'
    r'^(?:广告推广|商业推广|赛事直播入口|观点评论)[：:]|'
    r'\b(?:black friday|cyber monday|early access)\b.{0,90}(?:\$[\d.]+ off|\d+% off|coupon code)|'
    r'\b(?:film|movie|series)\b.{0,120}\brotten tomatoes (?:rating|score)\b', re.I)
_SPECULATION = re.compile(r'\b(?:may|might|could|fears|opinion)\b|'
                          r'\b(?:issues? (?:a )?warning|backs? .{0,40}remarks)\b|'
                          r'可能|或将|恐将|呼吁|[?？]', re.I)
# Permit headline rewording with a supported action and enough matching
# concrete detail. Dates, numbers and negative/conditional wording must match.
_ACTIONS = {word: action for action, variants in (
    ('lift', ('lift', 'lifts', 'lifted', 'lifting')),
    ('sign', ('sign', 'signs', 'signed')),
    ('approve', ('approve', 'approves', 'approved')),
    ('catch', ('catch', 'catches', 'caught', 'capture', 'captured')),
    ('find', ('find', 'finds', 'found')),
    ('resume', ('resume', 'resumes', 'resumed')),
    ('halt', ('halt', 'halts', 'halted')),
    ('cancel', ('cancel', 'cancels', 'cancelled', 'canceled')),
) for word in variants}
_QUALIFIERS = {'no','not','never','denies','denied','may','might','could','will','would',
               'claims','allegedly','plans','proposed','after','before','prior'}
_CONTEXT = {'central','local','police','order','near','at','in','the','a','an'}


@dataclass(frozen=True)
class _StoryIdentity:
    key: str
    words: tuple
    detailed: bool
    urls: frozenset
    event_id: str
    stages: tuple
    anchors: frozenset
    qualifiers: tuple
    core: tuple


def _identity(card):
    title = unicodedata.normalize('NFKC', card['title']).casefold().replace('’', "'")
    title = _WIRE.sub('', title)
    # Remove a suffix that spells the source host (e.g. "| apnews.com").
    title, sep, credit = title.rpartition(' | ') if ' | ' in title else (title, '', '')
    host = urlsplit(card['source_url']).hostname or ''
    if sep and credit.removeprefix('www.') != host.removeprefix('www.'):
        title += sep + credit
    words = tuple(w for w in _WORD.findall(title) if w not in _ARTICLES)
    has_context = len(words) >= 5 or sum(len(w) for w in words if re.search(r'[一-鿿]', w)) >= 12
    progress = progress_keys(card['title'], card['source_url'])[1]
    docs = {canonicalize_url(link) for link in [card['source_url'], *[
        s.get('url') for s in (card.get('sources') or ()) if isinstance(s, dict)]] if _url(link)}
    # A single story can contain several actions. A shared URL / GlobalEventID
    # alone never collapses different headlines, reversals, figures or stages.
    official_id = (card.get('official_coverage') or {}).get('event_id')
    roles = tuple(_ACTIONS[w] for w in words if w in _ACTIONS)
    core = tuple(_ACTIONS.get(w, w) for w in words if w not in _CONTEXT)
    qualifiers = tuple(w for w in words if w in _QUALIFIERS or any(c.isdecimal() for c in w))
    return _StoryIdentity(progress, words, has_context, frozenset(docs), official_id or '',
                          roles, frozenset(core), qualifiers, core)


def _same_story(left, right):
    if left.key and left.key == right.key:
        return True
    common_id = bool(left.urls & right.urls or left.event_id and left.event_id == right.event_id)
    if left.words and left.words == right.words and (left.detailed or common_id):
        return True
    # Keep action order and other concrete details. The ordered core protects
    # against a swap of actors or the addition of a changed quantity.
    if (not left.stages or left.stages != right.stages or len(left.anchors) < 6 or
            left.anchors != right.anchors or left.qualifiers != right.qualifiers):
        return False
    if left.core == right.core:
        return True
    # An adjacent non-directional object inverts in passive constructions
    # such as “Police lift evacuation” / “Evacuation order lifted”.
    swap = {(('lift', 'evacuation'), ('evacuation', 'lift')),
            (('lift', 'ban'), ('ban', 'lift'))}
    heads = left.core[:2], right.core[:2]
    return (heads in swap or heads[::-1] in swap) and left.core[2:] == right.core[2:]


def _related(anchors, other):
    overlap = len(anchors & other)
    return overlap >= 5 and overlap >= 0.6 * min(len(anchors), len(other))


def _tier(card, why):
    """Order editorial checks. This grade expresses material availability only."""
    kind = why['change']['kind']
    excerpt = _plain(card.get('excerpt'))
    has_excerpt = bool(excerpt and excerpt.casefold() != _plain(card['title']).casefold())
    _, issuer = headline_signals(card['title'], card['source_url'])
    if kind == 'reconstructed' or card.get('reconstruction'):
        return 1, '核对重建片段对应的原文、上下文和行动主体。'
    if kind == 'coverage_growth':
        return 1, '查新增报道是否带来新行动，核对是否为同稿转载。'
    if kind == 'attention':
        return 1, '找到触发热榜的原始报道，核对具体进展和发生时间。'
    if kind in ('dated_action', 'action_lead') and (has_excerpt or issuer):
        if kind == 'dated_action':
            return 4, '对照原文核实行动日期、主体和适用范围。'
        return 3, '查明行动日期，核对是否已经实施。'
    if issuer or has_excerpt:
        return 2, '对照原文核对发生时间，并找出本期新增的事实。'
    return 1, '目前只保存了标题线索，先核实原文中的行动、日期和直接影响。'


def select_brief(issue):
    """Return up to ten distinct leads in stable order, partitioned by readiness.

    Only the edition's already-selected cards compete. In each evidence tier,
    give underrepresented beats and countries a chance, using the daily rank
    as a bounded tie-break. Leave the remainder unfilled if nothing qualifies.
    """
    candidates = []
    for rank, card in enumerate(issue['items'], 1):
        title = _plain(card.get('title'))
        if not usable_title(title) or not _url(card.get('source_url')) or _BACKGROUND.search(title.casefold()):
            continue
        why = describe(card, issue)
        tier, note = _tier(card, why)
        # A readable isolated headline can enter the 100. The video brief
        # needs some additional reason to spend even a verification slot.
        title_impact, _ = headline_signals(title, card['source_url'])
        if (tier == 1 and why['change']['kind'] == 'new_material' and
                not any((title_impact, card.get('impact'), why.get('focus'),
                         len(why['evidence']['links']) > 1))):
            continue
        category = (card.get('category') or {}).get('label') or card.get('channel') or ''
        country = (card.get('geography') or {}).get('primary') or card.get('country') or ''
        # Publisher families are already folded in the edition. Limit their
        # contribution; extra coverage cannot certify a claim or replace text.
        units = (card.get('publisher_breadth') or {}).get('units') or card.get('domain_count') or 1
        source_bonus = 8 * min(max(units - 1, 0), 3)
        lead_bonus = 8 if why['change']['kind'] in ('dated_action', 'action_lead') else 0
        tentative = 20 if _SPECULATION.search(title) else 0
        priority = rank - source_bonus - lead_bonus + tentative
        candidates.append(dict(rank=rank, priority=priority, card=card, why=why, note=note, tier=tier,
                               status='candidate' if tier >= 2 else 'check', category=category,
                               country=country, identity=_identity(card)))

    selected = []
    beats, places = Counter(), Counter()
    while candidates and len(selected) < LIMIT:
        # Rank is counted in seats: one additional selected article in a beat
        # costs six places; one in a country costs three. Unknown is neutral.
        def order(row):
            penalty = 6 * beats[row['category']] + 3 * places[row['country']]
            # A separate development in the same incident can still enter;
            # prefer another story when several newsworthy leads compete.
            if any(_related(row['identity'].anchors, prior['identity'].anchors) for prior in selected):
                penalty += 24
            return -row['tier'], row['priority'] + penalty, row['rank']
        best = min(candidates, key=order)
        candidates = [row for row in candidates if row is not best
                      and not _same_story(best['identity'], row['identity'])]
        if best['category']:
            beats[best['category']] += 1
        if best['country']:
            places[best['country']] += 1
        selected.append(best)
    for row in selected:
        del row['identity']
    return selected


def _clock(value):
    if isinstance(value, str) and value:
        try:
            when = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if when.tzinfo:
                return when.astimezone(BEIJING).strftime('%Y-%m-%d %H:%M')
        except (ValueError, OverflowError):
            pass
    return ''


def render_brief(issue, *, translations=None):
    """UTF-8-ready Markdown, with direct links and concrete verification tasks."""
    if issue.get('status') != 'ready':
        raise ValueError('该日日报尚未发布')
    entries = select_brief(issue)
    leads = sum(row['status'] == 'candidate' for row in entries)
    kind = '当日滚动版' if issue.get('edition_kind') == 'intraday' else '收盘版'
    cutoff = _clock(issue.get('cutoff_at')) or '早期版本未保存'
    lines = [f"# {_md(issue['day'])} 十件大事 · 视频选题简报", '',
             f"北京时间 · {kind} · 日报第 {issue['revision']} 版 · 资料截至 {cutoff}", '',
             f"本期从日报入选的 {len(issue['items'])} 条中选出 {leads} 条视频候选，"
             f"另列 {len(entries) - leads} 条待核线索。来源组用于衡量报道覆盖，录制前请核查原文及转载关系。"]
    if note := selection_note(issue):
        lines.append(note)
    if issue.get('backfill_id') or issue.get('stats', {}).get('sources', {}).get('backfill_id'):
        lines.append('本期经过历史补采，含事后取得的资料。')
    if not entries:
        lines.extend(['', '本期没有满足选题条件且带可用原文链接的条目。'])
    for status, label in [('candidate', '优先核实的视频候选'), ('check', '待核线索')]:
        group = [row for row in entries if row['status'] == status]
        if not group:
            continue
        lines.extend(['', '## ' + label])
        for i, row in enumerate(group, 1):
            card, why = row['card'], row['why']
            title = _plain(card['title'])
            order = i if status == 'candidate' else ('线索 ' + str(i))
            lines.extend(['', f"### {order}. {_md(title)}", '',
                          f"日报第 {row['rank']} 条" + (f" · {_md(row['category'])}" if row['category'] else ''),
                          '', f"- 本期变化：{_md(why['change']['label'])}。{_md(why['change']['detail'])}"])
            context = why['change'].get('context') or {}
            if context.get('status') in {'seen', 'not_seen_in_sample'} and context.get('summary'):
                reference = _url((context.get('example') or {}).get('source_url'))
                link = ' · ' + _md_link(reference) if reference else ''
                lines.append('- 参与方对照：' + _md(context['summary']) + link)
            if translations:
                descriptor = daily_card_descriptor(issue['day'], card)
                chinese = translations.get(descriptor['key']) if descriptor else None
                if chinese:
                    truncated = len(chinese) > 500
                    label = '已有中文译读节选' if truncated else '已有中文译读'
                    lines.append(f'- {label}：' + _md(chinese) + ('…' if truncated else ''))
            passage = _plain(why['change'].get('quote') or card.get('excerpt'), 240)
            if passage and passage.casefold() != title.casefold():
                lines.append('- 材料摘录：' + _md(passage))
            timing = why['timing']
            if timing.get('event_day'):
                lines.append(f"- 日期依据：{_md(timing['event_day'])}（{_md(timing['event_basis'])}）")
            else:
                lines.append('- 行动日期：待核。本期收录时间只说明系统何时看到这条材料。')
            if restored := _clock(timing.get('restored_at')):
                lines.append('- 实际补录：' + restored + '（北京时间）')
            evidence = why['evidence']
            lines.append('- 来源证据：' + _md(evidence['summary']))
            if why.get('focus'):
                focus = '、'.join(f"{item['label']}（{'已关联' if item['kind'] == 'membership' else '范围匹配，待核'}）"
                                 for item in why['focus'][:3])
                lines.append('- 关联专题：' + _md(focus))
            lines.append('- 录制前核对：' + row['note'])
            links = list(dict.fromkeys(url for url in [card['source_url'], *[
                link.get('url') for link in evidence['links']]] if _url(url)))
            lines.append('- 阅读原文：' + ' · '.join(_md_link(url) for url in links[:3]))
    return '\n'.join(lines) + '\n'

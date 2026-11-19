"""Bounded, read-only association of source reports with coded events.

A GDELT source URL is a document relationship. A report can describe several
events, so its headline/excerpt is never promoted to a verified event summary.
"""
from __future__ import annotations

from sqlalchemy import union_all
from sqlmodel import select

from ..intake.official_models import OfficialMention
from ..storage.articles import identity_key

MENTIONS_PER_EVENT = 3
RECENT_MENTION_CANDIDATES = 12
MAX_EVENT_PAGE = 100
RELATION_NOTE = '相关报道可能涉及多个事件；标题和摘要不表示本条自动编码已核实。'


def mention_sources(session, event_ids):
    """Rank three previews within at most twelve recent indexed rows per event.

    Ranking all historical mentions by confidence would scan a hot event's
    entire history even though only three rows are returned. Each UNION branch
    first uses the existing (event_id,last_seen) index and LIMIT; confidence and
    key tie-breaking happen only inside that bounded candidate set.
    """
    keys = list(dict.fromkeys(event_ids))
    if len(keys) > MAX_EVENT_PAGE:
        raise ValueError('事件报道内容每次最多读取100条事件')
    result = {key: [] for key in keys}
    if not keys:
        return result
    branches=[]
    for key in keys:
        recent=select(OfficialMention.key,OfficialMention.event_id,OfficialMention.doc_identifier,
            OfficialMention.domain,OfficialMention.confidence,OfficialMention.last_seen)
        recent=recent.where(OfficialMention.event_id==key).order_by(OfficialMention.last_seen.desc())
        recent=recent.limit(RECENT_MENTION_CANDIDATES).subquery()
        branches.append(select(recent.c.key,recent.c.event_id,recent.c.doc_identifier,
            recent.c.domain,recent.c.confidence,recent.c.last_seen))
    query=union_all(*branches) if len(branches)>1 else branches[0]
    rows=session.exec(query.execution_options(n2d_query_name='selection.report_mentions.page')).all()
    for key, event_id, uri, domain, confidence, seen in rows:
        result[event_id].append(dict(key=key, url=uri, domain=domain, confidence=confidence, last_seen=seen))
    for event_id,items in result.items():
        items.sort(key=lambda row:row['key'])
        items.sort(key=lambda row:row['last_seen'] or '',reverse=True)
        items.sort(key=lambda row:row['confidence'] if row['confidence'] is not None else -1,reverse=True)
        result[event_id]=items[:MENTIONS_PER_EVENT]
    return result


def candidates(events, mentions, safe_url):
    """Stable document candidates; deduplicate tracking aliases by URL identity."""
    result = {}
    for event in events:
        found, seen = [], set()
        values = [dict(url=event.source_url, relation='source_url')]
        values.extend(dict(url=row['url'], relation='mention', mention_key=row['key'])
                      for row in mentions.get(event.event_id, ()))
        for row in values:
            url = safe_url(row['url'])
            if not url:
                continue
            key = identity_key(dict(url=url))
            if key in seen:
                continue
            seen.add(key)
            found.append(dict(row, url=url))
        result[event.event_id] = found
    return result


def attach(card, candidates, previews):
    """Decorate one card using saved preview data only; no fetch or enqueue."""
    from ..translations.language import describe_language
    reports = []
    for source in candidates:
        preview = dict(previews.get(source['url']) or {})
        preview.setdefault('source_url', source['url'])
        preview.setdefault('title', '')
        preview.setdefault('excerpt', '')
        preview.setdefault('status', 'missing')
        preview.update(relation=source['relation'], relation_note=RELATION_NOTE,
            source_kind='search_snippet', material_kind='source_excerpt', requires_original_text=True)
        if source.get('mention_key'):
            preview['mention_key'] = source['mention_key']
        reports.append(preview)
    # Prefer an actually readable title+excerpt; source_url wins equal-quality
    # ties because the list above preserves official-source / mention order.
    readable = [p for p in reports if str(p.get('title') or '').strip() or str(p.get('excerpt') or '').strip()]
    def quality(preview):
        return (bool(preview.get('title')) and bool(preview.get('excerpt')),
                bool(preview.get('title')), preview.get('origin') == 'local_article')
    best = max(readable, key=quality) if readable else None
    card['reports'] = reports
    card['report_preview'] = best
    job_states = {p.get('fetch_status') or p.get('job_status') or p.get('status') for p in reports}
    card['content_job_status'] = next((state for state in ('running','pending','failed','unavailable','ready')
        if state in job_states),'missing' if candidates else 'missing_source')
    card['content_note'] = RELATION_NOTE
    card['content_scope'] = dict(mentions_per_event=MENTIONS_PER_EVENT,
        recent_candidate_limit=RECENT_MENTION_CANDIDATES,ranking_scope='recent_candidates_not_full_history',
        candidate_documents=len(candidates),counts_are_partial=True,
        note='每事件先取最近12条提及，再从中按置信度选最多3条报道预览；不是全历史最高置信，也未覆盖所有报道。')
    if best:
        card['display_title'] = str(best.get('title') or '').strip() or '已取得报道摘录，标题待补充'
        card['title'] = card['display_title']
        card['excerpt'] = str(best.get('excerpt') or '')
        card['content_status'] = 'ready' if card['excerpt'] else 'title_only'
        language = best.get('language_info')
        if not isinstance(language, dict) or 'display' not in language:
            language = describe_language(card['display_title'] + '\n' + card['excerpt'], best.get('language', ''))
        card['language'] = language
        card['source_language'] = language
    else:
        card['display_title'] = '报道内容待补充'
        card['excerpt'] = ''
        states = {p.get('status') for p in reports}
        card['content_status'] = next((state for state in ('running','pending','failed','unavailable')
            if state in states), 'missing' if candidates else 'missing_source')
    return card


def enrich_cards(session, events, cards, safe_url, *, source_mentions=None, resolved_previews=None):
    """At most400 URL candidates for100 cards, one batched resolver call."""
    if not events:
        return cards
    from .source_content import resolve_previews
    if source_mentions is None:
        source_mentions = mention_sources(session, [row.event_id for row in events])
    selected = candidates(events, source_mentions, safe_url)
    urls = list(dict.fromkeys(row['url'] for rows in selected.values() for row in rows))
    previews = resolved_previews if resolved_previews is not None else (resolve_previews(session, urls) if urls else {})
    for event, card in zip(events, cards):
        attach(card, selected[event.event_id], previews)
    missing = [card for card in cards if not (card.get('report_preview') or {}).get('excerpt')]
    if missing:
        from .ngram_enrichment import qualified_for_events
        leads = qualified_for_events(session, [str(card['event_id']) for card in missing])
        for card in missing:
            lead = leads.get(str(card['event_id']))
            if lead and safe_url(lead.get('url')):
                card['reconstruction'] = lead
                if not card.get('report_preview'):
                    card['display_title'] = '片段重建中的行动线索'
    return cards

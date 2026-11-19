"""Read the separate video-candidate edition and explicitly queue its builds."""
from datetime import date, timedelta
from hashlib import sha256
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from ..digest.markdown_text import _md, _md_link, _url
from ..digest.shortlist import _identifier, _local_time
from .daily_radar import SOURCE_LABELS, _title_label, resolve_day
from .research import same_origin

SIGNALS = (('person', '人物'), ('movement', '情节'), ('life', '生活关联'))
MATERIAL_LABELS = {'headline': '标题线索', 'excerpt': '标题与摘录', 'reconstruction': '归档重建片段 · 待核原文'}


def _page_url(day):
    return '/radar/video?' + urlencode({'day': day})


def _anchor(key):
    return 'video-' + sha256(key.encode()).hexdigest()[:20] if isinstance(key, str) and key else ''


def _history(entries, current_day):
    history = []
    for item in list(entries or [])[:3]:
        try:
            day = date.fromisoformat(item['day']).isoformat()
        except (KeyError, TypeError, ValueError):
            continue
        anchor = _anchor(item.get('key'))
        if day >= current_day or not anchor or not item.get('title'):
            continue
        history.append(dict(day=day, title=item['title'], url=_url(item.get('url')), reason=item.get('reason'),
                            previous_url=_page_url(day) + '#' + anchor))
    return history


def _cards(issue):
    from ..digest.video_story import focus_url

    cards = []
    for ordinal, saved in enumerate(issue['items'], 1):
        source_key = saved.get('source_key') or ''
        source_prefix, _, source_id = source_key.partition(':')
        source_kind = saved.get('source_kind') or ''
        provider = (saved.get('source_ref') or {}).get('provider')
        link = _url(saved.get('source_url'))
        details = saved.get('video') or {}
        signals = [dict(details[field], label=label) for field, label in SIGNALS
                   if isinstance(details.get(field), dict) and details[field].get('text')]
        material = MATERIAL_LABELS.get((details.get('evidence') or {}).get('status')) or _title_label(saved)
        sources = {}
        for item in saved.get('sources') or []:
            if url := _url(item.get('url')):
                if url != link:
                    sources.setdefault(url, item.get('title') or urlsplit(url).hostname)
        select_url = ''
        if link and source_id and len(source_key) <= 240 and source_prefix in {'gkg', 'gdelt', 'local', 'newsnow', 'report'}:
            select_url = '/workbench/new?' + urlencode({'candidate': source_key})
        cards.append(dict(ordinal=ordinal, key=saved.get('key'), anchor=_anchor(saved.get('key')), title=saved.get('title'),
                          excerpt=saved.get('excerpt'), url=link, domain=urlsplit(link).hostname or '',
                          source='WorldNewsAPI' if provider == 'worldnewsapi' else SOURCE_LABELS.get(source_kind, source_kind),
                          material=material, signals=signals,
                          reasons=saved.get('reasons') or (), sources=sources, select_url=select_url,
                          focus_url=focus_url(saved, day=issue['day']),
                          history=_history(saved.get('story_history'), issue['day'])))
    return cards


def render_markdown(issue, cards):
    cutoff = _local_time(issue.get('cutoff_at'))
    header = f"第 {issue['revision']} 版 · {len(cards)} 条"
    if cutoff:
        header += f' · 资料截至 {cutoff}（北京时间）'
    lines = [f"# {issue['day']} 视频候选", '', header]
    for card in cards:
        lines.extend(['', f"## {card['ordinal']:03d}. {_md(card['title'], None)}", ''])
        if card['key']:
            lines.append('- 条目 ID　' + _identifier(card['key']))
        provenance = ' · '.join(filter(None, (_md(card['source']), _md(card['material']))))
        if card['url']:
            provenance += ' · ' + _md_link(card['url'], '原文')
        if provenance:
            lines.append('- 来源　' + provenance)
        if card['excerpt'] and card['excerpt'] != card['title']:
            lines.append('- 报道摘录　' + _md(card['excerpt'], None))
        for signal in card['signals']:
            line = f"- {signal['label']}　{_md(signal['text'], None)}"
            if signal.get('quote'):
                line += ' · ' + ('标题' if signal.get('field') == 'title' else '摘录') + '「' + _md(signal['quote'], None) + '」'
            lines.append(line)
        if not card['signals'] and card['reasons']:
            lines.append('- 入选依据　' + ' · '.join(_md(reason, None) for reason in card['reasons']))
        for earlier in card['history']:
            title = _md_link(earlier['url'], earlier['title']) if earlier['url'] else _md(earlier['title'], None)
            lines.append(f"- 可回看　{earlier['day']} · {title}" +
                         (' · ' + _md(earlier['reason']) if earlier['reason'] else ''))
        for url, title in card['sources'].items():
            lines.append('- 关联来源　' + _md_link(url, title))
    return '\n'.join(lines) + '\n'


def register(app, engine, render):
    from ..digest import video_service

    def page(issue, selected, latest, *, notice='', status_code=200):
        response = render('video_candidates.html', active='radar', issue=issue, cards=_cards(issue),
                          latest_day=latest, worker_enabled=video_service.worker_enabled(), notice=notice,
                          today_url=_page_url(latest),
                          prev_url=_page_url(selected - timedelta(days=1)) if selected > date.min else '',
                          next_url=_page_url(selected + timedelta(days=1)) if selected < latest else '',
                          status_url='/api/radar/video?' + urlencode({'day': selected}),
                          export_url='/radar/video/export.md?' + urlencode({'day': selected, 'revision': issue['revision']}))
        response.status_code = status_code
        return response

    @app.get('/radar/video', response_class=HTMLResponse, include_in_schema=False)
    def video_candidates(day: str | None = Query(None, max_length=10)):
        selected, latest = resolve_day(day)
        with Session(engine) as session:
            issue = video_service.read_issue(session, selected)
        return page(issue, selected, latest)

    @app.get('/api/radar/video')
    def video_build_state(day: str | None = Query(None, max_length=10)):
        selected, _ = resolve_day(day)
        with Session(engine) as session:
            return video_service.read_issue_state(session, selected)

    @app.post('/radar/video/generate', dependencies=[Depends(same_origin)], include_in_schema=False)
    def generate_video_candidates(day: str = Form(max_length=10)):
        selected, latest = resolve_day(day)
        with Session(engine) as session:
            state = video_service.read_issue_state(session, selected)
        try:
            if selected < latest and state['status'] == 'ready' and state['build_status'] not in ('pending', 'building'):
                video_service.request_rebuild(engine, selected)
            else:
                video_service.request_issue(engine, selected)
        except RuntimeError:
            if video_service.worker_enabled():
                raise
            with Session(engine) as session:
                issue = video_service.read_issue(session, selected)
            return page(issue, selected, latest, notice='视频候选生成已暂停。', status_code=503)
        wake = getattr(getattr(app.state, 'video_digest_worker', None), 'wake', None)
        if wake:
            wake()
        return RedirectResponse(_page_url(selected), status_code=303)

    @app.get('/radar/video/export.md', response_class=Response)
    def export_video_candidates(day: str | None = Query(None, max_length=10),
                                revision: int | None = Query(None, ge=1)):
        selected, latest = resolve_day(day)
        with Session(engine) as session:
            issue = video_service.read_issue(session, selected)
        if issue['status'] != 'ready':
            raise HTTPException(404, '这期视频候选尚未发布')
        if revision is not None and issue['revision'] != revision:
            return page(issue, selected, latest, notice='本期候选已更新，请重新下载。', status_code=409)
        name = f"news2douyin-video-candidates-{issue['day']}-r{issue['revision']}.md"
        return Response(render_markdown(issue, _cards(issue)), media_type='text/markdown',
                        headers={'Content-Disposition': f'attachment; filename="{name}"',
                                 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

"""WebUI: HTML pages over the v7 API (dashboard / runs / articles / events / reports).

Pages are server-rendered with Jinja2 templates in server/templates/.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import FastAPI, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlmodel import or_, select

from .tasks import task_call
from .scripts import script_call
from ..video import workbench as script_workbench
from ..video.service import build_script_package
from ..report.html_report import build_html_report
from ..search.service import article_page, event_page
from ..storage.articles import latest_version, version_page
from ..events.service import evidence_counts
from .news import query_call
from urllib.parse import urlencode
from difflib import unified_diff
from ..storage.db import session_scope
from ..storage.queries import all_rows, count_rows
from ..storage.models import GdeltDiscovery, Article, ArticleEventLink, CollectJob, CollectProfile, Event, RunRecord, ArticleVersion, EventAssignment
from ..storage.utils import loads
from ..search.dates import calendar_day, timezone_name
from ..editorial.daily import list_picks
from ..collect.providers.gdelt import profile_codes as gdelt_profile_codes
from ..collect.management import profile_enabled, job_dict, save_job, set_job_enabled
from ..collect.diagnostics import task_diagnostics, summary as diagnostic_summary, REASONS, FALLBACKS

_TEMPLATES = Path(__file__).parent / 'templates'


def _fmt_ts(s: str | None) -> str:
    return (s or '').replace('T', ' ').replace('Z', '')


def register_webui_routes(app: FastAPI, engine, scheduler, storage_root: str) -> None:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(['html']),
    )

    def render(name: str, **ctx) -> HTMLResponse:
        page = env.get_template(name)
        return HTMLResponse(page.render(fmt_ts=_fmt_ts, active=ctx.pop('active', ''), **ctx))

    from .causal import register as register_causal
    register_causal(app, engine, render)

    @app.get('/resources/countries', response_class=HTMLResponse, include_in_schema=False, name='country_directory_legacy')
    @app.get('/country-reference.html', response_class=HTMLResponse)
    def country_directory():
        from .countries import directory_context
        return render('country-reference.html', active='countries', **directory_context())

    @app.get('/handoff', response_class=HTMLResponse)
    def video_app_handoff():
        return render('handoff.html', active='videos')

    @app.get('/setup', response_class=HTMLResponse)
    def first_run_setup():
        return render('setup.html', active='setup')

    from urllib.parse import urlsplit
    def safe_url(value):
        try:
            parsed = urlsplit(value or '')
            return value if parsed.scheme.lower() in {'https', 'http'} and parsed.netloc else ''
        except ValueError:
            return ''
    env.filters['safe_url'] = safe_url
    def local_ts(value, timezone):
        from datetime import datetime, timezone as utc
        from zoneinfo import ZoneInfo
        try:
            moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=utc.utc)
            return moment.astimezone(ZoneInfo(timezone)).strftime('%Y-%m-%d %H:%M')
        except (ValueError, TypeError, AttributeError):
            return value or '时间未知'
    env.filters['local_ts'] = local_ts

    @app.get('/scripts', response_class=HTMLResponse)
    def page_scripts(event_key: str = ''):
        return RedirectResponse('/workbench?view=creation',303)

    @app.get('/events/{event_key}/generate', response_class=HTMLResponse)
    def page_generation(event_key: str):
        from ..events.workbench import workspace
        with session_scope(engine) as session:
            view = task_call(workspace, session, event_key)
        return render('script_generate.html', active='scripts', view=view)

    @app.get('/scripts/{package_key}', response_class=HTMLResponse)
    def page_script(package_key: str):
        from .workbench import owner
        from ..workbench.service import sync
        with session_scope(engine) as session:
            sync(session);session.commit()
            task=owner(session,'script',package_key)
            if task:return RedirectResponse(f'/workbench/{task}/creation?script={package_key}',303)
            script = script_call(script_workbench.script_detail, session, package_key)
        return render('script_editor.html', active='scripts', script=script)

    @app.post('/webui/scripts/build')
    def webui_script_build(event_key: str = Form(...)):
        with session_scope(engine) as session:
            package = script_call(build_script_package, session, event_key,
                                  output_root=Path(storage_root) / 'packages')
            key = package.package_key
        return RedirectResponse('/scripts/' + key, status_code=303)

    from .video import video_call, list_productions
    from ..video.production import production_detail

    @app.get('/videos', response_class=HTMLResponse)
    def page_videos():
        return render('videos.html', active='videos', timezone=timezone_name())

    @app.get('/scripts/{package_key}/video', response_class=HTMLResponse)
    def page_video_setup(package_key: str, from_task: str = ''):
        from .workbench import owner
        with session_scope(engine) as session:
            task=owner(session,'script',package_key)
            if task and not from_task:return RedirectResponse(f'/workbench/{task}/creation?mode=video&script={package_key}',303)
            script = video_call(script_workbench.script_detail, session, package_key)
        previous = video_call(production_detail, engine, from_task) if from_task else None
        if previous and previous['package_key'] != package_key:
            raise HTTPException(422, '原视频任务不属于此脚本')
        from ..video.templates import catalog, scene_plan
        from ..video.production import VideoOptions
        scenes = video_call(scene_plan, script, VideoOptions().model_dump()) if script.get('document') else []
        return render('video_setup.html', active='videos', script=script, previous=previous, scenes=scenes, templates=catalog())

    @app.get('/videos/{task_id}', response_class=HTMLResponse)
    def page_video_result(task_id: str):
        from .workbench import owner
        from ..storage.models import VideoProduction
        with session_scope(engine) as session:
            production=session.get(VideoProduction,task_id)
            task=owner(session,'script',production.package_key) if production else None
            if task:return RedirectResponse(f'/workbench/{task}/works?video={task_id}',303)
        video = video_call(production_detail, engine, task_id)
        return render('video_detail.html', active='videos', video=video)

    # ------------------------------------------------------------------ pages
    @app.get('/daily', response_class=HTMLResponse)
    def page_daily(day: str = '', timezone: str = '', q: str = '', profile_name: str = '',
                   time_field: str = 'collected', task_id: str = '', mode: str = '',
                   offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100)):
        if task_id:
            task_call(app.state.tasks.get,task_id)
            return RedirectResponse('/articles?'+urlencode(dict(task_id=task_id,q=q,timezone=timezone)),303)
        return RedirectResponse('/articles?'+urlencode(dict(period='custom' if day else 'all',date_from=day,date_to=day,q=q,profile_name=profile_name,time_field=time_field,timezone=timezone,limit=limit,offset=offset,mode=mode or 'contains')),303)

    @app.get('/profiles', response_class=HTMLResponse)
    def page_profiles(selected: str = ''):
        from .app import _profile_to_dict
        from ..collect.providers.newsnow import SOURCE_CATALOG,DEFAULT_SOURCES
        with session_scope(engine) as session:
            profiles = [_profile_to_dict(p, session) for p in session.exec(select(CollectProfile).order_by(CollectProfile.name))]
            jobs = [job_dict(j) for j in session.exec(select(CollectJob).where(CollectJob.schedule_type != 'archived').order_by(CollectJob.id))]
        return render('profiles.html', active='profiles', profiles=profiles, jobs=jobs, selected=selected, gdelt=gdelt_profile_codes(),
                      today=calendar_day(), timezone=timezone_name(),newsnow_catalog=SOURCE_CATALOG,newsnow_default_sources=DEFAULT_SOURCES)

    @app.get('/admin', response_class=HTMLResponse)
    def page_dashboard():
        with session_scope(engine) as session:
            profiles = [
                {
                    'name': p.name, 'country': p.country, 'language': p.language,
                    'categories': loads(p.categories_json, []),
                    'max_items': p.max_items,
                    'enabled': profile_enabled(session, p.name),
                }
                for p in session.exec(select(CollectProfile).order_by(CollectProfile.name)).all()
            ]
            jobs = [
                {
                    'id': j.id, 'name': j.name, 'enabled': j.enabled, 'cron_expr': j.cron_expr,
                    'timezone': j.timezone, 'profile_name': j.profile_name,
                    'last_run_at': j.last_run_at, 'last_status': j.last_status,
                }
                for j in session.exec(select(CollectJob).where(CollectJob.schedule_type != 'archived').order_by(CollectJob.id)).all()
            ]
            runs = [
                {
                    'id': r.id, 'run_key': r.run_key, 'status': r.status, 'trigger_type': r.trigger_type,
                    'profile_name': r.profile_name, 'started_at': r.started_at,
                    'finished_at': r.finished_at, 'stats': loads(r.stats_json, {}),
                    'error_text': r.error_text,
                }
                for r in all_rows(session,select(RunRecord).order_by(RunRecord.id.desc()).limit(10),name='admin.recent_runs')
            ]
            counts = {
                'articles': count_rows(session,Article,name='admin.count.articles'),
                'events': count_rows(session,Event,name='admin.count.events'),
                'runs': count_rows(session,RunRecord,name='admin.count.runs'),
            }
        return render(
            'dashboard.html', active='home', profiles=profiles, jobs=jobs, runs=runs, counts=counts,
            scheduler_running=scheduler._thread is not None and scheduler._thread.is_alive(),
            llm_alive=_llm_alive(),
        )

    @app.get('/runs', response_class=HTMLResponse)
    def page_runs():
        with session_scope(engine) as session:
            runs = [
                {
                    'id': r.id, 'run_key': r.run_key, 'status': r.status, 'trigger_type': r.trigger_type,
                    'profile_name': r.profile_name, 'started_at': r.started_at,
                    'finished_at': r.finished_at, 'stats': loads(r.stats_json, {}),
                    'error_text': r.error_text,
                }
                for r in session.exec(select(RunRecord).order_by(RunRecord.id.desc()).limit(200)).all()
            ]
        return render('runs.html', active='runs', runs=runs)

    @app.get('/runs/{run_id}', response_class=HTMLResponse)
    def page_run_detail(run_id: int):
        with session_scope(engine) as session:
            row = session.get(RunRecord, run_id)
            if not row:
                return render('404.html', active='runs', title='运行记录不存在')
            run = {
                'id': row.id, 'run_key': row.run_key, 'status': row.status,
                'trigger_type': row.trigger_type, 'profile_name': row.profile_name,
                'started_at': row.started_at, 'finished_at': row.finished_at,
                'stats': loads(row.stats_json, {}), 'error_text': row.error_text,
                'storage_path': row.storage_path,
            }
        articles: list[dict] = []
        raw_count = 0
        if row.storage_path:
            run_dir = Path(row.storage_path)
            art_file = run_dir / 'articles.jsonl'
            if art_file.exists():
                try:
                    articles = [json.loads(l) for l in art_file.read_text(encoding='utf-8').splitlines() if l.strip()]
                except Exception:
                    articles = []
            raw_file = run_dir / 'raw.jsonl'
            if raw_file.exists():
                raw_count = sum(1 for _ in raw_file.open(encoding='utf-8'))
        for a in articles:
            src = a.get('source') or {}
            a['_domain'] = src.get('domain', '')
        return render('run_detail.html', active='runs', run=run, articles=articles, raw_count=raw_count,
                      diagnostic_summary=diagnostic_summary(run['stats'], run['status'], run['error_text']), reasons=REASONS)

    def pagination(path, page, params):
        def url(offset):
            return path + '?' + urlencode({**params, 'offset': offset})
        return {'total': page.total, 'offset': page.offset, 'limit': page.limit,
                'prev_url': url(max(0, page.offset - page.limit)) if page.offset else '',
                'next_url': url(page.offset + page.limit) if page.offset + len(page.items) < page.total else ''}

    @app.get('/articles', response_class=HTMLResponse)
    def page_articles(q: str = '', country: str = '', category: str = '', duplicates: str = 'any',
                      limit: int = Query(60, ge=1, le=500), offset: int = Query(0, ge=0), mode: str = 'contains',
                      period: str = 'all', date_from: str = '', date_to: str = '', time_field: str = 'published', timezone: str = '', task_id: str = '', profile_name: str = ''):
        timezone = query_call(timezone_name, timezone)
        if task_id:task_call(app.state.tasks.get,task_id)
        with session_scope(engine) as session:
            page = query_call(article_page, session, query=q, country=country, category=category,
                              duplicates=duplicates, limit=limit, offset=offset, mode=mode,
                              period=period, date_from=date_from, date_to=date_to, time_field=time_field, timezone=timezone,task_id=task_id,profile_name=profile_name)
            from ..translations.language import describe_language
            rows = [{'key': r.article_key, 'title': r.title, 'url': r.url, 'domain': r.source_domain,
                     'provider': r.provider, 'country': r.country, 'published_at': r.published_at, 'categories': loads(r.category_tags_json, []),
                     'sentiment': r.sentiment, 'score': r.market_relevance_score,
                     'is_duplicate': r.is_duplicate, 'snippet': r.content[:200],
                     'language_info': describe_language(r.title+'\n'+r.content[:2000], r.language),
                     'metadata_only': r.provider=='newsnow' or (r.provider in ('gdelt','gkg') and not r.content.strip()),
                     'newsnow':loads(r.raw_json,{}).get('newsnow',{}) if r.provider=='newsnow' else {}} for r in page.items]
            countries = list(session.exec(select(Article.country).distinct().order_by(Article.country)))
        params = dict(q=q, country=country, category=category, duplicates=duplicates, limit=limit, mode=mode,
                      period=period, date_from=date_from, date_to=date_to, time_field=time_field, timezone=timezone,task_id=task_id,profile_name=profile_name)
        return render('articles.html', active='articles', articles=rows, countries=countries,
                      paging=pagination('/articles', page, params), **params)

    @app.get('/articles/{article_key}', response_class=HTMLResponse)
    def page_article(article_key: str, revision: int = Query(0, ge=0), offset: int = Query(0, ge=0)):
        with session_scope(engine) as session:
            row = session.exec(select(Article).where(Article.article_key == article_key)).first()
            if row is None:
                raise HTTPException(404, 'article not found')
            history = query_call(version_page, session, article_key, offset=offset, limit=20)
            current = latest_version(session, article_key)
            selected = session.exec(select(ArticleVersion).where(ArticleVersion.article_key == article_key,
                       ArticleVersion.revision == revision)).first() if revision else current
            if selected is None:
                raise HTTPException(404, 'version not found')
            document = loads(selected.payload_json, {})
            previous = session.exec(select(ArticleVersion).where(ArticleVersion.article_key == article_key,
                        ArticleVersion.revision == selected.revision - 1)).first()
            old = loads(previous.payload_json, {}) if previous else {}
            diff = '\n'.join(unified_diff((old.get('title', '') + '\n' + old.get('content', '')).splitlines(),
                    (document.get('title', '') + '\n' + document.get('content', '')).splitlines(),
                    fromfile=f'r{selected.revision - 1}', tofile=f'r{selected.revision}', lineterm='')) if previous else ''
            from ..extraction.service import article_view
            from ..extraction.schema import LABELS
            from ..translations.language import describe_language
            language_info = describe_language(document.get('title','')+'\n'+document.get('content','')[:4000], document.get('language',''))
            return render('article_detail.html', language_info=language_info, actions=article_view(session,selected), action_labels=LABELS, active='articles', article=row, document=document,
                          selected=selected, current=current, history=history, diff=diff,
                          newsnow_info=(document.get('raw') or {}).get('newsnow',{}),discovery=session.get(GdeltDiscovery, article_key))

    from .desk import register_desk, safe_back
    from ..desk import service as desk
    from ..desk.models import Watch, RadarEntry, EventPlace
    from ..desk.places import countries as country_labels
    register_desk(app,engine,render)
    from .attention import register as register_attention
    register_attention(app,engine)
    from .workbench import register as register_workbench
    register_workbench(app,engine,storage_root,render)
    from .candidates import register as register_candidates
    register_candidates(app,engine,storage_root,render)
    from .knowledge import register as register_knowledge
    register_knowledge(app,engine,render)
    from .intake import register as register_intake
    register_intake(app,engine,storage_root,render)
    from .daily_management import register as register_daily_management
    register_daily_management(app,engine,render)
    from .hotspots import register as register_hotspots
    register_hotspots(app,engine,render)
    from .georisk import register as register_georisk
    register_georisk(app, engine, render)
    from .video_candidates import register as register_video_candidates
    register_video_candidates(app,engine,render)
    from .graph_browser import register as register_graph_browser
    register_graph_browser(app,engine,render)
    from .graph_explorer import register as register_graph_explorer
    register_graph_explorer(app,engine,render)

    @app.get('/events/{event_key}', response_class=HTMLResponse)
    def page_event_detail(event_key: str, back: str = '/radar'):
        from ..events.workbench import workspace
        from .interop import event_market_context, trade_view_settings
        from .event_window import anchor_options
        with session_scope(engine) as session:
            view = task_call(workspace, session, event_key)
            market = None
            windows = []
            if trade_view_settings() and not view['merged_into']:
                recorded_at = session.exec(select(Event.first_seen_at).where(Event.event_key == event_key)).one()
                market = event_market_context(view, recorded_at)
                windows = anchor_options(view)
            scripts = script_workbench.list_scripts(session, event_key=event_key, limit=10)
            related=desk.memberships(session,event_key)
            from ..desk.focus_lifecycle import visible_watch
            watches=[(w.key,w.name) for w in session.exec(select(Watch).where(visible_watch()).order_by(Watch.name))]
            from ..workbench.service import related as related_work
            content_tasks=related_work(session,event=event_key)
            from ..knowledge.service import status as graph_status
            knowledge=graph_status(session,app.state.desk.graph)
            card=session.get(RadarEntry,event_key)
            reports=loads(card.sources,[]) if card else []
            places=loads(card.geography,[]) if card else []
            source_groups={}
            for row in reports:
                if row.get('duplicate_of'):
                    label='系统判为副本 · 对照 '+row['duplicate_of']
                elif row.get('author'):
                    label='署名 '+row['author']
                else:
                    label='媒体 '+(row['domain'] or '待查')
                source_groups.setdefault(label,[]).append(row)
            marks=[m.model_dump() for m in session.exec(select(EventPlace).where(EventPlace.event==event_key))]
            layers={'action':[m['iso'] for m in marks if m['role']=='action'],
                    'impact':[m['iso'] for m in marks if m['role']=='impact'],
                    'media':sorted({r.get('media_country','') for r in reports if r.get('media_country')}),
                    'mention':sorted({iso for r in reports for iso in r.get('mention_iso',[])})}

        from ..extraction.schema import LABELS
        back=safe_back(back)
        return render('event_workspace.html', active='focus' if back.startswith('/focus') else 'radar',
                      view=view, scripts=scripts, timezone=timezone_name(),related=related,watches=watches,content_tasks=content_tasks,knowledge=knowledge,market=market,windows=windows,
                      back=back,action_labels=LABELS,reports=reports,source_groups=source_groups,places=places,layers=layers,marks=marks,countries=country_labels())

    @app.get('/report/{run_id}')
    def page_report(run_id: int):
        with session_scope(engine) as session:
            row = session.get(RunRecord, run_id)
            if not row or not row.storage_path:
                return render('404.html', active='runs', title='运行记录或存档不存在')
            run_dir = Path(row.storage_path)
        if not (run_dir / 'articles.jsonl').exists() and not (run_dir / 'raw.jsonl').exists():
            return render('404.html', active='runs', title='该运行没有存档文件')
        report_dir = run_dir / 'report'
        if not (report_dir / 'index.html').exists():
            try:
                build_html_report(run_dir)
            except Exception as e:
                return render('404.html', active='runs', title=f'报告生成失败: {e}')
        index = report_dir / 'index.html'
        if not index.exists():
            return render('404.html', active='runs', title='报告生成失败')
        return FileResponse(index)

    # ----------------------------------------------------------------- actions
    @app.post('/webui/run-now')
    def webui_run_now(
        profile_name: str = Form(...),
        date_str: str = Form(''),
        country: str = Form(''),
        categories: str = Form(''),
        keywords: str = Form(''),
        max_items: str = Form(''),
        timezone: str = Form(''),
    ):
        override: dict = {}
        override['timezone'] = query_call(timezone_name, timezone)
        if date_str.strip():
            override['date_str'] = date_str.strip()
        if country.strip():
            override['country'] = country.strip()
        if categories.strip():
            override['categories'] = [c.strip() for c in categories.split(',') if c.strip()]
        if keywords.strip():
            override['keywords_include'] = [k.strip() for k in keywords.split(',') if k.strip()]
        if max_items.strip().isdigit() and int(max_items) > 0:
            override['max_items'] = int(max_items)
        task = task_call(app.state.tasks.submit, profile_name.strip(), override)
        return RedirectResponse('/tasks/' + task['task_id'], status_code=303)

    @app.get('/tasks', response_class=HTMLResponse)
    def page_tasks():
        return render('tasks.html', active='tasks', timezone=timezone_name())

    @app.get('/tasks/{task_id}', response_class=HTMLResponse)
    def page_task(task_id: str):
        task = task_call(app.state.tasks.get, task_id)
        return render('task_detail.html', active='tasks', task=task)

    @app.get('/tasks/{task_id}/diagnostics', response_class=HTMLResponse)
    def page_diagnostics(task_id: str):
        with session_scope(engine) as session:
            report = task_call(task_diagnostics, session, task_id)
        return render('diagnostics.html', active='profiles', report=report)

    @app.post('/webui/jobs/{job_id}/toggle')
    def webui_toggle_job(job_id: int):
        with session_scope(engine) as session:
            job = session.get(CollectJob, job_id)
            if job is None:
                raise HTTPException(404, '计划不存在')
            task_call(set_job_enabled, session, job_id, not job.enabled)
        return RedirectResponse('/admin', status_code=303)

    @app.post('/webui/jobs')
    def webui_add_job(
        name: str = Form(...), cron_expr: str = Form('0 9 * * 1-5'),
        timezone: str = Form('UTC'), profile_name: str = Form(...),
    ):
        with session_scope(engine) as session:
            task_call(save_job, session, dict(name=name.strip(), cron_expr=cron_expr.strip(),
                      timezone=timezone.strip() or 'UTC', profile_name=profile_name.strip(), enabled=True))
        return RedirectResponse('/admin', status_code=303)

    # ------------------------------------------------- timeline (ported from v6 tools)
    @app.get('/timeline', response_class=HTMLResponse)
    def page_timeline():
        with session_scope(engine) as session:
            runs = [
                {'id': r.id, 'run_key': r.run_key, 'profile_name': r.profile_name,
                 'started_at': r.started_at, 'status': r.status,
                 'stored': loads(r.stats_json, {}).get('stored_articles', 0)}
                for r in session.exec(select(RunRecord).order_by(RunRecord.id.desc()).limit(30)).all()
                if r.storage_path and Path(r.storage_path).joinpath('articles.jsonl').exists()
            ]
        return render('timeline.html', active='timeline', runs=runs, error='')

    @app.post('/webui/timeline')
    def webui_timeline(
        markdown: str = Form(''), title: str = Form(''), time_range: str = Form(''),
        intro: str = Form(''), watermark: str = Form('@news2douyin'),
    ):
        from ..timeline import parse_timeline_md, render_html
        if not markdown.strip():
            return render('timeline.html', active='timeline', runs=[], error='请粘贴 Markdown 内容')
        doc = parse_timeline_md(markdown)
        if title.strip():
            doc.title = title.strip()
        if time_range.strip():
            doc.time_range = time_range.strip()
        if intro.strip():
            doc.intro = intro.strip()
        return HTMLResponse(render_html(doc, author_watermark=watermark.strip() or '@news2douyin'))

    @app.get('/timeline/run/{run_id}')
    def page_timeline_from_run(run_id: int):
        from ..timeline import parse_timeline_md, render_html
        with session_scope(engine) as session:
            row = session.get(RunRecord, run_id)
            if not row or not row.storage_path:
                return render('404.html', active='timeline', title='运行记录不存在')
            run_dir = Path(row.storage_path)
            profile_name = row.profile_name
            run_key = row.run_key
        art_file = run_dir / 'articles.jsonl'
        if not art_file.exists():
            return render('404.html', active='timeline', title='该运行没有入库文章')
        try:
            articles = [json.loads(l) for l in art_file.read_text(encoding='utf-8').splitlines() if l.strip()]
        except Exception:
            articles = []
        if not articles:
            return render('404.html', active='timeline', title='该运行没有入库文章')
        md = _run_articles_to_timeline_md(articles, profile_name, run_key)
        doc = parse_timeline_md(md)
        return HTMLResponse(render_html(doc, author_watermark='@news2douyin'))


def _run_articles_to_timeline_md(articles: list[dict], profile_name: str, run_key: str) -> str:
    """Build a timeline markdown (v6 format) from a run's stored articles."""
    from collections import OrderedDict
    date_part = (run_key or '').split('/')[0] or ''
    year = (date_part.split('-')[0] + '年') if len(date_part) >= 10 else '时间线'
    groups: 'OrderedDict[str, list[dict]]' = OrderedDict()
    for a in articles:
        cats = a.get('category_tags') or []
        tag = str(cats[0]) if cats else '要闻'
        groups.setdefault(tag, []).append(a)
    lines = [f'# {profile_name} 新闻时间线', '']
    if date_part:
        lines.append(f'## （{date_part}）')
    lines.append(f'> 共 {len(articles)} 条新闻 · 按分类整理 · 生成于 {run_key}')
    lines.append('')
    lines.append(f'## {year}')
    for tag, items in groups.items():
        lines.append('')
        lines.append(f'### {tag}')
        for a in items:
            title = (a.get('title') or '').replace('：', ' ')
            content = re.sub(r'\s+', ' ', (a.get('content') or '')).strip()[:160]
            lines.append(f'- **{title}**：{content}')
    return '\n'.join(lines)


def _llm_alive(timeout: float = 2.0) -> bool:
    from ..llm.settings import probe_endpoint
    return probe_endpoint(timeout)

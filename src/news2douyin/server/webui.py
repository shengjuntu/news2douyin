"""WebUI: HTML pages over the v7 API (dashboard / runs / articles / events / reports).

Pages are server-rendered with Jinja2 templates in server/templates/.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlmodel import or_, select

from .tasks import task_call
from ..report.html_report import build_html_report
from ..search.service import search_articles, search_events
from ..storage.db import session_scope
from ..storage.models import Article, ArticleEventLink, CollectJob, CollectProfile, Event, RunRecord
from ..storage.utils import loads

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

    # ------------------------------------------------------------------ pages
    @app.get('/', response_class=HTMLResponse)
    def page_dashboard():
        with session_scope(engine) as session:
            profiles = [
                {
                    'name': p.name, 'country': p.country, 'language': p.language,
                    'categories': loads(p.categories_json, []),
                    'max_items': p.max_items,
                }
                for p in session.exec(select(CollectProfile).order_by(CollectProfile.name)).all()
            ]
            jobs = [
                {
                    'id': j.id, 'name': j.name, 'enabled': j.enabled, 'cron_expr': j.cron_expr,
                    'timezone': j.timezone, 'profile_name': j.profile_name,
                    'last_run_at': j.last_run_at, 'last_status': j.last_status,
                }
                for j in session.exec(select(CollectJob).order_by(CollectJob.id)).all()
            ]
            runs = [
                {
                    'id': r.id, 'run_key': r.run_key, 'status': r.status, 'trigger_type': r.trigger_type,
                    'profile_name': r.profile_name, 'started_at': r.started_at,
                    'finished_at': r.finished_at, 'stats': loads(r.stats_json, {}),
                    'error_text': r.error_text,
                }
                for r in session.exec(select(RunRecord).order_by(RunRecord.id.desc()).limit(10)).all()
            ]
            counts = {
                'articles': len(session.exec(select(Article)).all()),
                'events': len(session.exec(select(Event)).all()),
                'runs': len(session.exec(select(RunRecord)).all()),
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
        return render('run_detail.html', active='runs', run=run, articles=articles, raw_count=raw_count)

    @app.get('/articles', response_class=HTMLResponse)
    def page_articles(q: str = '', country: str = '', category: str = '', limit: int = 60):
        limit = max(1, min(int(limit or 60), 300))
        with session_scope(engine) as session:
            rows = [
                {
                    'title': r.title, 'url': r.url, 'domain': r.source_domain, 'country': r.country,
                    'published_at': r.published_at, 'categories': loads(r.category_tags_json, []),
                    'sentiment': r.sentiment, 'score': r.market_relevance_score,
                    'is_duplicate': r.is_duplicate, 'snippet': (r.content or '')[:200],
                }
                for r in search_articles(session, query=q, country=country, category=category, limit=limit)
            ]
            countries = sorted({c for c in session.exec(select(Article.country)).all() if c})
        return render('articles.html', active='articles', articles=rows, q=q, country=country,
                      category=category, limit=limit, countries=countries)

    @app.get('/events', response_class=HTMLResponse)
    def page_events(q: str = '', country: str = '', topic: str = '', limit: int = 60):
        limit = max(1, min(int(limit or 60), 300))
        with session_scope(engine) as session:
            rows = [
                {
                    'key': r.event_key, 'title': r.event_title, 'topic': r.topic,
                    'summary': (r.summary or '')[:160], 'article_count': r.article_count,
                    'importance': r.importance, 'sentiment': r.sentiment,
                    'last_seen_at': r.last_seen_at,
                }
                for r in search_events(session, query=q, country=country, topic=topic, limit=limit)
            ]
        return render('events.html', active='events', events=rows, q=q, country=country,
                      topic=topic, limit=limit)

    @app.get('/events/{event_key}', response_class=HTMLResponse)
    def page_event_detail(event_key: str):
        with session_scope(engine) as session:
            row = session.exec(select(Event).where(Event.event_key == event_key)).first()
            if not row:
                return render('404.html', active='events', title='事件不存在')
            ev = {
                'key': row.event_key, 'title': row.event_title, 'topic': row.topic,
                'summary': row.summary, 'article_count': row.article_count,
                'importance': row.importance, 'sentiment': row.sentiment,
                'market_scope': row.market_scope, 'sectors': loads(row.sectors_json, []),
                'symbols': loads(row.symbols_json, []), 'countries': loads(row.countries_json, []),
                'first_seen_at': row.first_seen_at, 'last_seen_at': row.last_seen_at,
            }
            links = session.exec(
                select(ArticleEventLink).where(ArticleEventLink.event_key == event_key)
            ).all()
            arts = []
            for link in links:
                a = session.exec(select(Article).where(Article.article_key == link.article_key)).first()
                if a:
                    arts.append({
                        'title': a.title, 'url': a.url, 'domain': a.source_domain,
                        'published_at': a.published_at, 'relation': link.relation_type,
                        'categories': loads(a.category_tags_json, []), 'sentiment': a.sentiment,
                        'snippet': (a.content or '')[:200],
                    })
        return render('event_detail.html', active='events', ev=ev, articles=arts)

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
    ):
        override: dict = {}
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
        return render('tasks.html', active='tasks', tasks=app.state.tasks.list(100),
                      worker_running=app.state.worker.running)

    @app.get('/tasks/{task_id}', response_class=HTMLResponse)
    def page_task(task_id: str):
        task = task_call(app.state.tasks.get, task_id)
        return render('task_detail.html', active='tasks', task=task)

    @app.post('/webui/jobs/{job_id}/toggle')
    def webui_toggle_job(job_id: int):
        with session_scope(engine) as session:
            job = session.get(CollectJob, job_id)
            if job:
                job.enabled = not job.enabled
                session.add(job)
                session.commit()
        return RedirectResponse('/', status_code=303)

    @app.post('/webui/jobs')
    def webui_add_job(
        name: str = Form(...), cron_expr: str = Form('0 9 * * 1-5'),
        timezone: str = Form('UTC'), profile_name: str = Form(...),
    ):
        with session_scope(engine) as session:
            if session.exec(select(CollectJob).where(CollectJob.name == name.strip())).first() is None:
                session.add(CollectJob(
                    name=name.strip(), cron_expr=cron_expr.strip(), timezone=timezone.strip() or 'UTC',
                    profile_name=profile_name.strip(),
                ))
                session.commit()
        return RedirectResponse('/', status_code=303)

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

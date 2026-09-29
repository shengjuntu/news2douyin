from __future__ import annotations

from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from sqlmodel import select

from ..collect.service import create_or_update_profile, import_profile_file, row_to_profile
from ..collect.profiles import PROFILE_FIELDS
from ..editorial.service import build_editorial_pack
from ..search.service import search_articles, search_events
from ..scheduler.service import SchedulerConfig, SchedulerService
from ..storage.db import make_engine, init_db, session_scope
from ..storage.models import CollectProfile, CollectJob, RunRecord, Event, ScriptPackage
from ..storage.utils import loads
from .schemas.common import ProfilePayload, JobPayload
from ..config import load_environment
from ..tasks.service import TaskService
from ..tasks.worker import TaskWorker
from .tasks import register_task_routes


def _profile_to_dict(row: CollectProfile) -> dict:
    return {
        'name': row.name,
        'provider': row.provider,
        'country': row.country,
        'language': row.language,
        'categories': loads(row.categories_json, []),
        'keywords_include': loads(row.keywords_include_json, []),
        'keywords_exclude': loads(row.keywords_exclude_json, []),
        'source_whitelist': loads(row.whitelist_domains_json, []),
        'source_blacklist': loads(row.blacklist_domains_json, []),
        'max_items': row.max_items,
        'market_scope': row.market_scope,
        'market_tags': loads(row.market_tags_json, []),
        'extra': {k: v for k, v in row_to_profile(row).items() if k not in PROFILE_FIELDS},
        'updated_at': row.updated_at,
    }


def _job_to_dict(row: CollectJob) -> dict:
    return {
        'id': row.id,
        'name': row.name,
        'enabled': row.enabled,
        'timezone': row.timezone,
        'cron_expr': row.cron_expr,
        'profile_name': row.profile_name,
        'auto_editorial': row.auto_editorial,
        'auto_video': row.auto_video,
        'auto_tts': row.auto_tts,
        'last_run_at': row.last_run_at,
        'last_status': row.last_status,
    }


def _run_to_dict(row: RunRecord) -> dict:
    return {
        'id': row.id,
        'run_key': row.run_key,
        'job_id': row.job_id,
        'trigger_type': row.trigger_type,
        'profile_name': row.profile_name,
        'started_at': row.started_at,
        'finished_at': row.finished_at,
        'status': row.status,
        'stats': loads(row.stats_json, {}),
        'error_text': row.error_text,
        'storage_path': row.storage_path,
    }


def create_app(*, db_url: str = 'sqlite:///runs_v7/news2douyin_v7.db', storage_root: str = 'runs_v7') -> FastAPI:
    load_environment()
    engine = make_engine(db_url)
    init_db(engine)
    scheduler = SchedulerService(engine, SchedulerConfig(storage_root=storage_root))
    tasks = TaskService(engine)
    worker = TaskWorker(engine, storage_root)
    @asynccontextmanager
    async def lifespan(app):
        Path(storage_root).mkdir(parents=True, exist_ok=True)
        worker.start()
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()
            worker.stop()

    app = FastAPI(title='news2douyin v7 server', lifespan=lifespan)
    app.state.engine = engine
    app.state.storage_root = storage_root
    app.state.scheduler = scheduler
    app.state.tasks = tasks
    app.state.worker = worker
    register_task_routes(app, tasks, worker, _run_to_dict)

    @app.get('/api/health')
    def health():
        return {'ok': True, 'service': 'news2douyin-v7'}

    @app.get('/api/system/status')
    def system_status():
        with session_scope(engine) as session:
            return {
                'profiles': len(list(session.exec(select(CollectProfile)))),
                'jobs': len(list(session.exec(select(CollectJob)))),
                'runs': len(list(session.exec(select(RunRecord)))),
                'events': len(list(session.exec(select(Event)))),
                'scheduler_running': scheduler._thread is not None and scheduler._thread.is_alive(),
                'storage_root': storage_root,
                'worker_running': worker.running,
            }

    @app.get('/api/scheduler/status')
    def scheduler_status():
        return {
            'running': scheduler._thread is not None and scheduler._thread.is_alive(),
            'interval_sec': scheduler.config.interval_sec,
            'storage_root': scheduler.config.storage_root,
            'pending_minute_keys': len(getattr(scheduler, '_fired_keys', set())),
        }

    @app.get('/api/profiles')
    def list_profiles():
        with session_scope(engine) as session:
            rows = list(session.exec(select(CollectProfile).order_by(CollectProfile.name)))
            return [_profile_to_dict(r) for r in rows]

    @app.get('/api/profiles/{name}')
    def get_profile_api(name: str):
        with session_scope(engine) as session:
            row = session.exec(select(CollectProfile).where(CollectProfile.name == name)).first()
            if not row:
                raise HTTPException(status_code=404, detail='profile not found')
            return _profile_to_dict(row)

    @app.post('/api/profiles')
    def upsert_profile(payload: ProfilePayload):
        with session_scope(engine) as session:
            row = create_or_update_profile(session, payload.model_dump())
            return _profile_to_dict(row)

    @app.post('/api/profiles/import')
    def import_profile(path: str):
        with session_scope(engine) as session:
            row = import_profile_file(session, path)
            return _profile_to_dict(row)

    @app.get('/api/jobs')
    def list_jobs():
        with session_scope(engine) as session:
            rows = list(session.exec(select(CollectJob).order_by(CollectJob.id.desc())))
            return [_job_to_dict(r) for r in rows]

    @app.post('/api/jobs')
    def upsert_job(payload: JobPayload):
        with session_scope(engine) as session:
            row = session.exec(select(CollectJob).where(CollectJob.name == payload.name)).first()
            if not row:
                row = CollectJob(name=payload.name)
                session.add(row)
            row.enabled = payload.enabled
            row.timezone = payload.timezone
            row.cron_expr = payload.cron_expr
            row.profile_name = payload.profile_name
            row.auto_editorial = payload.auto_editorial
            row.auto_video = payload.auto_video
            row.auto_tts = payload.auto_tts
            session.add(row)
            session.commit(); session.refresh(row)
            return _job_to_dict(row)

    @app.post('/api/jobs/{job_id}/enable')
    def enable_job(job_id: int):
        with session_scope(engine) as session:
            row = session.get(CollectJob, job_id)
            if not row:
                raise HTTPException(status_code=404, detail='job not found')
            row.enabled = True
            session.add(row); session.commit(); session.refresh(row)
            return _job_to_dict(row)

    @app.post('/api/jobs/{job_id}/disable')
    def disable_job(job_id: int):
        with session_scope(engine) as session:
            row = session.get(CollectJob, job_id)
            if not row:
                raise HTTPException(status_code=404, detail='job not found')
            row.enabled = False
            session.add(row); session.commit(); session.refresh(row)
            return _job_to_dict(row)

    @app.get('/api/runs')
    def list_runs(limit: int = 50):
        with session_scope(engine) as session:
            rows = list(session.exec(select(RunRecord).order_by(RunRecord.id.desc()).limit(limit)))
            return [_run_to_dict(r) for r in rows]

    @app.get('/api/runs/{run_id}')
    def get_run(run_id: int):
        with session_scope(engine) as session:
            row = session.get(RunRecord, run_id)
            if not row:
                raise HTTPException(status_code=404, detail='run not found')
            return _run_to_dict(row)

    @app.get('/api/articles/search')
    def api_search_articles(query: str = '', country: str = '', category: str = '', duplicates: str = 'any', limit: int = 50):
        with session_scope(engine) as session:
            rows = search_articles(session, query=query, country=country, category=category, duplicates=duplicates, limit=limit)
            return [
                {
                    'article_key': r.article_key,
                    'title': r.title,
                    'source_domain': r.source_domain,
                    'country': r.country,
                    'published_at': r.published_at,
                    'market_relevance_score': r.market_relevance_score,
                    'is_duplicate': r.is_duplicate,
                    'dedup_reason': r.dedup_reason,
                }
                for r in rows
            ]

    @app.get('/api/events/search')
    def api_search_events(query: str = '', country: str = '', topic: str = '', limit: int = 50):
        with session_scope(engine) as session:
            rows = search_events(session, query=query, country=country, topic=topic, limit=limit)
            return [
                {
                    'event_key': r.event_key,
                    'event_title': r.event_title,
                    'topic': r.topic,
                    'summary': r.summary,
                    'article_count': r.article_count,
                    'importance': r.importance,
                    'last_seen_at': r.last_seen_at,
                }
                for r in rows
            ]

    @app.get('/api/events/{event_key}')
    def api_get_event(event_key: str):
        with session_scope(engine) as session:
            row = session.exec(select(Event).where(Event.event_key == event_key)).first()
            if not row:
                raise HTTPException(status_code=404, detail='event not found')
            return {
                'event_key': row.event_key,
                'event_title': row.event_title,
                'topic': row.topic,
                'summary': row.summary,
                'article_count': row.article_count,
                'importance': row.importance,
                'sentiment': row.sentiment,
                'market_scope': row.market_scope,
                'sectors': loads(row.sectors_json, []),
                'symbols': loads(row.symbols_json, []),
                'countries': loads(row.countries_json, []),
                'first_seen_at': row.first_seen_at,
                'last_seen_at': row.last_seen_at,
            }

    @app.post('/api/editorial/build')
    def api_build_editorial(event_key: str):
        with session_scope(engine) as session:
            return build_editorial_pack(session, event_key)

    from .scripts import register_script_routes
    register_script_routes(app, engine, storage_root)
    from .video import register_video_routes
    register_video_routes(app, engine, storage_root)

    # HTML WebUI pages (dashboard / runs / articles / events / timeline / reports)
    from .webui import register_webui_routes
    register_webui_routes(app, engine, scheduler, storage_root)

    return app

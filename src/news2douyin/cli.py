from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from .pipeline import run_all
from .report import build_html_report
from .assets.prep import prep_assets
from .tts.synth import synthesize
from .server.app import create_app
from .storage.db import make_engine, init_db, session_scope
from .storage.models import CollectJob
from .collect.service import import_profile_file, run_collection
from .search.service import search_articles, search_events
from .video.service import build_script_package
from .storage.utils import loads
from .config import load_yaml


def _find_latest_run(runs_root: Path = Path("runs")) -> Path | None:
    if not runs_root.exists():
        return None
    candidates = []
    for day in sorted([p for p in runs_root.iterdir() if p.is_dir()], reverse=True):
        for run in sorted([p for p in day.iterdir() if p.is_dir() and p.name.startswith("run_")], reverse=True):
            candidates.append(run)
    return candidates[0] if candidates else None


def _iter_event_dirs(out_dir: Path) -> list[Path]:
    if not out_dir.exists():
        return []
    return sorted([p for p in out_dir.iterdir() if p.is_dir()], key=lambda x: x.name)


def _upsert_jobs_from_yaml(session, path: str | Path) -> int:
    data = load_yaml(path)
    jobs = data.get('jobs', []) if isinstance(data, dict) else []
    n = 0
    for item in jobs:
        row = session.query(CollectJob).filter(CollectJob.name == item['name']).first() if hasattr(session, 'query') else None
        if row is None:
            from sqlmodel import select
            row = session.exec(select(CollectJob).where(CollectJob.name == item['name'])).first()
        if not row:
            row = CollectJob(name=item['name'])
            session.add(row)
        row.enabled = bool(item.get('enabled', True))
        row.timezone = item.get('timezone', 'UTC')
        row.cron_expr = item.get('cron_expr', '0 9 * * 1-5')
        row.profile_name = item['profile_name']
        row.auto_editorial = bool(item.get('auto_editorial', False))
        row.auto_video = bool(item.get('auto_video', False))
        row.auto_tts = bool(item.get('auto_tts', False))
        session.add(row)
        n += 1
    session.commit()
    return n


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="news2douyin")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # legacy v6
    p_run = sub.add_parser("run", help="Run full legacy pipeline (collect -> select -> scripts)")
    p_run.add_argument("--config", required=True)

    p_prep = sub.add_parser("prep-assets", help="Prepare per-event asset folders from a run directory")
    p_prep.add_argument("--run-dir", default="", help="Run dir. If empty, uses latest run under ./runs")
    p_prep.add_argument("--llm", default="none", help="LLM mode for script generation (project-specific)")

    p_tts = sub.add_parser("tts", help="Synthesize TTS for prepared assets")
    p_tts.add_argument("--run-dir", default="", help="Run directory containing assets/ (default: latest run under ./runs)")
    p_tts.add_argument("--lang", choices=["zh","en","both"], default="both")
    p_tts.add_argument("--backend", default="auto", help="auto|edge-tts|pyttsx3")
    p_tts.add_argument("--force", action="store_true", help="Re-generate even if audio already exists")

    p_report = sub.add_parser("report", help="Generate a static HTML report for a run directory")
    p_report.add_argument("--run-dir", required=True, help="Path to a run dir, e.g. runs/2026-01-25/run_1340")
    p_report.add_argument("--out", default="report", help="Output folder name under run-dir (default: report)")

    # v7 server-first
    p_init = sub.add_parser('v7-init', help='Initialize v7 DB and optionally import profiles/jobs')
    p_init.add_argument('--db-url', default='sqlite:///runs_v7/news2douyin_v7.db')
    p_init.add_argument('--profile', action='append', default=[])
    p_init.add_argument('--jobs-yaml', default='')

    p_serve = sub.add_parser('v7-serve', help='Run v7 FastAPI server')
    p_serve.add_argument('--host', default='127.0.0.1')
    p_serve.add_argument('--port', type=int, default=18080)
    p_serve.add_argument('--db-url', default='sqlite:///runs_v7/news2douyin_v7.db')
    p_serve.add_argument('--storage-root', default='runs_v7')

    p_collect = sub.add_parser('v7-run-now', help='Run one v7 collection now')
    p_collect.add_argument('--db-url', default='sqlite:///runs_v7/news2douyin_v7.db')
    p_collect.add_argument('--storage-root', default='runs_v7')
    p_collect.add_argument('--profile-name', required=True)
    p_collect.add_argument('--override-json', default='{}')

    p_sa = sub.add_parser('v7-search-articles', help='Search stored articles')
    p_sa.add_argument('--db-url', default='sqlite:///runs_v7/news2douyin_v7.db')
    p_sa.add_argument('--query', default='')
    p_sa.add_argument('--country', default='')
    p_sa.add_argument('--category', default='')
    p_sa.add_argument('--duplicates', default='any')
    p_sa.add_argument('--limit', type=int, default=20)

    p_se = sub.add_parser('v7-search-events', help='Search stored events')
    p_se.add_argument('--db-url', default='sqlite:///runs_v7/news2douyin_v7.db')
    p_se.add_argument('--query', default='')
    p_se.add_argument('--country', default='')
    p_se.add_argument('--topic', default='')
    p_se.add_argument('--limit', type=int, default=20)

    p_sb = sub.add_parser('v7-build-script', help='Build a script package from an event')
    p_sb.add_argument('--db-url', default='sqlite:///runs_v7/news2douyin_v7.db')
    p_sb.add_argument('--storage-root', default='runs_v7')
    p_sb.add_argument('--event-key', required=True)
    p_sb.add_argument('--profile-name', default='douyin_market_60s')

    args = ap.parse_args()

    if args.cmd == "run":
        try:
            run_dir = run_all(args.config)
            logger.success(f"Run completed: {run_dir}")
            print(str(run_dir))
        except Exception:
            logger.exception("Run failed")
            print("ERROR: run failed (see logs in run_dir/logs if created)", file=sys.stderr)
            raise
        return

    if args.cmd == "prep-assets":
        try:
            run_dir = Path(args.run_dir) if args.run_dir else _find_latest_run(Path("runs"))
            if run_dir is None:
                raise FileNotFoundError("No run directory found under ./runs")
            assets_dir = prep_assets(run_dir, None, llm=args.llm)
            logger.success(f"Assets prepared: {assets_dir}")
            print(str(assets_dir))
        except Exception:
            logger.exception("prep-assets failed")
            print("ERROR: prep-assets failed", file=sys.stderr)
            raise
        return

    if args.cmd == "tts":
        try:
            run_dir = Path(args.run_dir) if args.run_dir else _find_latest_run(Path("runs"))
            if run_dir is None:
                raise FileNotFoundError("No run directory found under ./runs")
            assets_dir = run_dir / "assets"
            if not assets_dir.exists():
                raise FileNotFoundError(f"assets dir not found: {assets_dir} (run prep-assets first)")
            ev_dirs = _iter_event_dirs(assets_dir)
            if not ev_dirs:
                raise FileNotFoundError(f"No event folders found in {assets_dir}")
            langs = ["zh","en"] if args.lang == "both" else [args.lang]
            for ev in ev_dirs:
                for lang in langs:
                    script = ev / (f"script_{lang}.txt")
                    if not script.exists():
                        logger.warning(f"Missing script: {script}")
                        continue
                    audio_mp3 = ev / f"voice_{lang}.mp3"
                    audio_wav = ev / f"voice_{lang}.wav"
                    if not args.force and (audio_mp3.exists() or audio_wav.exists()):
                        logger.info(f"Skip existing audio: {ev.name} {lang}")
                        continue
                    text = script.read_text(encoding="utf-8")
                    backend, outp = synthesize(text, audio_mp3, backend=args.backend, voice=None, lang=lang, preprocess_markdown=True)
                    logger.success(f"TTS {lang}: {ev.name} -> {outp} ({backend})")
            print(str(assets_dir))
        except Exception:
            logger.exception("tts failed")
            print("ERROR: tts failed", file=sys.stderr)
            raise
        return

    if args.cmd == "report":
        try:
            out = build_html_report(Path(args.run_dir), args.out)
            logger.success(f"Report created: {out / 'index.html'}")
            print(str(out / "index.html"))
        except Exception:
            logger.exception("Report failed")
            print("ERROR: report failed", file=sys.stderr)
            raise
        return

    if args.cmd == 'v7-init':
        engine = make_engine(args.db_url)
        init_db(engine)
        imported = 0
        jobs = 0
        with session_scope(engine) as session:
            for path in args.profile:
                import_profile_file(session, path)
                imported += 1
            if args.jobs_yaml:
                jobs = _upsert_jobs_from_yaml(session, args.jobs_yaml)
        print(json.dumps({'db_url': args.db_url, 'profiles_imported': imported, 'jobs_imported': jobs}, ensure_ascii=False))
        return

    if args.cmd == 'v7-serve':
        import uvicorn
        app = create_app(db_url=args.db_url, storage_root=args.storage_root)
        uvicorn.run(app, host=args.host, port=args.port)
        return

    if args.cmd == 'v7-run-now':
        engine = make_engine(args.db_url)
        init_db(engine)
        override = json.loads(args.override_json or '{}')
        with session_scope(engine) as session:
            run = run_collection(session, args.profile_name, storage_root=args.storage_root, override=override)
            print(json.dumps({'run_key': run.run_key, 'status': run.status, 'stats': loads(run.stats_json, {})}, ensure_ascii=False, indent=2))
        return

    if args.cmd == 'v7-search-articles':
        engine = make_engine(args.db_url)
        with session_scope(engine) as session:
            rows = search_articles(session, query=args.query, country=args.country, category=args.category, duplicates=args.duplicates, limit=args.limit)
            print(json.dumps([
                {
                    'article_key': r.article_key,
                    'title': r.title,
                    'source_domain': r.source_domain,
                    'country': r.country,
                    'published_at': r.published_at,
                    'market_relevance_score': r.market_relevance_score,
                    'is_duplicate': r.is_duplicate,
                    'dedup_reason': r.dedup_reason,
                } for r in rows
            ], ensure_ascii=False, indent=2))
        return

    if args.cmd == 'v7-search-events':
        engine = make_engine(args.db_url)
        with session_scope(engine) as session:
            rows = search_events(session, query=args.query, country=args.country, topic=args.topic, limit=args.limit)
            print(json.dumps([
                {
                    'event_key': r.event_key,
                    'event_title': r.event_title,
                    'topic': r.topic,
                    'summary': r.summary,
                    'importance': r.importance,
                    'article_count': r.article_count,
                } for r in rows
            ], ensure_ascii=False, indent=2))
        return

    if args.cmd == 'v7-build-script':
        engine = make_engine(args.db_url)
        with session_scope(engine) as session:
            pkg = build_script_package(session, args.event_key, profile_name=args.profile_name, output_root=Path(args.storage_root) / 'packages')
            print(json.dumps({'package_key': pkg.package_key, 'output_dir': pkg.output_dir}, ensure_ascii=False, indent=2))
        return


if __name__ == "__main__":
    main()

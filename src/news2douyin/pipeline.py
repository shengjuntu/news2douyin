from __future__ import annotations

import json
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Type

from loguru import logger

from .logging import setup_logger
from .config import load_yaml
from .export.render import save_script_pack
from .export.script_builder import build_script_from_story
from .export.script_builder_llm import build_script_from_story_llm
from .ingest.worldnewsapi import fetch_top_news
from .store.io_jsonl import write_jsonl, write_json
from .storyline.builder_llm import build_storypacks
from .storyline.cluster import cluster_by_event_id
from .triage.rules import apply_rules
from .triage.scorer_llm import score_items
from .utils.timeutil import today_str, now_hhmm


# -----------------------------
# Reliability helpers
# -----------------------------



@contextmanager
def stage(name: str, *, log: Any = logger):
    """Timing + structured stage logs."""
    t0 = time.perf_counter()
    log.info(f"[{name}] ▶ start")
    try:
        yield
    except Exception:
        log.exception(f"[{name}] ✖ failed")
        raise
    finally:
        dt = time.perf_counter() - t0
        log.info(f"[{name}] ✔ done in {dt:.2f}s")


def _retry_call(
    fn: Callable[..., Any],
    *args: Any,
    retries: int = 2,
    backoff_s: float = 1.5,
    retry_exceptions: tuple[Type[BaseException], ...] = (Exception,),
    log: Any = logger,
    **kwargs: Any,
) -> Any:
    """Simple retry wrapper with exponential backoff."""
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except retry_exceptions as e:
            attempt += 1
            if attempt > retries:
                raise
            sleep_s = backoff_s * (2 ** (attempt - 1))
            log.warning(
                f"Retrying after error ({attempt}/{retries}) in {sleep_s:.1f}s: {type(e).__name__}: {e}"
            )
            time.sleep(sleep_s)


def _safe_write_json(path: Path, obj: Any, *, log: Any = logger) -> None:
    try:
        write_json(path, obj)
    except Exception:
        log.exception(f"Failed to write JSON: {path}")
        raise


def _safe_write_jsonl(path: Path, items: Iterable[dict[str, Any]], *, log: Any = logger) -> None:
    try:
        write_jsonl(path, list(items))
    except Exception:
        log.exception(f"Failed to write JSONL: {path}")
        raise


# -----------------------------
# Pipeline
# -----------------------------

def make_run_paths(cfg: dict, date_str: str, hhmm: str) -> dict[str, Path]:
    run_dir = Path(cfg["project"]["run_dir"]) / date_str / f"run_{hhmm}"
    return {
        "run_dir": run_dir,
        "raw": run_dir / "raw.jsonl",
        "dedup": run_dir / "dedup.jsonl",
        "scored": run_dir / "scored.jsonl",
        "selected": run_dir / "selected.json",
        "storypacks": run_dir / "storypacks.jsonl",
        "scriptpacks": run_dir / "scriptpacks",
        "meta": run_dir / "meta.json",
        "errors": run_dir / "errors.json",
        "logs": run_dir / "logs",
    }


def run_all(config_path: str) -> Path:
    # Load config
    cfg = load_yaml(config_path)

    # Resolve date
    ingest_block = cfg.get("ingest") or {}
    date_mode = ingest_block.get("date_mode", "today")
    tz = (cfg.get("project") or {}).get("timezone")
    date_str = ingest_block.get("fixed_date") if date_mode == "fixed" else today_str(tz)
    hhmm = now_hhmm()

    paths = make_run_paths(cfg, date_str, hhmm)
    run_dir = paths["run_dir"]
    run_dir.mkdir(parents=True, exist_ok=True)
    paths["scriptpacks"].mkdir(parents=True, exist_ok=True)

    # Setup logging
    log_level = ((cfg.get("logging") or {}).get("level")) or "INFO"
    setup_logger(paths["logs"], level=str(log_level).upper())

    run_logger = logger.bind(run_id=run_dir.name, date=date_str)
    run_logger.info("=" * 88)
    run_logger.info("🚀 Pipeline started")
    run_logger.info(f"Config path: {config_path}")
    run_logger.info(f"Run dir: {run_dir}")
    run_logger.info(f"Timezone: {tz}")
    run_logger.info(f"Date mode: {date_mode} | date_str={date_str} | hhmm={hhmm}")

    # Meta + errors accumulator
    meta: dict[str, Any] = {
        "config_path": config_path,
        "date_mode": date_mode,
        "date_str": date_str,
        "hhmm": hhmm,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {},
        "durations_s": {},
    }
    errors: list[dict[str, Any]] = []

    # Retry policy (override-able via cfg.reliability)
    rel = cfg.get("reliability") or {}
    retries_api = int(rel.get("retries_api", 2))
    retries_llm = int(rel.get("retries_llm", 1))
    backoff_s = float(rel.get("backoff_s", 1.5))

    def record_error(stage_name: str, exc: BaseException, extra: dict[str, Any] | None = None):
        item = {
            "stage": stage_name,
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(limit=50),
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        if extra:
            item.update(extra)
        errors.append(item)

    # -----------------
    # INGEST
    # -----------------
    with stage("INGEST", log=run_logger):
        ingest_cfg = dict(ingest_block)
        ingest_cfg["date_str"] = date_str

        t0 = time.perf_counter()
        raw_items = _retry_call(
            fetch_top_news,
            ingest_cfg,
            retries=retries_api,
            backoff_s=backoff_s,
            log=run_logger,
        )
        meta["durations_s"]["ingest_fetch"] = round(time.perf_counter() - t0, 3)

        min_chars = int(ingest_cfg.get("min_content_chars", 0))
        before = len(raw_items)
        if min_chars > 0:
            raw_items = [it for it in raw_items if len((it.get("content") or "")) >= min_chars]
        run_logger.info(f"[INGEST] Items: {before} → {len(raw_items)} (min_content_chars={min_chars})")

        _safe_write_jsonl(paths["raw"], raw_items, log=run_logger)

        meta["counts"]["raw"] = len(raw_items)

    # -----------------
    # TRIAGE
    # -----------------
    with stage("TRIAGE", log=run_logger):
        rules_cfg = cfg.get("triage_rules") or {}
        before = len(raw_items)

        dedup_items = apply_rules(raw_items, rules_cfg)

        run_logger.info(f"[TRIAGE] Items: {before} → {len(dedup_items)}")
        _safe_write_jsonl(paths["dedup"], dedup_items, log=run_logger)

        meta["counts"]["dedup"] = len(dedup_items)

    # -----------------
    # SCORE (LLM)
    # -----------------
    llm_cfg = cfg.get("llm") or {}
    scoring_cfg = cfg.get("scoring") or {}
    prompts_cfg = cfg.get("prompts") or {}

    with stage("SCORING", log=run_logger):
        # Optional: process in chunks for resilience (if configured)
        chunk_size = int(scoring_cfg.get("chunk_size", 0))
        if chunk_size > 0:
            scored_items: list[dict[str, Any]] = []
            for i in range(0, len(dedup_items), chunk_size):
                chunk = dedup_items[i : i + chunk_size]
                run_logger.info(f"[SCORING] Chunk {i//chunk_size + 1} | size={len(chunk)}")
                chunk_scored = _retry_call(
                    score_items,
                    chunk,
                    llm_cfg,
                    scoring_cfg,
                    prompts_cfg,
                    retries=retries_llm,
                    backoff_s=backoff_s,
                    log=run_logger,
                )
                scored_items.extend(chunk_scored)
        else:
            scored_items = _retry_call(
                score_items,
                dedup_items,
                llm_cfg,
                scoring_cfg,
                prompts_cfg,
                retries=retries_llm,
                backoff_s=backoff_s,
                log=run_logger,
            )

        _safe_write_jsonl(paths["scored"], scored_items, log=run_logger)
        meta["counts"]["scored"] = len(scored_items)

        # Quick distribution stats (helps catch scoring bugs)
        vs = [float(it.get("value_score", 0) or 0) for it in scored_items]
        if vs:
            meta["value_score_stats"] = {
                "min": min(vs),
                "max": max(vs),
                "avg": sum(vs) / len(vs),
            }
            run_logger.info(
                f"[SCORING] value_score stats: min={meta['value_score_stats']['min']:.3f}, "
                f"avg={meta['value_score_stats']['avg']:.3f}, max={meta['value_score_stats']['max']:.3f}"
            )

    # -----------------
    # SELECT
    # -----------------
    with stage("SELECT", log=run_logger):
        top_k = int(scoring_cfg.get("top_k", 8))
        min_value = float(scoring_cfg.get("min_value_score", 0))

        kept = [s for s in scored_items if float(s.get("value_score", 0) or 0) >= min_value]
        kept.sort(key=lambda x: float(x.get("value_score", 0) or 0), reverse=True)
        kept = kept[:top_k]

        run_logger.info(f"[SELECT] Kept {len(kept)} items (min_value_score={min_value}, top_k={top_k})")

        _safe_write_json(paths["selected"], {"top_k": top_k, "min_value_score": min_value, "items": kept}, log=run_logger)
        meta["counts"]["selected"] = len(kept)

    # -----------------
    # STORYLINE (cluster + LLM storypacks)
    # -----------------
    with stage("STORYLINE", log=run_logger):
        raw_by_id = {it.get("id"): it for it in raw_items if it.get("id") is not None}

        cluster_cfg = ((cfg.get("storyline") or {}).get("cluster") or {})
        max_items_per_event = int(cluster_cfg.get("max_items_per_event", 5))

        bundles = cluster_by_event_id(kept, raw_by_id, max_items_per_event=max_items_per_event)
        meta["counts"]["event_bundles"] = len(bundles)
        run_logger.info(f"[STORYLINE] Bundles: {len(bundles)} (max_items_per_event={max_items_per_event})")

        story_cfg = ((cfg.get("storyline") or {}).get("storypack") or {})
        storypacks = _retry_call(
            build_storypacks,
            bundles,
            llm_cfg,
            story_cfg,
            prompts_cfg,
            retries=retries_llm,
            backoff_s=backoff_s,
            log=run_logger,
        )
        _safe_write_jsonl(paths["storypacks"], storypacks, log=run_logger)
        meta["counts"]["storypacks"] = len(storypacks)

    # -----------------
    # EXPORT (per-event, best-effort)
    # -----------------
    with stage("EXPORT", log=run_logger):
        export_cfg = cfg.get("export") or {}
        script_cfg = (cfg.get("script") or {})
        script_mode = str(script_cfg.get("mode", "rule")).lower()
        templates_dir = Path(export_cfg.get("templates_dir", "templates/douyin"))
        default_template = export_cfg.get("default_template", "fast_news.j2")
        templates_map = export_cfg.get("templates") or {}
        hashtags_default = export_cfg.get("hashtags_default") or ["#新闻"]
        safe_disclaimer = bool(export_cfg.get("safe_disclaimer", True))
        out_formats = set(export_cfg.get("output_formats") or ["json", "md"])

        exported = 0
        failed = 0

        for idx, sp in enumerate(storypacks, start=1):
            event_id = sp.get("event_id") or "event_unknown"
            style = sp.get("recommended_style") or "fast_news"
            tmpl_file = (templates_map.get(style, {}) or {}).get("file", default_template)
            template_path = templates_dir / tmpl_file

            event_logger = run_logger.bind(event_id=str(event_id), style=str(style))
            event_logger.info(f"[EXPORT] ({idx}/{len(storypacks)}) event={event_id} style={style} template={template_path}")

            try:
                if script_mode == "llm":
                    script_pack = build_script_from_story_llm(
                        sp,
                        llm_cfg=llm_cfg,
                        prompts_cfg=prompts_cfg,
                        script_cfg=script_cfg,
                        export_cfg=export_cfg,
                    )
                else:
                    script_pack = build_script_from_story(
                        sp,
                        hashtags_default=hashtags_default,
                        safe_disclaimer=safe_disclaimer,
                    )
                out_dir = paths["scriptpacks"] / f"event_{event_id}"
                save_script_pack(
                    out_dir,
                    script_pack,
                    template_path,
                    write_json=("json" in out_formats),
                    write_md=("md" in out_formats),
                )
                exported += 1
            except Exception as e:
                failed += 1
                event_logger.exception("[EXPORT] Failed exporting this event (continuing)")
                record_error("EXPORT", e, extra={"event_id": event_id, "style": style, "template": str(template_path)})

        meta["counts"]["exported"] = exported
        meta["counts"]["export_failed"] = failed
        run_logger.info(f"[EXPORT] Exported={exported}, failed={failed}")

    # -----------------
    # Finalize
    # -----------------
    meta["finished_at"] = datetime.now().isoformat(timespec="seconds")

    _safe_write_json(paths["meta"], meta, log=run_logger)
    if errors:
        _safe_write_json(paths["errors"], {"errors": errors}, log=run_logger)

    if errors:
        run_logger.warning(f"Pipeline finished with {len(errors)} error(s). See: {paths['errors']}")
    else:
        run_logger.success("✅ Pipeline finished successfully")

    return run_dir

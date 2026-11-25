from __future__ import annotations

from pathlib import Path
from contextlib import nullcontext
import os

from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import make_url


def default_db_url() -> str:
    """Resolve the default without silently reopening a legacy production file.

    SQLite remains available for local development and explicitly addressed
    offline migration sources. Formal Compose enables this guard; it checks
    default runtime configuration before any SQLite directory/file is opened.
    """
    value = os.getenv('NEWS2DOUYIN_DATABASE_URL', '').strip()
    flag = os.getenv('NEWS2DOUYIN_DATABASE_REQUIRE_POSTGRES', '0').strip().lower() or '0'
    if flag not in ('0', '1', 'false', 'true', 'off', 'on', 'no', 'yes'):
        raise ValueError('NEWS2DOUYIN_DATABASE_REQUIRE_POSTGRES must be a boolean')
    if flag in ('1', 'true', 'on', 'yes'):
        try:
            valid = bool(value) and make_url(value).get_backend_name() in ('postgresql', 'postgres')
        except Exception:
            valid = False
        if not valid:
            # Never include a configured URL/credential in the error message.
            raise ValueError('NEWS2DOUYIN_DATABASE_URL must be a PostgreSQL URL when '
                             'NEWS2DOUYIN_DATABASE_REQUIRE_POSTGRES=1')
    return value or 'sqlite:///runs_v7/news2douyin_v7.db'


def _integer(name: str, default: int, low: int, high: int) -> int:
    value = os.getenv(name, '').strip() or str(default)
    try:
        result = int(value)
    except ValueError:
        raise ValueError(f'{name} must be an integer') from None
    if not low <= result <= high:
        raise ValueError(f'{name} must be between {low} and {high}')
    return result


def normalize_db_url(db_url: str) -> str:
    if db_url.startswith('postgres://'):
        db_url = 'postgresql+psycopg://' + db_url[len('postgres://'):]
    elif db_url.startswith('postgresql://'):
        db_url = 'postgresql+psycopg://' + db_url[len('postgresql://'):]
    url = make_url(db_url)
    if url.get_backend_name() == 'sqlite' and url.database not in (None, '', ':memory:'):
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    return db_url


def make_engine(db_url: str | None = None, *, maintenance: bool = False):
    db_url = normalize_db_url(db_url or default_db_url())
    url = make_url(db_url)
    dialect = url.get_backend_name()
    if dialect == 'sqlite':
        timeout = _integer('NEWS2DOUYIN_SQLITE_BUSY_TIMEOUT_MS', 30000, 0, 60000)
        memory = url.database in (None, '', ':memory:')
        # Queue capacity is connection reuse/concurrent reading capacity;
        # SQLite still has one writer. Keep its file-backed defaults modest.
        # In-memory SQLite retains SQLAlchemy's original per-thread pool and
        # must not receive QueuePool-only overflow/timeout arguments.
        pool = {} if memory else dict(
            pool_size=_integer('NEWS2DOUYIN_DB_POOL_SIZE', 5, 1, 64),
            max_overflow=_integer('NEWS2DOUYIN_DB_MAX_OVERFLOW', 10, 0, 64),
            pool_timeout=_integer('NEWS2DOUYIN_DB_POOL_TIMEOUT', 10, 0, 300))
        engine = create_engine(db_url, echo=False, hide_parameters=True, **pool, connect_args={
            'check_same_thread': False, 'timeout': timeout / 1000})
        @event.listens_for(engine, 'connect')
        def sqlite_options(connection, _record):
            connection.execute(f'PRAGMA busy_timeout={timeout}')
        # Configure WAL once, before workers start. Doing it on every pooled
        # connection would itself compete for a schema/write lock.
        with engine.connect() as connection:
            mode = connection.exec_driver_sql('PRAGMA journal_mode=WAL').scalar()
            if not memory and str(mode).lower() != 'wal':
                engine.dispose()
                raise RuntimeError('SQLite WAL could not be enabled; stop other database writers before startup')
    else:
        if dialect != 'postgresql':
            raise ValueError('Online database must use PostgreSQL or SQLite')
        lock_ms = _integer('NEWS2DOUYIN_DB_LOCK_TIMEOUT_MS', 2000, 0, 60000)
        statement_ms = _integer('NEWS2DOUYIN_DB_STATEMENT_TIMEOUT_MS', 15000, 0, 3600000)
        engine = create_engine(db_url, echo=False, hide_parameters=True, pool_pre_ping=True,
            pool_size=_integer('NEWS2DOUYIN_DB_POOL_SIZE', 8, 1, 64),
            max_overflow=_integer('NEWS2DOUYIN_DB_MAX_OVERFLOW', 8, 0, 64),
            pool_timeout=_integer('NEWS2DOUYIN_DB_POOL_TIMEOUT', 10, 0, 300),
            connect_args={'connect_timeout': 10, 'options':
                f'-c lock_timeout={30000 if maintenance else lock_ms} '
                f'-c statement_timeout={0 if maintenance else statement_ms} '
                f'-c idle_in_transaction_session_timeout={0 if maintenance else 60000}'})
    from .access import install_observer
    install_observer(engine)
    return engine


def is_lock_error(exc: BaseException) -> bool:
    original = getattr(exc, 'orig', exc)
    code = getattr(original, 'sqlstate', None) or getattr(original, 'pgcode', None)
    if code in {'55P03', '40P01', '40001'}:
        return True
    sqlite_code = getattr(original, 'sqlite_errorcode', None)
    return isinstance(sqlite_code, int) and sqlite_code & 255 in (5, 6)


def load_models() -> None:
    from . import models as storage_models
    from ..research import models as research_models
    from ..topics import models as topic_models
    from ..desk import models as desk_models
    from ..desk import focus_models
    from ..desk import graph_maintenance_models
    from ..desk import graph_focus_models
    from ..desk import focus_event_models
    from ..extraction import models as extraction_models
    from ..knowledge import models as knowledge_models
    from ..workbench import models as workbench_models
    from ..intake import models as intake_models
    from ..intake import official_models
    from ..selection import ngram_models
    from ..selection import models as selection_models
    from ..selection import source_models as source_preview_models
    from ..translations import models as translation_models
    from ..intake import projection as intake_projection
    from ..intake import fetch_diagnostics
    from ..attention import models as attention_models
    from ..digest import models as digest_models
    from ..candidates import models as candidate_models
    from ..world_radar import checkpoints as world_index_checkpoints
    from ..georisk import models as georisk_models
    from ..event_embeddings import models as event_embedding_models
    from ..causal import models as causal_models
    from ..semantic import models as semantic_vector_models, daily_models as semantic_daily_models
    from ..world_radar import models as story_models
    from ..world_radar import relation_models as world_relation_models
    from ..history_archive import models as history_archive_models


def create_schema(engine) -> None:
    load_models()
    SQLModel.metadata.create_all(engine)


def install_runtime_schema(engine, *, backfill: bool = False, startup_checkpoint: bool = False,
                           phase_callback=None) -> None:
    from ..search.index import init_search
    from ..desk.service import initialize as init_desk
    from ..extraction.service import initialize as init_extraction
    phase = phase_callback or (lambda _name: nullcontext())
    with phase('database.search_indexes'):
        init_search(engine)
    with phase('database.desk_triggers'):
        init_desk(engine, backfill=backfill)
    with phase('database.extraction_triggers_backfill'):
        init_extraction(engine, backfill=backfill, checkpoint=startup_checkpoint)


def init_db(engine, *, phase_callback=None) -> None:
    """Install required schema before workers; report code-owned phase timings.

    ``phase_callback(name)`` may return a context manager for startup telemetry.
    Historical extraction backfill is checkpointed only for normal startup;
    explicit initialization/migration retains its existing backfill semantics.
    """
    phase = phase_callback or (lambda _name: nullcontext())
    # A partially copied target is never a live application database.
    with phase('database.migration_guard'):
        if inspect(engine).has_table('_n2d_sqlite_migration'):
            with engine.connect() as connection:
                phases = connection.execute(text('SELECT phase FROM _n2d_sqlite_migration')).scalars().all()
            if phases != ['ready']:
                raise RuntimeError('SQLite migration is incomplete; resume and verify migration before starting the service')
    with phase('database.schema'):
        create_schema(engine)
    from .index_safety import ensure_safe_text_indexes
    with phase('database.text_index_safety'):
        ensure_safe_text_indexes(engine)
    from .articles import backfill_versions
    with phase('database.article_versions_backfill'):
        backfill_versions(engine)
    install_runtime_schema(engine, backfill=True, startup_checkpoint=True, phase_callback=phase_callback)
    from ..video.library import backfill_works
    with phase('database.video_works_backfill'):
        backfill_works(engine)
    from ..research.service import initialize as init_research
    with phase('database.research'):
        init_research(engine)
    from ..knowledge.service import initialize as init_knowledge
    with phase('database.knowledge'):
        init_knowledge(engine)
    from ..workbench.service import initialize as init_workbench
    with phase('database.workbench'):
        init_workbench(engine)
    from ..intake.service import initialize as init_intake
    with phase('database.intake'):
        init_intake(engine)


def session_scope(engine) -> Session:
    return Session(engine)

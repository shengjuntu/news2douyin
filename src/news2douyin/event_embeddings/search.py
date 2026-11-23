"""Bounded, read-only cosine lookup among current curated event embeddings."""
from heapq import nlargest
import json
from math import fsum, hypot, isfinite

from sqlalchemy import Float, case, cast, func, literal, or_, text
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.exc import DBAPIError
from sqlalchemy.types import UserDefinedType
from sqlmodel import select

from ..storage.models import Event, EventState
from .models import EventEmbeddingJob, EventEmbeddingRecord


CANDIDATE_LIMIT = 5000


def _recent(profile, lower, upper):
    return (
        select(Event.event_key, func.coalesce(EventState.title_override, Event.event_title).label('title'), Event.first_seen_at,
               EventEmbeddingRecord.embedding)
        .select_from(Event)
        .join(EventEmbeddingRecord, EventEmbeddingRecord.event_key == Event.event_key)
        .join(EventEmbeddingJob, EventEmbeddingJob.event_key == Event.event_key)
        .outerjoin(EventState, EventState.event_key == Event.event_key)
        .where(EventEmbeddingRecord.profile == profile, EventEmbeddingJob.state == 'ready',
               or_(EventState.merged_into.is_(None), EventState.merged_into == ''),
               Event.first_seen_at >= lower, Event.first_seen_at < upper)
        .order_by(Event.first_seen_at.desc(), Event.event_key)
        .limit(CANDIDATE_LIMIT)
    )


def unit_vector(vector):
    """Normalize a finite, nonzero stored or query embedding."""
    if not isinstance(vector, list) or not vector or any(type(value) not in (int, float) for value in vector):
        return None
    try:
        norm = hypot(*vector)
    except OverflowError:
        return None
    if not norm or not isfinite(norm):
        return None
    return [value / norm for value in vector]


def _item(key, title, timestamp, score):
    return dict(event_key=key, title=title, first_seen_at=timestamp,
                similarity=max(-1., min(1., score)))


def _portable(connection, recent, query, limit):
    def scored(rows):
        for key, title, timestamp, serialized in rows:
            try:
                vector = json.loads(serialized)
            except (ValueError, TypeError, RecursionError):
                continue
            if isinstance(vector, list) and len(vector) == len(query) and (unit := unit_vector(vector)):
                yield _item(key, title, timestamp, fsum(a * b for a, b in zip(query, unit)))

    # Stream records, keeping only the best matches. Stable order resolves ties.
    statement = recent.execution_options(yield_per=128, n2d_query_name='event_embedding.search.portable')
    with connection.execute(statement) as rows:
        return nlargest(limit, scored(rows), key=lambda item: item['similarity'])


class _PgVector(UserDefinedType):
    cache_ok = True

    def __init__(self, schema):
        self.schema = schema

    def get_col_spec(self, **kw):
        return self.schema + '.vector'


def _pg_statement(recent, query, limit, schema):
    # Materialize the candidate cap, then filter dimensions before evaluating
    # the operator. Old records with another dimension are skipped.
    recent = recent.cte('recent').prefix_with('MATERIALIZED')
    data = cast(recent.c.embedding, JSON)
    size = case((func.json_typeof(data) == 'array', func.json_array_length(data)), else_=0)
    sized = select(recent).where(size == len(query)).cte('sized').prefix_with('MATERIALIZED')
    vector_type = _PgVector(schema)
    other = cast(literal(json.dumps(query, separators=(',', ':'))), vector_type)
    distance = cast(sized.c.embedding, vector_type).op(f'OPERATOR({schema}.<=>)', return_type=Float())(other)
    score = (1. - distance).label('similarity')
    scores = select(sized.c.event_key, sized.c.title, sized.c.first_seen_at, score).subquery()
    # pgvector returns NaN for cosine against a zero vector. PostgreSQL orders
    # NaN above finite floats, so the upper comparison excludes it.
    return (
        select(scores)
        .where(scores.c.similarity > -2., scores.c.similarity < 2.)
        .order_by(scores.c.similarity.desc(), scores.c.first_seen_at.desc(), scores.c.event_key)
        .limit(limit)
        .execution_options(n2d_query_name='event_embedding.search.pgvector')
    )


def _pgvector(connection, recent, query, limit):
    # Use a connection savepoint so invalid legacy text cannot poison the
    # caller's transaction, and so this read never flushes a pending ORM edit.
    try:
        with connection.begin_nested():
            schema = connection.execute(text('''
                SELECT ns.nspname FROM pg_catalog.pg_extension AS ext
                JOIN pg_catalog.pg_namespace AS ns ON ns.oid=ext.extnamespace
                WHERE ext.extname='vector'
            ''')).scalar_one_or_none()
            if schema is None:
                return None
            quoted = connection.dialect.identifier_preparer.quote_schema(schema)
            rows = connection.execute(_pg_statement(recent, query, limit, quoted))
            return [_item(key, title, timestamp, score) for key, title, timestamp, score in rows]
    except DBAPIError as exc:
        state = getattr(exc.orig, 'sqlstate', '') or ''
        if exc.connection_invalidated or not state.startswith(('22', '42', '0A')):
            raise
        return None


def find_similar(session, *, profile: str, vector: list[float], lower: str, upper: str,
                 limit: int = 10) -> list[dict]:
    """Search the latest 5,000 eligible events in the caller's half-open time window.

    Exact cosine scores are computed over that bounded set. PostgreSQL's optional
    pgvector path applies the same window and candidate cap as the portable path.
    """
    query = unit_vector(vector)
    if query is None or limit <= 0:
        return []
    recent = _recent(profile, lower, upper)
    connection = session.connection()
    if connection.dialect.name == 'postgresql':
        rows = _pgvector(connection, recent, query, limit)
        if rows is not None:
            return rows
    return _portable(connection, recent, query, limit)

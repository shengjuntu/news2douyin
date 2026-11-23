"""Independent nearest earlier events for each proposed cause."""
from contextlib import closing
from datetime import date
from itertools import groupby, islice
import json
from math import fsum
from operator import itemgetter, mul

from sqlalchemy import or_, union_all
from sqlmodel import Session, select

from ..event_embeddings.models import EventEmbeddingJob, EventEmbeddingRecord
from ..event_embeddings.search import unit_vector
from ..storage.models import Article, ArticleEventLink, EventMoment, EventState
from .chronology import interval


HITS_PER_HYPOTHESIS = 6
# Inspect additional neighbors because a dated record can lack a usable quote.
NEIGHBORS_TO_CHECK = HITS_PER_HYPOTHESIS * 4
BATCH_SIZE = 256


def date_envelope(floor, ceiling):
    # Stored dates can have a zone offset or only day precision. Keep adjacent
    # days in the indexed SQL range and check exact instants before scoring.
    return (date.fromordinal(max(1, floor.date().toordinal() - 1)).isoformat(),
            date.fromordinal(min(date.max.toordinal(), ceiling.date().toordinal() + 1)).isoformat() + '~')


def _dated_keys(session, profile, floor, ceiling, excluded, checkpoint):
    start, stop = date_envelope(floor, ceiling)
    publications = (select(ArticleEventLink.event_key, Article.published_at.label('stamp'))
                    .select_from(Article).join(ArticleEventLink, ArticleEventLink.article_key == Article.article_key)
                    .where(Article.published_at >= start, Article.published_at < stop, Article.url != ''))
    occurrences = select(EventMoment.event_key, EventMoment.date_start.label('stamp')).where(
        EventMoment.date_start >= start, EventMoment.date_start < stop, EventMoment.reviewed.is_(True),
        EventMoment.deleted.is_(False), EventMoment.time_kind == 'occurred', EventMoment.certainty == 'exact')
    clocks = union_all(publications, occurrences).subquery()
    statement = (select(clocks.c.event_key, clocks.c.stamp)
                 .join(EventEmbeddingRecord, EventEmbeddingRecord.event_key == clocks.c.event_key)
                 .join(EventEmbeddingJob, EventEmbeddingJob.event_key == clocks.c.event_key)
                 .outerjoin(EventState, EventState.event_key == clocks.c.event_key)
                 .where(EventEmbeddingRecord.profile == profile, EventEmbeddingJob.state == 'ready',
                        or_(EventState.merged_into.is_(None), EventState.merged_into == ''),
                        clocks.c.event_key.not_in(excluded))
                 .order_by(clocks.c.event_key)
                 .execution_options(yield_per=BATCH_SIZE, n2d_query_name='causal.tree.vector_dates'))
    with closing(session.exec(statement)) as rows:
        for i, (key, group) in enumerate(groupby(rows, key=itemgetter(0))):
            if checkpoint and i % BATCH_SIZE == 0:
                checkpoint()
            if any(floor <= point[0] < ceiling and point[1] <= ceiling
                   for _, stamp in group if (point := interval(stamp))):
                yield key


def nearest(engine, profile, vectors, floor, ceiling, excluded, checkpoint=None):
    """Exact cosine neighbors of each unit query among earlier, ready events."""
    best = [[] for _ in vectors]
    scanned = 0
    if not vectors:
        return best, scanned
    dimensions = len(vectors[0])
    with Session(engine) as session, closing(_dated_keys(session, profile, floor, ceiling, excluded, checkpoint)) as keys:
        while batch := list(islice(keys, BATCH_SIZE)):
            rows = session.exec(select(EventEmbeddingRecord.event_key, EventEmbeddingRecord.embedding)
                .join(EventEmbeddingJob, EventEmbeddingJob.event_key == EventEmbeddingRecord.event_key)
                .where(EventEmbeddingRecord.profile == profile, EventEmbeddingJob.state == 'ready',
                       EventEmbeddingRecord.event_key.in_(batch))
                .execution_options(n2d_query_name='causal.tree.vector_page')).all()
            scanned += len(rows)
            for key, raw in rows:
                try:
                    values = json.loads(raw)
                except (TypeError, ValueError, RecursionError):
                    continue
                if not isinstance(values, list) or len(values) != dimensions:
                    continue
                vector = unit_vector(values)
                if vector is None:
                    continue
                for query, group in zip(vectors, best):
                    score = fsum(map(mul, vector, query))
                    if score > 0:
                        group.append((-score, key))
            for group in best:
                group.sort()
                del group[NEIGHBORS_TO_CHECK:]
    return [[key for _, key in group] for group in best], scanned

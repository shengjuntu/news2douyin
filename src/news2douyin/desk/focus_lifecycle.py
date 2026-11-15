"""Recoverable topic removal with one durable tombstone and bounded updates.

No Article, Event, content task, research result or graph evidence is deleted.
Successful removal is serialized against a topic research submission. An
already submitted research task remains an independent, inspectable result.
"""
import time

from sqlalchemy import update
from sqlmodel import select

from .focus_models import FocusBackfill, FocusCandidate, FocusTrash
from .models import GraphLease, Watch, WatchReview
from ..storage.articles import lock_news
from ..storage.models import utc_now_iso
from ..tasks.control import TaskConflict


def visible_watch():
    return ~select(FocusTrash.watch).where(FocusTrash.watch == Watch.key,
        FocusTrash.deleted_at != '').correlate(Watch).exists()


def is_deleted(session, key):
    row = session.get(FocusTrash, key)
    return bool(row and row.deleted_at)


def require_active(session, key):
    row = session.get(Watch, key)
    if not row or is_deleted(session, key):
        raise KeyError('专题不存在或已移入回收站')
    return row


def trash_items(session):
    rows = session.exec(select(FocusTrash, Watch.version).join(Watch, Watch.key == FocusTrash.watch)
        .where(FocusTrash.deleted_at != '').order_by(FocusTrash.deleted_at.desc(), FocusTrash.watch)
        .limit(100)).all()
    return [dict(key=row.watch, name=row.name, deleted_at=row.deleted_at, version=version)
        for row, version in rows]


def remove(session, key, version):
    lock_news(session, 'watch:'+key, 'lease/watch-lifecycle/'+key)
    watch = session.get(Watch, key)
    if not watch:
        raise KeyError('专题不存在')
    tombstone = session.get(FocusTrash, key)
    if tombstone and tombstone.deleted_at:
        return dict(ok=True, deleted=True, key=key, name=tombstone.name)
    if watch.version != version:
        raise TaskConflict('专题已被修改，请刷新后再移入回收站')
    lease = session.get(GraphLease, 'watch-lifecycle/'+key)
    if lease and lease.owner and lease.until > time.time():
        raise TaskConflict('该专题正在提交研究任务，请稍后重试移入回收站')
    now = utc_now_iso()
    tombstone = tombstone or FocusTrash(watch=key, name=watch.name)
    baseline = tombstone.previous_scope_version if tombstone.restored_version == watch.version else watch.version
    tombstone.name, tombstone.deleted_at, tombstone.restored_at = watch.name, now, ''
    tombstone.previous_enabled, tombstone.previous_auto_research = watch.enabled, watch.auto_research
    tombstone.previous_scope_version, tombstone.restored_version = baseline, 0
    watch.enabled = watch.auto_research = False
    watch.version += 1
    session.add(tombstone)
    session.add(watch)
    backfill = session.get(FocusBackfill, key)
    if backfill:
        backfill.status, backfill.error, backfill.updated = 'paused', '专题已移入回收站', now
        session.add(backfill)
    session.exec(update(FocusCandidate).where(FocusCandidate.watch == key,
        FocusCandidate.status.in_(('pending', 'running'))).values(status='superseded',
        reason='专题已移入回收站', updated=now).execution_options(synchronize_session=False))
    session.exec(update(WatchReview).where(WatchReview.watch == key,
        WatchReview.status.in_(('pending', 'blocked'))).values(status='cancelled',
        message='专题已移入回收站，未提交的补查已取消').execution_options(synchronize_session=False))
    from .service import enqueue_graph
    enqueue_graph(session, 'watch/'+key)
    session.commit()
    return dict(ok=True, deleted=True, key=key, name=watch.name)


def restore(session, key, version):
    lock_news(session, 'watch:'+key)
    watch, tombstone = session.get(Watch, key), session.get(FocusTrash, key)
    if not watch or not tombstone:
        raise KeyError('回收站中没有这个专题')
    if not tombstone.deleted_at:
        return dict(ok=True, key=key, enabled=watch.enabled)
    if watch.version != version:
        raise TaskConflict('回收站记录已更新，请刷新后再恢复')
    tombstone.deleted_at, tombstone.restored_at = '', utc_now_iso()
    watch.enabled = watch.auto_research = False
    watch.version += 1
    tombstone.restored_version = watch.version
    watch.next_review = ''
    session.add(tombstone)
    session.add(watch)
    from .focus_models import FocusDefinition
    definition = session.get(FocusDefinition, key)
    if definition:
        definition.scope_version, definition.updated = watch.version, utc_now_iso()
        session.add(definition)
    from .focus_jobs import queue_backfill
    queue_backfill(session, watch)
    from .service import enqueue_graph
    enqueue_graph(session, 'watch/'+key)
    session.commit()
    return dict(ok=True, key=key, enabled=False)

"""Manually reviewed commodity references to immutable news article versions."""
import re
from typing import Annotated
from urllib.parse import quote

from pydantic import StringConstraints
from sqlalchemy import func
from sqlmodel import select

from ..research.materials import original_body
from ..selection.source_content import source_url
from ..storage.models import (
    ArticleEventLink, ArticleVersion, Event, EventMoment, EventProductEvidence, EventState, utc_now_iso,
)
from ..storage.utils import loads
from ..tasks.control import TaskConflict
from . import workbench
from .schemas import Evidence, VersionPayload
from .service import reviewed_day

PRODUCT_PATTERN = r'^[a-z0-9][a-z0-9_-]{0,63}$'
ProductID = Annotated[str, StringConstraints(pattern=PRODUCT_PATTERN)]


class ProductEvidenceEdit(VersionPayload, Evidence):
    product_id: ProductID


def _item(row):
    data = row.model_dump(exclude={'revoked_at'})
    data['article_path'] = '/articles/' + quote(row.article_key, safe='') + '?revision=' + str(row.revision)
    return data


def active_for_event(session, key):
    return session.exec(select(EventProductEvidence).where(
        EventProductEvidence.event_key == key, EventProductEvidence.revoked_at.is_(None)
    ).order_by(EventProductEvidence.id.desc())).all()


def card(session, key):
    event = workbench.require_event(session, key)
    state = session.get(EventState, key)
    moments = session.exec(select(EventMoment).where(
        EventMoment.event_key == key, EventMoment.reviewed.is_(True),
        EventMoment.deleted.is_(False), EventMoment.time_kind == 'occurred'
    ).limit(2)).all()
    title = state.title_override if state and state.title_override is not None else event.event_title
    return dict(event_key=key, title=title, version=state.version if state else 0,
                merged_into=workbench.canonical_event(session, key) if state and state.merged_into else None,
                time_basis='latest', first_seen_at=event.first_seen_at,
                occurred_on=reviewed_day([workbench.moment_data(row) for row in moments]),
                items=[_item(row) for row in active_for_event(session, key)])


def for_product(session, product_id, *, limit=50, before_id=None):
    if not re.fullmatch(PRODUCT_PATTERN, product_id):
        raise ValueError('商品标识格式无效')
    if not 1 <= limit <= 100 or (before_id is not None and not 1 <= before_id <= 2**63-1):
        raise ValueError('分页范围无效')
    stmt = (select(EventProductEvidence, func.coalesce(EventState.title_override, Event.event_title))
            .join(Event, Event.event_key == EventProductEvidence.event_key)
            .outerjoin(EventState, EventState.event_key == Event.event_key)
            .where(EventProductEvidence.product_id == product_id, EventProductEvidence.revoked_at.is_(None)))
    if before_id is not None:
        stmt = stmt.where(EventProductEvidence.id < before_id)
    rows = session.exec(stmt.order_by(EventProductEvidence.id.desc()).limit(limit + 1)).all()
    items = [dict(_item(row), event_title=title, event_path='/events/' + quote(row.event_key, safe=''))
             for row, title in rows[:limit]]
    return dict(product_id=product_id, time_basis='latest', items=items,
                next_before_id=items[-1]['id'] if len(rows) > limit else None)


def add(session, key, data):
    state = workbench.lock_event(session, key, data['expected_version'])
    article = data['article_key']
    link = session.exec(select(ArticleEventLink.id).where(
        ArticleEventLink.event_key == key, ArticleEventLink.article_key == article
    ).limit(1)).first()
    if link is None:
        raise ValueError('所选报道尚未加入本事件')
    version = session.exec(select(ArticleVersion).where(
        ArticleVersion.article_key == article, ArticleVersion.revision == data['revision']
    )).first()
    if not version:
        raise ValueError('所选报道版本不存在')
    if version.content_hash != data['content_hash']:
        raise TaskConflict('来源版本指纹不匹配，请重新加载')
    document = loads(version.payload_json, {})
    body = original_body(document)
    url = source_url(document.get('url')) if isinstance(document, dict) else ''
    if not body.strip() or not url or '\\' in url:
        raise ValueError('请先获取带来源网址的报道原文，再核对商品关联')
    excerpt = data['excerpt'].strip()
    if not excerpt or excerpt not in body:
        raise ValueError('证据摘录必须是该版本正文中的连续片段')
    exists = session.exec(select(EventProductEvidence.id).where(
        EventProductEvidence.event_key == key, EventProductEvidence.product_id == data['product_id'],
        EventProductEvidence.article_key == article, EventProductEvidence.revision == version.revision,
        EventProductEvidence.excerpt == excerpt, EventProductEvidence.revoked_at.is_(None)
    ).limit(1)).first()
    if exists is None:
        row = EventProductEvidence(event_key=key, product_id=data['product_id'], article_key=article,
                                   revision=version.revision, content_hash=version.content_hash, excerpt=excerpt,
                                   source_url=url, source_title=document.get('title') or '',
                                   observed_at=version.observed_at or None, published_at=document.get('published_at') or None)
        session.add(row)
        session.flush()
        workbench.audit(session, key, 'verify_product', _item(row))
        workbench.touch(session, state)
    session.commit()
    return card(session, key)


def revoke(session, key, link_id, expected_version):
    state = workbench.lock_event(session, key, expected_version)
    row = session.get(EventProductEvidence, link_id)
    if not row or row.event_key != key:
        raise KeyError('此事件没有这条商品关联')
    if row.revoked_at is None:
        row.revoked_at = utc_now_iso()
        session.add(row)
        workbench.audit(session, key, 'revoke_product', {'id': row.id, 'product_id': row.product_id})
        workbench.touch(session, state)
    session.commit()
    return card(session, key)

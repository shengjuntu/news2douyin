"""Durable description compilation, bounded history routing and evidence judging.

HTTP handlers compile bounded local drafts and enqueue model jobs. Model calls
run outside SQL sessions, and a lease/version check protects publication.
"""
from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import delete, func, update
from sqlmodel import Session, select

from ..storage.articles import lock_news, latest_version
from ..storage.models import ArticleVersion, utc_now_iso
from ..tasks.control import TaskConflict
from ..extraction.models import FocusPoint, PointReview
from ..knowledge.models import PointMaterial
from ..extraction.schema import stable_id
from .models import Watch, WatchEvent, RadarEntry
from .focus_models import LOCAL_METHOD, FocusDefinition, FocusBuild, FocusBackfill, FocusCandidate
from .leases import acquire

BATCH = 25


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _loads(value, default):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def fingerprint(card):
    return hashlib.sha256(_json({name: getattr(card, name, '') for name in
        ('key', 'title', 'headings', 'material', 'sources', 'geography', 'seen')}).encode()).hexdigest()


def definition(s, key):
    row = s.get(FocusDefinition, key)
    if row is None:
        return None
    return dict(description=row.description, spec=_loads(row.spec_json, {}), method=row.method,
                scope_version=row.scope_version, updated=row.updated)


def submit_build(s, description, *, method='model'):
    description = description.strip()
    if not 1 <= len(description) <= 4000:
        raise ValueError('请用 1–4000 字描述希望长期关注的内容')
    row = FocusBuild(key='fd_'+uuid4().hex, description=description)
    if method == LOCAL_METHOD:
        from .intent import local_scope
        row.spec_json = _json(local_scope(description).model_dump())
        row.status = 'ready'
        row.method = LOCAL_METHOD
        row.message = '请确认关注词，系统会按这些词匹配新材料。'
    s.add(row)
    s.commit()
    return get_build(s, row.key)


def get_build(s, key):
    row = s.get(FocusBuild, key)
    if row is None:
        raise KeyError('焦点生成任务不存在')
    return dict(key=row.key, status=row.status, spec=_loads(row.spec_json, None),
                method=row.method, message=row.message, watch=row.watch)


def queue_backfill(s, w):
    """Only enqueue; COUNT/MAX and iteration are performed by the worker."""
    job = s.get(FocusBackfill, w.key)
    if job is None:
        job = FocusBackfill(watch=w.key, scope_version=w.version)
    job.scope_version = w.version
    job.status = 'pending' if w.enabled else 'paused'
    job.cursor = job.through_key = job.error = ''
    job.checked = job.matched = job.total = 0
    job.updated = utc_now_iso()
    s.add(job)
    return job


def _write_definition(s, spec, description, method, *, key='', expected=None, seed='', anchors=None):
    from . import intent, service as desk
    spec = intent.validate_spec(spec, description=description)
    if anchors is not None:
        if method != LOCAL_METHOD:
            raise ValueError('关注词用于本地规则焦点')
        spec = intent.replace_anchors(spec, anchors)
    scope = intent.to_scope(spec)
    ident = key or 'w_'+uuid4().hex
    lock_news(s, 'watch:'+ident, 'watch-name:'+scope.name)
    old = s.get(Watch, ident)
    if key:
        from .focus_lifecycle import require_active
        require_active(s, key)
    if old and old.version != expected:
        raise TaskConflict('焦点已更新，请刷新后重试')
    duplicate = s.exec(select(Watch.key).where(Watch.name == scope.name, Watch.key != ident)).first()
    if duplicate:
        from .focus_lifecycle import is_deleted
        raise ValueError('同名焦点在回收站中，请恢复或改名' if is_deleted(s,duplicate) else '焦点名称已存在')
    w = old or Watch(key=ident, **scope.model_dump())
    if old:
        for field in ('name', 'kind', 'scope', 'dimensions', 'exclude', 'question'):
            setattr(w, field, getattr(scope, field))
        w.version += 1
    else:
        w.next_review = desk.iso_after(w.review_days)
    s.add(w)
    s.flush()
    s.merge(FocusDefinition(watch=ident, description=description.strip(), spec_json=_json(spec.model_dump()),
                            method=method, scope_version=w.version, updated=utc_now_iso()))
    # Only the initializer's own question nodes are replaced; manual/research
    # questions and their evidence are never erased by a range edit.
    old_points = s.exec(select(FocusPoint).where(FocusPoint.watch == ident,
                                               FocusPoint.key.startswith('focusinit_'))).all()
    wanted = set()
    for question in spec.questions[:12]:
        point = 'focusinit_'+stable_id(ident, question.casefold())
        wanted.add(point)
        if not s.get(FocusPoint, point):
            s.add(FocusPoint(key=point, watch=ident, parent=ident, text=question[:400], created=utc_now_iso()))
    for point in old_points:
        if point.key not in wanted:
            # A renamed initialization question may already carry manual child
            # questions, research history or evidence. Preserve that knowledge.
            children = s.exec(select(FocusPoint.key).where(FocusPoint.parent == point.key).limit(1)).first()
            history = s.exec(select(PointReview.ticket).where(PointReview.point == point.key).limit(1)).first()
            material = s.exec(select(PointMaterial.key).where(PointMaterial.point == point.key).limit(1)).first()
            if not children and not history and not material:
                s.delete(point)
    queue_backfill(s, w)
    if seed:
        desk.link_event(s, ident, seed, 'include', commit=False)
    desk.enqueue_graph(s, 'watch/'+ident)
    return w


def create_from_build(s, draft, spec=None, seed='', *, anchors=None):
    lock_news(s, 'focus-build/'+draft)
    build = s.get(FocusBuild, draft)
    if build is None:
        raise KeyError('焦点生成任务不存在')
    if build.status != 'ready':
        raise TaskConflict('焦点正在生成')
    if build.watch:
        from .focus_lifecycle import require_active
        return require_active(s, build.watch)
    w = _write_definition(s, spec if spec is not None else _loads(build.spec_json, {}),
                          build.description, build.method, seed=seed, anchors=anchors)
    build.watch = w.key
    build.updated = utc_now_iso()
    s.add(build)
    s.commit()
    s.refresh(w)
    return w


def update_definition(s, key, version, description, spec=None, draft='', *, anchors=None):
    method = 'edited'
    if draft:
        build = s.get(FocusBuild, draft)
        if not build or build.status != 'ready':
            raise TaskConflict('请先生成关注范围')
        description = build.description
        spec = spec if spec is not None else _loads(build.spec_json, {})
        method = build.method
    else:
        current = s.get(FocusDefinition, key)
        if current and current.method == LOCAL_METHOD:
            method = LOCAL_METHOD
    if spec is None:
        raise ValueError('请先根据描述生成关注范围')
    if not 1 <= len(description.strip()) <= 4000:
        raise ValueError('关注描述须为 1–4000 字')
    w = _write_definition(s, spec, description, method, key=key, expected=version, anchors=anchors)
    s.commit()
    s.refresh(w)
    return w


def _clear_auto(s, watch, event, generation=''):
    link = s.get(WatchEvent, (watch, event))
    if link and link.choice == 'auto':
        s.delete(link)
        points = select(FocusPoint.key).where(FocusPoint.watch == watch)
        s.exec(delete(PointMaterial).where(PointMaterial.event == event, PointMaterial.point.in_(points)))
        from .service import enqueue_graph
        enqueue_graph(s, 'watch/'+watch)
        if generation:
            from ..knowledge.service import note_change
            note_change(s, watch, event, 'rechecking', '材料已更新，正在重新匹配', generation)


def queue_event(s, w, card, *, recall_extra=None, force=False):
    """Queue a versioned candidate without running a model or opening a transaction."""
    from . import intent
    if not w.enabled:
        return None
    definition_row = s.get(FocusDefinition, w.key)
    if definition_row is None:
        return None
    manual = s.get(WatchEvent, (w.key, card.key))
    if manual and manual.choice in ('include', 'exclude'):
        return None
    spec = intent.validate_spec(_loads(definition_row.spec_json, {}), description=definition_row.description)
    route = intent.recall(spec, card)
    # Origin metadata alone must not turn an unrelated historical row into a
    # candidate; graph paths may broaden recall, a history marker may not.
    if recall_extra and any(recall_extra.get(field) for field in ('channels', 'paths', 'reason')):
        route = route or dict(channels=[], paths=[], reason='')
        for field in ('channels', 'paths'):
            route[field] = list(dict.fromkeys([_json(v) for v in route.get(field, [])+recall_extra.get(field, [])]))
            route[field] = [_loads(v, v) for v in route[field]][:24]
        route['reason'] = '；'.join(v for v in (route.get('reason', ''), recall_extra.get('reason', '')) if v)[:600]
    if not route and manual:
        route = dict(channels=['recheck'], paths=[], reason='已收录材料更新，重新匹配')
    if not route and force:
        route = dict(channels=['recheck'], paths=[], reason='人工恢复自动判断')
    if not route:
        return None
    if recall_extra and recall_extra.get('historical'):
        route['historical'] = True
    digest = fingerprint(card)
    key = 'fc_'+stable_id(w.key, w.version, card.key, digest)
    existing = s.get(FocusCandidate, key)
    if existing:
        if force or existing.decision == 'manual':
            existing.status, existing.decision = 'pending', ''
            existing.attempts, existing.next_attempt = 0, 0
            existing.reason = '重新匹配'
            existing.updated = utc_now_iso()
            s.add(existing)
            _clear_auto(s, w.key, card.key, key)
        return existing
    # A changed event supersedes old automatic decisions, while retaining their
    # content as an audit trail. Only one current candidate per event is active.
    s.exec(update(FocusCandidate).where(FocusCandidate.watch == w.key,
        FocusCandidate.event == card.key, FocusCandidate.scope_version == w.version,
        FocusCandidate.status != 'superseded').values(status='superseded')
        .execution_options(synchronize_session=False))
    candidate = FocusCandidate(key=key, watch=w.key, event=card.key, scope_version=w.version,
        event_fingerprint=digest, recall_json=_json(route))
    s.add(candidate)
    # A correction/new scope awaits a fresh judgment. An old automatic member
    # must not continue to appear as if the new evidence had already passed.
    _clear_auto(s, w.key, card.key, key)
    return candidate


def _build_step(engine):
    with Session(engine) as s:
        keys = list(s.exec(select(FocusBuild.key).where(FocusBuild.status.in_(['pending', 'running']))
                           .order_by(FocusBuild.created, FocusBuild.key).limit(5)))
    for key in keys:
        with acquire(engine, 'focus-build/'+key, 120) as lease:
            if lease is None:
                continue
            with Session(engine) as s:
                row = s.get(FocusBuild, key)
                if not row or row.status not in ('pending', 'running'):
                    continue
                description = row.description
                row.status = 'running'
                row.attempts += 1
                row.updated = utc_now_iso()
                s.add(row)
                s.commit()
            # No read/write Session is alive during interpretation/model I/O.
            from . import intent
            try:
                result = intent.interpret(description)
                parsed = intent.validate_spec(result['spec'], description=description)
                error = ''
            except (ValueError, TypeError, KeyError):
                result = {}
                parsed = None
                error = '描述生成未完成，请调整描述后重试'
            with Session(engine) as s:
                lock_news(s, 'lease/'+lease.key, 'focus-build/'+key)
                lease.confirm(s)
                row = s.get(FocusBuild, key)
                if not row or row.description != description:
                    return True
                row.status = 'failed' if error else 'ready'
                row.spec_json = _json(parsed.model_dump()) if parsed else ''
                row.method = result.get('method', '')
                row.message = error or str(result.get('message', ''))[:600]
                row.updated = utc_now_iso()
                s.add(row)
                s.commit()
            return True
    return False


def _backfill_step(engine):
    from . import service as desk
    with Session(engine) as s:
        keys = list(s.exec(select(FocusBackfill.watch).where(FocusBackfill.status.in_(['pending', 'running']))
                           .order_by(FocusBackfill.updated, FocusBackfill.watch).limit(5)))
    for key in keys:
        with acquire(engine, 'focus-backfill/'+key, 60) as lease:
            if lease is None:
                continue
            with Session(engine) as s:
                lock_news(s, 'lease/'+lease.key, 'watch:'+key)
                lease.confirm(s)
                job = s.get(FocusBackfill, key)
                w = s.get(Watch, key)
                if not job:
                    continue
                if not w or job.scope_version != w.version:
                    job.status = 'paused' if w and not w.enabled else 'superseded'
                    job.updated = utc_now_iso()
                    s.add(job)
                    s.commit()
                    continue
                if not w.enabled:
                    job.status = 'paused'
                    job.updated = utc_now_iso()
                    s.add(job)
                    s.commit()
                    return True
                if job.status == 'pending':
                    job.through_key = s.exec(select(func.max(RadarEntry.key))).one() or ''
                    job.total = s.exec(select(func.count()).select_from(RadarEntry)).one()
                    job.status = 'running'
                stmt = select(RadarEntry).where(RadarEntry.key > job.cursor, RadarEntry.key <= job.through_key)
                cards = s.exec(stmt.order_by(RadarEntry.key).limit(BATCH)).all() if job.through_key else []
                natural = s.get(FocusDefinition, key) is not None
                for card in cards:
                    if natural:
                        if queue_event(s, w, card, recall_extra={'historical': True}):
                            job.matched += 1
                    else:
                        desk.sync_auto(s, w, card)
                        link = s.get(WatchEvent, (key, card.key))
                        job.matched += int(bool(link and link.choice != 'exclude'))
                    job.checked += 1
                    job.cursor = card.key
                if len(cards) < BATCH or (cards and job.cursor == job.through_key):
                    job.status = 'complete'
                job.updated = utc_now_iso()
                s.add(job)
                if cards:
                    desk.enqueue_graph(s, 'watch/'+key)
                s.commit()
            return True
    return False


def _snapshot(s, card):
    """Pin up to six immutable source versions and preserve verbatim evidence."""
    sources = _loads(card.sources, [])
    enriched, signatures = [], []
    stale = False
    for source in sources[:6]:
        article = source.get('key', '')
        if not article:
            continue
        latest = latest_version(s, article)
        revision = source.get('revision')
        if revision:
            version = s.exec(select(ArticleVersion).where(ArticleVersion.article_key == article,
                                                        ArticleVersion.revision == int(revision))).first()
        else:
            version = latest
        if version is None:
            continue
        if latest and latest.id != version.id:
            stale = True
        doc = _loads(version.payload_json, {})
        item = dict(source)
        item.update(key=article, article=article, revision=version.revision, version_id=version.id,
                    title=str(doc.get('title') or source.get('title') or ''),
                    content=str(doc.get('content') or ''), published_at=str(doc.get('published_at') or ''))
        enriched.append(item)
        signatures.append(dict(article=article, id=version.id, revision=version.revision,
            hash=version.content_hash, payload_sha256=hashlib.sha256(version.payload_json.encode()).hexdigest()))
    data = card.model_dump()
    data['sources'] = enriched
    return SimpleNamespace(**data), signatures, stale


def _versions_unchanged(s, signatures):
    for item in signatures:
        latest = latest_version(s, item['article'])
        if not latest or latest.id != item['id'] or latest.content_hash != item['hash']:
            return False
        if hashlib.sha256(latest.payload_json.encode()).hexdigest() != item['payload_sha256']:
            return False
    return True


def _judge_step(engine):
    from . import intent, service as desk
    now = time.time()
    with Session(engine) as s:
        keys = list(s.exec(select(FocusCandidate.key).where(
            FocusCandidate.status.in_(['pending', 'running']), FocusCandidate.next_attempt <= now)
            .order_by(FocusCandidate.created, FocusCandidate.key).limit(8)))
    for key in keys:
        with acquire(engine, 'focus-candidate/'+key, 120) as lease:
            if lease is None:
                continue
            with Session(engine) as s:
                row = s.get(FocusCandidate, key)
                if not row or row.status not in ('pending', 'running'):
                    continue
                w = s.get(Watch, row.watch)
                spec_row = s.get(FocusDefinition, row.watch)
                card = s.get(RadarEntry, row.event)
                manual = s.get(WatchEvent, (row.watch, row.event))
                if (not w or not w.enabled or not spec_row or w.version != row.scope_version or not card
                        or fingerprint(card) != row.event_fingerprint):
                    row.status = 'superseded'
                    s.add(row)
                    s.commit()
                    continue
                if manual and manual.choice in ('include', 'exclude'):
                    row.status, row.decision, row.method = 'done', 'manual', 'manual'
                    row.reason = '保留人工纳入或排除的决定'
                    s.add(row)
                    s.commit()
                    continue
                spec = intent.validate_spec(_loads(spec_row.spec_json, {}), description=spec_row.description)
                local_rules = spec_row.method == LOCAL_METHOD
                snapshot, signatures, stale = _snapshot(s, card)
                watch_key, event_key, scope_version, digest = row.watch, row.event, row.scope_version, row.event_fingerprint
                route = _loads(row.recall_json, {})
                row.status = 'running'
                row.attempts += 1
                attempts = row.attempts
                row.source_versions_json = _json(signatures)
                row.updated = utc_now_iso()
                s.add(row)
                s.commit()
            # The Session above is closed: model I/O never holds a SQL lock.
            if stale:
                result = dict(decision='pending', method='evidence', reason='原文版本正在更新，等待事件索引同步', evidence=[], questions=[])
            else:
                try:
                    result = intent.judge_local(spec, snapshot) if local_rules else intent.judge(spec, snapshot, route)
                except (ValueError, TypeError, KeyError):
                    result = dict(decision='pending', method='unavailable', reason='模型判断未完成，保留待核', evidence=[], questions=[])
            decision = result.get('decision')
            if decision not in ('core', 'background', 'pending', 'exclude'):
                result = dict(decision='pending', method='unavailable', reason='模型结果不符合协议，保留待核', evidence=[], questions=[])
                decision = 'pending'
            with Session(engine) as s:
                lock_news(s, 'lease/'+lease.key, 'watch:'+watch_key, 'desk-event:'+event_key)
                lease.confirm(s)
                row = s.get(FocusCandidate, key)
                w = s.get(Watch, watch_key)
                current = s.get(RadarEntry, event_key)
                manual = s.get(WatchEvent, (watch_key, event_key))
                if not row or row.status == 'superseded':
                    return True
                if (not w or not w.enabled or w.version != scope_version or not current
                        or fingerprint(current) != digest or not _versions_unchanged(s, signatures)):
                    row.status = 'superseded'
                    row.reason = '焦点或原文已更新，旧判断已失效'
                    s.add(row)
                    s.commit()
                    return True
                row.decision = decision
                row.method = str(result.get('method', ''))[:40]
                row.reason = str(result.get('reason', ''))[:1000]
                row.evidence_json = _json(result.get('evidence', [])[:12])
                row.questions_json = _json(result.get('questions', [])[:12])
                row.updated = utc_now_iso()
                # Transient model failures get bounded delayed attempts; source
                # insufficiency remains visible for a manual retry/new revision.
                transient = decision == 'pending' and row.method in ('unavailable', 'error', 'model_error')
                row.status = 'pending' if transient and attempts < 3 else 'done'
                row.next_attempt = time.time()+min(300, 30*2**min(attempts, 3)) if row.status == 'pending' else 0
                if manual and manual.choice in ('include', 'exclude'):
                    row.status, row.decision, row.method = 'done', 'manual', 'manual'
                    row.reason = '保留人工纳入或排除的决定'
                elif decision in ('core', 'background'):
                    branch = (result.get('questions') or ['核心进展' if decision == 'core' else '背景材料'])[0]
                    s.merge(WatchEvent(watch=watch_key, event=event_key, choice='auto',
                        relevance=2 if decision == 'core' else 1, branch=str(branch)[:400],
                        reason=('模型判断 · ' if row.method == 'model' else '焦点匹配 · ')+row.reason,
                        first_seen=manual.first_seen if manual else utc_now_iso()))
                    # These links express relevance to a question, not support
                    # for a factual claim. Only exact existing question texts.
                    points = s.exec(select(FocusPoint).where(FocusPoint.watch == watch_key,
                        FocusPoint.text.in_(result.get('questions', [])[:12]))).all()
                    for point in points:
                        s.merge(PointMaterial(key=stable_id(point.key, event_key), point=point.key,
                            event=event_key, reason='模型判断与该问题相关；不表示事实已被证实',
                            generation='focus-'+str(scope_version)))
                    from ..knowledge.service import note_change
                    generation = stable_id(scope_version, digest, decision)
                    note_change(s, watch_key, event_key, 'updated' if manual else 'added',
                                current.title, generation)
                    today = utc_now_iso()[:10]
                    if (decision == 'core' and w.enabled and w.auto_research
                            and w.last_auto_day != today and current.priority >= 2
                            and current.seen >= w.created and not route.get('historical')):
                        desk.make_review(s, w, event_key, 'signal-'+today)
                        w.last_auto_day = today
                        w.next_review = desk.iso_after(w.review_days)
                        s.add(w)
                    desk.enqueue_graph(s, 'watch/'+watch_key)
                elif decision in ('exclude', 'pending'):
                    _clear_auto(s, watch_key, event_key)
                s.add(row)
                s.commit()
            return True
    return False


def step(engine):
    """One compilation OR one bounded history block plus at most one LLM judge."""
    if _build_step(engine):
        return True
    scanned = _backfill_step(engine)
    judged = _judge_step(engine)
    return bool(scanned or judged)


def scan_step(engine):
    """Separate fast history worker so a slow judge cannot stall backfill."""
    return _backfill_step(engine)


def build_step(engine):
    return _build_step(engine)


def judge_step(engine):
    return _judge_step(engine)


def decisions(s, key, event):
    """Bounded read-only explanation trail, including fixed source references."""
    from .focus_lifecycle import require_active
    require_active(s, key)
    card = s.get(RadarEntry, event)
    if card is None:
        raise KeyError('事件不存在')
    rows = s.exec(select(FocusCandidate).where(FocusCandidate.watch == key,
        FocusCandidate.event == event).order_by(FocusCandidate.updated.desc(), FocusCandidate.key).limit(5)).all()
    return dict(event=event, title=card.title, items=[dict(scope_version=row.scope_version,
        status=row.status, decision=row.decision, method=row.method, reason=row.reason,
        evidence=_loads(row.evidence_json, []), questions=_loads(row.questions_json, []),
        recall=_loads(row.recall_json, {}), source_versions=_loads(row.source_versions_json, []),
        updated=row.updated) for row in rows])


def retry(s, key):
    from . import service as desk
    lock_news(s, 'watch:'+key)
    w = desk.get_watch(s, key)
    if not w.enabled:
        raise TaskConflict('请先启用跟踪')
    job = s.get(FocusBackfill, key)
    if job and job.status in ('paused', 'failed'):
        job.status = 'running' if job.through_key else 'pending'
        job.error = ''
        job.updated = utc_now_iso()
        s.add(job)
    s.exec(update(FocusCandidate).where(FocusCandidate.watch == key,
        FocusCandidate.scope_version == w.version, FocusCandidate.decision == 'pending',
        FocusCandidate.status != 'superseded').values(status='pending', attempts=0, next_attempt=0)
        .execution_options(synchronize_session=False))
    s.commit()
    return dict(ok=True)


def status(s, key):
    from .service import current_membership
    from .focus_lifecycle import require_active
    w = require_active(s, key)
    job = s.get(FocusBackfill, key)
    backfill = dict(status=job.status, checked=job.checked, matched=job.matched, total=job.total,
                    error=job.error, updated=job.updated) if job else dict(status='idle' if w.enabled else 'paused', checked=None, matched=None, total=None, error='', updated='')
    scope = (FocusCandidate.watch == key, FocusCandidate.scope_version == w.version,
             FocusCandidate.status != 'superseded')
    pending_filter = (*scope, (FocusCandidate.status.in_(['pending', 'running'])) | (FocusCandidate.decision == 'pending'))
    pending_count = s.exec(select(func.count()).select_from(FocusCandidate).where(*pending_filter)).one()
    core = s.exec(select(func.count()).select_from(WatchEvent).where(WatchEvent.watch == key,
        WatchEvent.choice != 'exclude', WatchEvent.relevance >= 2, current_membership())).one()
    background = s.exec(select(func.count()).select_from(WatchEvent).where(WatchEvent.watch == key,
        WatchEvent.choice != 'exclude', WatchEvent.relevance < 2, current_membership())).one()
    excluded = s.exec(select(func.count()).select_from(FocusCandidate).where(*scope, FocusCandidate.decision == 'exclude')).one()
    rows = s.exec(select(FocusCandidate, RadarEntry.title).join(RadarEntry, RadarEntry.key == FocusCandidate.event)
        .where(*pending_filter).order_by(FocusCandidate.updated.desc(), FocusCandidate.key).limit(8)).all()
    return dict(backfill=backfill, counts=dict(pending=pending_count, core=core, background=background, excluded=excluded),
                pending=[dict(event=row.event, title=title, reason=row.reason or '等待相关性判断') for row, title in rows],
                definition=definition(s, key))

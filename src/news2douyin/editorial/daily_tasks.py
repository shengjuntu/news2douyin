"""Durable daily-pick generation. Frozen input, one current request, atomic result."""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlmodel import Session, select

from ..llm.settings import get_settings
from ..storage.articles import lock_news
from ..storage.models import (ArticleEventLink, DailySelection, DailyScriptGeneration,
                              ScriptPackage, TaskRecord, utc_now_iso)
from ..tasks.control import TaskConflict
from ..tasks.service import TERMINAL, add_event, task_dict
from ..video import workbench as wb
from . import daily
from .service import build_editorial_pack


class DailyTaskRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['basic', 'llm'] = 'basic'


class DailyBatchRequest(DailyTaskRequest):
    selection_keys: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def distinct(self):
        if len(self.selection_keys) != len(set(self.selection_keys)):
            raise ValueError('选题不能重复')
        return self


def current(session, selection_key):
    return session.exec(select(DailyScriptGeneration).where(
        DailyScriptGeneration.active_selection_key == selection_key)).first()


def require_no_background(session, selection_key):
    if current(session, selection_key):
        raise wb.ScriptConflict('此选题已使用后台生成；请在每日选题查看进度或重试。')


def require_current(session, task_id):
    row = session.get(DailyScriptGeneration, task_id, populate_existing=True)
    pick = session.get(DailySelection, row.selection_key, populate_existing=True) if row else None
    if not row or row.active_selection_key != row.selection_key or not pick or not pick.active:
        raise TaskConflict('此任务对应的选题已移出或已被新任务替代；请回每日选题重新生成。')
    if pick.package_key and pick.package_key != row.package_key:
        raise TaskConflict('此选题已有脚本，不能用旧任务覆盖。')
    return row, pick


def detach_selection(session, selection_key):
    """Called under choose's write lock, so a late result cannot reattach itself."""
    row = current(session, selection_key)
    if not row:
        return
    row.active_selection_key = None
    session.add(row)
    task = session.get(TaskRecord, row.task_id)
    if task and task.status in {'queued', 'running'}:
        task.status = 'cancelled' if task.status == 'queued' else 'cancel_requested'
        if task.status == 'cancelled':
            task.finished_at = utc_now_iso()
        add_event(session, task, task.status)


def _result(row, task, pick, *, reused):
    key = pick.package_key or (row.package_key if row else None)
    return dict(selection_key=pick.selection_key, task=task_dict(task) if task else None,
                package_key=key, script_url='/scripts/' + key if key else None, reused=reused)


def submit_many(engine, data):
    request = DailyBatchRequest.model_validate(data)
    results = []
    with Session(engine) as session:
        lock_news(session)
        for selection_key in request.selection_keys:
            pick = session.get(DailySelection, selection_key)
            if not pick or not pick.active:
                raise KeyError('选题已取消或不存在，请刷新选题列表')
            row = current(session, selection_key)
            old = session.get(TaskRecord, row.task_id) if row else None
            if pick.package_key:
                results.append(_result(row, old, pick, reused=True))
                continue
            if row:
                spec = json.loads(row.spec_json)
                if spec['mode'] == request.mode:
                    results.append(_result(row, old, pick, reused=True))
                    continue
                if old.status not in TERMINAL:
                    raise TaskConflict('选题正在使用另一种方式生成；请先取消并等待任务结束，再切换生成方式。')
                row.active_selection_key = None
                session.add(row)
                session.flush()
            link = session.exec(select(ArticleEventLink).where(ArticleEventLink.article_key == pick.article_key)
                                .order_by(ArticleEventLink.id)).first()
            if not link:
                raise ValueError('所选新闻缺少事件归属，请先检查事件关联')
            editorial = build_editorial_pack(session, link.event_key, article_keys=[pick.article_key])
            if not editorial.get('sources') or not editorial['sources'][0].get('excerpt', '').strip():
                raise ValueError('所选新闻没有可用正文，请补充资料后再生成')
            scope = dict(selection_key=pick.selection_key, day=pick.day, timezone=pick.timezone)
            editorial['daily_selection'] = scope
            spec = dict(schema_version=1, mode=request.mode, snapshot_at=utc_now_iso(), editorial=editorial)
            if request.mode == 'llm':
                settings = get_settings()
                spec['model_target'] = dict(base_url=settings.base_url, model=settings.model)
            encoded = wb.canonical(spec)
            task = TaskRecord(task_id=uuid4().hex, profile_name=editorial['event_title'], trigger_type='daily_script',
                              profile_json=wb.canonical(dict(kind='daily_script', date_str=pick.day, timezone=pick.timezone)),
                              request_hash=wb.digest(encoded.encode()))
            row = DailyScriptGeneration(task_id=task.task_id, selection_key=selection_key,
                active_selection_key=selection_key, event_key=link.event_key,
                spec_json=encoded, input_hash=task.request_hash)
            session.add(row)
            add_event(session, task, 'queued')
            results.append(_result(row, task, pick, reused=False))
        # The full batch commits before any worker can claim a member.
        session.commit()
    return dict(items=results)


def _detail(session, row):
    spec = json.loads(row.spec_json)
    pick = session.get(DailySelection, row.selection_key)
    task = session.get(TaskRecord, row.task_id)
    scope = spec['editorial']['daily_selection']
    source = spec['editorial']['sources'][0]
    is_current = bool(row.active_selection_key and pick and pick.active)
    can_retry = is_current and not pick.package_key and task.status in {'failed', 'cancelled'}
    return dict(task=task_dict(task), selection_key=row.selection_key, event_key=row.event_key,
        title=spec['editorial']['event_title'], mode=spec['mode'], snapshot_at=spec['snapshot_at'],
        day=scope['day'], timezone=scope['timezone'], input_hash=row.input_hash,
        source=dict(article_key=source['article_key'], title=source['title'], revision=source['article_revision'],
                    content_hash=source['article_content_hash']),
        current=is_current, can_retry=can_retry, package_key=row.package_key,
        script_url='/scripts/' + row.package_key if row.package_key else None,
        daily_url='/daily?' + urlencode(dict(day=scope['day'], timezone=scope['timezone'])))


def detail(engine, task_id):
    with Session(engine) as session:
        row = session.get(DailyScriptGeneration, task_id)
        if not row:
            raise KeyError('每日脚本生成任务不存在')
        return _detail(session, row)


def list_generations(engine, *, day='', timezone='', event_key='', limit=30):
    with Session(engine) as session:
        query = select(DailyScriptGeneration).join(DailySelection, DailySelection.selection_key == DailyScriptGeneration.selection_key)
        if day:
            day, timezone = daily.selection_scope(day, timezone)
            query = query.where(DailySelection.day == day, DailySelection.timezone == timezone)
        if event_key:
            query = query.where(DailyScriptGeneration.event_key == event_key)
        rows = session.exec(query.order_by(DailyScriptGeneration.created_at.desc(), DailyScriptGeneration.task_id.desc()).limit(limit)).all()
        return [_detail(session, row) for row in rows]


def selection_tasks(session, selection_keys):
    if not selection_keys:
        return {}
    rows = session.exec(select(DailyScriptGeneration).where(DailyScriptGeneration.active_selection_key.in_(selection_keys))).all()
    return {row.selection_key: _detail(session, row) for row in rows}


def validate_document(spec, value):
    document = wb.ScriptDocument.model_validate(value).model_dump()
    sources = spec['editorial']['sources']
    if document['source_keys'] != [s['article_key'] for s in sources] or document['segments']:
        raise ValueError('生成稿与固定的每日选题来源不一致')
    wb._validate_sources(document, sources, spec['editorial'])
    return document


def run_daily_generation(engine, task, storage_root, context):
    with Session(engine) as session:
        row, _ = require_current(session, task.task_id)
        spec, input_hash = json.loads(row.spec_json), row.input_hash
    if wb.digest(wb.canonical(spec).encode()) != input_hash:
        raise ValueError('每日脚本生成输入校验失败')
    context.progress('daily_writing', 0, 3)
    cached = context.checkpoint('daily_document')
    if cached:
        if (cached.get('input_hash') != input_hash or
                cached.get('document_hash') != wb.digest(wb.canonical(cached.get('document')).encode())):
            raise ValueError('每日脚本检查点与输入或生成稿不匹配')
        document = validate_document(spec, cached['document'])
    else:
        document = daily.draft_document(copy.deepcopy(spec['editorial']), spec['mode'], model_target=spec.get('model_target'))
        context.check()
        document = validate_document(spec, document)
        context.progress('daily_validating', 1, 3)
        context.save_checkpoint('daily_document', dict(input_hash=input_hash, document=document,
            document_hash=wb.digest(wb.canonical(document).encode())))
    context.progress('daily_saving', 2, 3)
    folder = Path(storage_root).resolve() / 'packages' / ('pkg_' + task.task_id) / ('generation_' + uuid4().hex)
    committed = False
    try:
        with Session(engine) as session:
            context.fence(session)
            row, pick = require_current(session, task.task_id)
            if not row.package_key:
                folder.mkdir(parents=True, exist_ok=False)
                package = ScriptPackage(package_key='pkg_' + task.task_id, event_key=row.event_key,
                    profile_name='daily_' + spec['mode'], script_text=document['script_text'], output_dir=str(folder))
                editorial = copy.deepcopy(spec['editorial'])
                editorial['generation'] = dict(mode=spec['mode'], label='AI 中文初稿' if spec['mode']=='llm' else '基础摘录稿',
                    task_id=task.task_id, snapshot_at=spec['snapshot_at'])
                if spec['mode'] == 'llm':
                    editorial['generation']['model'] = spec['model_target']['model']
                wb.initialize_script(session, package, editorial, document=document)
                row.package_key = pick.package_key = package.package_key
                session.add(row)
                session.add(pick)
            context.complete(session)
            session.commit()
            committed = True
    finally:
        if not committed:
            shutil.rmtree(folder, ignore_errors=True)

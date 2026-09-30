"""Versioned script editing. Database snapshots are authoritative for exports."""
from __future__ import annotations

import hashlib
from html import escape
import io
import json
from pathlib import Path
import shutil
from typing import Literal
from uuid import uuid4
import zipfile

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..storage.models import (ScriptPackage, ScriptState, ScriptRevision,
                              ScriptReviewEvent, ScriptExport, utc_now_iso)
from ..storage.utils import loads


class ScriptConflict(ValueError):
    pass


class ScriptSegment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    segment_key: str = Field(min_length=1, max_length=80)
    kind: Literal['fact', 'analysis'] = 'fact'
    text: str = Field(min_length=1, max_length=5000)
    moment_keys: list[str] = Field(min_length=1, max_length=12)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    visual: str = Field(default='', max_length=2000)
    assets: str = Field(default='', max_length=2000)

    @model_validator(mode='after')
    def valid(self):
        self.text = self.text.strip()
        if not self.text or not self.segment_key.strip():
            raise ValueError('段落标识和正文不能为空')
        if len(set(self.moment_keys)) != len(self.moment_keys) or len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError('段落中的节点与来源不能重复')
        return self


def segment_narration(segments):
    return '\n\n'.join(('分析：' if s['kind'] == 'analysis' else '') + s['text'] for s in segments)


class ScriptDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=300)
    script_text: str = Field(min_length=1, max_length=50000)
    target_duration_sec: int = Field(default=60, ge=10, le=600)
    visual_notes: str = Field(default='', max_length=10000)
    notes: str = Field(default='', max_length=5000)
    source_keys: list[str] = Field(default_factory=list, max_length=20)
    segments: list[ScriptSegment] = Field(default_factory=list, max_length=30)

    @model_validator(mode='after')
    def consistent_segments(self):
        if self.segments:
            if len({s.segment_key for s in self.segments}) != len(self.segments):
                raise ValueError('段落标识不能重复')
            if self.script_text != segment_narration([s.model_dump() for s in self.segments]):
                raise ValueError('口播全文必须与分段正文一致；请在分段编辑中修改')
        return self

    @field_validator('title', 'script_text')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('title and script_text cannot be blank')
        return value.strip()

    @field_validator('source_keys')
    @classmethod
    def distinct_sources(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('source_keys must be unique')
        return value


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def revision_key(package_key, revision):
    return f'{package_key}:{revision}'


def get_package(session, package_key):
    row = session.exec(select(ScriptPackage).where(ScriptPackage.package_key == package_key)).first()
    if row is None:
        raise KeyError('script package not found')
    return row


def get_revision(session, package_key, revision):
    row = session.get(ScriptRevision, revision_key(package_key, revision))
    if row is None:
        raise KeyError('script revision not found')
    return row


def revision_dict(row, *, full=False):
    value = {'revision': row.revision, 'content_hash': row.content_hash,
             'created_at': row.created_at, 'change_note': row.change_note,
             'restored_from': row.restored_from}
    if full:
        value.update(loads(row.payload_json, {}))
    return value


def review_dict(row):
    return {name: getattr(row, name) for name in
            ('id', 'revision', 'state_version', 'action', 'status', 'note', 'reviewer', 'created_at')} | {
                'checks': loads(row.checks_json, {})}


def export_dict(row):
    return {name: getattr(row, name) for name in
            ('export_key', 'package_key', 'revision', 'state_version', 'content_hash',
             'archive_sha256', 'size_bytes', 'created_at')} | {
                'download_url': f'/api/scripts/{row.package_key}/exports/{row.export_key}/download'}


def script_detail(session, package_key):
    row = get_package(session, package_key)
    state = session.get(ScriptState, package_key)
    result = {'package_key': row.package_key, 'event_key': row.event_key,
              'profile_name': row.profile_name, 'output_dir': row.output_dir,
              'script_text': row.script_text, 'script_json': loads(row.script_json, {}),
              'tts_status': row.tts_status, 'created_at': row.created_at, 'legacy': state is None}
    if state:
        result.update({'revision': state.current_revision, 'version': state.version,
                       'status': state.status, 'approved_revision': state.approved_revision,
                       'updated_at': state.updated_at})
        result.update(revision_dict(get_revision(session, package_key, state.current_revision), full=True))
    else:
        result.update({'revision': 0, 'version': 0, 'status': 'legacy', 'document': None, 'sources': []})
    return result


def list_scripts(session, *, event_key='', limit=50):
    query = select(ScriptPackage, ScriptState).outerjoin(
        ScriptState, ScriptPackage.package_key == ScriptState.package_key)
    if event_key:
        query = query.where(ScriptPackage.event_key == event_key)
    rows = session.exec(query.order_by(ScriptPackage.id.desc()).limit(max(1, min(limit, 200))))
    return [{'package_key': row.package_key, 'event_key': row.event_key,
             'title': loads(row.script_json, {}).get('document', {}).get('title', ''),
             'profile_name': row.profile_name, 'created_at': row.created_at,
             'status': state.status if state else 'legacy',
             'revision': state.current_revision if state else 0,
             'updated_at': state.updated_at if state else row.created_at} for row, state in rows]


def _audit(session, state, action, note='', reviewer='', checks=None):
    session.add(ScriptReviewEvent(package_key=state.package_key, revision=state.current_revision,
                 state_version=state.version, action=action, status=state.status, note=note,
                 reviewer=reviewer, checks_json=canonical(checks or {})))


def _lock(session, package_key, expected_version):
    result = session.exec(update(ScriptState).where(ScriptState.package_key == package_key,
                          ScriptState.version == expected_version).values(updated_at=utc_now_iso())
                          .execution_options(synchronize_session=False))
    state = session.get(ScriptState, package_key, populate_existing=True)
    if state is None:
        get_package(session, package_key)
        raise ScriptConflict('旧脚本包尚未启用版本管理')
    if not result.rowcount:
        raise ScriptConflict('脚本已在其他页面更新，请保留本地修改并重新加载后合并')
    return state


def _validate_sources(document, sources, editorial=None):
    allowed = {s['article_key'] for s in sources}
    if set(document['source_keys']) - allowed:
        raise ValueError('所选来源不在此脚本的来源快照中')
    evidence = (editorial or {}).get('evidence')
    if evidence and not document.get('segments'):
        raise ValueError('有证据的脚本必须保留逐段引用，请在分段编辑中修改')
    if document.get('segments'):
        if not evidence:
            raise ValueError('此稿没有冻结的事件节点，请从事件专题创建分段草稿')
        refs = {s['evidence_id']: s for s in sources}
        moments = {m['moment_key']: set(m['evidence_ids']) for m in evidence['moments']}
        for part in document['segments']:
            cited, nodes = set(part['evidence_ids']), set(part['moment_keys'])
            if cited - refs.keys() or nodes - moments.keys():
                raise ValueError('段落引用不在冻结的来源或节点中')
            if cited - set().union(*(moments[n] for n in nodes)) or any(not cited & moments[n] for n in nodes):
                raise ValueError('段落引用必须对应所选节点的来源')
            if {refs[k]['article_key'] for k in cited} - set(document['source_keys']):
                raise ValueError('段落使用的来源不能取消勾选')


def _payload(editorial, document, *, origin='generated'):
    sources = editorial.get('sources') or []
    _validate_sources(document, sources, editorial)
    return {'schema_version': 1, 'document': document, 'sources': sources,
            'editorial': editorial, 'snapshot_origin': origin, 'snapshot_at': utc_now_iso()}


def _revision_files(row, payload):
    # Retain the former top-level script_text/editorial/profile_name contract.
    document = payload['document']
    legacy = dict(payload, script_text=document['script_text'], profile_name=row.profile_name)
    files = {'script.txt': document['script_text'], 'script.json': json.dumps(legacy, ensure_ascii=False, indent=2),
            'assets_manifest.json': json.dumps({'images': [], 'video_clips': [],
              'visual_notes': document['visual_notes'], 'storyboard': document.get('segments', []),
              'status': 'unassigned'}, ensure_ascii=False, indent=2)}
    if document.get('segments'):
        files['storyboard.json'] = json.dumps(document['segments'], ensure_ascii=False, indent=2)
        files['evidence_snapshot.json'] = json.dumps(payload['editorial']['evidence'], ensure_ascii=False, indent=2)
    return files


def _persist_revision(session, package, state, payload, note='', restored_from=None):
    folder = Path(state.root_dir) / 'revisions' / f'r{state.current_revision:06d}_{uuid4().hex}'
    folder.mkdir(parents=True, exist_ok=False)
    try:
        files = _revision_files(package, payload)
        for name, value in files.items():
            (folder / name).write_text(value, encoding='utf-8')
        row = ScriptRevision(revision_key=revision_key(package.package_key, state.current_revision),
                 package_key=package.package_key, revision=state.current_revision,
                 payload_json=canonical(payload), content_hash=digest(canonical(payload).encode()),
                 output_dir=str(folder), change_note=note, restored_from=restored_from)
        session.add(row)
        package.script_text = payload['document']['script_text']
        package.script_json = files['script.json']
        package.output_dir = str(folder)
        package.tts_status = 'pending'
        session.add(package)
        session.add(state)
        session.flush()
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return folder


def initialize_script(session, package, editorial, *, origin='generated', document=None):
    """Caller commits this initial revision together with the ScriptPackage."""
    document = ScriptDocument.model_validate(document).model_dump() if document else ScriptDocument(title=editorial.get('event_title') or package.event_key,
                              script_text=package.script_text,
                              source_keys=[s['article_key'] for s in editorial.get('sources', [])]).model_dump()
    state = ScriptState(package_key=package.package_key, root_dir=package.output_dir)
    folder = _persist_revision(session, package, state, _payload(editorial, document, origin=origin), '初始草稿')
    _audit(session, state, 'created' if origin == 'generated' else 'adopted')
    return folder


def adopt_legacy(session, package_key):
    package = get_package(session, package_key)
    if session.get(ScriptState, package_key):
        return script_detail(session, package_key)
    from ..editorial.service import build_editorial_pack
    old_payload = loads(package.script_json, {})
    editorial = old_payload.get('editorial') or {'event_title': package.event_key}
    # Legacy files did not retain article identities. Capture current references
    # explicitly as a new snapshot, never claim they are historical evidence.
    if not editorial.get('sources'):
        try:
            current = build_editorial_pack(session, package.event_key)
            editorial = dict(editorial, sources=current.get('sources', []))
        except KeyError:
            editorial = dict(editorial, sources=[])
    folder = None
    try:
        folder = initialize_script(session, package, editorial, origin='legacy_adopted_now')
        session.commit()
    except IntegrityError:
        session.rollback()
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        if not session.get(ScriptState, package_key):
            raise
    except Exception:
        session.rollback()
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    return script_detail(session, package_key)


def save_script(session, package_key, *, expected_version, document, change_note='', restore_revision=None):
    folder = None
    try:
        state = _lock(session, package_key, expected_version)
        package = get_package(session, package_key)
        previous = loads(get_revision(session, package_key, state.current_revision).payload_json, {})
        if restore_revision is not None:
            payload = loads(get_revision(session, package_key, restore_revision).payload_json, {})
        else:
            document = ScriptDocument.model_validate(document).model_dump()
            _validate_sources(document, previous['sources'], previous.get('editorial'))
            payload = dict(previous, document=document)
            if payload == previous:
                session.rollback()
                return script_detail(session, package_key)
        state.current_revision += 1
        state.version += 1
        state.status, state.approved_revision = 'draft', None
        folder = _persist_revision(session, package, state, payload, change_note, restore_revision)
        _audit(session, state, 'restored' if restore_revision else 'saved', change_note)
        session.commit()
    except Exception:
        session.rollback()
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    return script_detail(session, package_key)


def review_script(session, package_key, *, expected_version, action, note='', reviewer='', checks=None):
    try:
        state = _lock(session, package_key, expected_version)
        checks = checks or {}
        if action == 'submit':
            if state.status != 'draft':
                raise ScriptConflict('只有草稿可以提交审核')
            state.status = 'in_review'
        elif action == 'approve':
            if state.status != 'in_review':
                raise ScriptConflict('请先提交当前版本审核')
            payload = loads(get_revision(session, package_key, state.current_revision).payload_json, {})
            if not payload['document']['source_keys']:
                raise ValueError('请至少选择一条来源，保存并核对后再通过审核')
            if checks.get('sources_checked') is not True or checks.get('wording_checked') is not True:
                raise ValueError('请确认已核对来源和口播内容')
            state.status, state.approved_revision = 'approved', state.current_revision
        elif action == 'request_changes':
            if state.status not in {'in_review', 'approved'}:
                raise ScriptConflict('当前版本未进入审核')
            if not note.strip():
                raise ValueError('退回修改需要填写原因')
            state.status, state.approved_revision = 'draft', None
        else:
            raise ValueError('unknown review action')
        state.version += 1
        session.add(state)
        _audit(session, state, action, note, reviewer, checks)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return script_detail(session, package_key)


def export_script(session, package_key, *, expected_version):
    archive = None
    created_file = False
    try:
        state = _lock(session, package_key, expected_version)
        if state.status != 'approved' or state.approved_revision != state.current_revision:
            raise ScriptConflict('只有当前审核通过的版本可以导出制作包')
        package = get_package(session, package_key)
        revision = get_revision(session, package_key, state.current_revision)
        key = 'exp_' + digest(f'{package_key}:{state.version}'.encode())[:24]
        existing = session.get(ScriptExport, key)
        if existing:
            # Do not return a broken download link if an archive was removed.
            read_export(session, package_key, key)
            result = export_dict(existing)
            session.rollback()
            return result
        payload = loads(revision.payload_json, {})
        selected = set(payload['document']['source_keys'])
        sources = [s for s in payload['sources'] if s['article_key'] in selected]
        reviews = [review_dict(r) for r in session.exec(select(ScriptReviewEvent).where(
                   ScriptReviewEvent.package_key == package_key,
                   ScriptReviewEvent.revision == revision.revision).order_by(ScriptReviewEvent.id))]
        files = {name: value.encode('utf-8') for name, value in _revision_files(package, payload).items()}
        files['revision.json'] = revision.payload_json.encode('utf-8')
        files['sources.json'] = json.dumps(sources, ensure_ascii=False, indent=2).encode()
        document = payload['document']
        markdown = (f"# {escape(document['title'])}\n\n{escape(document['script_text'])}\n\n"
                    f"## 画面提示\n\n{escape(document['visual_notes'])}\n\n## 来源\n\n" +
                    '\n'.join(f"- {escape(s['title'])} — {escape(s['url'])} ({escape(s['published_at'])})" for s in sources))
        if document.get('segments'):
            markdown += '\n\n## 分段引用与分镜\n\n' + '\n\n'.join(
                f"### 第 {i} 段 · {'分析 / 推测' if part['kind'] == 'analysis' else '事实陈述'}\n\n"
                f"{escape(part['text'])}\n\n来源：{escape('、'.join(part['evidence_ids']))}\n\n"
                f"画面：{escape(part['visual'])}\n\n素材需求：{escape(part['assets'])}"
                for i, part in enumerate(document['segments'], 1))
            markdown += '\n\n来源编号对应 sources.json；节点和固定原文见 evidence_snapshot.json。\n'
        files['script.md'] = markdown.encode()
        manifest = {'schema_version': 1, 'kind': 'news2douyin.script_package', 'export_key': key,
                    'package_key': package_key, 'event_key': package.event_key,
                    'revision': revision.revision, 'state_version': state.version,
                    'status': 'approved', 'content_hash': revision.content_hash,
                    'target_duration_sec': document['target_duration_sec'],
                    'duration_kind': 'editorial_target', 'review_history': reviews,
                    'created_at': utc_now_iso(), 'files': {n: digest(v) for n, v in files.items()}}
        files['manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zipped:
            for name, data in files.items():
                zipped.writestr(name, data)
        content = buffer.getvalue()
        folder = Path(state.root_dir) / 'exports'
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder / f'{key}_{uuid4().hex}.zip'
        # Unique name and exclusive creation; only committed rows are exposed.
        with archive.open('xb') as out:
            created_file = True
            out.write(content)
        row = ScriptExport(export_key=key, package_key=package_key, revision=revision.revision,
                 state_version=state.version, content_hash=revision.content_hash,
                 archive_sha256=digest(content), archive_path=str(archive), size_bytes=len(content))
        session.add(row)
        session.commit()
        created_file = False  # A committed archive must survive a later read error.
        session.refresh(row)
        return export_dict(row)
    except Exception:
        session.rollback()
        if created_file and archive:
            archive.unlink(missing_ok=True)
        raise


def read_export(session, package_key, export_key):
    row = session.get(ScriptExport, export_key)
    if row is None or row.package_key != package_key:
        raise KeyError('script export not found')
    try:
        content = Path(row.archive_path).read_bytes()
    except FileNotFoundError as exc:
        raise FileNotFoundError('导出包文件丢失，请恢复运行目录备份') from exc
    if digest(content) != row.archive_sha256:
        raise ScriptConflict('导出包校验失败，请恢复运行目录备份')
    return row, content

import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.editorial.service import build_editorial_pack
from news2douyin.server.app import create_app
from news2douyin.storage.models import (Article, ArticleEventLink, Event, ScriptPackage,
    ScriptRevision, ScriptState, ScriptReviewEvent, ScriptExport)
from news2douyin.video.service import build_script_package
from news2douyin.video import workbench as wb


def seed(session):
    session.add(Event(event_key='event', event_title='芯片市场动态', summary='行业报道摘要'))
    session.add(Article(article_key='source', title='新闻标题', content='来源原文数字为 12。',
                        url='https://example.com/news', source_domain='example.com', published_at='2026-09-29T00:00:00Z'))
    session.add(ArticleEventLink(article_key='source', event_key='event'))
    session.commit()


@pytest.fixture
def package(engine, tmp_path):
    with Session(engine) as s:
        seed(s)
        row = build_script_package(s, 'event', output_root=tmp_path / 'packages')
        return row.package_key


def get(engine, key):
    with Session(engine) as s:
        return wb.script_detail(s, key)


def approve(engine, key):
    with Session(engine) as s:
        current = wb.script_detail(s, key)
        current = wb.review_script(s, key, expected_version=current['version'], action='submit')
        return wb.review_script(s, key, expected_version=current['version'], action='approve',
                                checks={'sources_checked': True, 'wording_checked': True}, reviewer='编辑A')


def test_initial_snapshot_and_legacy_api_fields(engine, package):
    current = get(engine, package)
    assert current['revision'] == current['version'] == 1
    assert current['status'] == 'draft'
    assert '来源原文数字为 12' in current['script_text']
    assert current['script_text'] == current['document']['script_text']
    assert current['script_json']['script_text'] == current['script_text']
    assert current['sources'][0]['excerpt'] == '来源原文数字为 12。'
    assert Path(current['output_dir'], 'script.txt').read_text() == current['script_text']
    with Session(engine) as s:
        art = s.exec(select(Article)).one()
        art.content = '已被上游改成 99'
        s.add(art); s.commit()
        assert wb.script_detail(s, package)['sources'][0]['excerpt'] == '来源原文数字为 12。'


def test_saved_revision_is_immutable_and_approval_resets(engine, package):
    first = get(engine, package)
    approved = approve(engine, package)
    document = dict(first['document'], script_text='人工核对后的口播。', visual_notes='显示来源截图')
    with Session(engine) as s:
        current = wb.save_script(s, package, expected_version=approved['version'], document=document, change_note='核对数字')
        assert current['status'] == 'draft' and current['approved_revision'] is None
        assert current['revision'] == 2
        old = wb.get_revision(s, package, 1)
        assert json.loads(old.payload_json)['document']['script_text'] == first['script_text']
        assert Path(first['output_dir'], 'script.txt').read_text() == first['script_text']
        assert Path(current['output_dir'], 'script.txt').read_text() == document['script_text']
        assert current['output_dir'] != first['output_dir']
        assert current['script_json']['script_text'] == document['script_text']
        with pytest.raises(wb.ScriptConflict):
            wb.export_script(s, package, expected_version=current['version'])


def test_noop_save_does_not_revoke_approval(engine, package):
    current = approve(engine, package)
    with Session(engine) as s:
        unchanged = wb.save_script(s, package, expected_version=current['version'], document=current['document'])
        assert unchanged['revision'] == current['revision']
        assert unchanged['version'] == current['version']
        assert unchanged['status'] == 'approved'


def test_concurrent_edit_detects_lost_update(engine, package):
    first = get(engine, package)
    barrier = Barrier(2)
    def save(text):
        with Session(engine) as s:
            barrier.wait(timeout=3)
            try:
                return wb.save_script(s, package, expected_version=first['version'],
                                      document=dict(first['document'], script_text=text))['script_text']
            except wb.ScriptConflict:
                return 'conflict'
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(save, ['编辑甲', '编辑乙']))
    assert results.count('conflict') == 1
    assert get(engine, package)['script_text'] in set(results) - {'conflict'}
    with Session(engine) as s:
        assert len(list(s.exec(select(ScriptRevision)))) == 2


def test_stale_approval_cannot_approve_unseen_content(engine, package):
    first = get(engine, package)
    with Session(engine) as s:
        reviewing = wb.review_script(s, package, expected_version=first['version'], action='submit')
        current = wb.save_script(s, package, expected_version=reviewing['version'],
                                 document=dict(first['document'], script_text='新内容'))
        with pytest.raises(wb.ScriptConflict):
            wb.review_script(s, package, expected_version=reviewing['version'], action='approve',
                             checks={'sources_checked': True, 'wording_checked': True})
        assert wb.script_detail(s, package)['status'] == 'draft'
        assert wb.script_detail(s, package)['version'] == current['version']


def test_restore_creates_new_draft_and_preserves_history(engine, package):
    first = get(engine, package)
    with Session(engine) as s:
        edited = wb.save_script(s, package, expected_version=1, document=dict(first['document'], script_text='第二版'))
        restored = wb.save_script(s, package, expected_version=edited['version'], document=None, restore_revision=1)
        assert restored['revision'] == 3 and restored['status'] == 'draft'
        assert restored['document'] == first['document']
        assert restored['content_hash'] == first['content_hash']
        assert restored['restored_from'] == 1
        assert json.loads(wb.get_revision(s, package, 2).payload_json)['document']['script_text'] == '第二版'


def test_review_requires_valid_transition_checks_and_sources(engine, package):
    first = get(engine, package)
    with Session(engine) as s:
        with pytest.raises(wb.ScriptConflict):
            wb.review_script(s, package, expected_version=1, action='approve', checks={'sources_checked': True,'wording_checked': True})
        no_sources = wb.save_script(s, package, expected_version=1, document=dict(first['document'], source_keys=[]))
        pending = wb.review_script(s, package, expected_version=no_sources['version'], action='submit')
        with pytest.raises(ValueError, match='来源'):
            wb.review_script(s, package, expected_version=pending['version'], action='approve',
                             checks={'sources_checked': True, 'wording_checked': True})
        with pytest.raises(ValueError, match='原因'):
            wb.review_script(s, package, expected_version=pending['version'], action='request_changes')
        returned = wb.review_script(s, package, expected_version=pending['version'], action='request_changes', note='缺少来源')
        assert returned['status'] == 'draft'
        current = wb.save_script(s, package, expected_version=returned['version'], document=first['document'])
        pending = wb.review_script(s, package, expected_version=current['version'], action='submit')
        with pytest.raises(ValueError, match='核对'):
            wb.review_script(s, package, expected_version=pending['version'], action='approve')


def test_source_ids_must_belong_to_snapshot(engine, package):
    first = get(engine, package)
    with Session(engine) as s, pytest.raises(ValueError, match='快照'):
        wb.save_script(s, package, expected_version=1, document=dict(first['document'], source_keys=['invented']))
    assert get(engine, package)['version'] == 1


def test_export_contains_exact_approved_snapshot_and_checksums(engine, package):
    approved = approve(engine, package)
    with Session(engine) as s:
        exported = wb.export_script(s, package, expected_version=approved['version'])
        assert wb.export_script(s, package, expected_version=approved['version'])['export_key'] == exported['export_key']
        row, content = wb.read_export(s, package, exported['export_key'])
        assert hashlib.sha256(content).hexdigest() == exported['archive_sha256']
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            assert manifest['status'] == 'approved' and manifest['revision'] == 1
            assert manifest['review_history'][-1]['reviewer'] == '编辑A'
            assert hashlib.sha256(archive.read('revision.json')).hexdigest() == manifest['content_hash']
            for name, checksum in manifest['files'].items():
                assert hashlib.sha256(archive.read(name)).hexdigest() == checksum
            assert archive.read('script.txt').decode() == approved['script_text']
            assert json.loads(archive.read('sources.json')) == approved['sources']
        wb.save_script(s, package, expected_version=approved['version'], document=dict(approved['document'], script_text='下一版'))
        assert wb.read_export(s, package, exported['export_key'])[1] == content
        with pytest.raises(KeyError):
            wb.read_export(s, 'another-package', exported['export_key'])


def test_concurrent_export_is_idempotent(engine, package):
    approved = approve(engine, package)
    barrier = Barrier(2)
    def export(_):
        with Session(engine) as s:
            barrier.wait(timeout=3)
            return wb.export_script(s, package, expected_version=approved['version'])['export_key']
    with ThreadPoolExecutor(2) as pool:
        keys = list(pool.map(export, range(2)))
    assert keys[0] == keys[1]
    with Session(engine) as s:
        assert len(list(s.exec(select(ScriptExport)))) == 1


def test_export_rejects_corrupt_or_missing_file(engine, package):
    approved = approve(engine, package)
    with Session(engine) as s:
        exported = wb.export_script(s, package, expected_version=approved['version'])
        row = s.get(ScriptExport, exported['export_key'])
        Path(row.archive_path).write_bytes(b'changed')
        with pytest.raises(wb.ScriptConflict, match='校验'):
            wb.read_export(s, package, row.export_key)
        Path(row.archive_path).unlink()
        with pytest.raises(FileNotFoundError):
            wb.export_script(s, package, expected_version=approved['version'])


def test_revision_commit_failure_leaves_current_state_intact(engine, package, monkeypatch):
    first = get(engine, package)
    root = Path(first['output_dir']).parent
    with Session(engine) as s:
        def fail():
            raise RuntimeError('commit failed')
        monkeypatch.setattr(s, 'commit', fail)
        with pytest.raises(RuntimeError, match='commit failed'):
            wb.save_script(s, package, expected_version=1, document=dict(first['document'], script_text='未提交修改'))
    current = get(engine, package)
    assert current['version'] == 1 and current['script_text'] == first['script_text']
    assert len(list(root.iterdir())) == 1


def test_export_commit_failure_removes_uncommitted_archive(engine, package, monkeypatch):
    approved = approve(engine, package)
    with Session(engine) as s:
        root = Path(s.get(ScriptState, package).root_dir)
        monkeypatch.setattr(s, 'commit', lambda: (_ for _ in ()).throw(RuntimeError('commit failed')))
        with pytest.raises(RuntimeError):
            wb.export_script(s, package, expected_version=approved['version'])
    assert not list((root / 'exports').glob('*.zip'))
    with Session(engine) as s:
        assert not list(s.exec(select(ScriptExport)))


def test_legacy_adoption_preserves_original_files_and_text(engine, tmp_path):
    folder = tmp_path / 'legacy'
    folder.mkdir()
    (folder / 'script.txt').write_text('原始口播')
    with Session(engine) as s:
        seed(s)
        s.add(ScriptPackage(package_key='old', event_key='event', script_text='原始口播',
                            script_json='{"editorial":{"event_title":"旧标题"}}', output_dir=str(folder)))
        s.commit()
        assert wb.script_detail(s, 'old')['legacy']
        assert not list(s.exec(select(ScriptState)))  # GET has no migration side effect.
        adopted = wb.adopt_legacy(s, 'old')
        assert adopted['script_text'] == '原始口播' and adopted['revision'] == 1
        assert adopted['snapshot_origin'] == 'legacy_adopted_now'
        assert adopted['sources'][0]['article_key'] == 'source'
        assert (folder / 'script.txt').read_text() == '原始口播'
        assert wb.adopt_legacy(s, 'old')['revision'] == 1


def test_editorial_chooses_unique_nondeduplicate_sources_first(engine):
    with Session(engine) as s:
        seed(s)
        s.add(ArticleEventLink(article_key='source', event_key='event'))
        s.add(Article(article_key='copy', title='转载', is_duplicate=True, published_at='2099-01-01'))
        s.add(ArticleEventLink(article_key='copy', event_key='event'))
        s.commit()
        sources = build_editorial_pack(s, 'event')['sources']
        assert [x['article_key'] for x in sources] == ['source', 'copy']


def test_webui_api_validation_workflow_and_safe_source_links(tmp_path):
    app = create_app(db_url=f'sqlite:///{tmp_path}/app.db', storage_root=str(tmp_path / 'runs'))
    with Session(app.state.engine) as s:
        seed(s)
        source = s.exec(select(Article)).one()
        source.title = '</script><script>alert(1)</script>'
        source.url = 'javascript:alert(1)'
        s.add(source); s.commit()
    with TestClient(app) as client:
        assert client.post('/api/scripts/build', json={'event_key':'missing'}).status_code == 404
        response = client.post('/webui/scripts/build', data={'event_key':'event'}, follow_redirects=False)
        assert response.status_code == 303
        path = response.headers['location']
        page = client.get(path)
        assert page.status_code == 200
        assert '<script>alert(1)</script>' not in page.text
        assert 'href="javascript:' not in page.text
        assert 'href="javascript:' not in client.get('/events/event').text
        key = path.split('/')[-1]
        base = '/api/scripts/' + key
        first = client.get(base).json()
        assert client.get('/scripts').status_code == 200
        assert len(client.get('/api/scripts?event_key=event').json()) == 1
        assert client.post(base+'/exports', json={'expected_version':1}).status_code == 409
        bad = dict(first['document'], script_text='   ')
        assert client.put(base, json={'expected_version':1,'document':bad}).status_code == 422
        edited = client.put(base, json={'expected_version':1,'document':dict(first['document'],script_text='人工修改')}).json()
        assert edited['revision'] == 2
        assert client.put(base,json={'expected_version':1,'document':first['document']}).status_code == 409
        pending = client.post(base+'/review',json={'expected_version':edited['version'],'action':'submit'}).json()
        approved = client.post(base+'/review',json={'expected_version':pending['version'],'action':'approve','sources_checked':True,'wording_checked':True}).json()
        exported = client.post(base+'/exports',json={'expected_version':approved['version']}).json()
        download = client.get(exported['download_url'])
        assert download.status_code == 200 and download.headers['content-type'] == 'application/zip'
        assert download.headers['etag'] == '"'+exported['archive_sha256']+'"'
        assert client.get(base+'/revisions?limit=1').json()[0]['revision'] == 2
        assert client.get(base+'/revisions?before=2').json()[0]['revision'] == 1
        assert client.get(base+'/revisions/99').status_code == 404
        restored = client.post(base+'/revisions/1/restore',json={'expected_version':approved['version']}).json()
        assert restored['revision'] == 3 and restored['status'] == 'draft'
        assert client.get(base+'/reviews').json()[0]['action'] == 'restored'
        assert client.get(base+'/exports').json()[0]['revision'] == 2
        assert client.get(exported['download_url']).content == download.content


def test_post_commit_refresh_failure_keeps_registered_files(engine, tmp_path, monkeypatch):
    with Session(engine) as s:
        seed(s)
        monkeypatch.setattr(s, 'refresh', lambda *_: (_ for _ in ()).throw(RuntimeError('read after commit failed')))
        with pytest.raises(RuntimeError):
            build_script_package(s, 'event', output_root=tmp_path / 'packages')
    with Session(engine) as s:
        row = s.exec(select(ScriptPackage)).one()
        key = row.package_key
        assert Path(row.output_dir, 'script.txt').is_file()
    approved = approve(engine, key)
    with Session(engine) as s:
        monkeypatch.setattr(s, 'refresh', lambda *_: (_ for _ in ()).throw(RuntimeError('read after commit failed')))
        with pytest.raises(RuntimeError):
            wb.export_script(s, key, expected_version=approved['version'])
    with Session(engine) as s:
        exported = s.exec(select(ScriptExport)).one()
        assert Path(exported.archive_path).is_file()
        assert wb.read_export(s, key, exported.export_key)[1]

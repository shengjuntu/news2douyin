import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

from news2douyin.collect import service as collect
from news2douyin.dedup.normalize import normalize_article
from news2douyin.dedup.service import decide_duplicate
from news2douyin.events.service import evidence_counts
from news2douyin.search.service import article_page, event_page
from news2douyin.server.app import create_app
from news2douyin.storage.articles import version_page
from news2douyin.storage.db import init_db, make_engine
from news2douyin.storage.models import (Article, ArticleEventLink, ArticleIdentity, ArticleVersion,
                                      CollectedObservation, Event, NewsWriteLock)
from news2douyin.tasks.service import TaskService
from news2douyin.tasks.worker import TaskWorker
from news2douyin.video.service import build_script_package
from news2douyin.video import workbench


def collect_items(session, monkeypatch, tmp_path, items):
    monkeypatch.setitem(collect.PROVIDERS, 'fixture', lambda _: items)
    collect.create_or_update_profile(session, {'name': 'fixture', 'provider': 'fixture'})
    return collect.run_collection(session, 'fixture', storage_root=tmp_path / 'runs')


def test_same_url_updates_versions_without_new_identity_or_event(engine, article, monkeypatch, tmp_path):
    with Session(engine) as session:
        first = collect_items(session, monkeypatch, tmp_path, [article])
        row = session.exec(select(Article)).one()
        key = row.article_key
        event_key = session.exec(select(Event.event_key)).one()
        changed = dict(article, title='Revised chip industry outlook', content='Corrected numbers and an additional source.',
                       published_at='2026-09-28T23:00:00-01:00')
        second = collect_items(session, monkeypatch, tmp_path, [changed])
        assert json.loads(second.stats_json)['updated_articles'] == 1
        assert json.loads(second.stats_json)['stored_articles'] == 0
        assert session.exec(select(Article)).one().article_key == key
        assert session.exec(select(Event)).one().article_count == 1
        assert session.exec(select(Event.event_key)).one() == event_key
        versions = version_page(session, key)['items']
        assert [v['revision'] for v in versions] == [2, 1]
        assert versions[1]['document']['content'] == article['content']
        assert versions[0]['document']['title'] == changed['title']
        assert json.loads((Path(first.storage_path) / 'articles.jsonl').read_text())['content'] == article['content']
        # Tracking and fetch metadata changes do not make a content revision.
        third = collect_items(session, monkeypatch, tmp_path, [dict(changed, url=article['url']+'?utm_source=test', fetched_at='later')])
        assert json.loads(third.stats_json)['skipped_existing'] == 1
        assert version_page(session, key)['total'] == 2
        # Returning to earlier content is an observed new version, not lost history.
        collect_items(session, monkeypatch, tmp_path, [article])
        assert version_page(session, key)['total'] == 3


def test_multiple_changes_in_one_run_export_exact_observed_versions(engine, article, monkeypatch, tmp_path):
    with Session(engine) as session:
        run = collect_items(session, monkeypatch, tmp_path, [article, dict(article, content='Second observed revision')])
        exported = [json.loads(line) for line in (Path(run.storage_path)/'articles.jsonl').read_text().splitlines()]
        assert [row['content'] for row in exported] == [article['content'], 'Second observed revision']
        assert len(list(session.exec(select(Article)))) == 1
        assert len(list(session.exec(select(ArticleVersion)))) == 2
        assert [o.revision for o in session.exec(select(CollectedObservation).order_by(CollectedObservation.input_index))] == [1, 2]


def test_update_is_atomic_including_search_index(engine, article, monkeypatch, tmp_path):
    with Session(engine) as session:
        collect_items(session, monkeypatch, tmp_path, [article])
        key = session.exec(select(Article.article_key)).one()
        def fail(*args):
            raise RuntimeError('injected event write failure')
        monkeypatch.setattr(collect, 'refresh_event', fail)
        with pytest.raises(RuntimeError, match='injected'):
            collect_items(session, monkeypatch, tmp_path, [dict(article, content='replacementXYZ')])
        assert version_page(session, key)['total'] == 1
        assert session.exec(select(Article)).one().content == article['content']
        assert article_page(session, query='replacementXYZ').total == 0
        assert article_page(session, query='semiconductor').total == 1


def test_new_script_records_article_version_and_old_script_stays_immutable(engine, article, monkeypatch, tmp_path):
    with Session(engine) as session:
        collect_items(session, monkeypatch, tmp_path, [article])
        key = session.exec(select(Event.event_key)).one()
        package = build_script_package(session, key, output_root=tmp_path / 'packages')
        before = workbench.script_detail(session, package.package_key)['sources']
        assert before[0]['article_revision'] == 1
        collect_items(session, monkeypatch, tmp_path, [dict(article, content='A revised source report.')])
        assert workbench.script_detail(session, package.package_key)['sources'] == before
        newer = build_script_package(session, key, output_root=tmp_path / 'packages')
        assert workbench.script_detail(session, newer.package_key)['sources'][0]['article_revision'] == 2


def test_upgrade_keeps_legacy_ids_duplicates_scripts_and_backfills_fts(tmp_path):
    engine = make_engine('sqlite:///' + str(tmp_path/'legacy.db'))
    # Create only the exact old tables relevant to this fixture, without new tables.
    for model in (Article, Event, ArticleEventLink):
        model.__table__.create(engine)
    with Session(engine) as session:
        session.add_all([Article(article_key='legacy1', url='https://legacy.test/a', title='旧库中文检索', content='版本之前的内容'),
                         Article(article_key='legacy2', url='https://legacy.test/a', title='历史副本'),
                         Event(event_key='keep-event'), ArticleEventLink(article_key='legacy1', event_key='keep-event')])
        session.commit()
    init_db(engine)
    init_db(engine)
    with Session(engine) as session:
        assert set(session.exec(select(Article.article_key))) == {'legacy1', 'legacy2'}
        assert len(list(session.exec(select(ArticleVersion)))) == 2
        assert len(list(session.exec(select(ArticleIdentity)))) == 1
        assert session.exec(select(ArticleIdentity)).one().article_key == 'legacy1'
        assert version_page(session, 'legacy1')['items'][0]['origin'] == 'legacy_baseline'
        assert article_page(session, query='中文检索').items[0].article_key == 'legacy1'
        assert session.exec(select(ArticleEventLink)).one().event_key == 'keep-event'
    engine.dispose()


def test_exact_copy_lookup_not_limited_to_recent_1000(engine, article):
    n = normalize_article(article['title'], article['content'], article['url'])
    with Session(engine) as session:
        session.add(Article(article_key='old-original', title=article['title'], content=article['content'],
                            normalized_title=n.normalized_title, normalized_content=n.normalized_content,
                            title_hash=n.title_hash, content_hash=n.content_hash))
        session.add_all([Article(article_key=f'unrelated-{i}', title=f'Other {i}') for i in range(1005)])
        session.commit()
        assert decide_duplicate(session, dict(article, url='https://copy.test/new')).duplicate_of_article_key == 'old-original'


def test_concurrent_updates_make_one_revision(engine, article, monkeypatch, tmp_path):
    with Session(engine) as session:
        collect_items(session, monkeypatch, tmp_path, [article])
    barrier = Barrier(2)
    def fetch(_):
        barrier.wait(timeout=5)
        return [dict(article, content='Concurrent correction')]
    monkeypatch.setitem(collect.PROVIDERS, 'fixture', fetch)
    def run(_):
        with Session(engine) as session:
            return collect.run_collection(session, 'fixture', storage_root=tmp_path/'parallel').status
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(run, range(2))) == ['succeeded', 'succeeded']
    with Session(engine) as session:
        assert len(list(session.exec(select(ArticleVersion)))) == 2
        assert session.exec(select(Event)).one().article_count == 1


def test_retry_does_not_reapply_completed_update(engine, article, monkeypatch, tmp_path):
    with Session(engine) as session:
        collect_items(session, monkeypatch, tmp_path, [article])
    monkeypatch.setitem(collect.PROVIDERS, 'fixture', lambda _: [dict(article, content='Queued correction')])
    tasks = TaskService(engine)
    task = tasks.submit('fixture')
    real_write = collect.write_jsonl
    def fail_export(path, rows):
        if Path(path).name == 'articles.jsonl':
            raise RuntimeError('injected export failure')
        return real_write(path, rows)
    monkeypatch.setattr(collect, 'write_jsonl', fail_export)
    worker = TaskWorker(engine, tmp_path/'tasks')
    worker.execute(tasks.claim())
    assert tasks.get(task['task_id'])['status'] == 'failed'
    monkeypatch.setattr(collect, 'write_jsonl', real_write)
    # A different collector moves the current article on before retrying the task.
    with Session(engine) as session:
        collect_items(session, monkeypatch, tmp_path, [dict(article, content='Later unrelated correction')])
    tasks.retry(task['task_id'])
    worker.execute(tasks.claim())
    result = tasks.get(task['task_id'])
    assert result['status'] == 'succeeded'
    from news2douyin.storage.models import RunRecord
    with Session(engine) as session:
        assert len(list(session.exec(select(ArticleVersion)))) == 3
        assert session.exec(select(Article)).one().content == 'Later unrelated correction'
        run = session.get(RunRecord, result['run_id'])
        assert json.loads(run.stats_json)['updated_articles'] == 1
        exported = json.loads((Path(run.storage_path)/'articles.jsonl').read_text())
        assert exported['content'] == 'Queued correction'


def test_search_full_body_terms_total_offset_and_trigger_updates(engine):
    with Session(engine) as session:
        session.add_all([Article(article_key=f'a{i}', title='芯片产业', content='z'*500+' 量子计算 needle', country='cn') for i in range(4)])
        session.add(Article(article_key='other', title='芯片产业', content='unrelated', country='us'))
        session.add(Event(event_key='event', event_title='不含关键词', summary='摘要'))
        session.add(ArticleEventLink(article_key='a0', event_key='event'))
        session.commit()
        page = article_page(session, query='芯片 needle', mode='terms', country='cn', limit=2, offset=2)
        assert page.total == 4 and [r.article_key for r in page.items] == ['a1', 'a0']
        assert event_page(session, query='量子计算').total == 1
        row = session.exec(select(Article).where(Article.article_key == 'a0')).one()
        row.content = 'new searchableword'
        session.add(row); session.commit()
        assert event_page(session, query='量子计算').total == 0
        assert event_page(session, query='searchableword').total == 1
        session.delete(row); session.commit()
        assert article_page(session, query='searchableword').total == 0


@pytest.mark.parametrize('query', ['芯片', '量子计算', '%', '_', '" OR *', 'café', 'NEEDLE'])
def test_fts_and_sql_fallback_have_identical_literal_results(engine, monkeypatch, query):
    from news2douyin.search import service
    with Session(engine) as session:
        session.add_all([Article(article_key='target', title='芯片量子计算 café NEEDLE 10% x_y " OR *'),
                         Article(article_key='other', title='unrelated')])
        session.commit()
        indexed = article_page(session, query=query)
        monkeypatch.setattr(service, 'index_available', lambda _: False)
        fallback = article_page(session, query=query)
        assert indexed.total == fallback.total == 1
        assert [r.article_key for r in indexed.items] == [r.article_key for r in fallback.items]


def test_news_api_web_paging_and_immutable_version_page(tmp_path, article, monkeypatch):
    app = create_app(db_url='sqlite:///' + str(tmp_path/'api.db'), storage_root=str(tmp_path/'runs'))
    with Session(app.state.engine) as session:
        collect_items(session, monkeypatch, tmp_path, [article, dict(article, url='https://other.test/copy')])
        key = session.exec(select(Article.article_key).order_by(Article.id)).first()
        event_key = session.exec(select(Event.event_key)).one()
        collect_items(session, monkeypatch, tmp_path, [dict(article, content='<script>alert(1)</script> Corrected')])
    client = TestClient(app)
    params = {'limit': 1, 'offset': 1}
    response = client.get('/api/articles/search', params=params)
    assert isinstance(response.json(), list) and response.headers['X-Total-Count'] == '2'
    page = client.get('/api/articles', params={**params, 'paginated': 'true'}).json()
    assert page['items'] == response.json() and page['total'] == 2 and page['has_more'] is False
    html = client.get('/articles', params={'limit': 1, 'q': 'chip'})
    assert html.status_code == 200 and '下一页' in html.text
    versions = client.get(f'/api/articles/{key}/versions').json()
    assert versions['total'] == 2
    html = client.get(f'/articles/{key}', params={'revision': 2})
    assert html.status_code == 200 and '正文差异' in html.text
    assert '<script>alert(1)</script>' not in html.text and '&lt;script&gt;' in html.text
    counts = client.get(f'/api/events/{event_key}').json()
    assert counts['article_count'] == 2 and counts['independent_article_count'] == 1
    assert client.get(f'/events/{event_key}').status_code == 200
    assert client.get(f'/api/articles/{key}/versions/1').json()['document']['content'] == article['content']
    assert client.get(f'/api/articles/{key}/versions/999').status_code == 404
    for params in [{'offset': -1}, {'limit': 501}, {'mode':'bogus'}, {'query':'x'*201}, {'duplicates':'bogus'}]:
        assert client.get('/api/articles', params=params).status_code == 422

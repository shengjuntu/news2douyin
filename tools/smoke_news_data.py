"""Offline installed-wheel check. Use --serve PORT for browser verification."""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.collect import service as collect, llm_filter
from news2douyin.server import webui
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article, Event


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--serve', type=int, default=0)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root/'news.db').exists():
        parser.error('Use a new output directory; an existing database will not be overwritten')
    with patch.object(llm_filter, 'endpoint_alive', return_value=False), patch.object(webui, '_llm_alive', return_value=False):
        app = create_app(db_url='sqlite:///' + str(root/'news.db'), storage_root=str(root/'runs'))
        first = {'title':'华星发布新款人工智能芯片','content':'华星在发布会上介绍新款人工智能芯片，展示了推理任务的性能测试。报道记录了工程师对产品工艺、功耗和交付安排的说明，并附上公开技术材料供读者核对。',
                 'country':'cn','language':'zh','published_at':'2026-09-29T00:00:00Z','url':'https://source.example/story','source':{'domain':'source.example'}}
        second = dict(first, title='华星发布人工智能芯片新品', url='https://independent.example/story', source={'domain':'independent.example'},
                      content='围绕华星人工智能芯片发布，另一家媒体采访了设备采购方。新款产品的推理测试、功耗和交付安排受到关注，采购方表示还需要结合实际业务进行验证。')
        items = [first, second, dict(first, url='https://copy.example/story', source={'domain':'copy.example'})]
        with patch.dict(collect.PROVIDERS, {'fixture':lambda _: items}):
            with Session(app.state.engine) as session:
                collect.create_or_update_profile(session, {'name':'fixture','provider':'fixture'})
                collect.run_collection(session, 'fixture', storage_root=root/'runs')
                key = session.exec(select(Article.article_key).order_by(Article.id)).first()
                event_key = session.exec(select(Event.event_key)).one()
                items[:] = [dict(first, content=first['content']+'\n更新：交付时间调整为十月，待核对实际供货情况。')]
                collect.run_collection(session, 'fixture', storage_root=root/'runs')
        checks = []
        with TestClient(app) as client:
            def check(name, condition):
                assert condition, name
                checks.append(name)
            page = client.get('/api/articles', params={'query':'芯片','limit':2,'paginated':'true'}).json()
            check('filtered_total', page['total'] == 3 and page['has_more'])
            next_page = client.get('/api/articles', params={'query':'芯片','limit':2,'offset':2,'paginated':'true'}).json()
            check('next_page', len(next_page['items']) == 1 and not next_page['has_more'])
            check('immutable_original', client.get(f'/api/articles/{key}/versions/1').json()['document']['content'] == first['content'])
            check('latest_revision', client.get(f'/api/articles/{key}').json()['revision'] == 2)
            check('version_history', client.get(f'/api/articles/{key}/versions').json()['total'] == 2)
            counts = client.get('/api/events/'+event_key).json()
            check('source_counts', counts['independent_article_count'] == 2 and counts['duplicate_article_count'] == 1)
            check('related_article_fulltext', len(client.get('/api/events/search', params={'query':'采购方'}).json()) == 1)
            check('multi_term', len(client.get('/api/articles/search', params={'query':'华星 交付','mode':'terms'}).json()) == 3)
            check('legacy_array_api', isinstance(client.get('/api/articles/search').json(), list))
            check('article_page_resource', client.get('/articles/'+key).status_code == 200)
            check('event_page_resource', client.get('/events/'+event_key).status_code == 200)
            check('pagination_resource', '下一页' in client.get('/articles?limit=2').text)
            script = client.post('/api/scripts/build', json={'event_key':event_key}).json()
            check('script_article_revision', next(s for s in script['sources'] if s['article_key'] == key)['article_revision'] == 2)
            for action in ['submit', 'approve']:
                response = client.post('/api/scripts/'+script['package_key']+'/review',
                    json={'expected_version':script['version'], 'action':action,
                          'sources_checked':True, 'wording_checked':True})
                assert response.status_code == 200
                script = response.json()
            exported = client.post('/api/scripts/'+script['package_key']+'/exports',
                                   json={'expected_version':script['version']})
            check('versioned_source_export', exported.status_code == 200)
            (root/'script-package.zip').write_bytes(client.get(exported.json()['download_url']).content)
        report = {'checks':checks,'passed':len(checks),'article_key':key,'event_key':event_key,
                  'scope':'offline fixture; no real news/LLM calls'}
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(report,ensure_ascii=False))
        if args.serve:
            import uvicorn
            uvicorn.run(app, host='127.0.0.1', port=args.serve, log_level='warning')


if __name__ == '__main__':
    main()

"""Run with python -I after installing the wheel, outside the source checkout.

Uses disposable SQLite and a deterministic translation provider. No remote
model, news source, graph server, or deployed data is contacted.
"""
import importlib.metadata
import importlib.resources
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

for flag in ('NEWS2DOUYIN_GKG_STREAM_ENABLED','NEWS2DOUYIN_GKG_PROJECTION_ENABLED',
             'NEWS2DOUYIN_GDELT_STREAM_ENABLED','NEWS2DOUYIN_GDELT_PROJECTION_ENABLED'):
    os.environ[flag]='0'
os.environ['NEWS2DOUYIN_FALKOR_URL']=''
os.environ['NEWS2DOUYIN_SOURCE_CONTENT_ENABLED']='0'

import news2douyin
from fastapi.testclient import TestClient
from sqlmodel import Session,select
from news2douyin.intake.feeds import FEEDS
from news2douyin.intake.official_models import OfficialEvent
from news2douyin.server.app import create_app
from news2douyin.storage.models import utc_now_iso
from news2douyin.topics.models import TopicSource


def main():
    version=importlib.metadata.version('news2douyin')
    assert version=='0.45.1'
    assert Path(news2douyin.__file__).resolve().parent!=Path(__file__).resolve().parents[1]/'src/news2douyin'
    assert len(FEEDS)==6
    resources=importlib.resources.files('news2douyin')
    for path in ('server/templates/gdelt_events.html','server/static/gdelt-events.js',
                 'server/static/gdelt-events.css','server/static/translation-ui.js',
                 'server/static/translation-ui.css'):
        assert resources.joinpath(path).is_file(),path
    with tempfile.TemporaryDirectory(prefix='n2d-intelligence-wheel-') as temporary:
        folder=Path(temporary)
        app=create_app(db_url='sqlite:///'+str(folder/'app.db'),storage_root=str(folder/'runs'))
        now=utc_now_iso()
        with Session(app.state.engine) as session:
            session.add(OfficialEvent(event_id='123456',event_date=now[:10],last_seen=now,
                actor1_name='DONALD TRUMP',actor2_name='UKRAINE',event_root_code='07',event_code='072',
                feed='events_english',stamp='20261114000000',row_index=1))
            session.commit()
        client=TestClient(app)  # No lifespan: do not launch external/background services.
        try:
            assert client.get('/radar?source=gdelt&event=123456').status_code==200
            assert client.get('/api/gdelt/events/count?period=all').json()['total']==1
            assert len(client.get('/api/gdelt/overview').json()['feeds'])==6
            response=client.post('/api/gdelt/events/123456/select',json={'request_id':'installed-wheel-source-1'})
            assert response.status_code==200,response.text
            key=response.json()['key']
            page=client.get(response.json()['url'])
            assert page.status_code==200 and '/articles/gdelt_event' not in page.text
            with Session(app.state.engine) as session:
                source=session.exec(select(TopicSource).where(TopicSource.workspace_id==key)).one()
                sid,before=source.source_id,source.content_hash
            result=client.post('/api/translations',json={'kind':'topic_source','key':sid})
            assert result.status_code==200 and result.json()['status']=='queued',result.text
            task=app.state.worker.service.claim(30)
            assert task and task.trigger_type=='translation'
            with patch('news2douyin.translations.service.translate',return_value='特朗普与乌克兰的援助行动。'):
                app.state.worker.execute(task)
            done=client.get('/api/translations/'+result.json()['id']).json()
            assert done['status']=='succeeded' and done['source_language']=='unknown'
            with Session(app.state.engine) as session:
                assert session.get(TopicSource,sid).content_hash==before
            watch=client.post('/api/focus',json={'name':'安装包专题','scope':'特朗普','kind':'人物'}).json()
            removed=client.request('DELETE','/api/focus/'+watch['key'],json={'version':watch['version']})
            assert removed.status_code==200,removed.text
            trash=client.get('/api/focus/trash').json()['items'][0]
            restored=client.post('/api/focus/'+watch['key']+'/restore',json={'version':trash['version']})
            assert restored.status_code==200 and restored.json()['enabled'] is False,restored.text
        finally:
            client.close();app.state.engine.dispose()
    print(json.dumps(dict(status='passed',version=version,checks=[
        'packaged_ui_resources','six_feeds','official_radar_deep_link','opt_in_count',
        'source_overview','metadata_selection_handoff','durable_translation_worker',
        'translation_preserves_source_hash','versioned_topic_recycle_and_restore'],
        remote_services_contacted=False,translation_provider='deterministic local stub')))


if __name__=='__main__':main()

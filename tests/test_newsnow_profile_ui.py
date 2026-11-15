"""NewsNow configuration and source diagnostics stay in existing UI routes."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.collect.diagnostics import summary, task_diagnostics
from news2douyin.collect.profiles import normalize_profile_dict,load_profile_file
from news2douyin.server.app import create_app
from news2douyin.server.schemas.common import ProfilePayload
from news2douyin.storage.models import Article,TaskCheckpoint,TaskRecord
from news2douyin.storage.articles import record_version


@pytest.fixture
def ui(tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_ENV_FILE',str(tmp_path/'absent.env'))
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL','')
    monkeypatch.setenv('NEWS2DOUYIN_GKG_STREAM_ENABLED','0')
    monkeypatch.setenv('NEWS2DOUYIN_GKG_PROJECTION_ENABLED','0')
    app=create_app(db_url='sqlite:///'+str(tmp_path/'ui.db'),storage_root=str(tmp_path/'runs'))
    yield TestClient(app),app
    app.state.engine.dispose()


def profile():
    return {'name':'中文热点','provider':'newsnow','extra':{'base_url':'http://192.168.0.234:4444',
        'newsnow_sources':['zhihu','cls','custom-source']}}


def test_newsnow_profile_defaults_are_unfiltered_and_do_not_invent_country_language(ui,monkeypatch):
    from news2douyin.collect.providers import newsnow
    monkeypatch.setattr(newsnow.requests,'get',lambda *a,**k:pytest.fail('Saving a profile must not access upstream'))
    client,_=ui
    response=client.post('/api/profiles',json=profile())
    assert response.status_code==200,response.text
    saved=response.json()
    assert saved['provider']=='newsnow' and saved['max_items']==1200
    assert saved['country']==saved['language']==''
    assert saved['categories']==saved['keywords_include']==saved['keywords_exclude']==[]
    assert saved['extra']['filter_mode']=='rules' and saved['extra']['collection_mode']=='top'
    assert saved['extra']['newsnow_sources']==['zhihu','cls','custom-source']
    assert saved['extra']['base_url']=='http://192.168.0.234:4444'
    again=client.get('/api/profiles').json()[0]
    assert again['extra']==saved['extra']
    setup=client.get('/api/setup/check')
    assert setup.status_code==200 and setup.json()['profiles'][0]['state']=='configured'
    assert '试跑' in setup.json()['profiles'][0]['note']


def test_newsnow_schema_preserves_other_source_limits_and_validates_address(ui):
    client,_=ui
    assert client.post('/api/profiles',json=dict(profile(),max_items=10000)).status_code==200
    assert client.post('/api/profiles',json={'name':'too-big','provider':'mock','max_items':1001}).status_code==422
    assert client.post('/api/profiles',json={'name':'default-base','provider':'newsnow','extra':{}}).status_code==200
    for base in ('javascript:alert(1)','http://user:password@host:4444','http://host/?token=secret'):
        payload=profile();payload['extra']['base_url']=base
        assert client.post('/api/profiles',json=payload).status_code==422
    payload=profile();payload['extra']['collection_mode']='search'
    assert client.post('/api/profiles',json=payload).status_code==422


def test_profile_page_has_simple_newsnow_address_and_source_controls(ui):
    client,_=ui
    html=client.get('/profiles').text
    assert 'value="newsnow"' in html
    assert 'id="newsnow-base-url"' in html and 'id="newsnow-source-list"' in html
    assert '缺少发布时间保持未知' in html
    assert '不自动进行财经AI筛选' in html
    assert '日期仅用于标识本次运行' in html
    assert 'data-newsnow-report' not in html


def test_source_diagnostics_separate_partial_failure_empty_and_all_failure():
    sources=[{'source_id':'zhihu','status':'success'},{'source_id':'fastbull','status':'empty'},
             {'source_id':'broken','status':'failed'},{'source_id':'later','status':'not_attempted'}]
    stats={'fetched':2,'newsnow':{'sources':sources},'diagnostics':{'fetched':2,'after_hard_filter':2,'kept':2},'after_filter':2,'dry_run':True}
    text=summary(stats)
    assert '1个来源失败' in text and '1个未尝试' in text and '没有写入新闻库' in text
    assert '失败' in summary({'newsnow':{'sources':[sources[2],sources[3]]}},'failed')
    assert '均成功返回空榜' in summary({'newsnow':{'sources':[sources[1]]}})


def test_small_checkpoint_diagnostics_do_not_need_raw_archive(ui):
    client,app=ui
    with Session(app.state.engine) as session:
        task=TaskRecord(task_id='checkpoint-newsnow',profile_name='热点',trigger_type='profile_test',request_hash='fixture',
            profile_json=json.dumps({'provider':'newsnow','base_url':'http://host:4444','newsnow_sources':['zhihu']}))
        session.add(task)
        session.add(TaskCheckpoint(checkpoint_key='checkpoint-newsnow:filtered',task_id=task.task_id,
            payload_json=json.dumps({'schema':'newsnow_stage_v1','path':'missing-not-opened','sha256':'abc',
                'mode':'rules','kept_count':7,'diagnostics':{'fetched':7}})))
        session.commit()
        report=task_diagnostics(session,task.task_id)
        assert report['stats']['after_filter']==7
        assert report['profile']['base_url']=='http://host:4444'


def test_newsnow_example_profile_preserves_all_sources():
    from pathlib import Path
    raw=load_profile_file(Path(__file__).resolve().parents[1]/'configs/v7/profiles/newsnow_cn.yaml')
    assert raw['provider']=='newsnow' and raw['filter_mode']=='rules'
    assert raw['max_items']==1200 and len(raw['newsnow_sources'])==35
    assert normalize_profile_dict({'name':'n','provider':'newsnow'})['max_items']==1200


def test_newsnow_article_keeps_snapshot_time_separate_from_unknown_or_date_only_publication(ui):
    client,app=ui
    with Session(app.state.engine) as session:
        for key,published,precision in [('unknown','', 'unknown'),('day','2026-11-04T16:00:00Z','date')]:
            row=Article(article_key='newsnow-'+key,provider='newsnow',title='已保存标题',content='这只是来源摘要。',
                url='https://source.example/'+key,source_domain='source.example',published_at=published,
                raw_json=json.dumps({'newsnow':{'source_id':'zhihu','source_name':'知乎',
                    'snapshot_reported_at':'2026-11-14T06:00:00Z','observed_at':'2026-11-14T06:05:00Z',
                    'observed_list_position':4,'publication_precision':precision}}))
            session.add(row);session.flush();record_version(session,row)
        session.commit()
    html=client.get('/articles/newsnow-unknown').text
    assert 'NewsNow · 标题／摘要线索，未取得原文' in html
    assert '来源摘要' in html and '不是已取得的新闻全文' in html
    assert '2026-11-14 14:00' in html and '2026-11-14 14:05' in html
    assert '本版本收录时列表位置 4' in html and '不是当前排名' in html
    assert '已保存正文' not in html and '<h2>保存的原文</h2>' not in html
    day=client.get('/articles/newsnow-day').text
    assert '2026-11-05（仅日期）' in day
    assert '2026-11-05 00:00' not in day
    listing=client.get('/articles').text
    assert 'NewsNow · 标题／摘要线索' in listing and '尚未取得新闻全文' in listing

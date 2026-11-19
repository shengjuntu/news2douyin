from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func
from sqlmodel import Session, select

from news2douyin.digest.models import DailyDigestIssue
from news2douyin.llm.settings import LLMSettings
from news2douyin.server.app import create_app
from news2douyin.storage.models import TaskRecord
from news2douyin.tasks.worker import TaskWorker
from news2douyin.translations import service
from news2douyin.translations.daily import daily_card_descriptor
from test_postgres_integration import postgres_url


@pytest.fixture(params=['sqlite','postgres'])
def daily_app(request,tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL','')
    url = request.getfixturevalue('postgres_url') if request.param=='postgres' else f'sqlite:///{tmp_path}/daily.db'
    app = create_app(db_url=url, storage_root=str(tmp_path/'runs'))
    day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    with Session(app.state.engine) as session:
        session.add(DailyDigestIssue(day=day,status='ready',items_json='[]'))
        session.commit()
    yield app, TestClient(app), day
    app.state.engine.dispose()


def save_cards(engine, day, *cards):
    with Session(engine) as s:
        row=s.get(DailyDigestIssue,day)
        row.items_json=json.dumps(cards,ensure_ascii=False)
        s.add(row)
        s.commit()


def payload(day,card):
    desc=daily_card_descriptor(day,card)
    return {name:desc[name] for name in ('key','fingerprint')}


def count_tasks(engine):
    with Session(engine) as session:
        return session.exec(select(func.count()).select_from(TaskRecord).where(TaskRecord.trigger_type=='translation')).one()


def execute(app):
    worker=TaskWorker(app.state.engine,str(app.state.storage_root))
    task=worker.service.claim()
    assert task is not None
    worker.execute(task)


def test_saved_card_changes_and_translation_is_immutable(daily_app,monkeypatch):
    app,client,day=daily_app
    endpoint=f'/api/radar/daily/{day}/translate'
    card=dict(key='progress_test',source_kind='gkg', title='Le port rouvre',excerpt='Le trafic reprend.',
              source_url='https://paper.example/a',action_evidence={'url':'https://other.example/a','quote':'Wrong quote'})
    save_cards(app.state.engine,day,card)
    unchanged=payload(day,card)
    calls=[]
    def translate(original,target):
        calls.append(original)
        return '港口重开\n\n交通恢复。'
    monkeypatch.setattr(service,'translate',translate)
    assert client.get(endpoint).status_code == 405
    assert client.post(endpoint,json=dict(unchanged,source_text='injected')).status_code==422
    assert client.post(endpoint,json=unchanged,headers={'Origin':'https://other.example'}).status_code==403
    assert count_tasks(app.state.engine)==0
    with ThreadPoolExecutor(max_workers=3) as pool:
        replies=list(pool.map(lambda _:client.post(endpoint,json=unchanged),range(3)))
    assert {r.status_code for r in replies} == {200}
    results=[r.json() for r in replies]
    assert len({d['task_id'] for d in results})==1 and count_tasks(app.state.engine)==1
    prior=results[0]
    assert prior['scope']=='document' and prior['original']=='Le port rouvre\n\nLe trafic reprend.' and not calls
    current=dict(card,excerpt='La cérémonie est reportée.')
    save_cards(app.state.engine,day,current)
    assert client.post(endpoint,json=unchanged).status_code==409
    execute(app)
    old=client.get('/api/translations/'+prior['id']).json()
    assert old['translated_text']=='港口重开\n\n交通恢复。' and calls==[prior['original']]
    new=client.post(endpoint,json=payload(day,current)).json()
    assert new['id']!=old['id'] and new['original'].endswith(current['excerpt'])
    save_cards(app.state.engine,day,card)
    monkeypatch.setattr(service,'get_settings',lambda:LLMSettings('','',''))
    cached=client.post(endpoint,json=unchanged)
    assert cached.status_code==200 and cached.json()['id']==old['id'] and cached.json()['status']=='succeeded'
    with Session(app.state.engine) as session:
        assert json.loads(session.get(DailyDigestIssue,day).items_json)==[card]


def test_chinese_title_only_matching_quote_and_explicit_config_failure(daily_app,monkeypatch):
    app,client,day=daily_app
    endpoint=f'/api/radar/daily/{day}/translate'
    zh=dict(key='zh1',title='港口重新开放',language='zh-CN')
    legacy=dict(key='old',title='Cargo lanes reopen')
    recovered=dict(key='recovered',source_kind='gdelt',title='Port update',excerpt='Saved document context',
                   source_url='https://example.test/report',reconstruction={'language':'en'},
                   action_evidence={'url':'https://example.test/report','quote':'The port authority reopened the terminal.'})
    save_cards(app.state.engine,day,zh,legacy,recovered)
    assert client.post(endpoint,json=payload(day,zh)).json()['status']=='already_chinese'
    assert count_tasks(app.state.engine)==0
    with monkeypatch.context() as m:
        m.setattr(service,'get_settings',lambda:LLMSettings('','',''))
        assert client.post(endpoint,json=payload(day,legacy)).status_code==503
        assert count_tasks(app.state.engine)==0
    title_only=client.post(endpoint,json=payload(day,legacy)).json()
    assert title_only['scope']=='title' and title_only['original']==legacy['title']
    result=client.post(endpoint,json=payload(day,recovered)).json()
    assert result['original']==recovered['title']+'\n\n'+recovered['action_evidence']['quote']
    assert not daily_card_descriptor(day,dict(title=''))
    assert daily_card_descriptor(day,dict(zh,language='zh-TW'))['already_chinese'] is False
    assert daily_card_descriptor(day,dict(zh,language=''))['likely_chinese'] is True
    assert daily_card_descriptor(day,dict(zh,excerpt='The port reopens.'))['already_chinese'] is False


def test_failed_job_is_retryable_and_does_not_replace_frozen_input(daily_app,monkeypatch):
    app,client,day=daily_app
    card=dict(key='failed',title='Le port a rouvert')
    save_cards(app.state.engine,day,card)
    params=payload(day,card); endpoint=f'/api/radar/daily/{day}/translate'
    def fail(*args):
        raise ValueError('翻译模型暂不可用')
    monkeypatch.setattr(service,'translate',fail)
    first=client.post(endpoint,json=params).json()
    execute(app)
    failed=client.get('/api/translations/'+first['id']).json()
    assert failed['status']=='failed' and '翻译模型暂不可用' in failed['error']
    retry=client.post(endpoint,json=dict(params,retry=True)).json()
    assert retry['task_id']!=failed['task_id'] and retry['id']==failed['id']
    monkeypatch.setattr(service,'translate',lambda original,target:'港口重新开放')
    execute(app)
    final=client.get('/api/translations/'+first['id']).json()
    assert final['status']=='succeeded' and final['original']==card['title']


def test_daily_card_widget_and_script_on_published_page(daily_app):
    app,client,day=daily_app
    card=dict(key='widget',title='A foreign title',source_url='https://press.example/story')
    save_cards(app.state.engine,day,card)
    page=client.get('/radar',params={'day':day})
    assert page.status_code==200, page.text
    assert 'data-daily-translation' in page.text and '译成中文' in page.text
    assert payload(day,card)['fingerprint'] in page.text and '/research-assets/daily-translation.js' in page.text
    assert client.get('/research-assets/daily-translation.js').status_code==200
    assert count_tasks(app.state.engine)==0

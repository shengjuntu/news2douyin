"""Short writing contracts: real app/DB/files, fixture model/search/image bytes."""
import copy
import json
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlmodel import Session, select

from test_research import research, rpc, value, make_claim
from test_daily_tasks import chosen, run as run_daily
from news2douyin.editorial import daily, short_scripts as short
from news2douyin.editorial.daily_tasks import DailyTaskRequest
from news2douyin.editorial import daily_tasks
from news2douyin.editorial.generation import GenerationOptions, model_draft
from news2douyin.research import service as svc
from news2douyin.research import sources
from news2douyin.research.models import ResearchRun, ResearchSubmission, ResearchSource, ShortScriptBrief
from news2douyin.storage.models import ScriptPackage, Article
from news2douyin.video import workbench as wb


def prepare(research, **kw):
    app, client, fake = research
    assert client.post('/api/research/setup').status_code == 200
    payload = dict(article_keys=['seed'], idempotency_key='one-short', **kw)
    response = client.post('/api/short-scripts', json=payload)
    assert response.status_code == 202, response.text
    case = client.get('/api/research/cases/' + response.json()['case_id']).json()
    run = case['runs'][0]
    app.state.research.dispatch(run['run_id'])
    return case, run, payload


def draft(research, case, run, **kw):
    cid = make_claim(research, case, run)
    return dict(run_id=run['run_id'], title='政策变化', segments=[dict(kind='fact',
        text='此前的政策于2023年推出。', claim_ids=[cid], visual='政策时间线', assets='公告配图')],
        gaps=['影响仍待核实'], **kw)


@pytest.mark.parametrize('language,expected', [('zh-CN','简体中文'), ('en','English'), ('ja','日本語')])
def test_daily_language_is_an_output_contract(language, expected, monkeypatch):
    import requests
    reply = dict(title='中文初稿', script_text='旧政策于2023年推出。', visual_notes='来源卡', notes='待核对')
    post = Mock(return_value=Mock(json=lambda:{'choices':[{'message':{'content':json.dumps(reply)}}]}))
    monkeypatch.setattr(requests, 'post', post)
    pack = dict(event_title='English story', sources=[dict(article_key='seed', excerpt='The policy started in 2023.')])
    output = daily.draft_document(pack, 'llm', output_language=language, duration_sec=90)
    assert output['output_language'] == language and output['target_duration_sec'] == 90
    messages = post.call_args.kwargs['json']['messages']
    assert expected in messages[0]['content'] and '90 秒' in messages[0]['content']
    assert 'The policy started in 2023.' in messages[1]['content']
    assert pack['sources'][0]['excerpt'] == 'The policy started in 2023.'


def test_extract_and_outline_never_claim_translation():
    assert DailyTaskRequest().output_language == 'source'
    assert DailyTaskRequest(mode='llm').output_language == 'zh-CN'
    assert GenerationOptions().output_language == 'source'
    assert GenerationOptions(backend='llm').output_language == 'zh-CN'
    for cls in [DailyTaskRequest, GenerationOptions]:
        with pytest.raises(ValueError): cls(output_language='en')


def test_preparation_freezes_language_budgets_and_one_submission(research):
    app, client, fake = research
    case, run, payload = prepare(research, output_language='en', duration_sec=90, find_images=False)
    for _ in range(2):
        again = client.post('/api/short-scripts', json=payload)
        assert again.json()['case_id'] == case['case_id']
    assert case['strategy']['max_searches'] == 3 and case['strategy']['max_reads'] == 6
    assert case['short_script']['output_language'] == 'en'
    assert len(fake.sessions) == 1
    with Session(app.state.engine) as s:
        text = json.loads(s.get(ResearchSubmission, run['run_id']).turn_payload)['text']
        assert 'English' in text and '不寻找图片' in text and 'research_save_short_script' in text
        assert len(s.exec(select(ShortScriptBrief)).all()) == 1
    changed = client.post('/api/short-scripts', json={**payload, 'output_language':'ja'})
    assert changed.status_code == 400
    for url in ['/short-scripts', '/short-scripts/' + case['case_id'], '/scripts', '/daily']:
        assert client.get(url).status_code == 200


def test_multi_source_short_draft_and_source_snapshots_survive_edits(research):
    app, client, fake = research
    with Session(app.state.engine) as s:
        s.add(Article(article_key='second', title='Second report', content='The new policy began in 2026.', url='https://second.example/news'))
        s.commit()
    assert client.post('/api/research/setup').status_code == 200
    response = client.post('/api/short-scripts', json=dict(article_keys=['seed','second'], idempotency_key='multi'))
    case = client.get('/api/research/cases/' + response.json()['case_id']).json();run = case['runs'][0]
    app.state.research.dispatch(run['run_id'])
    args = draft(research, case, run)
    second = next(s for s in case['sources'] if s['provenance']['article_key'] == 'second')
    cid = value(rpc(research,'research_save_claim',dict(run_id=run['run_id'], text='New policy in 2026',kind='attributed',
        evidence=[dict(source_id=second['source_id'],paragraph_id='P1',quote='began in 2026')])) )['claim_id']
    args['segments'].append(dict(kind='fact',text='另一份报道表示，新政策于2026年开始。',claim_ids=[cid]))
    result = value(rpc(research, 'research_save_short_script', args))
    assert value(rpc(research, 'research_save_short_script', args)) == result
    with Session(app.state.engine) as s:
        assert len(s.exec(select(ScriptPackage)).all()) == 1
        article=s.exec(select(Article).where(Article.article_key=='seed')).one();article.content='UPDATED';s.add(article);s.commit()
    script = client.get('/api/scripts/' + result['package_key']).json()
    assert script['document']['output_language'] == 'zh-CN' and len(script['document']['segments']) == 2
    assert len(script['sources']) == 2 and all(x['paragraphs'] for x in script['sources'])
    assert 'introduced in 2023' in json.dumps(script) and 'UPDATED' not in json.dumps(script)
    assert script['status'] == 'draft' and not script['editorial']['evidence']['moments'][0]['reviewed']
    assert client.get(result['script_url']).status_code == 200
    assert rpc(research, 'research_save_short_script', {**args,'title':'cannot overwrite'})['isError']
    assert client.post('/api/short-scripts/' + case['case_id'] + '/start').status_code == 400


def test_light_budget_stops_new_search_but_accepts_final_draft(research):
    app, client, fake = research;case, run, _ = prepare(research)
    args = draft(research, case, run)
    for i in range(3):
        ticket=value(rpc(research,'research_search',dict(run_id=run['run_id'],query='topic '+str(i))))
        assert ticket['needs_external_call'] and not ticket['server_side_search_performed']
    assert rpc(research,'research_search',dict(run_id=run['run_id'],query='fourth'))['isError']
    with Session(app.state.engine) as s:
        row=s.get(ResearchRun,run['run_id']);row.started_at=(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat();s.add(row);s.commit()
    assert rpc(research,'research_read_source',dict(run_id=run['run_id'],article_key='seed'))['isError']
    assert value(rpc(research,'research_save_short_script',args))['package_key']


def test_evidence_scope_and_fact_analysis_enforced(research):
    app, client, _ = research;case, run, _=prepare(research)
    args=draft(research,case,run)
    bad=copy.deepcopy(args);bad['segments'][0]['claim_ids']=['unknown']
    assert rpc(research,'research_save_short_script',bad)['isError']
    cid=value(rpc(research,'research_save_claim',dict(run_id=run['run_id'],text='Possibly a change in approach',kind='analysis',
        evidence=[dict(source_id=case['sources'][0]['source_id'],paragraph_id='P1',quote='introduced in 2023')])) )['claim_id']
    args['segments'][0]['claim_ids']=[cid]
    assert rpc(research,'research_save_short_script',args)['isError']
    args['segments'][0]['kind']='analysis';result=value(rpc(research,'research_save_short_script',args))
    script=client.get('/api/scripts/'+result['package_key']).json();doc=script['document'];doc['segments'][0]['kind']='fact'
    doc['script_text']=wb.segment_narration(doc['segments'])
    assert client.put('/api/scripts/'+result['package_key'],json=dict(expected_version=1,document=doc)).status_code==422


def image_source(research, run, monkeypatch):
    html=b'<html><title>Official announcement</title><meta property="og:image" content="/photo.png"><article><p>The policy was announced in 2026.</p><img src="data:image/png,invalid"></article></html>'
    monkeypatch.setattr(sources,'safe_fetch',lambda url:('https://agency.example/announcement','text/html',html))
    return value(rpc(research,'research_read_source',dict(run_id=run['run_id'],url='https://agency.example/announcement')))


def test_real_page_image_discovery_cache_preview_and_export(research,monkeypatch):
    from PIL import Image
    app,client,_=research;case,run,_=prepare(research)
    source=image_source(research,run,monkeypatch)
    candidates=source['provenance']['image_candidates'];assert len(candidates)==1
    assert candidates[0]['url']=='https://agency.example/photo.png'
    img=BytesIO();Image.new('RGB',(300,200),'blue').save(img,format='PNG');data=img.getvalue()
    fetch=Mock(return_value=('https://cdn.example/photo.png','image/png',data));monkeypatch.setattr(sources,'safe_fetch',fetch)
    args=draft(research,case,run,images=[dict(source_id=source['source_id'],url=candidates[0]['url'],caption='公告配图')])
    result=value(rpc(research,'research_save_short_script',args));script=client.get('/api/scripts/'+result['package_key']).json()
    item=script['editorial']['image_candidates'][0];assert item['status']=='downloaded' and item['rights_status']=='unknown'
    url=f'/api/scripts/{result["package_key"]}/images/{item["filename"]}'
    assert client.get(url).content==data and client.get(result['script_url']).status_code==200
    value(rpc(research,'research_save_short_script',args));assert fetch.call_count==1
    with Session(app.state.engine) as s:
        reviewing=wb.review_script(s,result['package_key'],expected_version=1,action='submit')
        approved=wb.review_script(s,result['package_key'],expected_version=reviewing['version'],action='approve',checks={'sources_checked':True,'wording_checked':True})
        exported=wb.export_script(s,result['package_key'],expected_version=approved['version'])
    zipped=zipfile.ZipFile(BytesIO(client.get(exported['download_url']).content))
    assert zipped.read('images/'+item['filename'])==data
    assert json.loads(zipped.read('assets_manifest.json'))['images'][0]['rights_status']=='unknown'
    assert json.loads(zipped.read('manifest.json'))['output_language']=='zh-CN'


def test_failed_image_is_a_gap_not_a_failed_script(research,monkeypatch):
    app,client,_=research;case,run,_=prepare(research)
    source=image_source(research,run,monkeypatch)
    monkeypatch.setattr(sources,'safe_fetch',Mock(side_effect=ValueError('unavailable')))
    args=draft(research,case,run,images=[dict(source_id=source['source_id'],url='https://agency.example/photo.png')])
    result=value(rpc(research,'research_save_short_script',args))
    item=client.get('/api/scripts/'+result['package_key']).json()['editorial']['image_candidates'][0]
    assert item['status']=='unavailable' and 'filename' not in item


def test_image_must_be_observed_and_cancellation_fences_late_save(research,monkeypatch):
    app,client,_=research;case,run,_=prepare(research)
    args=draft(research,case,run,images=[dict(source_id=case['sources'][0]['source_id'],url='https://made-up.example/a.jpg')])
    fetched=Mock();monkeypatch.setattr(short,'fetch_images',fetched)
    assert rpc(research,'research_save_short_script',args)['isError'];fetched.assert_not_called()
    args['images']=[]
    def stop(*args):
        with Session(app.state.engine) as s:
            row=s.get(ResearchRun,run['run_id']);row.status='paused';s.add(row);s.commit()
        return []
    monkeypatch.setattr(short,'fetch_images',stop)
    assert rpc(research,'research_save_short_script',args)['isError']
    with Session(app.state.engine) as s: assert not s.exec(select(ScriptPackage)).all()


def test_image_fetch_uses_existing_public_address_guard(tmp_path,monkeypatch):
    monkeypatch.setattr('socket.getaddrinfo',lambda *a,**kw:[(2,1,6,'',('127.0.0.1',80))])
    result=short.fetch_images([dict(url='http://internal.example/p.png',rights_status='unknown')],tmp_path)
    assert result[0]['status']=='unavailable' and not (tmp_path/'short-script-images').exists()


def test_english_analysis_roundtrip_keeps_language(research):
    app,client,_=research;case,run,_=prepare(research,output_language='en')
    args=draft(research,case,run);args['segments'][0].update(text='The previous policy dates to 2023.',kind='analysis')
    result=value(rpc(research,'research_save_short_script',args))
    script=client.get('/api/scripts/'+result['package_key']).json()
    assert script['document']['script_text'].startswith('Analysis: ')
    from news2douyin.video.templates import scene_plan
    assert scene_plan(script, {'image_ids': []})[0]['narration'] == script['document']['script_text']
    saved=client.put('/api/scripts/'+result['package_key'],json=dict(expected_version=1,document=script['document']))
    assert saved.status_code==200 and saved.json()['document']['output_language']=='en'


def test_changed_language_creates_new_draft_and_preserves_previous(engine, chosen, tmp_path, monkeypatch):
    import requests
    post=Mock(return_value=Mock(json=lambda:{'choices':[{'message':{'content':json.dumps(dict(title='Draft',script_text='A short report.'))}}]}))
    monkeypatch.setattr(requests,'post',post)
    request=dict(selection_keys=[chosen[0]], mode='llm', output_language='zh-CN', duration_sec=60)
    first=daily_tasks.submit_many(engine,request)['items'][0];assert run_daily(engine,tmp_path)['status']=='succeeded'
    first_key=daily_tasks.detail(engine,first['task']['task_id'])['package_key']
    changed=daily_tasks.submit_many(engine,{**request,'output_language':'en'})['items'][0]
    assert changed['task']['task_id']!=first['task']['task_id']
    assert run_daily(engine,tmp_path)['status']=='succeeded'
    with Session(engine) as s:
        assert wb.script_detail(s,first_key)['document']['output_language']=='zh-CN'
        assert len(s.exec(select(ScriptPackage)).all())==2
    reused=daily_tasks.submit_many(engine,{**request,'output_language':'en'})['items'][0]
    assert reused['reused'] and reused['package_key']


def test_concurrent_short_save_and_file_failure_do_not_overwrite(research,monkeypatch):
    app,client,_=research;case,run,_=prepare(research,find_images=False)
    args=draft(research,case,run)
    original=wb.initialize_script
    def fail(*a,**kw):
        original(*a,**kw)
        raise OSError('fixture disk error')
    monkeypatch.setattr(wb,'initialize_script',fail)
    assert rpc(research,'research_save_short_script',args)['isError']
    with Session(app.state.engine) as s: assert not s.exec(select(ScriptPackage)).all()
    assert not list(app.state.research_settings.path.parent.parent.rglob('script.txt'))
    monkeypatch.setattr(wb,'initialize_script',original)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:short.save_draft(app.state.engine,app.state.research_settings,args),range(2)))
    assert results[0]==results[1]
    with Session(app.state.engine) as s: assert len(s.exec(select(ScriptPackage)).all())==1

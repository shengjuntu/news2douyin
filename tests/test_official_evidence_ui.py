"""Published and online cards keep a reconstructed lead distinct through research."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, select

from news2douyin.digest.models import DailyDigestIssue
from news2douyin.digest.service import last_closed_day
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.research import service as research
from news2douyin.research.models import ResearchCase, ResearchSource
from news2douyin.selection.ngram_models import NgramTarget
from news2douyin.server.app import create_app
from news2douyin.topics import service as topics
from news2douyin.topics.models import TopicWorkspace
from sql_readonly_audit import is_read_only_execution
from test_event_metadata_handoff import RecordingRuntime

URL = 'https://port.example/licence'
EXCERPT = 'Port Aurora met with Council Linden to discuss a new licence.'
EVENT = '43001'


def seed(app):
    day = last_closed_day().isoformat()
    stamp = day.replace('-', '')+'081500'
    observed = day+'T08:16:00Z'
    ref = dict(first_feed='mentions_english',first_stamp=stamp,first_row_index=1,
               last_feed='mentions_english',last_stamp=stamp,last_row_index=1)
    with Session(app.state.engine) as s:
        s.add(OfficialEvent(event_id=EVENT, actor1_name='Port Aurora',actor2_name='Council Linden',
            event_code='040',event_base_code='040',event_root_code='04',action_place='Tallinn',event_date=day,
            feed='events_english', stamp=stamp,row_index=1,first_seen=observed,last_seen=observed,ingested_at=observed))
        s.add(OfficialMention(key='known-document',event_id=EVENT,doc_identifier=URL,domain='port.example',
            first_seen=observed,last_seen=observed,updated_at=observed,**ref))
        s.add(NgramTarget(key='known-document',event_id=EVENT,url=URL, state='ready',
            reference_feed='mentions_english',reference_stamp=stamp,reference_row=1,minute=stamp,minutes_left=0,
            requested_at=observed,observed_at=observed, language='en',excerpt=EXCERPT,
            archive_sha256='a'*64,archives_url='https://data.gdeltproject.org/example.gz',
            method='gdeltnews',anchors_json=json.dumps(['Port Aurora','Council Linden'])))
        s.commit()
    return day


@pytest.fixture
def online(tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL','')
    app=create_app(db_url='sqlite:///'+str(tmp_path/'test.db'),storage_root=str(tmp_path/'runs'))
    day=seed(app)
    with Session(app.state.engine) as s:
        from news2douyin.selection.ngram_enrichment import event_status
        item=event_status(s,EVENT)['items'][0]
        card=dict(key='watch-1',title=EXCERPT,excerpt=EXCERPT,source_kind='gdelt',source_key='gdelt:'+EVENT,
                  source_url=URL,observed_at=item['observed_at'],country='',sources=[],reasons=[],
                  reconstruction=item,official_coverage=dict(documents=1,domains=1,new_documents=1,new_domains=1,history_complete=True))
        s.add(DailyDigestIssue(day=day,status='ready',items_json=json.dumps([card]),stats_json='{}'));s.commit()
    client=TestClient(app)
    try: yield app,client,day
    finally: client.close();app.state.engine.dispose()


def test_reads_are_local_and_expose_role_lineage_growth_and_labeled_material(online):
    app,c,day=online
    writes=[]
    def audit(_,__,statement,___,context,____):
        if not is_read_only_execution(statement,context):writes.append(statement)
    event.listen(app.state.engine,'before_cursor_execute',audit)
    try:
        prefix='/api/gdelt/events/'+EVENT
        raw=c.get(prefix+'/evidence',params={'day':day}).json()
        assert raw['actor1']['name']=='Port Aurora' and raw['actor2']['name']=='Council Linden'
        assert raw['coverage']['new_documents']==1 and raw['documents'][0]['url']==URL
        assert c.get(prefix+'/ngrams').json()['items'][0]['provenance']=='gdelt_web_ngrams'
        assert c.get(prefix+'/graph').json()['state']=='not_requested'
        assert '片段重建 · 待核对原文' in c.get('/radar',params={'day':day}).text
        page=c.get('/radar',params={'source':'gdelt','period':'all'}).text
        assert EXCERPT in page.split('<section class="gdelt-event-list"',1)[1].split('</section>',1)[0]
        assert c.get('/research-assets/gdelt-evidence.js').is_success
        assert not writes
    finally:event.remove(app.state.engine,'before_cursor_execute',audit)
    for endpoint in ['/graph','/ngrams']:
        assert c.post(prefix+endpoint,json={},headers={'Origin':'https://foreign.example'}).status_code==403
    assert c.post(prefix+'/graph',json={}).status_code==409


@pytest.mark.parametrize('origin',['official','daily'])
def test_frozen_reconstruction_survives_handoff_and_cannot_back_a_fact(online,origin):
    app,c,day=online
    if origin=='official':
        created=c.post('/api/gdelt/events/'+EVENT+'/select',json=dict(request_id='lead-official-1'))
    else:
        created=c.post('/api/workbench',json=dict(request_id='lead-daily-1',title='核查会面',
                                                   digest_day=day,progress='watch-1'))
    assert created.is_success,created.text
    key=created.json()['key']
    topic=c.get('/api/topics/'+key).json()
    lead=next(src for src in topic['sources'] if src['material_kind']=='gdelt_ngram_reconstruction')
    assert lead['material_label']=='GDELT 片段重建，待核对出版方原文' and not lead['has_readable_text']
    frozen=c.get('/api/topics/'+key+'/sources/'+lead['source_id']).json()
    assert frozen['content']==EXCERPT
    assert frozen['reconstruction']['archives'][0]['sha256']=='a'*64
    with Session(app.state.engine) as s: revision=s.get(TopicWorkspace,key).revision
    runtime=RecordingRuntime()
    with pytest.raises(ValueError,match='先补材或研究'):
        topics.handoff(app.state.engine,runtime,key,dict(revision=revision,request_id='source-only-1',action='short'))
    started=topics.handoff(app.state.engine,runtime,key,dict(revision=revision,request_id='deep-study-1',action='deep'))
    with Session(app.state.engine) as s:
        case=s.get(ResearchCase,started['case_id'])
        ref=s.exec(select(ResearchSource).where(ResearchSource.case_id==case.case_id,
                                                ResearchSource.kind=='search_snippet')).one()
        assert json.loads(ref.provenance_json)['reconstruction']['url']==URL
        para=json.loads(ref.paragraphs_json)[0]
        with pytest.raises(ValueError):
            research.save_claim(s,case,SimpleNamespace(run_id='test'),dict(text='Tested claim',kind='fact',evidence=[
                dict(source_id=ref.source_id,paragraph_id=para['id'],quote=EXCERPT,relation='supports')]))

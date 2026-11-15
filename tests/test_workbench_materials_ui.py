"""Content work starts from honest material states and usable source links."""
import json
from sqlmodel import Session

from news2douyin.intake.official_models import OfficialEvent
from news2douyin.topics import service as topics
from news2douyin.topics.models import TopicSource
from news2douyin.workbench import service as workbench
from test_event_metadata_handoff import selected_metadata
from test_content_foundation import article, report_fixture
from test_research import research


def test_official_source_is_a_snapshot_with_a_real_radar_return(research):
    app, client, _ = research
    key, _, _ = selected_metadata(app)
    with Session(app.state.engine) as session:
        session.add(OfficialEvent(event_id='90001',event_date='2026-01-01',feed='events_english',stamp='20260101000000',row_index=2))
        session.commit()
    snapshot=client.get('/api/topics/'+key+'/sources/topic-source-official-90001').json()
    assert snapshot['detail_url']=='/radar?source=gdelt&event=90001'
    assert snapshot['has_readable_text'] is False
    assert snapshot['language']=='unknown'
    assert client.get(snapshot['detail_url']).status_code==200
    page=client.get('/workbench/'+key+'/selection')
    assert page.status_code==200
    assert '/articles/gdelt_event' not in page.text
    assert '事件线索快照，尚无原文' in page.text
    assert 'data-work-source="topic-source-official-90001"' in page.text
    assert 'id="topic-short" class="ghost" disabled' in page.text
    assert 'id="work-source-translate"' in page.text
    assert 'id="topic-source-translate"' in page.text
    data=client.get('/api/workbench/'+key).json()
    assert data['materials']['stage']=='leads'
    assert data['materials']['can_write_from_sources'] is False


def test_research_and_creation_offer_actionable_original_reading(research):
    app, client, _ = research
    key, _, _ = selected_metadata(app)
    research_page=client.get('/workbench/'+key+'/research')
    assert research_page.status_code==200
    assert 'data-work-handoff="materials"' in research_page.text
    assert '补充原文与背景' in research_page.text
    assert 'data-work-handoff="deep"' in research_page.text
    creation_page=client.get('/workbench/'+key+'/creation')
    assert creation_page.status_code==200
    assert '当前只有事件线索' in creation_page.text
    assert 'data-work-handoff="materials"' in creation_page.text
    # Reading a page is not a research submission.
    assert client.get('/api/topics/'+key).json()['tasks']==[]


def test_readable_materials_and_reports_have_distinct_next_steps(research):
    app, client, _ = research
    article(app,'readable','A source report with a readable body.','Read a report')
    task=workbench.start(app.state.engine,dict(request_id='materials-ui-readable',articles=['readable']))
    assert task['materials']['stage']=='readable'
    assert task['materials']['readable']==1
    assert task['materials']['can_write_from_sources'] is True
    key=task['key']
    report_fixture(app,key)
    data=client.get('/api/workbench/'+key).json()
    assert data['materials']['stage']=='report'
    assert data['materials']['research_readable']==1
    creation=client.get('/workbench/'+key+'/creation')
    assert 'id="work-from-report"' in creation.text
    assert '回到研究阶段，选择报告' not in creation.text


def test_source_links_reject_untrusted_metadata_and_preserve_snapshot(research):
    app, client, _ = research
    key, _, document=selected_metadata(app)
    with Session(app.state.engine) as session:
        source=session.get(TopicSource,'topic-source-official-90001')
        document.update(url='javascript:alert(1)',official_event_id='bad/id',source_ref={'feed':'../bad','stamp':'20260101000000','row_index':True})
        source.document_json=json.dumps(document);session.add(source);session.commit()
    snapshot=client.get('/api/topics/'+key+'/sources/topic-source-official-90001').json()
    assert snapshot['url']==snapshot['detail_url']==snapshot['archive_url']==''
    assert 'GDELT 自动编码元数据' in snapshot['content']
    assert snapshot['source_kind']=='event_metadata'

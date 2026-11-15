"""Official event metadata stays a research lead until original text is read."""
import json
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from news2douyin.research import service as research_service
from news2douyin.research.models import ResearchCase, ResearchSource
from news2douyin.storage.models import Article, Event
from news2douyin.topics import service as topics
from news2douyin.topics.models import TopicSource, TopicWorkspace
from news2douyin.workbench import service as workbench
from test_research import research


class RecordingRuntime:
    def __init__(self):
        self.calls = []

    def enqueue(self, case_id, **kwargs):
        self.calls.append((case_id, kwargs))


def selected_metadata(app):
    engine = app.state.engine
    task = workbench.start(engine, dict(request_id='select-official-event-90001',
        title='军事援助线索', angle='核查实际交付与承诺的区别'))
    document = dict(title='GDELT 事件线索 90001', content='GDELT 自动编码元数据，仅作线索，尚未核对原文。\n事件类型：072。',
        url='https://example.com/original', published_at='2026-01-01T00:00:00Z', provider='gdelt_events',
        source_kind='event_metadata', revision=None, official_event_id='90001',
        source_ref=dict(feed='events_english', stamp='20260101000000', row_index=2, sha256='immutable-archive'))
    with Session(engine) as session:
        source = TopicSource(source_id='topic-source-official-90001', workspace_id=task['key'],
            article_key='gdelt_event/90001', document_json=json.dumps(document, ensure_ascii=False),
            content_hash=research_service.digest(document))
        session.add(source)
        workspace = session.get(TopicWorkspace, task['key'])
        workspace.selected_json = json.dumps([source.source_id])
        workspace.revision += 1
        session.add(workspace)
        session.commit()
        return task['key'], workspace.revision, document


@pytest.mark.parametrize('action,write_script', [('deep', True), ('materials', False), ('materials', True)])
def test_selected_metadata_can_start_research_without_creating_articles_or_events(research, action, write_script):
    app, client, _ = research
    key, revision, document = selected_metadata(app)
    with Session(app.state.engine) as session:
        before_articles = len(session.exec(select(Article)).all())
        before_events = len(session.exec(select(Event)).all())
    runtime = RecordingRuntime()
    result = topics.handoff(app.state.engine, runtime, key,
        dict(revision=revision, request_id='handoff-metadata-'+action, action=action, write_script=write_script))
    assert len(runtime.calls) == 1 and runtime.calls[0][0] == result['case_id']
    with Session(app.state.engine) as session:
        source = session.exec(select(ResearchSource).where(ResearchSource.case_id == result['case_id'])).one()
        assert source.kind == 'event_metadata'
        provenance = json.loads(source.provenance_json)
        assert provenance['official_event_id'] == '90001'
        assert provenance['source_ref'] == document['source_ref']
        assert provenance['requires_original_text'] is True
        case = session.get(ResearchCase, result['case_id'])
        assert '不是已核验新闻正文' in case.goal
        assert len(session.exec(select(Article)).all()) == before_articles
        assert len(session.exec(select(Event)).all()) == before_events
    response = client.get('/api/topics/'+key)
    assert response.status_code == 200
    assert response.json()['sources'][0]['source_kind'] == 'event_metadata'
    source_data = client.get('/api/topics/'+key+'/sources/topic-source-official-90001').json()
    assert source_data['source_kind'] == 'event_metadata' and '仅作线索' in source_data['content']


def test_metadata_alone_cannot_be_used_for_source_only_draft(research):
    app, _, _ = research
    key, revision, _ = selected_metadata(app)
    runtime = RecordingRuntime()
    with pytest.raises(ValueError, match='先补材或研究'):
        topics.handoff(app.state.engine, runtime, key,
            dict(revision=revision, request_id='unsafe-short-metadata', action='short'))
    assert runtime.calls == []


def test_metadata_claim_is_rejected_but_read_original_can_support_claim(research):
    app, _, _ = research
    key, revision, _ = selected_metadata(app)
    result = topics.handoff(app.state.engine, RecordingRuntime(), key,
        dict(revision=revision, request_id='metadata-claim-research', action='deep'))
    with Session(app.state.engine) as session:
        case = session.get(ResearchCase, result['case_id'])
        source = session.exec(select(ResearchSource).where(ResearchSource.case_id == case.case_id)).one()
        paragraph = json.loads(source.paragraphs_json)[0]
        claim = dict(text='甲国已经向乙国提供军事援助。', kind='fact', evidence=[dict(source_id=source.source_id,
            paragraph_id=paragraph['id'], quote=paragraph['text'], relation='supports')])
        with pytest.raises(ValueError, match='事件元数据'):
            research_service.save_claim(session, case, SimpleNamespace(run_id='run-meta'), claim)
        original = research_service.add_source(session, case, title='Actual source report',
            url='https://example.com/read-original', text='The aid delivery was confirmed by the ministry.',
            kind='web_page', provenance={'read_by':'fixture'})
        paragraph = json.loads(original.paragraphs_json)[0]
        claim['evidence'] = [dict(source_id=original.source_id, paragraph_id=paragraph['id'],
            quote=paragraph['text'], relation='supports')]
        assert research_service.save_claim(session, case, SimpleNamespace(run_id='run-meta'), claim)['claim_id']


def test_direct_case_creation_also_blocks_metadata_only_source_only_short(research):
    app, _, _ = research
    _, _, doc = selected_metadata(app)
    with pytest.raises(ValueError, match='先补材或研究'):
        research_service.create_case(app.state.engine,
            dict(title='Metadata short', goal='Generate from selected source', article_keys=['gdelt_event/90001']),
            frozen_sources=[dict(doc, article_key='gdelt_event/90001')],
            preparation=dict(request_key='unsafe-prep', request_hash='fixed', options=dict(source_only=True)),
            fixed_case_id='unsafe-metadata-short')

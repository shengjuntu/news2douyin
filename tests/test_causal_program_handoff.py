"""Completed program tree -> frozen, evidence-only RunDesk analysis."""
import json

import pytest
from sqlalchemy import event as sql_event
from sqlmodel import Session, select

from news2douyin.causal import agent_tools
from news2douyin.causal.models import (CausalAgentNote, CausalAgentQuery, CausalNode,
                                     CausalProgram, CausalProgramRetrieval, CausalProgramStep)
from news2douyin.research import causal_dispatch, sources
from news2douyin.research.models import (ResearchRun, ResearchSource, ResearchSubmission, ResearchToolCall)
from news2douyin.research.rundesk import CAUSAL_ANALYSIS_TOOLS, validate_runtime
from news2douyin.research.tools import ResearchTools
from news2douyin.storage.utils import dumps, loads
from test_causal_rundesk_dispatch import causal_case, saved
from test_research import research


QUOTE = 'The 2014 agreement established a joint commission to monitor implementation.'


@pytest.fixture
def program_case(causal_case):
    app, client, fake, case, key = causal_case
    with Session(app.state.engine) as session:
        root = session.exec(select(CausalNode).where(CausalNode.case_key == key, CausalNode.depth == 0)).one()
        child = CausalNode(key='cn_program_child', case_key=key, parent_key=root.key,
            ref='evt_program_prior', text='A prior agreement established a joint commission.',
            occurred_at='2014-01-01', published_at='2014-01-02', time_status='earlier', depth=1,
            sources_json=dumps([dict(url='https://source.example/agreement', title='Earlier publisher article',
                quote=QUOTE, published_at='2014-01-02', kind='publisher_history')]))
        headline = CausalNode(key='cn_program_headline', case_key=key, parent_key=root.key,
            ref='evt_metadata', text='Unverified headline lead', depth=1,
            sources_json=dumps([dict(url='https://source.example/headline', title='Headline only',
                quote='Headline only', published_at='2014-01-03', kind='local_headline')]))
        session.add_all([CausalProgram(case_key=key, date_from='1991-01-01'), child, headline,
            CausalAgentNote(node_key=child.key, case_key=key, relation='context'),
            CausalProgramStep(node_key=root.key, case_key=key,
                queries_json=dumps(['Earlier agreements and compliance mechanism']), candidate_count=7,
                stop_reason='程序已选取本轮可用线索'),
            CausalProgramRetrieval(node_key=root.key, report_json=dumps({'history': {'status': 'verified'}})),
            CausalProgramStep(node_key=child.key, case_key=key, queries_json=dumps(['joint commission history']),
                candidate_count=0, stop_reason='没有找到可核对的新材料')])
        session.commit()
    return app, client, fake, case, key


def begin(program_case, *, dispatch=True):
    app, _, fake, case, _ = program_case
    fake.available = set(CAUSAL_ANALYSIS_TOOLS)
    run = app.state.research.enqueue(case['case_id'])
    if dispatch:
        app.state.research.dispatch(run['run_id'])
        assert saved(app, run['run_id'])[0].status == 'running'
    return run['run_id'], ResearchTools(app.state.engine, app.state.research_settings)


def test_program_prompt_and_registration_are_frozen_without_fake_query_receipts(program_case):
    app, _, _, _, key = program_case
    with Session(app.state.engine) as session:
        before = [node.model_dump() for node in session.exec(select(CausalNode).where(CausalNode.case_key == key).order_by(CausalNode.key))]
    run_id, _ = begin(program_case, dispatch=False)
    with Session(app.state.engine) as session:
        submission = session.get(ResearchSubmission, run_id)
        payload = loads(submission.turn_payload, {})
        assert set(payload) == {'text', 'files', 'skills'}
        assert payload['text'].startswith(causal_dispatch.PROGRAM_ANALYSIS_MARKER)
        for fragment in ('仅解读', '不能继续搜索', '程序已经负责', '停止理由', 'source_id', 'partial', '反证'):
            assert fragment in payload['text']
        assert 'research_search(scope=web) 申请预算票据' not in payload['text']
        excerpts = list(session.exec(select(ResearchSource).where(ResearchSource.kind == 'causal_excerpt')))
        assert len(excerpts) == 1
        source = excerpts[0]
        provenance = loads(source.provenance_json, {})
        assert provenance['causal_case_key'] == key and provenance['run_id'] == run_id
        assert provenance['registration'] == 'existing_program_tree_excerpt'
        assert provenance['source_kind'] == 'publisher_history'
        assert 'search_id' not in provenance
        assert loads(source.paragraphs_json, []) == [dict(id='P1', text=QUOTE)]
        assert not list(session.exec(select(CausalAgentQuery)))
        after = [node.model_dump() for node in session.exec(select(CausalNode).where(CausalNode.case_key == key).order_by(CausalNode.key))]
        assert after == before


@pytest.mark.parametrize('name,arguments', [
    ('research_search', dict(query='new search', scope='web')),
    ('research_search', dict(query='new search', scope='library')),
    ('research_record_search', dict(ticket='forged', results=[])),
    ('research_read_source', dict(url='https://new.example/document')),
    ('research_read_source', dict(article_key='seed')),
    ('research_read_source', dict(source_id='anything', url='https://new.example/document')),
    ('research_save_short_script', dict(title='Unexpected new task', segments=[])),
    ('topic_get_workspace', {}),
    ('causal_search_events', dict(node_key='cn_program_child', query='more history', date_from='1991-01-01', date_to='2013-01-01')),
    ('causal_save_step', dict(node_key='cn_program_child', stop_reason='change tree')),
])
def test_analysis_gate_blocks_retrieval_and_tree_mutation_before_side_effects(program_case, monkeypatch, name, arguments):
    app, _, _, _, _ = program_case
    run_id, tools = begin(program_case)
    def forbidden(*_args, **_kwargs):
        pytest.fail('Analysis-only run attempted further retrieval')
    monkeypatch.setattr(agent_tools, 'retrieve', forbidden)
    monkeypatch.setattr(sources, 'read_document', forbidden)
    with pytest.raises(ValueError, match='本轮'):
        tools.call(name, dict(run_id=run_id, **arguments))
    with Session(app.state.engine) as session:
        run = session.get(ResearchRun, run_id)
        assert run.search_count == run.read_count == 0
        assert not list(session.exec(select(CausalAgentQuery)))
        assert not list(session.exec(select(ResearchToolCall)))


@pytest.mark.parametrize('name,arguments', [
    ('causal_search_events', dict(node_key='cn_program_child', query='more history', date_from='1991-01-01', date_to='2013-01-01')),
    ('causal_save_step', dict(node_key='cn_program_child', stop_reason='change tree')),
])
def test_direct_causal_dispatch_cannot_bypass_the_same_run_gate(program_case, name, arguments):
    app, _, _, _, _ = program_case
    run_id, _ = begin(program_case)
    with pytest.raises(ValueError, match='本轮仅解读'):
        agent_tools.call(app.state.engine, name, dict(run_id=run_id, **arguments))


def test_tree_read_is_read_only_and_its_existing_evidence_can_be_claimed_and_reported(program_case):
    app, _, _, _, _ = program_case
    run_id, tools = begin(program_case)
    statements = []
    def record(_connection, _cursor, statement, *_args):
        statements.append(statement.lower().lstrip())
    sql_event.listen(app.state.engine, 'before_cursor_execute', record)
    try:
        tree = tools.call('causal_get_investigation', dict(run_id=run_id))
        again = tools.call('causal_get_investigation', dict(run_id=run_id))
        assert tree == again and statements
        assert all(statement.startswith('select') for statement in statements)
    finally:
        sql_event.remove(app.state.engine, 'before_cursor_execute', record)
    assert tree['analysis_only'] and tree['limits']['searches'] == 0
    assert not any(node['expandable'] for node in tree['nodes'])
    child = next(node for node in tree['nodes'] if node['key'] == 'cn_program_child')
    assert child['queries'] == ['joint commission history'] and child['candidate_count'] == 0
    assert child['stop_reason'] == '没有找到可核对的新材料' and child['relation'] == 'context'
    assert child['evidence'][0]['quote'] == QUOTE
    assert next(node for node in tree['nodes'] if not node['depth'])['retrieval']['history']['status'] == 'verified'
    headline = next(node for node in tree['nodes'] if node['key'] == 'cn_program_headline')
    assert headline['evidence'] == [] and '前置时间待核' in headline['stop_reason']
    context = tools.call('research_get_case', dict(run_id=run_id))
    assert context['analysis_only'] and context['search_policy']['web_action'] == 'disabled'
    citation = child['evidence'][0]
    page = tools.call('research_read_source', dict(run_id=run_id, source_id=citation['source_id']))
    assert page['paragraphs'] == [dict(id='P1', text=QUOTE)]
    claim = tools.call('research_save_claim', dict(run_id=run_id, text='The earlier agreement established a commission.',
        kind='attributed', evidence=[citation]))
    result = tools.call('research_save_report', dict(run_id=run_id, title='Evidence-only interpretation', completeness='partial',
        gaps=['The tree provides context but does not establish causation.'], sections=[dict(heading='Prior context',
            body='The saved excerpt refers to a monitoring commission.', claim_ids=[claim['claim_id']])]))
    assert result['version'] == 1


def test_later_program_registration_does_not_reclassify_a_saved_agent_run(causal_case):
    app, _, _, case, key = causal_case
    run = app.state.research.enqueue(case['case_id'])
    with Session(app.state.engine) as session:
        submission = session.get(ResearchSubmission, run['run_id'])
        original = submission.turn_payload
        session.add(CausalProgram(case_key=key, date_from='1991-01-01'))
        session.commit()
    app.state.research.dispatch(run['run_id'])
    with Session(app.state.engine) as session:
        assert not causal_dispatch.analysis_only(session, run['run_id'])
        assert session.get(ResearchSubmission, run['run_id']).turn_payload == original
    result = agent_tools.call(app.state.engine, 'causal_get_investigation', dict(run_id=run['run_id']))
    assert not result.get('analysis_only') and result['limits']['searches'] > 0


def test_program_mode_remains_gated_if_program_configuration_changes(program_case):
    app, _, _, _, key = program_case
    run_id, tools = begin(program_case)
    with Session(app.state.engine) as session:
        session.delete(session.get(CausalProgram, key))
        session.commit()
    with pytest.raises(ValueError, match='本轮仅解读'):
        tools.call('research_search', dict(run_id=run_id, query='do not retrieve', scope='web'))


@pytest.mark.parametrize('docker', [False, True])
def test_only_analysis_tools_are_required_for_actual_selected_runtime(program_case, monkeypatch, docker):
    app, _, fake, case, _ = program_case
    fake.available = set(CAUSAL_ANALYSIS_TOOLS)
    if docker:
        monkeypatch.setenv('NEWS2DOUYIN_CAUSAL_RUN_PRESET', 'analysis-history')
        monkeypatch.setenv('NEWS2DOUYIN_CAUSAL_RUN_INSTANCE_ID', 'instance-causal')
        monkeypatch.setenv('NEWS2DOUYIN_CAUSAL_RUN_WORKSPACE_ID', 'workspace-causal')
    run = app.state.research.enqueue(case['case_id'])
    app.state.research.dispatch(run['run_id'])
    assert saved(app, run['run_id'])[0].status == 'running'
    assert not any(path.endswith('/mcp/exa') for _, path, _, _ in fake.calls)
    assert any(path.endswith('/turns') for _, path, _, _ in fake.calls)


def test_analysis_docker_admission_does_not_weaken_older_causal_role():
    config = dict(instance_id='i', workspace_id='w', search_provider='rundesk', search_backend='exa')
    runtime = dict(instanceId='i', workspaceId='w', executionMode='docker',
        environment=dict(id='env', state='running'), probed=True, accountReady=True, errors={},
        servers={'news2douyin-research': sorted(CAUSAL_ANALYSIS_TOOLS)})
    assert validate_runtime(runtime, config, causal=True, analysis_only=True)['id'] == 'env'
    with pytest.raises(ValueError, match='因果'):
        validate_runtime(runtime, config, causal=True)


def test_program_handoff_registers_only_the_cited_version_and_quote(program_case):
    app, _, _, _, _ = program_case
    cited = 'The negotiators signed the earlier agreement.'
    with Session(app.state.engine) as session:
        child = session.get(CausalNode, 'cn_program_child')
        older, = loads(child.sources_json, [])
        child.published_at = '2014-01-15'
        child.sources_json = dumps([older, dict(older, url='https://source.example/second',
            quote='Unrelated introduction. '+cited, cited_quote=cited, modified_at=child.published_at)])
        session.commit()
    run_id, tools = begin(program_case)
    tree = tools.call('causal_get_investigation', dict(run_id=run_id))
    child = next(node for node in tree['nodes'] if node['key'] == 'cn_program_child')
    registered, = child['evidence']
    assert registered['quote'] == cited
    with Session(app.state.engine) as session:
        source = session.get(ResearchSource, registered['source_id'])
        assert source.url == 'https://source.example/second'
        assert source.published_at == '2014-01-15'
        assert loads(source.provenance_json, {})['first_published_at'] == '2014-01-02'


def test_handoff_already_registered_by_older_version_reuses_existing_paragraph(program_case):
    app, _, _, _, _ = program_case
    run_id, tools = begin(program_case)
    cited = 'established a joint commission'
    with Session(app.state.engine) as session:
        child = session.get(CausalNode, 'cn_program_child')
        child.reason = '候选解释，因果待核。\n来源摘录：“'+cited+'”'
        session.commit()
    tree = tools.call('causal_get_investigation', dict(run_id=run_id))
    child = next(node for node in tree['nodes'] if node['key'] == 'cn_program_child')
    evidence, = child['evidence']
    assert evidence['quote'] == cited
    source = tools.call('research_read_source', dict(run_id=run_id, source_id=evidence['source_id']))
    assert source['paragraphs'] == [dict(id='P1', text=QUOTE)]

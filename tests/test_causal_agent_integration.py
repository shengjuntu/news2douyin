"""Business API -> RunDesk adapter -> research MCP -> saved investigation.

All news and model/search responses are controlled fixtures. TestClient advances
the durable queues explicitly; no deployed service, model or source is used.
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event
from sqlmodel import Session, select

from news2douyin.causal import agent_tools, model
from news2douyin.causal.models import CausalAgentLink, CausalJob
from news2douyin.causal.runtime import CausalWorker
from news2douyin.desk.models import Watch
from news2douyin.research.models import ResearchCase, ResearchReport, ResearchRun, ResearchSubmission
from news2douyin.research.rundesk import RemoteError
from news2douyin.server.app import create_app
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article
from sql_readonly_audit import is_read_only_execution
from test_postgres_integration import postgres_url
from test_research import FakeRunDesk


BASE = '/api/causal/investigations'
EARLIEST = '1991-01-01'
TARGET_AT = '2022-02-24T12:00:00Z'
TARGET = '合成测试：某地区2022年冲突升级，追查其历史前因。'
SOURCE_TEXT = 'On 2014-09-05, the agreement established a negotiation framework.\nImplementation remained disputed.'


class AgentRunDesk(FakeRunDesk):
    """Application auth can read its own session but not session configuration."""

    def _request(self, method, path, payload=None, params=None):
        if path.startswith('/sessions/') and path.endswith('/configuration'):
            self.calls.append((method, path, payload, params))
            raise RemoteError('Application credentials cannot inspect session configuration',
                              status=403, code='forbidden')
        result = super()._request(method, path, payload, params)
        if path.endswith('/configuration'):
            for server in result['mcp']['status']['data']:
                if server['name'] == 'news2douyin-research':
                    server['tools'].update({name: {} for name in agent_tools.NAMES})
        return result


@pytest.fixture(params=['sqlite', 'postgres'])
def agent_case(request, tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE', 'UTC')
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    monkeypatch.setenv('NEWS2DOUYIN_ENV_FILE', str(tmp_path/'absent.env'))
    for name in ('NEWS2DOUYIN_CAUSAL_RUN_PRESET', 'NEWS2DOUYIN_CAUSAL_RUN_INSTANCE_ID',
                 'NEWS2DOUYIN_CAUSAL_RUN_WORKSPACE_ID'):
        monkeypatch.delenv(name, raising=False)
    url = (request.getfixturevalue('postgres_url') if request.param == 'postgres'
           else 'sqlite:///'+str(tmp_path/'causal-agent.db'))
    app = create_app(db_url=url, storage_root=str(tmp_path/'runs'))
    config = app.state.research_settings.get()
    config.update(runtime_mode='local', search_provider='rundesk', search_backend='exa')
    app.state.research_settings.write(config)
    fake = AgentRunDesk()
    app.state.research.client_factory = lambda _config: fake

    def forbidden_model(*_args, **_kwargs):
        raise AssertionError('Agent investigation invoked the independent local attribution model')

    monkeypatch.setattr(model, 'configure', forbidden_model)
    monkeypatch.setattr(model, 'propose', forbidden_model)
    monkeypatch.setattr(model, 'rank', forbidden_model)
    from news2douyin.server import causal as routes
    monkeypatch.setattr(routes, 'configure', forbidden_model)
    with Session(app.state.engine) as session:
        source = Article(article_key='qa-causal-original', title='Synthetic primary record',
            content=SOURCE_TEXT, url='https://qa.example/primary-record',
            published_at='2014-09-05T10:00:00Z')
        session.add(source); session.flush()
        record_version(session, source, origin='test_fixture')
        session.add(Watch(key='qa-causal-focus', name='合成历史调查焦点', kind='topic', scope='地区冲突'))
        session.commit()
    client = TestClient(app)  # Lifespan intentionally off: no live worker/model.
    try:
        response = client.post('/api/research/setup')
        assert response.status_code == 200, response.text
        yield app, client, fake
    finally:
        client.close(); app.state.engine.dispose()


def create_agent(client, **changes):
    payload = dict(text=TARGET, event_at=TARGET_AT, runner='agent', date_from=EARLIEST)
    payload.update(changes)
    response = client.post(BASE, json=payload)
    assert response.status_code == 202, response.text
    assert response.headers['cache-control'] == 'no-store'
    return response.json()


def detail(client, case):
    response = client.get(BASE+'/'+case['key'])
    assert response.status_code == 200, response.text
    return response.json()


def rpc(app, client, name, **args):
    response = client.post('/mcp/research', headers={
        'Authorization': 'Bearer '+app.state.research_settings.get()['mcp_token'],
    }, json=dict(jsonrpc='2.0', id=17, method='tools/call', params=dict(name=name, arguments=args)))
    assert response.status_code == 200, response.text
    return response.json()['result']


def value(result):
    assert not result['isError'], result
    return json.loads(result['content'][0]['text'])


def dispatch(app, case):
    run_id = case['agent']['run_id']
    app.state.research.dispatch(run_id)
    with Session(app.state.engine) as session:
        row = session.get(ResearchRun, run_id)
        assert row.status == 'running', row.error
    return run_id


def test_agent_creation_queues_one_linked_run_and_never_starts_the_local_worker(agent_case):
    app, client, fake = agent_case
    case = create_agent(client, focus_key='qa-causal-focus')
    agent = case['agent']
    assert agent['status'] == 'queued' and agent['run_id']
    assert agent['date_from'] == EARLIEST
    assert agent['research_path'].startswith('/research/')
    assert case['focus_key'] == 'qa-causal-focus'
    assert not any(path.endswith('/turns') for _method, path, _payload, _params in fake.calls)
    assert CausalWorker(app.state.engine).step() is False
    with Session(app.state.engine) as session:
        link = session.get(CausalAgentLink, case['key'])
        assert link.current_run_id == agent['run_id'] and link.date_from == EARLIEST
        research = session.get(ResearchCase, link.research_case_id)
        assert research.date_from == EARLIEST and research.cutoff
        assert not session.exec(select(CausalJob).where(CausalJob.case_key == case['key'],
            CausalJob.state.in_(('pending', 'running')))).first()
        submission = session.get(ResearchSubmission, agent['run_id'])
        assert EARLIEST in submission.turn_payload


def test_existing_agent_start_is_idempotent_before_and_after_dispatch(agent_case):
    app, client, fake = agent_case
    case = create_agent(client)
    for _ in range(2):
        response = client.post(BASE+'/'+case['key']+'/agent', json={'date_from': EARLIEST})
        assert response.status_code == 202, response.text
        assert response.json()['agent']['run_id'] == case['agent']['run_id']
    dispatch(app, case)
    response = client.post(BASE+'/'+case['key']+'/agent', json={'date_from': EARLIEST})
    assert response.status_code == 202, response.text
    assert response.json()['agent']['run_id'] == case['agent']['run_id']
    assert len(fake.sessions) == 1
    assert sum(path.endswith('/turns') for _method, path, _payload, _params in fake.calls) == 1


def test_agent_read_pages_are_local_and_mcp_and_mutations_require_authorized_origin(agent_case):
    app, client, fake = agent_case
    case = create_agent(client)
    call_count = len(fake.calls)
    statements = []

    def audit(_connection, _cursor, sql, _parameters, context, _many):
        statements.append((sql, is_read_only_execution(sql, context)))

    sql_event.listen(app.state.engine, 'before_cursor_execute', audit)
    try:
        for url in (BASE, BASE+'/'+case['key'], '/radar/causes?investigation='+case['key']):
            response = client.get(url)
            assert response.status_code == 200, response.text
        assert statements and all(read for _sql, read in statements)
    finally:
        sql_event.remove(app.state.engine, 'before_cursor_execute', audit)
    assert len(fake.calls) == call_count
    payload = dict(text=TARGET, runner='agent', date_from=EARLIEST, event_at=TARGET_AT)
    assert client.post(BASE, json=payload, headers={'Origin': 'https://other.example'}).status_code == 403
    assert client.post(BASE+'/'+case['key']+'/agent', json={'date_from': EARLIEST},
        headers={'Origin': 'https://other.example'}).status_code == 403
    call = dict(jsonrpc='2.0', id=1, method='tools/call',
        params=dict(name='causal_get_investigation', arguments={'run_id': case['agent']['run_id']}))
    assert client.post('/mcp/research', json=call).status_code == 401
    assert client.post('/mcp/research', json=call,
        headers={'Authorization': 'Bearer wrong-fixture-token'}).status_code == 401
    assert client.post('/mcp/research', json=call, headers={
        'Authorization': 'Bearer '+app.state.research_settings.get()['mcp_token'],
        'Origin': 'https://other.example'}).status_code == 403


@pytest.mark.parametrize('bad_date', ['1991-02-30', '2023-01-01'])
def test_agent_requires_a_valid_explicit_earliest_date_before_the_target(agent_case, bad_date):
    _app, client, _fake = agent_case
    response = client.post(BASE, json=dict(text=TARGET, event_at=TARGET_AT,
        runner='agent', date_from=bad_date))
    assert response.status_code == 422, response.text


def local_candidate(ref, year, *, occurred=None, published=None):
    occurred = occurred or f'{year}-09-05T08:00:00Z'
    published = published or f'{year}-09-05T09:00:00Z'
    return dict(ref=ref, text=f'Synthetic {year} primary record', event_key='',
        occurred_at=occurred, published_at=published, similarity=.8,
        sources=[dict(url=f'https://qa.example/{ref}', title=f'Synthetic {year} record',
            quote=f'On {year}-09-05, a preceding agreement was recorded.',
            published_at=published, kind='local_article')])


def test_agent_dispatch_mcp_hops_saved_evidence_report_and_final_sync(agent_case, monkeypatch):
    app, client, fake = agent_case
    case = create_agent(client)
    run_id = dispatch(app, case)
    calls = []

    def retrieve(engine, queries, *, lower, upper, exclude):
        assert engine.pool.checkedout() == 0, 'Local retrieval held a research SQL transaction'
        calls.append(dict(query=queries[0], lower=lower, upper=upper, exclude=set(exclude)))
        if len(calls) == 1:
            return [local_candidate('framework-2014', 2014), local_candidate('before-explicit-floor', 1990),
                local_candidate('late-retrospective', 2014, published='2023-01-01T00:00:00Z')]
        return [local_candidate('assurances-1994', 1994)]

    monkeypatch.setattr(agent_tools, 'retrieve', retrieve)
    graph = value(rpc(app, client, 'causal_get_investigation', run_id=run_id))
    assert graph['case_key'] == case['key'] and graph['date_from'] == EARLIEST
    root = next(node for node in graph['nodes'] if not node['parent_key'])
    found = value(rpc(app, client, 'causal_search_events', run_id=run_id,
        node_key=root['key'], query='framework', date_from=EARLIEST, date_to='2022-02-23'))
    assert found['effective_from'].startswith(EARLIEST)
    assert [item['ref'] for item in found['candidates']] == ['framework-2014']
    first_args = dict(run_id=run_id, node_key=root['key'], candidates=[dict(
        search_id=found['search_id'], ref='framework-2014',
        reason='Earlier framework; causal relevance remains to be reviewed.', relation='possible_cause')])
    first = value(rpc(app, client, 'causal_save_step', **first_args))
    assert value(rpc(app, client, 'causal_save_step', **first_args)) == first
    child = first['saved'][0]
    found2 = value(rpc(app, client, 'causal_search_events', run_id=run_id,
        node_key=child['key'], query='assurances', date_from=EARLIEST, date_to='2014-09-05'))
    second = value(rpc(app, client, 'causal_save_step', run_id=run_id, node_key=child['key'],
        candidates=[dict(search_id=found2['search_id'], ref='assurances-1994',
            reason='Earlier diplomatic background, not an established cause.', relation='context')]))
    leaf = second['saved'][0]
    value(rpc(app, client, 'causal_save_step', run_id=run_id, node_key=leaf['key'],
        stop_reason='Stop at the earliest supported primary record in this controlled test.'))
    assert calls[0]['lower'].startswith('1991-01-01'), 'Explicit earliest silently became a 90-day window'
    assert calls[1]['upper'] <= '2014-09-05T08:00:00'
    assert 'framework-2014' in calls[1]['exclude']

    source = value(rpc(app, client, 'research_read_source', run_id=run_id, article_key='qa-causal-original'))
    source_id = source['source_id']
    quote = 'On 2014-09-05, the agreement established a negotiation framework.'
    claim = value(rpc(app, client, 'research_save_claim', run_id=run_id,
        text='The saved record describes a negotiation framework in 2014.', kind='fact',
        occurred_at='2014-09-05', evidence=[dict(source_id=source_id,
            paragraph_id='P1', quote=quote, relation='supports')]))
    report = value(rpc(app, client, 'research_save_report', run_id=run_id,
        title='Synthetic antecedent research', sections=[dict(heading='Earlier framework',
            body='The 2014 framework and 1994 background are clues; chronology alone does not establish causality.',
            claim_ids=[claim['claim_id']])], gaps=['The influence of each antecedent still needs review.'],
        completeness='complete'))
    session_id = next(iter(fake.sessions))
    fake.sessions[session_id]['status'] = 'completed'
    assert app.state.research.sync(run_id)['status'] == 'completed'
    final = detail(client, case)
    assert final['agent']['status'] == 'completed' and not final['working']
    assert {node['depth'] for node in final['nodes']} == {0, 1, 2}
    saved = next(node for node in final['nodes'] if node['key'] == child['key'])
    assert saved['sources'][0]['url'] == 'https://qa.example/framework-2014'
    assert saved['sources'][0]['search_id'] == found['search_id']
    with Session(app.state.engine) as session:
        assert session.get(ResearchReport, report['report_id']).run_id == run_id
    exported = client.get(final['agent']['research_path'])
    assert exported.status_code == 200, exported.text
    assert len(fake.sessions) == 1
    assert not any(path.startswith('/sessions/') and path.endswith('/configuration')
                   for _method, path, _payload, _params in fake.calls)


def test_mcp_rejects_foreign_nodes_sources_and_search_receipts(agent_case, monkeypatch):
    app, client, _fake = agent_case
    one = create_agent(client)
    two = create_agent(client, text='Independent synthetic investigation')
    run_one, run_two = dispatch(app, one), dispatch(app, two)
    root_one, root_two = one['nodes'][0]['key'], two['nodes'][0]['key']
    monkeypatch.setattr(agent_tools, 'retrieve', lambda *_args, **_kwargs: [local_candidate('stored', 2014)])
    assert rpc(app, client, 'causal_search_events', run_id=run_one, node_key=root_two,
        query='wrong node', date_from=EARLIEST, date_to='2022-02-23')['isError']
    found = value(rpc(app, client, 'causal_search_events', run_id=run_one, node_key=root_one,
        query='same case', date_from=EARLIEST, date_to='2022-02-23'))
    assert rpc(app, client, 'causal_save_step', run_id=run_two, node_key=root_two,
        candidates=[dict(search_id=found['search_id'], ref='stored', reason='Wrong run.', relation='context')])['isError']
    source = value(rpc(app, client, 'research_read_source', run_id=run_one, article_key='qa-causal-original'))
    external = dict(source_id=source['source_id'], paragraph_id='P1', quote=SOURCE_TEXT.splitlines()[0],
        text=SOURCE_TEXT.splitlines()[0], occurred_at='2014-09-05', reason='Wrong source scope.', relation='context')
    assert rpc(app, client, 'causal_save_step', run_id=run_two, node_key=root_two, candidates=[external])['isError']
    assert len(detail(client, one)['nodes']) == len(detail(client, two)['nodes']) == 1


def test_external_step_keeps_literal_evidence_and_rejects_invented_text_or_date(agent_case):
    app, client, _fake = agent_case
    case = create_agent(client)
    run_id = dispatch(app, case)
    root = case['nodes'][0]['key']
    source = value(rpc(app, client, 'research_read_source', run_id=run_id, article_key='qa-causal-original'))
    external = dict(source_id=source['source_id'], paragraph_id='P1',
        quote=SOURCE_TEXT.splitlines()[0], text=SOURCE_TEXT.splitlines()[0],
        occurred_at='2014-09-05', reason='Original source records the preceding event.', relation='context')
    for changes in ({'text': 'Invented conclusion absent from the original.'},
                    {'quote': 'An invented quotation.'}, {'occurred_at': '1994-12-05'}):
        assert rpc(app, client, 'causal_save_step', run_id=run_id,
            node_key=root, candidates=[{**external, **changes}])['isError']
    saved = value(rpc(app, client, 'causal_save_step', run_id=run_id, node_key=root, candidates=[external]))
    child = next(node for node in detail(client, case)['nodes'] if node['key'] == saved['saved'][0]['key'])
    assert child['occurred_at'] == '2014-09-05'
    assert child['sources'][0]['source_id'] == source['source_id']
    assert child['sources'][0]['paragraph_id'] == 'P1'
    assert child['sources'][0]['quote'] == external['quote']


def test_retrospective_reference_stays_unknown_and_expansion_is_before_its_quoted_occurrence(agent_case, monkeypatch):
    app, client, _fake = agent_case
    case = create_agent(client)
    run_id = dispatch(app, case)
    source = value(rpc(app, client, 'research_read_source', run_id=run_id, article_key='qa-causal-original'))
    first = dict(source_id=source['source_id'], paragraph_id='P1', quote=SOURCE_TEXT.splitlines()[0],
        text=SOURCE_TEXT.splitlines()[0], occurred_at='2014-09-05', reason='Primary earlier record.', relation='context')
    child = value(rpc(app, client, 'causal_save_step', run_id=run_id,
        node_key=case['nodes'][0]['key'], candidates=[first]))['saved'][0]
    older_text = 'On 1991-08-24, the declaration was adopted.'
    with Session(app.state.engine) as session:
        session.add(Article(article_key='qa-causal-retrospective', title='Synthetic later anniversary text',
            content=older_text, url='https://qa.example/later-anniversary', published_at='2020-08-24T10:00:00Z'))
        session.commit()
    retrospective = value(rpc(app, client, 'research_read_source', run_id=run_id,
        article_key='qa-causal-retrospective'))
    entry = dict(source_id=retrospective['source_id'], paragraph_id='P1', quote=older_text, text=older_text,
        occurred_at='1991-08-24', reason='A later text points to an older document.', relation='possible_cause')
    assert rpc(app, client, 'causal_save_step', run_id=run_id,
        node_key=child['key'], candidates=[entry])['isError']
    entry['relation'] = 'document_reference'
    leaf = value(rpc(app, client, 'causal_save_step', run_id=run_id,
        node_key=child['key'], candidates=[entry]))['saved'][0]
    assert leaf['time_status'] == 'unknown' and leaf['relation'] == 'document_reference'
    bounds = []
    def retrieve(_engine, _queries, *, lower, upper, exclude):
        bounds.append((lower, upper))
        return []
    monkeypatch.setattr(agent_tools, 'retrieve', retrieve)
    historical = value(rpc(app, client, 'causal_get_investigation', run_id=run_id))
    assert next(node for node in historical['nodes'] if node['key'] == leaf['key'])['expandable']
    result = value(rpc(app, client, 'causal_search_events', run_id=run_id, node_key=leaf['key'],
        query='Earlier record', date_from=EARLIEST, date_to='2022-02-23'))
    assert result['candidates'] == []
    assert bounds == [('1991-01-01T00:00:00', '1991-08-24T00:00:00')]
    final = detail(client, case)
    saved = next(node for node in final['nodes'] if node['key'] == leaf['key'])
    assert saved['sources'][0]['published_at'] == '2020-08-24T10:00:00Z'
    assert saved['sources'][0]['retrospective'] is True


def test_stopping_agent_fences_mcp_steps_and_continuation_retains_the_tree(agent_case):
    app, client, _fake = agent_case
    case = create_agent(client)
    run_id = dispatch(app, case)
    root = case['nodes'][0]['key']
    with Session(app.state.engine) as session:
        case_id = session.get(CausalAgentLink, case['key']).research_case_id
    stop = client.post('/api/research/cases/'+case_id+'/stop')
    assert stop.status_code == 200, stop.text
    assert rpc(app, client, 'causal_save_step', run_id=run_id, node_key=root,
        stop_reason='Late callback after stop.')['isError']
    resumed = client.post(BASE+'/'+case['key']+'/agent', json={'date_from': EARLIEST})
    assert resumed.status_code == 202, resumed.text
    newer = resumed.json()
    assert newer['agent']['run_id'] != run_id and newer['agent']['status'] == 'queued'
    assert [node['key'] for node in newer['nodes']] == [root]
    assert rpc(app, client, 'causal_get_investigation', run_id=run_id)['isError']


def test_local_handoff_waits_for_local_job_and_agent_blocks_local_expansion(agent_case, monkeypatch):
    app, client, _fake = agent_case
    from news2douyin.server import causal as routes
    monkeypatch.setattr(routes, 'configure', lambda: object())
    response = client.post(BASE, json=dict(text=TARGET, event_at=TARGET_AT, runner='local'))
    assert response.status_code == 202, response.text
    local = response.json()
    handoff = client.post(BASE+'/'+local['key']+'/agent', json={'date_from': EARLIEST})
    assert handoff.status_code == 409, handoff.text
    with Session(app.state.engine) as session:
        job = session.get(CausalJob, local['nodes'][0]['key'])
        job.state = 'ready'; session.add(job); session.commit()
    handoff = client.post(BASE+'/'+local['key']+'/agent', json={'date_from': EARLIEST})
    assert handoff.status_code == 202, handoff.text
    assert handoff.json()['agent']['status'] == 'queued'
    expansion = client.post(BASE+'/'+local['key']+'/expand', json=dict(node_key=local['nodes'][0]['key']))
    assert expansion.status_code == 409, expansion.text
    assert CausalWorker(app.state.engine).step() is False

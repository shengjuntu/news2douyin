"""Durable program tree: hypotheses -> local recall -> evidence ranking -> jobs.

The attribution and retrieval boundaries are controlled fixtures. No deployed
model, embedding service, source site or RunDesk instance is contacted.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.causal import history, model, retrieval, service, tree_worker
from news2douyin.causal.models import CausalJob, CausalNode, CausalProgramRetrieval, CausalProgramStep
from news2douyin.causal.runtime import CausalWorker
from news2douyin.server.app import create_app
from news2douyin.storage.db import create_schema
from test_postgres_integration import postgres_url
from test_causal_model_isolated import SETTINGS, Response, candidate as model_candidate, completion, ranked, wire


BASE = '/api/causal/investigations'
TARGET = '合成测试：港口运输成本为何增加'
FLOOR = '1991-01-01'


def candidate(ref, *, year=2014, score=.8, occurred=None, published=None, kind='local_article'):
    occurred = f'{year}-03-01T08:00:00Z' if occurred is None else occurred
    published = f'{year}-03-01T09:00:00Z' if published is None else published
    return dict(ref=ref, text='保存材料 '+ref, event_key='', occurred_at=occurred,
        published_at=published, fixture_score=score,
        sources=[dict(url='https://qa.example/'+ref, title='原始资料 '+ref,
                      quote='原始资料记载的先前运输条件 '+ref, published_at=published, kind=kind)],
        retrieval=dict(pool_count=100, mode='lexical_embedding'))


class ProgramBoundaries:
    def __init__(self, engine):
        self.engine = engine
        self.calls, self.results, self.by_target, self.rank_inputs = [], [], {}, []
        self.empty_hypotheses = False
        self.failure = ''

    def free(self, name):
        assert self.engine.pool.checkedout() == 0, name+' held a SQL connection'

    def configure(self):
        self.free('configure')
        return object()

    def hypotheses(self, _settings, target):
        self.free('hypotheses')
        self.calls.append(('hypotheses', target))
        return [] if self.empty_hypotheses else ['待查假说：先前运输条件可能影响 '+target]

    def retrieve(self, engine, queries, *, lower, upper, exclude, event_text):
        self.free('retrieval/embedding')
        assert engine is self.engine and queries[0].startswith('待查假说：')
        self.calls.append(('retrieve', event_text, lower, upper, set(exclude)))
        if self.failure == 'retrieve':
            raise RuntimeError('private-api-key-and-source-body must not reach a saved error')
        rows = deepcopy(self.by_target.get(event_text, self.results))
        dated = sum(bool(row['published_at']) and row['sources'][0]['kind'] != 'local_headline' for row in rows)
        return SimpleNamespace(rows=rows, local=dict(status='covered' if dated else 'unknown',
            pool_count=len(rows), eligible_count=len(rows), dated_evidence_count=dated, vector_ranked_count=0))

    def rank_pool(self, engine, pool, *, historical, checkpoint):
        self.free('retrieval ranking')
        assert engine is self.engine
        return SimpleNamespace(rows=pool.rows + historical, local=pool.local, mode='',
                               history_eligible_count=len(historical), history_ranked_count=len(historical))

    def rank(self, _settings, target, rows):
        self.free('rank')
        self.calls.append(('rank', target))
        self.rank_inputs.append(deepcopy(rows))
        if self.failure == 'rank':
            raise model.CausalModelError('归因模型没有返回有效 JSON。')
        return [dict(ref=row['ref'], reason='候选解释，因果待核：来源记载了先前条件。',
                     score=row.get('fixture_score', .8),
                     **({'relation': row['fixture_relation']} if 'fixture_relation' in row else {}))
                for row in rows]


@pytest.fixture(params=['sqlite', 'postgres'])
def program_case(request, tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE', 'UTC')
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    monkeypatch.setenv('NEWS2DOUYIN_ENV_FILE', str(tmp_path/'absent.env'))
    url = (request.getfixturevalue('postgres_url') if request.param == 'postgres'
           else 'sqlite:///'+str(tmp_path/'program.db'))
    app = create_app(db_url=url, storage_root=str(tmp_path/'runs'))
    fake = ProgramBoundaries(app.state.engine)
    monkeypatch.setattr(model, 'configure', fake.configure)
    monkeypatch.setattr(model, 'propose_hypotheses', fake.hypotheses)
    monkeypatch.setattr(model, 'rank', fake.rank)
    monkeypatch.setattr(retrieval, 'collect_tree_pool', fake.retrieve)
    monkeypatch.setattr(retrieval, 'rank_tree_pool', fake.rank_pool)
    monkeypatch.setattr(history, 'search_history', lambda *_args, **_kwargs:
                        history.HistoryResult([], {'status': 'unconfigured'}))
    from news2douyin.server import causal as routes
    monkeypatch.setattr(routes, 'configure', fake.configure)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Program tree used the manual or RunDesk path')
    monkeypatch.setattr(app.state.research, 'enqueue', forbidden)
    monkeypatch.setattr(model, 'propose', forbidden)
    monkeypatch.setattr(retrieval, 'retrieve', forbidden)
    client = TestClient(app)  # Durable queue advances explicitly, no lifespan.
    try:
        yield app, client, fake
    finally:
        client.close(); app.state.engine.dispose()


def create(client, **changes):
    payload = dict(text=TARGET, event_at='2022-02-24', runner='program', date_from=FLOOR)
    payload.update(changes)
    response = client.post(BASE, json=payload)
    assert response.status_code == 202, response.text
    return response.json()


def detail(client, case):
    response = client.get(BASE+'/'+case['key'])
    assert response.status_code == 200, response.text
    return response.json()


def drain(app, maximum=40):
    for count in range(maximum):
        # A fresh worker proves continuation depends on SQL jobs, not a local
        # recursive stack, process memory or one Agent conversation.
        if not CausalWorker(app.state.engine).step():
            return count
    raise AssertionError('Controlled program queue did not settle')


def test_program_grows_multiple_depths_with_global_beam_and_saved_hypotheses(program_case, monkeypatch):
    app, client, fake = program_case
    monkeypatch.setattr(service, 'MAX_DEPTH', 3)
    first = [candidate('first-'+str(i), score=.9-i*.05) for i in range(5)]
    first += [candidate('headline', score=1., kind='local_headline'),
              candidate('unknown', score=1., occurred='', published='')]
    first[0]['fixture_relation'] = 'possible_cause'
    fake.by_target[TARGET] = first
    for i in range(5):
        fake.by_target['保存材料 first-'+str(i)] = [candidate(f'second-{i}-{j}', year=2001, score=.9-j*.1)
                                               for j in range(4)]
        for j in range(4):
            fake.by_target[f'保存材料 second-{i}-{j}'] = [candidate(f'third-{i}-{j}', year=1994)]
    case = create(client)
    assert case['program']['date_from'] == FLOOR and case['agent'] is None
    assert not fake.calls, 'POST waited for model/retrieval work'
    assert CausalWorker(app.state.engine).step()
    state = detail(client, case)
    assert state['working'] and state['program']['queued'] == 3
    assert drain(app) == 6  # root + 3 depth-one + 3 depth-two work items.
    state = detail(client, case)
    assert not state['working'] and max(node['depth'] for node in state['nodes']) == 3
    for depth in (1, 2):
        assert sum(node['depth'] == depth and node['beam_selected'] for node in state['nodes']) == 3
    assert not any(node['beam_selected'] for node in state['nodes'] if node['text'] in ('保存材料 headline', '保存材料 unknown'))
    phases = [call[0] for call in fake.calls]
    assert phases == ['hypotheses', 'retrieve', 'rank'] * 7
    recalls = [call for call in fake.calls if call[0] == 'retrieve']
    assert all(call[2] == '1991-01-01T00:00:00' for call in recalls)
    assert recalls[0][3] == '2022-02-24T00:00:00'
    assert all(call[3] <= '2014-03-01T08:00:00' for call in recalls[1:])
    with Session(app.state.engine) as session:
        jobs = session.exec(select(CausalJob).where(CausalJob.case_key == case['key'])).all()
        steps = session.exec(select(CausalProgramStep).where(CausalProgramStep.case_key == case['key'])).all()
        assert len(jobs) == len(steps) == 7 and all(job.state == 'ready' for job in jobs)
        assert all(json.loads(step.queries_json)[0].startswith('待查假说：') for step in steps)
        assert all(step.candidate_count == 100 and step.retrieval_mode == 'lexical_embedding' for step in steps)
    first_node = next(node for node in state['nodes'] if node['text'] == '保存材料 first-0')
    assert first_node['relation'] == 'possible_cause'
    assert all(node['relation'] == 'context' for node in state['nodes'] if node['depth'] and node != first_node)


def test_program_strict_parent_date_floor_and_source_filters_precede_model_ranking(program_case):
    app, client, fake = program_case
    fake.results = [candidate('valid'), candidate('before-floor', year=1990),
        candidate('at-parent', occurred='2022-02-24T00:00:00Z'),
        candidate('late-source', published='2022-02-24T00:00:00Z'),
        candidate('unknown', occurred='', published='')]
    mixed = candidate('mixed')
    mixed['sources'].append(dict(mixed['sources'][0], published_at='2023-01-01T00:00:00Z'))
    fake.results.append(mixed)
    fake.results[0]['retrieval']['mode'] = 'lexical'
    for item in fake.results:
        item['retrieval']['mode'] = 'lexical'
    case = create(client)
    claimed = service.claim(app.state.engine)
    step = tree_worker.investigate(app.state.engine, claimed)
    assert {row['ref'] for row in fake.rank_inputs[-1]} == {'valid', 'unknown', 'mixed'}
    assert len(next(row for row in fake.rank_inputs[-1] if row['ref'] == 'mixed')['sources']) == 1
    assert next(row for row in step.results if row['ref'] == 'unknown')['time_status'] == 'unknown'
    assert step.retrieval_mode == 'lexical' and step.candidate_count == 100
    assert all(row['relation'] == 'context' for row in step.results)
    assert detail(client, case)['nodes'][0]['job_state'] == 'running', 'Helper published outside queue lifecycle'


@pytest.mark.parametrize('failure', ['retrieve', 'rank'])
def test_program_failed_step_keeps_hypotheses_and_can_retry_without_duplicate_nodes(program_case, failure):
    app, client, fake = program_case
    fake.results = [candidate('prior')]
    fake.failure = failure
    case = create(client)
    assert CausalWorker(app.state.engine).step()
    data = detail(client, case)
    root = data['nodes'][0]
    assert root['job_state'] == 'failed' and root['stop_reason'] and root['queries']
    assert 'private-api-key-and-source-body' not in json.dumps(data)
    assert len(data['nodes']) == 1
    fake.failure, fake.empty_hypotheses = '', False
    response = client.post(BASE+'/'+case['key']+'/expand', json={'node_key': root['key']})
    assert response.status_code == 202, response.text
    assert CausalWorker(app.state.engine).step()
    data = detail(client, case)
    assert len(data['nodes']) == 2
    assert next(node for node in data['nodes'] if not node['depth'])['job_state'] == 'ready'
    with Session(app.state.engine) as session:
        row = session.get(CausalProgramStep, root['key'])
        assert row.candidate_count == 100 and row.retrieval_mode == 'lexical_embedding'


def test_program_empty_hypotheses_stops_without_repeating_the_target_as_a_query(program_case):
    app, client, fake = program_case
    fake.empty_hypotheses = True
    case = create(client)
    assert drain(app) == 1
    data = detail(client, case)
    assert not data['working'] and data['nodes'][0]['stop_reason']
    assert [call[0] for call in fake.calls] == ['hypotheses']
    assert data['nodes'][0]['queries'] == []


@pytest.mark.parametrize('upstream', ['no_results', 'unverified', 'error'])
def test_program_history_keeps_a_report_even_when_there_are_no_candidates(program_case, monkeypatch, upstream):
    app, client, fake = program_case

    def supplement(queries, lower, upper, *, event_text, checkpoint):
        fake.free('BigQuery')
        assert queries and event_text == TARGET
        assert lower == '1991-01-01T00:00:00' and upper == '2022-02-24T00:00:00'
        checkpoint()
        return history.HistoryResult([], dict(status=upstream, date_from='2021-11-26', date_to='2022-02-23',
                                             lead_count=2 if upstream == 'unverified' else 0))

    monkeypatch.setattr(history, 'search_history', supplement)
    case = create(client)
    assert drain(app) == 1
    root = detail(client, case)['nodes'][0]
    assert root['job_state'] == ('failed' if upstream == 'error' else 'ready')
    assert root['retrieval']['local']['eligible_count'] == 0
    assert root['retrieval']['history']['status'] == upstream
    assert root['retrieval']['history']['date_to'] == '2022-02-23'
    assert root['retrieval']['ranked_count'] == root['retrieval']['selected_count'] == 0


def test_verified_history_requeues_an_existing_root_and_grows_the_same_tree(program_case, monkeypatch):
    app, client, fake = program_case
    case = create(client)
    assert drain(app) == 1
    first = detail(client, case)['nodes'][0]
    assert first['retrieval']['history']['status'] == 'unconfigured'

    def supplement(_queries, lower, upper, *, event_text, checkpoint):
        fake.free('BigQuery')
        assert lower < upper
        checkpoint()
        if event_text != TARGET:
            return history.HistoryResult([], {'status': 'no_results'})
        actual = candidate('gdelt-history:prior', year=2022, occurred='', published='2022-02-21', kind='publisher_history')
        late = candidate('gdelt-history:late', year=2022, occurred='', published='2022-02-25', kind='publisher_history')
        return history.HistoryResult([actual, late], {'status': 'verified', 'verified_count': 2})

    monkeypatch.setattr(history, 'search_history', supplement)
    response = client.post(BASE+'/'+case['key']+'/expand', json={'node_key': first['key']})
    assert response.status_code == 202
    assert drain(app) == 2
    tree = detail(client, case)
    assert tree['key'] == case['key'] and tree['program']['completed'] == 2
    root, child = tree['nodes']
    assert root['key'] == child['parent_key'] == first['key']
    assert root['retrieval']['history']['status'] == 'verified'
    assert root['retrieval']['ranked_count'] == root['retrieval']['selected_count'] == 1
    assert child['occurred_at'] == '' and child['sources'][0]['published_at'] == '2022-02-21'
    assert child['time_status'] == 'earlier' and child['sources'][0]['kind'] == 'publisher_history'


def test_startup_schema_adds_retrieval_diagnostics_to_an_existing_store(program_case):
    app, client, _ = program_case
    case = create(client)
    assert drain(app) == 1
    root = detail(client, case)['nodes'][0]
    CausalProgramRetrieval.__table__.drop(app.state.engine)
    create_schema(app.state.engine)
    preserved = detail(client, case)['nodes'][0]
    assert preserved['key'] == root['key'] and preserved['queries'] == root['queries']
    assert preserved['stop_reason'] == root['stop_reason'] and preserved['retrieval'] == {}


@pytest.mark.parametrize('evidence', ['none', 'unknown', 'headline'])
def test_program_no_confirmed_continuation_records_why_it_stopped(program_case, evidence):
    app, client, fake = program_case
    fake.results = ([] if evidence == 'none' else
                    [candidate('unknown', occurred='', published='')] if evidence == 'unknown' else
                    [candidate('headline', kind='local_headline')])
    case = create(client)
    assert drain(app) == 1
    data = detail(client, case)
    root = next(node for node in data['nodes'] if not node['depth'])
    assert not data['working'] and root['stop_reason']
    assert '历史材料不存在' not in root['stop_reason'] or '不能证明' in root['stop_reason']
    if evidence == 'none':
        assert [call[0] for call in fake.calls] == ['hypotheses', 'retrieve']
    else:
        assert len(data['nodes']) == 2 and not data['nodes'][1]['beam_selected']


def test_program_stale_lease_cannot_publish_nodes_or_overwrite_step_diagnostics(program_case):
    app, client, fake = program_case
    case = create(client)
    stale = service.claim(app.state.engine)
    with Session(app.state.engine) as session:
        row = session.get(CausalJob, stale.node_key)
        row.due_at = 0; session.add(row); session.commit()
    current = service.claim(app.state.engine)
    assert current.lease != stale.lease and current.program
    with pytest.raises(service.CausalLeaseLost):
        service.renew(app.state.engine, stale)
    service.renew(app.state.engine, current)
    service.complete(app.state.engine, current, queries=['new hypothesis'], candidate_count=7,
        retrieval_mode='lexical', stop_reason='No new supported candidates.',
        retrieval_report={'history': {'status': 'no_results'}})
    service.complete(app.state.engine, stale, [{**candidate('obsolete'), 'time_status': 'earlier',
        'reason': 'obsolete', 'score': .8}], queries=['old hypothesis'], candidate_count=100,
        retrieval_mode='lexical_embedding', stop_reason='obsolete',
        retrieval_report={'history': {'status': 'obsolete'}})
    data = detail(client, case)
    assert len(data['nodes']) == 1
    root = data['nodes'][0]
    assert root['queries'] == ['new hypothesis'] and root['candidate_count'] == 7
    assert root['retrieval_mode'] == 'lexical' and root['stop_reason'] == 'No new supported candidates.'
    assert root['retrieval']['history']['status'] == 'no_results'
    assert not fake.calls


def test_hypothesis_protocol_precedes_retrieval_and_stays_a_bounded_unverified_prompt(wire):
    queue, calls, _sessions = wire
    queue.append(Response(completion({'hypotheses': ['可能的港口停运降低供给', 'Possible supply disruption',
        'POSSIBLE SUPPLY DISRUPTION', '可能的需求变化改变运输价格', 'ignored fourth']})))
    result = model.propose_hypotheses(SETTINGS, TARGET)
    assert result == ['可能的港口停运降低供给', 'Possible supply disruption', '可能的需求变化改变运输价格']
    request = json.loads(calls[0][1]['data'])
    assert calls[0][0] == SETTINGS.base_url+'/chat/completions'
    assert request['max_completion_tokens'] == 1600 and request['model'] == SETTINGS.model
    assert '假说只用于检索' in request['messages'][0]['content']
    assert len(calls) == 1


def test_optional_model_relation_is_validated_and_never_a_causal_probability(wire):
    queue, _calls, _sessions = wire
    queue.append(Response(completion({'rankings': [ranked(relation='possible_cause')]})))
    assert model.rank(SETTINGS, TARGET, [model_candidate()])[0]['relation'] == 'possible_cause'
    queue.append(Response(completion({'rankings': [ranked(relation='proven_cause')]})))
    with pytest.raises(model.CausalModelError, match='关系类型'):
        model.rank(SETTINGS, TARGET, [model_candidate()])


@pytest.mark.parametrize('value', [{'queries': ['not a hypothesis']}, {'hypotheses': ['https://invented.example/proof']},
                                  {'hypotheses': [123]}, {'hypotheses': [' '] }])
def test_malformed_hypothesis_output_is_not_retrieval_input(wire, value):
    queue, _calls, _sessions = wire
    queue.append(Response(completion(value)))
    with pytest.raises(model.CausalModelError):
        model.propose_hypotheses(SETTINGS, TARGET)

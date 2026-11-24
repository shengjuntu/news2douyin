"""Round replacement, human expansion, and frozen evidence handoff."""
from datetime import date
from unittest.mock import Mock

from sqlmodel import Session

from news2douyin.causal import agent, service
from news2douyin.causal.models import CausalRetiredVisit
from test_causal_program_worker import BASE, candidate, create, detail, program_case
from test_postgres_integration import postgres_url


def found(ref, score=.7, relation='possible_cause', year=2014):
    item = candidate(ref, year=year)
    return {**item, 'time_status': 'earlier', 'score': score, 'relation': relation, 'reason': ref}


def test_recheck_replaces_full_set_preserves_prior_subtree_and_auto_budget(program_case):
    app, client, _ = program_case
    engine = app.state.engine
    case = create(client)
    api = BASE + '/' + case['key']
    root = service.claim(engine)
    old = [found('old-%d' % i, .99-i/100) for i in range(8)]
    service.complete(engine, root, old, queries=['ancienne'], retrieval_report={'selected_count':8})
    first = service.claim(engine)
    service.complete(engine, first, [found('grandchild', year=2010)])
    while claimed := service.claim(engine):
        service.complete(engine, claimed, [])
    prior = detail(client, case)
    assert len(prior['nodes']) == 10
    assert client.post(api + '/expand', json={'node_key':root.node_key}).status_code == 202
    recheck = service.claim(engine)
    service.complete(engine, recheck, [found('fresh-%d' % i) for i in range(8)],
                     queries=['nouvelle'], retrieval_report={'selected_count':8})
    current = detail(client, case)
    parent, *children = current['nodes']
    assert parent['retrieval']['selected_count'] == len(children) == 8
    assert all('fresh-' in item['text'] and item['expandable'] and not item['job_state'] for item in children)
    assert not current['working'] and service.claim(engine) is None
    archive = '/radar/causes/' + case['key'] + '/revisions/' + str(parent['previous_revision'])
    page = client.get(archive)
    assert page.status_code == 200 and 'ancienne' in page.text and 'grandchild' in page.text
    assert all(row['text'] in page.text for row in old)
    assert client.get(archive.replace(case['key'], 'another-investigation')).status_code == 404
    with Session(engine) as s:
        assert s.get(CausalRetiredVisit, first.node_key).beam_selected
    assert client.post(api + '/expand', json={'node_key':children[0]['key']}).status_code == 202
    assert service.claim(engine).node_key == children[0]['key']


def test_beam_reserves_a_supported_cause_and_failed_retry_keeps_live_nodes(program_case):
    app, client, _ = program_case
    case = create(client)
    engine = app.state.engine
    root = service.claim(engine)
    choices = [('doc', .99, 'document_reference'), ('against', .98, 'contradicts'),
               ('context', .97, 'context'), ('cause', .94, 'possible_cause')]
    service.complete(engine, root, [found(*choice) for choice in choices])
    active = detail(client, case)
    selected = [item['text'] for item in active['nodes'] if item['beam_selected']]
    assert len(selected) == 3 and '保存材料 cause' in selected
    while claimed := service.claim(engine):
        service.complete(engine, claimed)
    api = BASE + '/' + case['key']
    assert client.post(api + '/expand', json={'node_key':root.node_key}).status_code == 202
    service.complete(engine, service.claim(engine), error='service timeout')
    failed = detail(client, case)
    assert failed['nodes'][0]['retrieval']['previous_results_visible']
    assert [r['key'] for r in active['nodes']] == [r['key'] for r in failed['nodes']]
    assert client.post(api + '/expand', json={'node_key':root.node_key}).status_code == 202
    service.complete(engine, service.claim(engine), [], retrieval_report={'selected_count':0})
    assert len(detail(client, case)['nodes']) == 1


def test_waiting_archive_blocks_handoff_even_with_idle_causal_queue(program_case, monkeypatch):
    app, client, _ = program_case
    engine = app.state.engine
    case = create(client)
    root = service.claim(engine)
    day = date(2022,2,20).isoformat()
    service.complete(engine, root, retrieval_report={'history':{'status':'archive_gap','date_from':day,'date_to':day}})
    req = dict(case_key=case['key'],node_key=root.node_key,date_from=day,date_to=day)
    assert client.post('/api/history-archive/request',json=req).status_code == 202
    active = detail(client, case)
    assert active['working'] and active['program']['status'] == 'awaiting_archive'
    monkeypatch.setattr(agent,'check_config',lambda _:None)
    submit = Mock(side_effect=AssertionError('premature handoff'))
    monkeypatch.setattr(agent,'submit',submit)
    api = BASE + '/' + case['key']
    response = client.post(api + '/agent',json={})
    assert response.status_code == 409 and '补档' in response.text
    assert client.post(api + '/expand',json={'node_key':root.node_key}).status_code == 409
    submit.assert_not_called()


def test_expired_owner_cannot_replace_a_successful_round(program_case):
    app, client, _ = program_case
    from news2douyin.causal.models import CausalJob

    engine = app.state.engine
    case = create(client)
    root = service.claim(engine)
    service.complete(engine, root, [found('current')])
    while job := service.claim(engine):
        service.complete(engine, job)
    api = BASE + '/' + case['key']
    assert client.post(api + '/expand', json={'node_key':root.node_key}).status_code == 202
    stale = service.claim(engine)
    with Session(engine) as s:
        job = s.get(CausalJob, root.node_key)
        job.due_at = 0
        s.add(job)
        s.commit()
    service.complete(engine, stale, [found('stale')])
    assert detail(client, case)['nodes'][1]['text'] == '保存材料 current'


def test_global_archive_retry_respects_completed_handoff(program_case):
    app, client, _ = program_case
    from news2douyin.causal.models import CausalAgentLink
    from news2douyin.history_archive.models import HistoryJob

    engine = app.state.engine
    case = create(client)
    root = service.claim(engine)
    service.complete(engine, root)
    key = 'node:' + root.node_key
    day = date(2022,2,21)
    with Session(engine) as s:
        s.add_all([HistoryJob(key=key, case_key=case['key'], node_key=root.node_key,
                              date_from=day,date_to=day,state='failed'),
                   CausalAgentLink(case_key=case['key'],research_case_id='bound',date_from='2020-01-01')])
        s.commit()
    app.state.history_archive.retry()
    with Session(engine) as s:
        assert s.get(HistoryJob,key).state == 'failed'

"""One research search executor, legacy migration and durable deduplication."""
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from sqlmodel import Session

from test_research import research, setup_case, rpc, value
from test_research_docker import docker_case
from news2douyin.research import service as svc
from news2douyin.research.models import ResearchEnvironment, ResearchRun, ResearchToolCall
from news2douyin.research.tools import ResearchTools


def block_direct_search(monkeypatch):
    request = Mock(side_effect=AssertionError('No direct research search'))
    monkeypatch.setattr('requests.get', request)
    monkeypatch.setattr('requests.post', request)
    return request


@pytest.mark.parametrize('provider', ['worldnewsapi', 'tavily'])
def test_legacy_settings_migrate_without_touching_credentials_or_run_history(research, provider):
    app, client, _ = research
    current, run = setup_case(research)
    with Session(app.state.engine) as s:
        env = s.get(ResearchEnvironment, run['run_id']);env.search_provider = provider;s.add(env);s.commit()
    settings = app.state.research_settings
    old = settings.get();old.update(search_provider=provider, search_api_key='legacy-private')
    settings.write(old)
    new = settings.get()
    assert new['search_provider'] == 'rundesk' and new['search_backend'] == 'exa'
    assert new['search_api_key'] == 'legacy-private' and new['mcp_token'] == old['mcp_token']
    public = client.get('/api/research/settings').json()
    assert 'legacy-private' not in json.dumps(public) and public['search_ready'] is None
    detail = client.get('/api/research/cases/' + current['case_id']).json()
    assert detail['search_policy']['restart_required']
    assert detail['runs'][0]['environment']['search_provider'] == provider


@pytest.mark.parametrize('provider', ['worldnewsapi', 'tavily'])
def test_stale_settings_cannot_reenable_second_search_provider(research, provider):
    app, client, _ = research
    result = client.put('/api/research/settings', json={'search_provider': provider})
    assert result.status_code == 400 and 'Codex' in result.json()['detail']
    assert app.state.research_settings.get()['search_provider'] == 'rundesk'


def test_identical_queries_reuse_ticket_and_recorded_results(research, monkeypatch):
    app, _, _ = research
    _, run = setup_case(research)
    network = block_direct_search(monkeypatch)
    args = {'run_id': run['run_id'], 'query': 'Iraq'}
    first = value(rpc(research, 'research_search', args))
    again = value(rpc(research, 'research_search', {**args, 'scope': 'web', 'query': ' Iraq '}))
    assert again == first and first['server_side_search_performed'] is False
    assert first['execution_owner'] == 'codex' and first['server'] == 'exa'
    saved = value(rpc(research, 'research_record_search', {'run_id': run['run_id'], 'ticket': first['ticket'],
                    'results': [{'title': 'Original', 'url': 'https://example.com/original'}]}))
    assert not saved['needs_external_call']
    assert value(rpc(research, 'research_search', args)) == saved
    with Session(app.state.engine) as s: assert s.get(ResearchRun, run['run_id']).search_count == 1
    network.assert_not_called()


def test_existing_exa_ticket_without_scope_survives_upgrade(research):
    app, _, _ = research
    _, run = setup_case(research)
    args = {'run_id': run['run_id'], 'query': 'Iraq'}
    call_id = svc.digest(['research_search', args])
    old = {'ticket': call_id, 'needs_external_call': True, 'server': 'exa', 'tool': 'web_search_exa', 'query': 'Iraq'}
    with Session(app.state.engine) as s:
        s.add(ResearchToolCall(call_id=call_id, run_id=run['run_id'], tool='research_search', status='succeeded', result_json=json.dumps(old)))
        row = s.get(ResearchRun, run['run_id']);row.search_count = 1;s.add(row);s.commit()
    assert value(rpc(research, 'research_search', {**args, 'scope': 'web'})) == old
    with Session(app.state.engine) as s: assert s.get(ResearchRun, run['run_id']).search_count == 1


def test_concurrent_reservations_share_one_ticket(research, monkeypatch):
    app, _, _ = research
    _, run = setup_case(research)
    block_direct_search(monkeypatch)
    tools = ResearchTools(app.state.engine, app.state.research_settings)
    def reserve(index):
        args = {'run_id': run['run_id'], 'query': 'Iraq'}
        if index: args['scope'] = 'web'
        try: return tools.call('research_search', args)['ticket']
        except ValueError as exc:
            assert '仍在执行' in str(exc)
            return None
    with ThreadPoolExecutor(max_workers=2) as pool: tickets = list(pool.map(reserve, range(2)))
    assert len(set(t for t in tickets if t)) == 1
    with Session(app.state.engine) as s: assert s.get(ResearchRun, run['run_id']).search_count == 1


def test_library_works_even_when_external_budget_is_exhausted(research, monkeypatch):
    app, _, _ = research
    _, run = setup_case(research)
    network = block_direct_search(monkeypatch)
    with Session(app.state.engine) as s:
        row = s.get(ResearchRun, run['run_id']);row.search_count = 10;s.add(row);s.commit()
    result = value(rpc(research, 'research_search', {'run_id': run['run_id'], 'scope': 'library', 'query': 'policy'}))
    assert result['results'][0]['article_key'] == 'seed'
    assert rpc(research, 'research_search', {'run_id': run['run_id'], 'query': 'Iraq'})['isError']
    network.assert_not_called()
    with Session(app.state.engine) as s: assert s.get(ResearchRun, run['run_id']).search_count == 10


def test_legacy_failed_run_gets_restart_guidance_instead_of_old_400(research, monkeypatch):
    app, client, _ = research
    current, run = setup_case(research)
    args = {'run_id': run['run_id'], 'query': 'Iraq', 'scope': 'web'}
    with Session(app.state.engine) as s:
        env = s.get(ResearchEnvironment, run['run_id']);env.search_provider = 'worldnewsapi';s.add(env)
        s.add(ResearchToolCall(call_id=svc.digest(['research_search', args]), run_id=run['run_id'], tool='research_search',
                              status='failed', result_json=json.dumps({'error': '搜索服务返回 HTTP 400'})))
        s.commit()
    network = block_direct_search(monkeypatch)
    failed = rpc(research, 'research_search', args)
    assert failed['isError'] and '请停止旧轮次' in failed['content'][0]['text']
    assert 'HTTP 400' not in failed['content'][0]['text']
    # Source reads and partial reports remain available to close out the old run.
    source_id = current['sources'][0]['source_id']
    assert not rpc(research, 'research_read_source', {'run_id': run['run_id'], 'source_id': source_id})['isError']
    assert not rpc(research, 'research_save_report', {'run_id': run['run_id'], 'title': 'Progress',
              'sections': [{'heading': 'Pending', 'body': 'Restart search route', 'claim_ids': []}],
              'gaps': ['Search needs restart'], 'completeness': 'partial'})['isError']
    app.state.research.cancel(current['case_id'])
    new = app.state.research.enqueue(current['case_id']);app.state.research.dispatch(new['run_id'])
    assert value(rpc(research, 'research_search', {**args, 'run_id': new['run_id']}))['server'] == 'exa'
    detail = client.get('/api/research/cases/' + current['case_id']).json()
    assert len(detail['reports']) == 1 and len(detail['sources']) == 1
    assert not detail['search_policy']['restart_required']
    network.assert_not_called()


def test_external_400_does_not_exhaust_research_budget(research):
    app, _, _ = research
    _, run = setup_case(research)
    args = {'run_id': run['run_id'], 'query': 'Iraq'}
    first = value(rpc(research, 'research_search', args))
    saved = value(rpc(research, 'research_record_search', {'run_id': run['run_id'], 'ticket': first['ticket'],
                         'results': [], 'error': 'Exa HTTP 400: invalid search parameter'}))
    assert not saved['needs_external_call']
    assert value(rpc(research, 'research_search', args)) == saved
    assert value(rpc(research, 'research_search', {**args, 'query': 'Iraq original sources'}))['needs_external_call']
    with Session(app.state.engine) as s: assert s.get(ResearchRun, run['run_id']).search_count == 2


def test_local_missing_exa_blocks_before_model_submission(research):
    app, client, fake = research
    current, _ = setup_case(research, start=False)
    fake.servers['exa']['enabled'] = False
    status = client.post('/api/research/check').json()
    assert status['ready'] is False and status['search_ready'] is False
    run = app.state.research.enqueue(current['case_id']);app.state.research.dispatch(run['run_id'])
    assert not fake.sessions
    with Session(app.state.engine) as s:
        row = s.get(ResearchRun, run['run_id'])
        assert row.status == 'failed' and 'Exa' in row.error


def test_research_settings_page_has_one_search_owner(research):
    _, client, _ = research
    html = client.get('/research/settings').text
    assert 'Codex → Exa' in html and 'value="worldnewsapi"' not in html and 'id="search-key"' not in html

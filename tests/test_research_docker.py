"""One-click orchestration contracts, with no Docker Engine or live model."""
import copy
import json

import pytest
from sqlmodel import Session

from test_research import FakeRunDesk, research, setup_case, rpc, value
from news2douyin.research.config import SettingsStore
from news2douyin.research.models import ResearchRun, ResearchEnvironment
from news2douyin.research.rundesk import RemoteError, RESEARCH_TOOLS, callback_for_docker


class DockerRunDesk(FakeRunDesk):
    def __init__(self):
        super().__init__()
        self.execution = {'mode': 'local'}
        self.permissions = {'sandbox': 'workspace-write', 'approvalPolicy': 'on-request', 'reviewer': 'user'}
        self.revision = self.mcp_revision = 0
        self.account_ready = True
        self.on_prepare = None
        self.tools = {'news2douyin-research': sorted(RESEARCH_TOOLS | {'research_record_search'}), 'wigolo': ['search'], 'exa': ['web_search_exa']}

    def _request(self, method, path, payload=None, params=None):
        if path == '/meta':
            return {'version': '0.47.1', 'demo': False, 'apiVersions': ['v1'], 'capabilities': ['api-v1', 'idempotency', 'configuration-summary', 'application-connect', 'application-runtime', 'mcp-server-approval-policy']}
        if path == '/applications/news2douyin/connect':
            if not self.instances:
                self.instances.append({'id': 'instance-research'})
                self.workspaces.append({'id': 'workspace-research'})
            return {'application': {'instanceId': 'instance-research', 'workspaceId': 'workspace-research'}, 'status': 'connected', 'message': 'connected', 'managementPath': '/#application/news2douyin'}
        self.calls.append((method, path, copy.deepcopy(payload), params))
        if path == '/instances/instance-research/configuration':
            return {'instance': {'id': 'instance-research', 'revision': self.revision, 'execution': self.execution, 'permissions': self.permissions, 'agentRuntime': {'kind': ''}}}
        if path.endswith('/execution'):
            assert payload['revision'] == self.revision
            self.revision += 1
            self.execution = payload['spec']; return {'execution': self.execution}
        if path == '/instances/instance-research' and method == 'PATCH':
            assert self.execution['mode'] == 'docker'
            assert payload['revision'] == self.revision
            self.revision += 1
            self.permissions = payload['permissions']; return {'permissions': self.permissions}
        if path.endswith('/runtime/prepare'):
            if self.on_prepare: self.on_prepare()
            return {'instanceId': 'instance-research', 'workspaceId': 'workspace-research', 'executionMode': 'docker',
                    'environment': {'id': 'env-research', 'state': 'running', 'imageId': 'sha256:fixture', 'pendingUpdate': False},
                    'probed': payload['probe'], 'accountReady': self.account_ready, 'servers': self.tools, 'errors': {}}
        if path.endswith('/mcp'):
            return {'version': str(self.mcp_revision), 'effectiveServers': self.servers, 'userServers': {}}
        if '/mcp/' in path and method == 'PUT':
            assert params['instanceId'] == 'instance-research'
            assert payload['version'] == str(self.mcp_revision)
            self.mcp_revision += 1
            self.servers[path.rsplit('/', 1)[1]] = copy.deepcopy(payload['config'])
            return {'saved': True}
        if '/skill-bundles/' in path:
            if 'news-research' not in self.skills: raise RemoteError('missing', status=404, code='skill_not_found')
            return {'name': 'news-research'}
        # Let the base fixture own and record other routes once.
        self.calls.pop()
        response = super()._request(method, path, payload, params)
        if path == '/sessions' and method == 'POST':
            response.update(executionMode='docker', environmentId='env-research')
        return response


def docker_case(research, start=False):
    app, client, _ = research
    fake = DockerRunDesk()
    app.state.research.client_factory = lambda _: fake
    config = app.state.research_settings.get()
    config.update(runtime_mode='docker', docker_image='rundesk-news:test', search_provider='rundesk', search_backend='exa')
    app.state.research_settings.write(config)
    case, run = setup_case((app, client, fake), start=start)
    return case, run, fake


def test_one_click_prepares_checks_then_submits_and_reuses_active_run(research):
    app, client, _ = research
    case, _, fake = docker_case(research)
    fake.calls.clear()
    first = client.post('/api/research/cases/' + case['case_id'] + '/start', json={}).json()
    assert client.post('/api/research/cases/' + case['case_id'] + '/start', json={}).json()['run_id'] == first['run_id']
    app.state.research.dispatch(first['run_id'])
    with Session(app.state.engine) as s:
        assert s.get(ResearchRun, first['run_id']).status == 'running'
        plan = s.get(ResearchEnvironment, first['run_id'])
        assert plan.status == 'ready' and plan.environment_id == 'env-research'
    paths = [p for _, p, _, _ in fake.calls]
    assert paths.index('/applications/news2douyin/runtime/prepare') < paths.index('/sessions') < next(i for i,p in enumerate(paths) if p.endswith('/turns'))
    assert len(fake.sessions) == 1
    detail = client.get('/api/research/cases/' + case['case_id']).json()
    assert detail['runs'][0]['environment']['image_id'] == 'sha256:fixture'
    assert any(a['kind'] == 'environment' for a in detail['activity'])


@pytest.mark.parametrize('missing', ['account', 'business', 'search'])
def test_missing_prerequisite_never_creates_session(research, missing):
    app, _, _ = research; case, _, fake = docker_case(research)
    if missing == 'account': fake.account_ready = False
    elif missing == 'business': fake.tools['news2douyin-research'] = ['research_get_case']
    else: fake.tools['exa'] = []
    run = app.state.research.enqueue(case['case_id']); app.state.research.dispatch(run['run_id'])
    with Session(app.state.engine) as s:
        current = s.get(ResearchRun, run['run_id'])
        assert current.status == 'failed' and current.error
    assert not fake.sessions


def test_cancel_during_container_prepare_does_not_submit(research):
    app, _, _ = research; case, _, fake = docker_case(research)
    fake.on_prepare = lambda: app.state.research.cancel(case['case_id'])
    run = app.state.research.enqueue(case['case_id']); app.state.research.dispatch(run['run_id'])
    with Session(app.state.engine) as s: assert s.get(ResearchRun, run['run_id']).status == 'paused'
    assert not fake.sessions


def test_lost_prepare_response_retries_only_preparation(research):
    app, _, _ = research; case, _, fake = docker_case(research)
    calls = 0
    def timeout_once():
        nonlocal calls
        calls += 1
        if calls == 1: raise RemoteError('lost preparation response', uncertain=True)
    fake.on_prepare = timeout_once
    run = app.state.research.enqueue(case['case_id']); app.state.research.dispatch(run['run_id'])
    assert not fake.sessions
    app.state.research.sync(run['run_id'])
    assert len(fake.sessions) == 1 and calls == 2


def test_setup_adds_only_missing_container_config_and_preserves_disabled(research):
    app, client, _ = research; _, _, fake = docker_case(research)
    assert fake.servers['news2douyin-research']['url'] == 'http://host.docker.internal:18080/mcp/research'
    assert fake.servers['unrelated'] == {'command': 'keep-me', 'default_tools_approval_mode': 'approve'}
    assert client.post('/api/research/setup').status_code == 200
    assert sum(p.endswith('/execution') for _, p, _, _ in fake.calls) == 1
    fake.servers['news2douyin-research']['enabled'] = False
    assert client.post('/api/research/setup').status_code == 400
    assert fake.servers['news2douyin-research']['enabled'] is False
    assert 'Bearer ' not in client.get('/api/research/settings').text


def test_external_search_budget_result_and_provenance(research):
    app, client, _ = research; case, run, fake = docker_case(research, start=True)
    ticket = value(rpc(research, 'research_search', {'run_id': run['run_id'], 'query': 'original policy'}))
    assert ticket['needs_external_call'] and ticket['server'] == 'exa'
    args = {'run_id': run['run_id'], 'ticket': ticket['ticket'], 'results': [{'title': 'Policy', 'url': 'https://example.com/policy', 'snippet': 'Search clue only'}]}
    recorded = value(rpc(research, 'research_record_search', args))
    assert recorded['provenance'] == 'agent_reported_mcp_search'
    assert value(rpc(research, 'research_record_search', args)) == recorded
    assert rpc(research, 'research_record_search', {**args, 'results': []})['isError']
    detail = client.get('/api/research/cases/' + case['case_id']).json()
    assert detail['runs'][0]['search_count'] == 1
    assert len(detail['sources']) == 1  # Original article only; no snippet as evidence.
    assert detail['searches'][0]['results'] == args['results']
    # Selection is frozen at enqueue, even if the settings file changes.
    config = app.state.research_settings.get(); config['search_provider'] = 'tavily'; app.state.research_settings.write(config)
    again = value(rpc(research, 'research_search', {'run_id': run['run_id'], 'query': 'another query'}))
    assert again['server'] == 'exa'


def test_wigolo_settings_migrate_but_old_run_snapshot_stays_wigolo(research):
    app, _, _ = research
    case, run, _ = docker_case(research, start=True)
    with Session(app.state.engine) as s:
        plan = s.get(ResearchEnvironment, run['run_id'])
        plan.search_backend = 'wigolo'
        s.add(plan); s.commit()
    settings = app.state.research_settings
    old = settings.get(); old['search_backend'] = 'wigolo'
    settings.write(old)
    new = settings.get()
    assert new['search_backend'] == 'exa'
    assert new['mcp_token'] == old['mcp_token']
    assert new['instance_id'] == old['instance_id']
    result = rpc(research, 'research_search', {'run_id': run['run_id'], 'query': 'legacy source'})
    assert result['isError'] and '请停止旧轮次' in result['content'][0]['text']
    with Session(app.state.engine) as s:
        assert s.get(ResearchEnvironment, run['run_id']).search_backend == 'wigolo'


def test_explicit_reconnect_enables_exa_preserving_custom_endpoint(research):
    app, client, _ = research
    _, _, fake = docker_case(research)
    fake.servers['exa'] = {'url': 'https://example.com/custom-exa', 'enabled': False,
                           'http_headers': {'Authorization': 'Bearer fixture-custom'}}
    fake.servers['wigolo'] = {'command': 'legacy-wigolo', 'enabled': True}
    assert client.post('/api/research/setup').status_code == 200
    assert fake.servers['exa']['enabled'] is True
    assert fake.servers['exa']['url'] == 'https://example.com/custom-exa'
    assert fake.servers['exa']['http_headers']['Authorization'] == 'Bearer fixture-custom'
    assert fake.servers['wigolo']['enabled'] is False


def test_external_result_rejected_after_stop_and_over_budget(research):
    app, _, _ = research; case, run, _ = docker_case(research, start=True)
    ticket = value(rpc(research, 'research_search', {'run_id': run['run_id'], 'query': 'first'}))
    with Session(app.state.engine) as s:
        row=s.get(ResearchRun,run['run_id']);row.search_count=10;s.add(row);s.commit()
    assert rpc(research, 'research_search', {'run_id': run['run_id'], 'query': 'over budget'})['isError']
    app.state.research.cancel(case['case_id'])
    assert rpc(research, 'research_record_search', {'run_id': run['run_id'], 'ticket': ticket['ticket'], 'results': []})['isError']


def test_callback_and_settings_upgrade(tmp_path):
    new = SettingsStore(tmp_path); value = new.get()
    assert value['runtime_mode'] == 'docker' and value['search_provider'] == 'rundesk'
    assert callback_for_docker(value) == 'http://host.docker.internal:18080'
    value['rundesk_url'] = 'http://192.168.1.5:3210'
    with pytest.raises(ValueError): callback_for_docker(value)
    for key in ['runtime_mode', 'docker_image', 'mcp_callback_url', 'search_backend']: value.pop(key)
    new.write(value)
    assert new.get()['runtime_mode'] == 'local'


def test_setup_allows_concurrent_mcp_auth_settings_read(research):
    import concurrent.futures
    app, client, _ = research; _, _, fake = docker_case(research)
    def callback():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(app.state.research_settings.get).result(timeout=2)['mcp_token']
    fake.on_prepare = callback
    assert client.post('/api/research/setup').status_code == 200


def test_reconnect_does_not_restore_deleted_mcp(research):
    app, client, _ = research; _, _, fake = docker_case(research)
    del fake.servers['news2douyin-research']
    response = client.post('/api/research/setup')
    assert response.status_code == 400 and '不会自动恢复' in response.json()['detail']
    assert 'news2douyin-research' not in fake.servers


def test_preflight_session_binding_change_never_sends_turn(research):
    app, _, _ = research; case, _, fake = docker_case(research)
    original = fake._request
    def request(method, path, payload=None, params=None):
        result = original(method, path, payload, params)
        if method == 'POST' and path == '/sessions': result['executionMode'] = 'local'
        return result
    fake._request = request
    run = app.state.research.enqueue(case['case_id']); app.state.research.dispatch(run['run_id'])
    assert not any(path.endswith('/turns') for _, path, _, _ in fake.calls)
    with Session(app.state.engine) as s: assert s.get(ResearchRun, run['run_id']).status == 'failed'


def test_full_access_upgrade_preserves_credentials_enablement_and_limits(research):
    app, client, _ = research; case, _, fake = docker_case(research)
    own = fake.servers['news2douyin-research']
    own.pop('default_tools_approval_mode')  # Simulate the installed 0.13.0 config.
    own.update(enabled_tools=['research_read_source'], disabled_tools=['research_save_report'],
               tools={'research_read_source': {'approval_mode': 'prompt', 'output_token_limit': 321}})
    old_headers = copy.deepcopy(own['http_headers'])
    fake.servers['wigolo'] = {'command': 'rundesk-wigolo', 'env': {'TOKEN': 'fixture-secret'}, 'tools': {'search': {'approval_mode': 'prompt'}}}
    fake.servers['disabled'] = {'command': 'do-not-enable', 'enabled': False, 'default_tools_approval_mode': 'prompt'}
    disabled = copy.deepcopy(fake.servers['disabled'])
    fake.permissions = {'sandbox': 'workspace-write', 'approvalPolicy': 'on-request', 'reviewer': 'auto_review', 'networkAccess': False}
    assert client.post('/api/research/setup').status_code == 200
    own = fake.servers['news2douyin-research']
    assert own['http_headers'] == old_headers
    assert own['enabled_tools'] == ['research_read_source'] and own['disabled_tools'] == ['research_save_report']
    assert own['tools']['research_read_source'] == {'approval_mode': 'approve', 'output_token_limit': 321}
    assert own['default_tools_approval_mode'] == 'approve'
    assert fake.servers['wigolo']['enabled'] is False
    assert fake.servers['wigolo']['tools']['search']['approval_mode'] == 'prompt'
    assert fake.servers['wigolo']['env']['TOKEN'] == 'fixture-secret'
    assert fake.servers['disabled'] == disabled
    assert fake.permissions == {'sandbox': 'danger-full-access', 'approvalPolicy': 'never', 'reviewer': 'user'}
    fake.calls.clear()
    assert client.post('/api/research/setup').status_code == 200
    assert not any(method in {'PATCH', 'PUT'} for method, _, _, _ in fake.calls)
    # Starting a research run is not another configuration action.
    fake.calls.clear()
    run = app.state.research.enqueue(case['case_id']); app.state.research.dispatch(run['run_id'])
    assert not any(method in {'PATCH', 'PUT'} for method, _, _, _ in fake.calls)


def test_preserve_setting_does_not_modify_existing_permissions(research):
    app, client, _ = research; _, _, fake = docker_case(research)
    config = app.state.research_settings.get(); config['docker_permissions'] = 'preserve'; app.state.research_settings.write(config)
    fake.servers['news2douyin-research']['default_tools_approval_mode'] = 'prompt'
    fake.permissions = {'sandbox': 'read-only', 'approvalPolicy': 'on-request', 'reviewer': 'user'}
    before = copy.deepcopy((fake.servers, fake.permissions)); fake.calls.clear()
    assert client.post('/api/research/setup').status_code == 200
    assert (fake.servers, fake.permissions) == before
    assert not any(method in {'PATCH', 'PUT'} for method, _, _, _ in fake.calls)


def test_full_access_refuses_host_execution(research):
    from news2douyin.research.rundesk import configure_docker_full_access
    app, _, _ = research; _, _, fake = docker_case(research)
    fake.execution = {'mode': 'local'}; fake.calls.clear()
    with pytest.raises(ValueError, match='未放宽'):
        configure_docker_full_access(fake, app.state.research_settings.get())
    assert not any(method in {'PATCH', 'PUT'} for method, _, _, _ in fake.calls)


def test_old_rundesk_rejected_before_configuration_write(research):
    app, client, _ = research; _, _, fake = docker_case(research)
    original = fake._request
    def request(method, path, payload=None, params=None):
        result = original(method, path, payload, params)
        if path == '/meta': result['capabilities'].remove('mcp-server-approval-policy')
        return result
    fake._request = request; fake.calls.clear()
    response = client.post('/api/research/setup')
    assert response.status_code == 400 and '0.47.1' in response.json()['detail']
    assert not any(method in {'POST', 'PATCH', 'PUT'} for method, _, _, _ in fake.calls)


def test_invalid_docker_permission_setting(research):
    _, client, _ = research
    assert client.put('/api/research/settings', json={'docker_permissions': 'anything'}).status_code == 400


@pytest.mark.parametrize('concurrent', ['local', 'same-image', 'other-image'])
def test_execution_conflict_refreshes_once_without_overwriting_image(research, concurrent):
    app, client, _ = research; _, _, fake = docker_case(research)
    fake.execution = {'mode': 'local'}
    original = fake._request
    revisions = []
    def request(method, path, payload=None, params=None):
        if method == 'PUT' and path.endswith('/execution'):
            revisions.append(payload['revision'])
            if len(revisions) == 1:
                fake.revision += 1
                if concurrent != 'local':
                    fake.execution = {'mode': 'docker', 'image': 'rundesk-news:test' if concurrent == 'same-image' else 'admin-image:keep'}
                raise RemoteError('conflict', status=409, code='revision_conflict')
        return original(method, path, payload, params)
    fake._request = request
    response = client.post('/api/research/setup')
    if concurrent == 'other-image':
        assert response.status_code == 400 and '已绑定镜像不同' in response.json()['detail']
        assert fake.execution['image'] == 'admin-image:keep' and len(revisions) == 1
    else:
        assert response.status_code == 200, response.text
        assert fake.execution['image'] == 'rundesk-news:test'
        if concurrent == 'local': assert revisions == [revisions[0], revisions[0] + 1]
        else: assert len(revisions) == 1


@pytest.mark.parametrize(('status', 'code', 'attempts'), [(401, 'unauthorized', 1), (502, 'docker_unavailable', 1), (409, 'other_conflict', 1), (409, 'revision_conflict', 2)])
def test_execution_setup_errors_propagate_with_bounded_retry(research, status, code, attempts):
    app, client, _ = research; _, _, fake = docker_case(research)
    fake.execution = {'mode': 'local'}
    original = fake._request
    writes = []
    def request(method, path, payload=None, params=None):
        if method == 'PUT' and path.endswith('/execution'):
            writes.append(payload)
            fake.revision += 1
            raise RemoteError('expected remote failure', status=status, code=code)
        return original(method, path, payload, params)
    fake._request = request; fake.calls.clear()
    response = client.post('/api/research/setup')
    assert response.status_code == 400 and 'expected remote failure' in response.json()['detail']
    assert len(writes) == attempts
    assert not any(path.endswith('/runtime/prepare') for _, path, _, _ in fake.calls)


def test_managed_docker_skill_does_not_send_stale_host_path(research):
    app, client, _ = research; case, _, fake = docker_case(research)
    config = app.state.research_settings.get()
    config.update(codex_home='/old-host/instance/codex', skill_path='/old-host/instance/codex/skills/news-research/SKILL.md')
    app.state.research_settings.write(config)
    fake.skills['news-research']['path'] = '/docker/environment/codex/skills/news-research/SKILL.md'
    assert client.post('/api/research/setup').status_code == 200
    run = app.state.research.enqueue(case['case_id']); app.state.research.dispatch(run['run_id'])
    turn = next(payload for method, path, payload, _ in fake.calls if method == 'POST' and path.endswith('/turns'))
    assert turn['skills'] == []
    assert '/old-host/' not in json.dumps(turn)
    assert fake.skills['news-research']['path'].startswith('/docker/environment/')
    assert app.state.research_settings.get()['managed_by_rundesk'] is True

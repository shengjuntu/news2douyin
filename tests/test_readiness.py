import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from news2douyin.collect.service import create_or_update_profile
from news2douyin.server import readiness
from news2douyin.server.app import create_app
from news2douyin.storage.models import CollectJob, CollectProfile, ProfileState, TaskRecord


@pytest.fixture
def app(tmp_path, monkeypatch):
    for name in ['API_KEY', 'ALT_NEWS_KEY', 'MODEL', 'OPENAI_BASE_URL', 'OPENAI_API_KEY']:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE', 'Asia/Shanghai')
    # Prevent a developer's .env from being loaded by this isolated fixture.
    monkeypatch.setenv('NEWS2DOUYIN_ENV_FILE', str(tmp_path/'absent.env'))
    result = create_app(db_url=f'sqlite:///{tmp_path}/app.db', storage_root=str(tmp_path/'runs'))
    yield result
    result.state.engine.dispose()


def test_empty_install_does_not_call_network_or_create_files(app, monkeypatch):
    monkeypatch.setattr(readiness, 'build_opener', lambda *a: pytest.fail('GET must not probe'))
    from news2douyin.video import media
    monkeypatch.setattr(media, 'capabilities', lambda: pytest.fail('GET must not run media probes'))
    client = TestClient(app)
    page = client.get('/setup'); assert page.status_code == 200 and '跑通第一条新闻' in page.text
    r = client.get('/api/setup/check'); assert r.headers['cache-control'] == 'no-store'
    data = r.json(); assert data['database_ok'] and not data['worker_running']
    assert data['profiles'] == [] and data['summary']['enabled_profiles'] == 0
    assert data['calendar']['timezone'] == 'Asia/Shanghai'
    assert data['model']['defaults'] == ['OPENAI_BASE_URL', 'OPENAI_API_KEY', 'MODEL']
    assert data['storage'] == dict(exists=False, permissions_ok=False)
    assert '首次运行检查' in client.get('/daily').text
    with Session(app.state.engine) as session:
        assert not session.exec(select(TaskRecord)).all()


def test_profile_credential_overrides_and_disabled_counts(app, monkeypatch):
    monkeypatch.setenv('ALT_NEWS_KEY', 'secret-news-token')
    with Session(app.state.engine) as s:
        for p in [dict(name='demo', provider='mock'), dict(name='no-key', provider='worldnewsapi'),
                  dict(name='real', provider='worldnewsapi', extra={'api_key_env':'ALT_NEWS_KEY'}),
                  dict(name='bad-key-name', provider='worldnewsapi', extra={'api_key_env':''}),
                  dict(name='paused', provider='mock')]:
            create_or_update_profile(s, p)
        s.add(CollectProfile(name='broken-extra', provider='worldnewsapi', extra_json='[]'))
        s.add(ProfileState(name='paused', enabled=False)); s.commit()
    data = readiness.configuration_report(app)
    profiles = {p['name']: p for p in data['profiles']}
    assert profiles['real']['state'] == 'configured' and profiles['no-key']['state'] == 'missing'
    assert not profiles['paused']['enabled']
    assert data['summary']['enabled_profiles'] == 5 and data['summary']['demo_profiles'] == 1
    assert profiles['bad-key-name']['state'] == 'invalid' and profiles['broken-extra']['state'] == 'invalid'
    assert 'secret-news-token' not in json.dumps(data)


def test_schedule_dependencies_next_time_and_no_mutation(app, monkeypatch):
    with Session(app.state.engine) as s:
        create_or_update_profile(s, dict(name='demo', provider='mock'))
        create_or_update_profile(s, dict(name='paused', provider='mock'))
        s.add(ProfileState(name='paused', enabled=False))
        for name, profile, cron, zone, enabled, kind in [
                ('ok', 'demo', '0 9 * * *', 'Asia/Shanghai', True, 'cron'),
                ('paused-profile', 'paused', '0 9 * * *', 'UTC', True, 'cron'),
                ('missing-profile', 'missing', '0 9 * * *', 'UTC', True, 'cron'),
                ('bad-cron', 'demo', 'broken', 'UTC', True, 'cron'),
                ('bad-zone', 'demo', '0 9 * * *', 'bad-zone', True, 'cron'),
                ('disabled', 'demo', '0 9 * * *', 'UTC', False, 'cron'),
                ('archived', 'demo', '0 9 * * *', 'UTC', False, 'archived')]:
            s.add(CollectJob(name=name, profile_name=profile, cron_expr=cron, timezone=zone, enabled=enabled, schedule_type=kind))
        s.commit(); before = [x.model_dump() for x in s.exec(select(CollectJob))]
    data = readiness.configuration_report(app)
    jobs = {j['name']:j for j in data['schedules']}
    assert len(jobs) == 6 and jobs['ok']['next_run'].endswith('+08:00')
    assert data['summary']['schedule_issues'] == 4
    assert not jobs['disabled']['next_run'] and not jobs['paused-profile']['next_run']
    with Session(app.state.engine) as s:
        assert [x.model_dump() for x in s.exec(select(CollectJob))] == before
        assert not s.exec(select(TaskRecord)).all()


def test_invalid_calendar_does_not_break_setup(app, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE', 'bad/zone')
    r = TestClient(app).get('/api/setup/check')
    assert r.status_code == 200 and r.json()['calendar']['ok'] is False


@pytest.mark.parametrize('address', ['file:///secret', 'https://name:secret@host/v1',
                                    'https://host/v1?key=secret', 'https://host/v1#secret',
                                    'https://host:invalid/v1', 'http://host/invalid path'])
def test_invalid_model_address_cannot_send_credentials(app, monkeypatch, address):
    monkeypatch.setenv('OPENAI_BASE_URL', address)
    monkeypatch.setenv('OPENAI_API_KEY', 'private-model-key')
    monkeypatch.setattr(readiness, 'build_opener', lambda *a: pytest.fail('invalid endpoint must not be called'))
    report = readiness.probe_model()
    assert report['state'] == 'invalid'
    assert 'secret' not in json.dumps(report) and 'private-model-key' not in json.dumps(report)


def test_model_catalog_success_is_not_generation_success(app, monkeypatch):
    monkeypatch.setenv('OPENAI_BASE_URL', 'http://127.0.0.1:19993/v1')
    monkeypatch.setenv('MODEL', 'fixture-model')
    monkeypatch.setenv('OPENAI_API_KEY', 'private-model-key')
    captured = {}
    class Response(io.BytesIO): status=200
    def open_request(req, timeout):
        captured.update(url=req.full_url, headers=req.headers, method=req.get_method(), timeout=timeout)
        return Response(b'{"data":[{"id":"fixture-model"}]}')
    monkeypatch.setattr(readiness, 'build_opener', lambda *a: SimpleNamespace(open=open_request))
    report = readiness.probe_model()
    assert report['state'] == 'listed' and '尚未验证生成能力' in report['message']
    assert captured == dict(url='http://127.0.0.1:19993/v1/models', headers={'Content-type':'application/json','Authorization':'Bearer private-model-key'}, method='GET', timeout=3)
    assert 'private-model-key' not in json.dumps(report)


@pytest.mark.parametrize('body,state', [(b'{"data":[{"id":"other"}]}', 'model_not_listed'),
    (b'not json', 'invalid_response'), (b'[]', 'invalid_response'),
    (b'{"data":{}}', 'invalid_response'), (b'x'*1048577, 'invalid_response')])
def test_model_invalid_and_limited_responses(app, monkeypatch, body, state):
    class Response(io.BytesIO): status=200
    monkeypatch.setattr(readiness, 'build_opener', lambda *a: SimpleNamespace(open=lambda *args, **kw: Response(body)))
    assert readiness.probe_model()['state'] == state


@pytest.mark.parametrize('code,state', [(401,'auth_error'),(403,'auth_error'),(404,'not_found'),(429,'rate_limited'),(302,'redirect'),(500,'http_error')])
def test_upstream_errors_are_actionable_and_redacted(app, monkeypatch, code, state):
    def fail(*a, **kw): raise HTTPError('https://host/secret',code,'private-token',{},io.BytesIO(b'private-body'))
    monkeypatch.setattr(readiness, 'build_opener', lambda *a: SimpleNamespace(open=fail))
    report=readiness.probe_model();assert report['state']==state
    assert 'private' not in json.dumps(report) and 'secret' not in json.dumps(report)


@pytest.mark.parametrize('error,state', [(TimeoutError('secret'),'timeout'),(URLError('secret'),'unreachable')])
def test_model_unreachable_without_raw_exception(app, monkeypatch, error, state):
    def fail(*a, **kw): raise error
    monkeypatch.setattr(readiness, 'build_opener', lambda *a: SimpleNamespace(open=fail))
    report=readiness.probe_model();assert report['state']==state and 'secret' not in json.dumps(report)


def test_redirect_handler_refuses_followup():
    assert readiness._NoRedirect().redirect_request(None,None,302,'',{},'https://another-host') is None


def test_video_report_only_exposes_curated_checks_and_handles_failure(app, monkeypatch):
    from news2douyin.video import media
    monkeypatch.setattr(media,'capabilities',lambda:dict(ready=True,checks=[{'key':'pillow','ok':True}],errors=['secret-token-path'],font_name='private'))
    client=TestClient(app);r=client.post('/api/setup/video-check');assert r.status_code==200 and r.json()['ready']
    assert 'secret' not in r.text and 'private' not in r.text
    def fail():raise RuntimeError('secret-token-path')
    monkeypatch.setattr(media,'capabilities',fail)
    assert client.post('/api/setup/video-check').json()['ok'] is False


def test_database_error_is_redacted_and_retryable(app, monkeypatch):
    def fail(*a):raise OperationalError('secret-db-url',{},Exception('private-password'))
    monkeypatch.setattr(readiness,'configuration_report',fail)
    r=TestClient(app).get('/api/setup/check')
    assert r.status_code==503 and 'secret' not in r.text and 'private' not in r.text

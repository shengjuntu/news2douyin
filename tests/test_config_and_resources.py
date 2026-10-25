import json
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from news2douyin.collect import llm_filter
from news2douyin.llm import settings
from news2douyin.prompts.manager import PromptManager
from news2douyin.export.render import render_markdown
from news2douyin.export.script_builder import build_script_from_story
from news2douyin.server.app import create_app
from sqlmodel import Session
from news2douyin.storage.models import CollectProfile


def test_app_loads_dotenv_after_modules_were_imported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('NEWS2DOUYIN_ENV_FILE', raising=False)
    monkeypatch.delenv('OPENAI_BASE_URL', raising=False)
    monkeypatch.delenv('MODEL', raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'existing-environment-wins')
    (tmp_path / '.env').write_text('OPENAI_BASE_URL=http://fixture/v1\nMODEL=fixture-model\nOPENAI_API_KEY=ignored\n')
    create_app(db_url=f'sqlite:///{tmp_path / "app.db"}', storage_root=str(tmp_path / 'runs'))
    config = settings.get_settings()
    assert config.base_url == 'http://fixture/v1'
    assert config.model == 'fixture-model'
    assert config.api_key == 'existing-environment-wins'
    # Do not leak values loaded by python-dotenv into following tests.
    monkeypatch.delenv('OPENAI_BASE_URL')
    monkeypatch.delenv('MODEL')


def test_probe_sends_same_authentication_as_completion(monkeypatch):
    monkeypatch.setenv('OPENAI_BASE_URL', 'http://fixture/v1/')
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-key')
    monkeypatch.setenv('MODEL', 'fixture-model')
    response = Mock()
    response.__enter__ = Mock(return_value=Mock(status=200))
    response.__exit__ = Mock(return_value=False)
    request = Mock(return_value=response)
    monkeypatch.setattr(settings.urllib.request, 'urlopen', request)
    assert settings.probe_endpoint()
    assert request.call_args.args[0].get_header('Authorization') == 'Bearer fixture-key'
    import requests
    post = Mock(return_value=Mock(json=lambda: {'choices': [{'message': {'content': 'ok'}}]}))
    monkeypatch.setattr(requests, 'post', post)
    assert llm_filter._llm_chat([{'role': 'user', 'content': 'fixture'}]) == 'ok'
    assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer fixture-key'
    assert post.call_args.kwargs['json']['model'] == 'fixture-model'


def test_packaged_prompts_and_templates_work_outside_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = {'profile': 'zh', 'profiles': {'zh': {'script': 'zh/script_v1.txt'}}}
    builtin = PromptManager(cfg).get('script')
    assert builtin
    pack = build_script_from_story({'topic': '测试主题', 'key_points': ['测试事实']}, ['#新闻'])
    for name in ['fast_news.j2', 'explain_60s.j2', 'three_stories.j2']:
        assert render_markdown(Path('templates/douyin') / name, pack)
    override = tmp_path / 'prompts/zh/script_v1.txt'
    override.parent.mkdir(parents=True)
    override.write_text('custom prompt')
    assert PromptManager(cfg).get('script') == 'custom prompt'


def test_complete_negative_llm_decision_is_not_reversed(article, monkeypatch):
    monkeypatch.setattr(llm_filter, 'endpoint_alive', lambda: True)
    decision = {'items': [{'id': 1, 'keep': False, 'categories': [], 'sentiment': 'neutral'}]}
    monkeypatch.setattr(llm_filter, '_generate_with_retry', lambda _: json.dumps(decision))
    assert llm_filter.llm_filter_items([article], {}) == ([], 'llm')


def test_partial_response_preserves_model_rejection(article, monkeypatch):
    monkeypatch.setattr(llm_filter, 'endpoint_alive', lambda: True)
    decision = {'items': [{'id': 1, 'keep': False, 'categories': [], 'sentiment': 'neutral'}]}
    monkeypatch.setattr(llm_filter, '_generate_with_retry', lambda _: json.dumps(decision))
    second = dict(article, url='https://example.com/b')
    kept, mode = llm_filter.llm_filter_items([article, second], {})
    assert [i['url'] for i in kept] == [second['url']]
    assert mode == 'mixed'


def test_app_lifecycle_starts_and_stops_scheduler(tmp_path):
    app = create_app(db_url=f'sqlite:///{tmp_path / "app.db"}', storage_root=str(tmp_path / 'runs'))
    with TestClient(app) as client:
        assert client.get('/api/scheduler/status').json()['running']
    assert not app.state.scheduler._thread.is_alive()


def test_legacy_nested_extra_survives_api_read_and_save(tmp_path):
    app = create_app(db_url=f'sqlite:///{tmp_path / "legacy.db"}', storage_root=str(tmp_path / 'runs'))
    with Session(app.state.engine) as session:
        session.add(CollectProfile(name='legacy', provider='mock', extra_json='{"extra":{"date_str":"2026-09-01","cache_enabled":false}}'))
        session.commit()
    client = TestClient(app)
    payload = client.get('/api/profiles/legacy').json()
    assert payload['extra'] == {'date_str': '2026-09-01', 'cache_enabled': False}
    assert client.post('/api/profiles', json=payload).status_code == 200
    assert client.get('/api/profiles/legacy').json()['extra'] == payload['extra']

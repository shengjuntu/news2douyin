"""Exercise the embedding adapter against an isolated OpenAI-shaped HTTP endpoint."""
import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
from threading import Thread
from types import SimpleNamespace

import pytest

from news2douyin.llm.client import embedding


@pytest.fixture
def endpoint():
    state = SimpleNamespace(calls=[], models=['BAAI/bge-m3'], dimensions=1024, reply=None)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, payload):
            output = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(output)))
            self.end_headers()
            self.wfile.write(output)

        def do_GET(self):
            state.calls.append(('GET', self.path, self.headers.get('Authorization'), None))
            if self.path == '/v1/models':
                self.reply(200, {'object': 'list', 'data': [{'id': model} for model in state.models]})
                return
            self.reply(404, {'error': {'message': 'unexpected route'}})

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state.calls.append(('POST', self.path, self.headers.get('Authorization'), data))
            if self.path != '/v1/embeddings':
                self.reply(404, {'error': {'message': 'unexpected route'}})
                return
            if state.reply is not None:
                self.reply(*state.reply)
                return
            self.reply(200, {'object': 'list', 'model': data['model'], 'data': [
                {'object': 'embedding', 'index': index, 'embedding': [float(index + 1)] * state.dimensions}
                for index in reversed(range(len(data['input'])))
            ]})

        def log_message(self, *_args):
            return

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        state.url = f'http://127.0.0.1:{server.server_port}/v1'
        worker = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.025}, daemon=True)
        worker.start()
        try:
            yield state
        finally:
            server.shutdown()
            worker.join(timeout=2)


@pytest.fixture(autouse=True)
def clean_embedding_env(monkeypatch):
    for name in ('BASE_URL', 'MODEL', 'API_KEY', 'DIMENSIONS', 'TIMEOUT'):
        monkeypatch.delenv(f'EMBEDDING_{name}', raising=False)
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')


def test_batch_preserves_input_order_and_respects_exact_configured_model(endpoint):
    client = embedding.EmbeddingClient('fixture-key', endpoint.url + '/', 'bge-m3-local', dimensions=1024)
    texts = ['上游工厂停产', 'The supplier halted production']
    assert client.get_embedding(texts) == [[1.0] * 1024, [2.0] * 1024]
    assert len(endpoint.calls) == 1
    method, path, authorization, body = endpoint.calls[0]
    assert (method, path, authorization) == ('POST', '/v1/embeddings', 'Bearer fixture-key')
    assert body['model'] == 'bge-m3-local' and body['input'] == texts


def test_env_discovers_the_single_served_id_only_on_first_request(endpoint, monkeypatch):
    monkeypatch.setenv('EMBEDDING_BASE_URL', endpoint.url)
    monkeypatch.setenv('EMBEDDING_DIMENSIONS', '1024')
    monkeypatch.setenv('EMBEDDING_API_KEY', 'environment-key')
    client = embedding.EmbeddingClient.from_env()
    assert endpoint.calls == []
    assert client.get_embedding('上游工厂停产') == [[1.0] * 1024]
    assert client.get_embedding('The factory stopped') == [[1.0] * 1024]
    assert [(method, path) for method, path, _, _ in endpoint.calls] == [
        ('GET', '/v1/models'), ('POST', '/v1/embeddings'), ('POST', '/v1/embeddings'),
    ]
    assert all(auth == 'Bearer environment-key' for _, _, auth, _ in endpoint.calls)
    assert endpoint.calls[1][3]['model'] == endpoint.calls[2][3]['model'] == 'BAAI/bge-m3'


def test_async_embedding_keeps_the_same_batch_shape(endpoint):
    client = embedding.EmbeddingClient('', endpoint.url, 'bge-m3')
    assert asyncio.run(client.get_embedding_async(['停产', 'shutdown'])) == [[1.0] * 1024, [2.0] * 1024]
    assert len(endpoint.calls) == 1
    assert endpoint.calls[0][2] is None


@pytest.mark.parametrize('models', [[], ['BAAI/bge-m3', 'another-model']])
def test_model_discovery_rejects_no_match_or_ambiguous_models(endpoint, monkeypatch, models):
    monkeypatch.setenv('EMBEDDING_BASE_URL', endpoint.url)
    endpoint.models = models
    client = embedding.EmbeddingClient.from_env()
    with pytest.raises(embedding.EmbeddingConfigurationError):
        client.get_embedding('新闻事件')
    assert [(method, path) for method, path, _, _ in endpoint.calls] == [('GET', '/v1/models')]


@pytest.mark.parametrize('text', ['', ['valid text', '  '], [3]])
def test_invalid_input_is_rejected_before_contacting_service(endpoint, text):
    client = embedding.EmbeddingClient('', endpoint.url, 'bge-m3')
    with pytest.raises(ValueError):
        client.get_embedding(text)
    assert endpoint.calls == []


@pytest.mark.parametrize('rows', [
    [{'index': 0, 'embedding': [0.1, 0.2]}],
    [{'index': 0, 'embedding': [0.1, 0.2]}, {'index': 0, 'embedding': [0.3, 0.4]}],
    [{'index': 0, 'embedding': [0.1, 0.2]}, {'index': 2, 'embedding': [0.3, 0.4]}],
    [{'index': 0, 'embedding': [0.1]}, {'index': 1, 'embedding': [0.3, 0.4]}],
    [{'index': 0, 'embedding': [float('inf'), 0.2]}, {'index': 1, 'embedding': [0.3, 0.4]}],
])
def test_rejects_corrupt_or_incompatible_vectors(endpoint, rows):
    endpoint.reply = 200, {'data': rows}
    client = embedding.EmbeddingClient('', endpoint.url, 'bge-m3', dimensions=2)
    with pytest.raises(embedding.EmbeddingError):
        client.get_embedding(['原材料供应', '工厂生产'])


def test_http_error_does_not_include_credentials_or_body(endpoint):
    endpoint.reply = 401, {'error': {'message': 'private-body-message'}}
    client = embedding.EmbeddingClient('private-auth-token', endpoint.url, 'bge-m3')
    with pytest.raises(embedding.EmbeddingError) as caught:
        client.get_embedding('private-input-text')
    message = str(caught.value)
    assert '401' in message
    assert all(secret not in message for secret in ('private-body-message', 'private-auth-token', 'private-input-text'))
    assert len(endpoint.calls) == 1


def test_check_command_uses_dotenv_and_only_core_requirements(endpoint, tmp_path):
    env_file = tmp_path / 'service.env'
    env_file.write_text(f'EMBEDDING_BASE_URL={endpoint.url}\nEMBEDDING_DIMENSIONS=1024\n', encoding='utf-8')
    guard = """
import runpy
import sys

class BlockOptionalImports:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'tenacity', 'openai', 'httpx', 'toml'}:
            raise ModuleNotFoundError(fullname)

sys.meta_path.insert(0, BlockOptionalImports())
sys.argv[1:] = ['--check']
runpy.run_module('news2douyin.llm.client.embedding', run_name='__main__')
"""
    env = dict(os.environ, NEWS2DOUYIN_ENV_FILE=str(env_file),
               PYTHONPATH=str(Path(embedding.__file__).resolve().parents[3]))
    checked = subprocess.run([sys.executable, '-c', guard], env=env, cwd=tmp_path,
                             capture_output=True, text=True, timeout=15)
    assert checked.returncode == 0, checked.stderr
    report = json.loads(checked.stdout)
    assert report == {'status': 'ok', 'model': 'BAAI/bge-m3', 'dimensions': 1024, 'batch_size': 2}
    assert [(method, path) for method, path, _, _ in endpoint.calls] == [
        ('GET', '/v1/models'), ('POST', '/v1/embeddings'),
    ]


def test_check_command_rejects_an_unlisted_explicit_id_without_sending_text(endpoint, tmp_path):
    env_file = tmp_path / 'service.env'
    env_file.write_text(f'EMBEDDING_BASE_URL={endpoint.url}\nEMBEDDING_MODEL=stale-name\n'
                        'EMBEDDING_API_KEY=private-auth-token\n', encoding='utf-8')
    env = dict(os.environ, NEWS2DOUYIN_ENV_FILE=str(env_file),
               PYTHONPATH=str(Path(embedding.__file__).resolve().parents[3]))
    checked = subprocess.run([sys.executable, '-m', 'news2douyin.llm.client.embedding', '--check'],
                             env=env, cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert checked.returncode == 1
    assert json.loads(checked.stderr)['status'] == 'error'
    assert 'private-auth-token' not in checked.stdout + checked.stderr
    assert endpoint.url not in checked.stdout + checked.stderr
    assert [(method, path) for method, path, _, _ in endpoint.calls] == [('GET', '/v1/models')]

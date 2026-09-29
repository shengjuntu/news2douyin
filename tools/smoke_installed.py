"""Run with python -I after installing the wheel, outside the source tree.

Requires the dev, assets and llm extras. No external network calls are made.
"""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

os.environ['DEDUP_LLM_ENABLED'] = '0'

from fastapi.testclient import TestClient
from news2douyin.collect import llm_filter
from news2douyin.server import webui
from news2douyin.server.app import create_app
from news2douyin.prompts.manager import PromptManager
from news2douyin.export.render import render_markdown
from news2douyin.export.script_builder import build_script_from_story
from news2douyin.assets.prep import prep_assets


@contextmanager
def working_directory(directory):
    previous = Path.cwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(previous)


def main():
    with tempfile.TemporaryDirectory(prefix='news2douyin-wheel-') as directory, working_directory(directory):
        root = Path(directory)
        with patch.object(llm_filter, 'endpoint_alive', return_value=False), patch.object(webui, '_llm_alive', return_value=False):
            app = create_app(db_url=f'sqlite:///{root / "app.db"}', storage_root=str(root / 'runs'))
            with TestClient(app) as client:
                assert client.get('/').status_code == 200
                assert client.post('/api/profiles', json={'name': 'mock', 'provider': 'mock'}).status_code == 200
                run = client.post('/api/collect/run-now', json={'profile_name': 'mock'})
                assert run.status_code == 200 and run.json()['status'] == 'succeeded'
                task = client.get('/api/tasks').json()[0]
                assert client.get('/tasks').status_code == 200
                assert client.get('/tasks/' + task['task_id']).status_code == 200
                stream = client.get('/api/tasks/' + task['task_id'] + '/stream')
                assert 'event: end' in stream.text and 'succeeded' in stream.text
                app.state.worker.stop()
                queued = client.post('/api/tasks/collect', json={'profile_name': 'mock'})
                assert queued.status_code == 202
                task_id = queued.json()['task_id']
                assert client.post('/api/tasks/' + task_id + '/cancel').json()['status'] == 'cancelled'
                assert client.post('/api/tasks/' + task_id + '/retry').status_code == 202
                app.state.worker.start()
                import time
                deadline = time.monotonic() + 5
                while client.get('/api/tasks/' + task_id).json()['status'] != 'succeeded':
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                events = client.get('/api/events/search').json()
                assert len(events) == 2
                built = client.post('/api/scripts/build', json={'event_key': events[0]['event_key']})
                assert built.status_code == 200
                assert (Path(built.json()['output_dir']) / 'script.txt').is_file()
            app.state.engine.dispose()
        cfg = {'profile': 'zh', 'profiles': {'zh': {'script': 'zh/script_v1.txt'}}}
        assert PromptManager(cfg).get('script')
        script = build_script_from_story({'topic': 'fixture', 'key_points': ['verified fact']}, ['#news'])
        assert render_markdown('templates/douyin/fast_news.j2', script)
        from news2douyin.llm.client_factory import create_client_with_config
        (root / 'model.toml').write_text('[openai_llm]\nmax_tokens = 32\n')
        os.environ['OPENAI_API_KEY'] = 'offline-fixture'
        os.environ['OPENAI_BASE_URL'] = 'http://127.0.0.1:9/v1'
        assert create_client_with_config(str(root / 'model.toml')) is not None
        legacy = root / 'legacy'
        legacy.mkdir()
        (legacy / 'raw.jsonl').write_text('')
        (legacy / 'scored.jsonl').write_text('')
        (legacy / 'selected.json').write_text('{"items": []}')
        assert prep_assets(legacy).is_dir()
        cli = subprocess.run([sys.executable, '-I', '-m', 'news2douyin.cli', '--help'], capture_output=True, text=True)
        assert cli.returncode == 0, cli.stderr
        print(json.dumps({'status': 'passed', 'checks': ['wheel_webui', 'mock_collection', 'script_package', 'bundled_prompts', 'bundled_templates', 'legacy_llm_import', 'packaged_asset_worker', 'cli_entrypoint', 'task_pages', 'task_sse', 'async_collection', 'cancel_and_retry']}))


if __name__ == '__main__':
    main()

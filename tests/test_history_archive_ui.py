from contextlib import closing
from datetime import date

from fastapi.testclient import TestClient

from news2douyin.server.app import create_app


class ArchiveRuntime:
    def __init__(self):
        self.calls = []

    def status(self, *, coverage_limit, coverage_offset):
        self.calls.append(('status', coverage_limit, coverage_offset))
        return {'state': 'paused', 'coverage_total': 1,
                'coverage': [{'state': 'partial', 'archive_missing': 1}],
                'progress': {'total_files': 3, 'downloaded_files': 2, 'ready_files': 1}}

    def start(self, **kwargs):
        self.calls.append(('start', kwargs))
        return {'state': 'planning'}

    def pause(self):
        self.calls.append(('pause',))

    def resume(self):
        self.calls.append(('resume',))

    def retry(self):
        self.calls.append(('retry',))

    def request_window(self, **kwargs):
        self.calls.append(('request_window', kwargs))
        return {'state': 'queued'}


def test_history_archive_product_routes(tmp_path):
    app = create_app(db_url=f'sqlite:///{tmp_path / "archive.db"}', storage_root=str(tmp_path))
    runtime = ArchiveRuntime()
    app.state.history_archive = runtime
    dates = {'date_from': '2022-01-01', 'date_to': '2022-01-07'}
    base = '/api/history-archive'
    with closing(TestClient(app)) as client:
        # The stub replaces the application service; worker lifecycle is
        # kept separate from HTTP behavior in this regression.
        page = client.get('/admin/history-archive')
        assert page.status_code == 200
        assert '历史归档' in page.text and '已可查询' in page.text and '调查加急补档' in page.text
        assert 'name="mentions" type="checkbox"' in page.text
        assert 'href="/admin/history-archive"' in page.text
        asset = client.get('/research-assets/history-archive.js')
        assert asset.status_code == 200 and base in asset.text
        response = client.get(base + '?limit=12&offset=4')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.json()['coverage'][0]['archive_missing'] == 1
        assert runtime.calls[-1] == ('status', 12, 4)
        assert client.post(base + '/start', json=dates).status_code == 202
        assert runtime.calls[-1] == ('start', {'date_from': date(2022, 1, 1),
                                               'date_to': date(2022, 1, 7), 'mentions': False})
        for action in ('pause', 'resume', 'retry'):
            assert client.post(base + '/' + action).status_code == 202
            assert runtime.calls[-1] == (action,)
        case = 'ci_' + 'a' * 32
        node = 'cn_' + 'b' * 32
        targeted = {**dates, 'case_key': case, 'node_key': node, 'mentions': True}
        assert client.post(base + '/request', json=targeted).status_code == 202
        assert runtime.calls[-1] == ('request_window', {'date_from': date(2022, 1, 1),
            'date_to': date(2022, 1, 7), 'mentions': True, 'case_key': case, 'node_key': node})
        count = len(runtime.calls)
        assert client.post(base + '/request', json=targeted,
                           headers={'Origin': 'https://external.example'}).status_code == 403
        assert client.post(base + '/start', json={**dates, 'date_from': '1970-01-01'}).status_code == 422
        assert client.post(base + '/request', json={**targeted, 'node_key': 'invalid'}).status_code == 422
        assert len(runtime.calls) == count

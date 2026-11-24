"""The archive request and program rerun are linked by a durable transaction."""
from datetime import date

from news2douyin.causal import service
from news2douyin.history_archive import catalog
from news2douyin.history_archive.service import query_groups
from test_history_archive_integration import FixtureTransport, _drain
from test_causal_program_worker import create, detail, program_case
from test_postgres_integration import postgres_url


def test_archived_window_resumes_the_existing_program_node_once(program_case, monkeypatch):
    app, client, _ = program_case
    engine, worker = app.state.engine, app.state.history_archive
    case = create(client)
    first = service.claim(engine)
    service.complete(engine, first, retrieval_report={'history': {
        'status': 'archive_gap', 'date_from': '2022-02-22', 'date_to': '2022-02-23'}})

    day = date(2022, 2, 23)
    transport = FixtureTransport(engine)
    transport.add_day(day, mentions=False)
    monkeypatch.setattr(catalog.requests, 'get', transport.get)
    worker.start(day, day)
    _drain(worker)
    missing = next(item['path'] for item in query_groups(engine, day, day) if item['kind'] == 'events')
    (worker.root / missing).unlink()
    payload = dict(case_key=case['key'], node_key=first.node_key, date_from=day.isoformat(), date_to=day.isoformat())
    api = '/api/history-archive/request'
    wrong = client.post(api, json={**payload, 'date_to': '2022-02-24'})
    assert wrong.status_code == 422
    for _ in range(2):
        assert client.post(api, json=payload).status_code == 202
    assert detail(client, case)['nodes'][0]['archive_request']['state'] == 'queued'
    assert worker.step()
    assert service.claim(engine) is None
    _drain(worker)
    assert (worker.root / missing).is_file()
    assert transport.calls.count('20220223000000.export.CSV.zip') == 2
    checked = detail(client, case)
    assert checked['working'] and checked['nodes'][0]['key'] == first.node_key
    assert checked['nodes'][0]['archive_request']['state'] == 'partial'
    resumed = service.claim(engine)
    assert resumed.node_key == first.node_key and resumed.lease != first.lease
    service.complete(engine, resumed, stop_reason='test completed')
    worker.step()
    assert service.claim(engine) is None

"""The archive request and program rerun are linked by a durable transaction."""
from datetime import date

import pytest
from sqlmodel import Session

from news2douyin.causal import service
from news2douyin.history_archive import catalog
from news2douyin.history_archive.models import HistoryGroup
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
    package = catalog.ArchivePackage(name='20220223000000.export.CSV.zip', kind='events', version=2,
        date_from=day, date_to=day, stamp='20220223000000', url='https://data.gdeltproject.org/gdeltv2/example',
        size=42, md5='9' * 32)
    monkeypatch.setattr(catalog, 'list_packages', lambda *a, **k: [package])
    with Session(engine) as session:
        session.add(HistoryGroup(key='2/events/20220223', kind='events', version=2, period='20220223',
                                 date_from=day, date_to=day, state='partial', total_files=1, ready_files=1))
        from news2douyin.history_archive.models import HistoryPart
        session.add(HistoryPart(key=package.name, group_key='2/events/20220223', stamp=package.stamp,
                                url=package.url, size=42, md5=package.md5, state='ready'))
        session.commit()
    payload = dict(case_key=case['key'], node_key=first.node_key, date_from=day.isoformat(), date_to=day.isoformat())
    api = '/api/history-archive/request'
    wrong = client.post(api, json={**payload, 'date_to': '2022-02-24'})
    assert wrong.status_code == 422
    for _ in range(2):
        assert client.post(api, json=payload).status_code == 202
    assert detail(client, case)['nodes'][0]['archive_request']['state'] == 'queued'
    assert worker.step()
    worker.step()
    checked = detail(client, case)
    assert checked['working'] and checked['nodes'][0]['key'] == first.node_key
    assert checked['nodes'][0]['archive_request']['state'] == 'partial'
    resumed = service.claim(engine)
    assert resumed.node_key == first.node_key and resumed.lease != first.lease
    service.complete(engine, resumed, stop_reason='test completed')
    worker.step()
    assert service.claim(engine) is None

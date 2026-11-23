"""A 429 delays the shared GDELT origin across groups and worker restarts."""
from news2douyin.history_archive import catalog, service
from test_history_archive_integration import BASE, FixtureTransport, NEW, OLD, archive_app
from test_postgres_integration import postgres_url


def test_retry_after_pauses_all_plans_and_downloads_until_the_deadline(archive_app, monkeypatch):
    app, client, root = archive_app
    worker = app.state.history_archive
    transport = FixtureTransport(app.state.engine)
    transport.add_day(OLD, mentions=False)
    transport.add_day(NEW, mentions=False)
    clock = [1000.]
    blocked = {'masterfilelist.txt', '20220223000000.export.CSV.zip'}

    def upstream(url, **kwargs):
        name = url.rsplit('/', 1)[-1]
        if name in blocked:
            blocked.remove(name)
            transport.calls.append(name)
            response = transport.Response(b'too many requests', 429)
            response.headers['Retry-After'] = '75'
            return response
        return transport.get(url, **kwargs)

    monkeypatch.setattr(service, 'time', lambda: clock[0])
    monkeypatch.setattr(catalog.requests, 'get', upstream)
    payload = dict(date_from=OLD.isoformat(), date_to=NEW.isoformat(), mentions=False)
    assert client.post(BASE + '/start', json=payload).status_code == 202
    assert not worker.step()
    assert transport.calls == ['masterfilelist.txt']
    state = client.get(BASE).json()
    assert state['state'] == 'planning' and state['retry_at'] == 1075.
    assert '限流' in state['error']

    clock[0] = 1075.
    assert worker.step(), 'same plan can now read the manifest'
    assert worker.step(), 'first package hits the rate limit'
    assert transport.calls[-1] == '20220223000000.export.CSV.zip'
    before = list(transport.calls)
    assert client.get(BASE).json()['retry_at'] == 1150.

    restarted = service.ArchiveWorker(app.state.engine, root)
    app.state.history_archive = restarted
    assert client.post(BASE + '/retry').status_code == 202
    clock[0] = 1149.
    assert not restarted.step()
    assert transport.calls == before
    clock[0] = 1150.
    assert restarted.step()
    assert transport.calls[len(before):] == ['20220223000000.export.CSV.zip']
    assert client.get(BASE).json()['progress']['ready_files'] == 1
    assert client.get(BASE).json()['retry_at'] is None

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from news2douyin.causal import history, model, retrieval
from news2douyin.causal.runtime import CausalWorker
from news2douyin.history_archive.search import ArchiveLeads
from test_causal_program_worker import BASE, create, detail, program_case
from test_postgres_integration import postgres_url


_REAL_HISTORY = history.search_history
LOWER = '1989-01-01T00:00:00'
UPPER = '2022-02-24T00:00:00'
COVERED = {'events': {'state': 'covered'}, 'gkg': {'state': 'covered'}}


def test_named_date_checked_before_recent_material_and_no_empty_window_repeated(monkeypatch):
    calls = []

    def lookup(engine, root, start, end, words, limit, *, cutoff):
        calls.append((start, end))
        assert cutoff == datetime.combine(end, datetime.min.time(), timezone.utc)
        return ArchiveLeads([], COVERED)

    monkeypatch.setattr(history, 'find_leads', lookup)
    terms = ['2014年3月16日的港口会议，port meeting']
    result = history.search_history(terms, LOWER, UPPER, engine='db', storage_root='archive')
    assert calls[0][0] <= date(2014, 3, 16) < calls[0][1] and calls[1][1] == date(2022, 2, 24)
    assert len(calls) == history.WINDOWS_PER_STEP
    assert len(result.diagnostics['windows']) == len(calls)
    assert result.diagnostics['scan']['can_continue']
    second = history.search_history(terms, LOWER, UPPER, engine='db', storage_root='archive',
                                    previous=result.diagnostics)
    assert len(calls) == 2 * history.WINDOWS_PER_STEP
    ordered = sorted(calls)
    assert all(a[1] <= b[0] for a, b in zip(ordered, ordered[1:]))
    assert second.diagnostics['windows'][0]['date_to'] < result.diagnostics['windows'][-1]['date_from']


def test_repair_rechecks_only_requested_range_even_if_cursor_advanced(monkeypatch):
    calls = []
    def find(engine, root, start, end, *args, **kwargs):
        calls.append((start, end))
        return ArchiveLeads([], COVERED)
    monkeypatch.setattr(history, 'find_leads', find)
    def run(**kwargs):
        return history.search_history(['2014 agreement'], LOWER, UPPER, engine='db', storage_root='root', **kwargs).diagnostics
    prior = run()
    assert all(span[0].year == 2014 for span in calls)
    lo = date.fromisoformat(prior['windows'][1]['date_from'])
    repair = {'serial': 7, 'from': lo, 'to': lo}
    calls.clear()
    after = run(previous=prior, repair=repair)
    assert calls[0] == (lo, lo + timedelta(days=1))
    calls.clear()
    run(previous=after, repair=repair)
    assert (lo, lo + timedelta(days=1)) not in calls


def test_node_uses_uncovered_hypothesis_then_resumes_its_saved_scan(program_case, monkeypatch):
    app, client, _ = program_case
    original = ['Port workers may have gone on strike', 'An export ban in 2014 may have cut freight volumes']
    generations = []
    def generate(*args, **kwargs):
        generations.append(1)
        return list(original)
    monkeypatch.setattr(model, 'propose_hypotheses', generate)
    monkeypatch.setattr(retrieval, 'collect_tree_pool', lambda *a, **kw: SimpleNamespace(rows=[],
        local={'status':'covered', 'keyword_dated_count':1, 'keyword_hypothesis_hits':[1,0]}))
    monkeypatch.setattr(history, 'search_history', _REAL_HISTORY)
    calls = []
    def find(engine, storage_root, start, end, terms, cap, *, cutoff):
        assert not any('strike' in key for key in terms)
        calls.append((start, end))
        return ArchiveLeads([], COVERED)
    monkeypatch.setattr(history, 'find_leads', find)
    case = create(client)
    worker = CausalWorker(app.state.engine, storage_root='local-storage')
    assert worker.step()
    root = detail(client, case)['nodes'][0]
    assert root['retrieval']['history']['trigger'] == 'hypothesis_without_evidence'
    assert root['retrieval']['history']['search_hypotheses'] == original[1:]
    assert all(start.year == 2014 for start, end in calls)
    assert client.post(BASE+'/'+case['key']+'/expand', json={'node_key': root['key'], 'continue_history': True}).status_code == 202
    assert CausalWorker(app.state.engine, storage_root='local-storage').step()
    assert len(calls) == len(set(calls)) == 2 * history.WINDOWS_PER_STEP
    assert len(generations) == 1


def test_node_repairs_one_of_multiple_checked_ranges_but_not_the_years_between_them(program_case, monkeypatch):
    app, client, _ = program_case
    monkeypatch.setattr(history, 'search_history', _REAL_HISTORY)
    monkeypatch.setattr(history, 'find_leads', lambda *a, **kw: ArchiveLeads([], {
        'events': {'state':'gap'}, 'gkg': {'state':'gap'}}))
    monkeypatch.setattr(model, 'propose_hypotheses', lambda *a, **kw: ['待查假说：2014年3月16日的港口协议'])
    case = create(client)
    assert CausalWorker(app.state.engine, storage_root='local-storage').step()
    root = detail(client, case)['nodes'][0]
    windows = root['retrieval']['history']['windows']
    assert len(windows) == 3
    assert windows[0]['date_from'].startswith('2014') and windows[1]['date_from'].startswith('2021')
    body = {'case_key':case['key'], 'node_key':root['key']}
    path = '/api/history-archive/request'
    outside = client.post(path, json={**body, 'date_from':'2018-01-01','date_to':'2018-01-02'})
    assert outside.status_code == 422
    dates = {field:windows[1][field] for field in ('date_from','date_to')}
    response = client.post(path, json={**body, **dates})
    assert response.status_code == 202
    assert response.json()['date_from'] == dates['date_from']


def test_continue_rechecks_prior_publisher_evidence_while_fresh_rerun_replaces_it(program_case, monkeypatch):
    from test_causal_program_worker import TARGET

    app, client, fake = program_case
    worker = CausalWorker(app.state.engine, storage_root='local-storage')
    stage = ['first']
    starts = []
    def find(engine, root, start, end, terms, cap, *, cutoff):
        starts.append(start)
        rows = [] if stage[0] == 'empty' else [dict(version=2, source_kind='events',
                url='https://qa.example/reports/'+stage[0])]
        return ArchiveLeads(rows, COVERED)
    def excerpt(url, terms, timeout):
        return dict(url=url, title='Port agreement '+stage[0], published_at='2001-03-20T05:00:00Z',
                    quote='Signed records cited local transport restrictions in the port.',
                    kind='publisher_history')
    monkeypatch.setattr(model, 'propose_hypotheses', lambda settings, target, **kwargs:
                       fake.hypotheses(settings, target, **kwargs) if target == TARGET else [])
    monkeypatch.setattr(history, 'search_history', _REAL_HISTORY)
    monkeypatch.setattr(history, 'find_leads', find)
    monkeypatch.setattr(history, '_publisher_excerpt', excerpt)
    case = create(client)
    path = BASE+'/'+case['key']+'/expand'
    def settle():
        for _ in range(12):
            if not worker.step():
                return detail(client, case)['nodes']
        raise AssertionError('queue did not settle')
    nodes = settle()
    root = nodes[0]
    assert len(nodes) == 2
    ref = root['key']
    for mode, continued, expected in [('empty', True, ['first']), ('second', True, ['first','second']),
                                       ('third', False, ['third'])]:
        stage[0] = mode
        assert client.post(path, json={'node_key':ref,'continue_history':continued}).status_code == 202
        nodes = settle()
        urls = sorted(n['sources'][0]['url'].split('/')[-1] for n in nodes if n['parent_key'] == ref)
        assert urls == expected
        assert nodes[0]['retrieval']['selected_count'] == len(expected)
        assert nodes[0]['retrieval']['history']['previous_candidate_count'] == (1 if continued else 0)
    assert starts[0] == starts[-1]

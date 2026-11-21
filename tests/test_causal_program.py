"""Program tree orchestration over the normal investigation API and queue."""
from sqlmodel import Session

from news2douyin.causal import service
from news2douyin.causal.agent_tools import _quote_dates
from news2douyin.causal.chronology import interval
from news2douyin.causal.models import CausalNode
from test_causal_investigations import BASE, candidate, causal_case, detail
from test_postgres_integration import postgres_url


def test_program_requires_an_anchor_and_earliest_precedes_it(causal_case):
    _app, client, _fake = causal_case
    payload = dict(text='某港口为什么停运', runner='program', date_from='1989-01-01')
    assert client.post(BASE, json=payload).status_code == 422
    response = client.post(BASE, json={**payload, 'event_at': '2022-02-24',
                                       'date_from': '2022-02-24'})
    assert response.status_code == 422
    response = client.post(BASE, json={**payload, 'event_at': '2022-02-24'})
    assert response.status_code == 202, response.text
    assert response.json()['program'] == dict(date_from='1989-01-01', status='pending',
                                              queued=1, completed=0, failed=0)


def test_program_queue_applies_a_single_beam_per_level_across_parents(causal_case):
    app, client, _fake = causal_case
    response = client.post(BASE, json=dict(text='供应中断', event_at='2022-02-24',
                                          date_from='1989-01-01', runner='program'))
    assert response.status_code == 202, response.text
    case = response.json()
    root = service.claim(app.state.engine)
    assert root.program

    def earlier(ref, score, year):
        item = candidate(ref, occurred=f'{year}-06-10', published=f'{year}-06-11')
        return {**item, 'time_status': 'earlier', 'score': score,
                'reason': '查到有原文的候选线索', 'relation': 'possible_cause'}

    service.complete(app.state.engine, root, [earlier(f'first-{i}', 1 - i * .1, 2014)
                                             for i in range(4)], queries=['早期原因'], candidate_count=50)
    assert detail(client, case)['program']['queued'] == 3
    first_level = [service.claim(app.state.engine) for _ in range(3)]
    for index, claim in enumerate(first_level):
        service.complete(app.state.engine, claim, [
            earlier(f'second-{index}-{i}', .9 - .1 * (index + i), 1994) for i in range(2)])
        assert detail(client, case)['program']['queued'] == (2, 1, 3)[index]

    for index in range(3):
        claim = service.claim(app.state.engine)
        with Session(app.state.engine) as session:
            assert session.get(CausalNode, claim.node_key).depth == 2
        results = [earlier('third', .9, 1991)] if index == 0 else []
        service.complete(app.state.engine, claim, results,
                         stop_reason='' if results else '这段时间未找到前因')
    last = service.claim(app.state.engine)
    with Session(app.state.engine) as session:
        assert session.get(CausalNode, last.node_key).depth == 3
    service.complete(app.state.engine, last, stop_reason='这段时间未找到前因')
    assert service.claim(app.state.engine) is None

    tree = detail(client, case)
    assert tree['agent'] is None and tree['program']['status'] == 'completed'
    assert tree['program']['completed'] == 8
    assert tree['nodes'][0]['candidate_count'] == 50
    assert tree['nodes'][0]['queries'] == ['早期原因']
    assert any(node['depth'] == 3 and node['stop_reason'] for node in tree['nodes'])
    assert all(node['relation'] == 'possible_cause' for node in tree['nodes'] if node['depth'])


def test_source_dates_include_day_month_year_but_require_the_year():
    assert interval('2014-02-22') in _quote_dates('On 22 February 2014 the record was signed.')
    assert interval('2014-02-22') in _quote_dates('On February 22, 2014 the record was signed.')
    assert _quote_dates('On 22 February the record was signed.') == []

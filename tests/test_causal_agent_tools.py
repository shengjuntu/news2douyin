import pytest
from sqlmodel import Session, select

from news2douyin.causal import agent_tools as tools
from news2douyin.causal.models import CausalAgentLink, CausalAgentNote, CausalAgentQuery, CausalInvestigation, CausalNode
from news2douyin.research.models import ResearchCase, ResearchRun, ResearchSource
from news2douyin.research.tools import ResearchTools
from news2douyin.storage.utils import dumps, loads


RUN = 'rrun_tool'
CASE = 'ci_tool'
NODE = 'cn_tool'
RESEARCH = 'research_tool'
TARGET = '2022-02-24T12:00:00Z'
EARLIEST = '1991-01-01'


@pytest.fixture
def ready(engine, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE', 'UTC')
    with Session(engine) as session:
        session.add_all([
            ResearchCase(case_id=RESEARCH, title='case', goal='history', strategy_json=dumps({'max_minutes': 20}),
                         active_run_id=RUN, cutoff='2025-01-01T00:00:00Z'),
            ResearchRun(run_id=RUN, case_id=RESEARCH, status='running', cutoff='2025-01-01T00:00:00Z'),
            CausalInvestigation(key=CASE, text='A target investigation', event_at=TARGET,
                                created_at='2025-01-01T00:00:00Z', event_key='evt_target'),
            CausalNode(key=NODE, case_key=CASE, text='The target', event_key='evt_target',
                       ref='evt_target', occurred_at=TARGET, time_status='root'),
            CausalAgentLink(case_key=CASE, research_case_id=RESEARCH, date_from=EARLIEST, current_run_id=RUN),
        ])
        session.commit()
    return engine


def query(engine, keyword='historical negotiation', **overrides):
    return tools.call(engine, 'causal_search_events',
        {**dict(run_id=RUN, node_key=NODE, query=keyword, date_from='1989-01-01', date_to='2024-12-31'), **overrides})


def save(engine, item=None, **overrides):
    return tools.call(engine, 'causal_save_step',
        {**dict(run_id=RUN, node_key=NODE, candidates=[item] if item else []), **overrides})


def evidence(ref, when):
    return dict(ref=ref, event_key=ref, text='Earlier local record', occurred_at='', published_at=when,
                similarity=0.5, sources=[{
                    'url': f'https://example.org/{ref}', 'title': 'Original article',
                    'quote': 'Original lines', 'published_at': when, 'kind': 'local_article',
                }])


def test_local_search_uses_linked_run_parent_clock_and_verified_query_results(ready, monkeypatch):
    received = []

    def retrieve(_engine, queries, **kwargs):
        received.append((queries, kwargs))
        return [evidence('evt_target', '2010-02-01'),
                evidence('evt_1989', '1989-09-09'),
                evidence('evt_2014', '2014-02-03'),
                evidence('evt_later', '2022-02-25'),
                evidence('evt_unknown', '')]

    monkeypatch.setattr(tools, 'retrieve', retrieve)
    with pytest.raises(ValueError, match='没有交集'):
        query(ready, date_from='2023-01-01')
    result = query(ready)
    assert query(ready) == result and len(received) == 1
    assert received[0][1] == dict(lower='1991-01-01T00:00:00', upper='2022-02-24T12:00:00',
                                   exclude={'evt_target'})
    assert [(x['ref'], x['time_status']) for x in result['candidates']] == [
        ('evt_2014', 'earlier'), ('evt_unknown', 'unknown')]
    candidate = dict(search_id=result['search_id'], ref='evt_2014', reason='Provides context', relation='possible_cause')
    with pytest.raises(ValueError, match='候选编号'):
        save(ready, {**candidate, 'ref': 'invented'})
    child = save(ready, candidate, stop_reason='已核对已有候选')['saved'][0]
    assert save(ready, candidate)['saved'] == [child]
    with pytest.raises(ValueError, match='不能由重复提交覆盖'):
        save(ready, {**candidate, 'relation': 'contradicts'})
    for keyword in ('test 2', 'test 3'):
        query(ready, keyword)
    with pytest.raises(ValueError, match='次数已达上限'):
        query(ready, 'test 4')
    with Session(ready) as session:
        row = session.get(CausalNode, child['key'])
        assert result['search_id'] in row.sources_json
        assert session.get(CausalAgentNote, NODE).stop_reason == '已核对已有候选'


def test_external_sources_require_literal_paragraphs_and_flag_retrospective_records(ready, monkeypatch):
    quote = 'On December 21, 1991, the agreement was signed.'
    with Session(ready) as session:
        session.add_all([
            ResearchSource(source_id=key, case_id=case_id, title='Source document', url='https://example.org/'+key,
                           kind='web', content_hash='irrelevant', published_at=when,
                           paragraphs_json=dumps([dict(id='P1', text=quote)]))
            for key, when, case_id in [('src_early', '2010-08-08', RESEARCH),
                                       ('src_retrospective', '2024-08-08', RESEARCH),
                                       ('src_other', '2010-08-08', 'other')]
        ])
        session.commit()
    item = dict(source_id='src_early', paragraph_id='P1', quote=quote, text=quote,
                occurred_at='1991-12-21', reason='A historical document', relation='document_reference')
    for changes, message in [
        ({'source_id': 'src_other'}, '本课题'), ({'quote': 'invented'}, '实际连续文本'),
        ({'occurred_at': '1980-01-01'}, '引文明确记载的日期'),
        ({'source_id': 'src_retrospective', 'relation': 'possible_cause'}, '前置时间范围'),
    ]:
        with pytest.raises(ValueError, match=message):
            save(ready, {**item, **changes})
    normal = save(ready, item)['saved'][0]
    later = save(ready, {**item, 'source_id': 'src_retrospective'})['saved'][0]
    assert normal['time_status'] == 'earlier' and later['time_status'] == 'unknown'
    monkeypatch.setattr(tools, 'retrieve', lambda *_args, **_kwargs: [evidence('evt_next', '1991-12-21')])
    branch = query(ready, node_key=later['key'])
    assert branch['effective_from'] == '1991-01-01T00:00:00Z'
    assert branch['effective_until'] == '1991-12-21T00:00:00Z' and branch['candidates'] == []
    tree = tools.call(ready, 'causal_get_investigation', {'run_id': RUN})
    assert tree['date_from'] == EARLIEST
    assert any(node['key'] == later['key'] and node['relation'] == 'document_reference'
               and node['expandable'] for node in tree['nodes'])


def test_search_releases_database_lock_and_drops_result_after_run_stops(ready, monkeypatch):
    def retrieve(engine, *_args, **_kwargs):
        with Session(engine) as session:
            run = session.get(ResearchRun, RUN)
            run.status = 'paused'
            session.commit()
        return [evidence('evt_late_callback', '2014-01-01')]

    monkeypatch.setattr(tools, 'retrieve', retrieve)
    with pytest.raises(ValueError, match='研究已停止'):
        query(ready)
    with Session(ready) as session:
        row = session.exec(select(CausalAgentQuery)).one()
        assert row.state == 'cancelled' and row.results_json == '[]'
    with pytest.raises(ValueError, match='研究已停止'):
        save(ready, stop_reason='has no active run')


def test_local_excerpts_become_research_claim_evidence_with_original_provenance(ready, monkeypatch):
    first = evidence('earlier-doc', '2014-01-03T08:00:00Z')
    line = 'The written agreement called for a joint commission to monitor compliance.'
    quote = f'Original paragraph heading\n{line}'
    first['sources'][0].update(quote=quote, kind='publisher_preview')
    alias = {**first, 'ref': 'related-event', 'event_key': 'related-event'}
    headline = evidence('headline-only', '2014-01-03')
    headline['sources'][0].update(quote='Title of the report', kind='local_headline')
    monkeypatch.setattr(tools, 'retrieve', lambda *_args, **_kwargs: [first, alias, headline])
    result = query(ready)
    entries = [dict(search_id=result['search_id'], ref=row['ref'], reason='Matches the search', relation='context')
               for row in (first, alias, headline)]
    saved = save(ready, candidates=entries)['saved']
    citation = saved[0]['evidence'][0]
    assert saved[1]['evidence'] == [citation] and saved[2]['evidence'] == []
    assert save(ready, entries[0])['saved'] == saved[:1]
    assert citation['quote'] == line

    report_tools = ResearchTools(ready, None)
    claim = report_tools.call('research_save_claim', dict(run_id=RUN, text='A joint commission was proposed.',
                                                          kind='fact', evidence=[citation]))
    report = report_tools.call('research_save_report', dict(run_id=RUN, title='Historical report', completeness='complete', gaps=[],
                               sections=[dict(heading='Findings', body='An earlier agreement provided for monitoring.',
                                              claim_ids=[claim['claim_id']])]))
    assert report['version'] == 1
    with Session(ready) as session:
        registered = session.exec(select(ResearchSource)).one()
        assert registered.kind == 'causal_excerpt' and registered.source_id == citation['source_id']
        assert loads(registered.provenance_json, {}) == dict(content_kind='excerpt', source_kind='publisher_preview',
            run_id=RUN, search_id=result['search_id'], candidate_ref=first['ref'], published_at='2014-01-03T08:00:00Z')
        assert loads(registered.paragraphs_json, []) == [dict(id='P1', text=line)]
        assert loads(session.get(CausalNode, saved[0]['key']).sources_json, [])[0]['quote'] == quote


def test_local_excerpt_at_research_source_cap_keeps_tree_evidence(ready, monkeypatch):
    with Session(ready) as session:
        session.add_all(ResearchSource(source_id=f'full-{i}', case_id=RESEARCH, title='Existing source',
                                      kind='web', content_hash=str(i), paragraphs_json='[]') for i in range(120))
        session.commit()
    monkeypatch.setattr(tools, 'retrieve', lambda *_args, **_kwargs: [evidence('overflow', '2014-01-03')])
    result = query(ready)
    child = save(ready, dict(search_id=result['search_id'], ref='overflow',
                             reason='Source retained for review', relation='context'))['saved'][0]
    assert child['evidence'] == []
    with Session(ready) as session:
        assert loads(session.get(CausalNode, child['key']).sources_json, [])[0]['quote'] == 'Original lines'
        assert len(session.exec(select(ResearchSource)).all()) == 120


def test_agent_handoff_makes_legacy_tree_quote_citable_and_saves_relation(ready, monkeypatch):
    candidate = evidence('evt_handoff', '2014-01-03')
    retrieved = candidate['sources'][0]
    old_quote = 'A distinct original excerpt from the same publication.'
    with Session(ready) as session:
        session.add(CausalNode(key='cn_existing', case_key=CASE, parent_key=NODE, ref=candidate['ref'],
                               event_key=candidate['event_key'], text='Earlier saved by local model',
                               published_at=candidate['published_at'], depth=1, time_status='earlier',
                               sources_json=dumps([{**retrieved, 'quote': old_quote}, retrieved])))
        session.commit()
    save(ready, node_key='cn_existing', stop_reason='End of prior local branch')
    monkeypatch.setattr(tools, 'retrieve', lambda *_args, **_kwargs: [candidate])
    result = query(ready)
    entry = dict(search_id=result['search_id'], ref=candidate['ref'], reason='Historical reference', relation='context')
    node = save(ready, entry)['saved'][0]
    assert node['key'] == 'cn_existing' and node['relation'] == 'context'
    assert node['evidence'][0]['quote'] == retrieved['quote']
    assert save(ready, entry)['saved'] == [node]
    tree = tools.call(ready, 'causal_get_investigation', {'run_id': RUN})
    recovered = next(row for row in tree['nodes'] if row['key'] == 'cn_existing')
    assert recovered['evidence'] == node['evidence'] and recovered['relation'] == 'context'
    assert recovered['stop_reason'] == 'End of prior local branch'
    with Session(ready) as session:
        sources = loads(session.get(CausalNode, 'cn_existing').sources_json, [])
        assert [item['quote'] for item in sources] == [old_quote, retrieved['quote']]
        assert 'source_id' not in sources[0] and sources[1]['source_id'] == node['evidence'][0]['source_id']

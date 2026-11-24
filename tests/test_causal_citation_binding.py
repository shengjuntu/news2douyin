"""A causal edge takes its citation and time from the same version of one source."""
from types import SimpleNamespace
import json

import pytest

from news2douyin.causal import chronology, history, model, retrieval, service
from news2douyin.causal.runtime import CausalWorker
from news2douyin.history_archive.search import ArchiveLeads
from test_causal_history import URL, TITLE, QUOTE, LOWER, UPPER, _enable, _metadata, _page
from test_causal_model_isolated import SETTINGS, Response, completion, ranked, wire
from test_causal_program_worker import candidate, create, detail, program_case
from test_postgres_integration import postgres_url


def test_worker_binds_decisions_to_the_cited_publication(program_case, monkeypatch, wire):
    app, client, fake = program_case
    quote = 'The sanctions were announced by the minister.'
    mixed = candidate('mixed', kind='local_headline')
    mixed['sources'][0]['quote'] = 'No specific details about the meeting.'
    mixed['sources'].append(dict(mixed['sources'][0], url=URL, quote=quote, published_at='', kind='local_article'))
    verified = candidate('verified')
    source = dict(verified['sources'][0], quote=quote, url=URL, published_at='2018-06-11T10:00:00Z')
    verified['sources'].append(source)
    fake.results = [mixed, verified]
    monkeypatch.setattr(model, 'configure', lambda: SETTINGS)
    monkeypatch.setattr(model, 'rank', actual_rank)
    wire[0].append(Response(completion({'rankings': [ranked(row['ref'], evidence_source='S2',
        reason='该来源记载了制裁声明。', evidence_quote=quote, relation='possible_cause') for row in fake.results]})))
    case = create(client)
    assert CausalWorker(app.state.engine).step()
    nodes = {row['text']: row for row in detail(client, case)['nodes']}
    uncertain = nodes[mixed['text']]
    assert uncertain['time_status'] == 'unknown' and uncertain['relation'] == 'context'
    assert not uncertain['published_at'] and not uncertain['expandable'] and not uncertain['beam_selected']
    assert uncertain['sources'][0]['url'] == URL and uncertain['sources'][0]['cited_quote'] == quote
    early = nodes[verified['text']]
    assert early['published_at'] == source['published_at'] and early['relation'] == 'possible_cause'
    assert early['time_status'] == 'earlier' and early['beam_selected']


# Keep the real protocol function before the program_case fixture replaces its boundary.
actual_rank = model.rank


@pytest.mark.parametrize('explicit', [None, 'S3'])
def test_repeated_quote_requires_the_actual_source_id(explicit, wire):
    pair = candidate('mixed')
    quote = pair['sources'][0]['quote']
    pair['sources'].append(dict(pair['sources'][0], url=URL))
    row = ranked('mixed', evidence_quote=quote)
    if explicit:
        row['evidence_source'] = explicit
    wire[0].append(Response(completion({'rankings': [row]})))
    with pytest.raises(model.CausalModelError, match='逐字'):
        model.rank(SETTINGS, '目标', [pair])


def test_old_mixed_node_cannot_borrow_a_headline_date():
    stamp = '2014-01-01'
    quote = 'The minister announced sanctions.'
    rows = [dict(kind='local_headline', published_at=stamp, quote='Annual meeting'),
            dict(kind='local_article', published_at='', quote=quote)]
    node = SimpleNamespace(time_status='earlier', published_at=stamp, sources_json=json.dumps(rows),
                           reason='候选解释，因果待核：制裁可能作用于供应。\n来源摘录：“'+quote+'”')
    assert not service._program_child(node)
    rows[1]['published_at'] = stamp
    node.sources_json = json.dumps(rows)
    assert service._program_child(node)


@pytest.mark.parametrize('kind', ['meta', 'json-ld', 'microdata'])
def test_post_cutoff_web_update_retained_as_retrospective_source(monkeypatch, kind):
    _enable(monkeypatch)
    later = '2022-03-02T10:15:00Z'
    changed = {'meta': '<meta property="article:modified_time" content="'+later+'">',
               'microdata': '<meta itemprop="dateModified" content="'+later+'">',
               'json-ld': '<script type="application/ld+json">'+json.dumps(
                   {'@type':'NewsArticle', 'headline':TITLE, 'dateModified':later})+'</script>'}[kind]
    html = _page(_metadata('2022-02-15') + changed, 'March 2 update: ' + QUOTE)
    monkeypatch.setattr(history, 'safe_fetch', lambda url, **kw: (url, 'text/html', html))
    monkeypatch.setattr(history, 'find_leads', lambda *args, **kw: ArchiveLeads([dict(url=URL, version=2)], {}))
    result = history.search_history(['talks'], LOWER, UPPER, engine='db', storage_root='storage')
    assert not result.candidates and result.diagnostics['verified_count'] == 0
    item, = result.diagnostics['retrospective_sources']
    assert item['modified_at'] == later and item['published_at'] == '2022-02-15'
    assert item['reason'] == 'updated_after_cutoff' and item['url'] == URL


def test_edition_before_cutoff_keeps_publication_and_revision_clocks(monkeypatch):
    _enable(monkeypatch)
    earlier = '2022-02-18T12:00:00Z'
    html = _page(_metadata('2022-02-15')+'<meta property="article:modified_time" content="'+earlier+'">')
    monkeypatch.setattr(history, 'safe_fetch', lambda url, **kw: (url, 'text/html', html))
    monkeypatch.setattr(history, 'find_leads', lambda *args, **kw: ArchiveLeads([dict(url=URL, version=2)], {}))
    result = history.search_history(['talks'], LOWER, UPPER, engine='db', storage_root='storage')
    row, = result.candidates
    floor, ceiling = history._utc(LOWER), history._utc(UPPER)
    candidate = retrieval._history_candidate(row, floor, ceiling)
    bound = chronology.bind_citation(candidate, dict(evidence_quote=QUOTE, evidence_source='S1',
        relation='possible_cause', score=.6, reason='先前谈判可能相关。'), (ceiling, ceiling), ceiling)
    assert bound['time_status'] == 'earlier' and bound['published_at'] == earlier
    assert bound['sources'][0]['published_at'] == '2022-02-15'

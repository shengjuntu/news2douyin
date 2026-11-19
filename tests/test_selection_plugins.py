import pickle
from dataclasses import replace
from datetime import date, datetime, timezone
from itertools import islice

import pytest

from news2douyin.digest import selection
from news2douyin.digest.selection import Candidate, SelectionPick


DAY = date(2026, 10, 9)
CUTOFF = datetime(2026, 10, 9, 16, tzinfo=timezone.utc)


def report(index):
    return Candidate(key=f'gkg:{index}', title='Several sources share exactly this headline',
                     observed_at='2026-10-09T09:00:00Z', source_url=f'https://paper.example/{index}',
                     source_kind='gkg', evidence_key=f'doc:{index}', excerpt=f'Passage {index}')


class ExampleStrategy:
    api_version = 1
    version = '1.2.3'

    def __init__(self, select):
        self.select = select


class EntryPoint:
    def __init__(self, name, factory):
        self.name = name
        self.factory = factory
        self.loads = 0

    def load(self):
        self.loads += 1
        return self.factory


def install(monkeypatch, *entries):
    def discover(*, group):
        assert group == 'news2douyin.selection'
        return entries
    monkeypatch.setattr(selection.metadata, 'entry_points', discover)


def start(monkeypatch, select, *, limit=100):
    install(monkeypatch, EntryPoint('thirdparty', lambda options: ExampleStrategy(select)))
    return selection.create_selection('thirdparty', limit)


def test_discovery_is_lazy_and_selection_refuses_ambiguous_names(monkeypatch):
    def unused(_options):
        pytest.fail('discovery or ambiguous lookup executed the factory')
    entries = [EntryPoint(name, unused) for name in ('custom', 'custom', 'balanced')]
    install(monkeypatch, *entries)
    names = selection.list_selection_strategies()
    assert names.count('custom') == 1 and 'balanced' in names
    for name in ('custom', 'balanced'):
        with pytest.raises(ValueError, match='ambiguous'):
            selection.create_selection(name)
    with pytest.raises(ValueError, match='unknown'):
        selection.create_selection('other.package:arbitrary_function')
    assert not any(ep.loads for ep in entries)


def test_config_precedence_default_and_detached_options(monkeypatch, tmp_path):
    def factory(options):
        options['labels'].append('factory mutation')
        return ExampleStrategy(lambda rows, ctx: ())
    install(monkeypatch, EntryPoint('custom', factory))
    env_file = tmp_path / 'config.toml'
    env_file.write_text('[selection]\nstrategy="custom"\nlimit=108\n[selection.options]\nlabels=["one"]\n')
    monkeypatch.setenv(selection.CONFIG_ENV, str(env_file))
    plan = selection.load_selection()
    assert (plan.strategy, plan.version, plan.limit, plan.needs_coarse) == ('custom', '1.2.3', 108, False)
    assert plan.options == {'labels': ['one']}

    explicit = tmp_path / 'config-override.toml'
    explicit.write_text('[selection]\nlimit=12\n')
    override = selection.load_selection(explicit)
    assert (override.strategy, override.limit) == ('balanced', 12)
    monkeypatch.delenv(selection.CONFIG_ENV)
    default = selection.load_selection()
    assert (default.strategy, default.limit) == ('balanced', 100)

    original = {'labels': ['two']}
    plan = selection.create_selection('custom', options=original)
    original['labels'].clear()
    first = plan.select([], day=DAY, cutoff=CUTOFF)['stats']['selection']
    assert first == dict(api_version=1, strategy='custom', version='1.2.3', limit=100, options={'labels': ['two']})
    first['options']['labels'].clear()
    assert plan.select([], day=DAY, cutoff=CUTOFF)['stats']['selection']['options'] == {'labels': ['two']}


@pytest.mark.parametrize(('toml', 'message'), [
    ('[other]\ncount=10', 'selection'),
    ('[selection]\nlimti=10', 'unknown'),
    ('[selection]\nlimit="10"', 'positive integer'),
    ('[selection]\nlimit=0', 'positive integer'),
    ('[selection]\nlimit=true', 'positive integer'),
    ('[selection]\noptions="x"', 'table'),
    ('[selection.options]\nkey=inf', 'JSON-compatible'),
    ('[selection.options]\nkey="x"', 'does not accept'),
])
def test_config_validation(monkeypatch, tmp_path, toml, message):
    install(monkeypatch)
    path = tmp_path / 'invalid.toml'
    path.write_text(toml)
    with pytest.raises(ValueError, match=message):
        selection.load_selection(path)


@pytest.mark.parametrize(('field', 'value', 'message'), [
    ('api_version', True, 'API v1'), ('api_version', 2, 'API v1'),
    ('version', '', 'implementation version'), ('select', None, 'implement select'),
    ('needs_coarse', 1, 'boolean'),
])
def test_contract_validation(monkeypatch, field, value, message):
    plugin = ExampleStrategy(lambda rows, ctx: ())
    setattr(plugin, field, value)
    install(monkeypatch, EntryPoint('broken', lambda options: plugin))
    with pytest.raises(ValueError, match=message):
        selection.create_selection('broken')


def test_full_single_pass_stream_is_available_before_grouping_even_above_100(monkeypatch):
    seen = []
    def observations():
        for i in range(109):
            seen.append(i)
            yield report(i)
    def by_reverse_key(rows, context):
        assert seen == []
        assert iter(rows) is rows
        assert (context.day, context.cutoff, context.limit, context.prior_keys) == (
            DAY, CUTOFF, 104, frozenset({'gkg:108'}))
        daily = list(rows)
        assert [r.key for r in daily] == [f'gkg:{i}' for i in range(109)]
        assert len({r.title for r in daily}) == 1
        eligible = (r for r in reversed(daily) if r.key not in context.prior_keys)
        return (SelectionPick(r, score=0.5, reasons=('Reverse order',)) for r in islice(eligible, context.limit))

    plugin = ExampleStrategy(by_reverse_key)
    plugin.needs_coarse = True
    install(monkeypatch, EntryPoint('reverse', lambda options: plugin))
    assert selection.create_selection('reverse').needs_coarse is True
    result = selection.select_top_n(observations(), strategy='reverse', n=104,
                                    day=DAY, cutoff=CUTOFF, prior_keys=['gkg:108'])
    assert [r['key'] for r in result['items']] == [f'gkg:{i}' for i in range(107, 3, -1)]
    assert result['stats']['scanned'] == 109 and result['stats']['selected'] == 104


def test_evidence_card_tracks_raw_sources_and_counts_even_on_prefix_read(monkeypatch):
    representative = replace(report(0), country='MA', channel='diplomacy', actors=('A', 'B'),
                             action_evidence={'passage': 'A proposed a meeting'},
                             reconstruction={'kind': 'official'}, source_clock={'basis': 'seen'})
    duplicate = replace(report(1), source_url=representative.source_url + '#paragraph')
    related = replace(report(2), source_url='https://wire.example/news', source_kind='newsapi')
    def select(rows, context):
        first, second, third = islice(rows, 3)
        return [SelectionPick(first, key='meeting', related=(second, third), score=3, reasons=('corroborated',))]
    result = start(monkeypatch, select).select([representative, duplicate, related, report(3)], day=DAY, cutoff=CUTOFF)
    assert result['stats']['scanned'] == 4 and result['stats']['selected'] == 1
    card, = result['items']
    assert (card['key'], card['source_key'], card['score'], card['reasons']) == ('meeting', 'gkg:0', 3, ['corroborated'])
    assert (card['observations'], card['candidate_documents'], card['domain_count']) == (3, 2, 2)
    assert card['domains'] == ['paper.example', 'wire.example']
    assert card['country'] == 'MA' and card['actors'] == ['A', 'B'] and card['channel'] == 'diplomacy'
    assert card['source_clock'] == representative.source_clock
    assert [(s['source_kind'], s['source_key'], s['evidence_key'], s['url']) for s in card['sources']] == [
        ('gkg', 'gkg:0', 'doc:0', representative.source_url),
        ('newsapi', 'gkg:2', 'doc:2', related.source_url)]
    assert card['sources'][0]['excerpt'] == representative.excerpt
    assert card['sources'][0]['action_evidence'] == representative.action_evidence


@pytest.mark.parametrize(('make_invalid', 'message'), [
    (lambda row: row, 'SelectionPick'),
    (lambda row: SelectionPick(row, key=''), 'key'),
    (lambda row: SelectionPick(row, score=float('nan')), 'finite'),
    (lambda row: SelectionPick(row, reasons='abc'), 'reasons'),
    (lambda row: SelectionPick(replace(row, title=' ')), 'source'),
    (lambda row: SelectionPick(row, related=(replace(row, source_url='file:///tmp/example'),)), 'source'),
])
def test_malformed_output_is_rejected_after_a_valid_pick(monkeypatch, make_invalid, message):
    def picks(rows, ctx):
        yield SelectionPick(next(rows))
        yield make_invalid(next(rows))
    plan = start(monkeypatch, picks)
    with pytest.raises(ValueError, match=message):
        plan.select([report(0), report(1)], day=DAY, cutoff=CUTOFF)


def test_no_partial_results_reused_after_limit_duplicate_or_generator_failure(monkeypatch):
    def picks(rows, ctx):
        first = next(rows)
        yield SelectionPick(first)
        for row in rows:
            if row.key == 'raise':
                raise RuntimeError('source projection failed')
            yield SelectionPick(row)
    plan = start(monkeypatch, picks, limit=2)
    for bad, error, message in [
        ([report(0), report(1), report(2)], ValueError, 'TopN'),
        ([report(0), report(0)], ValueError, 'duplicate'),
        ([report(0), replace(report(1), key='raise')], RuntimeError, 'source projection failed'),
    ]:
        with pytest.raises(error, match=message):
            plan.select(bad, day=DAY, cutoff=CUTOFF)
        clean = plan.select([report(9)], day=DAY, cutoff=CUTOFF)
        assert [c['key'] for c in clean['items']] == ['gkg:9']
        assert clean['stats']['scanned'] == clean['stats']['selected'] == 1


def test_candidate_provenance_survives_disk_roundtrip_and_preserves_order(monkeypatch, tmp_path):
    cache = tmp_path / 'strategy-cache.pickle'
    def select(rows, ctx):
        cache.write_bytes(pickle.dumps(list(rows)))
        first, second = pickle.loads(cache.read_bytes())
        return [SelectionPick(second, key='s' * 160, related=(first,))]
    plan = start(monkeypatch, select)
    card, = plan.select([report(0), report(1)], day=DAY, cutoff=CUTOFF)['items']
    assert card['key'] == 's' * 160 and [s['source_key'] for s in card['sources']] == ['gkg:1', 'gkg:0']


@pytest.mark.parametrize('story_key', ['a' * 161, ' lead', 'trailing ', 'zero\x00byte', 'line\nbreak'])
def test_story_keys_are_suitable_for_storage(monkeypatch, story_key):
    plan = start(monkeypatch, lambda rows, ctx: [SelectionPick(next(rows), key=story_key)])
    with pytest.raises(ValueError):
        plan.select([report(0)], day=DAY, cutoff=CUTOFF)


@pytest.mark.parametrize('change', [
    lambda row: report(99),
    lambda row: replace(row, source_url='https://another.example/fabricated'),
    lambda row: replace(row, title='Changed source headline'),
    lambda row: replace(row, excerpt='Unreported material'),
])
@pytest.mark.parametrize('representative', [True, False])
def test_plugin_cannot_substitute_unobserved_evidence(monkeypatch, change, representative):
    def select(rows, ctx):
        first, second = rows
        altered = change(second)
        return [SelectionPick(altered) if representative else SelectionPick(first, related=(altered,))]
    plan = start(monkeypatch, select)
    with pytest.raises(ValueError):
        plan.select([report(0), report(1)], day=DAY, cutoff=CUTOFF)


def test_same_record_from_an_earlier_run_cannot_be_reused_as_fresh_evidence(monkeypatch):
    retained = []
    def select(rows, ctx):
        first, second = rows
        if retained:
            return [SelectionPick(first, related=(retained[0],))]
        retained.append(second)
        return [SelectionPick(first, related=(second,))]
    plan = start(monkeypatch, select)
    plan.select([report(0), report(1)], day=DAY, cutoff=CUTOFF)
    with pytest.raises(ValueError):
        plan.select([report(0), report(1)], day=DAY, cutoff=CUTOFF)


def test_output_evidence_is_snapshotted_when_an_iterator_yields(monkeypatch):
    source = replace(report(0), action_evidence={'quote': 'Original quote'})

    def select(rows, ctx):
        candidate = next(rows)
        yield SelectionPick(candidate)
        candidate.action_evidence['quote'] = 'Changed after yielding'

    (card,) = start(monkeypatch, select).select([source], day=DAY, cutoff=CUTOFF)['items']
    assert card['action_evidence']['quote'] == 'Original quote'
    assert card['sources'][0]['action_evidence']['quote'] == 'Original quote'

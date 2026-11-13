"""Interest definitions and relevance decisions must not turn intent into fact."""
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from news2douyin.desk import intent


QUESTION = '军援政策发生了哪些实质变化？'
QUOTE = 'The United States denied that military aid to Ukraine was suspended.'


def spec(**changes):
    value = dict(name='对乌政策', kind='长期事件', description='关注美国对乌克兰军援政策',
                 entities=[dict(id='us', name='美国', type='country', aliases=['United States'], core=True),
                           dict(id='ua', name='乌克兰', type='country', aliases=['Ukraine'], core=True)],
                 relations=[dict(subject='us', relation='军援', object='ua', keywords=['military aid'])],
                 keywords=['military aid', '军援'], include=[], exclude=['私人生活'],
                 questions=[QUESTION], time_start='', time_end='', geography=[])
    value.update(changes)
    return intent.validate_spec(value)


def card(text=QUOTE, *, actions=None, revision=3, **changes):
    source = dict(key='a_fixed', revision=revision, title=text, content=text,
                  actions=actions or [], when='2026-11-12T10:00:00Z')
    value = dict(key='e_fixed', title=text, headings=text.casefold(), material=text.casefold(),
                 sources=json.dumps([source]), seen='2026-11-12T10:00:00Z')
    value.update(changes)
    return SimpleNamespace(**value)


def action(*, reverse=False, status='denied', date=''):
    actors = [('Ukraine', 'actor'), ('United States', 'target')] if reverse else [('United States', 'actor'), ('Ukraine', 'target')]
    return dict(id='mention_fixed', article='a_fixed', revision=3, kind='decision', status=status,
                quote=QUOTE, date=date, arguments=[dict(role=role, text=name) for name, role in actors])


def decision(label='core', *, quote=QUOTE, article='a_fixed', revision=3, questions=None):
    return dict(decision=label, reason='报道直接涉及对乌军援政策，否认同样属于专题进展。',
                evidence=[dict(article=article, revision=revision, quote=quote)],
                questions=[QUESTION] if questions is None else questions)


def mock_model(monkeypatch, value):
    monkeypatch.setattr(intent, '_llm_chat', lambda *a, **k: json.dumps(value, ensure_ascii=False))


def test_interpret_model_preserves_original_and_normalizes_query_endpoints(monkeypatch):
    value = spec().model_dump()
    value['description'] = 'model rewrote user description'
    mock_model(monkeypatch, value)
    description = '请持续跟踪美国对乌克兰军援，不要把否认报道排除。'
    result = intent.interpret(description)
    parsed = intent.validate_spec(result['spec'])
    assert result['method'] == 'model'
    assert parsed.description == description
    assert parsed.relations[0].subject == parsed.entities[0].id
    assert parsed.relations[0].object == parsed.entities[1].id
    assert parsed.entities[0].id.startswith('intent_')
    assert '发生' not in parsed.relations[0].relation


def test_fallback_recognizes_bilingual_attention_and_keeps_exclusions_out_of_anchors(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError('secret https://user:password@model.invalid')
    monkeypatch.setattr(intent, '_llm_chat', unavailable)
    description = '持续跟踪特朗普对乌克兰的军援和停火谈判；排除法国娱乐新闻。'
    result = intent.interpret(description)
    assert result['method'] == 'rules' and '模型暂不可用' in result['message']
    assert 'password' not in result['message']
    entities = {row['name'] for row in result['spec']['entities']}
    assert {'特朗普', '乌克兰'} <= entities
    assert '法国' not in entities
    assert result['spec']['exclude'] == ['法国娱乐新闻']
    assert 'military aid' in result['spec']['keywords']
    assert result['spec']['questions']


def test_offline_unknown_object_is_retained_without_inventing_an_identity():
    result = intent.interpret('关注量子电池商业化的进展', use_model=False)
    assert result['method'] == 'rules'
    assert any(row['name'] == '量子电池商业化的进展' and row['type'] == 'unknown' for row in result['spec']['entities'])
    assert result['spec']['description'] == '关注量子电池商业化的进展'


@pytest.mark.parametrize('description', ['', '   ', 'x' * 4001, None])
def test_description_bounds(description):
    with pytest.raises(ValueError):
        intent.interpret(description)


def test_short_named_entity_is_a_valid_topic_description():
    result = intent.interpret('特朗普', use_model=False)
    assert result['spec']['kind'] == '人物'
    assert any(row['name'] == '特朗普' for row in result['spec']['entities'])


def test_invalid_model_graph_falls_back_instead_of_accepting_arbitrary_code(monkeypatch):
    mock_model(monkeypatch, {**spec().model_dump(), 'cypher': 'MATCH (n) DELETE n'})
    result = intent.interpret('跟踪美国对乌克兰军援政策')
    assert result['method'] == 'rules'
    assert 'cypher' not in result['spec']


def test_curated_entity_equivalents_have_stable_ids_and_relations_are_rewritten():
    first = intent.validate_spec(dict(name='人物', entities=[dict(id='raw', name='特朗普', type='person')],
                                     relations=[dict(subject='raw', relation='会见', object='')]))
    second = intent.validate_spec(dict(name='人物', entities=[dict(name='Donald Trump', type='person')]))
    assert first.entities[0].id == second.entities[0].id
    assert first.relations[0].subject == first.entities[0].id


def test_spec_does_not_accept_oversized_unbounded_or_inverted_time_scope():
    with pytest.raises(ValidationError):
        spec(keywords=['test'] * 49)
    with pytest.raises(ValidationError):
        spec(time_start='2026-12-01', time_end='2026-01-01')
    with pytest.raises(ValidationError):
        spec(time_start='2026-02-30')


def test_to_scope_remains_compatible_with_watch_and_research():
    value = intent.to_scope(spec())
    assert value.scope == '美国，乌克兰'
    assert value.question == QUESTION
    assert value.name == '对乌政策'


def test_recall_gives_directional_relation_only_for_grounded_argument_roles():
    forward = intent.recall(spec(), card(actions=[action()]))
    backward = intent.recall(spec(), card(actions=[action(reverse=True)]))
    cooccurrence = intent.recall(spec(), card())
    assert set(forward['channels']) == {'entity', 'topic', 'relation'}
    assert 'relation' not in backward['channels']
    assert 'relation' not in cooccurrence['channels']
    assert any(path.get('status') == 'denied' for path in forward['paths'])


def test_recall_topic_route_does_not_require_old_named_anchor():
    result = intent.recall(spec(), card('New negotiator discusses military aid conditions.'))
    assert result is not None and result['channels'] == ['topic']


def test_recall_exclusion_terms_do_not_act_as_context_free_word_veto():
    result = intent.recall(spec(exclude=['military aid']), card())
    assert result is not None and 'topic' in result['channels']


def test_recall_ascii_alias_requires_token_boundary():
    value = intent.validate_spec(dict(name='人物', entities=[dict(name='特朗普', type='person')]))
    assert intent.recall(value, card('Trumpington museum opens.')) is None
    assert intent.recall(value, card('Trump announces a policy.')) is not None


def test_seen_is_not_used_as_occurrence_time_during_recall():
    value = spec(time_start='2020-01-01', time_end='2020-12-31')
    assert intent.recall(value, card()) is not None


def test_model_can_include_denial_as_relevant_without_claiming_world_truth(monkeypatch):
    mock_model(monkeypatch, decision())
    value = spec()
    result = intent.judge(value, card(actions=[action()]), intent.recall(value, card()))
    assert result['decision'] == 'core' and result['method'] == 'model'
    assert result['evidence'][0]['revision'] == 3
    assert result['questions'] == [QUESTION]
    assert 'truth' not in result


@pytest.mark.parametrize('change', [dict(quote='Invented confirmation never present in article'),
                                   dict(article='another_article'), dict(revision=4), dict(revision=True)])
def test_model_evidence_must_match_exact_article_revision_and_original(monkeypatch, change):
    value = decision()
    value['evidence'][0].update(change)
    mock_model(monkeypatch, value)
    result = intent.judge(spec(), card(), {})
    assert result['decision'] == 'pending' and result['method'] == 'invalid'
    assert result['evidence'] == []


@pytest.mark.parametrize('label', ['core', 'background', 'exclude'])
def test_accept_and_reject_both_require_locatable_evidence(monkeypatch, label):
    mock_model(monkeypatch, {**decision(label), 'evidence': []})
    result = intent.judge(spec(), card(), {})
    assert result['decision'] == 'pending'


def test_model_unavailable_retains_candidate_and_sanitizes_provider_error(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError('request to https://user:secret@private.endpoint failed')
    monkeypatch.setattr(intent, '_llm_chat', unavailable)
    result = intent.judge(spec(), card(), {})
    assert result['decision'] == 'pending' and result['method'] == 'unavailable'
    assert 'secret' not in result['reason'] and 'private.endpoint' not in result['reason']


def test_unknown_question_and_malformed_json_are_deferred(monkeypatch):
    mock_model(monkeypatch, decision(questions=['模型杜撰的问题']))
    assert intent.judge(spec(), card(), {})['decision'] == 'pending'
    monkeypatch.setattr(intent, '_llm_chat', lambda *args, **kwargs: 'not-json')
    assert intent.judge(spec(), card(), {})['decision'] == 'pending'


def test_no_version_or_disabled_model_never_silently_promotes_to_membership(monkeypatch):
    monkeypatch.setattr(intent, '_llm_chat', lambda *args, **kwargs: pytest.fail('should not call model'))
    assert intent.judge(spec(), card(revision=None), {})['decision'] == 'pending'
    assert intent.judge(spec(), card(), {}, use_model=False)['decision'] == 'pending'


def test_pending_model_decision_can_be_saved_without_fabricated_evidence(monkeypatch):
    mock_model(monkeypatch, dict(decision='pending', reason='标题不足以区分同名者。', evidence=[], questions=[]))
    result = intent.judge(spec(), card(), {})
    assert result['decision'] == 'pending' and result['method'] == 'model'


def test_publication_time_alone_cannot_qualify_as_dated_core_event(monkeypatch):
    mock_model(monkeypatch, decision())
    value = spec(time_start='2026-11-01', time_end='2026-11-30')
    assert intent.judge(value, card(), {})['decision'] == 'pending'
    assert intent.judge(value, card(actions=[action(date='2026-11-10')]), {})['decision'] == 'core'
    assert intent.judge(value, card(actions=[action(date='2025-11-10')]), {})['decision'] == 'pending'


def test_actions_from_another_version_cannot_supply_evidence_or_date(monkeypatch):
    original = action(date='2026-11-10')
    original['revision'] = 2
    original['quote'] = 'Only this other version contains this sentence.'
    mock_model(monkeypatch, decision(quote=original['quote']))
    assert intent.judge(spec(), card(actions=[original]), {})['decision'] == 'pending'


def test_unrelated_in_window_action_cannot_qualify_an_outdated_citation(monkeypatch):
    outdated = action(date='2025-11-10')
    unrelated = action(date='2026-11-10')
    unrelated['id'] = 'another_action'
    unrelated['quote'] = 'A different official met a sports delegation in November.'
    text = QUOTE + '\n' + unrelated['quote']
    mock_model(monkeypatch, decision(quote=QUOTE))
    value = spec(time_start='2026-11-01', time_end='2026-11-30')
    assert intent.judge(value, card(text, actions=[outdated, unrelated]), {})['decision'] == 'pending'


def test_multi_action_paragraph_with_ambiguous_dates_stays_pending(monkeypatch):
    original = action(date='2026-11-10')
    second = action(date='2025-11-10')
    second['id'] = 'same_paragraph_other_action'
    mock_model(monkeypatch, decision())
    value = spec(time_start='2026-11-01', time_end='2026-11-30')
    assert intent.judge(value, card(actions=[original, second]), {})['decision'] == 'pending'

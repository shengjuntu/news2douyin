"""Pure current-revision people evidence, not identity/affiliation inference."""
from copy import deepcopy

import pytest

from news2douyin.desk import people_evidence as evidence


CHECKED={'article-a':{'revision':2,'version_id':7}}


def argument(text,type='person',role='actor',**kw):
    return dict(text=text,entity_type=type,role=role,**kw)


def source(quote='Alice met Example Org.',args=None,**updates):
    value=dict(key='article-a',revision=2,version_id=7,event_key='event-a',event_title='An attributed action',
        url='https://fixture.example/report',title='Original report',source_path='/articles/article-a?revision=2',
        published_at='2026-11-20T08:00:00Z',actions=[dict(id='mention-a',article='article-a',revision=2,
            field='content',start=10,end=10+len(quote),quote=quote,kind='meeting',status='reported',decision='pending',
            arguments=args if args is not None else [argument('Alice'),argument('Example Org','organization','participant')])])
    value.update(updates)
    return value


def test_typed_global_identity_curated_aliases_and_ambiguous_names():
    identities=[evidence.identity(name,'person') for name in ('特朗普','川普','Donald Trump')]
    assert {x['key'] for x in identities}=={'person:donald-trump'}
    assert all(not x['provisional'] and x['identity_status']=='curated' for x in identities)
    assert evidence.identity('Ａlice','person')['key']==evidence.identity(' alice ','person')['key']
    person=evidence.identity('Alice','person');org=evidence.identity('Alice','organization')
    assert person['key']!=org['key'] and person['provisional'] and org['provisional']
    assert person['identity_label']==org['identity_label']=='身份待核'
    assert evidence.identity('Donald Trump','organization')['identity_status']=='name_candidate'
    assert evidence.identity('Unknown name','unknown') is None
    assert evidence.identity('Paris','place') is None
    assert evidence.identity('特朗普','unknown')['kind']=='Person'


def test_same_action_links_keep_literal_evidence_version_roles_and_not_affiliation():
    data=evidence.evidence_from_sources([source()],CHECKED)
    assert len(data['entities'])==2 and len(data['actions'])==len(data['relations'])==1
    action=data['actions'][0];relation=data['relations'][0]
    assert action['mention_id']=='mention-a' and relation['mention_id']=='mention-a'
    assert action['revision']==relation['revision']==2 and action['version_id']==relation['version_id']==7
    assert relation['relation']=='joint_action' and relation['relation_basis']=='same_extracted_action'
    assert not relation['formal_affiliation'] and '不表示隶属' in relation['label']
    assert relation['evidence']=='Alice met Example Org.'
    assert action['time_kind']=='published' and action['status']=='reported' and action['review']=='pending'
    assert all(a['participation'] and a['role_link']=='PARTICIPATES_IN' for a in action['arguments'])


def test_target_and_source_are_not_claimed_to_participate_or_work_together():
    record=source('Alice criticised Example Org.',args=[argument('Alice'),argument('Example Org','organization','target')])
    data=evidence.evidence_from_sources([record],CHECKED)
    assert data['relations'][0]['relation']=='action_context'
    arg=next(a for a in data['actions'][0]['arguments'] if a['role']=='target')
    assert not arg['participation'] and arg['role_link']=='TARGET_OF'
    record['actions'][0]['arguments'][1]['role']='source'
    data=evidence.evidence_from_sources([record],CHECKED)
    assert data['actions'][0]['arguments'][1]['role_link']=='MENTIONED_IN'
    assert data['relations'][0]['relation']=='action_context'


@pytest.mark.parametrize(('quote','person','org','relation'),[
    ('Alice is a member of Example Org.','Alice','Example Org','member_of'),
    ('Alice joined Example Org.','Alice','Example Org','member_of'),
    ('Alice works for Example Org.','Alice','Example Org','serves_at'),
    ('Alice represented Example Org at talks.','Alice','Example Org','represents'),
    ('张三是示例组织的成员。','张三','示例组织','member_of'),
    ('张三任职于示例组织。','张三','示例组织','serves_at'),
    ('张三代表示例组织出席会谈。','张三','示例组织','represents'),
])
def test_only_narrow_literal_affiliation_statements_are_classified(quote,person,org,relation):
    record=source(quote,args=[argument(person),argument(org,'organization','participant')])
    row=evidence.evidence_from_sources([record],CHECKED)['relations'][0]
    assert row['relation']==relation and row['relation_basis']=='literal_statement'
    assert row['literal_phrase'] in quote and not row['formal_affiliation']
    assert row['label'].startswith('报道明确')


@pytest.mark.parametrize('quote',[
    'Example Org chairman Alice attended a meeting.',
    'Alice and Example Org appeared in the same report.',
    'It is not true that Alice is a member of Example Org.',
    'Alice is no longer a member of Example Org.',
    'Alice is a former member of Example Org.',
    '有人否认张三是示例组织的成员，Alice met Example Org.',
    '张三并未加入示例组织，Alice met Example Org.',
])
def test_titles_coappearance_and_negation_do_not_become_affiliation(quote):
    record=source(quote,args=[argument('Alice'),argument('Example Org','organization','participant')])
    row=evidence.evidence_from_sources([record],CHECKED)['relations'][0]
    assert row['relation']=='joint_action' and row['relation_basis']=='same_extracted_action'
    assert not row['formal_affiliation']


@pytest.mark.parametrize(('quote','person','org'),[
    ('Alice joined Example Org at a meeting.','Alice','Example Org'),
    ('张三获准加入示例组织。','张三','示例组织'),
])
def test_joining_an_activity_or_permission_to_join_is_not_membership(quote,person,org):
    record=source(quote,args=[argument(person),argument(org,'organization','participant')])
    row=evidence.evidence_from_sources([record],CHECKED)['relations'][0]
    assert row['relation']=='joint_action' and row['relation_basis']=='same_extracted_action'


def test_action_and_evidence_identity_is_independent_of_event_and_scope_grouping():
    one=source();two=source(event_key='event-b')
    first=evidence.evidence_from_sources([one],CHECKED)
    second=evidence.evidence_from_sources([two],CHECKED)
    assert first['actions'][0]['key']==second['actions'][0]['key']
    assert first['actions'][0]['evidence_key']==second['actions'][0]['evidence_key']
    assert {x['key'] for x in first['entities']}=={x['key'] for x in second['entities']}
    combined=evidence.evidence_from_sources([one,two],CHECKED)
    assert len(combined['actions'])==len(combined['relations'])==1
    assert combined['actions'][0]['event_keys']==['event-a','event-b']


def test_stale_and_rejected_and_mismatched_source_revisions_are_excluded():
    for updates in ({'revision':1},{'version_id':8}):
        data=evidence.evidence_from_sources([source(**updates)],CHECKED)
        assert not data['actions'] and data['limits']['stale_sources']==1
    record=source();record['actions'][0]['revision']=1
    assert not evidence.evidence_from_sources([record],CHECKED)['entities']
    record=source();record['actions'][0]['decision']='rejected'
    data=evidence.evidence_from_sources([record],CHECKED)
    assert not data['entities'] and data['limits']['rejected_actions']==1
    assert not evidence.evidence_from_sources([source()],{})['actions']


def test_planned_denied_and_uncertain_status_are_preserved_in_relations():
    for status,label in [('planned','拟议'),('denied','否认'),('uncertain','待核'),('conditional','有条件')]:
        record=source('Alice is a member of Example Org.')
        record['actions'][0]['status']=status
        row=evidence.evidence_from_sources([record],CHECKED)['relations'][0]
        assert row['status']==status and row['label'].startswith(label+'：')
        assert not row['formal_affiliation']


def test_discovery_time_never_becomes_publication_or_occurrence():
    record=source(published_at='',when='2026-11-20T08:00:00Z',time_kind='GDELT 收录')
    row=evidence.evidence_from_sources([record],CHECKED)['actions'][0]
    assert row['time']=='' and row['time_kind']=='unknown'
    record['time_kind']='发布'
    assert evidence.evidence_from_sources([record],CHECKED)['actions'][0]['time_kind']=='published'
    record=source('Alice met Example Org on 2026-11-19.')
    record['actions'][0].update(date='2026-11-19',time='2026-11-19')
    row=evidence.evidence_from_sources([record],CHECKED)['actions'][0]
    assert row['time']=='2026-11-19' and row['time_kind']=='reported_occurrence'
    record['actions'][0].update(date='2026-11-18',time='2026-11-19')
    assert evidence.evidence_from_sources([record],CHECKED)['actions'][0]['time_kind']=='published'


def test_arguments_need_name_grounding_and_cross_passage_coordinates():
    record=source();record['actions'][0]['arguments'][0]['text']='Absent Person'
    assert not evidence.evidence_from_sources([record],CHECKED)['actions']
    record=source('She met Example Org.',args=[argument('Alice',evidence='Alice spoke earlier.'),
        argument('Example Org','organization','participant')])
    assert not evidence.evidence_from_sources([record],CHECKED)['actions']
    record['actions'][0]['arguments'][0].update(field='content',start=100,end=120)
    data=evidence.evidence_from_sources([record],CHECKED)
    assert data['relations'][0]['relation']=='joint_action'
    assert 'Alice spoke earlier.' in data['relations'][0]['argument_evidence']


def test_resolver_is_pure_and_limits_are_explicit(monkeypatch):
    record=source();before=deepcopy(record)
    monkeypatch.setattr(evidence,'MAX_ACTIONS',1)
    other=source('Alice visited Example Org.');other['actions'][0]['id']='mention-b'
    result=evidence.evidence_from_sources([record,other],CHECKED)
    assert len(result['actions'])==1 and result['limits']['truncated']
    assert record==before

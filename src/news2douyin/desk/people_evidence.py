"""Pure, revision-grounded Person/Organization evidence for a Focus read view.

Names group only by an existing curated alias or a normalized *typed* name.
A name group is not verified real-world identity. Shared action evidence is
not an affiliation. Even narrowly matched explicit membership/service phrases
remain attributed source statements, with their original status and revision.
No model, database, graph write or remote read is performed here.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import re
from typing import Mapping

from ..extraction.schema import entity_key, referent, stable_id, normalize_date


MAX_SOURCES = 512
MAX_ACTIONS = 1024
MAX_ENTITIES = 300
MAX_RELATIONS = 3000
MAX_ARGUMENTS = 16
_ACTING = {'actor', 'speaker', 'participant'}
_ROLES = (*sorted(_ACTING), 'target', 'source')
_ORDER = {'actor':0, 'speaker':1, 'participant':2, 'target':3, 'source':4}
_STATUSES = {'reported', 'planned', 'denied', 'conditional', 'uncertain'}
_REJECTED = {'rejected', 'reject', 'exclude'}
_QUALIFIER = re.compile(r'\b(?:not|never|denies|denied|alleged|rumou?r|might|could|whether|if|former)\b|'
                        r'\bno\s+longer\b|\bceased\s+to\s+be\b|\bex[- ]member\b|'
                        r'否认|并非|不是|并未|尚未|从未|不再|不属于|已退出|未曾|没有|辟谣|传闻|据传|可能|是否|假如|如果|拟议|计划', re.I)


def _text(value, maximum=4000):
    return value if isinstance(value, str) and 0 < len(value) <= maximum and '\x00' not in value else ''


def _positive(value):
    return value if type(value) is int and value > 0 else None


def identity(text, entity_type):
    """Return a scope-independent typed identity candidate, or None.

    Only the extraction schema's curated person alias can promote an unknown
    type. An explicitly typed organization never merges with a person name.
    """
    name = _text(text, 160)
    if not name:
        return None
    name = ' '.join(name.split())
    curated = referent('actor', name)
    if entity_type in ('unknown', '', None) and curated.startswith('person:'):
        entity_type = 'person'
    if entity_type not in ('person', 'organization'):
        return None
    known = curated if curated.startswith(entity_type + ':') else ''
    normalized = entity_key(name)
    return dict(key=known or entity_type + ':name:' + stable_id(entity_type, normalized),
        name='Donald Trump' if known == 'person:donald-trump' else name,
        kind='Person' if entity_type=='person' else 'Organization', entity_type=entity_type,
        identity_status='curated' if known else 'name_candidate', provisional=not bool(known),
        identity_label='已整理别名（非独立身份核实）' if known else '身份待核', aliases=[name])


def _version(source, checked_versions):
    article = source.get('article_key') or source.get('key') or source.get('article')
    revision = _positive(source.get('revision'))
    if not isinstance(article, str) or not article or revision is None:
        return None
    current = checked_versions.get(article)
    if isinstance(current, Mapping):
        expected = _positive(current.get('revision'))
        version_id = _positive(current.get('version_id') or current.get('id'))
    else:
        expected, version_id = _positive(current), None
    if expected != revision:
        return None
    supplied = _positive(source.get('version_id'))
    if supplied is not None and version_id is not None and supplied != version_id:
        return None
    return article, revision, version_id or supplied


def time_order(value):
    """Order validated action clocks by instant, treating day-only values as UTC dates."""
    if not value:
        return float('-inf')
    instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.timestamp()


def _clock(action, source):
    raw = _text(action.get('date'), 48)
    quote = action['quote']
    literal_time = _text(action.get('time'), 100)
    published = source.get('published_at') or (source.get('when') if source.get('time_kind')=='发布' else '')
    try:
        day = date.fromisoformat(raw)
        if day.isoformat()==raw and (raw in quote or (literal_time and literal_time in quote
                and normalize_date(literal_time,published)==raw)):
            return raw, 'reported_occurrence'
    except ValueError:
        pass
    if isinstance(published, str) and published:
        try:
            if len(published)==10:
                if date.fromisoformat(published).isoformat()==published:return published, 'published'
            elif datetime.fromisoformat(published.replace('Z','+00:00')).utcoffset() is not None:
                return published, 'published'
        except ValueError:
            pass
    # Discovery/collection times are not supplied as an occurrence or a
    # publisher date. The SQL view may separately expose its scope clock.
    return '', 'unknown'


def _name_pattern(value):
    escaped = re.escape(value)
    if value.isascii():
        return r'(?<![A-Za-z0-9_])' + escaped + r'(?![A-Za-z0-9_])'
    return escaped


def _literal_relation(person, organization, quote):
    """Very narrow explicit phrases; titles/coappearance never imply a post."""
    if _QUALIFIER.search(quote):
        return None
    p, o = _name_pattern(person), _name_pattern(organization)
    patterns = (
        ('member_of', '报道明确成员关系', (
            p+r'\s*(?:是|为)\s*'+o+r'\s*(?:的)?成员',
            p+r'\s*加入\s*'+o+r'(?=$|[\s，。！？；、,.!?;]|后|并)',
            p+r'\s+(?:is|was)\s+(?:a\s+)?member\s+of\s+'+o,
            p+r'\s+(?:joined|joins|has\s+joined)\s+'+o+r'(?=\s*(?:$|[.!?;,]))',
        )),
        ('serves_at', '报道明确任职/服务关系', (
            p+r'\s*(?:任职于|就职于|供职于)\s*'+o+r'(?=$|[\s，。！？；、,.!?;]|并|后)',
            p+r'\s*(?:在|于)\s*'+o+r'\s*(?:担任|任职|出任)',
            p+r'\s+(?:works?|worked|serves?|served)\s+(?:at|for)\s+'+o,
        )),
        ('represents', '报道明确代表该组织', (
            p+r'\s*代表\s*'+o+r'(?=$|[\s，。！？；、,.!?;]|出席|参加|签署|发表|表示|会谈)',
            p+r'\s+(?:represents?|represented)\s+'+o,
            p+r'\s+(?:spoke|signed|acted)\s+on\s+behalf\s+of\s+'+o,
        )),
    )
    for kind,label,expressions in patterns:
        for expression in expressions:
            match = re.search(expression, quote, re.I)
            if match:
                return kind,label,match.group()
    return None


def _label(label, status):
    prefix = {'planned':'拟议：', 'denied':'否认：', 'conditional':'有条件：', 'uncertain':'待核：'}.get(status, '')
    return prefix + label


def evidence_from_sources(sources, checked_versions, *, require_person=True):
    """Resolve checked sources to entities, actions and attributed relations.

    ``checked_versions`` is the SQL view's authoritative current revision map:
    {article_key: revision} or {article_key: {revision, version_id}}. Every action
    must name that same revision. The caller loads TextMention/MentionReview;
    their validated same-version passage offsets remain the evidence basis.
    """
    if not isinstance(sources, (list,tuple)) or not isinstance(checked_versions, Mapping):
        raise ValueError('人物活动来源或当前版本映射无效')
    entities, actions, relations = {}, {}, {}
    without_person = set()
    example_sources = {}
    limits = dict(truncated=len(sources)>MAX_SOURCES, max_sources=MAX_SOURCES,
        max_actions=MAX_ACTIONS,max_entities=MAX_ENTITIES,max_relations=MAX_RELATIONS,
        stale_sources=0,rejected_actions=0,invalid_actions=0,invalid_arguments=0)
    for source in sources[:MAX_SOURCES]:
        if not isinstance(source, dict) or (checked := _version(source, checked_versions)) is None:
            limits['stale_sources']+=1;continue
        article, revision, version_id = checked
        rows = source.get('actions', [])
        if not isinstance(rows, list):
            limits['invalid_actions']+=1;continue
        for raw in rows:
            if len(actions)>=MAX_ACTIONS:
                limits['truncated']=True;break
            if not isinstance(raw, dict):
                limits['invalid_actions']+=1;continue
            review = raw.get('decision',raw.get('review','pending'))
            if not isinstance(review,str):review='pending'
            if review in _REJECTED:
                limits['rejected_actions']+=1;continue
            quote = _text(raw.get('quote'))
            if (not quote or _positive(raw.get('revision'))!=revision
                    or (raw.get('article') and raw['article']!=article)
                    or (_positive(raw.get('version_id')) and version_id and raw['version_id']!=version_id)):
                limits['invalid_actions']+=1;continue
            arguments = raw.get('arguments', [])
            if not isinstance(arguments, list):
                limits['invalid_actions']+=1;continue
            if len(arguments)>MAX_ARGUMENTS:limits['truncated']=True
            prepared=[]
            for argument in arguments[:MAX_ARGUMENTS]:
                if not isinstance(argument,dict) or argument.get('role') not in _ROLES:
                    limits['invalid_arguments']+=1;continue
                text = _text(argument.get('text'),160)
                evidence = _text(argument.get('evidence') or argument.get('quote')) or quote
                if not text or text not in evidence:
                    limits['invalid_arguments']+=1;continue
                if evidence not in quote:
                    start,end = argument.get('start'),argument.get('end')
                    if (argument.get('field') not in ('title','content') or type(start) is not int
                            or type(end) is not int or start<0 or end-start!=len(evidence)):
                        limits['invalid_arguments']+=1;continue
                entity = identity(text, argument.get('entity_type','unknown'))
                if not entity:continue
                role = argument['role']
                prepared.append((entity,dict(entity_key=entity['key'],role=role,text=text,evidence=evidence,
                    field=argument.get('field',raw.get('field','')),start=argument.get('start'),end=argument.get('end'),
                    participation=role in _ACTING,role_link='PARTICIPATES_IN' if role in _ACTING else
                        'TARGET_OF' if role=='target' else 'MENTIONED_IN')))
            if require_person and not any(entity['kind']=='Person' for entity,_ in prepared):
                # A revision may belong to more than one event in this focus.
                mention_id = _text(raw.get('id'), 160)
                without_person.add((article, revision, mention_id or quote))
                if len(example_sources) < 3:
                    example_sources.setdefault((article, revision), dict(article_key=article,
                        revision=revision, mention_id=mention_id, title=_text(source.get('title'), 2000)))
                continue
            included=[]
            for entity,argument in prepared:
                key = entity['key']
                if key not in entities and len(entities)>=MAX_ENTITIES:
                    limits['truncated']=True;continue
                if key not in entities:entities[key]=entity
                elif argument['text'] not in entities[key]['aliases'] and len(entities[key]['aliases'])<24:
                    entities[key]['aliases'].append(argument['text'])
                included.append(argument)
            if not included or require_person and not any(entities[x['entity_key']]['kind']=='Person' for x in included):
                continue
            status = raw.get('status') if isinstance(raw.get('status'),str) and raw['status'] in _STATUSES else 'uncertain'
            event = source.get('event_key', source.get('event',''))
            mention_id = _text(raw.get('id'),160)
            evidence_key = 'people_evidence:'+stable_id(article,revision,mention_id,raw.get('field',''),
                raw.get('start'),raw.get('end'),quote)
            key = 'people_action:'+evidence_key.split(':',1)[1]
            clock,time_kind = _clock({**raw,'quote':quote},source)
            action = dict(key=key,mention_id=mention_id,evidence_key=evidence_key,
                event_key=event,event_title=source.get('event_title',''),event_keys=[event] if event else [],
                article_key=article,revision=revision,version_id=version_id,
                source_url=source.get('url',source.get('source_url','')),source_title=source.get('title',source.get('source_title','')),
                source_path=source.get('source_path',''),kind=raw.get('kind','other'),status=status,
                review=review,quote=quote,evidence=quote,time=clock,time_kind=time_kind,
                field=raw.get('field',''),start=raw.get('start'),end=raw.get('end'),arguments=included)
            prior = actions.setdefault(key,action)
            if event and event not in prior['event_keys']:prior['event_keys'].append(event)
            grouped={}
            for argument in included:grouped.setdefault(argument['entity_key'],[]).append(argument)
            for person,person_args in grouped.items():
                if entities[person]['kind']!='Person':continue
                for organization,org_args in grouped.items():
                    if entities[organization]['kind']!='Organization':continue
                    if len(relations)>=MAX_RELATIONS:
                        limits['truncated']=True;break
                    pa=min(person_args,key=lambda a:_ORDER[a['role']]);oa=min(org_args,key=lambda a:_ORDER[a['role']])
                    explicit=None
                    for p in person_args:
                        for o in org_args:
                            if p['text'] in quote and o['text'] in quote:
                                explicit = _literal_relation(p['text'],o['text'],quote)
                            if explicit:break
                        if explicit:break
                    relation = explicit[0] if explicit else 'joint_action' if pa['participation'] and oa['participation'] else 'action_context'
                    label = explicit[1] if explicit else '报道共同行动（不表示隶属）' if relation=='joint_action' else '同一行动证据中的提及（不表示隶属）'
                    relation_key='people_relation:'+stable_id(key,person,organization,relation)
                    relations.setdefault(relation_key,dict(key=relation_key,person_key=person,organization_key=organization,
                        action_key=key,mention_id=mention_id,evidence_key=evidence_key,
                        relation=relation,label=_label(label,status),role=pa['role'],organization_role=oa['role'],
                        person_roles=sorted({a['role'] for a in person_args}),organization_roles=sorted({a['role'] for a in org_args}),
                        event_key=event,event_title=action['event_title'],article_key=article,revision=revision,version_id=version_id,
                        source_url=action['source_url'],source_title=action['source_title'],source_path=action['source_path'],
                        time=clock,time_kind=time_kind,evidence=quote,argument_evidence=[a['evidence'] for a in (*person_args,*org_args)],
                        field=action['field'],start=action['start'],end=action['end'],
                        literal_phrase=explicit[2] if explicit else '',relation_basis='literal_statement' if explicit else 'same_extracted_action',
                        status=status,review=review,formal_affiliation=False))
    limits['statements_without_person'] = len(without_person)
    return dict(entities=list(entities.values()), actions=list(actions.values()), relations=list(relations.values()),
                limits=limits, examples_without_person=[] if actions else list(example_sources.values()))

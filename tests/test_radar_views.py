"""Radar channels and country links must follow current, attributed role evidence."""
import json
from fastapi.testclient import TestClient
from sqlmodel import Session,select
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article,ArticleEventLink,Event,TaskRecord,CollectedObservation,GdeltDiscovery
from news2douyin.storage.articles import record_version,latest_version
from news2douyin.desk.models import GkgSource,RadarEntry
from news2douyin.desk.service import drain
from news2douyin.extraction.models import TextMention,MentionReview
from news2douyin.extraction.schema import Candidate,Argument,grounded,passages
from news2douyin.knowledge import radar
from datetime import datetime,timezone
from sqlalchemy import event as sql_event


def app_at(tmp_path):
    return create_app(db_url='sqlite:///'+str(tmp_path/'news.db'),storage_root=str(tmp_path/'runs'))


def seed(app,key,*,event=None,provider='worldnewsapi',kind='meeting',status='reported',
         quote='France met Germany in Poland.',args=None):
    event=event or 'e_'+key
    with Session(app.state.engine) as s:
        if not s.exec(select(Event).where(Event.event_key==event)).first():s.add(Event(event_key=event,event_title='Headline '+event))
        article=Article(article_key=key,provider=provider,title='Source '+key,content=quote,
            source_domain='media.example',url='https://media.example/'+key,country='us',published_at='2026-11-03T12:00:00Z')
        s.add(article);s.add(ArticleEventLink(event_key=event,article_key=key));record_version(s,article);s.flush()
        version=latest_version(s,key)
        arg_values=args if args is not None else [('actor','France','place'),('target','Germany','place'),('location','Poland','place')]
        doc=json.loads(version.payload_json);blocks=passages(doc);block=next(b.id for b in blocks.values() if b.field=='content')
        candidate=Candidate(block=block,quote=quote,trigger={'meeting':'met','attack':'attacked','visit':'visited','protest':'protested'}.get(kind,'signed'),
            kind=kind,status=status,arguments=[Argument(role=role,text=text,entity_type=entity) for role,text,entity in arg_values])
        m=grounded(candidate,blocks,key,version.revision,article.published_at)
        s.add(TextMention(id=m['id'],version_id=version.id,article=key,revision=version.revision,data=json.dumps(m)))
        s.commit()
    while drain(app.state.engine):pass
    return m['id']


def test_recorded_collection_paths_keep_media_separate_and_gkg_is_not_doc(tmp_path):
    app=app_at(tmp_path);c=TestClient(app)
    seed(app,'mixed')
    with Session(app.state.engine) as s:
        s.add(GkgSource(article_key='mixed',record_id='20261103120000-1',attributes='{}'))
        s.add(GdeltDiscovery(article_key='mixed',seen_at='2026-11-03T12:00:00Z'))
        s.commit()
    data=c.get('/api/radar/view?period=all&provider=gkg').json()
    assert data['total']==1
    paths={p['provider'] for p in data['network']['nodes'][0]['sources']}
    assert paths=={'worldnewsapi','gkg'}  # GdeltDiscovery was also written by GKG.
    assert c.get('/api/radar/view?period=all&provider=gdelt').json()['total']==0
    with Session(app.state.engine) as s:
        s.add(TaskRecord(task_id='doc-task',profile_name='changed-since',profile_json='{"provider":"gdelt"}',request_hash='snapshot'))
        s.add(CollectedObservation(observation_key='observation',scope_id='doc-task',input_index=0,article_key='mixed',revision=1,disposition='updated'))
        s.commit()
        groups=radar.sources(s,['e_mixed'])['e_mixed']
        assert {g['provider'] for g in groups}=={'worldnewsapi','gkg','gdelt'}
        assert {a['domain'] for g in groups for a in g['articles']}=={'media.example'}
        assert all(a['revision']==1 for g in groups for a in g['articles'])
    assert c.get('/api/radar/view?period=all&provider=gdelt').json()['total']==1
    assert '历史路径可能不完整' in data['scope_note']


def test_channels_follow_current_extraction_instead_of_headline_words(tmp_path):
    app=app_at(tmp_path);c=TestClient(app)
    seed(app,'diplomacy',provider='gkg')
    seed(app,'conflict',provider='gdelt',kind='attack',quote='Russia attacked Ukraine in Poland.',args=[('actor','Russia','place'),('target','Ukraine','place'),('location','Poland','place')])
    seed(app,'protest',provider='mock',kind='protest',quote='France protested in Germany.',args=[('actor','France','place'),('location','Germany','place')])
    dip=c.get('/api/radar/view?channel=diplomacy&period=all').json()
    assert {n['id'] for n in dip['network']['nodes']}=={'e_diplomacy'}
    assert dip['countries']['links'][0]['value']==1
    conflict=c.get('/api/radar/view?channel=conflict&period=all').json()
    assert {n['id'] for n in conflict['network']['nodes']}=={'e_conflict'}
    assert c.get('/api/radar/view?period=all').json()['total']==3
    assert c.get('/api/radar/view?period=all&channel=unknown').status_code==422
    assert c.get('/radar?period=all&view=countries&provider=gdelt&channel=conflict').status_code==200


def test_country_links_exclude_media_locations_persons_denials_and_plans(tmp_path):
    app=app_at(tmp_path);c=TestClient(app)
    seed(app,'normal')
    seed(app,'plan',status='planned')
    seed(app,'denial',status='denied')
    seed(app,'person',quote='Donald Trump met Germany in Poland.',args=[('actor','Donald Trump','person'),('target','Germany','place'),('location','Poland','place')])
    seed(app,'locations',quote='Alice met Bob in France and Germany.',args=[('actor','Alice','person'),('target','Bob','person'),('location','France','place'),('location','Germany','place')])
    countries=c.get('/api/radar/view?period=all').json()['countries']
    assert {n['id'] for n in countries['nodes']}=={'FR','DE'}
    assert len(countries['links'])==1 and countries['links'][0]['value']==1
    assert countries['considered_mentions']==5 and countries['eligible_mentions']==1 and countries['excluded_mentions']==4
    evidence=countries['links'][0]['evidence'][0]
    assert evidence['article']=='normal' and evidence['revision']==1 and evidence['status']=='reported'
    assert evidence['quote']=='France met Germany in Poland.'
    for text in ('Jordan','Chad','Georgia','us'):
        assert radar.country(dict(role='actor',entity_type='person',text=text)) is None
    for text in ('Jordan','Chad','Georgia','us','US','Congo','Korea'):
        assert radar.country(dict(role='actor',entity_type='unknown',text=text)) is None
    assert radar.country(dict(role='location',entity_type='place',text='France')) is None


def test_pair_counts_event_records_once_and_old_revisions_do_not_survive(tmp_path):
    app=app_at(tmp_path);c=TestClient(app)
    first=seed(app,'wire1',event='same')
    second=seed(app,'wire2',event='same',provider='gkg')
    link=c.get('/api/radar/view?period=all').json()['countries']['links'][0]
    assert link['value']==1 and len(link['evidence'])==2
    with Session(app.state.engine) as s:
        s.add(MentionReview(id=first,decision='rejected'))
        article=s.exec(select(Article).where(Article.article_key=='wire2')).one()
        article.content='Corrected unrelated report.';s.add(article);record_version(s,article);s.commit()
    # Channels and country graph use only current versions, even before another model pass.
    assert c.get('/api/radar/view?period=all&channel=diplomacy').json()['total']==0
    assert c.get('/api/radar/view?period=all').json()['countries']['links']==[]


def test_explicit_shared_participants_are_undirected_without_invented_target(tmp_path):
    app=app_at(tmp_path);c=TestClient(app)
    seed(app,'participants',quote='France met Germany in Poland.',args=[('participant','France','place'),('participant','Germany','place'),('location','Poland','place')])
    link=c.get('/api/radar/view?period=all').json()['countries']['links'][0]
    assert not link['directed'] and {link['source'],link['target']}=={'FR','DE'}


def test_country_name_must_be_a_whole_explicit_argument_token():
    assert radar.country(dict(role='actor',entity_type='place',text='Russia',evidence='Russian troops attacked Ukraine.')) is None
    assert radar.country(dict(role='actor',entity_type='place',text='France',evidence='France met Germany.'))=='FR'


def test_unknown_and_other_filters_agree_with_recovered_source_labels(tmp_path):
    app=app_at(tmp_path);c=TestClient(app)
    seed(app,'recovered_gkg',provider='')
    seed(app,'recovered_other',provider='unknown')
    seed(app,'unrecorded',provider='')
    with Session(app.state.engine) as s:
        s.add(GkgSource(article_key='recovered_gkg',record_id='20261103120000-2',attributes='{}'))
        s.add(TaskRecord(task_id='foreign-task',profile_name='old',profile_json='{"provider":"custom_collector"}',request_hash='old'))
        s.add(CollectedObservation(observation_key='custom-observation',scope_id='foreign-task',input_index=0,article_key='recovered_other',revision=1,disposition='updated'))
        s.commit()
    for provider,event in [('gkg','e_recovered_gkg'),('other','e_recovered_other'),('unknown','e_unrecorded')]:
        data=c.get('/api/radar/view?period=all&provider='+provider).json()
        assert {n['id'] for n in data['network']['nodes']}=={event}
        assert [group['provider'] for group in data['network']['nodes'][0]['sources']]==[provider]


def test_directed_and_undirected_same_country_pair_remain_distinct(tmp_path):
    app=app_at(tmp_path);c=TestClient(app)
    seed(app,'directed')
    seed(app,'undirected',args=[('participant','France','place'),('participant','Germany','place'),('location','Poland','place')])
    links=c.get('/api/radar/view?period=all').json()['countries']['links']
    assert len(links)==2
    assert {link['directed'] for link in links}=={True,False}
    assert all(link['kind']=='meeting' and link['value']==1 for link in links)


def test_facets_match_independent_filters_and_each_auto_historical_fallback(tmp_path,monkeypatch):
    class FixedTime(datetime):
        @classmethod
        def now(cls,tz=None):return cls(2026,11,12,12,tzinfo=timezone.utc)
    monkeypatch.setattr(radar,'datetime',FixedTime)
    app=app_at(tmp_path)
    seed(app,'old_diplomacy',provider='gkg')
    seed(app,'recent_conflict',kind='attack',quote='Russia attacked Ukraine in Poland.',args=[('actor','Russia','place'),('target','Ukraine','place')])
    seed(app,'recovered_other',provider='unknown')
    seed(app,'rejected',provider='mock')
    with Session(app.state.engine) as s:
        s.get(RadarEntry,'e_recent_conflict').seen='2026-11-11T12:00:00Z'
        s.get(RadarEntry,'e_rejected').seen='2026-11-11T12:00:00Z'
        rejected=s.exec(select(TextMention).where(TextMention.article=='rejected')).one()
        s.add(MentionReview(id=rejected.id,decision='rejected'))
        s.add(TaskRecord(task_id='custom-task',profile_name='old',profile_json='{"provider":"custom_collector"}',request_hash='custom'))
        s.add(CollectedObservation(observation_key='custom-path',scope_id='custom-task',input_index=0,article_key='recovered_other',revision=1,disposition='kept'));s.commit()
        for period in ('auto','all','1','7','30'):
            for channel,provider in [('all','all'),('diplomacy','all'),('all','gkg'),('conflict','worldnewsapi'),('all','other'),('all','unknown')]:
                filters=dict(period=period,channel=channel,provider=provider)
                result=radar.facets(s,filters)
                main,historical=radar.statement(s,filters)
                assert result['total']==radar.count(s,main)
                assert result['historical']==historical
                for group,field in [(result['channels'],'channel'),(result['providers'],'provider')]:
                    for facet in group:
                        stmt,_=radar.statement(s,dict(filters,**{field:facet['id']}))
                        assert facet['count']==radar.count(s,stmt),(filters,field,facet)
        auto=radar.facets(s,dict(period='auto',channel='all',provider='all'))
        assert auto['total']==2 and not auto['historical']
        # The diplomacy facet independently falls back to old material, even
        # while the current all-channel page has recent conflict/protest data.
        assert next(c for c in auto['channels'] if c['id']=='diplomacy')['count']==2


def test_radar_page_has_one_named_facet_query_and_no_wide_source_or_count_projection(tmp_path):
    app=app_at(tmp_path);seed(app,'display')
    queries=[]
    def capture(_connection,_cursor,statement,_parameters,context,_many):
        queries.append((context.execution_options.get('n2d_query_name'),statement.lower()))
    sql_event.listen(app.state.engine,'before_cursor_execute',capture)
    try:
        with Session(app.state.engine) as s:
            page=radar.page(s,dict(period='all',channel='all',provider='all'))
            assert page['total']==1
            radar.country_view(s,['e_display'],'all')
    finally:sql_event.remove(app.state.engine,'before_cursor_execute',capture)
    assert sum(name=='radar.facets' for name,_ in queries)==1
    assert not any(name=='radar.count_keys' for name,_ in queries)
    sources=next(statement for name,statement in queries if name=='radar.sources')
    assert 'article.content' not in sources and 'article.raw_json' not in sources
    assert 'gkgsource.attributes' not in sources
    facets=next(statement for name,statement in queries if name=='radar.facets')
    assert 'radarentry.material' not in facets and 'radarentry.sources' not in facets
    assert 'group by articleversion.article_key' not in facets
    assert all('group by articleversion.article_key' not in statement for _,statement in queries)


def test_facet_sources_ignore_orphan_metadata_and_observation_paths(tmp_path):
    app=app_at(tmp_path)
    seed(app,'orphan');seed(app,'present')
    with Session(app.state.engine) as s:
        s.add(GkgSource(article_key='orphan',record_id='20261103120000-7',attributes='{}'))
        s.add(TaskRecord(task_id='old-custom',profile_name='old',profile_json='{"provider":"custom_reader"}',request_hash='old'))
        s.add(CollectedObservation(observation_key='orphan-path',scope_id='old-custom',input_index=0,article_key='orphan',revision=1,disposition='kept'))
        article=s.exec(select(Article).where(Article.article_key=='orphan')).one();s.delete(article);s.commit()
        filters=dict(period='all',channel='all',provider='all')
        result=radar.facets(s,filters)
        assert result['total']==2  # Cached event remains until its dirty entry is processed.
        counts={p['id']:p['count'] for p in result['providers']}
        assert counts['worldnewsapi']==1 and counts['gkg']==0 and counts['other']==0
        assert radar.sources(s,['e_orphan'])=={}
        for facet in result['providers']:
            stmt,_=radar.statement(s,dict(filters,provider=facet['id']))
            assert facet['count']==radar.count(s,stmt)


def test_preview_returns_bounded_records_without_exact_statistics(tmp_path):
    app=app_at(tmp_path)
    for key in ('one','two','three'):seed(app,key)
    queries=[]
    def capture(_connection,_cursor,_statement,_parameters,context,_many):
        queries.append(context.execution_options.get('n2d_query_name'))
    sql_event.listen(app.state.engine,'before_cursor_execute',capture)
    try:
        with Session(app.state.engine) as s:
            filters=dict(period='all',channel='all',provider='all')
            first=radar.preview(s,filters,limit=2)
            assert len(first['items'])==2 and first['has_more']
            assert first['total'] is None
            assert all(value is None for value in first['radar_stats'].values())
            assert all(c['count'] is None for c in first['channels']+first['providers'])
            last=radar.preview(s,filters,offset=2,limit=2)
            assert len(last['items'])==1 and not last['has_more']
    finally:sql_event.remove(app.state.engine,'before_cursor_execute',capture)
    assert 'radar.facets' not in queries and 'radar.summary' not in queries
    assert 'radar.count_keys' not in queries

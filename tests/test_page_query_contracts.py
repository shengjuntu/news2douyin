"""Counts project business keys; visible pages keep their original result meaning."""
import json

import pytest
from sqlalchemy import event as sql_event, func, inspect
from sqlmodel import Session, select
from fastapi.testclient import TestClient

from news2douyin.storage.db import make_engine, init_db
from news2douyin.storage.models import Article, ArticleVersion, ArticleEventLink, Event
from news2douyin.storage.articles import record_version, version_page
from news2douyin.search.service import article_page, event_page, _page
from news2douyin.desk.models import Watch, WatchEvent, RadarEntry
from news2douyin.desk.service import focus_entries, memberships, memberships_many
from news2douyin.server.app import create_app
from news2douyin.extraction.models import FocusPoint
from news2douyin.knowledge.models import PointMaterial
from test_postgres_integration import postgres_url


@pytest.fixture(params=['sqlite','postgres'])
def query_engine(request,tmp_path):
    url=request.getfixturevalue('postgres_url') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'query.db')
    engine=make_engine(url);init_db(engine)
    try:yield engine
    finally:engine.dispose()


def observed(engine):
    rows=[]
    def record(conn,cursor,sql,parameters,context,many):
        rows.append((context.execution_options.get('n2d_query_name'),sql.upper()))
    sql_event.listen(engine,'before_cursor_execute',record)
    return rows,lambda:sql_event.remove(engine,'before_cursor_execute',record)


def seed_articles(engine):
    large='原始材料 '*30000
    with Session(engine) as s:
        for n,country in enumerate(('us','fr','us')):
            a=Article(article_key='article_'+str(n),title='Report '+str(n),country=country,
                content=large,raw_json=json.dumps({'large':large}))
            s.add(a);record_version(s,a)
            s.add(Event(event_key='event_'+str(n),event_title='Event '+str(n),summary=large))
        s.add(ArticleEventLink(article_key='article_0',event_key='event_0'))
        s.add(ArticleEventLink(article_key='article_0',event_key='event_1'))
        s.commit()


def test_article_and_event_counts_do_not_project_payload(query_engine):
    seed_articles(query_engine)
    sql,stop=observed(query_engine)
    try:
        with Session(query_engine) as s:
            page=article_page(s,limit=1,offset=1)
            assert page.total==3 and len(page.items)==1 and page.items[0].article_key=='article_1'
            # API-visible articles still contain their original full document.
            assert len(page.items[0].content)>100000
            events=event_page(s,limit=1,offset=2)
            assert events.total==3 and len(events.items)==1
    finally:stop()
    counts={name:statement for name,statement in sql if name and name.endswith('.count')}
    assert {'search.articles.count','search.events.count'}<=counts.keys()
    assert 'ARTICLE.ID' in counts['search.articles.count']
    assert 'CONTENT' not in counts['search.articles.count'] and 'RAW_JSON' not in counts['search.articles.count']
    assert 'EVENT.ID' in counts['search.events.count'] and 'SUMMARY' not in counts['search.events.count']
    assert all('ORDER BY' not in q and 'LIMIT' not in q for q in counts.values())


def test_page_count_preserves_join_distinct_and_group_semantics(query_engine):
    seed_articles(query_engine)
    with Session(query_engine) as s:
        joined=select(Article).join(ArticleEventLink,ArticleEventLink.article_key==Article.article_key)
        raw=_page(s,joined,[Article.id],offset=0,limit=20,indexed=False,key=Article.id)
        assert raw.total==2 and len(raw.items)==2
        unique=_page(s,joined.distinct(),[Article.id],offset=0,limit=20,indexed=False,key=Article.id)
        assert unique.total==1 and len(unique.items)==1
        grouped=select(Article.country,func.count(Article.id)).group_by(Article.country)
        groups=_page(s,grouped,[Article.country],offset=1,limit=1,indexed=False,key=Article.country)
        assert groups.total==2 and groups.items==[('us',2)]


def test_version_page_counts_rows_without_loading_other_payloads(query_engine):
    seed_articles(query_engine)
    with Session(query_engine) as s:
        row=s.exec(select(Article).where(Article.article_key=='article_0')).one()
        row.content='corrected version';s.add(row);record_version(s,row);s.commit()
    sql,stop=observed(query_engine)
    try:
        with Session(query_engine) as s:
            result=version_page(s,'article_0',offset=1,limit=1)
            assert result['total']==2 and len(result['items'])==1
            assert result['items'][0]['revision']==1 and len(result['items'][0]['document']['content'])>100000
            loaded=[row for row in s.identity_map.values() if isinstance(row,ArticleVersion)]
            assert len(loaded)<=1
    finally:stop()
    count=[q for name,q in sql if name=='articles.versions.count']
    assert len(count)==1 and 'PAYLOAD_JSON' not in count[0] and 'COUNT(' in count[0]


def test_focus_list_fetches_only_displayed_fields(query_engine):
    large='large source payload '*15000
    with Session(query_engine) as s:
        s.add(Watch(key='watch',name='Topic',kind='人物',scope='Alice'))
        for n in range(3):
            key='e'+str(n)
            s.add(RadarEntry(key=key,title='Title '+str(n),seen='2026-11-0'+str(n+1),priority=1,
                band='其他进展',reason='ranking explanation',evidence='two reports',headings=large,material=large,sources=large,geography=large))
            s.add(WatchEvent(watch='watch',event=key,choice='exclude' if n==2 else 'auto',reason='because '+str(n),
                relevance=n,branch='A' if n==0 else 'B'))
        s.commit()
    sql,stop=observed(query_engine)
    try:
        with Session(query_engine) as s:
            total,items=focus_entries(s,'watch',limit=1,order='relevance')
            assert total==2 and len(items)==1 and items[0][0].key=='e1'
            card,reason=items[0]
            assert reason=='because 1' and card.title=='Title 1' and card.evidence=='two reports'
            assert {'sources','material','headings','geography'}<=inspect(card).unloaded
            assert memberships(s,'e1')==[dict(key='watch',name='Topic',reason='because 1')]
            assert focus_entries(s,'watch',branch='A')[0]==1
    finally:stop()
    for name,statement in sql:
        if name in ('desk.focus_entries.count','desk.focus_entries.page'):
            assert 'RADARENTRY.SOURCES' not in statement and 'RADARENTRY.MATERIAL' not in statement
            assert 'RADARENTRY.HEADINGS' not in statement and 'RADARENTRY.GEOGRAPHY' not in statement
        if name=='desk.event_memberships':
            assert 'WATCH.SCOPE' not in statement and 'WATCH.DIMENSIONS' not in statement


def seed_radar(engine):
    with Session(engine) as s:
        for key,name in [('w_alpha','Alpha'),('w_bravo','Bravo'),('w_excluded','Excluded')]:
            s.add(Watch(key=key,name=name,kind='人物',scope='Alice'))
        for n in range(30):
            key='visible_'+str(n)
            s.add(Event(event_key=key,event_title='Visible '+str(n)))
            s.add(RadarEntry(key=key,title='Visible '+str(n),seen='2026-11-12T00:00:00Z',priority=1,
                band='其他进展',reason='explanation',evidence='source count',
                headings='large '*5000,material='large '*5000,sources='[]',geography='[]'))
            s.add(WatchEvent(watch='w_alpha',event=key,reason='Alpha reason'))
            if n%2:s.add(WatchEvent(watch='w_bravo',event=key,choice='include',reason='Bravo reason'))
            s.add(WatchEvent(watch='w_excluded',event=key,choice='exclude',reason='hidden'))
        s.add(FocusPoint(key='point',watch='w_alpha',parent='w_alpha',text='A question',created='2026-11-12T00:00:00Z'))
        for n in range(30):
            s.add(PointMaterial(key='material_'+str(n),point='point',event='visible_'+str(n),reason='material hint',generation='fixture'))
        s.commit()


def test_memberships_many_matches_single_result_and_empty_keys(query_engine):
    seed_radar(query_engine)
    with Session(query_engine) as s:
        rows=memberships_many(s,['visible_0','visible_1','missing','visible_0'])
        assert rows['visible_0']==[dict(key='w_alpha',name='Alpha',reason='Alpha reason')]
        assert rows['visible_1']==[dict(key='w_alpha',name='Alpha',reason='Alpha reason'),
                                   dict(key='w_bravo',name='Bravo',reason='Bravo reason')]
        assert rows['missing']==[]
        assert all(rows[key]==memberships(s,key) for key in rows)
        sql,stop=observed(query_engine)
        try:assert memberships_many(s,[])=={}
        finally:stop()
        assert not sql


def test_radar_page_membership_queries_stay_constant_for_1_and_30(query_engine,tmp_path):
    seed_radar(query_engine)
    app=create_app(db_url=query_engine.url.render_as_string(hide_password=False),storage_root=str(tmp_path/'runs'))
    client=TestClient(app)
    try:
        for size in (1,30):
            sql,stop=observed(app.state.engine)
            try:response=client.get('/radar',params={'period':'all','limit':size})
            finally:stop()
            assert response.status_code==200,response.text
            assert response.text.count('data-radar-event=')==size
            queries=[q for name,q in sql if name=='desk.event_memberships']
            assert len(queries)==1
            assert 'WATCH.SCOPE' not in queries[0] and 'WATCH.QUESTION' not in queries[0]
            assert 'RADARENTRY' not in queries[0]
            assert 'Alpha' in response.text
        sql,stop=observed(app.state.engine)
        try:response=client.get('/focus/w_alpha?view=archive')
        finally:stop()
        assert response.status_code==200,response.text
        assert response.text.count('material hint')==30
        queries=[q for name,q in sql if name=='desk.focus_point_materials']
        assert len(queries)==1
        assert 'RADARENTRY.MATERIAL' not in queries[0] and 'RADARENTRY.SOURCES' not in queries[0]
    finally:
        client.close();app.state.engine.dispose()

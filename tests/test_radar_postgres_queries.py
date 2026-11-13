"""Cross-check grouped radar facets on real PostgreSQL, not only SQLite."""
from datetime import datetime,timezone
from sqlmodel import Session,select
from test_postgres_integration import pg_app,postgres_url
from test_radar_views import seed
from news2douyin.desk.models import RadarEntry,GkgSource
from news2douyin.extraction.models import TextMention,MentionReview
from news2douyin.storage.models import Article,TaskRecord,CollectedObservation
from news2douyin.knowledge import radar


def test_pg_facet_aggregate_matches_filters_and_bounded_sources(pg_app,monkeypatch):
    class FixedTime(datetime):
        @classmethod
        def now(cls,tz=None):return cls(2026,11,12,12,tzinfo=timezone.utc)
    monkeypatch.setattr(radar,'datetime',FixedTime)
    app,_,_=pg_app
    seed(app,'old',provider='gkg')
    seed(app,'recent',kind='attack',quote='Russia attacked Ukraine in Poland.',args=[('actor','Russia','place'),('target','Ukraine','place')])
    seed(app,'recovered',provider='unknown')
    seed(app,'rejected',provider='mock')
    with Session(app.state.engine) as s:
        s.get(RadarEntry,'e_recent').seen='2026-11-11T12:00:00Z'
        s.get(RadarEntry,'e_rejected').seen='2026-11-11T12:00:00Z'
        mention=s.exec(select(TextMention).where(TextMention.article=='rejected')).one()
        s.add(MentionReview(id=mention.id,decision='rejected'))
        s.add(TaskRecord(task_id='custom',profile_name='saved',profile_json='{"provider":"custom_reader"}',request_hash='saved'))
        s.add(CollectedObservation(observation_key='custom',scope_id='custom',input_index=0,article_key='recovered',revision=1,disposition='kept'));s.commit()
        for period in ('auto','all','1','7','30'):
            filters=dict(period=period,channel='all',provider='all')
            data=radar.facets(s,filters)
            for group,field in [(data['channels'],'channel'),(data['providers'],'provider')]:
                for facet in group:
                    stmt,_=radar.statement(s,dict(filters,**{field:facet['id']}))
                    assert facet['count']==radar.count(s,stmt),(period,field,facet)
        page=radar.page(s,dict(period='all',channel='all',provider='gkg'))
        assert page['total']==1
        assert page['sources_by_event']['e_old'][0]['articles'][0]['revision']==1
        assert radar.country_view(s,['e_recent'],'conflict')['links'][0]['value']==1

from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import event
from sqlmodel import Session

from news2douyin.digest import actor_context
from news2douyin.digest.why_follow import describe
from news2douyin.intake.official_models import OfficialEvent

DAY = date(2026, 11, 15)
CUTOFF = datetime(2026, 11, 15, 6, tzinfo=timezone.utc)
HIST = '2026-11-12T12:00:00Z'
NOW = '2026-11-15T04:00:00Z'


def official(key, when=HIST, name='Minister B', **more):
    data = dict(event_id=str(key), feed='events_english', stamp='20261112120000', row_index=1,
                event_date=when[:10], added_at=when, last_seen=when, ingested_at=when, actor1_name='Minister A',
                actor1_country='AAA', actor2_name=name, action_place='City X', action_country_iso2='AA',
                event_code='035', source_url=f'https://source.example/report/{key}')
    return OfficialEvent(**dict(data, **more))


def card(row):
    return dict(source_key='gdelt:'+row.event_id, source_kind='gdelt',
                source_clock=dict(clock_kind='official', local_received_at=row.ingested_at, upstream_last=row.last_seen,
                                  event_ref=[row.feed,row.stamp,row.row_index]))


def test_comparison_freezes_only_exact_role_code_location_and_visible_versions(engine):
    with Session(engine) as session:
        prior = official(1)
        current = [official(200,NOW), official(201,NOW,'Minister C'), official(202,NOW,action_place='City Y'),
                   official(203,NOW,actor1_name=''), official(204,NOW,action_country_iso2='BB')]
        for row in [prior, *current, official(2, name='Minister C', event_code='034'),
                    official(3, name='Minister C', actor1_name='Different'), official(4,name='Minister C',action_place='City Z'),
                    official(5,name='Minister C', ingested_at=NOW), official(6,name='Minister C', ingested_at=''),
                    official(7,name='Minister C', actor1_country='FFF'), official(8,name='Minister C', added_at='2026-10-01T00:00:00Z')]:
            session.add(row)
        session.commit()
        cards = [card(r) for r in current]
        stale = card(current[0])
        stale['source_clock']['event_ref'][2] += 1
        gkg = dict(source_kind='gkg',source_key='gkg:other')
        sql = []
        def track(conn,cursor,statement,*args):
            sql.append(statement)
        event.listen(engine,'before_cursor_execute',track)
        try:
            actor_context.attach_actor_context([*cards,stale,gkg],session,DAY,CUTOFF)
        finally:
            event.remove(engine,'before_cursor_execute',track)
        assert len(sql) == 2 and all('officialevent' in q.lower() for q in sql)
        assert 'union all' in sql[1].lower() and 'limit' in sql[1].lower()
        seen, unseen, empty, incomplete, other_country = [x['actor_context'] for x in cards]
        assert (seen['status'], unseen['status']) == ('seen','not_seen_in_sample')
        assert seen['comparable_count'] == unseen['comparable_count'] == 1
        assert seen['sample_count'] == 5 and unseen['truncated'] is False
        assert (seen['window_start'],seen['window_end']) == ('2026-11-08','2026-11-14')
        assert unseen['example']['event_id'] == prior.event_id
        assert parse_qs(urlsplit(unseen['example']['detail_url']).query) == {
            'event': ['1'],'source':['gdelt'],'evidence_day':['2026-11-14']}
        assert all(x['status'] == 'unknown' for x in (empty, incomplete, other_country, stale['actor_context']))
        assert other_country['sample_count'] == 0 and 'actor_context' not in gkg
        assert describe(cards[1],{})['change']['context'] == unseen
        assert describe(dict(source_kind='gdelt'),{})['change']['context'] is None


def test_limited_country_sample_and_exclusive_midnight_boundary(engine, monkeypatch):
    monkeypatch.setattr(actor_context,'PER_COUNTRY',2)
    with Session(engine) as session:
        current=official(200,NOW,'Minister C')
        session.add_all([current, official(9,HIST,'Minister C'),
                         official(10,'2026-11-13T00:15:00Z'), official(11,'2026-11-14T15:59:59Z'),
                         official(12,'2026-11-14T16:00:00Z','Minister C')])
        session.commit()
        item = card(current)
        actor_context.attach_actor_context([item],session,DAY,CUTOFF)
        detail = item['actor_context']
        assert detail['status']=='not_seen_in_sample' and detail['truncated']
        assert detail['comparable_count']==detail['sample_count']==2
        assert detail['example']['event_id']=='11' and '仅在这组样本中' in detail['summary']

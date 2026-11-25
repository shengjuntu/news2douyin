from datetime import datetime, timedelta, timezone

from sqlmodel import Session, create_engine

from news2douyin.candidates.service import record_reports
from news2douyin.candidates.models import SourceReport
from news2douyin.world_radar.market import classify, leads


def test_market_hypothesis_requires_a_visible_action_or_a_specific_logistics_lead():
    match = classify('Chile announces an export tariff on copper concentrate effective next month')
    assert match['state'] == 'hypothesis'
    assert (match['category'], match['product_id']) == ('policy', 'copper')
    assert match['evidence'].startswith('Chile announces')

    match = classify('铜矿临时停产，等待监管检查')
    assert (match['category'], match['product_id']) == ('industry', 'copper')
    match = classify('天然气库存下降幅度超过预期')
    assert (match['category'], match['product_id']) == ('supply', 'natural-gas')

    match = classify('Kerch Bridge critically damaged and closed to traffic')
    assert match['category'] == 'transport'
    assert match['products'] == []
    assert '承运货物' in match['next_check']
    assert '地区价差' not in match['indicators']

    assert classify('Copper price higher after the close') is None
    assert classify('Officials deny reports that copper output will halt') is None
    assert classify('G20 summit opens in Brazil') is None
    assert classify('Latest market headlines', 'Copper jumps. Oilfield production suspended.') is None
    assert classify('铜陵工厂关闭') is None


def test_market_recall_uses_the_prefilter_and_keeps_location_uncertain(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "radar.sqlite"}')
    SourceReport.__table__.create(engine)
    start = datetime(2026, 11, 23, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    in_window = (start + timedelta(minutes=15)).isoformat()
    with Session(engine) as session:
        sources = [
            dict(title='Chile places a copper export ban', url='https://ministry.test/copper',
                 country='us', published_at=in_window),
            dict(title='Chile places a copper export ban', url='https://wire.test/copper',
                 country='cl', published_at=in_window),
            dict(title='Strong copper prices boost stock valuations', url='https://price.test/copper'),
            dict(title='Flood shuts port operations', url='https://operator.test/port'),
            dict(title='Natural gas inventory falls', url='https://old.test/gas',
                 published_at=(start - timedelta(days=1)).isoformat()),
        ]
        record_reports(session, sources, provider='worldnewsapi', batch='now', observed_at=in_window)
        record_reports(session, [dict(title='LNG export ban', url='https://future.test/gas')],
                       provider='worldnewsapi', batch='next', observed_at=end.isoformat())
        session.commit()

        result = leads(session, [], start=start, end=end)
        assert [card['market']['category'] for card in result['items']] == ['policy', 'transport']
        policy = result['items'][0]
        assert policy['candidate_key'].startswith('report:')
        assert policy['event_path'].startswith('/organize?candidate=')
        assert policy['country'] == '' and policy['precision'] == 'unknown'
        assert policy['latitude'] is None and policy['longitude'] is None
        assert policy['source_url'].endswith('/copper')

        assert not leads(session, [], start=start, end=end, country='cl')['items']
        assert len(leads(session, [], start=start, end=end, category='transport')['items']) == 1
        assert len(leads(session, [], start=start, end=end, category='disaster')['items']) == 1

        event = dict(policy, key='event-existing', event_key='event-existing', candidate_key=None,
                     country='cl', source_kind='local')
        result = leads(session, [event], start=start, end=end)
        assert result['items'][0]['event_key'] == 'event-existing'
        assert len(result['items']) == 2
        narrow = leads(session, [event], start=start, end=end, country='cl')
        assert [item['event_key'] for item in narrow['items']] == ['event-existing']

        later = end + timedelta(minutes=15)
        record_reports(session, [
            dict(title=sources[0]['title'], url=sources[0]['url'], published_at=later.isoformat()),
        ], provider='worldnewsapi', batch='distinct-day', observed_at=later.isoformat())
        session.commit()
        held = leads(session, [], start=start, end=end + timedelta(days=1), category='policy')
        repeats = [item for item in held['items'] if item['title'] == sources[0]['title']]
        assert {item['published_at'][:10] for item in repeats} == {'2026-11-23', '2026-11-24'}

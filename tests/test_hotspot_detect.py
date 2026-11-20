from datetime import datetime, timedelta, timezone

from news2douyin.hotspots.detect import CATEGORIES, detect_hotspots


NOW = datetime(2026, 11, 20, 3, tzinfo=timezone.utc)


def report(title, host, path='one', *, hours=2, **fields):
    return dict(title=title, source_url=f'https://{host}/{path}', observed_at=NOW - timedelta(hours=hours),
                source_kind='report', source_key=f'report:{host}/{path}', **fields)


def test_discovers_supported_topics_without_merging_an_entire_category():
    records = [
        report('日本北海道发生7.2级地震，多地出现强烈震感', 'geo1.test'),
        report('Hokkaido hit by magnitude 7.3 earthquake and evacuation alert', 'geo2.test'),
        report('秘鲁发生6.4级地震，部分地区有震感', 'geo3.test'),
        report('四川宜宾发生6.3级地震，启动紧急疏散', 'geo4.test'),
        report('俄乌战事升级，双方公布最新战况', 'war1.test'),
        report('俄罗斯与乌克兰交火，多个地区受影响', 'war2.test'),
        report('Russia shells Ukraine as delegations discuss risks', 'war3.test'),
        report('乌克兰战争前线局势受到关注', 'war-old.test', hours=45),
        report('APEC leaders summit opens in Seoul', 'meeting1.test'),
        report('APEC峰会在首尔开幕，各国领导人讨论贸易合作', 'meeting2.test'),
        report('西班牙全国大规模停电，多地铁路服务中断', 'energy1.test'),
        report('西班牙多地停电，全国多城公共交通受影响', 'energy2.test'),
    ]
    result = detect_hotspots(records, as_of=NOW)
    topics = {item['title']: item for item in result}
    assert {row['category'] for row in result} == CATEGORIES.keys()
    assert topics['北海道地震']['recent_reports'] == 2
    assert topics['北海道地震']['focus_terms'][:2] == ['北海道地震', 'Hokkaido earthquake']
    assert topics['北海道地震']['location'] == '北海道'
    assert topics['俄乌战争']['status'] == 'rising'
    assert topics['APEC重要会晤']['confidence'] == 'multi_source'
    assert topics['西班牙停电影响']['domains'] == 2
    assert topics['秘鲁地震']['status_label'] == '单源待核'
    assert topics['四川地震']['confidence'] == 'single_source'
    assert result == detect_hotspots(reversed(records), as_of=NOW)


def test_rise_continuity_and_evidence_provenance_use_distinct_reports():
    old_url_title = '俄乌战争前线局势与当前外交进展'
    old = report(old_url_title, 'one.test', hours=47)
    reused = dict(old, observed_at=NOW - timedelta(hours=1), source_url=old['source_url'] + '?utm_source=feed')
    today1 = report('Russia and Ukraine exchange fire after ceasefire talks', 'one.test', 'new')
    today2 = report('俄军炮击乌克兰，多地受影响', 'two.test', excerpt='报道摘录。', provider='worldnewsapi')
    official = report('俄罗斯与乌克兰冲突', 'codes.test', event_key='gdelt:7', official_only=True,
                      event_root_code='19', actor_countries=['RU', 'UA'])
    attention = report('俄乌战争出现新消息', 'trends.test', attention_only=True)
    result = detect_hotspots([old, reused, today1, today2, official, attention], as_of=NOW)
    topic, = result
    assert topic['status'] == 'ongoing'
    assert (topic['recent_reports'], topic['previous_reports'], topic['domains']) == (2, 1, 2)
    evidence = {row['url']: row for row in topic['sources']}
    assert evidence[today2['source_url']]['provider'] == 'worldnewsapi'
    assert evidence[today2['source_url']]['excerpt'] == '报道摘录。'
    assert evidence[official['source_url']]['official_only']
    assert evidence[attention['source_url']]['attention_only']
    assert reused['source_url'] not in evidence
    assert not detect_hotspots([today1, official, attention], as_of=NOW)


def test_retry_or_late_ingestion_of_historical_material_does_not_gloss_over_missing_evidence():
    rejected = [
        report('两伊战争爆发46周年纪念', 'history1.test'),
        report('两伊战争爆发第46周年，新书访谈', 'history2.test'),
        report('1980年两伊战争历史回顾', 'history3.test'),
        report('日本北海道7.5级地震演练通知', 'drill.test'),
        report('中国与美国贸易战再次上热搜', 'business.test'),
        report('某公司高管计划举行普通会议', 'offtopic.test'),
        report('日本东京单一住户停电', 'noise.test'),
        report('日本北海道发生7.2级地震', 'late.test', published_at=(NOW - timedelta(days=5))),
        report('日本北海道发生7.2级地震', 'future.test', hours=-1),
        dict(report('日本北海道发生7.2级地震', 'bad.test'), source_url='javascript:alert(1)'),
        report('涉及地震的电影将在全球上映', 'film.test'),
    ]
    assert detect_hotspots(rejected, as_of=NOW) == []


def test_distinguishes_local_episodes_and_same_network_reprints():
    records = [
        report('日本北海道发生7.2级地震', 'nbcbayarea.com', 'h1'),
        report('北海道发生7.2级地震，多地震感强烈', 'nbcboston.com', 'h2'),
        report('北海道发生6.0级地震，日本当地报告余震', 'nature.test', 'h3'),
        report('日本冲绳发生6.5级地震，多地监测震感', 'nature.test', 'h4'),
    ]
    topics = detect_hotspots(records, as_of=NOW)
    assert len(topics) == 3
    assert len({item['id'] for item in topics}) == 3
    assert all(topic['confidence'] == 'single_source' for topic in topics)
    linked = [report('北海道地震后学校临时关闭', 'publisher1.test', event_key='local:quake', local_event_key='quake'),
              report('日本北海道地震引发紧急疏散', 'publisher2.test', event_key='local:quake')]
    topic, = detect_hotspots(linked, as_of=NOW)
    assert topic['confidence'] == 'multi_source'
    assert {row['local_event_key'] for row in topic['sources']} == {'', 'quake'}


def test_category_cap_preserves_lower_ranked_eligible_disaster():
    rows = [report(title, f'domain{host}.test', str(index))
            for host in range(3) for index, title in enumerate((
                '俄乌战事进展后双方交换声明', '印度与巴基斯坦交火，邻近地区疏散'))]
    rows.append(report('北海道发生7.1级地震', 'nature.test'))
    assert all(topic['category'] == 'conflict' for topic in detect_hotspots(rows, as_of=NOW, limit=2))
    shown = detect_hotspots(rows, as_of=NOW, limit=2, per_category=1)
    assert [topic['category'] for topic in shown] == ['conflict', 'disaster']

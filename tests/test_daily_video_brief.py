import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from news2douyin.digest.brief import render_brief, select_brief
from news2douyin.digest.models import DailyDigestIssue
from news2douyin.server.daily_brief import register
from news2douyin.storage.db import create_schema, make_engine
from news2douyin.translations.daily import daily_card_descriptor


DAY = '2025-03-15'


def card(title, index, *, url='', excerpt='', category='', **kwargs):
    url = url or f'https://paper{index}.example/report'
    result = dict(key=f'card-{index}', title=title, excerpt=excerpt, source_key='local:'+str(index),
                  source_url=url, source_kind='local', channel=category, impact=0.6,
                  domains=[f'paper{index}.example'], domain_count=1, observations=1,
                  sources=[dict(url=url, title=title, source_key='local:'+str(index), source_kind='local')],
                  observed_at='2025-03-15T01:00:00Z')
    return dict(result, **kwargs)


def edition(cards, **kwargs):
    return dict(dict(day=DAY, status='ready', edition_kind='intraday', revision=2,
                     cutoff_at='2025-03-15T01:00:00Z', stats={}, items=cards), **kwargs)


def test_conservative_identity_and_evidence_priority():
    # A rolling single URL can cover a new staged action or a changed number.
    url = 'https://paper.example/council'
    original = card('The city council approved a new transport law', 1, url=url)
    rewrite = card('City council approved the new transport law', 2, url=url+'?utm_source=feed')
    changed = card('The city council revoked a new transport law', 3, url=url)
    amount = card('Port authority allocated 40 million for the terminal', 4)
    more = card('Port authority allocated 50 million for the terminal', 5)
    action = card('Senate signed the treaty', 6, excerpt='Documents describe the signature ceremony.')
    action['action_evidence'] = dict(kind='dated_action_report', basis='摘录含日期和签署', quote=action['excerpt'],
                                     url=action['source_url'])
    entries = select_brief(edition([original, rewrite, changed, amount, more, action]))
    assert entries[0]['rank'] == 6 and {r['rank'] for r in entries} == {1,3,4,5,6}
    assert entries[0]['status'] == 'candidate'
    assert all(r['status'] == 'check' for r in entries[1:])
    assert [r['rank'] for r in select_brief(edition([original,rewrite]))] == [1]


def test_same_action_on_different_urls_and_a_distinct_stage():
    found = card('Bomb found at the ethanol plant in Illinois', 1)
    lift = card('Police lift evacuation after bomb found near the ethanol plant in Illinois', 2)
    passive = card('Evacuation order lifted after bomb found at the ethanol plant in Illinois', 3)
    picks = select_brief(edition([found, lift, passive]))
    assert {p['rank'] for p in picks} == {1, 2}


def test_limits_and_diverse_editorial_shortlist_without_fillers():
    cards = [card(f'Rail service disruption in district {i}', i, category='交通') for i in range(1,15)]
    cards.insert(4, card('Port authority delays tariff decision', 24, category='贸易'))
    cards.insert(6, card('Earthquake shuts down municipal offices', 25, category='社会'))
    picks = select_brief(edition(cards))
    assert len(picks) == 10
    assert picks[0]['rank'] == 1 and picks[1]['category'] == '贸易'
    assert picks == select_brief(edition(cards))
    junk = [card('Best local sandwiches', 1, impact=0), card('Sponsored: Buy this today', 2),
            card('Retailer announces Black Friday early access: $140 off with coupon code', 5),
            card('New war film has a record Rotten Tomatoes score', 6),
            card('Where to watch the final playoff', 3), card('Local schools approved a new plan', 4, url='javascript:alert(1)')]
    md = render_brief(edition(junk))
    assert '0 条视频候选' in md and '没有满足选题条件' in md and '###' not in md


def test_markdown_source_and_translation_are_data():
    url = 'https://news.example/policy)(?a=ok&b=x'
    story = card('City tariff update <img src=x> [pay](javascript:alert(1))', 1, url=url)
    story['actor_context'] = dict(status='seen', summary='在过去 7 天的样本里有相同参与方。',
                                  example=dict(source_url='https://archive.example/paper?a=1&b=2',
                                               detail_url='/internal/relative'))
    story.pop('key')
    desc = daily_card_descriptor(DAY, story)
    md = render_brief(edition([story], backfill_id='repair'), translations={desc['key']:'译文' + '甲'*510})
    assert '资料截至 2025-03-15 09:00' in md
    assert '参与方对照' in md and 'https://archive.example/paper?a=1&amp;b=2' in md
    assert '/internal/relative' not in md
    assert '历史补采' in md and '待核线索' in md and '行动日期：待核' in md
    assert '<img' not in md and '(javascript:alert(1))' not in md
    assert '%29%28' in md and 'b=x>' in md and '已有中文译读节选' in md
    assert '甲'*501 not in md


def test_download_honors_revision_while_rebuilding_and_leaves_database_alone(tmp_path):
    engine = make_engine(f'sqlite:///{tmp_path}/brief.db')
    create_schema(engine)
    app = FastAPI()
    register(app, engine)
    client = TestClient(app)
    endpoint = f'/api/radar/daily/{DAY}/brief.md'
    try:
        assert client.get(endpoint).status_code == 404
        payload = dict(publication=dict(kind='intraday', revision=3, cutoff_at='2025-03-15T07:00:00Z'))
        with Session(engine) as s:
            s.add(DailyDigestIssue(day=DAY, status='refreshing', stats_json=json.dumps(payload), items_json='[]'))
            s.commit()
        result = client.get(endpoint, params={'revision':3})
        assert result.status_code == 200 and result.headers['content-type'].startswith('text/markdown')
        assert result.headers['content-disposition'].endswith(f'{DAY}-r3.md"')
        assert '第 3 版' in result.text and '0 条视频候选' in result.text
        stale = client.get(endpoint, params={'revision':2})
        assert stale.status_code == 409 and f'/radar?day={DAY}' in stale.text
        assert client.get(endpoint, params={'revision':0}).status_code == 422
        assert client.get('/api/radar/daily/2099-03-15/brief.md').status_code == 422
        with Session(engine) as s:
            assert s.get(DailyDigestIssue,DAY).status == 'refreshing'
    finally:
        client.close()
        engine.dispose()

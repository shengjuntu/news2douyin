"""Read-time ageing when collection stops, without opening the observation lake."""
from datetime import datetime,timedelta,timezone
import json
import pytest

from sqlmodel import Session
from news2douyin.attention.models import AttentionTopic
from news2douyin.attention.service import card,list_topics,checked
from news2douyin.storage.db import make_engine,init_db
from test_postgres_integration import postgres_url


def topic(key,hours,platforms=2):
    stamp=(datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat(timespec='seconds').replace('+00:00','Z')
    return AttentionTopic(key=key,title=key,last_seen=stamp,attention_score=80,rise_score=20,
        platform_count=platforms,baseline_state='comparable',summary_json=json.dumps(dict(
            observed_at=stamp,platforms=[dict(platform='weibo',counts_for_attention=True)],
            series=[dict(observed_at=stamp,attention_score=80)],notes=[])))


def test_old_positive_trend_ages_to_unknown_but_keeps_historical_samples():
    row=topic('old',7)
    data=card(row)
    assert data['attention_score'] is None and data['rise_score'] is None
    assert data['summary']['platforms'][0]['observation_state']=='stale'
    assert data['summary']['series'][0]['attention_score']==80
    assert row.attention_score==80 and row.rise_score==20


def test_recent_observation_keeps_its_current_position_signal():
    data=card(topic('recent',1))
    assert data['rise_score']==20 and data['platform_count']==2


@pytest.mark.parametrize('backend',['sqlite','postgres'])
def test_stopped_sources_cannot_keep_old_issues_in_rising_or_above_fresh_coverage(tmp_path,request,backend):
    url=request.getfixturevalue('postgres_url') if backend=='postgres' else 'sqlite:///'+str(tmp_path/'freshness.db')
    engine=make_engine(url);init_db(engine)
    try:
        with Session(engine) as s:
            s.add(topic('old_many_platforms',7,10));s.add(topic('current',1,1));s.commit()
            rising=list_topics(s,checked(period='all',order='rising'))
            assert [r['key'] for r in rising['items']]==['current']
            ordered=list_topics(s,checked(period='all'))
            assert [r['key'] for r in ordered['items']]==['current','old_many_platforms']
            assert ordered['items'][1]['rise_score'] is None
    finally:engine.dispose()

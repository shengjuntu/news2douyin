from datetime import date, datetime, timezone
import json

from sqlmodel import Session

from news2douyin.desk.focus_models import FocusCandidate
from news2douyin.desk.models import Watch, WatchEvent
from news2douyin.digest.focus_hints import annotate_focus, read_focus_hints
from news2douyin.digest.ranking import Candidate, rank_day
from news2douyin.digest.why_follow import attach_focus, describe
from news2douyin.extraction.models import FocusPoint
from news2douyin.knowledge.models import PointMaterial


DAY = '2025-03-16'
STAMP = '2025-03-16T04:05:00Z'
LATER = '2026-11-15T06:30:00Z'
KEY = 'gkg:gkg_english:20250316040000:1'
URL = 'https://city.example/story'
TITLE = 'Council signed a transit agreement on March 16, 2025'


def test_counts_candidate_urls_independently_of_sample_and_domain_caps():
    rows = [Candidate(f'gkg:{i}', TITLE, STAMP, f'https://wire.example/story/{i}', 'gkg') for i in range(80)]
    card = rank_day(rows + rows[:40])['items'][0]
    why = describe(card, {'day': DAY})
    assert card['candidate_documents_capped'] and card['candidate_documents'] == 65
    assert len(why['evidence']['links']) == 8 and why['evidence']['links'][0]['url'] == card['source_url']
    assert '至少 65 个去重候选链接' in why['evidence']['summary']
    assert '1 个报道域名' in why['evidence']['summary']
    small = rank_day(rows[:5] * 3)['items'][0]
    assert small['candidate_documents'] == 5 and not small['candidate_documents_capped']


def test_historical_receipt_does_not_supply_event_day_or_turn_old_action_into_a_new_one():
    card = rank_day([Candidate(KEY, TITLE, LATER, URL, 'gkg', source_clock={'repair_day': DAY})])['items'][0]
    issue = dict(day=DAY, stats={'sources': {'gkg_restored_batches': {'gkg_english:20250316040000': LATER}}})
    why = describe(card, issue)
    assert why['change']['kind'] == 'dated_action'
    assert why['timing']['event_day'] == DAY
    assert why['timing']['restored_at'] == why['timing']['local_received_at'] == LATER
    assert why['timing']['first_observed_at'] == LATER

    legacy = dict(card, action_evidence=dict(kind='dated_action_report', quote='The council signed on 2026-11-15.', url=URL))
    reread = describe(legacy, issue)
    assert reread['change']['kind'] == 'action_lead' and reread['timing']['event_day'] == '2026-11-15'
    unknown = describe({'title': '一个标题', 'candidate_documents': None}, issue)
    assert unknown['timing']['event_day'] == unknown['timing']['first_observed_at'] == ''
    assert unknown['evidence']['links'] == [] and '总数未知' in unknown['evidence']['limits'][0]


def test_official_index_counts_and_clocks_remain_scoped_to_the_official_id():
    card = dict(key='g', source_kind='gdelt', source_key='gdelt:5', source_url=URL,
                source_clock=dict(clock_kind='official', event_day='2025-02-05', upstream_first='2025-02-06T00:00:00Z',
                                  upstream_last=STAMP, local_received_at=LATER),
                coverage_growth_only=True, official_coverage=dict(documents=70, history_complete=False, new_documents=3),
                sources=[dict(url='javascript:bad'), dict(url='https://a.example/new', title='第二个来源')])
    why = describe(card, {'day':DAY})
    assert why['timing']['event_day'] == '2025-02-05'
    assert why['timing']['latest_observed_at'] == STAMP and why['timing']['local_received_at'] == LATER
    assert why['change']['kind'] == 'coverage_growth'
    detail = ' '.join(why['evidence']['limits'])
    assert '70 篇去重文档' in detail and '至少 3 篇报道' in detail and '首次出现 0 个' not in detail
    assert len(why['evidence']['links']) == 2
    legacy = dict(card, official_coverage={'new_domains':2, 'history_complete':True})
    detail = ' '.join(describe(legacy, {'day': DAY})['evidence']['limits'])
    assert '首次出现 2 个媒体域名' in detail and '首次关联 0' not in detail and '共 0' not in detail


def test_scope_word_and_confirmed_question_are_different_paths_and_are_frozen(engine):
    from datetime import timedelta

    day = date.fromisoformat(DAY)
    cutoff = datetime(2025,3,16,8,tzinfo=timezone.utc)
    old = '2025-03-12T00:00:00Z'
    question = 'Welche Gesellschaft betreibt die Fähre?'
    with Session(engine) as s:
        s.add_all([
            Watch(key='ports', name='港口', kind='长期事件', scope='ferry', created=old),
            WatchEvent(watch='ports', event='local5', first_seen=old, choice='auto', branch=question),
            FocusPoint(key='pointA', watch='ports', parent='', text=question, created=old),
            PointMaterial(key='material', point='pointA', event='local5', reason='相关', generation='focus-1'),
            FocusCandidate(key='candidate', watch='ports', event='local5', scope_version=1,
                           status='done', decision='core', questions_json=json.dumps([question]),
                           event_fingerprint='1', updated=STAMP)])
        s.commit()
        matcher = read_focus_hints(s, day, cutoff=cutoff)
        rows = [Candidate('local:local5', 'A new ferry operator was appointed by the city', STAMP, URL, 'local', watches=('港口',)),
                Candidate(KEY, 'Ferry rescue exercise announced', STAMP, 'https://b.example/other', 'gkg')]
        cards = rank_day(annotate_focus(rows, matcher))['items']
        attach_focus(cards,s,matcher,cutoff)
        by_kind = {c['source_kind']:c for c in cards}
        accepted = describe(by_kind['local'], {'day': DAY})['focus'][0]
        hint = describe(by_kind['gkg'], {'day': DAY})['focus'][0]
        assert (accepted['kind'], accepted['question'], accepted['watch_key'], accepted['event']) == (
            'membership', question, 'ports', 'local5')
        assert hint['kind'] == 'scope_hint' and hint['terms'] == ['ferry'] and not hint['question']
        assert accepted['source_url'] == URL and hint['source_url'] == 'https://b.example/other'
        # A matching question reviewed after this edition cannot enter its snapshot.
        record = s.get(FocusCandidate, 'candidate')
        record.updated = (cutoff + timedelta(seconds=1)).isoformat()
        s.add(record)
        s.commit()
        new = dict(by_kind['local'])
        attach_focus([new], s, matcher, cutoff)
        assert describe(new, {'day': DAY})['focus'][0]['question'] == ''
        assert describe(by_kind['local'], {'day': DAY})['focus'][0]['question'] == question


# This join spans watched local events, dated decisions, material and outline
# points. Exercise the production PostgreSQL planner as well as the SQLite fixture.
from test_postgres_integration import postgres_url


def test_postgres_material_to_question_and_cutoff(postgres_url):
    from news2douyin.storage.db import init_db, make_engine
    db = make_engine(postgres_url)
    try:
        init_db(db)
        test_scope_word_and_confirmed_question_are_different_paths_and_are_frozen(db)
    finally:
        db.dispose()

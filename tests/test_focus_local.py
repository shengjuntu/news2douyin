from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient
import pytest
from sqlmodel import Session, select

from news2douyin.candidates import service as candidates
from news2douyin.desk import focus_jobs, focus_leads, intent
from news2douyin.desk.focus_models import FocusDefinition, FocusLead
from news2douyin.desk.models import DeskMeta, RadarEntry, Watch, WatchEvent
from news2douyin.server.app import create_app
from news2douyin.storage.models import ArticleVersion


@pytest.fixture
def local_app(tmp_path, monkeypatch):
    monkeypatch.setattr(intent, '_llm_chat', lambda *args, **kwargs: pytest.fail('local focus must not call a model'))
    app = create_app(db_url=f'sqlite:///{tmp_path / "focus.db"}', storage_root=str(tmp_path))
    try:
        yield app, TestClient(app)
    finally:
        app.state.engine.dispose()


def draft(client, description):
    reply = client.post('/api/focus/interpret', json={'description': description, 'method': 'local'})
    assert reply.status_code == 202, reply.text
    assert reply.json()['status'] == 'ready'
    assert reply.json()['method'] == 'local'
    return reply.json()


def create(client, value, anchors):
    reply = client.post('/api/focus/from-description', json={'draft': value['key'], 'anchors': anchors})
    assert reply.status_code == 201, reply.text
    return reply.json()


def test_web_local_draft_creates_and_edits_quote_seed_and_explicit_anchors(local_app):
    app, client = local_app
    heading = '公交司机换班后把乘客送回家'
    value = draft(client, f'持续跟踪「{heading}」的后续进展，记录当事人的行动。')
    assert [row['name'] for row in value['spec']['entities']] == [heading]
    watch = create(client, value, ['公交司机', '返乡乘客'])
    assert watch['scope'] == '公交司机，返乡乘客'

    newer = draft(client, '继续关注特朗普的公开活动。')
    payload = {'version': watch['version'], 'draft': newer['key'], 'anchors': ['特朗普']}
    updated = client.put(f'/api/focus/{watch["key"]}/definition', json=payload)
    assert updated.status_code == 200, updated.text
    assert updated.json()['version'] == watch['version'] + 1
    assert updated.json()['scope'] == '特朗普'

    with Session(app.state.engine) as s:
        defined = s.get(FocusDefinition, watch['key'])
        assert defined.method == 'local'
        [actor] = json.loads(defined.spec_json)['entities']
        assert 'Donald Trump' in actor['aliases']
        assert actor['type'] == 'person'


def test_switching_existing_scope_to_local_preserves_watch_settings_and_manual_links(local_app):
    app, client = local_app
    spec = intent.local_scope('持续跟踪特朗普本人的公开活动')
    with Session(app.state.engine) as s:
        watch = Watch(key='legacy_model', **intent.to_scope(spec).model_dump())
        watch.enabled, watch.auto_research, watch.review_days = False, True, 14
        s.add(watch)
        s.add(FocusDefinition(watch=watch.key, description=spec.description, spec_json=spec.model_dump_json(), method='model'))
        s.add(WatchEvent(watch=watch.key, event='manual-kept', choice='include', reason='人工收录'))
        s.commit()

    value = draft(client, '持续跟踪特朗普公开的政策行动')
    response = client.put('/api/focus/legacy_model/definition', json={
        'version': 1, 'draft': value['key'], 'anchors': ['特朗普'],
    })
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated['key'] == 'legacy_model' and updated['version'] == 2
    assert (updated['enabled'], updated['auto_research'], updated['review_days']) == (False, True, 14)
    with Session(app.state.engine) as s:
        assert s.get(FocusDefinition, updated['key']).method == 'local'
        assert s.get(WatchEvent, (updated['key'], 'manual-kept')).choice == 'include'


def test_versioned_event_tracking_publishes_local_rule_with_exact_evidence(local_app):
    app, client = local_app
    watch = create(client, draft(client, '跟踪特朗普的公开活动'), ['特朗普'])
    quote = 'Trump signed the public document.'
    article, event = 'public_document', 'document_event'
    action = dict(article=article, revision=1, kind='decision', status='reported', quote=quote,
                  date='2026-11-19', arguments=[dict(role='actor', text='Trump')])
    with Session(app.state.engine) as s:
        s.add(ArticleVersion(article_key=article, revision=1, content_hash='document',
                            payload_json=json.dumps(dict(title=quote, content=quote))))
        card = RadarEntry(key=event, title=quote, headings=quote.casefold(), material=quote.casefold(),
                          seen='2026-11-19T00:00:00Z', priority=2, band='', reason='', evidence='',
                          geography='[]', sources=json.dumps([dict(key=article, title=quote, revision=1, actions=[action])]))
        s.add(card)
        decision = focus_jobs.queue_event(s, s.get(Watch, watch['key']), card)
        assert decision is not None
        s.commit()

    assert focus_jobs.judge_step(app.state.engine)
    with Session(app.state.engine) as s:
        link = s.get(WatchEvent, (watch['key'], event))
        assert link.relevance == 2 and link.branch == '核心进展'
        [decision] = focus_jobs.decisions(s, watch['key'], event)['items']
        assert decision['method'] == 'rules' and decision['decision'] == 'core'
        assert decision['evidence'] == [dict(article=article, revision=1, quote=quote)]


def test_raw_local_lead_is_visible_without_model_budget(local_app, tmp_path):
    app, client = local_app
    watch = create(client, draft(client, '跟踪特朗普的公开活动'), ['特朗普'])
    arrived = datetime(2026, 11, 19, 5, 40, tzinfo=timezone.utc)
    with Session(app.state.engine) as s:
        s.add(DeskMeta(key=focus_leads.CURSOR, value=json.dumps({
            'through': focus_leads._timestamp(arrived-timedelta(minutes=3)),
            'versions': [[watch['key'], watch['version']]],
        })))
        candidates.record_reports(s, [dict(title='Trump released a public document',
            content='The full public document is now available.', url='https://news.example/document',
            language='en')], provider='worldnewsapi', batch='test-local-focus',
            observed_at=focus_leads._timestamp(arrived))
        s.commit()

    assert focus_leads.scan_step(app.state.engine, tmp_path, now=arrived+timedelta(minutes=1))
    assert not focus_leads.judge_step(app.state.engine)
    with Session(app.state.engine) as s:
        lead = s.exec(select(FocusLead)).one()
        assert lead.watch == watch['key']
        assert lead.status == 'done' and lead.decision == 'pending'
        assert lead.judge_day == '' and lead.attempts == 0

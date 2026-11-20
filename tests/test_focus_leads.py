from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import Session, select

from news2douyin.candidates import service as candidates
from news2douyin.desk import focus_leads, intent
from news2douyin.desk.focus_models import FocusDefinition, FocusLead
from news2douyin.desk.models import DeskMeta, Watch, WatchEvent
from news2douyin.digest.models import DailyDigestIssue
from news2douyin.intake import official_projection
from news2douyin.intake.official_models import OfficialFocusUpdate
from news2douyin.intake.projection import project_step
from news2douyin.selection.source_models import SourcePreview
from news2douyin.server.app import create_app
from news2douyin.storage.articles import identity_key
from news2douyin.storage.models import Event
from test_official_events import _stage, event_line

ARRIVED = datetime(2026, 11, 19, 5, 40, tzinfo=timezone.utc)


def source(title, content, path='statement'):
    return dict(title=title, content=content, url=f'https://news.example/{path}', language='ar')


def cursor(session, watches):
    session.add(DeskMeta(key=focus_leads.CURSOR, value=json.dumps({
        'through': focus_leads._timestamp(ARRIVED-timedelta(minutes=3)),
        'versions': [[watch.key, watch.version] for watch in sorted(watches, key=lambda item: item.key)],
    })))


def test_shared_focus_scan_routes_unselected_arabic_report_without_an_event(tmp_path, monkeypatch):
    app = create_app(db_url=f'sqlite:///{tmp_path / "ui.db"}', storage_root=tmp_path)
    engine = app.state.engine
    original = candidates.iter_day
    passes = []

    def observed(*args, **kwargs):
        passes.append(kwargs['since'])
        yield from original(*args, **kwargs)

    monkeypatch.setattr(candidates, 'iter_day', observed)
    monkeypatch.setattr(focus_leads, 'WRITE_BATCH', 1)
    watch = Watch(key='tunis', name='突尼斯', kind='长期事件', scope='تونس', dimensions='الموانئ')
    other = Watch(key='paris', name='巴黎', kind='长期事件', scope='باريس')
    title = 'تونس تعلن خطة جديدة لتطوير الموانئ'
    with Session(engine) as session:
        session.add_all([watch, other])
        cursor(session, [watch, other])
        [key] = candidates.record_reports(session, [source(title, 'أعلنت الحكومة خطة لتطوير الموانئ.')],
            provider='worldnewsapi', batch='arabic-report', observed_at=focus_leads._timestamp(ARRIVED))
        session.commit()
    try:
        assert focus_leads.scan_step(engine, tmp_path, now=ARRIVED + timedelta(minutes=1))
        assert len(passes) == 1
        assert focus_leads.scan_step(engine, tmp_path, now=ARRIVED + timedelta(minutes=2))
        with Session(engine) as session:
            lead = session.exec(select(FocusLead)).one()
            assert (lead.watch, lead.candidate, lead.decision, lead.language) == ('tunis', key, 'pending', 'ar')
            for model in (Event, WatchEvent, DailyDigestIssue):
                assert session.exec(select(func.count()).select_from(model)).one() == 0
        client = TestClient(app)
        focus = client.get('/focus/tunis?view=leads')
        assert focus.status_code == 200
        assert title in focus.text and 'worldnewsapi · ar' in focus.text
        assert '/workbench/new?candidate=' in focus.text
        assert '/organize?candidate=' in focus.text
        assert '关注匹配 · 待核' in focus.text
        assert title not in client.get('/focus/paris').text
    finally:
        engine.dispose()


def test_shared_focus_judgment_cites_pinned_excerpt_and_keeps_titles_pending(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(focus_leads, 'MODEL_PER_FOCUS_PER_DAY', 1)
    spec = intent.validate_spec(dict(name='突尼斯港口', description='跟踪突尼斯港口', kind='长期事件',
        entities=[dict(name='تونس', type='country')], keywords=['الموانئ']))
    watch = Watch(key='ports', **intent.to_scope(spec).model_dump())
    quoted = 'أعلنت الحكومة خطة لتطوير الموانئ.'
    with Session(engine) as session:
        session.add(watch)
        session.add(FocusDefinition(watch=watch.key, description=spec.description, spec_json=spec.model_dump_json()))
        cursor(session, [watch])
        candidates.record_reports(session,
            [source('تونس تعلن خطة للموانئ', quoted), source('تونس تبحث الموانئ', '', path='title')],
            provider='worldnewsapi', batch='ports', observed_at=focus_leads._timestamp(ARRIVED))
        session.commit()
    assert focus_leads.scan_step(engine, tmp_path, now=ARRIVED+timedelta(minutes=1))

    def judge(messages, **kwargs):
        assert engine.pool.checkedout() == 0
        evidence, = json.loads(messages[1]['content'])['sources']
        assert quoted in evidence['excerpts']
        return json.dumps(dict(decision='core', reason='报道直接涉及港口计划',
            evidence=[dict(article=evidence['article'], revision=1, quote=quoted)]))

    monkeypatch.setattr(intent, '_llm_chat', judge)
    assert focus_leads.judge_step(engine)
    later = ARRIVED + timedelta(minutes=2)
    with Session(engine) as session:
        candidates.record_reports(session, [source('تونس تبحث الموانئ من جديد', quoted, path='later')],
            provider='worldnewsapi', batch='ports-later', observed_at=focus_leads._timestamp(later))
        session.commit()
    assert focus_leads.scan_step(engine, tmp_path, now=later+timedelta(minutes=1))
    assert not focus_leads.judge_step(engine)
    with Session(engine) as session:
        rows = {lead.title: lead for lead in session.exec(select(FocusLead))}
        core = rows['تونس تعلن خطة للموانئ']
        assert core.decision == 'core' and json.loads(core.evidence_json)[0]['quote'] == quoted
        assert rows['تونس تبحث الموانئ'].excerpt == ''
        later_lead = rows['تونس تبحث الموانئ من جديد']
        assert later_lead.decision == 'pending' and not later_lead.attempts and not later_lead.judge_day
        assert later_lead in focus_leads.recent(session, session.get(Watch, 'ports'))[0]
        assert session.exec(select(func.count()).select_from(WatchEvent)).one() == 0


def test_focus_reads_late_official_projection_without_reopening_daily_window(engine, tmp_path, monkeypatch):
    from news2douyin.digest.calendar import BEIJING
    from news2douyin.intake import lake

    earlier = ARRIVED-timedelta(days=1)
    original_url = 'https://wire.example/article'
    with Session(engine) as session:
        watch = Watch(key='policy', name='关税政策', kind='长期事件', scope='tariff')
        session.add(watch)
        cursor(session, [watch])
        session.add(SourcePreview(key=identity_key({'url': original_url}), source_url=original_url,
            title='Officials discuss tariff policy', excerpt='The meeting revisited tariff changes.',
            fetched_at=focus_leads._timestamp(earlier)))
        session.commit()

    monkeypatch.setattr(lake, 'utc_now_iso', lambda: focus_leads._timestamp(earlier))
    monkeypatch.setattr(official_projection, 'utc_now_iso', lambda: focus_leads._timestamp(ARRIVED))
    _stage(engine, tmp_path, 'events_english', [event_line()])
    assert focus_leads.scan_step(engine, tmp_path, now=ARRIVED)
    assert project_step(engine, tmp_path)
    day = ARRIVED.astimezone(BEIJING).date()
    cutoff = ARRIVED+timedelta(minutes=1)
    assert not list(candidates.iter_day(engine, tmp_path, day, since=ARRIVED, cutoff=cutoff))
    assert focus_leads.scan_step(engine, tmp_path, now=cutoff)
    with Session(engine) as session:
        lead = session.exec(select(FocusLead)).one()
        assert (lead.candidate, lead.observed_at, lead.provider) == (
            'gdelt:001234567', '2026-11-14T04:15:00Z', 'gdelt')
        assert session.get(OfficialFocusUpdate, '001234567').available_at == focus_leads._timestamp(ARRIVED)
        assert session.exec(select(Event)).first() is None

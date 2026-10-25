import json

from sqlmodel import Session, select

from news2douyin.storage.db import make_engine
from news2douyin.storage.models import Article, ArticleEventLink, Event, ScriptPackage
from news2douyin.storage.repair import apply_event_repairs, main, plan_event_repairs


def seed_legacy_split(engine):
    with Session(engine) as session:
        session.add_all([
            Article(article_key='root', published_at='2026-09-29T00:00:00Z'),
            Article(article_key='copy', is_duplicate=True, duplicate_of_article_key='root', published_at='2026-09-28T00:00:00Z'),
            Event(event_key='original', article_count=1),
            Event(event_key='split', article_count=1),
            ArticleEventLink(article_key='root', event_key='original'),
            ArticleEventLink(article_key='copy', event_key='split', relation_type='duplicate'),
            ScriptPackage(package_key='historical', event_key='split', script_text='Keep this snapshot'),
        ])
        session.commit()


def test_repair_is_previewable_idempotent_and_preserves_history(engine):
    seed_legacy_split(engine)
    with Session(engine) as session:
        plan = plan_event_repairs(session)
        assert len(plan['moves']) == 1
        assert session.exec(select(ArticleEventLink).where(ArticleEventLink.article_key == 'copy')).one().event_key == 'split'
        apply_event_repairs(session, plan)
        session.commit()
        assert plan_event_repairs(session)['moves'] == []
        events = {e.event_key: e for e in session.exec(select(Event))}
        assert events['original'].article_count == 2
        assert events['original'].first_seen_at == '2026-09-28T00:00:00Z'
        assert events['split'].article_count == 0
        assert session.exec(select(ScriptPackage)).one().event_key == 'split'


def test_repair_cli_backs_up_before_changes(engine, monkeypatch, capsys):
    seed_legacy_split(engine)
    monkeypatch.setattr('sys.argv', ['repair', '--db', engine.url.database, '--apply'])
    main()
    report = json.loads(capsys.readouterr().out)
    backup_engine = make_engine('sqlite:///' + report['backup'])
    with Session(backup_engine) as session:
        assert session.exec(select(ArticleEventLink).where(ArticleEventLink.article_key == 'copy')).one().event_key == 'split'
    backup_engine.dispose()
    with Session(engine) as session:
        assert session.exec(select(ArticleEventLink).where(ArticleEventLink.article_key == 'copy')).one().event_key == 'original'


def test_repair_skips_broken_duplicate_chain(engine):
    with Session(engine) as session:
        session.add(Article(article_key='bad', is_duplicate=True, duplicate_of_article_key='missing'))
        session.commit()
        plan = plan_event_repairs(session)
        assert plan['moves'] == []
        assert plan['skipped'][0]['article_key'] == 'bad'

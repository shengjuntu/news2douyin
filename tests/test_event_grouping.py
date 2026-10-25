"""Hand-authored regression cases, NOT an independent semantic benchmark."""
import json
from pathlib import Path

import pytest
from sqlmodel import Session, select

from news2douyin.collect import service as collect
from news2douyin.events.service import evidence_counts
from news2douyin.storage.models import Article, ArticleEventLink, Event, EventAssignment

CASES = json.loads((Path(__file__).parent/'fixtures/event_grouping.json').read_text())


@pytest.mark.parametrize('case', CASES, ids=[c['name'] for c in CASES])
def test_event_grouping_cases(engine, monkeypatch, tmp_path, case):
    items = [dict(case['first'], url='https://first.test/story', source={'domain':'first.test'}),
             dict(case['second'], url='https://second.test/report', source={'domain':'second.test'})]
    monkeypatch.setitem(collect.PROVIDERS, 'fixture', lambda _: items)
    with Session(engine) as session:
        collect.create_or_update_profile(session, {'name':'fixture','provider':'fixture'})
        collect.run_collection(session, 'fixture', storage_root=tmp_path/'runs')
        articles = session.exec(select(Article).order_by(Article.id)).all()
        links = session.exec(select(ArticleEventLink).order_by(ArticleEventLink.id)).all()
        assert len(articles) == 2
        assert articles[1].is_duplicate is case['second_duplicate']
        assert (links[0].event_key == links[1].event_key) is case['same_event']
        if case['same_event']:
            counts = evidence_counts(session, links[0].event_key)
            assert counts['article_count'] == 2
            assert counts['independent_article_count'] == (1 if case['second_duplicate'] else 2)
            assert counts['source_domain_count'] == counts['independent_article_count']
            if not case['second_duplicate']:
                assert session.get(EventAssignment, articles[1].article_key).reason == 'independent_report_rule_v1'

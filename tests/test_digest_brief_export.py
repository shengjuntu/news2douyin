import json
from datetime import date

import pytest
from sqlmodel import Session, select
from sqlalchemy import func

from news2douyin.digest import cli
from news2douyin.digest.brief_export import markdown_for_issue
from news2douyin.digest.models import DailyDigestIssue
from news2douyin.digest.service import read_issue
from news2douyin.llm.settings import LLMSettings
from news2douyin.storage.db import make_engine, init_db
from news2douyin.storage.models import TaskRecord
from news2douyin.tasks.worker import TaskWorker
from news2douyin.translations import service as translations
from news2douyin.translations.daily import daily_card_descriptor, resource_key


def test_brief_reuses_only_completed_translation_of_exact_published_text(tmp_path, monkeypatch):
    day = date(2025, 11, 7)
    url = f'sqlite:///{tmp_path}/work.db'
    engine = make_engine(url)
    init_db(engine)
    card = dict(key='bridge', source_kind='gkg', title='County bridge reopens after inspection',
                source_url='https://local.example/bridge', excerpt='The county approved the reopening.',
                observed_at=f'{day}T04:00:00Z', sources=[])
    with Session(engine) as session:
        session.add(DailyDigestIssue(day=str(day), status='ready', items_json=json.dumps([card])))
        session.commit()
    monkeypatch.setattr(translations, 'get_settings', lambda: LLMSettings('http://test.local', '', 'test'))
    monkeypatch.setattr(translations, 'translate', lambda *args: '已保存的中文译文')
    descriptor = daily_card_descriptor(day, card)
    key = resource_key(str(day), descriptor['key'], descriptor['fingerprint'])
    translations.submit(engine, kind='daily_card', key=key)
    def read():
        with Session(engine) as session:
            return markdown_for_issue(session, read_issue(session, day))
    assert '已保存的中文译文' not in read()
    worker = TaskWorker(engine, str(tmp_path))
    worker.execute(worker.service.claim())
    markdown = read()
    assert '已保存的中文译文' in markdown and card['title'] in markdown
    out = tmp_path/'briefs'/'one.md'
    assert cli.main(['brief','--day',str(day),'--db-url',url,'--out',str(out)]) == 0
    assert markdown == out.read_text(encoding='utf-8')
    with Session(engine) as session:
        issue = session.get(DailyDigestIssue, str(day))
        issue.items_json = json.dumps([dict(card, title='County bridge closure extended')])
        session.add(issue)
        session.commit()
    assert '已保存的中文译文' not in read()
    with Session(engine) as session:
        assert session.exec(select(func.count()).select_from(TaskRecord)
                            .where(TaskRecord.trigger_type=='translation')).one() == 1
    with pytest.raises(SystemExit) as missing:
        cli.main(['brief','--day','2025-11-06','--db-url',url,'--out',str(out)])
    assert missing.value.code == 2 and out.read_text(encoding='utf-8') == markdown
    engine.dispose()

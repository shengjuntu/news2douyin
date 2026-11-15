"""Reading copies translate the visible report, never event-label inventions."""
import json

import pytest
from sqlmodel import Session,select
from news2douyin.selection.source_models import SourcePreview
from news2douyin.storage.articles import identity_key
from news2douyin.storage.models import Article,ArticleIdentity
from news2douyin.translations import service
from news2douyin.translations.models import ReadingTranslation
from news2douyin.tasks.control import TaskConflict
from test_postgres_integration import postgres_url


@pytest.fixture(params=['sqlite','postgres'])
def preview_engine(request):
    if request.param=='sqlite':
        yield request.getfixturevalue('engine');return
    from news2douyin.storage.db import make_engine,init_db
    engine=make_engine(request.getfixturevalue('postgres_url'));init_db(engine)
    try:yield engine
    finally:engine.dispose()


def test_remote_preview_translation_freezes_visible_source_text(preview_engine,monkeypatch):
    engine=preview_engine
    url='https://publisher.example/story';key=identity_key({'url':url})
    with Session(engine) as s:
        s.add(SourcePreview(key=key,source_url=url,title='Le parlement adopte une réforme',
            excerpt='Le texte sera examiné vendredi.',language='fr',content_hash='first'))
        s.commit()
    monkeypatch.setattr(service,'translate',lambda *args:pytest.fail('Submit called a model'))
    first=service.submit(engine,kind='source_preview',key=key)
    assert first['source_language']=='fr'
    assert first['original']=='Le parlement adopte une réforme\n\nLe texte sera examiné vendredi.'
    assert service.submit(engine,kind='source_preview',key=key)['id']==first['id']
    with Session(engine) as s:
        row=s.get(SourcePreview,key);row.title='Une autre version du titre';s.add(row);s.commit()
    changed=service.submit(engine,kind='source_preview',key=key)
    assert changed['id']!=first['id']
    assert service.get(engine,first['id'])['original']==first['original']


def test_local_report_translation_is_the_bounded_card_excerpt_not_full_body(preview_engine):
    engine=preview_engine
    url='https://publisher.example/local';key=identity_key({'url':url})
    body='可读报道内容。'*500
    with Session(engine) as s:
        s.add(Article(article_key='local-source',url=url,title='议会审议交通预算',content=body,language='zh'))
        s.add(ArticleIdentity(identity_key=key,article_key='local-source'));s.commit()
    result=service.submit(engine,kind='source_preview',key=key)
    assert result['original']=='议会审议交通预算\n\n'+body[:1200]
    assert len(result['original'])<len(body)
    assert '主体：' not in result['original'] and '行为：' not in result['original']


def test_changed_local_headline_cannot_translate_an_incompatible_old_excerpt(preview_engine):
    engine=preview_engine
    url='https://publisher.example/updated';key=identity_key({'url':url})
    with Session(engine) as s:
        s.add(Article(article_key='updated-source',url=url,title='总统否认签署该协议',content='',language='zh'))
        s.add(ArticleIdentity(identity_key=key,article_key='updated-source'))
        s.add(SourcePreview(key=key,source_url=url,title='总统宣布签署该协议',excerpt='协议已签署并生效。',language='zh'))
        s.commit()
    result=service.submit(engine,kind='source_preview',key=key)
    assert result['original']=='总统否认签署该协议'
    assert '已签署' not in result['original']


def test_missing_source_preview_does_not_create_translation(preview_engine):
    engine=preview_engine
    with pytest.raises(KeyError):service.submit(engine,kind='source_preview',key='f'*64)
    with Session(engine) as s:assert s.exec(select(ReadingTranslation)).first() is None

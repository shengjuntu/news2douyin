"""Saved-language labels and explicit translation stay attached to article versions."""
from fastapi.testclient import TestClient
import pytest
from sqlmodel import Session

from news2douyin.server.app import create_app
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article


@pytest.fixture
def article_ui(tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL','')
    app=create_app(db_url='sqlite:///'+str(tmp_path/'ui.db'),storage_root=str(tmp_path/'runs'))
    with Session(app.state.engine) as session:
        row=Article(article_key='lang-article',provider='fixture',title='Ancien titre <script>alert(1)</script>',
            content='Une ancienne version vérifiable.',language='fr',country='fr',source_domain='source.example',
            url='https://source.example/report',published_at='2026-11-14T00:00:00Z')
        session.add(row);session.flush();record_version(session,row)
        row.title='Новый заголовок';row.content='Новая версия.';row.language='ru'
        session.add(row);session.flush();record_version(session,row)
        metadata=Article(article_key='lang-metadata',provider='gdelt',title='Notice',language='',content='',
            source_domain='metadata.example',url='https://metadata.example/report')
        session.add(metadata);session.flush();record_version(session,metadata)
        session.commit()
    yield TestClient(app),app
    app.state.engine.dispose()


def test_library_exposes_declared_language_title_translation_and_material_state(article_ui):
    client,_=article_ui
    page=client.get('/articles')
    assert page.status_code==200,page.text
    assert '俄语 · 来源字段' in page.text
    assert '语言未知 · 无可靠语言字段' in page.text
    assert '报道线索 · 尚无正文' in page.text
    assert '已保存正文' in page.text
    assert 'data-translation-scope="title"' in page.text
    assert 'data-translation-key="lang-article"' in page.text
    assert '原文发布时间 · Asia/Shanghai' in page.text
    assert '2026-11-14 08:00' in page.text
    assert '<summary>分类、日期与更多条件</summary>' in page.text
    assert '此处是已进入抽取与研究流程' not in page.text


def test_detail_translates_the_selected_snapshot_and_preserves_original(article_ui):
    client,_=article_ui
    old=client.get('/articles/lang-article?revision=1')
    assert old.status_code==200,old.text
    assert '法语 · 来源字段' in old.text and '俄语 · 来源字段' not in old.text
    assert 'data-translation-revision="1"' in old.text
    assert 'data-translation-scope="document"' in old.text
    assert 'Une ancienne version vérifiable.' in old.text
    assert '<script>alert(1)</script>' not in old.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in old.text
    assert '译读使用当前查看的 r1' in old.text
    assert '原文发布时间（北京时间）' in old.text
    latest=client.get('/articles/lang-article')
    assert latest.status_code==200 and '俄语 · 来源字段' in latest.text
    assert 'data-translation-revision="2"' in latest.text
    assert '正文差异' in latest.text


def test_metadata_translation_does_not_offer_nonexistent_full_text(article_ui):
    client,_=article_ui
    page=client.get('/articles/lang-metadata')
    assert page.status_code==200,page.text
    assert '翻译已保存标题' in page.text
    assert '报道线索 · 尚无正文' in page.text
    assert 'data-translation-revision="1"' in page.text
    assert '打开原文 ↗' in page.text


def test_advanced_filter_fields_survive_and_translation_assets_are_served(article_ui):
    client,_=article_ui
    page=client.get('/articles?country=fr&period=custom&date_from=2026-11-01&date_to=2026-11-30&mode=terms&limit=1')
    assert page.status_code==200,page.text
    assert '<details class="article-advanced-filters" open>' in page.text
    assert 'name="date_from" value="2026-11-01"' in page.text
    assert 'name="date_to" value="2026-11-30"' in page.text
    script=client.get('/research-assets/translation-ui.js')
    assert script.status_code==200
    assert client.get('/research-assets/translation-ui.css').status_code==200

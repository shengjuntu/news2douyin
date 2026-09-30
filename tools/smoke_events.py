"""Offline 0.8 browser fixture; every news item is fictional."""
import argparse
import tempfile
from pathlib import Path
from sqlmodel import Session

from news2douyin.collect import llm_filter
from news2douyin.events import research
from news2douyin.events.service import refresh_event
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article,Event,ArticleEventLink
from news2douyin.server import webui
from news2douyin.server.app import create_app


def seed(engine):
    with Session(engine) as s:
        a=Article(article_key='fixture_a',title='离线示例：团队宣布芯片研发计划',content='示例团队于九月一日宣布芯片研发计划，计划开展三组对照实验。本条为虚构资料，用于验证页面流程。',url='https://example.com/chip-plan',canonical_url='https://example.com/chip-plan',source_domain='example.com',country='cn',language='zh',published_at='2026-09-02T01:00:00Z')
        b=Article(article_key='fixture_b',title='离线示例：芯片实验取得阶段结果',content='示例团队在九月五日披露阶段实验结果，后续实验日期仍未确定。本条为虚构资料，不代表真实研究成果。',url='https://second.example.com/chip-result',canonical_url='https://second.example.com/chip-result',source_domain='second.example.com',country='cn',language='zh',published_at='2026-09-06T02:00:00Z')
        s.add_all([a,b,Event(event_key='fixture_background',event_title='离线示例：此前的实验背景')]);s.flush()
        for row in [a,b]:record_version(s,row)
        s.add(ArticleEventLink(article_key='fixture_a',event_key='fixture_background'));refresh_event(s,'fixture_background');s.commit()


def mock_search(query):
    return {'items':[dict(title='离线检索示例：补充的研发背景',content='示例材料介绍了此前设备准备和实验方法。本条为虚构的外部检索结果，用于验证收录及溯源。',url='https://research.example.com/background',source={'domain':'research.example.com'},country='cn',language='zh',published_at='2026-08-29T00:00:00Z',research_meta={'provider':'offline_fixture','content_kind':'text','content_truncated':False})], 'available':1}


def main():
    import uvicorn
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=18195);args=parser.parse_args()
    llm_filter.endpoint_alive=lambda *a,**k:False
    webui._llm_alive=lambda *a,**k:False
    research.search_news=mock_search
    with tempfile.TemporaryDirectory(prefix='events-browser-') as directory:
        app=create_app(db_url=f'sqlite:///{directory}/app.db',storage_root=str(Path(directory)/'runs'))
        seed(app.state.engine)
        uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()

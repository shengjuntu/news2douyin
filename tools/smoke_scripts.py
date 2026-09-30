"""Offline 0.9 browser fixture. Fictional sources and an explicitly mocked model."""
import argparse
import tempfile
import time
from pathlib import Path
from sqlmodel import Session

from news2douyin.collect import llm_filter
from news2douyin.editorial import generation
from news2douyin.events import workbench as events
from news2douyin.events.schemas import MomentEdit
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article, Event, ArticleEventLink
from news2douyin.server import webui
from news2douyin.server.app import create_app


def seed(engine):
    with Session(engine) as s:
        rows=[Article(article_key='fixture_a',title='离线示例：研发计划公布',content='示例团队于九月一日公布研发计划，计划开展三组对照实验。本条为虚构资料。',url='https://example.com/a',source_domain='example.com',published_at='2026-09-02T01:00:00Z'),
              Article(article_key='fixture_b',title='离线示例：阶段实验结果',content='示例团队在九月五日披露阶段实验结果。后续实验日期尚不明确。本条为虚构资料。',url='https://second.example.com/b',source_domain='second.example.com',published_at='2026-09-06T01:00:00Z')]
        s.add_all(rows+[Event(event_key='fixture_topic',event_title='芯片研发进展（离线演示）')]);s.flush()
        for a in rows:
            record_version(s,a);s.add(ArticleEventLink(article_key=a.article_key,event_key='fixture_topic'))
        s.commit()
        for title,description,key,day,reviewed in [('研发计划公布','示例团队公布研发计划，准备三组对照实验。','fixture_a','2026-09-01',True),('阶段结果披露','示例团队披露阶段实验结果，后续日期尚不明确。','fixture_b','2026-09-05',True),('待确认的下一阶段','尚无足够材料确认下一阶段安排。','fixture_b','',False)]:
            view=events.workspace(s,'fixture_topic');source=next(a for a in view['sources'] if a['article_key']==key)
            events.save_moment(s,'fixture_topic',MomentEdit(expected_version=view['version'],title=title,description=description,
                time_kind='occurred' if day else 'unknown',date_start=day,certainty='exact' if day else 'unknown',reviewed=reviewed,
                sources=[{k:source[k] for k in ('article_key','revision','content_hash','excerpt')}]).model_dump())


def main():
    import uvicorn
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=18196);args=parser.parse_args()
    llm_filter.endpoint_alive=lambda *a,**k:False;webui._llm_alive=lambda *a,**k:False
    attempts={}
    def mock_model(spec):
        time.sleep(1)
        mode=spec['options']['mode'];attempts[mode]=attempts.get(mode,0)+1
        if mode=='explain' and attempts[mode]==1:raise ValueError('离线演示：模拟模型首次失败，可点击重试')
        result=generation.outline(spec)
        result['title']='离线模型演示｜'+spec['evidence']['title']
        result['notes']='模型响应桩，仅供界面和任务恢复验证。'
        return result
    generation.model_draft=mock_model
    with tempfile.TemporaryDirectory(prefix='scripts-browser-') as directory:
        app=create_app(db_url=f'sqlite:///{directory}/app.db',storage_root=str(Path(directory)/'runs'));seed(app.state.engine)
        uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()

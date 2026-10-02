"""Local browser fixture. Never use this fixture server for real deployments."""
import argparse
import importlib.util
from pathlib import Path
import sys

import uvicorn
from sqlmodel import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article
from news2douyin.storage.articles import record_version
from news2douyin.research.tools import ResearchTools
from news2douyin.research import service as svc

spec = importlib.util.spec_from_file_location('research_contract_fixture', ROOT / 'tests/test_research.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


def fixture_app(root):
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    app = create_app(db_url='sqlite:///' + str(root / 'browser.db'), storage_root=str(root))
    fake = module.FakeRunDesk(); app.state.research.client_factory = lambda config: fake
    with Session(app.state.engine) as s:
        if not s.exec(__import__('sqlmodel').select(Article).where(Article.article_key == 'research-demo')).first():
            article=Article(article_key='research-demo', title='示例政策调整：从一条新闻追查历史背景',
                content='旧政策于 2023 年实施。\n新政策于 2026 年公布，扩大了适用范围。\n相关机构尚未公布实施效果数据。',
                url='https://example.com/policy', published_at='2026-01-01T00:00:00Z')
            s.add(article);s.flush();record_version(s,article,origin='test_fixture')
            s.commit()

    @app.post('/fixture/research/{case_id}/finish')
    def finish(case_id: str):
        with Session(app.state.engine) as s:
            data = svc.detail(s, case_id)
        run = data['runs'][0]; run_id = run['run_id']
        tools = ResearchTools(app.state.engine, app.state.research_settings)
        source = data['sources'][0]['source_id']
        tools.call('research_save_question', dict(run_id=run_id,key='history',text='此前政策何时实施？',status='researching'))
        claim = tools.call('research_save_claim', dict(run_id=run_id,text='旧政策于 2023 年实施。',kind='fact',occurred_at='2023',
            evidence=[dict(source_id=source,paragraph_id='P1',quote='旧政策于 2023 年实施。',relation='supports')]))
        tools.call('research_save_question', dict(run_id=run_id,key='history',text='此前政策何时实施？',status='answered',answer='资料表明旧政策于 2023 年实施。',claim_ids=[claim['claim_id']]))
        tools.call('research_save_report', dict(run_id=run_id,title='示例：政策背景研究',sections=[
            dict(heading='历史背景',body='旧政策于 2023 年实施。这是理解此次调整的历史起点。',claim_ids=[claim['claim_id']]),
            dict(heading='仍需调查',body='缺少实施效果数据，暂不能推断政策带来的实际影响。',claim_ids=[])],
            gaps=['实施效果尚无足够数据支持。'],completeness='partial'))
        fake.sessions[run['session_id']]['status'] = 'completed'
        return app.state.research.sync(run_id)

    return app


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--port',type=int,default=18311)
    args=parser.parse_args();uvicorn.run(fixture_app(args.root),host='127.0.0.1',port=args.port)

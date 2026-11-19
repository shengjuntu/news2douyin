"""Run the description-first topic flow against real HTTP, FalkorDB and Chromium.

The model output and news below are deliberately controlled QA fixtures. This
does not contact a live model provider or claim evaluation of model accuracy.
All databases are disposable; an optional NEWS2DOUYIN_TEST_POSTGRES_URL must
point to a dedicated empty test database. Screenshots stay in the output folder.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import requests
import uvicorn
from fastapi import HTTPException
from sqlmodel import Session, select

from news2douyin.desk import focus_jobs, intent, service as desk
from news2douyin.desk.models import Watch, WatchEvent
from news2douyin.extraction import service as extraction
from news2douyin.server.app import create_app
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article, ArticleEventLink, Event


DESCRIPTION = '持续跟踪特朗普政府对乌克兰的军援和停火谈判，少看私人生活，了解政策的历史变化。'
SPEC = dict(
    name='对乌军援持续跟踪', kind='长期事件',
    entities=[dict(name='特朗普', type='person', aliases=['Donald Trump', 'Trump'], core=True),
              dict(name='乌克兰', type='country', aliases=['Ukraine'], core=True),
              dict(name='美国政府', type='organization', aliases=['White House'], core=False)],
    relations=[dict(subject='特朗普', relation='宣布或调整军援', object='乌克兰',
                    keywords=['military aid', '军援'])],
    keywords=['军援', 'military aid', '谈判', 'peace talks'],
    include=['政策决定、否认和更正均应关注'], exclude=['纯私人生活和品牌营销'],
    questions=['军援政策发生了哪些实质变化？', '这些变化有怎样的历史背景？'],
    time_start='', time_end='', geography=['乌克兰'],
)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def seed(app):
    rows = [
        ('core', 'QA_CORE Donald Trump announced military aid to Ukraine after talks in Paris.', '演示材料：对乌军援政策变化'),
        ('exclude', 'QA_EXCLUDE Donald Trump Hotel hosted a Ukraine arts promotion. This was brand marketing.', '演示材料：酒店品牌活动'),
        ('pending', 'QA_PENDING Donald Trump discussed Ukraine military aid, but this fragment supplies no details.', '演示材料：信息不足的援助线索'),
    ]
    with Session(app.state.engine) as session:
        for key, content, title in rows:
            article = Article(article_key='focusqa_'+key, title=title, content=content,
                published_at='2026-11-12T02:00:00Z', source_domain=key+'.example',
                url='https://'+key+'.example/fixture')
            session.add(article)
            session.add(Event(event_key='focusqa_event_'+key, event_title=title))
            session.add(ArticleEventLink(article_key=article.article_key, event_key='focusqa_event_'+key))
            record_version(session, article)
        session.commit()
    for _ in range(30):
        if not extraction.step(app.state.engine):
            break
    for _ in range(30):
        if not desk.drain(app.state.engine):
            break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ['NEWS2DOUYIN_GKG_STREAM_ENABLED'] = '0'
    os.environ['NEWS2DOUYIN_EVENT_READER'] = 'rules'
    import redislite
    from redis import Redis
    binary = Path(redislite.__file__).parent / 'bin'
    state = dict(mode='normal', calls=0, interpret_calls=0, judge_calls=0)

    def model_fixture(messages, **kwargs):
        state['calls'] += 1
        payload = json.loads(messages[-1]['content'])
        if 'description' in payload:
            state['interpret_calls'] += 1
            # Long enough to verify an actual nonblocking loading state.
            time.sleep(.55)
            if 'QA_OFFLINE' in payload['description'] or state['mode'] == 'unavailable':
                raise RuntimeError('controlled_fixture_unavailable')
            return json.dumps(SPEC, ensure_ascii=False)
        state['judge_calls'] += 1
        if state['mode'] == 'unavailable':
            raise RuntimeError('controlled_fixture_unavailable')
        sources = payload['sources']
        text = '\n'.join(piece for row in sources for piece in row['excerpts'])
        if 'QA_PENDING' in text:
            return json.dumps(dict(decision='pending', reason='演示片段信息不足，保留待核。', evidence=[], questions=[]), ensure_ascii=False)
        selected = sources[0]
        quote = next((piece for piece in selected['excerpts'] if 'QA_' in piece), selected['excerpts'][0])[:600]
        return json.dumps(dict(decision='exclude' if 'QA_EXCLUDE' in text else 'core',
            reason='演示品牌营销，与焦点政策无关。' if 'QA_EXCLUDE' in text else '演示报道直接涉及对乌军援政策。',
            evidence=[dict(article=selected['article'], revision=selected['revision'], quote=quote)],
            questions=payload['topic'].get('questions', [])[:1]), ensure_ascii=False)

    intent._llm_chat = model_fixture
    graph_port, http_port = free_port(), free_port()
    server = None
    thread = None
    with tempfile.TemporaryDirectory(prefix='n2d-focus-browser-') as temporary:
        folder = Path(temporary)
        with (output / 'falkor.log').open('w') as graph_log:
            graph = subprocess.Popen([str(binary / 'redis-server'), '--loadmodule', str(binary / 'falkordb.so'),
                '--port', str(graph_port), '--bind', '127.0.0.1', '--dir', str(folder),
                '--save', '', '--appendonly', 'no'], stdout=graph_log, stderr=subprocess.STDOUT)
            app = None
            try:
                client = Redis(port=graph_port, socket_connect_timeout=.2)
                for _ in range(100):
                    try:
                        if client.ping():
                            break
                    except Exception:
                        time.sleep(.05)
                else:
                    raise RuntimeError('QA FalkorDB did not start')
                os.environ['NEWS2DOUYIN_FALKOR_URL'] = f'redis://127.0.0.1:{graph_port}'
                os.environ['NEWS2DOUYIN_GRAPH_PREFIX'] = 'focus_browser_qa'
                database = os.getenv('NEWS2DOUYIN_TEST_POSTGRES_URL') or 'sqlite:///' + str(folder / 'news.db')
                app = create_app(db_url=database, storage_root=str(folder / 'runs'))
                seed(app)

                @app.get('/qa/focus-state/{key}')
                def qa_state(key: str):
                    with Session(app.state.engine) as session:
                        tracking = focus_jobs.status(session, key)
                        links = [row.model_dump() for row in session.exec(select(WatchEvent).where(WatchEvent.watch == key))]
                    mirror = app.state.desk.graph
                    counts = {}
                    for label in ('InterestScope', 'InterestEntity', 'InterestPattern', 'EventMention'):
                        try:
                            rows = mirror.read('focus_' + key, 'MATCH (n:' + label + ') RETURN count(n)')
                            counts[label] = rows[0][0] if rows else 0
                        except Exception:
                            counts[label] = None
                    return dict(tracking=tracking, links=links, graph=counts, fixture_model=state)

                @app.post('/qa/focus-model/{mode}')
                def qa_mode(mode: str):
                    if mode not in ('normal', 'unavailable'):
                        raise HTTPException(422, 'Unknown QA model mode')
                    state['mode'] = mode
                    return dict(fixture=True, mode=mode)

                server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=http_port, log_level='warning'))
                thread = threading.Thread(target=server.run, daemon=True)
                thread.start()
                url = f'http://127.0.0.1:{http_port}'
                for _ in range(120):
                    try:
                        if requests.get(url + '/api/health', timeout=.3).ok:
                            break
                    except Exception:
                        time.sleep(.05)
                else:
                    raise RuntimeError('QA HTTP server did not start')
                env = dict(os.environ, N2D_QA_URL=url, N2D_QA_OUTPUT=str(output),
                    N2D_QA_DESCRIPTION=DESCRIPTION)
                result = subprocess.run(['node', str(ROOT / 'tools/check_focus_browser.cjs')], env=env, timeout=210)
                if result.returncode:
                    raise SystemExit(result.returncode)
                with app.state.engine.connect() as connection:
                    from sqlalchemy import text
                    assert connection.execute(text('SELECT 1')).scalar() == 1
                print(json.dumps(dict(ok=True, backend=app.state.engine.dialect.name,
                    real_http=True, real_falkordb=True, real_chromium=True,
                    model='controlled local QA fixture', model_calls=state), ensure_ascii=False))
            finally:
                if server is not None:
                    server.should_exit = True
                    if thread is not None:
                        thread.join(timeout=30)
                if app is not None:
                    app.state.engine.dispose()
                graph.terminate()
                try:
                    graph.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    graph.kill()
                    graph.wait()


if __name__ == '__main__':
    main()

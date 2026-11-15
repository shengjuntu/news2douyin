"""Exercise v0.27 UI using isolated local HTTP, SQL and the real task worker.

Collection, research and graph workers are not started. Translation's provider
boundary returns a deterministic reading copy; no remote model is contacted.
Screenshots stay in the specified scratch output, outside the source package.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
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
sys.path.insert(0, str(ROOT/'src'))
import requests
import uvicorn
from fastapi import Response
from sqlmodel import Session, select
from sqlalchemy import func
from news2douyin.server.app import create_app
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.intake.projection import import_document
from news2douyin.storage.models import Article, ArticleVersion, Event
from news2douyin.topics.models import TopicSource
from news2douyin.workbench.models import ContentTask
from news2douyin.translations import service as translations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for flag in ('NEWS2DOUYIN_GKG_STREAM_ENABLED', 'NEWS2DOUYIN_GKG_PROJECTION_ENABLED',
                 'NEWS2DOUYIN_GDELT_STREAM_ENABLED', 'NEWS2DOUYIN_GDELT_PROJECTION_ENABLED'):
        os.environ[flag] = '0'
    os.environ['NEWS2DOUYIN_FALKOR_URL'] = ''
    with tempfile.TemporaryDirectory(prefix='n2d-ui0270-') as temporary:
        folder = Path(temporary)
        app = create_app(db_url='sqlite:///' + str(folder/'fixture.db'), storage_root=str(folder/'runs'))
        now = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
        with Session(app.state.engine) as session:
            for ident, first, second, code, iso in (
                ('123456', 'DONALD TRUMP', 'UKRAINE', '072', 'ua'),
                ('987654', '甲国', '乙国', '040', 'cn'),
                ('456789', 'الولايات المتحدة', 'أوكرانيا', '190', 'ua')):
                session.add(OfficialEvent(event_id=ident, event_date='2026-10-01', first_seen=now, last_seen=now,
                    actor1_name=first, actor2_name=second, event_code=code, event_base_code=code, event_root_code=code[:2],
                    action_country_iso2=iso, action_place='Kyiv' if iso=='ua' else '示例地点', latitude=50.45 if iso=='ua' else 39.9,
                    longitude=30.52 if iso=='ua' else 116.4, source_url='https://news.example/' + ident,
                    feed='events_english', stamp=stamp, row_index=1))
            session.add(OfficialMention(key='ui0270-mention', event_id='123456', doc_identifier='https://one.example/report',
                domain='one.example', first_seen=now, last_seen=now, confidence=85,
                first_feed='mentions_english', first_stamp=stamp, first_row_index=1,
                last_feed='mentions_english', last_stamp=stamp, last_row_index=1))
            session.commit()
        article = import_document(app.state.engine, dict(provider='qa.monitor', record_id='pl-report',
            uri='https://pl.example/report', title='Polska negocjuje nowe porozumienie',
            content='Polska prowadzi rozmowy o nowym porozumieniu. Szczegóły wymagają potwierdzenia.',
            published_at='2026-11-13T15:00:00Z', language='pl', country='pl'))
        with Session(app.state.engine) as session:
            version_before = session.exec(select(ArticleVersion.payload_json).where(
                ArticleVersion.article_key == article['article_key'])).one()
        graph_calls = []
        def forbidden(*_args, **_kwargs):
            graph_calls.append(True)
            raise AssertionError('UI HTTP contacted remote graph')
        app.state.desk.graph.query = app.state.desk.graph.read = app.state.desk.graph.explain = forbidden
        translation_calls = []
        original_translate = translations.translate
        def model_fixture(original, _target):
            translation_calls.append(original)
            if len(translation_calls) == 1:
                raise ValueError('离线测试模型暂不可用，请重试。原文已保留。')
            return '<script>window.qaInjected=true</script> 中文阅读译文：各方正在就协议进行磋商，细节仍待核实。'
        translations.translate = model_fixture
        @app.get('/favicon.ico', include_in_schema=False)
        def favicon():
            return Response(status_code=204)
        @app.get('/qa/config', include_in_schema=False)
        def config():
            return dict(article=article['article_key'])
        @app.get('/qa/state', include_in_schema=False)
        def state():
            with Session(app.state.engine) as session:
                return dict(counts={model.__name__:session.exec(select(func.count()).select_from(model)).one()
                    for model in (Article, Event, ContentTask, TopicSource)}, translation_calls=len(translation_calls))
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        base = 'http://127.0.0.1:' + str(port)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, lifespan='off', log_level='warning'))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        app.state.worker.start()
        try:
            for _ in range(100):
                try:
                    if requests.get(base+'/api/health', timeout=.2).ok:
                        break
                except requests.RequestException:
                    time.sleep(.05)
            else:
                raise RuntimeError('Disposable HTTP fixture did not start')
            env = dict(os.environ, N2D_QA_URL=base, N2D_QA_OUTPUT=str(output))
            completed = subprocess.run(['node', str(ROOT/'tools/check_ui0270_browser.cjs')], env=env, timeout=180)
            if completed.returncode:
                raise SystemExit(completed.returncode)
            with Session(app.state.engine) as session:
                version_after = session.exec(select(ArticleVersion.payload_json).where(
                    ArticleVersion.article_key == article['article_key'])).one()
            assert version_before == version_after, 'Translation changed the immutable news source'
            assert not graph_calls, graph_calls
            report = json.loads((output/'ui0270-browser-results.json').read_text(encoding='utf-8'))
            report.update(version='0.27.0', executed_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                real_http=True, real_chromium=True, database='disposable SQLite', graph_http_calls=0,
                background_workers=['existing TaskWorker only'], translation_provider='deterministic local stub, no remote model',
                article_version_unchanged=True, article_version_sha256=hashlib.sha256(version_before.encode()).hexdigest(),
                fixtures='three synthetic official events, one mention and one Polish-language saved article')
            (output/'ui0270-browser-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False))
        finally:
            app.state.worker.stop()
            server.should_exit = True
            thread.join(timeout=10)
            translations.translate = original_translate
            app.state.engine.dispose()


if __name__ == '__main__':
    main()

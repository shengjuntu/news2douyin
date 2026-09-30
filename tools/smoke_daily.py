"""Offline browser fixture server. All news below is fictional test data."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import tempfile

from news2douyin.collect import llm_filter, service as collect
from news2douyin.dedup import service as dedup
from news2douyin.server import webui
from news2douyin.server.app import create_app


def main():
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18191)
    args = parser.parse_args()
    llm_filter.endpoint_alive = lambda *a, **kw: False
    dedup.DEDUP_LLM_ENABLED = False
    webui._llm_alive = lambda *a, **kw: False
    def fixture(config):
        now = datetime.now(timezone.utc).isoformat()
        headlines = ['离线示例：研究团队公开新型芯片测试方案', '离线示例：城市公布公共交通更新计划', '离线示例：企业发布季度经营数据']
        texts = ['测试方案包含三组对照实验，团队同时公布了方法说明。本条为虚构的离线验证数据，不代表真实新闻。', '计划介绍了线路调整与服务反馈安排，具体实施仍以正式公告为准。本条为虚构的离线验证数据。', '企业在说明材料中列出了主要业务变化及统计范围。本条为虚构的离线验证数据，不代表真实经营结果。']
        return [{'title': title, 'content': texts[i], 'url': f'https://example.com/demo-{i}',
                 'source': {'domain':'example.com'}, 'country':'cn', 'language':'zh',
                 'published_at':now, 'fetched_at':now} for i, title in enumerate(headlines)]
    collect.PROVIDERS['mock'] = fixture
    with tempfile.TemporaryDirectory(prefix='news2douyin-daily-') as directory:
        app = create_app(db_url=f'sqlite:///{directory}/app.db', storage_root=str(Path(directory)/'runs'))
        uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()

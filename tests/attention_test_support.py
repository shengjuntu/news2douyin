"""Synthetic NewsNow rounds served through the existing loopback HTTP fixture.

These data exercise ingestion, not real popularity or source availability. No
private NewsNow deployment, public platform, or model endpoint is contacted.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from newsnow_test_support import newsnow_http, snapshot


TARIFF = '特朗普宣布对进口钢铝加征25%关税'
CEASEFIRE = '特朗普与乌克兰总统讨论俄乌停火方案'
DENIAL = '特朗普否认对进口钢铝加征25%关税'
SOURCES = ['zhihu', 'weibo', 'douyin', 'cls', 'fastbull', 'xueqiu', 'github']


def now_stamp():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def item(native_id, title, platform, **changes):
    value = dict(id=native_id, title=title,
                 url=f'https://{platform}.example/news/{native_id}',
                 extra=dict(info='合成测试指标', hover='仅用于QA的摘要线索，未获取全文。'))
    value.update(changes)
    return value


def rounds(stamp=None):
    """Cached headers change; the actual third list changes and one source fails."""
    stamp = now_stamp() if stamp is None else stamp
    first = {
        'zhihu': snapshot('zhihu', [item('ports', '全球多个港口工人举行罢工', 'zhihu'),
            item('ceasefire', CEASEFIRE, 'zhihu'), item('tariff', TARIFF, 'zhihu')], stamp=stamp),
        'weibo': snapshot('weibo', [item('tariff', TARIFF, 'weibo')], stamp=stamp),
        'douyin': snapshot('douyin', [], stamp=stamp),
        'cls': snapshot('cls', [item('tariff', TARIFF, 'cls')], stamp=stamp),
        'fastbull': snapshot('fastbull', [], stamp=stamp),
        'xueqiu': snapshot('xueqiu', [item('NVDA', '英伟达', 'xueqiu')], stamp=stamp),
        'github': snapshot('github', [item('repo', 'example/research-agent', 'github')], stamp=stamp),
    }
    cached = deepcopy(first)
    for value in cached.values():
        value['updatedTime'] = stamp + 60_000
    changed = deepcopy(cached)
    changed['zhihu']['items'] = [item('tariff', TARIFF, 'zhihu'),
        item('ceasefire', CEASEFIRE, 'zhihu'), item('ports', '全球多个港口工人举行罢工', 'zhihu')]
    changed['weibo'] = (503, 'application/json', dict(error=True, message='synthetic temporary source outage'))
    changed['douyin']['items'] = [item('tariff', TARIFF, 'douyin'), item('denial', DENIAL, 'douyin')]
    return [first, cached, changed]


@contextmanager
def attention_http():
    with newsnow_http() as upstream:
        upstream.attention_rounds = rounds()
        upstream.observation_start = datetime.now(timezone.utc).replace(microsecond=0)-timedelta(minutes=40)
        def set_round(index):
            for source, response in upstream.attention_rounds[index].items():
                upstream.set(source, response)
            upstream.round_index = index
        upstream.set_round = set_round
        set_round(0)
        yield upstream


def profile(client, upstream, name='Attention journey'):
    response = client.post('/api/profiles', json=dict(name=name, provider='newsnow',
        categories=[], keywords_include=[], keywords_exclude=[], country='', language='',
        max_items=1200, extra=dict(base_url=upstream.base, newsnow_sources=SOURCES, filter_mode='rules')))
    assert response.status_code == 200, response.text
    return name


def execute(app, client, response):
    assert response.status_code == 202, response.text
    tid = response.json()['task_id']
    task = app.state.tasks.claim()
    assert task is not None and task.task_id == tid
    app.state.worker.execute(task)
    data = client.get('/api/tasks/' + tid + '/diagnostics').json()
    assert data['task']['status'] == 'succeeded', data
    return data


def collect_round(app, client, name):
    return execute(app, client, client.post('/api/tasks/collect', json={'profile_name': name}))


def collect_observed_round(app, client, name, upstream):
    """Use explicit synthetic observation instants while retaining real HTTP I/O.

    Twenty-minute intervals make comparisons meaningful and deterministic even
    when a complete QA journey executes within one wall-clock second.
    """
    stamp = (upstream.observation_start+timedelta(minutes=20*upstream.round_index)).isoformat().replace('+00:00', 'Z')
    with patch('news2douyin.collect.providers.newsnow._now', return_value=stamp):
        return collect_round(app, client, name)

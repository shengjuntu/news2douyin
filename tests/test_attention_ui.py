"""Attention discovery renders unknowns and saved snapshots without executing sources."""
import json
from pathlib import Path
from urllib.parse import urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape


TEMPLATES = Path(__file__).parents[1] / 'src/news2douyin/server/templates'


def render(**values):
    environment = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(['html']))
    environment.filters['urlencode'] = lambda value: urlencode(value) if isinstance(value, dict) else str(value)
    topic = dict(key='att_example', title='某地港口工人举行罢工', platform_count=2,
                 attention_score=61.3, rise_score=None, baseline_state='insufficient',
                 first_seen='2026-11-14T02:00:00Z', last_seen='2026-11-14T02:15:00Z',
                 item_count=3, selected_task='', summary={'platforms':[
                     {'platform':'zhihu','cached':True,'changed':False,
                      'continuity':'cached_content_repeat','observation_state':'observed'}]})
    context = dict(active='radar', filters={'q':'港口', 'period':'72h', 'order':'rising', 'watch':'w_ports'},
                   offset=30, limit=30, watches=[{'key':'w_ports', 'name':'港口物流'}],
                   attention_time=lambda value: value or '未知', open_topic='',
                   data={'items':[topic], 'has_more':True})
    context.update(values)
    return environment.get_template('attention_radar.html').render(**context)


def test_attention_cards_keep_unknown_rise_and_cached_observations_explicit():
    page = render()
    assert '2 个中文热榜平台' in page
    assert '升温基线不足' in page
    visible_warning = page.split('data-attention-notes="att_example">',1)[1].split('</div>',1)[0]
    assert '含缓存或重复快照，不视为新升温。' in visible_warning
    assert '关注排序分 61.3' in page and '升温排序分 0' not in page
    assert '第 2 页' in page and '总页数' not in page
    assert 'order=rising' in page and 'watch=w_ports' in page and 'offset=60' in page
    assert 'src="/research-assets/attention.js" defer' in page


def test_first_observation_is_not_mislabeled_as_repeated_content():
    topic = dict(key='first', title='首次观察', platform_count=1, attention_score=50,
        rise_score=None, baseline_state='insufficient', first_seen='', last_seen='',
        item_count=1, selected_task='', summary={'platforms':[
            {'platform':'zhihu','cached':False,'changed':False,
             'continuity':'first_observation','observation_state':'observed'}]})
    page = render(data={'items':[topic],'has_more':False})
    visible_warning = page.split('data-attention-notes="first">',1)[1].split('</div>',1)[0]
    assert '含缓存或重复快照' not in visible_warning
    assert '升温基线不足' in page


def test_source_failure_and_missing_observation_have_visible_unknown_warning():
    topic = dict(key='partial', title='观测不完整', platform_count=1, attention_score=50,
        rise_score=None, baseline_state='insufficient', first_seen='', last_seen='',
        item_count=1, selected_task='', summary={'platforms':[
            {'platform':'weibo','observation_state':'source_failed','changed':False,
             'continuity':'not_continuously_observed'},
            {'platform':'douyin','observation_state':'not_returned','changed':False,
             'continuity':'not_continuously_observed'}]})
    page = render(data={'items':[topic],'has_more':False})
    visible_warning = page.split('data-attention-notes="partial">',1)[1].split('</div>',1)[0]
    assert '部分来源失败，缺失观测为未知。' in visible_warning
    assert '部分来源本次未返回此议题，变化未知。' in visible_warning
    assert '热度为零' not in visible_warning
    assert '含缓存或重复快照' not in visible_warning


def test_stale_summary_preserves_history_without_calling_it_currently_valid():
    topic = dict(key='old', title='停止采集后的历史议题', platform_count=0,
        attention_score=None, rise_score=None, baseline_state='stale', first_seen='',
        last_seen='', item_count=4, selected_task='', summary={'platforms':[
            {'platform':'zhihu','observation_state':'stale','stale':True}]})
    page = render(data={'items':[topic],'has_more':False})
    assert '历史条目 4 个 · 等待新观测' in page
    assert '当前有效 4 个条目' not in page
    assert '观测已过期' in page
    assert '关注排序分 0' not in page and '升温排序分 0' not in page


def test_attention_page_escapes_titles_and_embedded_snapshot_json():
    value = '</script><img src=x onerror=bad()>'
    page = render(data={'items':[dict(key='x',title=value,platform_count=0,attention_score=None,
        rise_score=None,baseline_state='insufficient',first_seen='',last_seen='',item_count=1,
        selected_task='',summary={'notes':[value]})], 'has_more':False})
    assert '&lt;/script&gt;&lt;img src=x onerror=bad()&gt;' in page
    assert value not in page
    config = page.split('<script id="attention-page-data" type="application/json">',1)[1].split('</script>',1)[0]
    assert json.loads(config)['items'][0]['title'] == value
    assert '</script>' not in config
    assert '中文热榜覆盖待确认' in page


def test_existing_selection_is_primary_and_new_selection_is_explicit():
    topic = dict(key='x',title='港口罢工',platform_count=1,attention_score=4,rise_score=None,
        baseline_state='insufficient',first_seen='',last_seen='',item_count=1,
        selected_task='ct_example',summary={})
    page = render(data={'items':[topic], 'has_more':False},offset=0)
    assert 'href="/workbench/ct_example/selection">继续选题' in page
    assert 'data-attention-select="x">另起一个角度' in page
    assert 'data-attention-open="x">来源、趋势与关联' in page
    assert 'id="attention-selection-angle"' in page
    assert 'id="attention-detail-dialog"' in page


def test_attention_remains_inside_radar_with_three_material_views():
    page = render(data={'items':[], 'has_more':False})
    assert 'href="/radar?source=gdelt">全球事件线索' in page
    assert 'href="/radar?source=local">已整理事件' in page
    assert 'href="/radar?source=attention" class="on"' in page
    assert '焦点筛选仅匹配词项' in page
    assert '当前范围没有可显示的议题' in page


def test_deleted_watch_does_not_look_like_an_empty_verified_result():
    page = render(data={'items':[], 'has_more':False},
        unavailable_watch={'clear_url':'/radar?source=attention'})
    assert '所选焦点已删除或不可用' in page
    assert '请先调整焦点范围' in page

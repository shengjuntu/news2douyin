"""Rendering contracts for the small natural-language topic entry."""
import json
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape


TEMPLATES = Path(__file__).parents[1] / 'src/news2douyin/server/templates'


def render(**values):
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(['html']))
    env.filters['urlencode'] = lambda value: value
    context = dict(w=None, data={}, watches=[], counts={}, presets=[], seed='', seed_title='',
        indexing=0, graph_error='', graph_enabled=False, knowledge={'state':'unconfigured'},
        active='focus', intent_definition=None, tracking={}, changes=[], items=[], offset=0,
        total=0, reviews=[], branches=[], tags={}, order='recent', branch='', timeline=[],
        excluded=[], candidate_dates=[], action_labels={}, points=[], point_history=[],
        point_material={}, questions=[], summaries=[], content_tasks=[], paging='', back='/focus')
    context.update(values)
    return env.get_template('desk_focus.html').render(**context)


def test_new_topic_default_is_description_and_manual_editor_starts_closed():
    page = render()
    assert '<h1>焦点</h1>' in page and '新建焦点' in page
    assert 'id="topic-description"' in page and 'id="topic-generate"' in page
    assert 'id="topic-preview" hidden' in page
    assert 'id="desk-editor" class="card focus-manual"' in page
    assert 'id="desk-editor" class="card focus-manual" open' not in page
    assert page.count('data-topic-example=') == 4
    assert 'src="/research-assets/focus.js" defer' in page
    assert 'maxlength="4000"' in page


def test_existing_topic_retains_material_timeline_research_and_escaped_description():
    watch = SimpleNamespace(key='w_example',name='军援变化',kind='长期事件',scope='主题',dimensions='',
        exclude='',question='历史背景？',enabled=True,auto_research=False,review_days=7,
        next_review='',version=1)
    description = '</textarea><script>bad()</script>'
    page = render(w=watch,data=vars(watch),watches=[watch],intent_definition={
        'description':description,'spec':{'name':'军援变化','entities':[],'relations':[]},'method':'model'})
    assert description not in page
    assert '&lt;/textarea&gt;&lt;script&gt;bad()&lt;/script&gt;' in page
    assert 'id="focus-form"' not in page  # One source of truth for natural-language scopes.
    assert 'id="focus-options"' in page and 'id="focus-tracking"' in page
    assert 'id="tracking-refresh" hidden' in page
    assert '近期进展与来源' in page and '时间线与关系图' in page
    assert '长期问题与深度研究' in page and '生成焦点综述' in page
    assert '暂无匹配材料' in page and '删除焦点' in page
    assert 'data-intent-graph="saved"' in page


def test_config_round_trips_model_text_without_script_termination():
    value = '</script><img src=x onerror=alert(1)>'
    page = render(intent_definition={'description':value,'spec':{'name':value},'method':'rules'})
    config = page.split('<script id="focus-config" type="application/json">',1)[1].split('</script>',1)[0]
    payload = json.loads(config)
    assert payload['definition']['spec']['name'] == value
    assert '</script>' not in config



def test_materials_keep_reasons_in_closed_details_in_both_modes():
    watch = SimpleNamespace(key='w_example',name='军援变化', kind='长期事件', question='援助历史？',
        enabled=True, auto_research=False, next_review='', version=1, review_days=7, scope='军援',
        dimensions='', exclude='')
    event = SimpleNamespace(key='e_example', title='公开声明', seen='2026-11-19T14:00:00Z',
        evidence='旧证据标签', band='旧全局级别')
    context = dict(w=watch, data=vars(watch), items=[(event, '人工关联')], tags={event.key:'军援'})
    definition = {'description':'军援变化','spec':{'name':watch.name},'method':'model'}
    for value in (None, definition):
        page = render(intent_definition=value, **context)
        card = page.split('<article class="desk-pane">',1)[1].split('</article>',1)[0]
        visible, evidence = card.split('<details class="focus-decision"',1)
        assert '公开声明' in visible and '2026-11-19</time>' in visible
        assert '人工关联' not in visible and '<summary>查看收录依据</summary>' in evidence
        assert '人工关联' in evidence and ' open' not in evidence.split('>',1)[0]
        assert '旧证据标签' not in card and '旧全局级别' not in card
        assert 'data-check="e_example"' in card and 'data-hide="e_example"' in card

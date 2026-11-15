"""Source reporting, rather than CAMEO coding, is the visible selection material."""
from pathlib import Path
from urllib.parse import urlencode

from jinja2 import Environment,FileSystemLoader,select_autoescape

TEMPLATES=Path(__file__).parents[1]/'src/news2douyin/server/templates'


def render(report=None,**changes):
    e=dict(event_id='123456',title='ZUNI · 交战 · MOROCCO',coded_title='ZUNI · 交战 · MOROCCO',
        display_title='报道内容待补充',report_preview=report,content_status='ready' if report else 'missing',
        cameo_label='交战',event_root='19',actor1_name='ZUNI',actor2_name='MOROCCO',
        event_date='2026-11-14',last_seen='2026-11-14T12:30:00Z',action_country='ma',action_name='Morocco',
        observed_sources=1,observed_documents=1,source_url='https://reports.example/article',selected_task='')
    e.update(changes)
    env=Environment(loader=FileSystemLoader(TEMPLATES),autoescape=select_autoescape(['html']))
    env.filters['urlencode']=lambda v:urlencode(v) if isinstance(v,dict) else str(v)
    return env.get_template('gdelt_events.html').render(active='radar',filters=dict(q='',country='',cameo='',period='all',
        min_sources=0,watch='',preset='',order='recent'),limit=30,offset=0,countries={'ma':'摩洛哥'},cameos={'19':'交战'},
        data=dict(items=[e],has_more=False),watches=[],presets=[],gdelt_time=lambda value:value)


def report(**changes):
    data=dict(key='a'*64,title='某港口工人举行罢工，货运等待时间延长',
        excerpt='来源报道称，当地码头工人要求改善合同条件；港口部门正在安排临时货运计划。',domain='reports.example',
        source_url='https://reports.example/article',language_info=dict(display='中文 · 来源声明'),
        translation=dict(kind='source_preview',key='a'*64,scope='document'))
    data.update(changes);return data


def test_real_headline_and_excerpt_precede_the_secondary_coding():
    page=render(report())
    visible=page.split('data-gdelt-event="123456"',1)[1].split('</article>',1)[0]
    headline=visible.split('data-gdelt-report-title>',1)[1].split('</h2>',1)[0]
    assert headline=='某港口工人举行罢工，货运等待时间延长'
    assert '货运计划' in visible and 'ZUNI · 交战 · MOROCCO' in visible
    assert visible.index('货运计划')<visible.index('编码线索')
    assert '中文 · 来源声明' in visible and '中文译读' in visible
    assert '>形成选题</button>' in visible


def test_missing_content_does_not_turn_actor_codes_into_a_news_headline():
    page=render()
    visible=page.split('data-gdelt-event="123456"',1)[1].split('</article>',1)[0]
    assert 'data-gdelt-report-title>报道内容待补充</h2>' in visible
    assert '尚未取得可读的报道标题和摘要' in visible
    assert 'data-gdelt-enrich="123456">补充报道' in visible
    assert '>保留线索</button>' in visible
    assert 'data-gdelt-translate-card' not in visible
    assert 'data-gdelt-report-title>ZUNI' not in visible


def test_reporting_is_escaped_and_a_title_only_lead_is_not_called_fulltext():
    malicious='<img src=x onerror=alert(1)>'
    page=render(report(title=malicious,excerpt=''))
    assert malicious not in page and '&lt;img src=x onerror=alert(1)&gt;' in page
    assert '尚无摘要，可先查看来源报道。' in page
    assert '已核实事件摘要' not in page
    assert '相关报道可能涉及多个事件，编码仍需对照原文。' in page


def test_content_enrichment_is_an_explicit_page_action_and_selected_work_continues():
    page=render(report(),selected_task='topic_saved')
    assert 'id="gdelt-enrich-page"' in page and '补充本页报道' in page
    assert 'href="/workbench/topic_saved/selection">继续这项选题' in page
    assert '另起一个角度' in page
    assert '自动抓取' not in page

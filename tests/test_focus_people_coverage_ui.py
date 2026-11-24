from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from news2douyin.server import desk


@pytest.fixture
def views():
    return Environment(loader=FileSystemLoader(Path(desk.__file__).with_name('templates')), autoescape=True)


def test_activity_view_shows_source_and_version_counts_in_their_own_units(views):
    window = dict(sampled_events=48, sampled_sources=48, checked_sources=5, ambiguous_legacy_sources=43,
                  date_from='2024-01-02', date_to='2024-01-31',
                  extraction=dict(versions=5, unqueued=3, repair_candidates=3, statements_without_person=2,
                                  examples_without_person=[
                                      dict(title='会面 <报道>', source_path='/articles/n?revision=1#m-1'),
                                      dict(title='错误出处', source_path='javascript:invalid')]))
    html = views.get_template('focus_people.html').render(w={'key':'w_votes'},
             people=dict(window=window, days=30, until=window['date_to'], empty_reason='本次未找到人物动作'))
    assert 'data-count="checked_sources">5</span> / <span data-count="sampled_sources">48</span>' in html
    assert 'data-metric="ambiguous_legacy_sources">旧来源版本有歧义 <b>43</b>' in html
    assert 'data-metric="extraction.unqueued">无抽取记录 <b>3</b> 个版本' in html
    assert 'data-metric="extraction.statements_without_person">未识别出可核实人物的陈述 <b>2</b> 条' in html
    assert html.count('data-people-repair>') == 1
    assert 'data-days="30" data-until="2024-01-31"' in html and '/api/focus/w_votes/people/extraction' in html
    assert '本次未找到人物动作' in html
    assert '<a href="/articles/n?revision=1#m-1">会面 &lt;报道&gt;</a>' in html
    assert 'javascript:invalid' not in html

    window['extraction']['repair_candidates'] = 0
    html = views.get_template('focus_people.html').render(w={'key':'w_votes'}, people=dict(window=window))
    assert 'data-people-repair hidden' in html


def test_both_focus_graph_lenses_rebuild_the_same_official_graph(views):
    page = views.get_template('graph_explorer.html')
    selected = {'key':'w_votes', 'name':'俄乌战争'}
    context = dict(mode='focus', selected=selected, choices=[selected], days=90,
                   until='2024-01-31', endpoint='/api/graphs', day_options=[30,90,365])
    for lens in ('all','people'):
        html = page.render(**context, lens=lens)
        assert 'data-rebuild="/api/graphs/focus/w_votes/rebuild"' in html
        assert '重建焦点图' in html and 'data-people-coverage' not in html
        assert '<option value="90" selected>' in html and 'value="2024-01-31"' in html

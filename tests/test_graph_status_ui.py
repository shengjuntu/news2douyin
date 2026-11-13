"""Projection, index preparation and analysis freshness remain distinct to users."""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


TEMPLATES=Path(__file__).parents[1]/'src/news2douyin/server/templates'


def render(state, *, page='graph_status.html'):
    environment=Environment(loader=FileSystemLoader(TEMPLATES),autoescape=select_autoescape(['html']))
    return environment.get_template(page).render(knowledge=state,state=state,active='knowledge')


def status(**values):
    result=dict(state='pending',pending=442993,analyzed_at='',count=0,truncated=False,
        indexes=dict(state='checking',ready=2,required=10,current='Article.id',error='',checked_at=''),
        gc={},projection={},extraction={},methods={},indexing=0)
    result.update(values)
    return result


def test_index_preparation_does_not_claim_projection_or_ranking_ready():
    page=render(status())
    assert '图索引准备中' in page
    assert '图投影已同步' not in page
    assert '图分析尚未完成' in page
    assert '已投影的节点不代表排名已刷新' in page
    assert '本轮完成后更新' not in page


def test_existing_analysis_is_visible_alongside_real_projection_backlog():
    page=render(status(indexes={'state':'ready'},analyzed_at='2026-11-13T02:30:00Z',count=5000,truncated=True))
    assert '图投影中 · 442993 项待同步' in page
    assert '图分析更新于 2026-11-13T02:30:00Z' in page
    assert '分析范围为最近最多 5000 个事件' in page
    assert '知识图已更新' not in page


def test_maintenance_error_uses_separate_retry_and_preserves_unknown_counts():
    page=render(status(pending=None,indexing=None,
        indexes=dict(state='error',ready=None,required=None,current='Article.id',error='<script>index()</script>',checked_at=''),
        gc=dict(state='error',scanned=None,deleted=None,error='维护暂不可用')),page='knowledge.html')
    assert 'id="retry-graph-maintenance" class="ghost" type="button" >' in page
    assert '&lt;script&gt;index()&lt;/script&gt;' in page
    assert '<script>index()</script>' not in page
    assert '待同步图记录：暂不可用' in page
    assert '待建立事件索引：暂不可用' in page
    assert '清理进度尚未记录' in page
    assert '本轮已检查 0' not in page
    assert 'src="/research-assets/graph-maintenance.js" defer' in page


def test_healthy_state_hides_retry_and_keeps_maintenance_collapsed():
    page=render(status(state='ready',pending=0,analyzed_at='2026-11-13T02:30:00Z',count=5000,
        indexes=dict(state='ready',ready=10,required=10),gc=dict(state='complete',scanned=150,deleted=2)),page='knowledge.html')
    assert '图索引已就绪 · 10 / 10' in page
    assert '图投影已同步' in page
    assert 'id="retry-graph-maintenance" class="ghost" type="button" hidden' in page
    assert '<details class="work-actions"><summary>索引与维护详情</summary>' in page
    assert '本轮已检查 150 个节点 · 清理 2 个无引用节点' in page

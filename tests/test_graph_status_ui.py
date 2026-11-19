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
    assert '累计已检查 150 个节点 · 清理 2 个无引用节点' in page


def test_event_and_topic_statistics_do_not_share_a_per_event_average():
    metrics=dict(totals=dict(events=40,watches=0),by_kind={
        'event':dict(totals=dict(acknowledged=40,attempts=40,partial=0),
            latency_ms=dict(mean=20,p95=40),recent=dict(window_seconds=300,rate_per_second=.133333)),
        'watch':dict(totals=dict(acknowledged=0,attempts=4,partial=4),
            latency_ms=dict(mean=100000,p95=110000),recent=dict(window_seconds=300,rate_per_second=0))})
    page=render(status(projection=metrics),page='knowledge.html')
    assert '事件投影：已确认 40 项 · 已尝试 40 次 · 单次平均 0.02 秒 / P95 0.04 秒' in page
    assert '焦点投影：已确认 0 项 · 已尝试 4 次 · 已完成分批 4 次 · 单次平均 100.0 秒 / P95 110.0 秒' in page
    assert '本次运行已确认事件投影 40 项 · 焦点投影 0 项' in page
    assert '分批完成不代表整个焦点已同步' in page
    assert '近 300 秒确认率 0.133 项/秒' in page


def test_paused_gc_keeps_real_scan_progress_and_offers_resume():
    page=render(status(gc=dict(state='paused',enabled=True,paused=True,scanned=4432,deleted=5,
        complete_labels=2,labels=7,diagnostics={})),page='knowledge.html')
    assert '图清理：已暂停' in page
    assert '累计已检查 4432 个节点' in page
    assert '已保留扫描位置' in page
    assert 'id="pause-graph-gc" class="ghost" type="button" hidden' in page
    assert 'id="resume-graph-gc" class="ghost" type="button" >' in page
    assert '图清理：本轮完成' not in page


def test_disabled_gc_hides_controls_and_diagnostics_are_readable():
    page=render(status(gc=dict(state='disabled',enabled=False,scanned=None,deleted=None,
        diagnostics=dict(last_failure=dict(phase='highwater',elapsed_ms=505,timeout=True,error='TimeoutError')))),page='knowledge.html')
    assert '图清理：服务配置已关闭' in page
    assert 'id="pause-graph-gc" class="ghost" type="button" hidden' in page
    assert 'id="resume-graph-gc" class="ghost" type="button" hidden' in page
    assert '最近维护异常：扫描上界检查 · 0.51 秒 · 超时' in page
    assert "{'phase':" not in page


def test_topic_progress_reports_batches_without_inventing_completion_percentage():
    page=render(status(focus_projection=dict(active=10,limited=True,topics=[
        dict(watch='private-watch-key',phase='events',scanned=400,changed=7,removed=0,batches=12)])),
        page='knowledge.html')
    assert '已记录至少 10 个未完成焦点' in page
    assert '最近批次：同步事件材料 · 已检查 400 项 · 写入 7 项 · 移除 0 项 · 完成 12 批' in page
    # User-facing progress does not display an opaque topic ID or assume that
    # a batch count represents completion of all pending material.
    line=page.split('id="knowledge-topic-progress"',1)[1].split('</p>',1)[0]
    assert 'private-watch-key' not in line and '%' not in line

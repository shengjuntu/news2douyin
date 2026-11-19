/* Read-only status refresh. Expensive work is always queued by the server. */
(() => {
  'use strict';
  const config = document.getElementById('knowledge-status-config');
  if (!config) return;
  let disposed = false, timer = null, failures = 0, active = null;
  const byId = id => document.getElementById(id);
  const number = value => typeof value === 'number' && Number.isFinite(value);
  const safeText = value => typeof value === 'string' ? value : '';
  function text(id, value) { const node = byId(id); if (node) node.textContent = value; }
  function error(id, value) { const node = byId(id); if (node) { node.hidden = !value; node.textContent = value || ''; } }
  const seconds = value => (value / 1000).toLocaleString('zh-CN', {maximumFractionDigits:2}) + ' 秒';
  function projectionLine(label, metric) {
    const totals = metric?.totals || {}, latency = metric?.latency_ms || {}, recent = metric?.recent || {};
    if (!number(totals.acknowledged)) return label + '：等待处理记录';
    let line = label + '：已确认 ' + totals.acknowledged + ' 项 · 已尝试 ' + (number(totals.attempts) ? totals.attempts : '未知') + ' 次';
    if (number(totals.partial) && totals.partial > 0) line += ' · 已完成分批 ' + totals.partial + ' 次';
    if (number(latency.mean)) line += ' · 单次平均 ' + seconds(latency.mean) + (number(latency.p95) ? ' / P95 ' + seconds(latency.p95) : '');
    if (number(recent.rate_per_second) && number(recent.window_seconds)) line += ' · 近 ' + Math.round(recent.window_seconds) + ' 秒确认率 ' + recent.rate_per_second.toLocaleString('zh-CN', {maximumFractionDigits:3}) + ' 项/秒';
    return line;
  }
  const extractionLabels = {baseline:'等待规则抽取',rule_running:'规则抽取中',model_queued:'等待模型抽取',model_running:'模型抽取中',model_wait:'模型重试等待',ready:'本轮完成',metadata_only:'仅元数据，等待原文',error:'抽取失败'};
  function render(state) {
    if (disposed || !state || typeof state !== 'object') return;
    const index = state.indexes || {}, gc = state.gc || {}, projection = state.projection || {};
    let primary;
    if (state.state === 'unconfigured') primary = '知识图尚未连接';
    else if (index.state === 'error') primary = '图索引检查需要重试';
    else if (['checking','building'].includes(index.state)) primary = '图索引准备中';
    else if (state.state === 'error') primary = '知识图暂时不可用，后台正在重试';
    else if (number(state.pending) && state.pending > 0) primary = '图投影中 · ' + state.pending + ' 项待同步';
    else if (state.state === 'ready') primary = '图投影已同步';
    else primary = '正在检查图处理状态';
    let analysis;
    if (state.state === 'unconfigured') analysis = ' · 当前列表是本地材料，图排序与关联尚未就绪。';
    else if (safeText(state.analyzed_at)) analysis = ' · 图分析更新于 ' + state.analyzed_at + (number(state.count) ? ' · ' + state.count + ' 个事件' : '') + (state.truncated ? ' · 分析范围为最近最多 5000 个事件' : '') + '。';
    else analysis = ' · 图分析尚未完成；已投影的节点不代表排名已刷新。';
    for (const node of document.querySelectorAll('[data-graph-status]')) {
      node.classList.toggle('warn', state.state !== 'ready' || ['checking','building','error'].includes(index.state));
      const heading = node.querySelector('[data-graph-primary]'), stamp = node.querySelector('[data-graph-analysis]');
      if (heading) heading.textContent = primary;
      if (stamp) stamp.textContent = analysis;
    }
    const indexLabel = {ready:'图索引已就绪',error:'图索引检查需要重试',checking:'图索引准备中',building:'图索引准备中',unconfigured:'图索引等待连接'};
    text('knowledge-indexes', (indexLabel[index.state] || '图索引状态待检查') + (number(index.ready) && number(index.required) ? ' · ' + index.ready + ' / ' + index.required : ''));
    text('knowledge-pending', '待同步图记录：' + (number(state.pending) ? state.pending : '暂不可用'));
    text('knowledge-indexing', '待建立事件索引：' + (number(state.indexing) ? state.indexing : '暂不可用'));
    text('knowledge-analysis', '图分析：' + (safeText(state.analyzed_at) || '尚未完成'));
    text('knowledge-index-current', safeText(index.current) ? '当前检查：' + index.current : '自动补建缺失索引。');
    text('knowledge-index-checked', '最近索引检查：' + (safeText(index.checked_at) || '暂无记录'));
    error('knowledge-index-error', safeText(index.error));
    const projectionTotals = projection.totals || {};
    text('knowledge-projection-completed', number(projectionTotals.events) ? '本次运行已确认事件投影 ' + projectionTotals.events + ' 项' + (number(projectionTotals.watches) ? ' · 焦点投影 ' + projectionTotals.watches + ' 项' : '') : '本次运行投影完成量：暂无记录');
    text('knowledge-projection-event', projectionLine('事件投影', projection.by_kind?.event));
    text('knowledge-projection-watch', projectionLine('焦点投影', projection.by_kind?.watch));
    const topicProgress = state.focus_projection || {}, topicBatch = Array.isArray(topicProgress.topics) ? topicProgress.topics[0] : null;
    const topicPhaseLabel = {adopt:'接续已有图',header:'同步关注范围',points:'同步知识提纲',events:'同步事件材料',point_material:'关联提纲材料',reviews:'同步研究任务',point_reviews:'关联提纲研究',questions:'同步研究问题',claims:'同步研究结论',answers:'关联研究结论',reports:'同步研究报告',prune:'清理已移除材料',finish:'确认本轮同步',complete:'本轮完成'};
    text('knowledge-topic-progress', topicBatch ? '焦点分批进度：已记录' + (topicProgress.limited ? '至少 ' : ' ') + (number(topicProgress.active) ? topicProgress.active : '未知') + ' 个未完成焦点。最近批次：' + (topicPhaseLabel[topicBatch.phase] || '等待下一批') + ' · 已检查 ' + (number(topicBatch.scanned) ? topicBatch.scanned : '未知') + ' 项 · 写入 ' + (number(topicBatch.changed) ? topicBatch.changed : '未知') + ' 项 · 移除 ' + (number(topicBatch.removed) ? topicBatch.removed : '未知') + ' 项 · 完成 ' + (number(topicBatch.batches) ? topicBatch.batches : '未知') + ' 批' : '焦点分批进度：暂无未完成的分批记录');
    const gcLabel = {complete:'本轮完成',sweeping:'分批检查中',running:'分批检查中',idle:'等待下一轮',pending:'等待检查',paused:'已暂停',disabled:'服务配置已关闭',error:'需要重试'};
    text('knowledge-gc', '图清理：' + (gcLabel[gc.state] || '尚无维护记录') + (safeText(gc.last_run) ? ' · ' + gc.last_run : ''));
    text('knowledge-gc-counts', number(gc.scanned) ? '累计已检查 ' + gc.scanned + ' 个节点' + (number(gc.deleted) ? ' · 清理 ' + gc.deleted + ' 个无引用节点' : '') : '清理进度尚未记录。');
    const pauseGc = byId('pause-graph-gc'), resumeGc = byId('resume-graph-gc');
    if (pauseGc) pauseGc.hidden = ['unconfigured','disabled','paused'].includes(gc.state);
    if (resumeGc) resumeGc.hidden = gc.state !== 'paused';
    text('knowledge-gc-control-note', gc.state === 'disabled' ? '自动清理已在服务配置中关闭。' : gc.state === 'paused' ? '已保留扫描位置，事件和焦点继续同步。' : '暂停时保存当前批次进度。');
    const diagnostic = gc.diagnostics || {}, failure = diagnostic.last_failure || {}, lastGc = safeText(failure.phase) ? failure : diagnostic.last || {};
    const gcPhaseLabel = {plan:'执行计划检查',highwater:'扫描上界检查',read:'读取批次',delete:'清理批次'};
    text('knowledge-gc-phase', safeText(lastGc.phase) ? '最近维护' + (safeText(failure.phase) ? '异常' : '阶段') + '：' + (gcPhaseLabel[lastGc.phase] || '维护批次') + (number(lastGc.elapsed_ms) ? ' · ' + seconds(lastGc.elapsed_ms) : '') + (lastGc.timeout ? ' · 超时' : safeText(lastGc.error) ? ' · 未完成' : '') : '');
    error('knowledge-gc-error', safeText(gc.error));
    const retry = byId('retry-graph-maintenance');
    if (retry) retry.hidden = index.state !== 'error' && gc.state !== 'error';
    if (state.extraction && typeof state.extraction === 'object') {
      const target = byId('knowledge-extraction');
      if (target) {
        const rows = Object.entries(state.extraction).filter(([, value]) => number(value));
        target.replaceChildren();
        for (const [key, value] of rows) { const row = document.createElement('p'); row.textContent = (extractionLabels[key] || key) + '：' + value; target.append(row); }
        if (!rows.length) { const row = document.createElement('p'); row.textContent = '尚无原文版本。'; target.append(row); }
      }
    }
    const methods = state.methods || {};
    text('knowledge-methods', '规则完成 ' + (number(methods.rules) ? methods.rules : '暂无记录') + ' · 模型完成 ' + (number(methods.model) ? methods.model : '暂无记录'));
  }
  async function request(path, method = 'GET') {
    const controller = new AbortController(), limit = setTimeout(() => controller.abort(), 15000);
    active = controller;
    try {
      const response = await fetch(path, {method, cache:'no-store', signal:controller.signal});
      let data;
      try { data = await response.json(); } catch (_) { throw new Error('服务返回了无法读取的结果，请稍后重试。'); }
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求未完成，请稍后重试。');
      return data;
    } catch (err) {
      if (err.name === 'AbortError') throw new Error('状态读取超时，稍后会自动重试。');
      throw err;
    } finally { clearTimeout(limit); if (active === controller) active = null; }
  }
  async function refresh() {
    if (disposed) return;
    try {
      if (!document.hidden) {
        render(await request('/api/knowledge/status')); failures = 0;
        const message = byId('knowledge-message');
        if (message?.dataset.refreshError === 'true') { message.textContent = ''; delete message.dataset.refreshError; }
      }
    } catch (_) {
      failures += 1; text('knowledge-message', '暂时无法刷新进度；保留上次状态，后台稍后重试。');
      const message = byId('knowledge-message'); if (message) message.dataset.refreshError = 'true';
    }
    if (!disposed) timer = setTimeout(refresh, Math.min(30000, 6000 * (failures + 1)));
  }
  for (const [id, path, message] of [
    ['retry-extraction','/api/knowledge/retry-extraction','已提交抽取重试，后台将继续处理。'],
    ['retry-graph-maintenance','/api/desk/graph-maintenance/retry','已提交图维护检查，不会重新投影全部新闻。'],
    ['pause-graph-gc','/api/desk/graph-maintenance/pause','自动清理已暂停，事件和焦点继续同步。'],
    ['resume-graph-gc','/api/desk/graph-maintenance/resume','已继续自动清理，后台将从保存的扫描位置处理。'],
    ['rebuild-graph','/api/knowledge/rebuild','已提交图重建，后台将重新投影保存的证据。']
  ]) {
    const button = byId(id);
    if (!button) continue;
    button.addEventListener('click', async () => {
      if (button.disabled) return;
      button.disabled = true;
      const feedback = byId('knowledge-message'); if (feedback) delete feedback.dataset.refreshError;
      try { await request(path, 'POST'); text('knowledge-message', message); render(await request('/api/knowledge/status')); }
      catch (err) { text('knowledge-message', err.message); }
      finally { if (!disposed) button.disabled = false; }
    });
  }
  try { render(JSON.parse(config.textContent)); } catch (_) { /* Server-rendered state remains available. */ }
  timer = setTimeout(refresh, 6000);
  window.addEventListener('pagehide', () => { disposed = true; clearTimeout(timer); if (active) active.abort(); }, {once:true});
})();

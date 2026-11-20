(() => {
  'use strict';

  const apiRoot = '/api/causal/investigations';
  const pageRoot = '/radar/causes';
  const el = id => document.getElementById(id);
  const form = el('causal-form');
  const tree = el('causal-tree');
  const history = el('causal-history');
  const repairForm = el('causal-repair-form');
  const repairList = el('causal-repair-list');
  const repairMessage = 'causal-repair-message';
  const config = JSON.parse(el('causal-config').textContent);
  const stateLabels = {pending:'排队中', running:'查找中', failed:'查找失败'};
  const timeLabels = {earlier:'有更早公开证据', unknown:'先后未明', root:'待查事件'};
  const repairLabels = {
    queued:'已排队', running:'补采中', partial:'仍有缺口', failed:'失败', completed:'已完成',
    submit_digest:'等待重算', awaiting_digest:'正在重算', completed_partial:'已用部分批次重算'
  };
  let current = null;
  let pollingKey = config.task_key || '';
  let pollTimer, pollRequest, submitting = false;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function link(path, label) {
    const element = node('a', label);
    element.href = path;
    return element;
  }

  function originalUrl(value) {
    try {
      const url = new URL(value);
      return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : '';
    } catch {
      return '';
    }
  }

  function localPath(path) {
    return typeof path === 'string' && path.startsWith('/') && !path.startsWith('//') && !/[\\\s]/.test(path);
  }

  function localLink(id, path) {
    const target = el(id), valid = localPath(path);
    target.hidden = !valid;
    if (valid) target.href = path;
    else target.removeAttribute('href');
  }

  function investigationPath(key) {
    return pageRoot + '?investigation=' + encodeURIComponent(key);
  }

  function withBack(path, key) {
    const [base, fragment] = path.split('#', 2);
    return base + (base.includes('?') ? '&' : '?') +
      'back=' + encodeURIComponent(investigationPath(key)) + (fragment ? '#' + fragment : '');
  }

  function message(value = '', error = false, id = 'causal-message') {
    const target = el(id);
    target.textContent = value;
    target.dataset.error = String(error);
  }

  async function request(path, {payload, signal} = {}) {
    const response = await fetch(path, {
      method: payload ? 'POST' : 'GET',
      headers: payload ? {'Content-Type':'application/json'} : {},
      body: payload ? JSON.stringify(payload) : undefined,
      cache: 'no-store',
      signal
    });
    const data = await response.json().catch(() => null);
    if (response.ok && data) return data;
    const detail = data?.detail;
    const error = new Error(typeof detail === 'string' ? detail :
      Array.isArray(detail) ? detail.map(item => item.msg).filter(Boolean).join('；') : '暂时无法完成请求，请重试。');
    error.status = response.status;
    throw error;
  }

  function disclosure(label, key, open) {
    const details = node('details');
    details.dataset.disclosure = key;
    details.open = open.has(key);
    details.append(node('summary', label));
    return details;
  }

  function showEvidence(item, open) {
    if (!item.sources?.length) return null;
    const details = disclosure('查看证据（' + item.sources.length + '）', 'source:' + item.key, open);
    details.className = 'causal-evidence';
    const list = node('ul');
    for (const source of item.sources) {
      const row = node('li');
      const url = originalUrl(source.url);
      if (url) {
        const anchor = link(url, source.title || '打开原文');
        anchor.target = '_blank';
        anchor.rel = 'noopener noreferrer';
        row.append(anchor);
      } else if (source.title) {
        row.append(node('span', source.title));
      }
      if (source.published_at) row.append(node('small', ' · 发布 ' + source.published_at, 'muted'));
      if (source.quote) row.append(node('blockquote', source.quote));
      if (row.childNodes.length) list.append(row);
    }
    details.append(list);
    return details;
  }

  function renderTree(items, caseKey) {
    const open = new Set([...tree.querySelectorAll('details[data-disclosure][open]')].map(item => item.dataset.disclosure));
    const focused = document.activeElement?.dataset?.expand;
    const siblings = new Map();
    for (const item of items) {
      const parent = item.parent_key || '';
      if (!siblings.has(parent)) siblings.set(parent, []);
      siblings.get(parent).push(item);
    }
    for (const group of siblings.values()) {
      group.sort((left, right) => Number(right.time_status === 'earlier') - Number(left.time_status === 'earlier') ||
        (right.score || 0) - (left.score || 0) || Number(Boolean(right.beam_selected)) - Number(Boolean(left.beam_selected)));
    }

    function renderList(group, depth) {
      const list = node('ol', undefined, depth ? 'causal-tree causal-branches' : 'causal-tree');
      for (const item of group) {
        const entry = node('li');
        const card = node('article', undefined, 'causal-node');
        card.id = 'cause-' + encodeURIComponent(item.key);
        card.append(node('h3', item.text));
        const meta = [timeLabels[item.time_status], item.time_status !== 'root' && item.occurred_at && '发生 ' + item.occurred_at,
          item.published_at && '报道 ' + item.published_at].filter(Boolean);
        if (meta.length) card.append(node('p', meta.join(' · '), 'causal-node-meta'));
        if (item.reason) card.append(node('p', item.reason));
        const evidence = showEvidence(item, open);
        if (evidence) card.append(evidence);

        const actions = node('div', undefined, 'row causal-node-actions');
        if (item.event_key) actions.append(link(withBack('/events/' + encodeURIComponent(item.event_key), caseKey), '查看整理事件'));
        if (stateLabels[item.job_state]) actions.append(node('span', stateLabels[item.job_state]));
        if (!['pending', 'running'].includes(item.job_state)) {
          const label = {failed:'重试查找', ready:'重新查找'}[item.job_state] || '沿此线索查找';
          const button = node('button', label, 'ghost');
          button.type = 'button';
          button.dataset.expand = item.key;
          button.setAttribute('aria-label', label + '：' + item.text.slice(0, 100));
          actions.append(button);
        }
        if (actions.childNodes.length) card.append(actions);
        if (item.job_error) card.append(node('p', item.job_error, 'small causal-node-error'));

        const branches = siblings.get(item.key) || [];
        if (!branches.length && item.job_state === 'ready') {
          card.append(node('p', '暂无可继续追查的候选。', 'small muted'));
        }
        entry.append(card);
        if (branches.length) entry.append(renderList(branches.slice(0, 3), depth + 1));
        if (branches.length > 3) {
          const more = disclosure('查看其余 ' + (branches.length - 3) + ' 条', 'branches:' + item.key, open);
          more.className = 'causal-other';
          more.append(renderList(branches.slice(3), depth + 1));
          entry.append(more);
        }
        list.append(entry);
      }
      return list;
    }

    tree.replaceChildren(renderList(siblings.get('') || [], 0));
    if (focused) [...tree.querySelectorAll('[data-expand]')].find(button => button.dataset.expand === focused)?.focus();
  }

  function setControls() {
    el('causal-submit').disabled = submitting;
    el('causal-repair-submit').disabled = submitting;
    el('causal-repair-refresh').disabled = submitting;
    for (const button of repairList.querySelectorAll('[data-retry-day]')) button.disabled = submitting;
    for (const button of tree.querySelectorAll('[data-expand]')) button.disabled = submitting || current.working;
  }

  function renderRepairs(repairs) {
    repairList.replaceChildren();
    repairList.hidden = !repairs.length;
    el('causal-repair-refresh').hidden = !repairs.length;
    for (const repair of repairs) {
      const item = node('li');
      const label = repair.day + ' · ' + (repairLabels[repair.status] || '状态待核');
      item.append(localPath(repair.archive_path) ? link(repair.archive_path, label) : node('span', label));
      if (repair.status === 'failed' || repair.status === 'partial') {
        const retry = node('button', '重试补采', 'ghost');
        retry.type = 'button';
        retry.dataset.retryDay = repair.day;
        item.append(' ', retry);
      }
      repairList.append(item);
    }
  }

  function render(data) {
    const changed = !current || JSON.stringify(current.nodes) !== JSON.stringify(data.nodes);
    const newCase = current?.key !== data.key;
    const changedRepairs = newCase || JSON.stringify(current.coverage?.repairs) !== JSON.stringify(data.coverage?.repairs);
    current = data;
    pollingKey = data.key;
    el('causal-result').hidden = false;
    el('causal-progress').textContent = data.working ? '查找中' :
      data.nodes.some(item => item.job_state === 'failed') ? '有分支待重试' : '本轮已结束';
    const anchor = el('causal-anchor');
    anchor.hidden = !data.event_at;
    anchor.textContent = data.event_at ? '检索时间 ' + data.event_at : '';
    el('causal-coverage').textContent = data.coverage?.message || '';
    localLink('causal-archive', data.coverage?.archive_path);
    const eventResearch = data.research_path?.startsWith('/events/');
    localLink('causal-research', eventResearch ? withBack(data.research_path, data.key) : data.research_path);
    el('causal-research').textContent = eventResearch ? '补充检索' : '研究助手设置';
    if (newCase) {
      repairForm.elements.day.value = data.coverage?.suggested_day || '';
      message('', false, repairMessage);
    }
    if (changedRepairs) renderRepairs(data.coverage?.repairs || []);
    if (changed) renderTree(data.nodes, data.key);
    setControls();
  }

  function stopPolling() {
    clearTimeout(pollTimer);
    pollRequest?.abort();
    pollRequest = null;
  }

  function schedulePolling(delay = 2300) {
    clearTimeout(pollTimer);
    if (pollingKey && !document.hidden && !submitting && (!current || current.working)) {
      pollTimer = setTimeout(refreshCase, delay);
    }
  }

  async function refreshCase(feedback = 'causal-message') {
    if (!pollingKey || submitting || pollRequest || document.hidden) return;
    const controller = new AbortController();
    pollRequest = controller;
    const timeout = setTimeout(() => controller.abort(), 12000);
    let delay;
    try {
      const data = await request(apiRoot + '/' + encodeURIComponent(pollingKey), {signal:controller.signal});
      if (controller !== pollRequest) return;
      render(data);
      message('', false, feedback);
    } catch (error) {
      if (controller !== pollRequest) return;
      message(error.name === 'AbortError' ? '读取进度超时，请稍后重试。' : error.message, true, feedback);
      if (error.status === 404 || error.status === 422) pollingKey = '';
      delay = 6000;
    } finally {
      clearTimeout(timeout);
      if (controller === pollRequest) {
        pollRequest = null;
        schedulePolling(delay);
      }
    }
  }

  async function changeCase(path, payload, create, feedback = 'causal-message') {
    if (submitting) return;
    stopPolling();
    submitting = true;
    setControls();
    message('正在提交…', false, feedback);
    try {
      const data = await request(path, {payload, signal:AbortSignal.timeout(15000)});
      render(data);
      message(feedback === repairMessage ? '已提交补采。' : '', false, feedback);
      if (create) {
        window.history.replaceState(null, '', investigationPath(data.key));
        el('causal-result-title').focus();
      }
      if (history.open) loadHistory();
    } catch (error) {
      const timeout = feedback === repairMessage ? '提交超时，请刷新补采状态。' : '提交超时，请在查找记录中核对。';
      message(error.name === 'TimeoutError' ? timeout : error.message, true, feedback);
    } finally {
      submitting = false;
      setControls();
      schedulePolling();
    }
  }

  form.addEventListener('submit', event => {
    event.preventDefault();
    const values = new FormData(form);
    const text = String(values.get('text') || '').trim();
    if (!text) {
      message('请填写事件描述。', true);
      return;
    }
    changeCase(apiRoot, {
      text, event_at:String(values.get('event_at') || '').trim(),
      event_key:values.get('event_key') || '', automatic:values.has('automatic')
    }, true);
  });
  tree.addEventListener('click', event => {
    const button = event.target.closest('button[data-expand]');
    if (button && current) {
      changeCase(apiRoot + '/' + encodeURIComponent(current.key) + '/expand',
        {node_key:button.dataset.expand, automatic:false}, false);
    }
  });

  function requestRepair(day, retry = false) {
    if (current) changeCase(apiRoot + '/' + encodeURIComponent(current.key) + '/archive',
      {day, retry}, false, repairMessage);
  }
  repairForm.addEventListener('submit', event => {
    event.preventDefault();
    requestRepair(repairForm.elements.day.value);
  });
  repairList.addEventListener('click', event => {
    const retry = event.target.closest('button[data-retry-day]');
    if (retry) requestRepair(retry.dataset.retryDay, true);
  });
  el('causal-repair-refresh').addEventListener('click', () => refreshCase(repairMessage));

  async function loadHistory() {
    const target = el('causal-history-list');
    target.textContent = '读取查找记录…';
    try {
      const data = await request(apiRoot, {signal:AbortSignal.timeout(12000)});
      const list = node('ul');
      for (const item of data.investigations) {
        const row = node('li');
        const label = item.text.length > 100 ? item.text.slice(0, 100) + '…' : item.text;
        row.append(link(investigationPath(item.key), label));
        if (item.updated_at) row.append(node('small', ' · ' + item.updated_at, 'muted'));
        list.append(row);
      }
      target.replaceChildren(list.childNodes.length ? list : node('p', '暂无查找记录。', 'muted'));
    } catch (error) {
      target.textContent = error.name === 'TimeoutError' ? '读取记录超时，请重新展开。' : error.message;
    }
  }

  history.addEventListener('toggle', () => { if (history.open) loadHistory(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopPolling();
    else schedulePolling(0);
  });
  window.addEventListener('pagehide', stopPolling);
  window.addEventListener('pageshow', () => schedulePolling(0));
  if (pollingKey) {
    message('读取查找进度…');
    refreshCase();
  }
})();

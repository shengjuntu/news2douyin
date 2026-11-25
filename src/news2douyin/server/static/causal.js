(() => {
  'use strict';

  const apiRoot = '/api/causal/investigations';
  const archivePage = '/admin/history-archive';
  const archiveApi = '/api/history-archive/request';
  const el = id => document.getElementById(id);
  const form = el('causal-form');
  const tree = el('causal-tree');
  const treeToggle = el('causal-tree-toggle');
  const history = el('causal-history');
  const agentForm = el('causal-agent-form');
  const agentMessage = 'causal-agent-message';
  const repairForm = el('causal-repair-form');
  const repairList = el('causal-repair-list');
  const repairMessage = 'causal-repair-message';
  const config = JSON.parse(el('causal-config').textContent);
  const pageRoot = localPath(config.page_root) ? config.page_root : '/radar/causes';
  const focusKey = config.focus_key || '';
  const panel = el('causal-panel');
  const stateLabels = {pending:'排队中', running:'查找中', ready:'已查找', failed:'查找失败'};
  const programLabels = {pending:'等待查找', running:'逐层查找中', awaiting_archive:'等待补档后重查', completed:'查找完成', partial:'部分完成'};
  const timeLabels = {earlier:'有更早公开证据', unknown:'先后未明', root:'待查事件'};
  const agentLabels = {
    draft:'待开始', queued:'排队中', preparing:'正在准备环境', starting:'正在提交',
    running:'研究中', waiting:'等待处理', stopping:'正在停止', uncertain:'待确认',
    completed:'已完成', partial:'部分完成', paused:'已暂停', failed:'研究失败', cancelled:'已取消',
    canceled:'已取消', interrupted:'已中断'
  };
  const resumableAgent = new Set(['completed', 'partial', 'paused']);
  const retryableAgent = new Set(['draft', 'failed', 'cancelled', 'canceled', 'interrupted']);
  const relationLabels = {
    possible_cause:'可能前因', context:'背景', contradicts:'反证', document_reference:'参考线索'
  };
  const retrievalLabels = {lexical_only:'关键词', lexical:'关键词', lexical_embedding:'关键词＋向量', hybrid:'关键词＋向量'};
  const localCoverageLabels = {gap:'本地缺档', covered:'本地有历史记录', unknown:'本地覆盖待核对'};
  const historyLabels = {
    skipped:'未触发历史补查', unconfigured:'历史补查未配置', out_of_range:'日期超出归档范围',
    no_keywords:'缺少历史检索词', error:'历史补查失败', no_results:'所查时段无结果',
    unverified:'有线索待核实', verified:'已核实原文', archive_gap:'历史归档待补齐'
  };
  const historyErrorLabels = {
    invalid_configuration:'历史补查配置无效', missing_dependency:'缺少 BigQuery 依赖',
    credentials_or_access:'BigQuery 凭据或权限有误', bytes_limit:'超出 BigQuery 扫描量上限',
    query_timeout:'BigQuery 查询超时', query_quota:'达到 BigQuery 配额上限', query_failed:'BigQuery 查询失败',
    archive_query_failed:'历史归档查询失败'
  };
  const repairLabels = {
    queued:'已排队', running:'补采中', partial:'仍有缺口', failed:'失败', completed:'已完成',
    submit_digest:'等待重算', awaiting_digest:'正在重算', completed_partial:'已用部分批次重算'
  };
  const archiveLabels = {waiting:'正在等补档', queued:'补档已排队', ready:'补档已完成',
    running:'正在补档', partial:'部分归档可查询', failed:'补档失败'};
  const archiveCoverageLabels = {gap:'缺档', partial:'部分可查', covered:'已可查', ready:'已可查',
    unavailable:'该年代没有此数据', not_expected:'该年代没有此数据', unknown:'状态待核'};
  const historyKinds = {events:'Events', gkg:'GKG', mentions:'Mentions'};
  const pendingArchive = item => ['waiting', 'queued', 'running'].includes(item.archive_request?.state);
  const archiveCoverageState = value => typeof value === 'string' ? value : value?.state || value?.status;
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
    const [path, query] = pageRoot.split('?', 2);
    const parameters = new URLSearchParams(query);
    parameters.set('investigation', key);
    return path + '?' + parameters.toString();
  }

  function scopedRead(path) {
    return focusKey ? path + '?focus_key=' + encodeURIComponent(focusKey) : path;
  }

  function withBack(path, key) {
    const [base, fragment] = path.split('#', 2);
    return base + (base.includes('?') ? '&' : '?') +
      'back=' + encodeURIComponent(key ? investigationPath(key) : pageRoot) + (fragment ? '#' + fragment : '');
  }

  function setRoot(key) {
    form.elements.event_key.value = key;
    const root = el('causal-root');
    root.hidden = !key;
    const path = key ? withBack('/events/' + encodeURIComponent(key), current?.key) : '';
    localLink('causal-root-link', path);
  }

  function openPanel() {
    if (panel) {
      panel.open = true;
      panel.scrollIntoView({block:'nearest'});
    }
    el('causal-text').focus();
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

  function disclosure(label, key, open, defaultOpen = false) {
    const details = node('details');
    details.dataset.disclosure = key;
    details.open = open.get(key) ?? defaultOpen;
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
      if (source.modified_at) row.append(node('small', ' · 更新 ' + source.modified_at, 'muted'));
      if (source.cited_quote) row.append(node('small', ' · 本项引文来源', 'muted'));
      if (source.quote) row.append(node('blockquote', source.quote));
      if (row.childNodes.length) list.append(row);
    }
    details.append(list);
    return details;
  }

  function countFacts(entries) {
    return entries.filter(([, count]) => Number.isInteger(count) && count >= 0)
      .map(([label, count]) => label + ' ' + count);
  }

  function showRetrieval(item, open) {
    const report = item.retrieval;
    const local = report?.local;
    const history = report?.history;
    const steps = [localCoverageLabels[local?.status], historyLabels[history?.status]].filter(Boolean);
    const mode = retrievalLabels[item.retrieval_mode];
    const active = ['pending', 'running'].includes(item.job_state);
    const retrieving = active && report?.phase === 'retrieving';
    const label = [retrieving ? '正在按假设检索' : active ? '上轮检索' : '检索过程', ...steps, mode,
      !retrieving && Number.isInteger(item.candidate_count) && item.candidate_count + ' 条候选'].filter(Boolean).join(' · ');
    if (!steps.length && !item.queries?.length && !Number.isInteger(item.candidate_count) && !mode) return null;
    const list = node('ul', undefined, 'causal-node-meta');
    if (local) {
      const facts = countFacts([['召回记录', local.recall_count], ['候选入池', local.pool_count], ['通过初筛', local.eligible_count],
        ['有更早原文证据', local.dated_evidence_count], ['比对历史事件向量', local.vector_scanned_count],
        ['仅由向量找到', local.vector_only_count], ['参与向量排序', local.vector_ranked_count]]);
      if (Number.isInteger(local.vector_scan_ms) && local.vector_scan_ms >= 0) {
        facts.push('向量检索 ' + (local.vector_scan_ms / 1000).toFixed(2) + ' 秒');
      }
      if (facts.length) list.append(node('li', ['本地检索', ...facts].join(' · ')));
      if (local.embedding_state === 'unconfigured' || local.embedding_state === 'disabled') {
        list.append(node('li', local.embedding_state === 'disabled' ? '向量检索已关闭，使用关键词检索' : '向量检索未配置，使用关键词检索'));
      }
    }
    if (history && history.status !== 'skipped') {
      const window = [history.date_from, history.date_to].filter(Boolean).join(' 至 ');
      const facts = countFacts([['线索', history.lead_count], ['已尝试核查', history.attempted_count],
        ['已读取页面', history.fetched_count], ['上轮原文参与复核', history.previous_candidate_count || undefined],
        ['已核实日期和出处', history.verified_count], ['纳入候选', history.eligible_count],
        ['参与向量排序', history.ranked_count]]);
      const coverage = Object.entries(history.coverage || {}).flatMap(([kind, value]) => {
        const status = archiveCoverageLabels[archiveCoverageState(value)];
        return historyKinds[kind] && status ? [historyKinds[kind] + ' ' + status] : [];
      });
      const notes = [window && window + '（UTC）', history.window_limited && !history.windows && '仅查此时间窗',
        history.coverage_incomplete && !coverage.length && '归档覆盖不完整',
        history.verification_limited && '还有线索未核查', historyErrorLabels[history.error_code], ...facts].filter(Boolean);
      notes.push(...coverage);
      if (notes.length) list.append(node('li', ['GDELT 补查', ...notes].join(' · ')));
      for (const step of history.windows || []) {
        const status = step.status === 'leads' ? '命中 ' + step.lead_count + ' 条线索' :
          historyLabels[step.status] || '';
        list.append(node('li', [step.date_from + ' 至 ' + step.date_to + '（UTC）', status].filter(Boolean).join(' · ')));
      }
      if (history.scan?.can_continue) {
        list.append(node('li', '可继续扫描更早的未查窗口。'));
      }
      for (const source of history.retrospective_sources || []) {
        const url = originalUrl(source.url);
        if (!url) continue;
        const entry = node('li');
        entry.append(node('span', source.reason === 'updated_after_cutoff' ? '事后更新 · ' : '版本日期待核 · '),
          link(url, source.title || '查看原文'));
        if (source.modified_at) entry.append(node('small', ' · 更新 ' + source.modified_at, 'muted'));
        list.append(entry);
      }
    }
    const ranking = countFacts([['待送审候选', report?.prepared_count],
      [Number.isInteger(report?.prepared_count) ? '模型已完成判断' : '交给模型判断', report?.ranked_count],
      ['保留为分支', report?.selected_count]]);
    if (ranking.length) list.append(node('li', ranking.join(' · ')));
    if (!list.childNodes.length && !item.queries?.length) return node('p', label, 'causal-node-meta');
    const details = disclosure(label, 'query:' + item.key, open);
    details.className = 'causal-queries';
    if (list.childNodes.length) details.append(list);
    if (item.queries?.length) {
      const queries = node('ol');
      for (const [index, query] of item.queries.entries()) {
        const hits = local?.hypothesis_hits?.[index];
        const suffix = Array.isArray(hits) ? ' · 向量召回 ' + hits.length + ' 条有原文的事件' : '';
        queries.append(node('li', query + suffix));
      }
      details.append(node('strong', '归因模型提出的前因假设'), queries);
    }
    return details;
  }

  function updateTreeToggle() {
    const branches = [...tree.querySelectorAll('details[data-branch]')];
    treeToggle.hidden = !branches.length;
    treeToggle.textContent = branches.some(branch => !branch.open) ? '展开全部分支' : '折叠全部分支';
  }

  function addArchiveAction(actions, item) {
    const status = item.archive_request;
    if (status) {
      const dates = [status.date_from, status.date_to].filter(Boolean).join(' 至 ');
      actions.append(link(archivePage, [archiveLabels[status.state] || '查看历史归档', dates].filter(Boolean).join(' · ')));
    }
    if (pendingArchive(item)) return;
    const history = item.retrieval?.history;
    const stage = status?.state;
    const windows = history?.windows?.length ? history.windows : (history ? [history] : []);
    const covered = ['covered', 'ready', 'unavailable', 'not_expected'];
    for (const window of windows) {
      if (!window.date_from || !window.date_to || window.status === 'error') continue;
      const mainReady = window.coverage && ['events', 'gkg'].every(kind =>
        covered.includes(archiveCoverageState(window.coverage[kind])));
      const needsMentions = mainReady && ['gap', 'partial', 'unknown'].includes(
        archiveCoverageState(window.coverage.mentions));
      const needsArchive = (window.status === 'archive_gap' || window.coverage_incomplete) && !mainReady;
      const retry = status && ['failed', 'partial'].includes(stage) &&
        window.date_from <= status.date_from && status.date_to <= window.date_to;
      if (!needsArchive && !needsMentions && !retry) continue;
      const target = retry ? status : window;
      const mentions = retry ? status.mentions : needsMentions;
      const label = retry && stage === 'failed' ? '重试补档' : mentions ? '补更多报道' : '补这个时间窗';
      const button = node('button', windows.length > 1 ? label + '（' + target.date_from + '）' : label, 'ghost');
      button.type = 'button';
      button.dataset.archiveNode = item.key;
      button.dataset.dateFrom = target.date_from;
      button.dataset.dateTo = target.date_to;
      button.dataset.mentions = String(Boolean(mentions));
      button.setAttribute('aria-label', label + '：' + target.date_from + ' 至 ' + target.date_to + ' · ' + item.text.slice(0, 100));
      actions.append(button);
    }
  }

  function renderTree({nodes:items, key:caseKey, agent, program, working}) {
    const open = new Map([...tree.querySelectorAll('details[data-disclosure]')].map(
      details => [details.dataset.disclosure, details.open]
    ));
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
        const meta = [relationLabels[item.relation], timeLabels[item.time_status],
          item.time_status !== 'root' && item.occurred_at && '发生 ' + item.occurred_at,
          item.published_at && '报道 ' + item.published_at].filter(Boolean);
        if (meta.length) card.append(node('p', meta.join(' · '), 'causal-node-meta'));
        if (item.hypothesis) card.append(node('p', '假设 · ' + item.hypothesis));
        if (item.reason && item.reason !== item.hypothesis) card.append(node('p', item.reason));
        const evidence = showEvidence(item, open);
        if (evidence) card.append(evidence);
        const retrieval = showRetrieval(item, open);
        if (retrieval) card.append(retrieval);

        const branches = siblings.get(item.key) || [];
        const actions = node('div', undefined, 'row causal-node-actions');
        if (item.event_key) actions.append(link(withBack('/events/' + encodeURIComponent(item.event_key), caseKey), '查看整理事件'));
        if (stateLabels[item.job_state]) actions.append(node('span', stateLabels[item.job_state]));
        const programRetry = !working && item.expandable;
        if (item.previous_revision) {
          actions.append(link('/radar/causes/' + encodeURIComponent(caseKey) + '/revisions/' + item.previous_revision,
            '查看上轮记录'));
        }
        if (program && !agent) addArchiveAction(actions, item);
        if (!agent && !pendingArchive(item) && (!program || programRetry) && !['pending', 'running'].includes(item.job_state)) {
          const label = {failed:'重试查找', ready:'重新查找'}[item.job_state] || '沿此线索查找';
          const labels = item.retrieval?.history?.scan?.can_continue ? ['继续查更早归档', label] : [label];
          for (const action of labels) {
            const button = node('button', action, 'ghost');
            button.type = 'button';
            button.dataset.expand = item.key;
            if (action !== label) button.dataset.continueHistory = 'true';
            button.setAttribute('aria-label', action + '：' + item.text.slice(0, 100));
            actions.append(button);
          }
        }
        if (actions.childNodes.length) card.append(actions);
        if (item.archive_request?.error) card.append(node('p', item.archive_request.error, 'small causal-node-error'));
        if (item.retrieval?.previous_results_visible) card.append(node('p', '本轮查找失败，保留上轮候选。', 'small muted'));
        if (item.job_error) card.append(node('p', item.job_error, 'small causal-node-error'));

        if (item.stop_reason && !['pending', 'running'].includes(item.job_state)) {
          card.append(node('p', '停止追查 · ' + item.stop_reason, 'small muted'));
        } else if (!branches.length && item.job_state === 'ready') {
          card.append(node('p', '暂无可继续追查的候选。', 'small muted'));
        }
        entry.append(card);
        if (branches.length) {
          const children = disclosure('第 ' + (depth + 1) + ' 层 · ' + branches.length + ' 条线索',
            'branches:' + item.key, open, depth === 0);
          children.className = 'causal-child-group';
          children.dataset.branch = '';
          children.append(renderList(branches, depth + 1));
          entry.append(children);
        }
        list.append(entry);
      }
      return list;
    }

    tree.replaceChildren(renderList(siblings.get('') || [], 0));
    updateTreeToggle();
    if (focused) [...tree.querySelectorAll('[data-expand]')].find(button => button.dataset.expand === focused)?.focus();
  }

  function setControls() {
    el('causal-submit').disabled = submitting;
    if (agentForm) el('causal-agent-submit').disabled = submitting || Boolean(current?.working);
    el('causal-repair-submit').disabled = submitting;
    el('causal-repair-refresh').disabled = submitting;
    for (const button of repairList.querySelectorAll('[data-retry-day]')) button.disabled = submitting;
    for (const button of tree.querySelectorAll('[data-expand]')) button.disabled = submitting || current.working;
    for (const button of tree.querySelectorAll('[data-archive-node]')) button.disabled = submitting || current.working;
  }

  function renderAgent(agent, program, working, newCase, caseKey) {
    if (!agentForm) return;
    const resumable = resumableAgent.has(agent?.status);
    el('causal-agent').hidden = !agent && working;
    agentForm.hidden = working || Boolean(agent && !resumable && !retryableAgent.has(agent.status));
    const date = agentForm.elements.date_from;
    date.hidden = date.disabled = Boolean(program);
    el('causal-agent-date-label').hidden = Boolean(program);
    const action = program ? '解读' : '研究';
    el('causal-agent-submit').textContent = resumable ? '继续 RunDesk ' + action :
      agent && agent.status !== 'draft' ? '重试 RunDesk ' + action : '交给 RunDesk ' + action;
    const status = el('causal-agent-status');
    status.hidden = !agent;
    status.textContent = agent ? agentLabels[agent.status] || '状态待核' : '';
    const windowLabel = el('causal-agent-window');
    windowLabel.hidden = !agent?.date_from;
    windowLabel.textContent = agent?.date_from ? '从 ' + agent.date_from + ' 开始查找' : '';
    const error = el('causal-agent-error');
    error.hidden = !agent?.error;
    error.textContent = agent?.error || '';
    const path = agent?.research_path;
    const eventResearch = localPath(path) && path.startsWith('/events/');
    localLink('causal-agent-research', eventResearch ? withBack(path, caseKey) : path);
    el('causal-agent-research').textContent = ['completed', 'partial'].includes(agent?.status) ? '查看研究报告' : '查看研究';
    if (newCase || (agent?.date_from && !agentForm.elements.date_from.value)) {
      agentForm.elements.date_from.value = agent?.date_from || '';
    }
    if (newCase) message('', false, agentMessage);
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
    const changed = !current || Boolean(current.agent) !== Boolean(data.agent) ||
      Boolean(current.program) !== Boolean(data.program) || current.key !== data.key ||
      (data.program && current.working !== data.working) ||
      JSON.stringify(current.nodes) !== JSON.stringify(data.nodes);
    const newCase = current?.key !== data.key;
    const changedRepairs = newCase || JSON.stringify(current.coverage?.repairs) !== JSON.stringify(data.coverage?.repairs);
    current = data;
    pollingKey = data.key;
    setRoot(form.elements.event_key.value);
    el('causal-result').hidden = false;
    const program = data.program;
    el('causal-progress').textContent = programLabels[program?.status] || (data.working ? '查找中' :
      data.nodes.some(item => item.job_state === 'failed') ? '有分支待重试' : '本轮已结束');
    const progress = el('causal-program-progress');
    progress.hidden = !program;
    progress.textContent = program ? [
      program.date_from && '查找起始日 ' + program.date_from,
      '已查 ' + program.completed,
      program.queued > 0 && '待查 ' + program.queued,
      program.failed > 0 && '失败 ' + program.failed
    ].filter(Boolean).join(' · ') : '';
    const anchor = el('causal-anchor');
    anchor.hidden = !data.event_at;
    anchor.textContent = data.event_at ? '检索时间 ' + data.event_at : '';
    el('causal-coverage').textContent = program ? '' : data.coverage?.message || '';
    localLink('causal-archive', program ? archivePage : data.coverage?.archive_path);
    repairForm.closest('.causal-repair').hidden = Boolean(program);
    const eventResearch = data.research_path?.startsWith('/events/');
    const researchPath = eventResearch ? withBack(data.research_path, data.key) : data.research_path;
    localLink('causal-research', program ? '' : researchPath);
    el('causal-research').textContent = eventResearch ? '补充检索' : '研究助手设置';
    renderAgent(data.agent, program, data.working, newCase, data.key);
    if (newCase) {
      repairForm.elements.day.value = data.coverage?.suggested_day || '';
      message('', false, repairMessage);
    }
    if (changedRepairs) renderRepairs(data.coverage?.repairs || []);
    if (changed) renderTree(data);
    setControls();
  }

  function stopPolling() {
    clearTimeout(pollTimer);
    pollRequest?.abort();
    pollRequest = null;
  }

  function schedulePolling(delay = 2300) {
    clearTimeout(pollTimer);
    if (pollingKey && !document.hidden && !submitting && (!current || current.working || current.nodes.some(pendingArchive))) {
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
      const data = await request(scopedRead(apiRoot + '/' + encodeURIComponent(pollingKey)), {signal:controller.signal});
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
      const timeout = feedback === repairMessage ? '提交超时，请刷新补采状态。' :
        feedback === agentMessage ? '提交超时，请刷新页面核对研究状态。' : '提交超时，请在查找记录中核对。';
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
    const isProgram = values.get('runner') === 'program';
    changeCase(apiRoot, {
      text, event_at:String(values.get('event_at') || '').trim(),
      progress_key:values.get('progress_key') || '', event_key:values.get('event_key') || '', automatic:isProgram || (!focusKey && values.has('automatic')),
      focus_key:focusKey, runner:isProgram ? 'program' : 'local', date_from:isProgram ? (values.get('date_from') || '') : ''
    }, true);
  });
  function updateRunnerForm() {
    const program = form.elements.runner.value === 'program';
    el('causal-date-options').hidden = !program;
    form.elements.date_from.disabled = !program;
    el('causal-at-label').textContent = config.progress_source === 'gdelt' ? 'GDELT 编码日期' : (program ? '事件时间' : '事件时间（可选）');
  }
  if (form.elements.runner) {
    form.elements.runner.addEventListener('change', updateRunnerForm);
    updateRunnerForm();
  }
  agentForm?.addEventListener('submit', event => {
    event.preventDefault();
    if (!current || current.working) return;
    const payload = current.program ? {} : {date_from:agentForm.elements.date_from.value};
    changeCase(apiRoot + '/' + encodeURIComponent(current.key) + '/agent', payload, false, agentMessage);
  });
  el('causal-root-clear').addEventListener('click', () => {
    setRoot('');
    el('causal-at').value = '';
    openPanel();
  });
  el('graph-causal-jump')?.addEventListener('click', event => {
    event.preventDefault();
    openPanel();
  });
  if (focusKey && panel) document.addEventListener('n2d:causal-root', event => {
    const {eventKey = '', title, eventDate = ''} = event.detail || {};
    if (typeof eventKey !== 'string' || typeof title !== 'string') return;
    if (!eventKey && !/^\d{4}-\d{2}-\d{2}$/.test(eventDate)) return;
    setRoot(eventKey);
    el('causal-text').value = title.slice(0, 2000);
    el('causal-at').value = eventDate;
    openPanel();
  });
  tree.addEventListener('click', event => {
    const archive = event.target.closest('button[data-archive-node]');
    if (archive && current) {
      submitArchiveWindow(current.key, archive.dataset);
      return;
    }
    const button = event.target.closest('button[data-expand]');
    if (button && current) {
      changeCase(apiRoot + '/' + encodeURIComponent(current.key) + '/expand',
        {node_key:button.dataset.expand, automatic:false, continue_history:button.dataset.continueHistory === 'true'}, false);
    }
  });

  async function submitArchiveWindow(caseKey, {archiveNode, dateFrom, dateTo, mentions}) {
    if (submitting) return;
    stopPolling();
    submitting = true;
    setControls();
    message('正在提交历史补档…');
    try {
      await request(archiveApi, {
        payload:{case_key:caseKey, node_key:archiveNode, date_from:dateFrom, date_to:dateTo, mentions:mentions === 'true'},
        signal:AbortSignal.timeout(15000)
      });
      const next = await request(scopedRead(apiRoot + '/' + encodeURIComponent(caseKey)),
        {signal:AbortSignal.timeout(12000)});
      render(next);
      message('已加入历史补档队列。');
    } catch (error) {
      message(error.name === 'TimeoutError' ? '提交超时，请到历史归档核对任务。' : error.message, true);
    } finally {
      submitting = false;
      setControls();
      schedulePolling();
    }
  }
  tree.addEventListener('toggle', event => {
    if (event.target.matches('details[data-branch]')) updateTreeToggle();
  }, true);
  treeToggle.addEventListener('click', () => {
    const branches = [...tree.querySelectorAll('details[data-branch]')];
    const open = branches.some(branch => !branch.open);
    for (const branch of branches) branch.open = open;
    updateTreeToggle();
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
      const data = await request(scopedRead(apiRoot), {signal:AbortSignal.timeout(12000)});
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
  setRoot(form.elements.event_key.value);
  if (pollingKey) {
    message('读取查找进度…');
    refreshCase();
  }
})();

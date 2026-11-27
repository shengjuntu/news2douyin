/* Topic creation stays small: describe, review, follow. Model text is always text. */
(() => {
  'use strict';
  const configNode = document.getElementById('focus-config');
  if (!configNode) return;
  let cfg;
  try { cfg = JSON.parse(configNode.textContent); } catch (_) { return; }
  const watch = cfg.watch || {}, definition = cfg.definition || null;
  const base = '/api/focus' + (watch.key ? '/' + encodeURIComponent(watch.key) : '');
  const msg = document.getElementById('focus-msg');
  const description = document.getElementById('topic-description');
  const form = document.getElementById('topic-description-form');
  const generate = document.getElementById('topic-generate');
  const cancel = document.getElementById('topic-cancel');
  const preview = document.getElementById('topic-preview');
  const buildStatus = document.getElementById('topic-build-status');
  const saveStatus = document.getElementById('topic-save-status');
  const start = document.getElementById('topic-start');
  const anchors = document.getElementById('topic-anchors');
  const mcpUrl = document.getElementById('focus-mcp-url');
  if (mcpUrl) mcpUrl.textContent = new URL('/mcp/focus', location.origin).href;
  let buildToken = 0, building = false, saving = false, draft = '', draftSpec = null, draftDescription = '', localDraft = false;
  let activeRequest = null, trackingTimer = null, disposed = false;
  const cacheKey = 'n2d-focus-draft:local:' + (watch.key || 'new:' + (cfg.draft_scope || cfg.seed || ''));
  function status(node, text, error = false) {
    if (!node) return;
    node.textContent = text || '';
    node.dataset.error = String(error);
  }
  function errorText(value) {
    if (typeof value.detail === 'string') return value.detail;
    if (Array.isArray(value.detail)) return value.detail.map(x => x.msg || '输入格式不正确').join('；');
    return value.message || value.error || '请求未完成，请稍后重试。';
  }
  async function request(url, body, method = 'POST', options = {}) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 18000);
    if (options.build) activeRequest = controller;
    try {
      const response = await fetch(url, {
        method, signal: controller.signal, headers: body === undefined ? {} : {'Content-Type': 'application/json'},
        ...(body === undefined ? {} : {body: JSON.stringify(body)}), cache: 'no-store'
      });
      let value;
      try { value = await response.json(); } catch (_) { throw new Error('服务返回了无法读取的结果，请稍后重试。'); }
      if (!response.ok) throw new Error(errorText(value));
      return value;
    } catch (err) {
      if (err.name === 'AbortError') throw new Error('请求等待超时，可保留描述后重试。');
      throw err;
    } finally {
      clearTimeout(timeout);
      if (activeRequest === controller) activeRequest = null;
    }
  }
  function setBuilding(value) {
    building = value;
    generate.disabled = value;
    generate.textContent = value ? '正在准备…' : '预览关注规则';
    cancel.hidden = !value;
    form.setAttribute('aria-busy', String(value));
  }
  function rememberDraft(value) {
    try { sessionStorage.setItem(cacheKey, JSON.stringify(value)); } catch (_) { /* Private browsing may disable storage. */ }
  }
  function forgetDraft() { try { sessionStorage.removeItem(cacheKey); } catch (_) {} }
  function stopBuild(text) {
    buildToken += 1;
    if (activeRequest) activeRequest.abort();
    activeRequest = null;
    setBuilding(false);
    if (text) status(buildStatus, text);
  }
  function listLines(text) {
    return [...new Set(text.split(/\r?\n/).map(x => x.trim()).filter(Boolean))];
  }
  const safeArray = value => Array.isArray(value) ? value : [];
  function entityName(entity) { return entity.name || entity.id || ''; }
  function focusSpecGraph(host, spec) {
    if (!host || !spec) return;
    if (host._focusCleanup) { host._focusCleanup(); host._focusCleanup = null; }
    else if (host._focusCy) host._focusCy.destroy();
    host._focusCy = null;
    host.replaceChildren();
    const entities = safeArray(spec.entities).slice(0, 24), relations = safeArray(spec.relations).slice(0, 24);
    const names = new Map(entities.map(e => [String(e.id), entityName(e)]));
    const toolbar = document.createElement('div'); toolbar.className = 'focus-intent-toolbar';
    const hint = document.createElement('span'); hint.className = 'desk-meta small';
    hint.textContent = '点选节点查看详情。'; toolbar.append(hint);
    const controls = document.createElement('div'); controls.className = 'desk-row'; toolbar.append(controls);
    const canvas = document.createElement('div'); canvas.className = 'focus-intent-canvas';
    canvas.tabIndex = 0; canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', '关注范围图；详细实体和关系在图下方以文字列出。');
    const selected = document.createElement('p'); selected.className = 'focus-intent-selection'; selected.setAttribute('aria-live', 'polite');
    const entitiesText = document.createElement('div'); entitiesText.className = 'focus-intent-entities';
    for (const entity of entities) {
      const tag = document.createElement('span'); tag.textContent = entityName(entity) + (entity.core ? ' · 核心' : '');
      entitiesText.append(tag);
    }
    const relationList = document.createElement('ul'); relationList.className = 'focus-intent-relations';
    for (const relation of relations) {
      const item = document.createElement('li');
      item.textContent = (names.get(String(relation.subject)) || relation.subject || '相关参与方') + ' → ' + (relation.relation || '关注关系') + ' → ' + (names.get(String(relation.object)) || relation.object || '相关对象');
      relationList.append(item);
    }
    host.append(toolbar, canvas, selected, entitiesText, relationList);
    if (typeof window.cytoscape !== 'function') {
      canvas.hidden = true; toolbar.hidden = true;
      selected.textContent = '关注图组件未加载，下面的实体与关系仍可查看。'; return;
    }
    const rootId = '__focus__';
    const nodes = [{data: {id: rootId, label: spec.name || '焦点', type: 'focus'}}];
    const nodeIds = new Set([rootId]);
    for (const entity of entities) {
      const id = String(entity.id || entity.name || ''); if (!id || nodeIds.has(id)) continue;
      nodeIds.add(id); nodes.push({data: {id, label: entityName(entity), type: entity.core ? 'core' : 'entity', detail: safeArray(entity.aliases).join('、')}});
    }
    const edges = [];
    for (const entity of entities) {
      const id = String(entity.id || entity.name || '');
      if (nodeIds.has(id)) edges.push({data: {id: '__watch__' + edges.length, source: rootId, target: id, label: entity.core ? '核心关注' : '关联关注', type: 'attention'}});
    }
    for (const relation of relations) {
      const from = String(relation.subject || ''), to = String(relation.object || '');
      if (from && to && nodeIds.has(from) && nodeIds.has(to) && from !== to) edges.push({data: {id: '__relation__' + edges.length, source: from, target: to, label: relation.relation || '关注关系', type: 'relation'}});
    }
    const cy = window.cytoscape({
      container: canvas, elements: [...nodes, ...edges], minZoom: .2, maxZoom: 3,
      wheelSensitivity: .2, layout: {name: 'cose', animate: false, padding: 34, idealEdgeLength: 110, nodeRepulsion: 9000, randomize: false},
      style: [
        {selector: 'node', style: {'label': 'data(label)', 'color': '#deedf6', 'font-size': 12, 'text-wrap': 'wrap', 'text-max-width': 110, 'text-valign': 'bottom', 'text-margin-y': 8, 'background-color': '#487485', 'width': 28, 'height': 28, 'border-width': 2, 'border-color': '#70b4c2'}},
        {selector: 'node[type="focus"]', style: {'background-color': '#cc3656', 'border-color': '#f38aa0', 'width': 42, 'height': 42, 'font-size': 13}},
        {selector: 'node[type="core"]', style: {'background-color': '#277f83', 'border-color': '#68d5cd', 'width': 34, 'height': 34}},
        {selector: 'edge', style: {'curve-style': 'bezier', 'line-color': '#3f5868', 'target-arrow-color': '#3f5868', 'target-arrow-shape': 'triangle', 'width': 1.4}},
        {selector: 'edge[type="relation"]', style: {'label': 'data(label)', 'font-size': 10, 'color': '#c1d5df', 'text-background-opacity': 1, 'text-background-color': '#101922', 'text-background-padding': 3, 'text-rotation': 'autorotate', 'line-color': '#96a167', 'target-arrow-color': '#96a167', 'width': 1.8}},
        {selector: ':selected', style: {'border-color': '#fff', 'border-width': 3}}
      ]
    });
    host._focusCy = cy;
    cy.on('tap', 'node', event => { const d = event.target.data(); selected.textContent = d.label + (d.detail ? ' · 别名：' + d.detail : ''); });
    const buttons = [['＋', '放大关注图', () => cy.zoom({level: Math.min(3, cy.zoom() * 1.25), renderedPosition: {x: canvas.clientWidth / 2, y: canvas.clientHeight / 2}})], ['−', '缩小关注图', () => cy.zoom({level: Math.max(.2, cy.zoom() / 1.25), renderedPosition: {x: canvas.clientWidth / 2, y: canvas.clientHeight / 2}})], ['适应', '显示全部关注图', () => cy.fit(undefined, 34)]];
    for (const [label, title, action] of buttons) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'ghost'; button.textContent = label; button.setAttribute('aria-label', title); button.addEventListener('click', action); controls.append(button);
    }
    const resize = () => { if (host.isConnected && canvas.clientWidth && !cy.destroyed()) { cy.resize(); cy.fit(undefined, 34); } };
    const parent = host.closest('details');
    const onToggle = () => { if (parent.open) requestAnimationFrame(resize); };
    if (parent) parent.addEventListener('toggle', onToggle);
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
    if (observer) observer.observe(canvas);
    host._focusCleanup = () => { if (parent) parent.removeEventListener('toggle', onToggle); if (observer) observer.disconnect(); cy.destroy(); };
    requestAnimationFrame(resize);
  }
  function showSpec(value, token) {
    if (token !== buildToken || disposed) return;
    if (!value.spec || typeof value.spec !== 'object' || !value.spec.name) throw new Error('生成结果不完整，请修改描述后重试。');
    draft = value.key || draft; draftSpec = value.spec;
    localDraft = value.method === 'local';
    const fromHotspot = localDraft && cfg.seed_name && draftDescription === cfg.seed_text;
    document.getElementById('topic-name').value = fromHotspot ? cfg.seed_name : draftSpec.name;
    document.getElementById('topic-questions').value = safeArray(draftSpec.questions).join('\n');
    document.getElementById('topic-exclude').value = safeArray(draftSpec.exclude).join('\n');
    status(document.getElementById('topic-method'), value.method === 'model' ? 'AI 生成' : '本地规则');
    status(document.getElementById('topic-method-message'), value.method === 'model' ? '' : value.message || '请确认关注词。');
    const anchorsPanel = document.getElementById('topic-anchors-panel');
    if (anchorsPanel) anchorsPanel.hidden = !localDraft;
    if (anchors && localDraft) {
      const entities = safeArray(draftSpec.entities);
      const core = entities.filter(entity => entity.core);
      const terms = fromHotspot && safeArray(cfg.seed_terms).length
        ? cfg.seed_terms : (core.length ? core : entities).map(entityName);
      anchors.value = terms.slice(0, 12).join('\n');
    }
    const boundaries = [];
    if (safeArray(draftSpec.include).length) boundaries.push('重点：' + draftSpec.include.slice(0, 6).join('；'));
    if (safeArray(draftSpec.geography).length) boundaries.push('地区：' + draftSpec.geography.join('、'));
    if (draftSpec.time_start || draftSpec.time_end) boundaries.push('关注时间：' + (draftSpec.time_start || '不限起点') + ' 至 ' + (draftSpec.time_end || '持续跟踪'));
    status(document.getElementById('topic-boundaries'), boundaries.join('。'));
    if (!localDraft) focusSpecGraph(document.querySelector('[data-intent-graph="preview"]'), draftSpec);
    preview.hidden = false; setBuilding(false);
    status(buildStatus, ''); status(saveStatus, '');
    rememberDraft({key: draft, description: draftDescription});
    document.getElementById('topic-name').focus({preventScroll: true});
  }
  async function pollBuild(key, token) {
    let attempts = 0;
    while (token === buildToken && !disposed) {
      const value = await request('/api/focus/interpret/' + encodeURIComponent(key), undefined, 'GET', {build: true});
      if (token !== buildToken || disposed) return;
      if (value.status === 'ready') { showSpec(value, token); return; }
      if (value.status === 'failed') { draft = ''; forgetDraft(); throw new Error(value.message || '暂时无法生成，请保留描述后重试。'); }
      attempts += 1;
      if (attempts === 26) status(buildStatus, '仍在后台处理，可取消等待。');
      await new Promise(resolve => setTimeout(resolve, 1200));
    }
  }
  if (form) form.addEventListener('submit', async event => {
    event.preventDefault(); if (building || saving) return;
    const text = description.value.trim(); if (!text) { status(buildStatus, '请先描述你想持续关注什么。', true); description.focus(); return; }
    const resumeKey = draft && draftDescription === text ? draft : '';
    stopBuild(); const token = buildToken;
    draftSpec = null; draftDescription = text; preview.hidden = true; setBuilding(true);
    status(buildStatus, ''); status(saveStatus, '');
    try {
      if (resumeKey) { await pollBuild(resumeKey, token); return; }
      draft = '';
      const value = await request('/api/focus/interpret', {description: text, method: 'local'}, 'POST', {build: true});
      if (token !== buildToken || disposed) return;
      if (!value.key) throw new Error('服务没有返回生成任务，请重试。');
      draft = value.key; rememberDraft({key: draft, description: text});
      if (value.status === 'ready') showSpec(value, token); else await pollBuild(draft, token);
    } catch (err) {
      if (token !== buildToken || disposed) return;
      setBuilding(false); status(buildStatus, err.message, true);
    }
  });
  cancel?.addEventListener('click', () => stopBuild('已取消等待。生成任务仍可能在后台完成。'));
  description?.addEventListener('input', () => {
    if (draftDescription && description.value.trim() !== draftDescription) {
      stopBuild(); draft = ''; draftSpec = null; draftDescription = ''; preview.hidden = true; forgetDraft();
      status(buildStatus, '');
    }
  });
  for (const button of document.querySelectorAll('[data-topic-example]')) button.addEventListener('click', () => {
    stopBuild(); draft = ''; draftSpec = null; draftDescription = ''; preview.hidden = true; forgetDraft();
    description.value = button.dataset.topicExample; description.focus(); status(buildStatus, '');
  });
  document.getElementById('topic-back')?.addEventListener('click', () => { preview.hidden = true; description.focus(); });
  start?.addEventListener('click', async () => {
    if (saving || !draft || !draftSpec) return;
    const name = document.getElementById('topic-name'); if (!name.value.trim()) { status(saveStatus, '请填写焦点名称。', true); name.focus(); return; }
    const spec = JSON.parse(JSON.stringify(draftSpec));
    spec.name = name.value.trim(); spec.questions = listLines(document.getElementById('topic-questions').value).slice(0, 12);
    spec.exclude = listLines(document.getElementById('topic-exclude').value).slice(0, 12); spec.description = draftDescription;
    const payload = {draft, spec};
    if (localDraft && anchors) {
      payload.anchors = listLines(anchors.value);
      if (!payload.anchors.length || payload.anchors.length > 12 || payload.anchors.some(term => term.length < 2 || term.length > 160)) {
        status(saveStatus, '请填写 1–12 个关注词，每项 2–160 个字。', true); anchors.focus(); return;
      }
    }
    saving = true; start.disabled = true; generate.disabled = true;
    status(saveStatus, '正在保存…');
    try {
      const value = watch.key
        ? await request(base + '/definition', {...payload, version: watch.version, description: draftDescription}, 'PUT')
        : await request('/api/focus/from-description', {...payload, seed: cfg.seed || ''});
      if (!value.key) throw new Error('未取得焦点编号，请刷新列表确认。');
      forgetDraft(); location.assign('/focus/' + encodeURIComponent(value.key));
    } catch (err) { status(saveStatus, err.message, true); saving = false; start.disabled = false; generate.disabled = false; }
  });
  const manual = document.getElementById('focus-form');
  function manualPayload() {
    const value = Object.fromEntries(new FormData(manual));
    value.enabled = manual.elements.enabled.checked; value.auto_research = manual.elements.auto_research.checked;
    value.review_days = Number(value.review_days); return value;
  }
  if (manual) {
    manual.addEventListener('submit', async event => {
      event.preventDefault(); const button = document.getElementById('manual-save'); if (button.disabled) return; button.disabled = true;
      try { const p = manualPayload(); if (watch.key) p.version = watch.version; else p.seed = cfg.seed || ''; const value = await request(base, p, watch.key ? 'PUT' : 'POST'); location.assign('/focus/' + encodeURIComponent(value.key)); }
      catch (err) { status(msg, err.message, true); button.disabled = false; }
    });
    document.getElementById('preset')?.addEventListener('change', event => {
      const preset = safeArray(cfg.presets)[event.target.value]; if (preset) for (const [key, value] of Object.entries(preset)) if (manual.elements[key]) manual.elements[key].value = value;
    });
    document.getElementById('preview')?.addEventListener('click', async event => {
      const button = event.currentTarget; button.disabled = true;
      try { const value = await request('/api/focus/preview', manualPayload()); const out = document.getElementById('preview-result'); out.hidden = false; out.textContent = '最近 ' + value.checked + ' 个已索引事件中命中 ' + value.matched + ' 个\n' + safeArray(value.examples).map(x => x.title + ' · ' + x.reason).join('\n') + '\n未纳入的样例：\n' + safeArray(value.excluded).join('\n'); }
      catch (err) { status(msg, err.message, true); } finally { button.disabled = false; }
    });
  }
  document.getElementById('focus-options')?.addEventListener('submit', async event => {
    event.preventDefault(); const options = event.currentTarget, button = options.querySelector('button'); if (button.disabled) return; button.disabled = true;
    try {
      const p = {}; for (const key of ['name','kind','scope','dimensions','exclude','question']) p[key] = watch[key] || '';
      p.enabled = options.elements.enabled.checked; p.auto_research = options.elements.auto_research.checked; p.review_days = Number(options.elements.review_days.value); p.version = watch.version;
      await request(base, p, 'PUT'); location.reload();
    } catch (err) { status(msg, err.message, true); button.disabled = false; }
  });
  const deleteDialog = document.getElementById('focus-delete-dialog');
  document.getElementById('focus-delete')?.addEventListener('click', () => {
    status(document.getElementById('focus-delete-message'), '');
    deleteDialog.showModal();
  });
  document.getElementById('focus-delete-cancel')?.addEventListener('click', () => deleteDialog.close());
  document.getElementById('focus-delete-confirm')?.addEventListener('click', async event => {
    const button = event.currentTarget; if (button.disabled) return; button.disabled = true;
    try {
      await request(base, {version: watch.version}, 'DELETE');
      disposed = true; clearTimeout(trackingTimer); forgetDraft(); location.assign('/focus');
    } catch (err) { status(document.getElementById('focus-delete-message'), err.message, true); button.disabled = false; }
  });
  for (const button of document.querySelectorAll('[data-focus-restore]')) button.addEventListener('click', async () => {
    if (button.disabled) return; button.disabled = true;
    try {
      const value = await request('/api/focus/' + encodeURIComponent(button.dataset.focusRestore) + '/restore', {version: Number(button.dataset.focusVersion)});
      location.assign('/focus/' + encodeURIComponent(value.key));
    } catch (err) { status(document.getElementById('focus-trash-message'), err.message, true); button.disabled = false; }
  });
  document.getElementById('mark-seen')?.addEventListener('click', async event => {
    const button = event.currentTarget; button.disabled = true;
    try { await request(base + '/seen', {}); location.reload(); } catch (err) { status(msg, err.message, true); button.disabled = false; }
  });
  for (const button of document.querySelectorAll('[data-hide],[data-restore]')) button.addEventListener('click', async () => {
    if (button.disabled) return; button.disabled = true;
    try {
      if (button.dataset.restore) await request(base + '/links', {event: button.dataset.restore, action: 'auto'});
      else await request(base + '/links', {event: button.dataset.hide, action: 'exclude'});
      location.reload();
    } catch (err) { status(msg, err.message, true); button.disabled = false; }
  });
  const initialCounts = cfg.tracking?.counts || {};
  let initialAutomaticCount = typeof initialCounts.core === 'number' || typeof initialCounts.background === 'number' ? (Number(initialCounts.core) || 0) + (Number(initialCounts.background) || 0) : null;
  document.getElementById('tracking-refresh')?.addEventListener('click', () => location.reload());
  function attachDecision(details, eventKey) {
    const body = details.querySelector('[data-decision-body]');
    if (!body) return;
    let loading = false, loaded = false;
    details.addEventListener('toggle', async () => {
      if (!details.open || loading || loaded) return;
      loading = true; body.textContent = '正在读取筛选依据…';
      try {
        const value = await request(base + '/decisions/' + encodeURIComponent(eventKey), undefined, 'GET');
        body.replaceChildren();
        const item = safeArray(value.items)[0];
        if (!item) { body.textContent = body.dataset.fallbackReason || '暂无筛选记录。'; loaded = true; return; }
        const reason = document.createElement('p'); reason.textContent = item.reason || '已记录本次筛选。'; body.append(reason);
        const method = document.createElement('p'); method.className = 'desk-meta small';
        const labels = {core: '核心进展', background: '背景材料', pending: '待核', exclude: '不相关'};
        method.textContent = (item.method === 'model' ? '模型判断' : item.method === 'rules' ? '规则判断' : '待核判断') + ' · ' + (labels[item.decision] || '待核'); body.append(method);
        if (safeArray(item.questions).length) { const questions = document.createElement('p'); questions.className = 'desk-meta'; questions.textContent = '对应问题：' + item.questions.join('；'); body.append(questions); }
        for (const evidence of safeArray(item.evidence).slice(0, 4)) {
          const quote = document.createElement('blockquote'); quote.textContent = evidence.quote || ''; body.append(quote);
          if (evidence.article) {
            const link = document.createElement('a'); const revision = Number(evidence.revision);
            link.href = '/articles/' + encodeURIComponent(evidence.article) + (Number.isInteger(revision) && revision > 0 ? '?revision=' + revision : '');
            link.textContent = '查看原文' + (Number.isInteger(revision) && revision > 0 ? ' · 版本 ' + revision : ''); body.append(link);
          }
        }
        loaded = true;
      } catch (err) { body.textContent = '暂时无法读取依据，关闭后展开可重试。'; }
      finally { loading = false; }
    });
  }
  for (const details of document.querySelectorAll('[data-focus-decision]')) attachDecision(details, details.dataset.focusDecision);
  let pendingSignature = '';
  function renderTracking(value) {
    const root = document.getElementById('focus-tracking'); if (!root) return;
    const backfill = value.backfill || {}, counts = value.counts || {};
    const names = {pending: '准备回溯历史', running: '正在回溯历史', complete: '历史材料已检查', completed: '历史材料已检查', ready: '历史材料已检查', paused: '已暂停历史回溯', failed: '历史回溯需要重试', idle: watch.enabled ? '等待新材料' : '已暂停'};
    document.getElementById('tracking-title').textContent = names[backfill.status] || (watch.enabled ? '等待新材料' : '已暂停');
    const pieces = [];
    if (Number.isFinite(backfill.checked)) pieces.push('已检查 ' + backfill.checked + (Number.isFinite(backfill.total) ? ' / ' + backfill.total : '') + ' 项');
    const automaticCount = (Number(counts.core) || 0) + (Number(counts.background) || 0);
    if (typeof counts.core === 'number' || typeof counts.background === 'number') {
      if (initialAutomaticCount === null) initialAutomaticCount = automaticCount;
      const refresh = document.getElementById('tracking-refresh');
      if (refresh && automaticCount > initialAutomaticCount) refresh.hidden = false;
    }
    document.getElementById('tracking-counts').textContent = pieces.join(' · ');
    const err = document.getElementById('tracking-error'); err.hidden = !backfill.error; err.textContent = backfill.error || '';
    const pending = safeArray(value.pending).slice(0, 8), box = document.getElementById('tracking-pending-box');
    box.hidden = !(pending.length || Number(counts.pending) || backfill.status === 'failed');
    document.getElementById('tracking-pending-summary').textContent = '待核材料' + (Number(counts.pending) ? ' · ' + counts.pending + ' 项' : '');
    const list = document.getElementById('tracking-pending-list');
    const signature = JSON.stringify(pending.map(item => [item.event, item.title, item.reason]));
    if (signature === pendingSignature) return;
    pendingSignature = signature; list.replaceChildren();
    for (const item of pending) {
      const row = document.createElement('div'); row.className = 'focus-pending-item';
      const link = document.createElement('a'); link.href = '/events/' + encodeURIComponent(item.event) + '?back=' + encodeURIComponent('/focus/' + watch.key); link.textContent = item.title || '查看事件'; row.append(link);
      const details = document.createElement('details'); details.className = 'focus-decision';
      const summary = document.createElement('summary'); summary.textContent = '查看待核依据';
      const body = document.createElement('div'); body.dataset.decisionBody = ''; body.dataset.fallbackReason = item.reason || ''; details.append(summary, body);
      attachDecision(details, item.event); row.append(details);
      list.append(row);
    }
  }
  let trackingFailures = 0;
  async function pollTracking() {
    if (!watch.key || disposed) return;
    try { if (!document.hidden) { renderTracking(await request(base + '/tracking', undefined, 'GET')); trackingFailures = 0; } }
    catch (err) { trackingFailures += 1; const node = document.getElementById('tracking-error'); if (node) { node.hidden = false; node.textContent = '暂时无法刷新进度。'; } }
    if (!disposed) trackingTimer = setTimeout(pollTracking, Math.min(30000, 6000 * (trackingFailures + 1)));
  }
  document.getElementById('tracking-retry')?.addEventListener('click', async event => {
    const button = event.currentTarget; if (button.disabled) return; button.disabled = true;
    try { await request(base + '/tracking/retry', {}); renderTracking(await request(base + '/tracking', undefined, 'GET')); status(msg, '已提交后台重试。'); }
    catch (err) { status(msg, err.message, true); } finally { button.disabled = false; }
  });
  const researchForm = document.getElementById('continuous-form');
  if (researchForm) {
    const endpoint = base + '/continuous-research';
    const output = document.getElementById('continuous-results');
    const panelUrl = output.dataset.before ? endpoint + '?before=' + encodeURIComponent(output.dataset.before) : endpoint;
    const notice = document.getElementById('continuous-message');
    let timer, fetching = false, sending = false, lastPanel = '';
    const schedule = () => {
      clearTimeout(timer);
      if (!disposed && !document.hidden) {
        const busy = output.querySelector('[data-watch-busy="true"]');
        timer = setTimeout(refresh, busy ? 6000 : 60000);
      }
    };
    async function refresh() {
      if (disposed || fetching || sending || document.hidden) return;
      // Keep the reader's current evidence open while they inspect it.
      if (output.querySelector('details[open]') || output.contains(document.activeElement)) {
        schedule();
        return;
      }
      fetching = true;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 15000);
      try {
        const response = await fetch(panelUrl, {
          headers: {Accept: 'text/html'}, cache: 'no-store', signal: controller.signal
        });
        if (!response.ok) throw new Error('暂时无法刷新持续研究进度。');
        const panel = await response.text();
        if (!disposed && !sending && panel !== lastPanel &&
            !output.querySelector('details[open]') && !output.contains(document.activeElement)) {
          output.innerHTML = panel;
          lastPanel = panel;
        }
        if (notice.dataset.refreshError) {
          status(notice, '');
          delete notice.dataset.refreshError;
        }
      } catch (_) {
        if (!disposed && !sending) {
          status(notice, '暂时无法刷新持续研究进度，将自动重试。', true);
          notice.dataset.refreshError = 'true';
        }
      } finally {
        fetching = false;
        clearTimeout(timeout);
        schedule();
      }
    }
    async function send(button, url, body, method) {
      if (sending || button.disabled) return;
      sending = true;
      clearTimeout(timer);
      button.disabled = true;
      delete notice.dataset.refreshError;
      status(notice, '正在提交…');
      try {
        await request(url, body, method);
        location.reload();
      } catch (error) {
        status(notice, error.message, true);
        button.disabled = false;
        sending = false;
        schedule();
      }
    }
    researchForm.addEventListener('submit', event => {
      event.preventDefault();
      const goal = document.getElementById('continuous-goal-text').value.trim();
      if (!goal) {
        status(notice, '请写下需要持续查证的问题。', true);
        return;
      }
      const enabled = document.getElementById('continuous-enabled')?.checked ?? true;
      const owner = document.getElementById('continuous-owner').value;
      send(researchForm.querySelector('[type="submit"]'), endpoint, {goal, enabled, owner}, 'PUT');
    });
    output.addEventListener('click', event => {
      const button = event.target.closest('#continuous-run');
      if (!button) return;
      const goal = document.getElementById('continuous-goal-text');
      const enabled = document.getElementById('continuous-enabled');
      const owner = document.getElementById('continuous-owner');
      const savedOwner = Array.from(owner.options).find(option => option.defaultSelected);
      if (goal.value !== goal.defaultValue || (enabled && enabled.checked !== enabled.defaultChecked) ||
          (savedOwner && owner.value !== savedOwner.value)) {
        status(notice, '请先保存修改的研究设置，再立即复查。', true);
        return;
      }
      send(button, endpoint + '/run', undefined, 'POST');
    });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) clearTimeout(timer);
      else if (!disposed) refresh();
    });
    window.addEventListener('pagehide', () => clearTimeout(timer), {once: true});
    schedule();
  }
  if (document.getElementById('focus-tracking')) { renderTracking(cfg.tracking || {}); pollTracking(); }
  if (definition?.spec) focusSpecGraph(document.querySelector('[data-intent-graph="saved"]'), definition.spec);
  try {
    const saved = JSON.parse(sessionStorage.getItem(cacheKey) || 'null');
    if (saved?.key && saved.description && (!description.value.trim() || description.value.trim() === saved.description)) {
      description.value = saved.description; draftDescription = saved.description; draft = saved.key;
      const token = buildToken; setBuilding(true); status(buildStatus, '正在恢复上次生成的关注范围…');
      pollBuild(draft, token).catch(err => { if (token === buildToken) { setBuilding(false); status(buildStatus, err.message, true); } });
    }
  } catch (_) {}
  window.addEventListener('pagehide', () => {
    disposed = true; buildToken += 1; clearTimeout(trackingTimer); if (activeRequest) activeRequest.abort();
    for (const host of document.querySelectorAll('[data-intent-graph]')) { if (host._focusCleanup) host._focusCleanup(); else if (host._focusCy) host._focusCy.destroy(); }
  }, {once: true});
})();

/* Platform observations are signals, not audience estimates or verified facts. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  if (!byId('attention-filter')) return;
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  };
  const object = value => {
    if (value && typeof value === 'object' && !Array.isArray(value)) return value;
    if (typeof value === 'string') { try { const parsed = JSON.parse(value); return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}; } catch {} }
    return {};
  };
  const list = value => Array.isArray(value) ? value : [];
  const numeric = value => typeof value === 'number' && Number.isFinite(value);
  const score = value => numeric(value) ? value.toFixed(1) : '暂无';
  const readPage = () => { try { return object(JSON.parse(byId('attention-page-data')?.textContent || '{}')); } catch { return {}; } };
  const page = readPage();
  const topics = new Map(list(page.items).map(item => [String(item.key), item]));
  const summary = topic => object(topic?.summary || topic?.summary_json);
  const time = value => {
    if (!value) return '未知';
    const raw = String(value);
    const date = new Date(/^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(?:\.\d+)?)?$/.test(raw) ? raw + 'Z' : raw);
    return Number.isNaN(date.valueOf()) ? raw : new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short'}).format(date) + ' 北京时间';
  };
  const shortTime = value => {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? '' : new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}).format(date);
  };
  const externalUrl = raw => { try { const url = new URL(raw); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''; } catch { return ''; } };
  const link = (text, href, external = false) => {
    const anchor = node('a', text); anchor.href = href;
    if (external) { anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; }
    return anchor;
  };
  const button = (text, handler, className = 'ghost') => {
    const element = node('button', text, className); element.type = 'button'; element.addEventListener('click', handler); return element;
  };
  const errorText = detail => typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(row => row.msg || '请求参数不可用').join('；') : '请求未完成，请稍后重试。';
  async function request(url, body) {
    const response = await fetch(url, {method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
    let data; try { data = await response.json(); } catch { throw Error('服务器没有返回可读取的结果，请稍后重试。'); }
    if (!response.ok) throw Error(errorText(data.detail));
    return data;
  }
  const stateNames = {running: '处理中', processing: '处理中', idle: '等待新观测', ready: '已就绪', disabled: '未启用', error: '处理受阻', failed: '处理受阻', paused: '已暂停', degraded: '部分来源暂不可用'};
  async function loadStatus() {
    const host = byId('attention-status');
    try {
      const data = await request('/api/attention/status');
      host.replaceChildren(); host.classList.toggle('warning', data.enabled === false || Boolean(data.error));
      host.append(node('span', data.enabled === false ? '议题处理未启用' : (stateNames[data.state] || '议题处理状态已读取')));
      if (numeric(data.backlog)) host.append(node('span', '待处理批次 ' + data.backlog + (data.backlog_limited ? '+' : '')));
      if (data.last_processed) host.append(node('span', '最近处理 ' + time(data.last_processed)));
      if (data.error) host.append(node('span', String(data.error), 'desk-error'));
      if (list(data.backfill_error).length) {
        host.classList.add('warning');
        for (const failure of list(data.backfill_error).slice(0, 2)) host.append(node('span', '旧快照回填受阻 · 采集 ' + failure.run_id + ' · ' + failure.error, 'desk-error'));
        host.append(button('重新发现旧快照', async event => {
          const control = event.currentTarget; control.disabled = true;
          try { await request('/api/attention/rescan', {}); await loadStatus(); }
          catch (error) { host.append(node('span', error.message, 'desk-error')); control.disabled = false; }
        }));
      }
      host.append(link('采集中心', '/intake'), button('刷新状态', loadStatus));
    } catch (error) {
      host.classList.add('warning'); host.replaceChildren(node('span', '处理状态暂不可用：' + error.message), button('重试', loadStatus), link('采集中心', '/intake'));
    }
  }
  const platformName = platform => object(page.source_names)[platform.platform] || platform.platform || platform.source_id || '来源未标识';
  const unchangedSnapshot = platform => platform.changed === false && platform.observation_state === 'observed' && ['comparable', 'cached_content_repeat'].includes(platform.continuity);
  const positionDelta = platform => {
    if (platform.observation_state === 'source_failed') return '来源失败 · 位置未知';
    if (platform.observation_state === 'not_returned') return '本次列表未返回 · 变化未知';
    if (platform.observation_state === 'stale') return '旧观测 · 变化未知';
    if (!numeric(platform.position_delta)) return '基线不足';
    if (platform.position_delta > 0) return '上移 ' + platform.position_delta + ' 位';
    if (platform.position_delta < 0) return '下移 ' + Math.abs(platform.position_delta) + ' 位';
    return '位置持平';
  };
  const baselineNames = {insufficient: '可比观测不足，暂不判断升温。', first_observation: '仅有首次观测，暂不判断升温。', not_continuously_observed: '观测不连续，无法确认期间的变化。', stale: '最近观察已过期，关注与升温暂为未知。'};
  function topicNotes(topic) {
    const info = summary(topic), notes = list(info.notes).filter(value => typeof value === 'string');
    if (!numeric(topic.rise_score) && !notes.some(value => /基线|可比|首次/.test(value))) notes.unshift(baselineNames[topic.baseline_state] || '升温基线不足，暂无可用变化分。');
    return [...new Set(notes)];
  }
  for (const [key, topic] of topics) {
    const info = summary(topic), platforms = list(info.platforms);
    const host = [...document.querySelectorAll('[data-attention-position]')].find(element => element.dataset.attentionPosition === key);
    if (host) {
      const comparable = platforms.filter(row => numeric(row.position_delta) && row.observation_state === 'observed');
      if (comparable.length) host.textContent = '返回列表位置 · ' + comparable.slice(0, 3).map(row => platformName(row) + ' ' + positionDelta(row)).join('；');
      else host.textContent = '返回列表位置 · 暂无可比变化；查看来源观测。';
    }
    const notesHost = [...document.querySelectorAll('[data-attention-notes]')].find(element => element.dataset.attentionNotes === key);
    if (notesHost) {
      const notes = [];
      if (platforms.some(row => row.observation_state === 'source_failed')) notes.push('部分来源失败，缺失观测为未知。');
      if (platforms.some(row => row.cached || unchangedSnapshot(row))) notes.push('含缓存或重复快照，不视为新升温。');
      if (platforms.some(row => row.observation_state === 'not_returned')) notes.push('部分来源本次未返回此议题，变化未知。');
      if (platforms.some(row => row.stale)) notes.push('含超过计分有效期的旧观测，请查看时间依据。');
      notesHost.replaceChildren(...notes.slice(0, 3).map(text => node('p', text)));
    }
  }
  function section(title) {
    const element = node('section', undefined, 'attention-detail-section'); element.append(node('h3', title)); byId('attention-detail-content').append(element); return element;
  }
  const methodNames = {new_topic: '首次建立议题', same_title: '标准化标题一致', conservative_title: '实体与进展词相近', same_url: '原文地址一致', canonical_url: '规范原文地址一致', url: '原文地址一致', exact_title: '标准化标题一致', title_signature: '标题特征相近', title_similarity: '标题相似', entity_action: '实体与进展词匹配', entity_overlap: '实体词项重合', structured_candidate: '主体、行为与时空线索匹配', source_document_url: '首次报道地址匹配', mention_document_url: '提及报道地址匹配', keyword: '词项召回', keyword_recall: '专题词项召回', rules: '规则匹配', rule: '规则匹配', llm: '模型辅助判断'};
  const displayMethod = value => methodNames[value] || (value ? String(value) : '未提供归并方式');
  const reasonNames = {entities: '实体词项', entity_overlap: '重合实体', shared_entities: '共同实体', terms: '命中词项', keywords: '关键词', matched_terms: '命中词项', title_terms: '标题词项', tokens: '标题特征', actions: '进展词', shared_actions: '共同进展词', locations: '地点', date: '时间', time: '时间', window: '时间窗', title_similarity: '标题相似度', similarity: '相似度', url: '原文地址', same_url: '原文地址一致', score: '关联排序分', note: '说明', reason: '依据', description: '说明', method: '方式'};
  function reasons(value) {
    if (typeof value === 'string') { try { return reasons(JSON.parse(value)); } catch { return value ? [value] : []; } }
    if (Array.isArray(value)) return value.flatMap(item => reasons(item));
    if (!value || typeof value !== 'object') return value === undefined || value === null ? [] : [String(value)];
    if (value.message) return [String(value.message)];
    if (value.term) return [String(value.term)];
    return Object.entries(value).filter(([key, item]) => item !== null && item !== undefined && key !== 'version').map(([key, item]) => (reasonNames[key] || key) + '：' + (Array.isArray(item) ? item.map(part => typeof part === 'object' ? reasons(part).join('，') : String(part)).join('、') : typeof item === 'object' ? reasons(item).join('，') : String(item)));
  }
  function appendReasons(host, values, empty) {
    const rows = [...new Set(reasons(values))].filter(Boolean);
    if (!rows.length) { if (empty) host.append(node('p', empty, 'attention-detail-note')); return; }
    const ul = node('ul', undefined, 'attention-detail-note'); for (const row of rows) ul.append(node('li', row)); host.append(ul);
  }
  const svgNode = (tag, attributes, text) => {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attributes || {})) element.setAttribute(key, String(value));
    if (text !== undefined) element.textContent = String(text);
    return element;
  };
  function drawChart(host, series) {
    const points = list(series).filter(point => Number.isFinite(new Date(point.observed_at).valueOf())).sort((a, b) => new Date(a.observed_at) - new Date(b.observed_at));
    const observed = points.filter(point => numeric(point.attention_score));
    if (!observed.length) { host.append(node('p', '尚无可绘制的关注观测。缺失值保留为未知。', 'attention-detail-note')); return; }
    const width = 650, height = 220, left = 44, right = 20, top = 24, bottom = 42;
    const dates = points.map(point => new Date(point.observed_at).valueOf()), first = Math.min(...dates), last = Math.max(...dates);
    const max = Math.max(1, ...observed.map(point => point.attention_score));
    const x = value => first === last ? (width + left - right) / 2 : left + (new Date(value).valueOf() - first) / (last - first) * (width - left - right);
    const y = value => height - bottom - Math.max(0, value) / max * (height - top - bottom);
    const box = node('div', undefined, 'attention-chart');
    const svg = svgNode('svg', {viewBox: '0 0 ' + width + ' ' + height, role: 'img', 'aria-label': '关注排序分随本站本地观察时间的采样点图。空心点含缓存，黄色点包含不完整来源。'});
    svg.append(svgNode('title', {}, '关注观测时间线：本站采样，不是人数或平台官方热度曲线'));
    for (const fraction of [0, 0.5, 1]) {
      const value = max * fraction, py = y(value);
      svg.append(svgNode('line', {x1: left, x2: width - right, y1: py, y2: py, class: 'attention-chart-grid'}), svgNode('text', {x: left - 8, y: py + 4, 'text-anchor': 'end'}, value.toFixed(value < 10 ? 1 : 0)));
    }
    svg.append(svgNode('text', {x: left, y: 13}, '关注排序分'));
    for (const [value, anchor, px] of [[first, 'start', left], [last, 'end', width - right]]) svg.append(svgNode('text', {x: px, y: height - 15, 'text-anchor': anchor}, shortTime(value)));
    /* Only observed points are drawn: no interpolated line or zero-filled gaps. */
    for (const point of points) {
      if (!numeric(point.attention_score)) continue;
      const cached = Boolean(point.cached), color = point.partial ? '#e8be73' : '#5dd5c4';
      const circle = svgNode('circle', {cx: x(point.observed_at), cy: y(point.attention_score), r: 4.5, fill: cached ? '#0c1924' : color, stroke: color, 'stroke-width': 2, tabindex: '0'});
      const label = time(point.observed_at) + ' · 关注排序分 ' + score(point.attention_score) + ' · 中文热榜平台 ' + (numeric(point.platform_count) ? point.platform_count : '未知') + (cached ? ' · 含缓存' : '') + (point.partial ? ' · 部分来源不完整' : '');
      circle.setAttribute('aria-label', label); circle.append(svgNode('title', {}, label)); svg.append(circle);
    }
    const legend = node('div', undefined, 'attention-chart-legend');
    for (const [label, color] of [['本地观察', '#5dd5c4'], ['来源不完整', '#e8be73']]) { const text = node('span'); const dot = node('i'); dot.style.backgroundColor = color; text.append(dot, document.createTextNode(label)); legend.append(text); }
    legend.append(node('span', '空心点：含缓存'));
    box.append(svg, legend); host.append(box, node('p', '时间为本站观察时间（北京时间）。散点保留采样间隔，不连接缺失时段；重复缓存不代表新一轮关注。', 'attention-detail-note'));
  }
  function observations(host, platforms) {
    if (!platforms.length) { host.append(node('p', '尚未提供分平台观测记录。', 'attention-detail-note')); return; }
    const wrapper = node('div', undefined, 'attention-table-wrap'); wrapper.tabIndex = 0; wrapper.setAttribute('aria-label', '各平台观测记录，可横向滚动');
    const table = node('table', undefined, 'attention-observation-table'), head = node('thead'), header = node('tr');
    for (const label of ['平台与状态', '返回列表位置', '变化', '本地最近观察']) header.append(node('th', label));
    head.append(header); table.append(head); const body = node('tbody');
    for (const platform of platforms) {
      const row = node('tr'), source = node('td', platformName(platform));
      const flags = [];
      if (platform.counts_for_attention === false) flags.push('不计入中文热榜关注分');
      if (platform.cached) flags.push('含缓存');
      if (unchangedSnapshot(platform)) flags.push('快照未变化');
      if (platform.stale) flags.push('旧观测');
      if (platform.observation_state === 'source_failed') flags.push('来源失败');
      if (platform.observation_state === 'not_returned') flags.push('本次列表未返回');
      if (!flags.length) flags.push('已观察');
      source.append(node('small', flags.join(' · ')));
      const position = node('td', numeric(platform.list_position) ? String(platform.list_position) + (numeric(platform.list_size) ? ' / ' + platform.list_size : '') + (platform.stale ? '（旧观测）' : '') : '未知');
      if (numeric(platform.previous_list_position)) position.append(node('small', '上一可比位置 ' + platform.previous_list_position));
      if (numeric(platform.normalized_position)) position.append(node('small', '位置归一分 ' + score(platform.normalized_position)));
      if (typeof platform.raw_metric_text === 'string' && platform.raw_metric_text) position.append(node('small', '平台原文：' + platform.raw_metric_text));
      const delta = node('td', positionDelta(platform));
      const observedAt = node('td', time(platform.observed_at));
      if (platform.snapshot_reported_at) observedAt.append(node('small', '上游返回时间 ' + time(platform.snapshot_reported_at)));
      if (platform.content_unchanged_since && unchangedSnapshot(platform)) observedAt.append(node('small', '同一快照自 ' + time(platform.content_unchanged_since)));
      row.append(source, position, delta, observedAt); body.append(row);
      for (const observation of list(platform.observations).slice(-8)) {
        const historic = node('tr'); historic.append(node('td', '↳ 历次本地观察'), node('td', numeric(observation.list_position) ? observation.list_position : '未知'), node('td', observation.status === 'failed' ? '来源失败' : observation.cached ? '含缓存' : numeric(observation.list_position) ? '已观察' : '本次未返回'), node('td', time(observation.observed_at))); body.append(historic);
      }
    }
    table.append(body); wrapper.append(table); host.append(wrapper, node('p', '位置仅指采集到的返回列表顺序；位置归一分为0–100，越靠前越高。上移为本来源内比较，不能跨平台比较。未返回、失败和中断都保留为未知。原始热度文字按平台原样展示，不换算为人数。', 'attention-detail-note'));
  }
  const detailDialog = byId('attention-detail-dialog'), detailHost = byId('attention-detail-content');
  let currentTopic = '', detailGeneration = 0, returnFocus = null;
  function closeDetail() { if (detailDialog.open) detailDialog.close(); }
  byId('attention-detail-close').addEventListener('click', closeDetail);
  detailDialog.addEventListener('close', () => {
    detailGeneration++; currentTopic = '';
    const url = new URL(location.href); url.searchParams.delete('attention_topic'); history.replaceState(null, '', url);
    if (returnFocus?.isConnected) returnFocus.focus(); returnFocus = null;
  });
  detailDialog.addEventListener('click', event => { if (event.target === detailDialog) { const bounds = detailDialog.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right) closeDetail(); } });
  function selectionActions(host, topic) {
    const actions = node('div', undefined, 'row attention-selection-entry');
    if (topic.selected_task) { const anchor = link('继续选题 →', '/workbench/' + encodeURIComponent(topic.selected_task) + '/selection'); anchor.className = 'action-link'; actions.append(anchor); }
    actions.append(button(topic.selected_task ? '另起一个角度' : '形成选题', () => openSelection(topic.key, topic.title), topic.selected_task ? 'ghost' : '')); host.append(actions);
  }
  function renderDetail(data, generation) {
    const topic = {...(topics.get(currentTopic) || {}), ...object(data.topic)}, info = summary(topic);
    if (topic.key) topics.set(String(topic.key), topic);
    byId('attention-detail-title').textContent = topic.title || '议题详情'; detailHost.replaceChildren();
    const overview = section('为什么出现在这里');
    const metrics = node('div', undefined, 'attention-metrics');
    for (const [value, label] of [[numeric(topic.platform_count) ? topic.platform_count : '未知', '中文热榜平台'], [score(topic.attention_score), '关注排序分'], [score(topic.rise_score), '升温排序分']]) { const box = node('div', undefined, 'attention-metric'); box.append(node('strong', value), node('span', label)); metrics.append(box); }
    overview.append(metrics, node('p', '首次观察 ' + time(topic.first_seen) + '；最近观察 ' + time(topic.last_seen), 'attention-detail-note'));
    if (info.computed_at) overview.append(node('p', '分数计算于 ' + time(info.computed_at) + '，以当时最近6小时的有效观测计分。', 'attention-detail-note'));
    appendReasons(overview, data.notes || topicNotes(topic));
    overview.append(node('p', '关注分用于发现议题，不能证明事实成立；升温只比较可比的本来源观测。', 'attention-detail-note'));
    selectionActions(overview, topic);
    const chart = section('本地关注观测时间线'); drawChart(chart, data.series || topic.series || info.series);
    const platformSection = section('各平台观测依据'); observations(platformSection, list(info.platforms));
    const sources = section('原文与条目归并');
    sources.append(node('p', '以下条目被归并为同一议题，仍可能存在语义歧义。请用原文核对人物、时间与具体进展。', 'attention-detail-note'));
    if (data.items_limited) sources.append(node('p', '本面板显示最近30个归并条目，其余条目保留在议题索引中。', 'attention-detail-note'));
    for (const item of list(data.items)) {
      const source = node('article', undefined, 'attention-source');
      const href = externalUrl(item.url || item.canonical_url), heading = node('h4');
      heading.append(href ? link(item.title || '查看原文', href, true) : node('span', item.title || '标题未提供')); source.append(heading);
      const meta = node('div', undefined, 'attention-source-meta'); meta.append(node('span', platformName(item)), node('span', numeric(item.list_position) ? '最近返回位置 ' + item.list_position : '返回位置未知'), node('span', '最近观察 ' + time(item.last_seen))); source.append(meta);
      if (!href) source.append(node('p', '尚无可访问的原文地址。', 'attention-detail-note'));
      if (item.excerpt) { const details = node('details'), summaryNode = node('summary', '已保存的条目摘要'); details.append(summaryNode, node('p', item.excerpt)); source.append(details); }
      const grouping = object(item.grouping || item.grouping_json);
      source.append(node('p', '归并方式 · ' + displayMethod(grouping.method), 'attention-detail-note'));
      appendReasons(source, grouping.reasons || grouping.evidence || grouping, '未提供额外归并依据。');
      sources.append(source);
    }
    if (!list(data.items).length) sources.append(node('p', '暂无已保存的条目来源。', 'attention-detail-note'));
    const matches = section('GDELT 事件关联');
    matches.append(node('p', '这些是主体、时间与词项召回的候选关联。确认只表示同一议题关联成立，官方自动编码仍需核原文。', 'attention-detail-note'));
    const matching = object(info.matching), matchScope = object(matching.scope);
    if (topic.match_error) matches.append(node('p', '最近匹配处理受阻：' + topic.match_error, 'desk-error'));
    if (topic.match_checked_at || matching.checked_at) matches.append(node('p', '最近检查 ' + time(topic.match_checked_at || matching.checked_at), 'attention-detail-note'));
    if (Object.keys(matchScope).length) {
      const scopeDetails = node('details', undefined, 'attention-detail-note'); scopeDetails.append(node('summary', '候选召回范围'));
      if (matchScope.observed_after || matchScope.observed_before) scopeDetails.append(node('p', '官方记录最近观测范围：' + time(matchScope.observed_after) + ' 至 ' + time(matchScope.observed_before) + '；不是事件发生日期范围。'));
      if (numeric(matchScope.events_read)) scopeDetails.append(node('p', '本轮读取事件记录 ' + matchScope.events_read + (matchScope.event_truncated ? '（已达到检索上限）' : '')));
      if (numeric(matchScope.mentions_read)) scopeDetails.append(node('p', '本轮读取报道提及 ' + matchScope.mentions_read + (matchScope.mention_truncated ? '（已达到检索上限）' : '')));
      scopeDetails.append(node('p', '关联排序分是规则分，不是事实成立的概率。')); matches.append(scopeDetails);
    }
    appendReasons(matches, matching.unmatched_reasons);
    for (const match of list(data.events)) {
      const event = object(match.event), card = node('article', undefined, 'attention-match');
      const status = {confirmed: '已确认关联', rejected: '已拒绝关联', candidate: '候选关联'}[match.status] || '候选关联';
      card.append(node('span', status, 'badge ' + (match.status === 'confirmed' ? 'ok' : match.status === 'rejected' ? '' : 'warn')));
      if (match.current === false) card.append(node('p', '历史关联，本轮未再召回；请结合当前条目重新核对。', 'attention-detail-note'));
      const heading = node('h4'); heading.append(link(event.title || 'GDELT事件 ' + match.event_id, '/radar?source=gdelt&event=' + encodeURIComponent(match.event_id))); card.append(heading);
      card.append(node('p', '发生日期 ' + (event.event_date || '未知') + ' · ' + displayMethod(match.method) + (numeric(match.score) ? ' · 关联排序分 ' + score(match.score) : ''), 'attention-detail-note'));
      appendReasons(card, match.reasons);
      const actions = node('div', undefined, 'row');
      const message = node('p', '', 'attention-match-status'); message.setAttribute('role', 'status');
      const options = match.status === 'confirmed' ? [['取消确认', 'candidate'], ['拒绝关联', 'rejected']] : match.status === 'rejected' ? [['恢复为候选', 'candidate']] : [['确认关联', 'confirmed'], ['拒绝关联', 'rejected']];
      for (const [label, statusValue] of options) actions.append(button(label, async () => {
        for (const control of actions.querySelectorAll('button')) control.disabled = true;
        message.textContent = '正在保存…';
        try {
          await request('/api/attention/matches/' + encodeURIComponent(match.key), {status: statusValue});
          if (generation === detailGeneration && detailDialog.open) await loadDetail(currentTopic, generation);
        } catch (error) { if (generation === detailGeneration) { message.textContent = error.message; for (const control of actions.querySelectorAll('button')) control.disabled = false; } }
      }));
      card.append(actions, message); matches.append(card);
    }
    if (!list(data.events).length) matches.append(node('p', '暂无候选事件关联。尚未匹配不代表全球没有相关事件。', 'attention-detail-note'));
    const watches = section('专题词项召回');
    watches.append(node('p', '这里只说明议题命中了哪些专题词项，不表示已经加入专题材料或完成研究。', 'attention-detail-note'));
    for (const watch of list(data.watches)) {
      const card = node('article', undefined, 'attention-watch'), heading = node('h4'); heading.append(link(watch.name || '查看专题', '/focus/' + encodeURIComponent(watch.key))); card.append(heading);
      card.append(node('p', '关键词召回' + (watch.scope_version ? ' · 专题范围版本 ' + watch.scope_version : ''), 'attention-detail-note')); appendReasons(card, watch.reasons); watches.append(card);
    }
    if (!list(data.watches).length) watches.append(node('p', '暂无专题词项命中。可以调整已有专题的关注范围。', 'attention-detail-note'));
  }
  async function loadDetail(key, generation) {
    try { const data = await request('/api/attention/topics/' + encodeURIComponent(key)); if (generation === detailGeneration && detailDialog.open) renderDetail(data, generation); }
    catch (error) { if (generation === detailGeneration && detailDialog.open) { detailHost.replaceChildren(node('p', error.message, 'desk-error'), button('重试加载', () => loadDetail(key, generation), 'ghost attention-retry')); } }
  }
  function openDetail(key, trigger) {
    currentTopic = String(key); returnFocus = trigger || document.activeElement; const generation = ++detailGeneration;
    byId('attention-detail-title').textContent = topics.get(currentTopic)?.title || '议题详情'; detailHost.replaceChildren(node('p', '正在读取来源与关联…', 'desk-meta'));
    const url = new URL(location.href); url.searchParams.set('attention_topic', currentTopic); history.replaceState(null, '', url);
    if (!detailDialog.open) detailDialog.showModal(); loadDetail(currentTopic, generation);
  }
  for (const control of document.querySelectorAll('[data-attention-open]')) control.addEventListener('click', () => openDetail(control.dataset.attentionOpen, control));
  const selectionDialog = byId('attention-select-dialog');
  let selectionKey = '', requestId = '', selectBusy = false;
  function openSelection(key, title) {
    selectionKey = String(key); requestId = globalThis.crypto?.randomUUID?.() || ('attention-' + Date.now() + '-' + Math.random().toString(36).slice(2));
    byId('attention-selection-title').value = title || topics.get(selectionKey)?.title || '新选题'; byId('attention-selection-angle').value = ''; byId('attention-select-message').textContent = '';
    selectionDialog.showModal(); byId('attention-selection-title').focus();
  }
  for (const control of document.querySelectorAll('[data-attention-select]')) control.addEventListener('click', () => openSelection(control.dataset.attentionSelect));
  byId('attention-select-close').addEventListener('click', () => { if (!selectBusy) selectionDialog.close(); });
  selectionDialog.addEventListener('cancel', event => { if (selectBusy) event.preventDefault(); });
  byId('attention-select-form').addEventListener('submit', async event => {
    event.preventDefault(); if (selectBusy) return; selectBusy = true;
    const submit = byId('attention-select-submit'); submit.disabled = true; byId('attention-select-close').disabled = true; byId('attention-select-message').textContent = '正在建立选题…';
    try {
      const data = await request('/api/attention/topics/' + encodeURIComponent(selectionKey) + '/select', {request_id: requestId, title: byId('attention-selection-title').value, angle: byId('attention-selection-angle').value});
      if (typeof data.url !== 'string' || !/^\/workbench\/[A-Za-z0-9_-]+(?:\/selection)?$/.test(data.url)) throw Error('请到内容工作台核对这项任务。');
      location.assign(data.url);
    } catch (error) { byId('attention-select-message').textContent = error.message; }
    finally { selectBusy = false; submit.disabled = false; byId('attention-select-close').disabled = false; }
  });
  if (byId('attention-message').textContent.trim()) byId('attention-message').append(document.createTextNode(' '), button('重新加载本页', () => location.reload(), 'ghost attention-retry'));
  loadStatus();
  const openTopic = page.open_topic || new URL(location.href).searchParams.get('attention_topic');
  if (openTopic) openDetail(openTopic);
})();

(() => {
  const select = (id) => document.getElementById(id);
  const bootstrap = JSON.parse(select('world-bootstrap').textContent);
  const form = select('world-filters');
  const { day, days, country, category, signal, layer } = form.elements;
  const tabs = Array.from(document.querySelectorAll('[data-world-view]'));
  const panel = select('world-list');
  const status = select('world-status');
  const headings = { events: '有新进展的议题', signals: '报道升温', market: '商品研究线索' };
  const riskStates = {
    unchecked: '未核对归档', pending: '待分析', missing: '缺归档', partial: '覆盖不足',
    live: '盘中累计', complete: '已结算', disabled: '汇总未启用', error: '暂不可读取',
  };
  const signalStatus = {
    disabled: '日度汇总未启用，仍可查看已有记录', error: '后台汇总遇到错误',
    archive_absent: '后台找不到 GKG 原始归档', unchecked: '选定日期归档尚未核对',
    config_error: '日度汇总配置有误', ready: '按 GKG 报道统计，显示地区汇总',
  };
  const kindNames = { local: '已整理事件', gdelt: 'GDELT 官方事件线索', report: '采集报道' };
  const stageNames = {
    announced: '宣布', announcement: '宣布', effective: '生效', implementation: '执行',
    resumed: '恢复', resumed_operations: '恢复', scheduled: '计划', reported: '报道',
    occurred: '发生', planned: '计划', observed: '收录', candidate: '待核',
    ongoing: '持续', verified: '已核对', followup: '后续进展',
  };
  const clock = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });
  const mapToggle = select('world-toggle-map');
  mapToggle.addEventListener('click', () => {
    const map = select('world-map');
    map.hidden = !map.hidden;
    mapToggle.setAttribute('aria-expanded', String(!map.hidden));
    mapToggle.textContent = map.hidden ? '展开地图' : '收起地图';
  });
  const pins = new Map();
  let snapshot = null;
  let shapes = null;
  let request = null;
  let active = '';
  let storyPage = 1;
  let loadError = '';

  function element(tag, text = '', className = '') {
    const item = document.createElement(tag);
    if (text) item.textContent = text;
    if (className) item.className = className;
    return item;
  }

  function svg(tag, attrs) {
    const item = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attrs)) item.setAttribute(key, value);
    return item;
  }

  function link(text, value, outside = false) {
    if (typeof value !== 'string' || !value || /[\u0000-\u0020\\]/.test(value)) return null;
    let address;
    try {
      address = new URL(value, window.location.origin);
    } catch {
      return null;
    }
    const local = value.startsWith('/') && address.origin === window.location.origin && !value.startsWith('//');
    if (!local && (!outside || !/^https?:\/\//i.test(value) || address.username || address.password)) return null;
    const item = element('a', text);
    item.href = local ? value : address.href;
    if (!local) {
      item.target = '_blank';
      item.rel = 'noopener noreferrer';
    }
    return item;
  }

  function options(control, fields, empty, selected = '') {
    control.replaceChildren(new Option(empty, ''));
    for (const [id, label] of fields) control.append(new Option(label, id));
    control.value = selected;
    if (control.selectedIndex < 0) control.value = '';
  }

  function applyFilters(values = {}) {
    day.value = values.day || bootstrap.today;
    days.value = [1, 3, 7].includes(Number(values.days)) ? String(values.days) : '3';
    country.value = values.country || '';
    category.value = values.category || '';
    if (values.signal) {
      if (![...signal.options].some((option) => option.value === values.signal)) {
        signal.append(new Option(values.signal, values.signal));
      }
      signal.value = values.signal;
    } else {
      signal.value = '';
    }
    const pageNumber = Number(values.page);
    storyPage = Number.isSafeInteger(pageNumber) && pageNumber > 0 && pageNumber <= 10000000 ? pageNumber : 1;
    layer.value = Object.hasOwn(headings, values.layer) ? values.layer : 'events';
  }

  function countryName(code) {
    return snapshot?.countries?.[code] || bootstrap.countries[code] || code?.toUpperCase() || '';
  }

  function positionLabel(row) {
    return [row.place, countryName(row.country)].filter((value, index, names) => value && names.indexOf(value) === index).join(' · ') || '行动地点待核';
  }

  function timeLabel(row) {
    if (row.occurred_on && /^\d{4}-\d{2}-\d{2}$/.test(row.occurred_on)) return '发生 ' + row.occurred_on;
    if (!row.observed_at) return '';
    const stamp = new Date(row.observed_at);
    return Number.isNaN(stamp.valueOf()) ? '' : '收录 ' + clock.format(stamp);
  }

  function safeFlag(value, names) {
    if (!value) return '';
    return names[value] || (/^[a-z0-9_ -]+$/i.test(value) ? '' : value);
  }

  function card(row, market) {
    const item = element('article', '', 'world-event');
    item.dataset.worldEvent = row.key;
    item.tabIndex = -1;
    const categoryLabel = row.category_label || snapshot.categories?.[row.category];
    const type = market ? '待核线索' : kindNames[row.source_kind] || '';
    const labels = [categoryLabel, type, row.status === 'reviewed' ? '已核对' : '', safeFlag(row.stage, stageNames)];
    const label = [...new Set(labels.filter(Boolean))].join(' · ');
    if (label) item.append(element('p', label, 'world-event-meta'));
    const title = element('h3');
    title.append(link(row.title, row.event_path) || document.createTextNode(row.title));
    const geography = row.geography_basis === 'gdelt_action_geo' ? '地点由 GDELT 编码' : '';
    item.append(title, element('p', [positionLabel(row), timeLabel(row), geography].filter(Boolean).join(' · '), 'world-event-meta'));

    const actions = element('div', '', 'world-actions');
    if (market) {
      const lead = row.market || {};
      const targets = (lead.products || []).map((product) => product.label).filter(Boolean).join('、');
      item.append(element('p', [lead.channel, targets || lead.product_label || '相关商品待核'].filter(Boolean).join(' · ')));
      if (lead.rationale) item.append(element('p', lead.rationale));
      if (lead.next_check) item.append(element('p', /^核对/.test(lead.next_check)
        ? '优先' + lead.next_check : '优先核对 ' + lead.next_check, 'world-event-meta'));
      if (lead.indicators?.length) item.append(element('p', '建议观察 ' + lead.indicators.join('、'), 'world-event-meta'));
      const research = link('研究市场影响', row.research_path);
      if (research) {
        research.className = 'world-research';
        actions.append(research);
      }
    }

    if (row.excerpt) {
      const excerpt = element('details', '', 'world-excerpt');
      excerpt.append(element('summary', '证据摘录'), element('p', row.excerpt));
      item.append(excerpt);
    }
    const original = link('原文 ↗', row.source_url, true);
    const version = row.source_path !== row.source_url && link('已收录版本', row.source_path);
    if (original) {
      if (row.source_title) original.title = row.source_title;
      actions.append(original);
    }
    if (version) actions.append(version);
    if (actions.childElementCount) item.append(actions);
    return item;
  }

  function storyCard(story, byKey) {
    const box = element('details', '', 'world-event');
    if (story.id) box.dataset.storyId = story.id;
    const summary = element('summary');
    summary.append(element('strong', story.title), element('p',
      (story.id ? '窗口内 ' : '') + story.count + ' 项进展 · ' + story.reports + ' 篇可查报道', 'world-event-meta'));
    box.append(summary);
    for (const key of story.keys) {
      const row = byKey.get(key);
      if (!row) continue;
      const entry = element('section', '', 'world-progress');
      entry.dataset.worldEvent = key;
      entry.tabIndex = -1;
      entry.append(element('p', [timeLabel(row), positionLabel(row), row.category_label,
        row.stage, row.status === 'coded' ? 'GDELT 自动编码' : ''].filter(Boolean).join(' · '), 'world-event-meta'));
      entry.append(link(row.title, row.event_path) || element('strong', row.title));
      const sources = element('details');
      sources.append(element('summary', story.id && row.sources_truncated
        ? '查看报道与证据 · 最多显示 16 条原文版本' : '查看报道与证据'));
      for (const source of row.sources) {
        const line = element('p');
        line.append(link(source.title || '原文', source.url, true) || element('span', source.title));
        const saved = source.path !== source.url && link(' 收录版本', source.path);
        if (saved) line.append(saved);
        sources.append(line);
        if (source.excerpt) sources.append(element('blockquote', source.excerpt));
      }
      entry.append(sources);
      const research = link('研究市场影响', row.research_path);
      if (research) entry.append(research);
      box.append(entry);
    }
    if (story.path) {
      const timeline = link('完整时间线', story.path);
      if (timeline) box.append(timeline);
    }
    return box;
  }

  function indexStatus(info) {
    const labels = { disabled: '后台索引已停用', pending: '等待后台整理', indexing: '后台正在整理',
      scanned: '最近一轮回扫完成', error: '后台索引遇错' };
    const meta = [labels[info.state] || '索引状态待核'];
    if (info.scanned > 0) meta.push('全库本轮已扫 ' + info.scanned + ' 条记录，其中 ' + info.indexed + ' 条产生进展');
    return meta.join(' · ');
  }

  function hotspotCard(row) {
    const item = element('div', '', 'world-hotspot');
    const title = link(row.title, row.focus_url);
    item.append(title || element('span', row.title));
    if (row.status_label) item.append(element('span', row.status_label, 'badge'));
    const original = (row.sources || []).map((source) => link('代表报道 ↗', source.url, true)).find(Boolean);
    const meta = element('small', [row.location, '近 36 小时 ' + row.recent_reports + ' 篇', '此前 ' + row.previous_reports + ' 篇'].filter(Boolean).join(' · '));
    if (original) meta.append(' · ', original);
    item.append(meta);
    return item;
  }

  function sparkline(points) {
    if (!Array.isArray(points) || points.filter((point) => typeof point.score === 'number').length < 2) return null;
    const image = svg('svg', { viewBox: '0 0 230 52', role: 'img', 'aria-label': '近期日度报道指数趋势', class: 'world-signal-chart' });
    const max = Math.max(...points.map((point) => point.score || 0), 1);
    let previous = null;
    points.forEach((point, index) => {
      if (typeof point.score !== 'number' || !Number.isFinite(point.score)) {
        previous = null;
        return;
      }
      const cx = 5 + index * 220 / (points.length - 1);
      const cy = 45 - point.score * 40 / max;
      if (previous) image.append(svg('line', { x1: previous.cx, y1: previous.cy, x2: cx, y2: cy }));
      const marker = svg('circle', { cx, cy, r: point.spike ? 2.4 : 1.4, class: point.spike ? 'spike' : '' });
      marker.append(svg('title', {}));
      marker.firstChild.textContent = point.day + ' · ' + point.score.toFixed(2);
      image.append(marker);
      previous = { cx, cy };
    });
    return image;
  }

  function signalCard(row) {
    const item = element('article', '', 'world-signal');
    item.dataset.worldSignal = row.scope;
    item.tabIndex = -1;
    const title = element('h3');
    title.append(link(row.label, row.path) || document.createTextNode(row.label));
    item.append(title);
    const state = row.spike ? '异常升温' : riskStates[row.state] || '状态待确认';
    item.append(element('p', [row.day, state].filter(Boolean).join(' · '), row.spike ? 'world-anomaly' : 'world-event-meta'));
    const counts = [];
    if (typeof row.doc_count === 'number') counts.push(row.doc_count + ' 篇去重报道');
    if (typeof row.coverage === 'number') counts.push('批次覆盖 ' + (row.coverage * 100).toFixed(0) + '%');
    if (typeof row.score === 'number') counts.push('日指数 ' + row.score.toFixed(2));
    if (Number.isFinite(row.zscore)) counts.push('较基线 ' + row.zscore.toFixed(2) + ' 个标准差');
    if (counts.length) item.append(element('p', counts.join(' · '), 'world-event-meta'));
    const chart = sparkline(row.points);
    if (chart) item.append(chart);
    const details = link('查看日度数据与原文', row.path);
    if (details) {
      const actions = element('div', '', 'world-actions');
      actions.append(details);
      item.append(actions);
    }
    return item;
  }

  function regionScopes() {
    const rows = snapshot?.signals?.items || [];
    const matches = country.value ? rows.filter((row) => row.scope === 'global' || row.countries?.includes(country.value)) : rows;
    return matches.slice().sort((left, right) => Number(Boolean(right.spike)) - Number(Boolean(left.spike)));
  }

  function shown() {
    if (layer.value === 'signals') return regionScopes();
    return snapshot?.[layer.value]?.items || [];
  }

  function selectCard(dataName, id) {
    for (const item of panel.querySelectorAll('[data-world-event], [data-world-scope], :scope > *')) {
      const match = item.dataset[dataName] === id;
      item.classList.toggle('is-selected', match);
      if (match) {
        const parent = item.closest('details');
        if (parent) parent.open = true;
        item.scrollIntoView({ block: 'nearest' });
        item.focus({ preventScroll: true });
      }
    }
    if (dataName === 'worldEvent') {
      active = id;
      for (const [key, mark] of pins) mark.classList.toggle('is-selected', key === id);
    }
  }

  function keyboardAction(el, label, action) {
    el.setAttribute('tabindex', '0');
    el.setAttribute('role', 'button');
    el.setAttribute('aria-label', label);
    el.addEventListener('click', action);
    el.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        action();
      }
    });
  }

  function geographyStatus() {
    const modes = {
      events: [['point', '行动地点'], ['country', '仅确定国家']],
      market: [['market', '待核线索地点'], ['country', '仅确定国家']],
      signals: [['spike', '异常升温'], ['country', '有日度数据'], ['unknown', '覆盖或分析不足'], ['unmapped', '无地区汇总']],
    };
    if (layer.value === 'events' && snapshot?.indexed && !snapshot.preview) {
      modes.events = [['point', '本页议题最近进展'], ['country', '本页议题所在国家']];
    }
    select('world-legend').replaceChildren(...modes[layer.value].map(([id, label]) => element('span', label, 'world-legend-item ' + id)));
  }

  function drawMap(rows) {
    geographyStatus();
    const map = select('world-regions');
    const marks = select('world-points');
    map.replaceChildren();
    marks.replaceChildren();
    pins.clear();
    if (!shapes) return;
    const countOnly = new Map();
    const storyCountries = new Map();
    if (layer.value === 'events' && snapshot?.indexed && !snapshot.preview) {
      for (const story of snapshot.indexed.items) {
        for (const code of story.countries) storyCountries.set(code, (storyCountries.get(code) || 0) + 1);
      }
    }
    const signals = new Map();
    const mode = layer.value;
    for (const row of rows) {
      if (mode === 'signals' && row.scope !== 'global') {
        for (const code of row.countries || []) {
          const current = signals.get(code);
          if (!current || !current.spike && (row.spike || typeof current.score !== 'number' && typeof row.score === 'number')) {
            signals.set(code, row);
          }
        }
      } else if (row.country && row.precision !== 'location') {
        countOnly.set(row.country, (countOnly.get(row.country) || 0) + 1);
      }
    }
    for (const [code, name, outline] of shapes) {
      const polygon = svg('path', { d: outline, class: 'world-map-region' });
      const details = svg('title', {});
      polygon.append(details);
      details.textContent = name;
      if (code) polygon.dataset.worldCountry = code;
      if (country.value && country.value === code) polygon.classList.add('is-selected');
      const currentSignal = signals.get(code);
      if (mode === 'signals' && currentSignal) {
        const insufficient = ['unchecked', 'pending', 'missing', 'partial', 'disabled', 'error'].includes(currentSignal.state);
        polygon.classList.add(currentSignal.spike ? 'is-signal-spike'
          : !insufficient && typeof currentSignal.score === 'number' ? 'is-signal-valid' : 'is-signal-unknown');
        details.textContent = name + ' · ' + currentSignal.label;
        keyboardAction(polygon, name + '，查看' + currentSignal.label + '报道数据', () => selectCard('worldSignal', currentSignal.scope));
      }
      if (mode !== 'signals' && (storyCountries.has(code) || countOnly.has(code))) {
        polygon.classList.add('is-counted');
        details.textContent = storyCountries.has(code)
          ? name + ' · 本页 ' + storyCountries.get(code) + ' 个议题'
          : name + ' · ' + countOnly.get(code) + ' 条，地点仅确定到国家';
        keyboardAction(polygon, details.textContent, () => {
          country.value = code;
          submitFilters();
        });
      }
      map.append(polygon);
    }
    if (mode === 'signals' || !window.d3) return;
    // Match desk-map.json's 604×260 Natural Earth projection with a 2 px margin.
    const project = window.d3.geoNaturalEarth1().scale(108.84992813).translate([302, 152.07556842]);
    for (const row of rows) {
      if (row.precision !== 'location' || !Number.isFinite(row.latitude) || !Number.isFinite(row.longitude)
          || Math.abs(row.latitude) > 90 || Math.abs(row.longitude) > 180) continue;
      const [x, y] = project([row.longitude, row.latitude]);
      if (x < 0 || x > 604 || y < 0 || y > 260) continue;
      const pin = svg('g', { transform: 'translate(' + x + ',' + y + ')', class: 'world-map-point' + (mode === 'market' ? ' is-market' : '') });
      pin.dataset.worldPoint = row.key;
      const title = svg('title', {});
      title.textContent = row.title + (row.geography_basis === 'gdelt_action_geo' ? ' · GDELT 自动定位' : '');
      pin.append(title, svg('circle', { r: 4 }));
      keyboardAction(pin, row.title, () => selectCard('worldEvent', row.key));
      if (active === row.key) pin.classList.add('is-selected');
      pins.set(row.key, pin);
      marks.append(pin);
    }
  }

  function render() {
    const mode = layer.value;
    const rows = shown();
    drawMap(rows);
    select('world-list-heading').textContent = headings[mode];
    for (const tab of tabs) tab.setAttribute('aria-pressed', String(tab.dataset.worldView === mode));
    for (const name of ['category', 'window', 'signal']) {
      const visible = name === 'signal' ? mode === 'signals' : mode !== 'signals';
      document.querySelector('[data-world-facet="' + name + '"]').hidden = !visible;
    }
    panel.replaceChildren();
    const pager = select('world-pager');
    pager.hidden = true;
    if (loadError || !snapshot) {
      status.textContent = loadError || '正在读取' + headings[mode] + '…';
    } else if (mode === 'signals') {
      status.textContent = signalStatus[snapshot.signals?.state] || (snapshot.signals?.enabled
        ? '按 GKG 报道统计，显示地区汇总' : '日度汇总未启用，仍可查看已有记录');
    } else {
      const unknown = rows.filter((row) => !row.country && row.precision !== 'location').length;
      const more = snapshot[mode]?.truncated ? ' · 有更多候选，请缩小范围' : '';
      status.textContent = rows.length + ' 条' + (unknown ? ' · ' + unknown + ' 条地点待核' : '') + more;
    }
    if (snapshot && !loadError) {
      if (mode === 'events' && snapshot.indexed && !snapshot.preview) {
        const data = snapshot.indexed;
        const pages = Math.ceil(data.total / data.limit);
        status.textContent = '窗口内已整理 ' + data.total + ' 个议题 · ' + indexStatus(snapshot.coverage);
        for (const story of data.items) {
          const latest = story.latest;
          panel.append(storyCard({ ...story, keys: [latest.key] }, new Map([[latest.key, latest]])));
        }
        pager.hidden = pages <= 1 && storyPage === 1;
        select('world-page').textContent = '第 ' + storyPage + ' / ' + Math.max(1, pages) + ' 页';
        select('world-prev').disabled = storyPage <= 1;
        select('world-next').disabled = storyPage >= pages;
      } else if (mode === 'events' && snapshot.events.stories) {
        const byKey = new Map(rows.map(row => [row.key, row]));
        status.textContent = snapshot.events.stories.length + ' 个议题 · ' + status.textContent;
        if (snapshot.preview) status.textContent = '窗口样本 · ' + status.textContent + ' · ' + indexStatus(snapshot.coverage);
        for (const story of snapshot.events.stories) panel.append(storyCard(story, byKey));
      } else for (const row of rows) panel.append(mode === 'signals' ? signalCard(row) : card(row, mode === 'market'));
      if (snapshot.time_basis === 'current_records' && day.value < bootstrap.today) status.textContent += ' · 按日期回看现存资料';
      if (!rows.length) panel.append(element('p', '当前筛选范围暂无可显示记录。', 'world-empty'));
    }
    const hotspots = (snapshot?.hotspots?.items || []).slice(0, 8);
    const showHotspots = mode === 'signals' && !country.value && !loadError && hotspots.length > 0;
    select('world-hotspots').replaceChildren(...(showHotspots ? hotspots.map(hotspotCard) : []));
    select('world-ongoing').hidden = !showHotspots;
  }

  function parameters() {
    const next = new URLSearchParams();
    for (const [key, value] of new FormData(form)) if (value) next.set(key, value);
    if (layer.value === 'events' && storyPage > 1) next.set('page', String(storyPage));
    return next;
  }

  async function load() {
    request?.abort();
    request = new AbortController();
    const pending = request;
    active = '';
    snapshot = null;
    loadError = '';
    render();
    panel.setAttribute('aria-busy', 'true');
    const query = parameters();
    if (layer.value === 'signals') query.delete('category');
    try {
      let endpoint = '/api/radar/world';
      if (layer.value === 'events') {
        endpoint += '/stories';
        query.delete('layer');
        query.delete('signal');
      } else {
        query.set('single', 'true');
      }
      const response = await fetch(endpoint + '?' + query, { signal: pending.signal });
      if (pending !== request) return;
      if (!response.ok) throw new Error('暂时无法读取世界态势，请重试。');
      const data = await response.json();
      if (pending !== request) return;
      snapshot = data;
      const signalOptions = snapshot.signals?.category_options;
      if (signalOptions && !Array.isArray(signalOptions)) {
        options(signal, Object.entries(signalOptions), '默认主题', snapshot.signals.category);
      }
    } catch (error) {
      if (pending !== request || error.name === 'AbortError') return;
      loadError = error.message || '世界态势暂不可用，请重试。';
    } finally {
      if (pending === request) {
        panel.removeAttribute('aria-busy');
        render();
      }
    }
  }

  function submitFilters(nextPage = 1) {
    storyPage = nextPage;
    history.pushState(null, '', '/radar/world?' + parameters());
    load();
  }

  select('world-prev').addEventListener('click', () => submitFilters(storyPage - 1));
  select('world-next').addEventListener('click', () => submitFilters(storyPage + 1));

  options(country, Object.entries(bootstrap.countries).sort((left, right) => left[1].localeCompare(right[1], 'zh-CN')), '全球');
  options(category, Object.entries(bootstrap.categories), '全部类别');
  applyFilters({ ...bootstrap.filters, ...Object.fromEntries(new URLSearchParams(window.location.search)) });
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    submitFilters();
  });
  for (const tab of tabs) {
    tab.addEventListener('click', () => {
      if (layer.value === tab.dataset.worldView) return;
      layer.value = tab.dataset.worldView;
      submitFilters();
    });
  }
  window.addEventListener('popstate', () => {
    applyFilters(Object.fromEntries(new URLSearchParams(window.location.search)));
    load();
  });
  fetch('/api/desk/map-shapes')
    .then((response) => {
      if (!response.ok) throw new Error();
      return response.json();
    })
    .then((data) => {
      shapes = data;
      drawMap(shown());
    })
    .catch(() => {
      const notice = select('world-map-error');
      notice.hidden = false;
      notice.textContent = '底图暂不可用，仍可从列表查看事件和地区。';
    });
  load();
})();

(() => {
  'use strict';

  const API = '/api/history-archive';
  const pageSize = 30;
  const el = id => document.getElementById('archive-' + id);
  const form = el('range');
  if (!form) return;

  const labels = {
    idle:'等待启动', planning:'正在核对官方清单', queued:'排队中', pending:'排队中',
    running:'补档中', paused:'已暂停', pausing:'正在暂停', complete:'已完成', completed:'已完成',
    ready:'已可查', available:'已可查', partial:'部分可查', gap:'待补档', missing:'官网清单未列',
    not_expected:'该年代没有此数据', not_requested:'按需补查', failed:'补档失败',
    retry:'待重试', waiting:'等待补档', retry_wait:'等待自动重试', error:'补档失败',
    disk_low:'磁盘空间不足', download:'下载', downloading:'下载', catalog:'下载官方清单', downloaded:'待转换',
    convert:'转换 Parquet', converting:'转换 Parquet', publish:'提交分区', publishing:'提交分区'
  };
  const kindLabels = {events:'Events', gkg:'GKG', mentions:'Mentions'};
  let offset = 0, loaded = false, busy = false, latest = null, poll = null, timer = null;
  let messageFromState = false;

  function number(value) {
    return Number.isFinite(value) && value >= 0 ? value.toLocaleString('zh-CN') : '—';
  }

  function bytes(value) {
    if (!Number.isFinite(value) || value < 0) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let n = 0;
    for (; value >= 1000 && n < units.length - 1; n++) value /= 1000;
    return value.toLocaleString('zh-CN', {maximumFractionDigits: n ? 2 : 0}) + ' ' + units[n];
  }

  function range(item) {
    if (!item?.date_from) return item?.date_to || '';
    return item.date_to && item.date_from !== item.date_to ? item.date_from + ' 至 ' + item.date_to : item.date_from;
  }

  function message(value, error = false, fromState = false) {
    const target = el('message');
    messageFromState = fromState;
    target.hidden = !value;
    target.dataset.error = String(error);
    target.textContent = value || '';
  }

  function text(tag, value) {
    const item = document.createElement(tag);
    item.textContent = value;
    return item;
  }

  function meter(key, count, expected, received, total) {
    const bar = el(key);
    if (Number.isFinite(expected) && expected > 0) {
      bar.max = expected;
      bar.value = Number.isFinite(count) ? count : 0;
    } else if (Number.isFinite(count)) {
      bar.max = 1;
      bar.value = 0;
    } else bar.removeAttribute('value');
    el(key + '-count').textContent = number(count) + ' / ' + number(expected) + ' 个 ZIP';
    el(key + '-bytes').textContent = bytes(received) + ' / ' + bytes(total) + '（按 ZIP 体积）';
  }

  function showCurrent(value) {
    const target = el('current');
    target.hidden = !value || !Object.keys(value).length;
    target.textContent = value ? [range(value), kindLabels[value.kind], value.phase && (labels[value.phase] || value.phase),
      (value.filename || value.stamp)].filter(Boolean).join(' · ') : '';
    const transfer = el('transfer');
    const received = value?.received_bytes;
    const expected = value?.expected_bytes;
    transfer.hidden = !Number.isFinite(received);
    if (transfer.hidden) return;
    const bar = el('current-file');
    if (expected > 0) {
      bar.max = expected;
      bar.value = Math.min(received, expected);
    } else {
      bar.removeAttribute('value');
    }
    const total = expected > 0 ? ' / ' + bytes(expected) : '';
    const speed = value.speed_bps > 0 ? ' · ' + bytes(value.speed_bps) + '/秒' : '';
    el('current-bytes').textContent = bytes(received) + total + speed;
  }

  function renderCoverage(state) {
    const items = Array.isArray(state.coverage) ? state.coverage : [];
    const rows = items.map(item => {
      const row = document.createElement('tr');
      const state = item.retry_at > Date.now() / 1000 ? '等待自动重试' : labels[item.state] || '状态待核';
      const kind = kindLabels[item.kind] || item.kind || '—';
      [range(item) || '—', kind + (item.version ? ' ' + item.version + '.0' : ''), state,
        number(item.ready_files) + ' / ' + number(item.total_files), number(item.failed_files),
        number(item.archive_missing), number(item.invalid_rows)].forEach(value => row.append(text('td', value)));
      return row;
    });
    if (!rows.length) {
      const row = document.createElement('tr');
      const cell = text('td', state.state === 'planning' ? '正在整理官方清单…' : '所选范围暂无归档记录。');
      cell.colSpan = 7;
      row.append(cell);
      rows.push(row);
    }
    el('coverage').replaceChildren(...rows);
    const hasTotal = Number.isFinite(state.coverage_total);
    el('page-label').textContent = items.length
      ? (offset + 1) + '–' + (offset + items.length) + (hasTotal ? ' / ' + number(state.coverage_total) : '')
      : '';
  }

  function renderFailures(state) {
    const failures = Array.isArray(state.failures) ? state.failures : [];
    el('failure-box').hidden = !failures.length;
    el('failures').replaceChildren(...failures.map(item => text('li', [
      range(item), kindLabels[item.kind], item.error || '待重试'
    ].filter(Boolean).join(' · '))));
  }

  function renderRequests(state) {
    const requests = Array.isArray(state.requests) ? state.requests : [];
    el('request-box').hidden = !requests.length;
    el('requests').replaceChildren(...requests.map(item => text('li', [
      range(item), labels[item.state] || '状态待核', item.mentions && '含 Mentions', item.error
    ].filter(Boolean).join(' · '))));
  }

  function controls() {
    const state = latest?.state;
    const unfinished = ['running', 'planning', 'queued', 'pending', 'pausing', 'waiting'].includes(state);
    const paused = ['paused', 'disk_low'].includes(state);
    el('start').disabled = busy || !latest || unfinished || paused;
    el('pause').disabled = busy || !unfinished || state === 'pausing';
    el('resume').disabled = busy || !paused;
    const delayed = ['catalog', 'group'].includes(latest?.retry_scope);
    el('retry').disabled = busy || !(delayed || latest?.progress?.errors > 0 || ['failed','error','partial'].includes(state));
    el('previous').disabled = busy || offset === 0;
    el('next').disabled = busy || !latest || (Number.isFinite(latest.coverage_total)
      ? offset + pageSize >= latest.coverage_total : (latest.coverage || []).length < pageSize);
  }

  function render(data) {
    latest = data;
    if (!loaded && data.date_from && data.date_to) {
      form.elements.date_from.value = data.date_from;
      form.elements.date_to.value = data.date_to;
      form.elements.mentions.checked = Boolean(data.mentions);
    }
    loaded = true;
    el('state').textContent = data.state === 'ready' ? '已完成' : labels[data.state] || '状态待核';
    const task = el('task-range');
    task.hidden = !data.date_from;
    task.textContent = range(data) + (data.mentions ? ' · 包含全部英文 Mentions' : ' · Mentions 按需');
    showCurrent(data.current);
    const p = data.progress || {};
    meter('downloaded', p.downloaded_files, p.total_files, p.downloaded_bytes, p.total_bytes);
    meter('ready', p.ready_files, p.total_files, p.ready_bytes, p.total_bytes);
    el('errors').textContent = number(p.errors);
    el('through').textContent = data.through || '—';
    el('disk').textContent = bytes(data.disk?.free_bytes) + ' / ' + bytes(data.disk?.total_bytes);
    renderCoverage(data);
    renderRequests(data);
    renderFailures(data);
    controls();
    if (data.error || data.message) {
      const stamp = Number.isFinite(data.retry_at)
        ? ' 预计继续时间 ' + new Date(data.retry_at * 1000).toISOString().slice(0, 19).replace('T', ' ') + ' UTC。'
        : '';
      message((data.error || data.message) + stamp, Boolean(data.error && !stamp), true);
    } else if (messageFromState) {
      message('');
    }
  }

  async function request(path, method, signal, payload) {
    const response = await fetch(path, {
      method, signal, cache:'no-store',
      headers:payload ? {'Content-Type':'application/json'} : {},
      body:payload ? JSON.stringify(payload) : undefined
    });
    const json = await response.json().catch(() => null);
    if (response.ok && json) return json;
    const detail = json?.detail;
    throw new Error(typeof detail === 'string' ? detail :
      Array.isArray(detail) ? detail.map(item => item.msg).filter(Boolean).join('；') : '暂时无法读取历史归档。');
  }

  function schedule(delay = 3500) {
    clearTimeout(timer);
    if (!document.hidden) timer = setTimeout(refresh, delay);
  }

  async function refresh() {
    if (busy || poll || document.hidden) return;
    const controller = new AbortController();
    poll = controller;
    const deadline = setTimeout(() => controller.abort(), 12000);
    try {
      const data = await request(API + '?limit=' + pageSize + '&offset=' + offset, 'GET', controller.signal);
      if (poll === controller) render(data);
    } catch (error) {
      if (poll === controller) message(error.name === 'AbortError' ? '读取归档进度超时。' : error.message, true);
    } finally {
      clearTimeout(deadline);
      if (poll === controller) {
        poll = null;
        schedule();
      }
    }
  }

  function stopRefresh() {
    clearTimeout(timer);
    poll?.abort();
    poll = null;
  }

  async function change(action, payload) {
    if (busy) return;
    stopRefresh();
    busy = true;
    controls();
    message('正在提交…');
    try {
      await request(API + '/' + action, 'POST', AbortSignal.timeout(15000), payload);
      if (action === 'start') offset = 0;
      message('已提交。');
    } catch (error) {
      message(error.name === 'TimeoutError' ? '提交超时，请查看任务状态。' : error.message, true);
    } finally {
      busy = false;
      controls();
      refresh();
    }
  }

  form.addEventListener('submit', event => {
    event.preventDefault();
    const values = new FormData(form);
    const date_from = values.get('date_from'), date_to = values.get('date_to');
    if (!date_from || !date_to || date_from > date_to) {
      message('请选择有效的起止日期。', true);
      return;
    }
    change('start', {date_from, date_to, mentions:values.has('mentions')});
  });
  for (const action of ['pause', 'resume', 'retry']) el(action).addEventListener('click', () => change(action));
  for (const [action, increment] of [['next', pageSize], ['previous', -pageSize]]) {
    el(action).addEventListener('click', () => {
      offset = Math.max(0, offset + increment);
      stopRefresh();
      refresh();
    });
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopRefresh();
    else refresh();
  });
  refresh();
})();

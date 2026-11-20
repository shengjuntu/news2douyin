(() => {
  const form = document.getElementById('event-window-form');
  if (!form) return;
  const panel = document.getElementById('event-market-panel');
  const list = document.getElementById('event-window-series');
  const link = document.getElementById('event-window-source');
  const status = document.getElementById('event-window-status');
  const results = document.getElementById('event-window-results');
  const select = form.elements.anchor;
  const submit = form.querySelector('button');
  const number = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 3, signDisplay: 'exceptZero'});
  const units = {percent: '%', points: ' 点', bps: ' bp'};
  const adjustments = {yahoo_adjusted_close: 'Yahoo 复权收盘', tushare_adj_factor: 'Tushare 复权因子',
    qfq: '前复权', hfq: '后复权', unadjusted: '未复权', provider_quote: '来源报价', unknown: '复权口径待确认'};
  const timezones = new Map();
  const states = {
    available: '有数据', pending: '窗口尚未结束', missing_price: '行情缺失',
    missing_adjustment: '复权数据缺失', missing_baseline: '基准收盘价缺失',
    unknown_calendar: '交易日历缺失', unknown_cutoff: '行情收盘时点未核实',
    unsupported_price_change: '无法计算该价格变化',
    mixed_adjustment_vintage: '需在 Trade View 重采完整窗口以统一复权版本',
    incomparable_sessions: '基准交易时段不一致',
  };

  panel.addEventListener('toggle', async () => {
    if (!panel.open || list.children.length) return;
    try {
      const reply = await fetch('/api/v1/interop/overseas-series');
      if (!reply.ok) return;
      const payload = await reply.json();
      for (const item of payload.data.items) {
        if (!item.series_id) continue;
        if (item.timezone) timezones.set(item.series_id, item.timezone);
        const option = document.createElement('option');
        option.value = item.series_id;
        option.label = item.label || item.ticker || item.series_id;
        list.append(option);
      }
    } catch (_) {
      // A manually entered code remains available if catalogue discovery fails.
    }
  });
  select.addEventListener('change', () => { link.href = select.selectedOptions[0].dataset.url; });

  function showRows(items, series, benchmark) {
    const table = document.createElement('table');
    const header = table.createTHead().insertRow();
    const columns = ['交易日窗口', '日期', series.series_id || '指标', '状态'];
    if (benchmark) columns.splice(3, 0, benchmark.series_id || '基准', '相对基准');
    for (const label of columns) {
      const cell = document.createElement('th');
      cell.scope = 'col';
      cell.textContent = label;
      header.append(cell);
    }
    const change = (value, unit) => value == null ? '—' : number.format(value) + unit;
    const body = table.createTBody();
    for (const item of items) {
      const row = body.insertRow();
      let notes = states[item.status] || item.status || '—';
      if (benchmark && item.benchmark_status && item.benchmark_status !== 'available') {
        notes += ' · 基准：' + (states[item.benchmark_status] || item.benchmark_status);
      }
      const cells = ['第 ' + item.offset + ' 个交易日', item.date || '—',
        change(item.value, units[series.change_unit] || ''), notes];
      if (benchmark) cells.splice(3, 0,
        change(item.benchmark_value, units[benchmark.change_unit] || ''),
        change(item.relative_value, ' 个百分点'));
      for (const value of cells) row.insertCell().textContent = value;
    }
    results.append(table);
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const anchor = select.selectedOptions[0].dataset;
    const seriesId = form.elements.series_id.value.trim().toUpperCase();
    const params = new URLSearchParams({series_id: seriesId, anchor_at: anchor.at, precision: anchor.precision});
    if (anchor.precision === 'timestamp' && form.elements.coarse.checked) {
      const zone = timezones.get(seriesId) || (/^[0-9]{6}/.test(seriesId) ? 'Asia/Shanghai' : null);
      if (!zone) {
        status.textContent = '还未取得该指标的交易时区。请重新打开此区块后再试。';
        return;
      }
      const parts = new Intl.DateTimeFormat('en', {timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit'})
        .formatToParts(new Date(anchor.at));
      const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
      params.set('anchor_at', [values.year, values.month, values.day].join('-'));
      params.set('precision', 'date');
    }
    const benchmark = form.elements.benchmark_id.value.trim().toUpperCase();
    if (benchmark) params.set('benchmark_id', benchmark);
    submit.disabled = true;
    results.replaceChildren();
    status.textContent = '正在计算交易日窗口…';
    try {
      const reply = await fetch('/api/v1/interop/event-window?' + params);
      const payload = await reply.json();
      if (!reply.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '请核对标的和时间');
      const {series, benchmark, windows, baseline} = payload.data;
      status.textContent = [
        params.get('precision') === 'date' ? '仅按交易日历粗对齐' : '以原文发布时间为锚点',
        baseline?.date ? '基点 ' + baseline.date : '',
        series.timezone || '', adjustments[series.adjustment] || series.adjustment,
        series.series_id === 'WTI' ? '近月报价，跨合约口径待核' : '',
      ].filter(Boolean).join(' · ');
      showRows(windows, series, benchmark);
    } catch (error) {
      status.textContent = '无法计算事件窗口。' + error.message;
    } finally {
      submit.disabled = false;
    }
  });
})();

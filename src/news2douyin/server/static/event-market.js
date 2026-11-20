(() => {
  const form = document.getElementById('event-market-form');
  if (!form) return;
  const status = document.getElementById('event-market-status');
  const results = document.getElementById('event-market-results');
  const submit = form.querySelector('button');
  const price = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 4});
  const adjustmentLabels = {unadjusted: '不复权', none: '不复权', qfq: '前复权', hfq: '后复权'};

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const params = new URLSearchParams(new FormData(form));
    if (params.get('start') > params.get('end')) {
      status.textContent = '起始日不能晚于结束日。';
      return;
    }
    submit.disabled = true;
    results.replaceChildren();
    status.textContent = '正在读取行情…';
    try {
      const response = await fetch('/api/v1/interop/market-series?' + params);
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(typeof payload.detail === 'string' ? payload.detail : '请核对日期范围和证券代码');
      }
      const {symbol, items} = payload.data;
      if (!items.length) {
        status.textContent = symbol + ' · 所选日期内没有已存行情。';
        return;
      }
      const uniform = (key) => {
        const values = new Set(items.map((item) => item[key] ?? null));
        return values.size === 1 ? values.values().next().value : null;
      };
      const priceUnit = uniform('price_unit');
      const volumeUnit = uniform('volume_unit');
      const adjustment = uniform('adjustment');
      status.textContent = [symbol, items.length + ' 个交易日', priceUnit,
        adjustmentLabels[adjustment] ?? adjustment].filter(Boolean).join(' · ');
      const columns = [
        ['date', '交易日'], ['open', '开盘', 'price_unit'], ['high', '最高', 'price_unit'],
        ['low', '最低', 'price_unit'], ['close', '收盘', 'price_unit'],
        ['volume', volumeUnit === 'shares' ? '成交量（股）' : volumeUnit ? `成交量（${volumeUnit}）` : '成交量', 'volume_unit'],
      ];
      if (!adjustment && items.some((bar) => bar.adjustment)) columns.push(['adjustment', '复权']);

      const table = document.createElement('table');
      const header = table.createTHead().insertRow();
      for (const [, label] of columns) {
        const cell = document.createElement('th');
        cell.scope = 'col';
        cell.textContent = label;
        header.append(cell);
      }
      const body = table.createTBody();
      for (const bar of items) {
        const row = body.insertRow();
        for (const [key, , unitKey] of columns) {
          const value = bar[key];
          const cell = row.insertCell();
          if (key === 'date') cell.textContent = value;
          else if (key === 'adjustment') cell.textContent = adjustmentLabels[value] ?? value ?? '—';
          else {
            const unit = unitKey === 'price_unit' ? priceUnit : volumeUnit;
            const suffix = !unit && bar[unitKey] ? ' ' + bar[unitKey] : '';
            cell.textContent = value == null ? '—' : price.format(value) + suffix;
          }
        }
      }
      results.append(table);
    } catch (error) {
      status.textContent = '无法读取行情。' + error.message;
    } finally {
      submit.disabled = false;
    }
  });
})();

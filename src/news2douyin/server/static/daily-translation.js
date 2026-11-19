/* Only a click can enqueue translation. Evidence remains in the original card. */
(() => {
  'use strict';
  const pending = new Set(['queued', 'pending', 'running', 'waiting', 'cancel_requested']);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function request(url, payload) {
    const res = await fetch(url, payload === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = typeof data.detail === 'string' ? data.detail : '翻译服务暂不可用，请稍后重试。';
      throw Object.assign(new Error(detail), {httpStatus:res.status});
    }
    return data;
  }
  for (const host of document.querySelectorAll('[data-daily-translation]')) {
    const button = host.querySelector('[data-translate-start]');
    if (!button) continue;
    const status = host.querySelector('[data-translate-status]');
    const result = host.querySelector('[data-translate-result]');
    const settings = host.querySelector('[data-translate-settings]');
    let complete = false, retry = false;
    button.addEventListener('click', async () => {
      if (complete) {
        result.hidden = !result.hidden;
        button.textContent = result.hidden ? '查看中文译文' : '收起译文';
        button.setAttribute('aria-expanded', String(!result.hidden));
        return;
      }
      if (button.disabled) return;
      button.disabled = true;
      settings.hidden = true;
      status.textContent = '正在请求翻译…';
      try {
        const payload = {key:host.dataset.key, fingerprint:host.dataset.fingerprint, retry};
        let data = await request('/api/radar/daily/'+encodeURIComponent(host.dataset.day)+'/translate', payload);
        const deadline = Date.now() + 120000;
        while (pending.has(data.status)) {
          if (!host.isConnected) return;
          if (Date.now() >= deadline) {
            status.textContent = '翻译仍在后台处理，稍后可再查看。';
            button.textContent = '查看翻译进度';
            return;
          }
          status.textContent = data.status === 'running' ? '正在翻译…' : '正在等待翻译任务…';
          await sleep(1200);
          data = await request('/api/translations/'+encodeURIComponent(data.id));
        }
        if (data.status === 'already_chinese') {
          status.textContent = '原文为中文';
          button.hidden = true;
          return;
        }
        if (data.status !== 'succeeded' || !data.translated_text) {
          settings.hidden = false;
          throw new Error(data.error || '翻译未完成，可重试。');
        }
        host.querySelector('[data-translate-text]').textContent = data.translated_text;
        host.querySelector('[data-translate-label]').textContent = ({
          title:'标题译文', content:'摘录译文', document:'标题与摘录译文'
        })[data.scope] || '中文译文';
        host.querySelector('[data-translate-note]').textContent =
          (data.scope === 'title' ? '此卡没有另存摘录。' : '') +
          (data.truncated ? '仅翻译了已保存文字的前一部分。' : '');
        result.hidden = false;
        complete = true;
        status.textContent = '机器译文';
        button.textContent = '收起译文';
        button.setAttribute('aria-expanded','true');
      } catch (err) {
        retry = true;
        status.textContent = err instanceof TypeError ? '无法连接翻译服务，请稍后重试。' : (err.message || '翻译失败，请重试。');
        if (err.httpStatus === 503) settings.hidden = false;
        button.textContent = '重试翻译';
      } finally { button.disabled = false; }
    });
  }
})();

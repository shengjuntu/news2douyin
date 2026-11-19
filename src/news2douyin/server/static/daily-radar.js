(() => {
  const edition = document.querySelector('[data-daily-edition]');
  if (!edition) return;
  const day = edition.dataset.dailyEdition;
  const url = '/api/radar/daily?' + new URLSearchParams({day});
  const button = edition.querySelector('[data-daily-request]');
  const message = edition.querySelector('[data-daily-status]');
  const revision = Number(edition.dataset.revision);
  let tracking = edition.dataset.poll === '1';
  let timer, inFlight = false;

  function schedule(delay = 4000) {
    clearTimeout(timer);
    if (tracking && !document.hidden) timer = setTimeout(poll, delay);
  }
  function handleState(data) {
    const busy = data.requested || data.build_status === 'pending' || data.build_status === 'building';
    if (Number(data.revision) !== revision || data.status !== edition.dataset.issueStatus && !busy ||
        data.build_status === 'failed' && edition.dataset.buildStatus !== 'failed') {
      tracking = false;
      window.location.reload();
      return;
    }
    tracking = busy;
    if (button) button.disabled = busy;
    message.textContent = busy ?
      (data.build_status === 'building' ? '正在生成，完成后将自动显示。' : '已排队，完成后将自动显示。') :
      (data.error || '已取得最新生成状态。');
    if (busy) schedule();
    else if (!button && edition.dataset.poll === '1') window.location.reload();
  }
  async function poll() {
    if (!tracking || document.hidden || inFlight) return;
    inFlight = true;
    try {
      const response = await fetch(url + '&summary=true', {cache: 'no-store'});
      if (!response.ok) throw new Error();
      handleState(await response.json());
    } catch {
      message.textContent = '暂时无法读取生成进度，稍后自动重试。';
      schedule(9000);
    } finally {
      inFlight = false;
    }
  }
  button?.addEventListener('click', async () => {
    button.disabled = true;
    message.textContent = '正在提交生成请求…';
    try {
      const response = await fetch(url, {method: 'POST', headers: {Accept: 'application/json'}});
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '暂时无法提交生成请求。');
      handleState(data);
    } catch (error) {
      button.disabled = false;
      message.textContent = error.message || '提交失败，请重试。';
    }
  });
  document.addEventListener('visibilitychange', () => schedule(100));
  schedule(2000);

  function focusAt(element) {
    element.scrollIntoView({block: 'start'});
    element.focus({preventScroll: true});
  }
  edition.querySelector('[data-group-jump]')?.addEventListener('change', event => {
    const target = document.getElementById(event.target.value);
    if (target) focusAt(target);
    event.target.selectedIndex = 0;
  });

  const resume = edition.querySelector('[data-reading-resume]');
  if (!resume) return;
  const note = edition.querySelector('[data-reading-note]');
  const key = 'news2douyin.daily.read.' + day;
  const entries = new Map();
  let saved = '', persistent = true;
  try {saved = localStorage.getItem(key) || '';} catch {persistent = false;}
  for (const card of edition.querySelectorAll('[data-daily-progress]')) {
    const marker = card.querySelector('[data-reading-ordinal]');
    const id = card.dataset.dailyProgress;
    if (!marker || !id) continue;
    entries.set(id, marker);
    marker.addEventListener('click', () => {
      saved = saved === id ? '' : id;
      try {
        if (saved) localStorage.setItem(key, saved);
        else localStorage.removeItem(key);
      } catch {persistent = false;}
      renderMark();
    });
  }
  function renderMark() {
    const target = entries.get(saved);
    for (const [id, marker] of entries) marker.setAttribute('aria-pressed', String(id === saved));
    resume.hidden = !target;
    if (target) {
      resume.textContent = '回到上次读到的第 ' + target.dataset.readingOrdinal + ' 条';
      note.textContent = persistent ? '' : '当前浏览器仅能在本页记住位置。';
    } else {
      note.textContent = saved ? '上次标记已不在本期。' : '';
    }
  }
  resume.addEventListener('click', () => {
    const marker = entries.get(saved);
    if (marker) focusAt(marker);
  });
  renderMark();
})();

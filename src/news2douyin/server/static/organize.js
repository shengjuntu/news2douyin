(() => {
  const form = document.getElementById('organize-event');
  if (!form) return;
  const title = document.getElementById('event-title');
  const titleField = document.getElementById('event-title-field');
  const fields = document.getElementById('existing-event-fields');
  const target = document.getElementById('existing-event');
  const query = document.getElementById('event-query');
  const find = document.getElementById('find-events');
  const status = document.getElementById('organize-status');
  const save = document.getElementById('save-event');
  const attaches = () => form.elements.destination.value === 'existing';
  let searched = false;

  async function request(path, options) {
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) {
      const fallback = response.status === 409 ? '材料或事件已更新，请刷新后核对。' : '请求未完成，请重试。';
      throw new Error(typeof data.detail === 'string' ? data.detail : fallback);
    }
    return data;
  }

  async function loadTargets() {
    find.disabled = true;
    target.replaceChildren(new Option('请选择', ''));
    status.textContent = '正在查询事件…';
    try {
      const items = await request('/api/candidates/organize/targets?' + new URLSearchParams({q: query.value.trim()}));
      for (const item of items) {
        const date = String(item.last_seen_at || '').slice(0, 10);
        const option = new Option(date ? item.title + ' · ' + date : item.title, item.event_key);
        option.dataset.version = String(item.version);
        target.append(option);
      }
      searched = true;
      status.textContent = items.length ? '' : '未找到可加入的事件。';
    } catch (error) {
      status.textContent = error.message;
    } finally {
      find.disabled = false;
    }
  }

  function updateMode() {
    const attached = attaches();
    titleField.hidden = attached;
    fields.hidden = !attached;
    target.required = attached;
    save.textContent = attached ? '加入已有事件' : '整理为新事件';
    if (attached && !searched && !find.disabled) loadTargets();
  }

  for (const option of form.elements.destination) option.addEventListener('change', updateMode);
  find.addEventListener('click', loadTargets);
  query.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      if (!find.disabled) loadTargets();
    }
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (save.disabled) return;
    const selected = target.selectedOptions[0];
    const data = {
      candidate_key: form.dataset.candidateKey,
      expected_token: form.dataset.token,
      request_id: form.dataset.requestId,
      title: attaches() ? '' : title.value.trim(),
      note: document.getElementById('event-note').value.trim(),
    };
    if (attaches()) {
      if (!selected?.value) return;
      data.event_key = selected.value;
      data.expected_version = Number(selected.dataset.version);
    }
    save.disabled = true;
    status.textContent = '正在整理…';
    try {
      const result = await request('/api/candidates/organize', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data),
      });
      const destination = new URL(result.url, window.location.origin);
      if (destination.origin !== window.location.origin || !/^\/events\/[^/]+$/.test(destination.pathname) || destination.search || destination.hash) {
        throw new Error('已提交，请从已整理事件查看。');
      }
      window.location.assign(destination.pathname);
    } catch (error) {
      status.textContent = error.message;
      save.disabled = false;
    }
  });
  updateMode();
  save.disabled = false;
})();

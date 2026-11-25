/* The event workspace supplies view, api, sourceLink, safeUrl and its HTML escaper. */
(() => {
  const panel = document.getElementById('event-products');
  const form = document.getElementById('product-review');
  const picker = form.elements.source;
  const status = document.getElementById('product-feedback');
  const list = document.getElementById('product-links');
  const path = base + '/product-evidence';
  let sources = [];
  let loadedVersion = null;
  let sequence = 0;
  let saving = false;

  function preview() {
    const source = sources.find(row => row.article_key === picker.value);
    const quote = document.getElementById('product-source-preview');
    quote.textContent = source?.excerpt || '';
    quote.hidden = !quote.textContent;
    document.getElementById('product-source-links').innerHTML = source ?
      `<a href="${esc(sourceLink(source))}" target="_blank" rel="noopener">查看归档版本 r${esc(source.revision)}</a>
       <a href="${esc(safeUrl(source.url))}" target="_blank" rel="noopener noreferrer">打开原文 ↗</a>` : '';
  }

  function setSources() {
    const previous = sources.find(row => row.article_key === picker.value);
    sources = view.sources.filter(row => row.can_verify_product && safeUrl(row.url));
    picker.replaceChildren(new Option('选择已归档的报道', ''));
    for (const row of sources) picker.add(new Option(row.title || row.article_key, row.article_key));
    if (previous && sources.some(row => row.article_key === previous.article_key && row.content_hash === previous.content_hash)) {
      picker.value = previous.article_key;
    } else {
      form.elements.excerpt.value = '';
    }
    preview();
    form.hidden = !!view.merged_into || !sources.length;
    document.getElementById('product-no-sources').hidden = !!view.merged_into || !!sources.length;
  }
  picker.addEventListener('change', () => { form.elements.excerpt.value = ''; preview(); });

  function show(card) {
    const market = panel.dataset.marketUrl;
    list.innerHTML = card.items.map(row => {
      const product = encodeURIComponent(row.product_id);
      const marketLink = market ? `<a href="${esc(market)}/console?product_id=${product}#commodities">查看商品行情</a>` : '';
      const original = safeUrl(row.source_url);
      return `<article class="evidence"><div class="desk-row"><strong>${esc(row.product_id)}</strong>
        <a href="/commodities/${product}/events">关联事件</a>${marketLink}</div>
        <blockquote class="evidence-quote">${esc(row.excerpt)}</blockquote><div class="desk-row">
        <a href="${esc(sourceLink(row))}">${esc(row.source_title || row.article_key)} · r${esc(row.revision)}</a>
        ${original ? `<a href="${esc(original)}" target="_blank" rel="noopener noreferrer">打开原文 ↗</a>` : ''}
        ${card.merged_into ? '' : `<button type="button" class="ghost" data-revoke="${esc(row.id)}">撤销关联</button>`}</div><p class="desk-meta">报道发布 ${esc(row.published_at || '未知')} · 版本收录 ${esc(row.observed_at || '未知')} · 人工核对 ${esc(row.verified_at)}</p></article>`;
    }).join('') || '<p class="muted">尚无已核对的商品关联。</p>';
    loadedVersion = card.version;
    setSources();
  }

  async function load() {
    if (!panel.open || loadedVersion === view.version || saving) return;
    const ticket = ++sequence;
    status.textContent = '正在读取商品关联…';
    try {
      const card = await api(path);
      if (ticket !== sequence) return;
      show(card);
      status.textContent = card.version === view.version ? '' : '事件已有更新，请刷新页面后再编辑。';
    } catch (error) {
      if (ticket === sequence) status.textContent = error.message;
    }
  }

  async function save(action, method, payload) {
    if (saving) return;
    if (view.version !== loadedVersion) {
      status.textContent = '事件已有更新，请刷新页面后再编辑。';
      return;
    }
    saving = true;
    ++sequence;
    form.querySelector('button').disabled = true;
    status.textContent = '正在保存…';
    try {
      const card = await api(action, method, {...payload, expected_version: view.version});
      show(card);
      if (method === 'POST') form.reset();
      updated(await api(base + '/workspace'));
      setSources();
      status.textContent = method === 'POST' ? '已核对并关联。' : '已撤销关联。';
    } catch (error) {
      status.textContent = error.message;
    } finally {
      saving = false;
      form.querySelector('button').disabled = false;
    }
  }

  form.addEventListener('submit', e => {
    e.preventDefault();
    const row = sources.find(source => source.article_key === picker.value);
    if (row) save(path, 'POST', {product_id: form.elements.product_id.value.trim(), article_key: row.article_key,
      revision: row.revision, content_hash: row.content_hash, excerpt: form.elements.excerpt.value.trim()});
  });
  list.addEventListener('click', e => {
    const button = e.target.closest('[data-revoke]');
    if (button && confirm('撤销这条商品证据关联？')) save(path + '/' + button.dataset.revoke, 'DELETE', {});
  });
  panel.addEventListener('toggle', load);
  panel.addEventListener('workspacechange', load);
  load();
})();

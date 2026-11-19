/* Expand local evidence on demand. Reads never queue a build or acquire a news page. */
window.mountGdeltEvidence = function (host, eventId, ui) {
  'use strict';
  const {node, link, request, sourceUrl, sourceArchive, time} = ui;
  const path = '/api/gdelt/events/' + encodeURIComponent(eventId);
  function action(label, callback) {
    const button = node('button', label, 'ghost');
    button.type = 'button';
    button.addEventListener('click', callback);
    return button;
  }
  function disclosure(parent, title, load, open = false) {
    const details = node('details', undefined, 'gdelt-evidence-disclosure');
    const summary = node('summary', title);
    const body = node('div');
    parent.append(details);
    details.append(summary, body);
    let started = false;
    function read() {
      if (!details.open || started) return;
      started = true;
      body.replaceChildren(node('p', '正在读取已保存的材料…', 'muted small'));
      load(body).catch(error => {
        body.replaceChildren(node('p', error.message, 'desk-error'), action('重试读取', () => { started = false; read(); }));
      });
    }
    details.addEventListener('toggle', read);
    if (open) { details.open = true; read(); }
    return details;
  }
  function sourceRow(parent, label, rawUrl) {
    const url = sourceUrl(rawUrl);
    if (url) parent.append(link(label, url));
  }
  function sentence(item) {
    const p = node('p', item.excerpt, 'gdelt-reconstruction-excerpt');
    p.lang = item.language || '';
    return p;
  }

  async function showNgrams(body) {
    const result = await request(path + '/ngrams');
    if (!body.isConnected) return;
    body.replaceChildren();
    const states = {none:'尚未查询关联报道片段。', pending:'已排队，后台将检查关联报道片段。', running:'后台正在拼接片段。', ready:'已有命中行动词与参与方的重建句子。', document_only:'已找到关联报道的片段。', unavailable:'本次未找到可用片段。', failed:'本次片段重建失败。'};
    const note = node('p', result.note || states[result.state] || '', 'gdelt-detail-note');
    note.setAttribute('role', 'status');
    body.append(note);
    if (!result.enabled && result.disabled_reason && result.disabled_reason !== result.note) {
      body.append(node('p', result.disabled_reason, 'gdelt-detail-note'));
    }
    if (result.enabled && result.budget) {
      const b=result.budget;
      body.append(node('small','UTC '+b.utc_day+' · 自动选取 '+b.automatic_used+'/'+b.automatic_limit+
        ' · 批次扫描 '+b.scans_used+'/'+b.scans_limit+(b.cache_only?' · 只读本地缓存':'')));
    }
    for (const item of result.items || []) {
      if (!item.excerpt) continue;
      const row = node('article', undefined, 'gdelt-source-item');
      row.append(node('strong', item.state === 'ready' ? '片段重建 · 行动线索待核' : '片段重建 · 文档上下文'), sentence(item));
      const provenance = ['GDELT Web News NGrams 3.0', item.language, item.observed_at ? '本地记录 ' + time(item.observed_at) : ''].filter(Boolean);
      row.append(node('small', provenance.join(' · ')));
      sourceRow(row, '打开出版方网页 ↗', item.url);
      for (const archive of item.archives || []) {
        sourceRow(row, '片段来源批次 ' + archive.stamp + ' ↗', archive.url);
      }
      body.append(row);
    }
    if (!result.enabled) return;
    const active = result.state === 'pending' || result.state === 'running';
    if (active) {
      body.append(action('刷新重建进度', () => showNgrams(body).catch(error => {note.textContent = error.message;})));
    } else if (result.state !== 'ready' && result.state !== 'document_only') {
      const retry = result.state === 'failed' || result.state === 'unavailable';
      const button = action(retry ? '重试片段重建' : '按关联报道查找片段', async () => {
        button.disabled = true;
        try {
          const reply = await request(path + '/ngrams', {retry});
          await showNgrams(body);
          if (reply.note && !reply.queued && !reply.existing) body.prepend(node('p', reply.note, 'muted small'));
        } catch (error) {
          note.textContent = error.message;
        } finally { button.disabled = false; }
      });
      body.append(button);
    }
  }

  async function showGraph(body) {
    const result = await request(path + '/graph');
    if (!body.isConnected) return;
    body.replaceChildren();
    const states = {not_requested:'尚未加入来源图。', queued:'已排队。', pending:'等待后台写入来源图。', ready:'来源图已写入。'};
    const note = node('p', result.configured ? states[result.state] || result.state : '尚未配置 FalkorDB。可继续查看上方报道和原始记录。', 'gdelt-detail-note');
    note.setAttribute('role','status');
    body.append(note);
    if (!result.configured) return;
    const button = action(result.state === 'ready' ? '更新来源图' : result.state === 'pending' || result.state === 'queued' ? '刷新写入进度' : '加入全局来源图', async () => {
      button.disabled = true;
      try {
        if (!['pending','queued'].includes(result.state)) await request(path + '/graph', {});
        await showGraph(body);
      } catch (error) { note.textContent = error.message; }
      finally { button.disabled = false; }
    });
    body.append(button);
    if (result.state === 'ready') {
      body.append(link('打开图数据库浏览器 ↗','/knowledge/browser'), node('p', '图名 ' + result.graph_name + ' · 节点 ' + result.node_key, 'small'));
      if (/^\d{1,30}$/.test(eventId)) {
        body.append(node('code', "MATCH (e:SourceEvent {key:'gdelt/" + eventId + "'})-[r]->(n) RETURN e,r,n LIMIT 40"));
      }
    }
  }

  const queryDay = new URL(location.href).searchParams.get('evidence_day') || '';
  const encodedDay = /^\d{4}-\d{2}-\d{2}$/.test(queryDay) ? '?day=' + encodeURIComponent(queryDay) : '';
  disclosure(host, '报道增长与编码来源', async body => {
    const result = await request(path + '/evidence' + encodedDay);
    if (!body.isConnected) return;
    const counts = result.coverage;
    body.replaceChildren(node('strong', result.day + ' · 北京时间'));
    const roles = [
      result.actor1?.name ? '行动方 ' + result.actor1.name : '',
      result.cameo_label || result.cameo_code ? '行为 ' + [result.cameo_label, result.cameo_code].filter(Boolean).join(' ') : '',
      result.actor2?.name ? '对象 ' + result.actor2.name : '',
      result.action_place?.name ? '地点 ' + result.action_place.name : '',
    ];
    body.append(node('p', ['官方编码 #' + result.event_id, ...roles.filter(Boolean)].join(' · '),'small'));
    const record = sourceArchive(result.source_ref);
    if (record) body.append(link('查看这条编码的原档 ↗',record));
    body.append(node('p', '当日新增报道 ' + (counts.new_documents ?? '未知') + ' 篇 · 首次出现的媒体域名 ' + (counts.new_domains ?? '未知') + ' 个。'));
    body.append(node('p', '截至 ' + time(result.cutoff) + '，已关联 ' + (counts.documents ?? '未知') + ' 篇报道、' + (counts.domains ?? '未知') + ' 个域名。' +
      (counts.history_complete ? '' : ' 当前索引的历史记录存在缺口。'), 'gdelt-detail-note'));
    body.append(node('p', '按此编码关联文档的首次提及时间统计。新增转载也会增加报道数，实际行动进展需对照报道。', 'gdelt-detail-note'));
    if (result.documents?.length) {
      disclosure(body, '核对关联文档与批次', async list => {
        list.replaceChildren();
        for (const doc of result.documents) {
          const item = node('div', undefined, 'gdelt-source-item');
          const url = sourceUrl(doc.url);
          item.append(url ? link(doc.domain || url, url) : node('span',doc.domain || doc.document || '来源标识未知'));
          item.append(node('small','首次提及 ' + time(doc.first_seen) + ' · 最近提及 ' + time(doc.last_seen)));
          const ref = sourceArchive(doc.first_ref);
          if (ref) item.append(link('查看首条提及原档 ↗',ref));
          const gkg = sourceArchive(doc.gkg);
          if (gkg) item.append(link('关联 GKG 文档记录 ↗',gkg));
          list.append(item);
        }
        if (result.documents_limited) list.append(node('p','仅显示部分关联文档。','muted small'));
      });
    }
  }, true);
  disclosure(host, '片段重建与出版方来源', showNgrams);
  disclosure(host, '全局活动图中的来源关系', showGraph);
};

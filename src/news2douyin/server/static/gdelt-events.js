/* Content-first discovery. Source enrichment, translation and selection are explicit actions. */
(() => {
  'use strict';
  const form = document.getElementById('gdelt-filter');
  if (!form) return;
  const byId = id => document.getElementById(id);
  const node = (tag,text,className) => {const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(className)item.className=className;return item;};
  const parseData = id => {try{return JSON.parse(byId(id)?.textContent||'{}');}catch{return {};}};
  const page = parseData('gdelt-page-data');
  const events = new Map((page.items||[]).map(item=>[String(item.event_id),item]));
  let presets = parseData('gdelt-preset-data');if(!Array.isArray(presets))presets=[];
  const message = (value,failed=false) => {const target=byId('gdelt-message');target.textContent=value;target.className=failed?'desk-error':'desk-meta';};
  const time = value => {if(!value)return '未知';const date=new Date(value);return Number.isNaN(date.valueOf())?String(value):new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',dateStyle:'short',timeStyle:'short'}).format(date)+' 北京时间';};
  const batchTime = value => /^\d{14}$/.test(value||'')?time(value.slice(0,4)+'-'+value.slice(4,6)+'-'+value.slice(6,8)+'T'+value.slice(8,10)+':'+value.slice(10,12)+':'+value.slice(12,14)+'Z'):time(value);
  const errorText = detail => typeof detail==='string'?detail:Array.isArray(detail)?detail.map(row=>row.msg).join('；'):'请求未完成，请稍后重试。';
  async function request(url,body) {
    const response=await fetch(url,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
    let data;try{data=await response.json();}catch{throw Error('服务器暂时未返回可读取的结果，请稍后重试。');}
    if(!response.ok)throw Error(errorText(data.detail));
    return data;
  }
  function sourceUrl(raw) {
    try {const url=new URL(raw);return ['http:','https:'].includes(url.protocol)&&!url.username&&!url.password?url.href:'';}catch{return '';}
  }
  function link(label,href) {const item=node('a',label);item.href=href;item.target='_blank';item.rel='noopener noreferrer';return item;}
  const reportFor = event => event?.report_preview && typeof event.report_preview==='object' && event.report_preview.title ? event.report_preview : null;
  const displayTitle = event => reportFor(event)?.title || event?.display_title || '报道内容待补充';
  const contentNote = event => ({pending:'报道已排队，稍后更新。',running:'正在后台读取来源报道…',failed:'来源暂不可读取，可重试补充或打开来源网页。',unavailable:'未取得可用报道内容，保留已有线索。',missing_source:'这条编码暂无可读取的报道地址。'})[event?.content_job_status||event?.content_status] || (event?.content_status==='title_only'?'已取得标题，摘要待补充。':'补充后显示来源报道标题和摘要。');
  function section(host,title) {const box=node('section',undefined,'gdelt-detail-section');box.append(node('h3',title));host.append(box);return box;}
  const sourceArchive = value => typeof value?.feed==='string'&&/^\d{14}$/.test(value.stamp||'')&&Number.isInteger(value.row_index)&&value.row_index>0?'/api/intake/records/'+encodeURIComponent(value.feed)+'/'+value.stamp+'/'+value.row_index:'';
  const detailDialog=byId('gdelt-detail-dialog'),detailHost=byId('gdelt-detail-content');
  let currentEvent='',detailGeneration=0,returnFocus=null,graph=null,shapesPromise=null,translationTimer=null,detailReportFingerprint='';
  function stopTranslationPolling(){if(translationTimer!==null){clearTimeout(translationTimer);translationTimer=null;}}
  function closeDetail(){detailDialog.close();}
  byId('gdelt-detail-close').addEventListener('click',closeDetail);
  detailDialog.addEventListener('close',()=>{
    detailGeneration++;currentEvent='';detailReportFingerprint='';stopTranslationPolling();if(graph){graph.destroy();graph=null;}
    const url=new URL(location.href);url.searchParams.delete('event');url.searchParams.delete('evidence_day');history.replaceState(null,'',url);
    if(returnFocus?.isConnected)returnFocus.focus();returnFocus=null;
  });
  detailDialog.addEventListener('click',event=>{if(event.target===detailDialog){const bounds=detailDialog.getBoundingClientRect();if(event.clientX<bounds.left||event.clientX>bounds.right)closeDetail();}});
  function useKeyword(word){if(!word)return;form.elements.namedItem('q').value=String(word).slice(0,200);form.elements.namedItem('preset').value='';closeDetail();form.requestSubmit();}
  function drawRelation(host,event,generation){
    const box=section(host,'官方编码的行为关系');
    box.append(node('p','主体 → 行为 → 对象，来自本条GDELT自动编码；尚未经过原文核查。','gdelt-detail-note'));
    const names=[event.actor1_name,event.actor2_name].filter(Boolean);
    if(!names.length){box.append(node('p','这条记录没有明确主体或对象。','muted'));return;}
    const entities=node('div',undefined,'row gdelt-graph-entities');
    for(const name of [...new Set(names)]){const button=node('button','筛选 '+name);button.type='button';button.addEventListener('click',()=>useKeyword(name));entities.append(button);}box.append(entities);
    if(!event.actor1_name||!event.actor2_name){box.append(node('p','缺少一方角色，不推测关系对象。','muted small'));return;}
    if(typeof globalThis.cytoscape!=='function'){box.append(node('p',event.actor1_name+' → '+(event.cameo_label||event.event_code||'行为待分类')+' → '+event.actor2_name));return;}
    const canvas=node('div',undefined,'gdelt-graph');canvas.setAttribute('role','img');canvas.setAttribute('aria-label',event.actor1_name+'对'+event.actor2_name+'的官方编码行为关系');box.append(canvas);
    const controls=node('div',undefined,'gdelt-graph-controls');box.append(controls);
    requestAnimationFrame(()=>{
      if(generation!==detailGeneration||!detailDialog.open)return;
      try {
        graph=globalThis.cytoscape({container:canvas,elements:[{data:{id:'actor1',label:event.actor1_name}},{data:{id:'actor2',label:event.actor2_name}},{data:{id:'action',source:'actor1',target:'actor2',label:event.cameo_label||event.event_code||'待分类'}}],
          layout:{name:'grid',rows:1,padding:40},minZoom:.25,maxZoom:3,wheelSensitivity:.18,
          style:[{selector:'node',style:{label:'data(label)','background-color':'#2f9b9d',color:'#effaff','font-size':13,'text-wrap':'wrap','text-max-width':145,'text-valign':'bottom','text-margin-y':10,width:40,height:40}},{selector:'edge',style:{label:'data(label)','curve-style':'bezier','target-arrow-shape':'triangle','line-color':'#8aa8bc','target-arrow-color':'#8aa8bc',width:2,color:'#b8d8e6','font-size':12,'text-background-color':'#0f1d28','text-background-opacity':1,'text-background-padding':'4px'}}]});
        graph.on('tap','node',e=>useKeyword(e.target.data('label')));
        for(const [label,action] of [['放大',()=>graph.zoom({level:Math.min(3,graph.zoom()*1.25),renderedPosition:{x:canvas.clientWidth/2,y:canvas.clientHeight/2}})],['缩小',()=>graph.zoom({level:Math.max(.25,graph.zoom()/1.25),renderedPosition:{x:canvas.clientWidth/2,y:canvas.clientHeight/2}})],['复位',()=>graph.fit(undefined,40)]]){const b=node('button',label,'ghost');b.type='button';b.addEventListener('click',action);controls.append(b);}
      }catch{canvas.replaceChildren(node('p','关系图暂不可用，可使用上方实体筛选。','muted'));}
    });
  }
  async function drawPlace(host,event,generation){
    const box=section(host,'发生地点');
    box.append(node('p',[page.countries?.[event.action_country]||event.action_country,event.action_name].filter(Boolean).join(' · ')||'官方记录未提供地点','desk-meta'));
    if(event.action_country){
      const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 604 260');svg.setAttribute('class','gdelt-map');svg.setAttribute('role','img');svg.setAttribute('aria-label','官方行动地点国家或地区');box.append(svg);
      try{
        if(!shapesPromise)shapesPromise=request('/api/desk/map-shapes').catch(error=>{shapesPromise=null;throw error;});
        const shapes=await shapesPromise;if(generation!==detailGeneration)return;
        for(const [code,,d] of shapes){const p=document.createElementNS('http://www.w3.org/2000/svg','path');p.setAttribute('d',d);p.setAttribute('fill',code===event.action_country?'#33bda8':'#344756');svg.append(p);}
        box.append(node('p','高亮官方行动地点所属国家或地区，并非事件影响范围。底图 Natural Earth · @d3-maps/atlas。','gdelt-detail-note'));
      }catch{svg.remove();box.append(node('p','底图暂不可用，文字地点仍可核对。','gdelt-detail-note'));}
    }
    if(typeof event.latitude==='number'&&typeof event.longitude==='number'&&Number.isFinite(event.latitude)&&Number.isFinite(event.longitude)&&Math.abs(event.latitude)<=90&&Math.abs(event.longitude)<=180){
      box.append(node('p','官方坐标 '+event.latitude.toFixed(4)+', '+event.longitude.toFixed(4),'small'));
      box.append(link('在地图中查看坐标 ↗','https://www.openstreetmap.org/?mlat='+event.latitude+'&mlon='+event.longitude+'#map=7/'+event.latitude+'/'+event.longitude));
    }else box.append(node('p','未提供可定位的有效坐标。','gdelt-detail-note'));
  }
  function translateSection(host,event,generation){
    const report=reportFor(event),target=report?.translation;
    const box=section(host,'中文译读');
    if(!report||!target?.kind||!target?.key){box.append(node('p','请先补充可读报道，再翻译标题和摘要。','gdelt-detail-note'));return;}
    box.append(node('p',report.language_info?.display||event.source_language?.display||'报道语言未知','desk-meta'));
    const button=node('button','中文译读','ghost');button.type='button';button.id='gdelt-translate';box.append(button);
    const status=node('p','点击后调用已配置的翻译模型。','gdelt-translation-status');status.id='gdelt-translation-status';status.setAttribute('role','status');box.append(status);
    const output=node('div',undefined,'gdelt-translation-output');output.id='gdelt-translation-output';output.hidden=true;box.append(output);
    let translationId='',failed=false;
    async function display(data){
      if(generation!==detailGeneration)return;
      translationId=data.id||translationId;
      if(data.status==='succeeded'){
        status.textContent='中文辅助译读 · 请对照来源报道'+(data.truncated?' · 仅翻译了已存文字的前一部分':'');output.textContent=data.translated_text||'模型未返回译文。';output.hidden=false;button.textContent='重新查看译文';button.disabled=false;failed=false;
      }else if(['failed','cancelled'].includes(data.status)){
        status.textContent=data.error||'翻译未完成，可以重试。';button.textContent='重试译读';button.disabled=false;failed=true;
      }else if(translationId){
        status.textContent=data.status==='running'?'模型正在翻译报道标题和摘要…':'译读已排队，可稍后重新查看。';button.disabled=true;
        translationTimer=setTimeout(async()=>{try{await display(await request('/api/translations/'+encodeURIComponent(translationId)));}catch(error){if(generation===detailGeneration){status.textContent=error.message;button.disabled=false;button.textContent='重试译读';failed=true;}}},1200);
      }else{status.textContent='翻译任务未返回编号，请重试。';button.disabled=false;failed=true;}
    }
    button.addEventListener('click',async()=>{
      button.disabled=true;status.textContent='正在提交报道译读…';stopTranslationPolling();
      try{await display(await request('/api/translations',{kind:target.kind,key:String(target.key),scope:target.scope||'document',...(target.revision?{revision:target.revision}:{}),target:'zh-CN',retry:failed}));}
      catch(error){if(generation===detailGeneration){status.textContent=error.message;button.disabled=false;button.textContent='重试译读';failed=true;}}
    });
  }
  function reportSection(host,event){
    const report=reportFor(event),box=section(host,'来源报道');
    if(report){
      box.append(node('h3',report.title,'gdelt-report-heading'));
      box.append(node('p',report.excerpt||'已取得标题，尚无可用摘要。','gdelt-report-full-excerpt'));
      const line=node('p',report.domain||'来源报道','gdelt-report-source');
      const href=sourceUrl(report.final_url||report.source_url);if(href){line.append(document.createTextNode(' · '),link('查看来源报道 ↗',href));}box.append(line);
      box.append(node('p','这是与该编码关联的报道摘录，文章可能涉及多个事件；需对照原文判断具体内容。','gdelt-detail-note'));
    }else if(!event.reconstruction){
      box.append(node('p','报道内容待补充','gdelt-report-heading'),node('p',contentNote(event),'gdelt-detail-note'));
      const href=sourceUrl(event.source_url);if(href)box.append(link('先查看官方记录的来源网页 ↗',href));
    }
    if(event.reconstruction) renderReconstructed(box,event.reconstruction);
    const enrich=node('button',report?'补充报道摘要':'补充报道','ghost');enrich.type='button';enrich.dataset.gdeltEnrich=String(event.event_id);enrich.addEventListener('click',()=>enrichEvents([String(event.event_id)],enrich));box.append(enrich);
    return box;
  }
  function renderDetail(data,generation){
    const event={...(events.get(currentEvent)||{}),...(data.event||{})};
    byId('gdelt-detail-title').textContent=displayTitle(event);
    events.set(String(event.event_id),event);
    detailReportFingerprint=(reportFor(event)?.content_hash||'')+':'+(event.content_status||'');
    detailHost.replaceChildren();
    reportSection(detailHost,event);
    window.mountGdeltEvidence(detailHost, String(event.event_id), {node, link, request, sourceUrl, sourceArchive, time});
    translateSection(detailHost,event,generation);
    const heading=section(detailHost,'事件时间与选题');heading.append(node('div',event.event_date||'发生日期未知','gdelt-detail-date'));
    heading.append(node('p','发生日期精度为日，不补造具体时刻。','gdelt-detail-note'));
    const list=node('ul',undefined,'gdelt-detail-time-list');
    for(const [label,value] of [['官方首次观测',event.first_seen],['最近官方观测',event.last_seen]])list.append(node('li',label+' · '+time(value)));heading.append(list);
    const actions=node('div',undefined,'row');
    if(event.selected_task){const a=node('a','继续这项选题 →','action-link');a.href='/workbench/'+encodeURIComponent(event.selected_task)+'/selection';actions.append(a);}
    const choose=node('button',event.selected_task?'另起一个角度':'形成选题',event.selected_task?'ghost':'');choose.type='button';choose.addEventListener('click',()=>openSelection(event.event_id,reportFor(event)?.title||event.reconstruction?.excerpt||''));actions.append(choose);const organize=node('a','整理事件');organize.href='/organize?candidate='+encodeURIComponent('gdelt:'+event.event_id);actions.append(organize);heading.append(actions);
    const sources=section(detailHost,'更多报道与采集依据');
    sources.append(node('p','本站已观测报道 '+(event.observed_documents??'未知')+' · 媒体域名 '+(event.observed_sources??'未知')+'。数量不代表独立核实。','gdelt-detail-note'));
    const mentions=Array.isArray(data.mentions)?data.mentions:[];
    for(const mention of mentions){
      const item=node('div',undefined,'gdelt-source-item'),href=sourceUrl(mention.doc_identifier),label=mention.domain||mention.doc_identifier||'来源标识待核对';
      const preview=mention.preview&&typeof mention.preview==='object'?mention.preview:null;
      item.append(href?link(preview?.title||label+' ↗',href):node('span',preview?.title||label));
      if(preview?.excerpt)item.append(node('p',preview.excerpt,'gdelt-source-excerpt'));
      if(preview?.title)item.append(node('small',label));
      item.append(node('small','最近提及 '+time(mention.last_seen)+(typeof mention.confidence==='number'?' · 抽取置信度 '+mention.confidence:'')));
      const archive=sourceArchive(mention);if(archive)item.append(link('查看这条提及的原档依据 ↗',archive));sources.append(item);
    }
    if(!mentions.length)sources.append(node('p','尚无本地已处理的 Mentions 报道关联；不代表没有报道。','muted'));
    if(data.mentions_limited)sources.append(node('p','仅展示前30条本地报道关联，其他原始记录保存在接收库。','gdelt-detail-note'));
    const first=sourceUrl(event.source_url);if(first)sources.append(link('官方事件记录的首次来源 ↗',first));
    const archive=sourceArchive(event.source_ref);if(archive){const p=node('p');p.append(link('查看事件记录原档 ↗',archive));sources.append(p);}
    drawRelation(detailHost,event,generation);drawPlace(detailHost,event,generation);
    const coding=section(detailHost,'编码与口径');coding.append(node('p','GDELT #'+event.event_id+' · 行为编码 '+(event.event_code||'未知'),'small'));
    if(event.initial_mentions!==null&&event.initial_mentions!==undefined)coding.append(node('p','官方首次批次提及 '+event.initial_mentions+'；不是持续累计报道数。','gdelt-detail-note'));
    if(event.goldstein!==null&&event.goldstein!==undefined)coding.append(node('p','Goldstein 类型参考分 '+event.goldstein+'，表示行为类型，不能衡量本次事件规模或视频价值。','gdelt-detail-note'));
  }
  async function openDetail(id,focus){
    if(!/^\d{1,30}$/.test(String(id)))return;
    stopTranslationPolling();if(graph){graph.destroy();graph=null;}
    currentEvent=String(id);const generation=++detailGeneration;returnFocus=focus||returnFocus;
    byId('gdelt-detail-title').textContent=displayTitle(events.get(currentEvent));detailHost.replaceChildren(node('p','正在读取本地来源记录…','muted'));
    if(!detailDialog.open)detailDialog.showModal();
    const url=new URL(location.href);url.searchParams.set('event',currentEvent);history.replaceState(null,'',url);
    try{const data=await request('/api/gdelt/events/'+encodeURIComponent(currentEvent));if(generation===detailGeneration)renderDetail(data,generation);}
    catch(error){if(generation===detailGeneration){detailHost.replaceChildren(node('p',error.message,'desk-error'));const retry=node('button','重试读取','ghost');retry.type='button';retry.addEventListener('click',()=>openDetail(id,focus));detailHost.append(retry);}}
  }
  document.querySelector('.gdelt-event-list')?.addEventListener('click',event=>{
    const summary=event.target.closest('[data-gdelt-sources] summary');
    if(summary){event.preventDefault();openDetail(summary.closest('[data-gdelt-sources]').dataset.gdeltSources,summary);return;}
    const enrich=event.target.closest('[data-gdelt-enrich]');if(enrich){enrichEvents([enrich.dataset.gdeltEnrich],enrich);return;}
    const translate=event.target.closest('[data-gdelt-translate-card]');if(translate){openDetail(translate.dataset.gdeltTranslateCard,translate).then(()=>byId('gdelt-translate')?.click());return;}
    const select=event.target.closest('[data-gdelt-select]');if(select){openSelection(select.dataset.gdeltSelect,reportFor(events.get(select.dataset.gdeltSelect))?.title||select.dataset.gdeltTitle);}
  });
  const dialog=byId('gdelt-select-dialog'),topicForm=byId('gdelt-select-form');
  let eventId='',requestId='';
  function openSelection(id,title){
    eventId=String(id);requestId=globalThis.crypto?.randomUUID?.()||('select-'+Date.now()+'-'+Math.random().toString(36).slice(2));
    byId('gdelt-topic-title').value=String(title||'').slice(0,240);byId('gdelt-topic-title').placeholder=title?'':'先补充报道，或填写要核查的问题';byId('gdelt-topic-angle').value='';byId('gdelt-select-message').textContent='';
    dialog.showModal();byId('gdelt-topic-title').focus();
  }
  byId('gdelt-select-close').addEventListener('click',()=>dialog.close());
  topicForm.addEventListener('submit',async event=>{
    event.preventDefault();const button=byId('gdelt-select-submit');button.disabled=true;byId('gdelt-select-message').textContent='正在建立选题…';
    try{
      const data=await request('/api/gdelt/events/'+encodeURIComponent(eventId)+'/select',{request_id:requestId,title:byId('gdelt-topic-title').value,angle:byId('gdelt-topic-angle').value});
      if(typeof data.url!=='string'||!/^\/workbench\/[A-Za-z0-9_-]+(?:\/selection)?$/.test(data.url))throw Error('任务已返回，请到内容工作台核对。');
      location.assign(data.url);
    }catch(error){byId('gdelt-select-message').textContent=error.message;}finally{button.disabled=false;}
  });
  let enrichmentGeneration=0,enrichmentTimer=null,pageClosed=false;
  function renderReconstructed(host, material) {
    if (!material?.excerpt) return;
    const text=node('p',material.excerpt,'gdelt-reconstruction-excerpt');
    text.lang=material.language||'';
    const credit=node('p','GDELT Web News NGrams 3.0 · 片段重建 · 待核','muted small');
    const url=sourceUrl(material.url);
    if(url) credit.append(document.createTextNode(' · '),link('核对出版方网页 ↗',url));
    host.append(text,credit);
  }
  function updateCard(event){
    const card=document.querySelector('[data-gdelt-event="'+String(event.event_id)+'"]');if(!card)return;
    const report=reportFor(event),ready=Boolean(report);
    card.classList.toggle('gdelt-has-report',ready);card.classList.toggle('gdelt-needs-report',!ready);
    card.querySelector('[data-gdelt-report-title]').textContent=displayTitle(event);
    const reconstructed=event.reconstruction;
    const recovered=card.querySelector('[data-gdelt-reconstructed]');
    recovered.replaceChildren(); recovered.hidden=!reconstructed;
    if(reconstructed) renderReconstructed(recovered,reconstructed);
    const excerpt=card.querySelector('[data-gdelt-report-excerpt]');excerpt.textContent=report?.excerpt||'';excerpt.hidden=!excerpt.textContent;
    card.querySelector('[data-gdelt-report-language]').textContent=report?.language_info?.display||event.source_language?.display||'报道语言未知';
    const badge=card.querySelector('[data-gdelt-content-badge]');badge.textContent=ready?(report.excerpt?'相关报道':'仅标题'):reconstructed?'片段重建 · 待核':'报道待补充';badge.className='badge '+(ready?'ok':'warn');
    const source=card.querySelector('[data-gdelt-report-source]');source.replaceChildren();
    if(ready){source.append(document.createTextNode(report.domain||'来源报道'));const href=sourceUrl(report.final_url||report.source_url);if(href)source.append(document.createTextNode(' · '),link('查看报道 ↗',href));}
    else{const href=sourceUrl(event.source_url);if(href){source.append(document.createTextNode(new URL(href).hostname+' · '),link('查看来源网页 ↗',href));}else source.textContent='暂无来源地址';}
    const select=card.querySelector('[data-gdelt-select]');if(select){select.dataset.gdeltTitle=report?.title||reconstructed?.excerpt||'';if(!event.selected_task){select.textContent=ready?'形成选题':reconstructed?'核查重建线索':'保留线索';select.classList.toggle('ghost',!ready);}}
    let translate=card.querySelector('[data-gdelt-translate-card]');
    if(ready&&!translate){translate=node('button','中文译读','ghost');translate.type='button';translate.dataset.gdeltTranslateCard=String(event.event_id);card.querySelector('.gdelt-event-actions').insertBefore(translate,card.querySelector('[data-gdelt-sources]'));}
    if(!ready&&translate)translate.remove();
    const state=card.querySelector('[data-gdelt-content-status]');state.textContent=['pending','running','failed','unavailable','missing_source'].includes(event.content_job_status||event.content_status)?contentNote(event):'';
  }
  async function refreshEnriched(ids,generation,round=0){
    if(pageClosed||generation!==enrichmentGeneration)return;
    let unfinished=false;
    try {
      const data=await request('/api/gdelt/content/cards?event_ids='+encodeURIComponent(ids.join(',')));
      if(pageClosed||generation!==enrichmentGeneration)return;
      for(const item of Array.isArray(data.items)?data.items:[]){
        const key=String(item.event_id),event={...(events.get(key)||{}),...item};events.set(key,event);updateCard(event);
        if(['pending','running'].includes(event.content_job_status||event.content_status))unfinished=true;
      }
      const visible=events.get(currentEvent),fingerprint=(reportFor(visible)?.content_hash||'')+':'+(visible?.content_status||'');
      if(currentEvent&&ids.includes(currentEvent)&&detailDialog.open&&fingerprint!==detailReportFingerprint){
        const detail=await request('/api/gdelt/events/'+encodeURIComponent(currentEvent));
        if(generation===enrichmentGeneration&&detailDialog.open){stopTranslationPolling();if(graph){graph.destroy();graph=null;}renderDetail(detail,++detailGeneration);}
      }
      if(unfinished&&round<5){message('报道在后台补充中，可继续浏览；本页会有限次刷新。');enrichmentTimer=setTimeout(()=>refreshEnriched(ids,generation,round+1),2000);}
      else{message(unfinished?'补充仍在后台进行，可稍后刷新查看。':'本次报道状态已更新。');finishEnrichment();}
    }catch(error){if(generation===enrichmentGeneration){message(error.message,true);finishEnrichment();}}
  }
  function finishEnrichment(){byId('gdelt-enrich-page').disabled=false;for(const button of document.querySelectorAll('[data-gdelt-enrich]'))button.disabled=false;}
  async function enrichEvents(ids,button){
    ids=[...new Set(ids.map(String).filter(id=>/^\d{1,30}$/.test(id)))].slice(0,30);if(!ids.length)return;
    const generation=++enrichmentGeneration;if(enrichmentTimer!==null)clearTimeout(enrichmentTimer);
    button.disabled=true;byId('gdelt-enrich-page').disabled=true;message('正在安排后台补充报道…');
    try{const data=await request('/api/gdelt/content/enrich',{event_ids:ids,retry:true});if(data.enabled===false||data.state==='disabled'){message(data.message||'后台报道补充尚未启用，已有报道仍可查看。');finishEnrichment();return;}await refreshEnriched(ids,generation);}
    catch(error){message(error.message,true);button.disabled=false;byId('gdelt-enrich-page').disabled=false;}
  }
  byId('gdelt-enrich-page').addEventListener('click',event=>{const missing=[...events.values()].filter(item=>!reportFor(item)?.excerpt).map(item=>String(item.event_id));enrichEvents((missing.length?missing:[...events.keys()]).slice(0,30),event.currentTarget);});
  window.addEventListener('pagehide',()=>{pageClosed=true;enrichmentGeneration++;if(enrichmentTimer!==null)clearTimeout(enrichmentTimer);stopTranslationPolling();});
  const presetSelect=form.elements.namedItem('preset');
  for(const control of form.querySelectorAll('input[name],select[name]'))if(!['preset','source','limit'].includes(control.name))control.addEventListener('change',()=>{presetSelect.value='';});
  presetSelect.addEventListener('change',()=>{
    const selected=presets.find(row=>row.key===presetSelect.value);if(!selected?.filters)return;
    for(const [name,value] of Object.entries(selected.filters)){const control=form.elements.namedItem(name);if(control&&name!=='preset')control.value=String(value??'');}
    form.requestSubmit();
  });
  byId('gdelt-save-filter').addEventListener('click',async()=>{
    const name=byId('gdelt-preset-name').value.trim();if(!name){message('请先填写筛选名称。');return;}
    const filters=Object.fromEntries(new FormData(form));for(const key of ['source','preset','limit'])delete filters[key];filters.min_sources=Number(filters.min_sources||0);
    const button=byId('gdelt-save-filter');button.disabled=true;
    try{const saved=await request('/api/gdelt/filters',{name,filters});presets=[saved,...presets.filter(row=>row.key!==saved.key)];const option=node('option',saved.name);option.value=saved.key;presetSelect.append(option);presetSelect.value=saved.key;message('筛选已保存，可直接切换。');}
    catch(error){message(error.message,true);}finally{button.disabled=false;}
  });
  function appliedQuery(){const query=new URLSearchParams();for(const [key,value] of Object.entries(page.filters||{}))if(value!==undefined&&value!==null)query.set(key,String(value));query.set('limit',String(page.limit||30));return query;}
  byId('gdelt-count').addEventListener('click',async()=>{
    const button=byId('gdelt-count'),target=byId('gdelt-total');button.disabled=true;target.textContent='正在统计当前筛选的已索引事件…';
    try{
      const data=await request('/api/gdelt/events/count?'+appliedQuery());
      if(data.status==='complete'&&Number.isInteger(data.total)&&Number.isInteger(data.pages)){
        target.textContent='共 '+data.total+' 条 · '+data.pages+' 页 · 每页 '+(page.limit||30)+' 条 · 统计于 '+time(data.computed_at);
        target.title=data.note||'按统计时已索引的匹配记录计算，采集推进后数量会变化。';button.textContent='刷新统计';
      }else{target.textContent=data.note||'统计暂未完成，仍可正常翻页。';button.textContent='重试统计';}
    }catch(error){target.textContent=error.message;button.textContent='重试统计';}finally{button.disabled=false;}
  });
  (async()=>{
    const host=byId('gdelt-overview');
    try{
      const data=await request('/api/gdelt/overview'),feeds=Array.isArray(data.feeds)?data.feeds:[];
      if(!feeds.length)return;
      const archived=feeds.map(row=>row.archived_at).filter(Boolean).sort().at(-1),indexed=feeds.map(row=>row.indexed_at).filter(Boolean).sort().at(-1),pending=feeds.reduce((sum,row)=>sum+(Number(row.pending_batches)||0),0);
      host.replaceChildren(node('span','最近完成归档 '+batchTime(archived)),node('span','最近完成索引 '+batchTime(indexed)),node('span',pending?'待索引 '+pending+' 批':'各流进度可查看'));
      const issues=feeds.filter(row=>row.last_error);if(issues.length)host.append(node('span',issues.length+' 条流有接收问题','desk-error'));
      const detail=node('details',undefined,'gdelt-feed-progress');detail.append(node('summary','展开六条流进度'));
      for(const feed of feeds){const text=(feed.label||feed.feed)+' · '+(feed.enabled===false?'已暂停':feed.enabled===null?'未初始化':'已启用')+' · 归档 '+batchTime(feed.archived_at)+' · 索引 '+batchTime(feed.indexed_at)+(feed.pending_batches?' · 待索引 '+feed.pending_batches+' 批':'')+(feed.last_error?' · 接收有问题':'');detail.append(node('p',text,'small'));}
      host.append(detail);
      const a=node('a','查看各流进度 →');a.href='/intake';host.append(a);host.title=data.note||'最近完成批次不等于所有历史数据已经齐全。';
    }catch{host.replaceChildren(node('span','采集进度暂不可读取。','muted'));const a=node('a','到采集中心检查');a.href='/intake';host.append(a);}
  })();
  const requestedEvent=new URL(location.href).searchParams.get('event');if(requestedEvent)openDetail(requestedEvent);
})();

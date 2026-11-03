(()=>{
 'use strict';
 const $=id=>document.getElementById(id), state={workspace:null,dirty:false,busy:false,loading:false,offset:0,total:0,epoch:0,serial:0,chatKey:'',candidateKey:'',taskKey:'',scopeEvent:''};
 const active=new Set(['queued','preparing','starting','running','waiting','stopping','uncertain']);
 const names={queued:'排队中',preparing:'准备环境',starting:'启动中',running:'讨论中',waiting:'等待处理',uncertain:'核对原请求中',stopping:'正在停止',paused:'已停止',completed:'已完成',partial:'部分完成',failed:'失败'};
 const node=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
 const uid=()=>crypto.randomUUID?crypto.randomUUID():Date.now().toString(16)+Math.random().toString(16).slice(2);
 const note=text=>$('topic-message').textContent=text;
 async function api(path,body,method){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),35000);
  try{const r=await fetch(path,{method:method||(body===undefined?'GET':'POST'),headers:{'Content-Type':'application/json'},signal:controller.signal,...(body===undefined?{}:{body:JSON.stringify(body)})});
   const d=await r.json();if(!r.ok){const e=Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));e.status=r.status;throw e;}return d;
  }finally{clearTimeout(timer);}
 }
 function stored(key){const raw=sessionStorage.getItem(key);return raw?JSON.parse(raw):null;}
 function put(key,value){sessionStorage.setItem(key,JSON.stringify(value));}
 const pendingKey=(kind,wid=state.workspace?.workspace_id)=>'topic-pending:'+kind+':'+(wid||'new');
 async function receipt(kind,body,send,wid){
  const key=pendingKey(kind,wid);let original=stored(key);if(!original){original=body;put(key,original);}
  try{const result=await send(original);sessionStorage.removeItem(key);return result;}
  catch(e){if([400,404,409,422].includes(e.status))sessionStorage.removeItem(key);throw e;}
 }
 function action(fn){return async()=>{if(state.busy)return;state.busy=true;state.serial++;controls();try{await fn();}catch(e){note(e.name==='AbortError'?'响应超时；创建/消息/任务请求已保留，再点击会确认原请求。':e.message);}finally{state.busy=false;controls();}};}
 function mark(){state.dirty=true;$('topic-dirty').textContent='有未保存的修改';}
 function scope(){return {query:$('topic-query').value.trim(),period:$('topic-period').value,date_from:$('topic-period').value==='custom'?$('topic-from').value:'',date_to:$('topic-period').value==='custom'?$('topic-to').value:'',time_field:$('topic-time').value,timezone:'Asia/Shanghai',event_key:state.scopeEvent};}
 function applyFields(w){
  $('topic-title').value=w.title;$('topic-angle').value=w.angle;
  $('topic-query').value=w.scope.query||'';$('topic-period').value=w.scope.period||'all';$('topic-from').value=w.scope.date_from||'';$('topic-to').value=w.scope.date_to||'';$('topic-time').value=w.scope.time_field||'published';state.scopeEvent=w.scope.event_key||'';
  state.dirty=false;$('topic-dirty').textContent='';
 }
 function controls(){
  const w=state.workspace,running=w?.messages?.some(m=>active.has(m.status));
  for(const id of ['topic-title','topic-angle','topic-query','topic-period','topic-from','topic-to','topic-time','topic-kind','topic-language','topic-duration','topic-images','topic-write','new-topic-brief'])$(id).disabled=state.busy;
  for(const id of ['topic-create','topic-list','topic-search','topic-save','topic-prev','topic-next','topic-short','topic-materials','topic-deep','topic-sync'])$(id).disabled=state.busy||(!w&&id!=='topic-create'&&id!=='topic-list');
  if(w){$('topic-prev').disabled=state.busy||state.offset===0;$('topic-next').disabled=state.busy||state.offset+20>=state.total;}
  let pending=null;try{pending=stored(pendingKey('message'));const creation=stored(pendingKey('create','new'));$('topic-create').textContent=creation?'确认上次选题创建':'新建选题 / 开始讨论';const task=stored(pendingKey('handoff'));for(const kind of ['short','materials','deep']){const button=$('topic-'+kind);button.disabled=state.busy||!w||!!task&&task.action!==kind;button.textContent=task?.action===kind?'确认上次任务请求':({short:'仅用所选资料写短稿',materials:'补充材料',deep:'深入研究'})[kind];}}catch(e){note('无法读取会话存储；请允许站点存储后重试。');}
  $('topic-send').textContent=pending?'确认上次消息':'发送';$('topic-send').disabled=state.busy||!w||(running&&!pending);$('topic-prompt').disabled=state.busy||!!pending;
  $('topic-stop').disabled=state.busy||!running;
 }
 async function showSource(id){
  try{const w=state.workspace;const d=await api('/api/topics/'+w.workspace_id+'/sources/'+encodeURIComponent(id));if(w!==state.workspace&&w.workspace_id!==state.workspace?.workspace_id)return;
   $('topic-source-title').textContent=d.title;$('topic-source-meta').textContent='报道日期 '+(d.published_at||'未知')+' · 快照 '+d.observed_at+' · '+(d.revision?'r'+d.revision:'内容快照');$('topic-source-content').textContent=d.content;$('topic-source-dialog').showModal();
  }catch(e){note(e.message);}
 }
 function citations(parent,ids){const row=node('div',undefined,'topic-citations');for(const [i,id] of ids.entries()){const b=node('button','来源 '+(i+1),'ghost');b.onclick=()=>showSource(id);row.append(b);}parent.append(row);}
 function draw(w){
  state.workspace=w;$('topic-workspace').hidden=false;
  const pending=stored(pendingKey('message',w.workspace_id));
  if(pending&&w.messages.some(m=>m.request_id===pending.request_id)){sessionStorage.removeItem(pendingKey('message',w.workspace_id));if($('topic-prompt').value===pending.text)$('topic-prompt').value='';}
  const pendingTask=stored(pendingKey('handoff',w.workspace_id));if(pendingTask&&w.tasks.some(t=>t.request_id===pendingTask.request_id))sessionStorage.removeItem(pendingKey('handoff',w.workspace_id));
  $('topic-selected-count').textContent=w.sources.length+'/12';const selected=$('topic-selected');selected.replaceChildren();
  if(!w.sources.length)selected.append(node('p','搜索后加入报道，或采用助手给出的候选选题。','muted'));
  for(const source of w.sources){const row=node('div',undefined,'topic-card'),title=node('button',source.title,'ghost');title.onclick=()=>showSource(source.source_id);row.append(title,node('p',(source.published_at||'发布时间未知')+' · 已固定原文','muted small'));const remove=node('button','移出','ghost');remove.onclick=action(async()=>{await save(w.source_ids.filter(i=>i!==source.source_id));});row.append(remove);selected.append(row);}
  const chatKey=JSON.stringify(w.messages);
  if(chatKey!==state.chatKey){state.chatKey=chatKey;const box=$('topic-chat');const previousTop=box.scrollTop,nearBottom=box.scrollHeight-box.scrollTop-box.clientHeight<80;box.replaceChildren();
   if(!w.messages.length)box.append(node('p','可以先说想了解的范围；无需先选新闻。','muted'));
   for(const m of w.messages){const user=node('article',undefined,'card topic-card topic-user');user.append(node('strong','你'),node('p',m.text,'topic-note'));box.append(user);
    if(m.reply){const reply=node('article',undefined,'card topic-card topic-assistant');reply.append(node('strong','选题助手'),node('p',m.reply.text,'topic-note'));citations(reply,m.reply.source_ids);box.append(reply);}
    if(m.error)box.append(node('p',m.error,'topic-message'));}
   box.scrollTop=nearBottom?box.scrollHeight:previousTop;
  }
  const latest=w.messages.at(-1);$('topic-state').textContent=latest?(names[latest.status]||latest.status)+' · 库内检索 '+latest.search_count+' 次 · 已读 '+latest.read_count+' 份原文'+(latest.session_id?' · 会话 '+latest.session_id:''):'尚未发起讨论';
  if(w.rundesk_url){const u=new URL(w.rundesk_url);if(['http:','https:'].includes(u.protocol))$('topic-rundesk').href=u.href;}
  const candidateKey=JSON.stringify(w.candidates);
  if(candidateKey!==state.candidateKey){state.candidateKey=candidateKey;const box=$('topic-candidates');box.replaceChildren();if(!w.candidates.length)box.append(node('p','助手整理的候选会显示在这里。','muted'));
   for(const c of w.candidates){const card=node('article',undefined,'topic-card');card.append(node('h3',c.title),node('p',c.angle),node('p',c.summary,'muted'));if(c.gaps?.length)card.append(node('p','待补：'+c.gaps.join('；'),'muted small'));citations(card,c.source_ids);const adopt=node('button','采用为当前选题');adopt.onclick=action(async()=>{await save();const current=state.workspace;const d=await api('/api/topics/'+current.workspace_id,{revision:current.revision,title:c.title,angle:c.angle,scope:scope(),source_ids:c.source_ids},'PUT');applyFields(d);draw(d);note('已采用候选，接下来可继续讨论、补材或研究。');await refreshList();});card.append(adopt);box.append(card);}
  }
  const taskKey=JSON.stringify(w.tasks);if(taskKey!==state.taskKey){state.taskKey=taskKey;const box=$('topic-tasks');box.replaceChildren();for(const t of w.tasks){const p=node('p'),a=node('a',({short:'短稿',materials:'材料补充',deep:'深入研究'})[t.action]+' · 查看任务');a.href=t.url;p.append(a);if(!t.started){const resume=node('button','继续提交原任务','ghost');resume.onclick=action(async()=>{await api('/api/topics/'+w.workspace_id+'/handoffs/'+t.handoff_id+'/resume',{});await refresh();});p.append(resume);}box.append(p);}}
  controls();
 }
 async function refreshList(){const rows=await api('/api/topics');const select=$('topic-list');select.replaceChildren(node('option','选择选题…'));select.firstChild.value='';for(const w of rows){const o=node('option',w.title);o.value=w.workspace_id;select.append(o);}if(state.workspace)select.value=state.workspace.workspace_id;return rows;}
 async function refresh(){if(!state.workspace)return;const gen=state.epoch,serial=state.serial,wid=state.workspace.workspace_id;const w=await api('/api/topics/'+wid);if(gen!==state.epoch||serial!==state.serial)return;draw(w);}
 async function open(wid){if(state.dirty){note('请先保存当前选题的修改，再切换。');$('topic-list').value=state.workspace.workspace_id;return;}state.epoch++;state.chatKey=state.candidateKey=state.taskKey='';const w=await api('/api/topics/'+encodeURIComponent(wid));applyFields(w);draw(w);state.offset=0;const url=new URL(location.href);url.searchParams.set('workspace',wid);history.replaceState(null,'',url.pathname+url.search);await search();}
 async function save(ids){const w=state.workspace;if(!state.dirty&&ids===undefined)return;const d=await api('/api/topics/'+w.workspace_id,{revision:w.revision,title:$('topic-title').value.trim()||'未命名选题',angle:$('topic-angle').value,scope:scope(),source_ids:ids||w.source_ids},'PUT');applyFields(d);draw(d);}
 async function search(offset=0){state.offset=offset;const w=state.workspace,gen=state.epoch;const s=scope();const data=await api('/api/topics/'+w.workspace_id+'/search?'+new URLSearchParams({kind:$('topic-kind').value,scope:JSON.stringify(s),offset:String(offset)}));if(gen!==state.epoch)return;state.total=data.total;const box=$('topic-results');box.replaceChildren();
  $('topic-search-scope').textContent=(s.period==='all'?'全部历史':s.period==='custom'?(s.date_from||'不限')+' 至 '+(s.date_to||'不限'):s.period==='today'?'今天':s.period==='last7'?'最近7天':'最近30天')+' · '+(s.event_key?'当前事件关联报道 · ':'')+'只搜索本地资料库';
  for(const item of data.items){const card=node('article',undefined,'topic-card');const title=node('a',item.title);title.href=item.url;title.target='_blank';title.rel='noopener';card.append(title,node('p',item.snippet||item.summary||'暂无摘要','muted'));if(item.article_key){card.append(node('p',item.published_at||'发布时间未知','muted small'));const add=node('button','加入选题','ghost');add.onclick=action(async()=>{await save();const current=state.workspace;const d=await api('/api/topics/'+current.workspace_id+'/sources',{revision:current.revision,article_key:item.article_key});draw(d);});card.append(add);}else{const view=node('button','查看关联报道','ghost');view.onclick=action(async()=>{state.scopeEvent=item.event_key;$('topic-kind').value='articles';$('topic-query').value='';mark();await search();});card.append(view);}box.append(card);}
  if(!data.items.length)box.append(node('p','当前范围没有匹配资料，可扩大日期或调整关键词。库内无结果不代表事件不存在。','muted'));$('topic-page').textContent='匹配 '+data.total+' 条 · '+(data.total?offset+1:0)+'–'+Math.min(offset+20,data.total);controls();
 }
 async function sendText(text,requestId){const w=state.workspace;const original=stored(pendingKey('message'));if(!original){await save();if(!text.trim())throw Error('请输入选题要求');}
  const current=state.workspace;await receipt('message',{revision:current.revision,request_id:requestId||uid(),text},body=>api('/api/topics/'+current.workspace_id+'/messages',body),current.workspace_id);$('topic-prompt').value='';await refresh();note('消息已保存，可关闭页面后继续查看。');}
 $('topic-create').onclick=action(async()=>{if(state.dirty)throw Error('请先保存当前选题修改');const key=pendingKey('create','new');let pending=stored(key);if(!pending){pending={request_id:uid(),title:'未命名选题',scope:{period:'all',timezone:'Asia/Shanghai'},first_text:$('new-topic-brief').value.trim()};put(key,pending);}
  const {first_text,...body}=pending;const w=await api('/api/topics',body);await open(w.workspace_id);if(first_text&&!state.workspace.messages.some(m=>m.request_id===pending.request_id+':first'))await sendText(first_text,pending.request_id+':first');sessionStorage.removeItem(key);$('new-topic-brief').value='';await refreshList();note(first_text?'已建立选题并提交讨论。':'选题已建立，可以搜索并加入资料。');});
 $('topic-list').onchange=action(async()=>{if($('topic-list').value)await open($('topic-list').value);});
 $('topic-save').onclick=action(async()=>{await save();await refreshList();note('选题已保存。');});$('topic-search').onclick=action(()=>search());$('topic-prev').onclick=action(()=>search(Math.max(0,state.offset-20)));$('topic-next').onclick=action(()=>search(state.offset+20));
 $('topic-send').onclick=action(()=>sendText($('topic-prompt').value));$('topic-stop').onclick=action(async()=>{await api('/api/topics/'+state.workspace.workspace_id+'/stop',{});await refresh();});$('topic-sync').onclick=action(async()=>{const w=await api('/api/topics/'+state.workspace.workspace_id+'/sync',{});draw(w);});
 for(const id of ['topic-title','topic-angle','topic-query','topic-period','topic-from','topic-to','topic-time'])$(id).addEventListener('input',mark);
 $('topic-query').addEventListener('input',()=>state.scopeEvent='');$('topic-source-close').onclick=()=>$('topic-source-dialog').close();
 async function handoff(action){const wid=state.workspace.workspace_id;if(!stored(pendingKey('handoff'))){await save();}
  const w=state.workspace;const result=await receipt('handoff',{revision:w.revision,request_id:uid(),action,output_language:$('topic-language').value,duration_sec:Number($('topic-duration').value),find_images:$('topic-images').checked,write_script:$('topic-write').checked},body=>api('/api/topics/'+wid+'/handoffs',body),wid);
  await refresh();note(result.pending?'任务已保存但尚未启动：'+result.error+'。请在任务列表继续提交原任务。':'任务已保存，点击任务链接查看进度。');const a=node('a','打开任务');a.href=result.url;$('topic-message').append(' ',a);}
 for(const kind of ['short','materials','deep'])$('topic-'+kind).onclick=action(()=>handoff(kind));
 let timer;async function poll(){try{if(!state.busy&&!state.loading&&!document.hidden){state.loading=true;await refresh();}}catch(e){note('暂时无法同步；已保存的请求和选题保留。');}finally{state.loading=false;timer=setTimeout(poll,3000);}}
 window.addEventListener('pagehide',()=>clearTimeout(timer));
 (async()=>{try{const rows=await refreshList(),wid=new URL(location.href).searchParams.get('workspace');if(wid)await open(wid);else if(rows.length)await open(rows[0].workspace_id);else controls();timer=setTimeout(poll,3000);}catch(e){note(e.message);}})();
})();

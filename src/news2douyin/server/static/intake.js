(()=>{'use strict';
const $=id=>document.getElementById(id),node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const labels={gkg_english:'GKG English',gkg_translation:'GKG Translingual',events_english:'Events English',events_translation:'Events Translingual',mentions_english:'Mentions English',mentions_translation:'Mentions Translingual'},cards=new Map(),pendingFailures=new Set();let offset=0,total=0,loading=false,timer,closed=false;
const numeric=n=>typeof n==='number'&&Number.isFinite(n),number=n=>numeric(n)?n.toLocaleString('zh-CN'):'未知';
const stamp=s=>s&&/^\d{14}$/.test(s)?s.slice(0,4)+'-'+s.slice(4,6)+'-'+s.slice(6,8)+' '+s.slice(8,10)+':'+s.slice(10,12)+' UTC':s||'尚未确定';
const clock=s=>{if(!s)return '未知';const d=new Date(numeric(s)?s*1000:s);return Number.isNaN(d.getTime())?String(s):d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+' 北京时间';};
const safe=raw=>{try{if(typeof raw!=='string'||/[\u0000-\u0020]/.test(raw))return '';const u=new URL(raw);return ['http:','https:'].includes(u.protocol)&&!u.username&&!u.password?u.href:'';}catch{return '';}};
const a=(text,url)=>{const n=node('a',text);n.href=url;return n;};
const button=(text,fn,cls='ghost')=>{const n=node('button',text,cls);n.type='button';n.onclick=fn;return n;};
const list=value=>Array.isArray(value)?value:[],object=value=>value&&typeof value==='object'&&!Array.isArray(value)?value:{};
const phases={manifest:'读取官方清单',download:'下载批次',archive:'验证与归档',select:'选择批次',pick:'选择批次',lake:'读取接收库',read:'读取接收库',prepare:'准备记录',lock:'等待事务锁',write:'写入索引',sql:'写入索引',lake_read:'读取接收库',sql_lock:'等待事务锁',sql_apply:'写入索引',commit:'提交事务',complete:'处理完成'};
const errorKinds={tls_error:'TLS连接失败',connection_error:'连接未建立',manifest_missing_feed:'清单未列出该流',archive_checksum:'归档校验失败',archive_invalid:'归档校验未通过',archive_io_error:'本地归档读写失败',http_redirect:'来源返回重定向',http_error:'HTTP请求失败',manifest_not_found:'官方清单返回404',batch_not_found_unverified:'请求批次返回404，原因待核实',rate_limited:'来源限流',ssl_error:'TLS连接失败',timeout:'请求超时',manifest_missing_kind:'清单未列出该类批次',network_error:'网络请求失败'};
function errorText(detail){return typeof detail==='string'?detail:Array.isArray(detail)?detail.map(x=>x.msg).join('；'):'请求未完成，请稍后重试。';}
async function request(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});let data;try{data=await r.json();}catch{throw Error('服务器未返回可读取的结果，请稍后重试。');}if(!r.ok)throw Error(errorText(data.detail));return data;}
function markUnavailable(){
 for(const c of cards.values()){c.state.textContent='状态未知';c.state.className='badge warn';c.progress.textContent='当前状态不可读取；请求详情保留上次取得的信息。';c.latest.textContent='';c.projected.textContent='索引进度未知';c.classes.textContent='成功 / 跳过计数未知';c.pause.disabled=c.retry.disabled=c.submit.disabled=c.probe.disabled=true;}
}
function counter(value,label){const n=node('div',undefined,'intake-counter');n.append(node('strong',number(value)),node('span',label));return n;}
function definition(host,label,value,url=false){const term=node('dt',label),item=node('dd');if(url&&safe(value)){const link=a(value,safe(value));link.target='_blank';link.rel='noopener noreferrer';item.append(link);}else item.textContent=value==null||value===''?'未知':String(value);host.append(term,item);}
function diagnostics(host,value){
 const d=object(value);host.replaceChildren();
 if(!Object.keys(d).length){host.append(node('p','暂无下载诊断。','muted small'));return;}
 const dl=node('dl',undefined,'intake-diagnostics');
 definition(dl,'请求状态',({running:'正在请求',succeeded:'上次请求成功',failed:'上次请求失败',waiting:'等待后续批次',interrupted:'上次请求中断，等待重试'})[d.status]||d.status||'未知');
 definition(dl,'请求阶段',phases[d.phase]||d.phase||'未知');definition(dl,'当前目标批次',stamp(d.target_stamp));definition(dl,'清单最新批次',stamp(d.latest_stamp||d.latest_manifest_stamp));
 definition(dl,'HTTP状态',numeric(d.http_status)?d.http_status:'未知 / 尚未取得响应');definition(dl,'请求时间',clock(d.attempted_at||d.requested_at));definition(dl,'完成时间',clock(d.completed_at));
 definition(dl,'请求地址',d.request_url||d.url,true);definition(dl,'官方清单',d.manifest_url,true);if(d.manifest_listed_url)definition(dl,'清单原始地址',d.manifest_listed_url,true);
 if(d.error_kind)definition(dl,'诊断类型',errorKinds[d.error_kind]||d.error_kind);if(d.message)definition(dl,'说明',d.message);
 if(d.expected_md5)definition(dl,'清单MD5',d.expected_md5);if(numeric(d.expected_bytes))definition(dl,'清单字节数',number(d.expected_bytes));
 if(d.archive_reused)definition(dl,'归档来源','复用已保存原档，未重新下载');host.append(dl);
 if(d.http_status===404)host.append(node('p','404：本次未取得文件，批次仍待核。','intake-note small'));
}
function showProbe(host,result){
 host.replaceChildren(node('p',result.message||'官方源检查完成。此操作不推进接收或投影游标。','intake-note'));
 for(const [key,label] of [['manifest','官方清单'],['target','当前目标'],['latest','清单最新批次']]){
  const data=object(result[key]);if(!Object.keys(data).length)continue;
  const section=node('section',undefined,'intake-probe-result');section.append(node('strong',label));const dl=node('dl',undefined,'intake-diagnostics');
  definition(dl,'状态',({succeeded:'可取得响应',failed:'未取得可用响应',not_requested:'本次未请求'})[data.status]||data.status||'未知');
  if(data.stamp)definition(dl,'批次',stamp(data.stamp));definition(dl,'HTTP状态',numeric(data.http_status)?data.http_status:'未知');definition(dl,'地址',data.request_url||data.url,true);
  if(data.error_kind)definition(dl,'诊断类型',errorKinds[data.error_kind]||data.error_kind);if(data.message)definition(dl,'说明',data.message);section.append(dl);host.append(section);
 }
}
function feedCard(feed){
 let c=cards.get(feed.key);if(c)return c;
 const host=node('article',undefined,'card intake-feed');host.dataset.intakeFeed=feed.key;
 const title=node('h3',labels[feed.key]||feed.key),state=node('span','等待状态','badge'),progress=node('p'),latest=node('p'),projected=node('p'),classes=node('p',undefined,'muted small'),error=node('p',undefined,'feed-error'),controls=node('div',undefined,'row'),pause=node('button','暂停接收','ghost'),retry=node('button','重试并继续','ghost'),form=node('form',undefined,'feed-start'),label=node('label','首批 UTC 时间（可空）'),start=node('input'),submit=node('button','设置开始并继续','ghost'),hint=node('small','仅首批可设置，留空取最新批次。'),message=node('p',undefined,'feed-control-message');
 progress.className=projected.className='feed-progress';
 const details=node('details',undefined,'intake-feed-details'),summary=node('summary','请求与处理详情'),diagnostic=node('div'),timing=node('div'),probeMessage=node('p',undefined,'muted small'),probeResult=node('div',undefined,'intake-probe-output');
 const probe=button('检查官方源',async()=>{
  if(c.probing)return;c.probing=true;probe.disabled=true;probeMessage.textContent='正在检查官方清单与文件响应…';
  try{const result=await request('/api/intake/feeds/'+encodeURIComponent(feed.key)+'/probe',{});c.probeResult=result;showProbe(probeResult,result);probeMessage.textContent='检查完成 · '+clock(result.completed_at);}
  catch(e){probeMessage.textContent=e.message;}finally{c.probing=false;probe.disabled=false;}
 });probe.dataset.intakeProbe=feed.key;details.append(summary,diagnostic,timing,probe,probeMessage,probeResult);
 start.type='datetime-local';start.step='900';start.setAttribute('aria-label',(labels[feed.key]||feed.key)+'开始批次 UTC');start.dataset.feedStart=feed.key;pause.type=retry.type='button';submit.type='submit';label.append(start);form.append(label,hint,submit);controls.append(pause,retry);host.append(title,state,progress,latest,projected,classes,error,controls,form,message,details);$('intake-feeds').append(host);
 const action=async(path,body)=>{if(c.busy)return;c.busy=true;pause.disabled=retry.disabled=submit.disabled=true;message.textContent='正在保存…';try{render(await request(path,body));message.textContent='已保存。';}catch(e){message.textContent=e.message;}finally{c.busy=false;pause.disabled=retry.disabled=false;submit.disabled=!!(c.feed.last_complete||c.feed.next_batch);c.form.hidden=submit.disabled;}};
 pause.onclick=()=>action('/api/intake/feeds/'+feed.key+'/enabled',{enabled:!c.feed.enabled});retry.onclick=()=>action('/api/intake/feeds/'+feed.key+'/retry',{});form.onsubmit=e=>{e.preventDefault();action('/api/intake/feeds/'+feed.key+'/enabled',{enabled:true,start_batch:start.value});};
 c={host,state,progress,latest,projected,classes,error,pause,retry,form,start,submit,message,details,diagnostic,timing,probe,probeMessage,probeResult,feed,busy:false,probing:false};cards.set(feed.key,c);return c;
}
function renderFailures(projection){
 const failures=list(projection.failures||projection.blocked_batches).filter(value=>value&&typeof value==='object'),host=$('intake-failures');host.replaceChildren();$('intake-failures-section').hidden=!failures.length;
 $('intake-failures-note').textContent=projection.failures_limited?'显示最近 20 个受阻批次。':'';
 for(const f of failures){
  const key=f.key||f.batch_key,card=node('article',undefined,'intake-failure');card.dataset.intakeFailure=key;const header=node('div',undefined,'row');header.append(node('strong',(labels[f.feed]||f.feed)+' · '+stamp(f.stamp)),node('span',f.retry_after>Date.now()/1000?'退避中':'等待重试','badge warn'));card.append(header);
  card.append(node('p','已处理至第 '+number(f.offset)+' 行'+(numeric(f.row_index)?' · 受阻记录第 '+number(f.row_index)+' 行':numeric(object(f.context).read_start)&&numeric(object(f.context).read_end)?' · 本次读取第 '+number(f.context.read_start)+'–'+number(f.context.read_end)+' 行，具体受阻位置待核':' · 受阻记录位置待核')+' · 失败 '+number(f.attempts)+' 次'),node('p',(phases[f.phase]||f.phase||'未知阶段')+'：'+(f.error||f.error_type||'未记录具体原因'),'feed-error'));
  if(f.retry_after>Date.now()/1000)card.append(node('p','下次自动重试：'+clock(f.retry_after),'muted small'));
  const message=node('p',undefined,'feed-control-message'),retry=button('重试该批次',async()=>{
   if(pendingFailures.has(key))return;pendingFailures.add(key);retry.disabled=true;message.textContent='正在解除退避…';
   try{await request('/api/intake/projection/'+encodeURIComponent(key)+'/retry',{});message.textContent='已加入重试，处理游标未移动。';await refresh();}
   catch(e){message.textContent=e.message;}finally{pendingFailures.delete(key);retry.disabled=false;}
  });retry.disabled=pendingFailures.has(key);retry.dataset.projectionRetry=key;
  card.append(retry,message);host.append(card);
 }
}
function render(data){
 Object.assign(labels,data.feed_labels||{});
 const counts=object(data.counters),projection=object(data.projection),receiver=object(data.receiver),available=data.database_ok!==false,db=object(data.database),byFeed=new Map(list(projection.per_feed).map(row=>[row.feed,row]));
 $('intake-database').textContent='当前事务库：'+(db.label||db.dialect||'未知')+(db.driver?' / '+db.driver:'')+(db.mode==='lightweight'?'（轻量兼容模式）':db.mode==='formal'?'（正式架构）':'')+' · 原始接收库：DuckDB · 关系图：FalkorDB'+(available?'。':'。'+(data.database_error||'事务库状态暂不可用。'));
 $('intake-database').className=available?'muted small':'desk-error';
 $('intake-counters').replaceChildren(counter(available?counts.rows:null,'已归档的原始记录'),counter(available?(projection.processed_rows??counts.processed_rows):null,'游标已处理（含跳过）'),counter(available?(projection.waiting??counts.backlog_rows):null,'等待投影的原始记录'),counter(available?projection.unresolved_batches:null,'未完成投影的批次'));
 const since=object(projection.since_upgrade);
 $('intake-counts-note').textContent=!available?'事务库状态不可用，计数与游标为未知。':projection.counts_known===false?'历史成功 / 跳过分类未知；升级后成功投影 '+number(since.projected)+'、保留原档跳过 '+number(since.skipped)+'。累计进度以处理游标为准。':'累计成功投影 '+number(projection.projected)+' · 保留原档跳过 '+number(projection.skipped)+'。这些是源记录行数，不等于新增文章数。';
 $('intake-projection-note').textContent=projection.last_error?'接收记录处理遇到错误：'+projection.last_error:projection.running===false?'接收记录处理后台当前未运行。':'';
 $('intake-runtime').textContent=data.runtime_enabled===false?'当前服务环境关闭了自动接收；仍可查看已保存材料。':receiver.running?(receiver.last_error?'接收后台正在运行，部分来源遇到错误；各流详情如下。':'接收后台运行中。'):receiver.last_error?'接收后台未运行：'+receiver.last_error:'接收后台当前未运行；已启用的流会在服务启动后按断点继续。';
 for(const feed of list(data.feeds)){
  const c=feedCard(feed),p=object(byFeed.get(feed.key));c.feed=feed;c.state.textContent=!feed.enabled?'已暂停':data.runtime_enabled===false?'已启用，自动接收关闭':receiver.running?'已启用接收':'已启用，等待接收服务';c.state.className='badge '+(feed.last_error||feed.enabled&&!receiver.running?'warn':feed.enabled?'ok':'');
  c.progress.textContent='最近归档完成：'+stamp(feed.last_complete)+'\n'+(feed.last_error?'受阻接收批次：':'下一接收批次：')+stamp(feed.next_batch);
  c.latest.textContent='上游清单最新：'+stamp(feed.latest_available)+(feed.retry_after>Date.now()/1000?' · 下次重试 '+clock(feed.retry_after):'');c.error.textContent=feed.last_error||'';
  c.projected.textContent='索引游标：'+number(p.processed_rows)+' / '+number(p.rows)+' 行 · 待处理 '+number(p.waiting)+' · 未完成 '+number(p.unresolved_batches)+' 批';
  c.classes.textContent=p.counts_known===false?'历史成功 / 跳过分类未知 · 升级后成功 '+number(p.since_upgrade_projected)+' / 跳过 '+number(p.since_upgrade_skipped):'成功投影 '+number(p.projected)+' · 保留原档跳过 '+number(p.skipped);
  if(p.oldest_pending)c.projected.textContent+='\n最早未完成：'+stamp(p.oldest_pending.stamp)+' · 游标 '+number(p.oldest_pending.offset);
  c.pause.disabled=c.retry.disabled=c.busy;c.probe.disabled=c.probing;
  c.pause.textContent=feed.enabled?'暂停接收':'继续接收';c.form.hidden=!!(feed.last_complete||feed.next_batch);c.submit.disabled=c.form.hidden||c.busy;diagnostics(c.diagnostic,feed.fetch_diagnostics||list(data.fetch_diagnostics).find(row=>row.feed===feed.key));
  c.timing.replaceChildren();const chunk=object(p.last_chunk);if(Object.keys(chunk).length){c.timing.append(node('p','最近处理 '+clock(chunk.at)+' · 本轮游标 '+number(chunk.offset_before)+' → '+number(chunk.offset_after)+' · 耗时 '+number(chunk.elapsed_ms)+' ms','muted small'));const dl=node('dl',undefined,'intake-diagnostics');for(const [phase,millis] of Object.entries(object(chunk.phase_ms)))if(numeric(millis))definition(dl,(phases[phase]||phase)+'耗时',millis.toFixed(1)+' ms');c.timing.append(dl);}
 }
 if(!available)markUnavailable();
 renderFailures(projection);
 const body=$('intake-batches');body.replaceChildren();for(const b of list(data.batches)){const row=node('tr');row.append(node('td',(labels[b.feed]||b.feed)+'\n'+stamp(b.stamp)),node('td',({archived:'原始完整归档',error:'失败，等待重试',downloading:'下载中',pending:'等待接收'})[b.state]||b.state||'待处理'),node('td',number(b.rows)+' 条\n可解析 '+number(b.valid_rows)+' / 待核 '+number(b.invalid_rows)),node('td',number(b.projected_offset)+' / '+number(b.rows)),node('td',b.last_error||b.error||(b.sha256?'SHA256 '+b.sha256.slice(0,16)+'…':'待校验')));body.append(row);}if(!body.children.length){const row=node('tr'),cell=node('td',available?'尚无可显示的最近批次。':'事务库状态不可用，批次信息未知。');cell.colSpan=5;row.append(cell);body.append(row);}
}
async function refresh(){if(loading)return;loading=true;$('intake-refresh').disabled=true;try{render(await request('/api/intake/status'));$('intake-message').textContent='';}catch(e){$('intake-message').textContent=e.message+' 最新接收状态未知，已有材料仍保留。';$('intake-counters').replaceChildren(counter(null,'已归档的原始记录'),counter(null,'游标已处理（含跳过）'),counter(null,'等待投影的原始记录'),counter(null,'未完成投影的批次'));markUnavailable();}finally{loading=false;$('intake-refresh').disabled=false;}}
async function detail(row){const dialog=$('intake-detail'),box=$('intake-detail-content');$('intake-detail-title').textContent='接收记录';box.replaceChildren(node('p','正在读取记录…'));dialog.showModal();try{const data=await request('/api/intake/records/'+encodeURIComponent(row.feed)+'/'+encodeURIComponent(row.stamp)+'/'+row.row_index);$('intake-detail-title').textContent=data.title||'无标题的报道元数据';box.replaceChildren(node('p',(labels[data.feed]||data.feed)+' · '+stamp(data.stamp)+' · 第 '+data.row_index+' 行'),node('p','记录编号：'+(data.record_id||'原始行未能解析')),node('p',data.content_note||'当前接收记录不含原始文章全文。'));const url=safe(data.url);if(url){const source=a('打开来源页面 ↗',url);source.target='_blank';source.rel='noopener noreferrer';box.append(source);}if(data.projection?.article_key){const p=node('p');p.append(a('查看新闻工作库版本','/articles/'+encodeURIComponent(data.projection.article_key)+(data.projection.revision?'?revision='+data.projection.revision:'')));box.append(p);}if(data.error)box.append(node('p','解析状态：'+data.error,'desk-error'));box.append(node('h3','结构化元数据'),node('pre',JSON.stringify(data.metadata||{},null,2)),node('h3','原始记录'+(data.raw_line_truncated?'（显示前200000字符，完整原档仍保留）':'')),node('pre',data.raw_line||''));}catch(e){box.replaceChildren(node('p',e.message,'desk-error'));}}
async function search(next=0){const button=$('intake-search').querySelector('button');button.disabled=true;try{const params=new URLSearchParams({q:$('intake-query').value,feed:$('intake-feed-filter').value,limit:'50',offset:String(next)}),data=await request('/api/intake/records?'+params);offset=data.offset;total=data.total;const box=$('intake-records');box.replaceChildren();for(const row of data.items){const host=node('article',undefined,'intake-record'),title=node('strong',row.title||'无标题的报道元数据'),line=node('div',undefined,'row'),view=node('button','查看原始记录','ghost');view.type='button';view.onclick=()=>detail(row);line.append(view);const url=safe(row.url);if(url){const link=a('来源页面 ↗',url);link.target='_blank';link.rel='noopener noreferrer';line.append(link);}if(row.projection?.article_key)line.append(a('新闻工作库','/articles/'+encodeURIComponent(row.projection.article_key)));host.append(node('span',(labels[row.feed]||row.feed)+' · '+stamp(row.stamp),'badge'),title,node('p','记录 '+(row.record_id||row.row_index)+' · '+(row.projection?.status==='projected'?'已投影':row.projection?.status==='skipped'?'原始保留，未投影':'等待投影')+' · '+(row.feed.startsWith('events_')?'官方事件编码':row.feed.startsWith('mentions_')?'文档提及关联':'GKG元数据')+'，不含原文全文'),line);box.append(host);}if(!data.items.length)box.append(node('p','当前检索没有匹配的已存记录。查询条件不会改变接收范围。','intake-empty'));$('intake-results-note').textContent='已存记录匹配 '+number(total)+' 条。';$('intake-page').textContent=number(total?offset+1:0)+'–'+number(Math.min(offset+50,total))+' / '+number(total);$('intake-prev').disabled=!offset;$('intake-next').disabled=offset+50>=total;}catch(e){$('intake-results-note').textContent=e.message;}finally{button.disabled=false;}}
 $('intake-refresh').onclick=refresh;$('intake-search').onsubmit=e=>{e.preventDefault();search();};$('intake-prev').onclick=()=>search(Math.max(0,offset-50));$('intake-next').onclick=()=>search(offset+50);$('intake-detail-close').onclick=()=>$('intake-detail').close();
 async function tick(){if(!closed&&!document.hidden)await refresh();if(!closed)timer=setTimeout(tick,5000);}window.addEventListener('pagehide',()=>{closed=true;clearTimeout(timer);});window.addEventListener('pageshow',e=>{if(e.persisted){closed=false;tick();}});tick();
})();

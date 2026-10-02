'use strict';
const R=Research,$=R.el,caseId=window.researchCaseId,base='/api/research/cases/'+encodeURIComponent(caseId);
let view=null,selectedReport='',loading=false;
const active=new Set(['queued','starting','running','waiting','stopping','uncertain']);
const qnames={open:'待查',researching:'调查中',answered:'已回答',gap:'证据不足'},knames={fact:'事实',attributed:'各方说法',analysis:'分析推断',unverified:'待证实'};
const time=t=>t?new Date(t).toLocaleString():'';
window.researchRefreshControls=()=>{if(!view)return;const status=view.runs.find(r=>r.run_id===view.active_run_id)?.status||'draft';$('start-research').disabled=active.has(status);$('stop-research').disabled=!active.has(status);$('steer-research').disabled=!['running','waiting'].includes(status);};
function sourceButton(id,paragraph='',label='查看证据'){return `<button class="evidence-button" data-source="${R.esc(id)}" data-paragraph="${R.esc(paragraph)}">${R.esc(label)}</button>`;}
function claimMarkup(c){return `<article class="question"><span class="badge">${R.esc(knames[c.kind]||c.kind)}</span><p class="research-text">${R.esc(c.text)}</p>${c.evidence.map(e=>`<div class="research-quote"><p class="research-text">${R.esc(e.quote)}</p>${sourceButton(e.source_id,e.paragraph_id,(e.relation==='contradicts'?'反驳':e.relation==='context'?'背景':'支持')+' · '+e.paragraph_id)}</div>`).join('')}</article>`;}
function renderReport(){
 const report=view.reports.find(r=>r.report_id===selectedReport)||view.reports[0];
 $('report-download').hidden=!report;$('report-version').hidden=!report;
 if(!report){$('report-content').innerHTML='<p class="muted">尚无研究报告。执行过程中，问题、来源和判断会陆续保存；可在“执行进度”和“证据与脉络”中查看。</p>';return;}
 selectedReport=report.report_id;$('report-version').value=selectedReport;$('report-download').href=base+'/reports/'+encodeURIComponent(report.report_id)+'/download';
 $('report-content').innerHTML=`<p class="small muted">${R.esc(time(report.created_at))} · ${report.completeness==='complete'?'研究完成':'部分完成，仍有待查事项'}</p>`+report.sections.map(section=>`<section><h3>${R.esc(section.heading)}</h3><div class="research-text">${R.esc(section.body)}</div>${section.claim_ids.map(id=>{const c=view.claims.find(c=>c.claim_id===id);return c?`<details><summary>查看本节判断与依据</summary>${claimMarkup(c)}</details>`:'';}).join('')}</section>`).join('')+`<h3>未解决的问题</h3><ul>${report.gaps.map(g=>`<li class="research-text">${R.esc(g)}</li>`).join('')||'<li>报告未列出；仍需人工核查。</li>'}</ul>`;
}
function render(){
 const current=view.runs.find(r=>r.run_id===view.active_run_id),status=current?.status||'draft';
 $('case-title').textContent=view.title;$('case-goal').textContent=view.goal;
 $('case-meta').textContent=`策略：${view.strategy.name} r${view.strategy.revision} · 资料截止：${time(view.cutoff)} · ${current?'本轮已检索 '+current.search_count+'/'+view.strategy.max_searches+' 次，读取 '+current.read_count+'/'+view.strategy.max_reads+' 份资料':'尚未开始执行'}`;
 $('run-status').textContent=R.statuses[status]||status;$('start-research').disabled=active.has(status);$('start-research').textContent=view.runs.length?'继续研究':'开始研究';$('stop-research').disabled=!active.has(status);$('steer-research').disabled=!['running','waiting'].includes(status);
 if(current?.error)$('research-message').textContent=current.error;
 $('questions').innerHTML=view.questions.map(q=>`<article class="question"><span class="badge">${R.esc(qnames[q.status]||q.status)}</span><p class="research-text">${R.esc(q.text)}</p>${q.answer?`<details><summary>回答与判断</summary><p class="research-text small">${R.esc(q.answer)}</p>${q.claim_ids.map(id=>{const c=view.claims.find(c=>c.claim_id===id);return c?claimMarkup(c):'';}).join('')}</details>`:''}</article>`).join('')||'<p class="muted">研究开始后展示问题和回答状态。</p>';
 $('sources').innerHTML=view.sources.map(s=>`<div class="research-source">${sourceButton(s.source_id,'',s.title)}<p class="small muted">${R.esc(s.kind)} · 发表：${R.esc(s.published_at?time(s.published_at):'未知')} · 读取：${R.esc(time(s.retrieved_at))}</p></div>`).join('')||'<p class="muted">还没有资料。</p>';
 $('claims').innerHTML=view.claims.map(claimMarkup).join('')||'<p class="muted">还没有保存的判断。</p>';
 $('timeline').innerHTML=view.claims.filter(c=>c.occurred_at).sort((a,b)=>a.occurred_at.localeCompare(b.occurred_at)).map(c=>`<div class="question"><span class="badge">${R.esc(c.occurred_at)}</span><p class="research-text">${R.esc(c.text)}</p>${c.evidence.map(e=>sourceButton(e.source_id,e.paragraph_id)).join('')}</div>`).join('')||'<p class="muted">尚无有日期依据的节点。</p>';
 $('progress').innerHTML=view.activity.map(a=>`<div class="question"><span class="small muted">${R.esc(time(a.created_at))} · ${R.esc(a.kind)}</span><div class="research-text">${R.esc(a.message)}</div></div>`).join('');
 $('runs').innerHTML=view.runs.map(r=>`<div class="question"><span class="badge">${R.esc(R.statuses[r.status]||r.status)}</span> ${R.esc(time(r.started_at))}<p class="small research-text">${R.esc(r.instruction||'初始研究目标')}<br>RunDesk 会话：${R.esc(r.session_id||'尚未创建')}</p></div>`).join('');
 $('report-version').innerHTML=view.reports.map(r=>`<option value="${R.esc(r.report_id)}">版本 ${r.version}</option>`).join('');renderReport();
}
async function load(){if(loading)return;loading=true;try{view=await R.api(base);render();}finally{loading=false;}}
$('report-version').onchange=()=>{selectedReport=$('report-version').value;renderReport();};
document.addEventListener('click',async e=>{
 const tab=e.target.closest('[data-tab]');if(tab){for(const b of document.querySelectorAll('[data-tab]'))b.classList.toggle('on',b===tab);for(const name of ['report','evidence','progress'])$('pane-'+name).hidden=tab.dataset.tab!==name;}
 const button=e.target.closest('[data-source]');if(button){try{const s=await R.api(base+'/sources/'+encodeURIComponent(button.dataset.source));const url=R.safeUrl(s.url);$('source-detail').innerHTML=`<h3>${R.esc(s.title)}</h3>${url?`<a href="${R.esc(url)}" target="_blank" rel="noopener noreferrer">打开原始来源 ↗</a>`:''}<p class="small muted">资料版本 ${R.esc(s.content_hash.slice(0,12))} · 以下为读取时保存的文本</p>`+s.paragraphs.map(p=>`<div class="paragraph ${p.id===button.dataset.paragraph?'focus':''}" id="source-${R.esc(p.id)}"><span class="small muted">${R.esc(p.id)}</span><div class="research-text">${R.esc(p.text)}</div></div>`).join('');if(button.dataset.paragraph)$('source-'+button.dataset.paragraph)?.scrollIntoView({block:'nearest'});}catch(error){$('research-message').textContent=error.message;}}
});
$('start-research').onclick=()=>R.action($('start-research'),async()=>{await R.api(base+'/start','POST',{instruction:$('followup').value,refresh_cutoff:$('refresh-cutoff').checked});$('followup').value='';$('refresh-cutoff').checked=false;selectedReport='';$('research-message').textContent='研究任务已提交，可关闭页面，服务运行时将继续执行。';await load();});
$('stop-research').onclick=()=>R.action($('stop-research'),async()=>{await R.api(base+'/stop','POST',{});$('research-message').textContent='已请求停止，保留已有资料。';await load();});
$('sync-research').onclick=()=>R.action($('sync-research'),async()=>{await R.api(base+'/sync','POST',{});$('research-message').textContent='状态已同步。';await load();});
$('steer-research').onclick=()=>R.action($('steer-research'),async()=>{const text=$('followup').value;const request_id=globalThis.crypto?.randomUUID?.()||'request-'+Date.now()+'-'+Math.random().toString(16).slice(2);const r=await R.api(base+'/steer','POST',{text,request_id});$('followup').value='';$('research-message').textContent=r.status==='instruction_accepted'?'追加要求已被 RunDesk 接收；执行中会逐步应用。':'追加要求的接收状态待确认，请查看执行记录。';await load();});
const prior=sessionStorage.getItem('research-start-message');if(prior){$('research-message').textContent=prior;sessionStorage.removeItem('research-start-message');}
load().catch(e=>$('research-message').textContent=e.message);
setInterval(()=>{if(document.hidden)return;load().catch(e=>$('research-message').textContent='进度暂时无法刷新：'+e.message);},4000);

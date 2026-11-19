(()=>{'use strict';
const $=id=>document.getElementById(id),message=()=>document.querySelector('#work-message');
function detailError(data){return typeof data.detail==='string'?data.detail:Array.isArray(data.detail)?data.detail.map(x=>x.msg).join('；'):'请求未完成，请稍后重试。';}
const task=window.contentTask;
for(const button of document.querySelectorAll('[data-work-trash]')) {
  button.addEventListener('click', async () => {
    const removing = button.dataset.workTrash === 'remove';
    if (removing && !window.confirm('删除这条选题？可在回收站恢复。')) return;
    button.disabled = true;
    message().textContent = '';
    const key = encodeURIComponent(button.dataset.workKey);
    const version = encodeURIComponent(button.dataset.workTrashVersion);
    const path = '/api/workbench/' + key + (removing ? '' : '/restore') + '?version=' + version;
    try {
      const response = await fetch(path, {method: removing ? 'DELETE' : 'POST'});
      if (!response.ok) throw Error(detailError(await response.json()));
      if (task) location.assign('/workbench');
      else location.reload();
    } catch (error) {
      message().textContent = error.message;
      button.disabled = false;
    }
  });
}
if(!task)return;
const uid=()=>globalThis.crypto?.randomUUID?.()||('work-'+Date.now()+'-'+Math.random().toString(36).slice(2));
async function request(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const d=await r.json();if(!r.ok){const e=Error(detailError(d));e.status=r.status;throw e;}return d;}
function go(url){if(typeof url!=='string'||!/^\/workbench\/[A-Za-z0-9_-]+(?:\/(?:selection|research|creation|works))?(?:\?[^#]*)?$/.test(url))throw Error('任务已保存，请在内容工作台查看。');location.assign(url);}
for(const button of document.querySelectorAll('[data-work-resume]'))button.onclick=async()=>{button.disabled=true;try{const d=await request('/api/topics/'+task.key+'/handoffs/'+button.dataset.workResume+'/resume',{});go(d.url);}catch(e){message().textContent=e.message;button.disabled=false;}};
async function post(path,body){return request('/api/workbench/'+task.key+path,body);}
function action(id,fn){$(id)?.addEventListener('click',async function(){this.disabled=true;try{await fn();}catch(e){message().textContent=e.message;this.disabled=false;}});}
action('work-from-report',async()=>{const report=$('work-report').value;const key='n2d-report-draft:'+task.key+':'+report+':'+task.version;let id=sessionStorage.getItem(key)||uid();sessionStorage.setItem(key,id);const d=await post('/from-report',{report_id:report,version:task.version,request_id:id});sessionStorage.removeItem(key);go(d.url);});
action('work-complete',async()=>{await post('/phase',{phase:'works',version:task.version});location.href='/workbench/'+task.key+'/works';});
let submitting=false;
for(const button of document.querySelectorAll('[data-work-handoff]'))button.addEventListener('click',async()=>{
 if(submitting)return;submitting=true;for(const b of document.querySelectorAll('[data-work-handoff]'))b.disabled=true;
 const key='topic-pending:handoff:'+task.key;
 try{let body=JSON.parse(sessionStorage.getItem(key)||'null');if(body&&body.action!==button.dataset.workHandoff)throw Error('上一次任务请求尚待确认，请先点击相同操作继续提交。');
  if(!body){body={revision:task.topic_revision,request_id:uid(),action:button.dataset.workHandoff,output_language:'zh-CN',duration_sec:60,find_images:false,write_script:false};sessionStorage.setItem(key,JSON.stringify(body));}
  message().textContent='正在保存并提交研究任务…';const d=await request('/api/topics/'+task.key+'/handoffs',body);sessionStorage.removeItem(key);go(d.url);
 }catch(e){if([400,404,409,422].includes(e.status))sessionStorage.removeItem(key);message().textContent=e.message;}finally{submitting=false;for(const b of document.querySelectorAll('[data-work-handoff]'))b.disabled=false;}
});
const dialog=$('work-source-dialog');let sourceId='',sourceSerial=0,translationSerial=0;
$('work-source-close')?.addEventListener('click',()=>dialog.close());
dialog?.addEventListener('close',()=>{translationSerial++;});
for(const button of document.querySelectorAll('[data-work-source]'))button.addEventListener('click',async()=>{
 const serial=++sourceSerial;translationSerial++;sourceId=button.dataset.workSource;
 $('work-source-title').textContent='正在读取资料快照…';$('work-source-meta').textContent='';$('work-source-content').textContent='';$('work-source-translation').hidden=true;$('work-translation-status').textContent='';$('work-source-translate').disabled=true;dialog.showModal();
 try{const d=await request('/api/topics/'+task.key+'/sources/'+encodeURIComponent(sourceId));if(serial!==sourceSerial)return;
  $('work-source-title').textContent=d.title;$('work-source-content').textContent=d.content;
  $('work-source-meta').textContent=(d.material_label||'保存的资料快照')+' · '+(d.language_display||'语言未知')+' · 保存于 '+d.observed_at;
  $('work-source-translate').disabled=false;
 }catch(e){if(serial===sourceSerial)$('work-source-meta').textContent=e.message;}
});
$('work-source-translate')?.addEventListener('click',async()=>{
 const serial=++translationSerial,id=sourceId,button=$('work-source-translate');button.disabled=true;$('work-translation-status').textContent='正在请求中文翻译…';
 try{let d=await request('/api/translations',{kind:'topic_source',key:id,target:'zh-CN',retry:true});const deadline=Date.now()+120000;
  while(['queued','running'].includes(d.status)&&Date.now()<deadline){if(serial!==translationSerial||!dialog.open)return;$('work-translation-status').textContent=d.status==='queued'?'翻译已排队…':'正在翻译…';await new Promise(resolve=>setTimeout(resolve,1000));d=await request('/api/translations/'+encodeURIComponent(d.id));}
  if(serial!==translationSerial||!dialog.open)return;
  if(d.status==='succeeded'){$('work-source-translated').textContent=d.translated_text;$('work-source-translation').hidden=false;$('work-translation-status').textContent='中文译文仅供阅读，原快照保留。'+(d.truncated?'本次只翻译已存文本的前一部分。':'')+(d.scope==='event_metadata'?' 这里翻译的是事件线索字段，不是新闻全文。':'');}
  else if(['queued','running'].includes(d.status))$('work-translation-status').textContent='翻译仍在后台进行，稍后再次点击查看。';
  else $('work-translation-status').textContent=d.error||'翻译未完成，可再次点击重试。';
 }catch(e){if(serial===translationSerial)$('work-translation-status').textContent=e.message;}finally{if(serial===translationSerial)button.disabled=false;}
});
})();

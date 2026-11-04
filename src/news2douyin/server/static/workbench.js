(()=>{'use strict';const task=window.contentTask;if(!task)return;
const message=()=>document.querySelector('#work-message');
for(const button of document.querySelectorAll('[data-work-resume]'))button.onclick=async()=>{button.disabled=true;try{const r=await fetch('/api/topics/'+task.key+'/handoffs/'+button.dataset.workResume+'/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const d=await r.json();if(!r.ok)throw Error(d.detail);location.href=d.url;}catch(e){message().textContent=e.message;button.disabled=false;}};
async function post(path,body){const r=await fetch('/api/workbench/'+task.key+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return d;}
function action(id,fn){document.getElementById(id)?.addEventListener('click',async function(){this.disabled=true;try{await fn();}catch(e){message().textContent=e.message;this.disabled=false;}});}
action('work-from-report',async()=>{const report=document.getElementById('work-report').value;const key='n2d-report-draft:'+task.key+':'+report+':'+task.version;let id=sessionStorage.getItem(key)||crypto.randomUUID();sessionStorage.setItem(key,id);const d=await post('/from-report',{report_id:report,version:task.version,request_id:id});sessionStorage.removeItem(key);location.href=d.url;});
action('work-complete',async()=>{await post('/phase',{phase:'works',version:task.version});location.href='/workbench/'+task.key+'/works';});
})();

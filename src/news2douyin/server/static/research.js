'use strict';
window.Research = {
  el: id => document.getElementById(id),
  esc: value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  statuses: {draft:'待开始',queued:'排队中',starting:'正在提交',running:'研究中',waiting:'等待处理',stopping:'正在停止',uncertain:'待确认',completed:'已完成',partial:'部分完成',paused:'已暂停',failed:'执行失败'},
  async api(path, method='GET', data) {
    const r = await fetch(path, {method, headers:data===undefined?{}:{'Content-Type':'application/json'}, body:data===undefined?undefined:JSON.stringify(data)});
    let value;
    try {value=await r.json();} catch {throw new Error('服务返回无效响应，请检查连接。');}
    if(!r.ok) throw new Error(typeof value.detail==='string'?value.detail:'请求失败，请检查输入或服务状态。');
    return value;
  },
  async action(button, fn) {
    button.disabled=true;
    const message=document.getElementById('research-message');
    if(message) message.textContent='正在处理…';
    try {await fn();} catch(e) {if(message)message.textContent=e.message;} finally {button.disabled=false;if(window.researchRefreshControls)window.researchRefreshControls();}
  },
  safeUrl(value) {try{const u=new URL(value);return ['https:','http:'].includes(u.protocol)?u.href:'';}catch{return '';}}
};

// Actual UI script + JSDOM + live application HTTP. No layout/GPU claim.
const assert=require('node:assert/strict'),fs=require('node:fs');
const {JSDOM}=require(process.env.TOPICS_JSDOM_MODULE||'jsdom');
const [base,source]=process.argv.slice(2);const errors=[],checks=[];
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function until(test,description){for(let i=0;i<150;i++){if(await test())return;await sleep(100)}throw Error(description);}
async function req(url,body){const r=await fetch(base+url,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});assert.ok(r.ok,await r.clone().text());return r.json();}
async function load(url,previousStorage){
 const html=await(await fetch(url)).text();const dom=new JSDOM(html,{url,runScripts:'outside-only',pretendToBeVisual:true});const w=dom.window;
 w.AbortController=AbortController;w.HTMLElement.prototype.scrollIntoView=function(){};
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};
 if(previousStorage)for(const [k,v] of previousStorage)w.sessionStorage.setItem(k,v);
 let dropMessage=false,dropHandoff=false;
 w.fetch=async(url,options)=>{const r=await fetch(new URL(url,w.location.href),options);if(options?.method==='POST'&&String(url).endsWith('/messages')&&dropMessage){dropMessage=false;throw new TypeError('response lost after commit')};if(options?.method==='POST'&&String(url).endsWith('/handoffs')&&dropHandoff){dropHandoff=false;throw new TypeError('handoff response lost after commit')};return r;};
 w.addEventListener('error',e=>errors.push(e.message));w.eval(fs.readFileSync(source,'utf8'));
 return {dom,w,$:id=>w.document.getElementById(id),dropMessage:()=>dropMessage=true,dropHandoff:()=>dropHandoff=true};
}
(async()=>{
 let view=await load(base+'/topics');let {$,w}=view;
 await until(()=>$('topic-create').disabled===false,'boot');
 $('new-topic-brief').value='找几个历史政策变化的选题';await $('topic-create').onclick();
 await until(()=>$('topic-candidates').textContent.includes('政策变化选题'),'candidate artifact');
 const wid=new URL(w.location.href).searchParams.get('workspace');assert.ok(wid);
 let d=await req('/api/topics/'+wid);assert.equal(d.messages.length,1);assert.equal(d.source_ids.length,0);checks.push('one-sentence-start-and-local-candidates');
 const adopt=[...$('topic-candidates').querySelectorAll('button')].find(b=>b.textContent==='采用为当前选题');await adopt.onclick();
 d=await req('/api/topics/'+wid);assert.equal(d.sources.length,1);assert.equal($('topic-title').value,'政策变化选题');checks.push('adopt-persists-snapshot-and-angle');
 await until(()=>$('topic-send').disabled===false,'first turn finishes');
 $('topic-prompt').value='进一步比较历史版本';view.dropMessage();await $('topic-send').onclick();
 assert.equal($('topic-send').textContent,'确认上次消息');await $('topic-send').onclick();
 await until(async()=>{const x=await req('/api/topics/'+wid);return x.messages.length===2&&x.messages[1].status==='completed'},'followup');
 d=await req('/api/topics/'+wid);assert.equal(new Set(d.messages.map(m=>m.session_id)).size,1);checks.push('lost-response-retry-and-same-session-followup');
 const storage=Array.from({length:w.sessionStorage.length},(_,i)=>{const k=w.sessionStorage.key(i);return [k,w.sessionStorage.getItem(k)]});
 view.dom.window.close();view=await load(base+'/topics?workspace='+wid,storage);({$,w}=view);
 await until(()=>$('topic-chat').textContent.includes('进一步比较历史版本'),'reload history');assert.match($('topic-selected-count').textContent,/1\/12/);checks.push('reload-restores-conversation-and-selection');
 const sourceButton=$('topic-selected').querySelector('button');await sourceButton.onclick();await until(()=>$('topic-source-dialog').open,'snapshot modal');assert.match($('topic-source-content').textContent,/introduced in 2023/);assert.doesNotMatch($('topic-source-content').textContent,/UPDATED/);$('topic-source-close').onclick();checks.push('source-preview-is-immutable');
 // Filter is independent of the saved selection; an empty result does not drop it.
 $('topic-query').value='no-such-subject';$('topic-query').dispatchEvent(new w.Event('input'));await $('topic-search').onclick();assert.match($('topic-results').textContent,/没有匹配资料/);assert.match($('topic-selected-count').textContent,/1\/12/);checks.push('manual-search-preserves-selection');
 $('topic-write').checked=false;view.dropHandoff();await $('topic-materials').onclick();
 await $('topic-materials').onclick();d=await req('/api/topics/'+wid);assert.equal(d.tasks.length,1);assert.equal(d.tasks[0].action,'materials');checks.push('materials-only-handoff-and-lost-response-dedup');
 assert.deepEqual(errors,[]);checks.push('no-ui-runtime-errors');
 view.dom.window.close();console.log(JSON.stringify({passed:checks.length,checks,realBrowserLayout:false}));
})().catch(e=>{console.error(e);process.exitCode=1});

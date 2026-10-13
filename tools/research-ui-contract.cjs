// Actual page JavaScript in a small DOM stub; no browser rendering claim.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const data=JSON.parse(fs.readFileSync(0,'utf8')),checks=[];
class Element {
 constructor(){this.value='';this.checked=false;this.disabled=false;this.hidden=false;this.textContent='';}
 set innerHTML(value){this.html=value;if(!this.value){const m=value.match(/<option value="([^"]+)"/);if(m)this.value=m[1];}}
 get innerHTML(){return this.html||'';}
 reportValidity(){return true;}
}
const settings={rundesk_url:'http://localhost:3210',callback_url:'http://localhost:18080',runtime_mode:'docker',docker_image:'news:test',docker_permissions:'full-access',mcp_callback_url:'',search_provider:'rundesk',search_backend:'wigolo',setup_message:'待连接',instance_id:''};
const strategy={strategy_id:'background',name:'背景研究',revision:1,questions:['原始依据'],preferred_domains:[],language:'zh',max_searches:10,max_reads:20,max_minutes:20,enabled:true};
const view={case_id:'case-test',title:'新闻标题',goal:'背景研究',strategy,cutoff:'2026-01-01T00:00:00Z',active_run_id:'run-test',questions:[],sources:[],claims:[],reports:[],activity:[],runs:[{run_id:'run-test',status:'preparing',started_at:'2026-01-01T00:00:00Z',search_count:0,read_count:0,environment:{mode:'docker',status:'preparing'}}]};
function page(name){
 const elements=Object.fromEntries([...data.pages[name].matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element()])),calls=[];
 const context={console,URL,Date,Math,JSON,String,Number,Set,Promise,crypto:{randomUUID:()=> 'request-12345678'},location:{origin:'http://localhost:18080',href:''},sessionStorage:{getItem:()=>null,removeItem:()=>{},setItem:()=>{}},setInterval:()=>{},document:{hidden:false,getElementById:id=>elements[id]||null,addEventListener:()=>{},querySelectorAll:()=>[]}};
 context.window=context;
 context.fetch=async(path,options={})=>{
  const method=options.method||'GET',body=options.body?JSON.parse(options.body):undefined;calls.push({path,method,body});let value={};
  if(path==='/api/research/settings'){if(method==='PUT')Object.assign(settings,body);value=settings;}
  else if(path==='/api/research/setup'){settings.instance_id='instance-test';settings.setup_message='Docker 环境与业务工具已配置';value=settings;}
  else if(path==='/api/research/check')value={version:'0.47.0',runtime:{environment:{state:'running'}},message:'Docker 检查通过'};
  else if(path==='/api/research/strategies')value=[strategy];
  else if(path==='/api/research/cases')value=method==='POST'?{case_id:'case-test'}:[];
  else if(path.endsWith('/start'))view.runs[0].status='preparing';
  else if(path.endsWith('/stop'))view.runs[0].status='paused';
  else if(path==='/api/research/cases/case-test')value=view;
  return {ok:true,json:async()=>JSON.parse(JSON.stringify(value))};
 };
 vm.createContext(context);vm.runInContext(data.common,context);
 for(const [,code]of data.pages[name].matchAll(/<script>([\s\S]*?)<\/script>/g))vm.runInContext(code,context);
 if(name==='research_case')vm.runInContext(data.caseJS,context);
 return {elements,calls,context};
}
const settled=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 const s=page('research_settings');await settled();
 assert.equal(s.elements['runtime-mode'].value,'docker');assert.equal(s.elements['docker-permissions'].value,'full-access');assert.equal(s.elements['search-backend'].value,'wigolo');
 await s.elements['setup-assistant'].onclick();
 const saved=s.calls.find(x=>x.method==='PUT');assert.equal(saved.body.runtime_mode,'docker');assert.equal(saved.body.docker_image,'news:test');
 assert(s.calls.findIndex(x=>x.method==='PUT')<s.calls.findIndex(x=>x.path.endsWith('/setup')));checks.push('settings_saves_docker_selection_before_setup');
 await s.elements['check-assistant'].onclick();assert.match(s.elements['research-message'].textContent,/Docker 检查通过/);checks.push('runtime_check_result_visible');
 const n=page('research');await settled();n.elements['research-title'].value='标题';n.elements['research-goal'].value='研究背景';
 n.elements['create-research'].onsubmit({preventDefault(){}});await settled();
 assert(n.calls.some(x=>x.path==='/api/research/cases'&&x.method==='POST'));assert(n.calls.some(x=>x.path.endsWith('/start')));assert.equal(n.context.location.href,'/research/case-test');checks.push('one_click_creates_case_starts_and_opens_progress');
 const c=page('research_case');await settled();
 assert.equal(c.elements['run-status'].textContent,'正在准备 Docker 环境');assert(c.elements['start-research'].disabled);assert(!c.elements['stop-research'].disabled);checks.push('preparing_disables_duplicate_start_and_allows_stop');
 await c.elements['stop-research'].onclick();assert.equal(c.elements['run-status'].textContent,'已暂停');assert(!c.elements['start-research'].disabled);checks.push('stop_refreshes_persisted_status');
 console.log(JSON.stringify({passed:true,checks,scope:'Rendered templates and actual JS handlers in DOM stub; browser layout not tested'}));
})().catch(e=>{console.error(e);process.exitCode=1;});

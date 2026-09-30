// Start with tools/smoke_daily_tasks.py. Model responses are offline fixtures.
const {chromium}=require(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES?process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES+'/playwright':'playwright');
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const base=process.argv[2]||'http://127.0.0.1:18198',out=process.argv[3]||'daily-task-browser-results';await fs.mkdir(out,{recursive:true});
 const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN||undefined,args:['--no-sandbox']});
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),errors=[],checks=[];
 context.on('page',p=>p.on('pageerror',e=>errors.push(String(e))));let page=await context.newPage();
 const api=async(url,data)=>{const r=await (data===undefined?context.request.get(base+url):context.request.post(base+url,{data}));assert.ok(r.ok(),await r.text());return r.json();};
 const control=mode=>api('/fixture/control/'+mode,{});
 async function until(fn){for(let i=0;i<120;i++){if(await fn())return;await new Promise(r=>setTimeout(r,100));}throw new Error('condition timeout');}
 async function choose(key,active=true){await page.locator('.choose-news[data-key="'+key+'"]').setChecked(active);await until(async()=>{const picks=await api('/api/daily/selections');return picks.some(p=>p.article_key===key)===active;});}
 async function shot(name){await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:path.join(out,name+'.png'),fullPage:true});}
 try{
  await page.goto(base+'/daily');await choose('demo0');await choose('demo1');await control('hold');
  await page.locator('#generation-mode').selectOption('llm');await page.locator('#build-picks').click();
  await until(async()=>await api('/fixture/state').then(x=>x.waiting));
  await page.waitForFunction(()=>document.querySelectorAll('#picked a[href^="/tasks/"]').length===2);
  let jobs=await api('/api/daily/script-tasks');assert.equal(jobs.length,2);assert.equal(jobs.filter(x=>x.task.status==='queued').length,1);checks.push('selected_batch_commits_both_jobs_before_model_returns');
  await page.locator('#build-picks').click();await page.waitForFunction(()=>!document.querySelector('#build-picks').disabled);assert.equal((await api('/api/daily/script-tasks')).length,2);checks.push('duplicate_submit_reuses_jobs');await shot('daily-background');
  await page.close();await control('release');await until(async()=>await api('/api/daily/script-tasks').then(x=>x.every(r=>r.task.status==='succeeded')));
  page=await context.newPage();await page.goto(base+'/scripts');assert.equal(await page.locator('#daily-generations tr').count(),3);checks.push('closed_page_does_not_interrupt_model_jobs');
  await page.locator('#daily-generations').getByRole('link',{name:'打开草稿',exact:true}).first().click();await page.locator('#script-text').waitFor();assert.equal(await page.locator('.source-key').count(),1);checks.push('workbench_recovers_source_bound_draft');
  await page.goto(base+'/daily');assert.equal(await page.locator('#picked').getByRole('link',{name:'打开脚本',exact:true}).count(),2);checks.push('daily_reload_restores_completed_results');
  await choose('demo2');await control('fail_once');await page.locator('#generation-mode').selectOption('llm');await page.locator('#build-picks').click();
  await page.waitForFunction(()=>document.querySelector('#picked').textContent.includes('生成失败'));
  jobs=await api('/api/daily/script-tasks');const failed=jobs.find(j=>j.source.article_key==='demo2');assert.equal(failed.task.status,'failed');
  await page.locator('[data-selection="'+failed.selection_key+'"]').getByRole('link',{name:'进度与重试',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#status').textContent==='失败');
  assert.ok((await page.locator('#script-input').textContent()).includes('固定报道 r1'));assert.equal(await page.locator('a[href$="/diagnostics"]').count(),0);await shot('daily-failed');checks.push('failure_has_source_snapshot_and_no_collection_diagnostics');
  await page.locator('#retry').click();await page.waitForFunction(()=>document.querySelector('#status').textContent==='已完成');assert.equal(await page.locator('#attempts').textContent(),'2');await page.locator('#script-result').waitFor({state:'visible'});checks.push('retry_completes_original_daily_job');
  await page.goto(base+'/daily');for(const key of ['demo0','demo1','demo2'])await choose(key,false);
  await choose('demo3');await control('hold');await page.locator('#generation-mode').selectOption('llm');await page.locator('#build-picks').click();await page.waitForURL('**/tasks/*');const oldId=page.url().split('/').pop();
  await until(async()=>await api('/fixture/state').then(x=>x.waiting));await page.goto(base+'/daily');await choose('demo3',false);await choose('demo3');
  await page.locator('#generation-mode').selectOption('basic');await page.locator('#build-picks').click();await page.waitForURL('**/tasks/*');const newId=page.url().split('/').pop();assert.notEqual(oldId,newId);await control('release');
  await page.waitForFunction(()=>document.querySelector('#status').textContent==='已完成');assert.equal((await api('/api/tasks/'+oldId)).status,'cancelled');assert.equal((await api('/api/daily/script-tasks/'+oldId)).package_key,null);checks.push('remove_reselect_blocks_late_model_result');
  await page.goto(base+'/tasks/'+oldId);await page.waitForFunction(()=>document.querySelector('#script-input').textContent.includes('历史任务'));assert.equal(await page.locator('#retry').isDisabled(),true);assert.equal((await context.request.post(base+'/api/tasks/'+oldId+'/retry')).status(),409);checks.push('detached_task_cannot_be_retried');
  await page.goto(base+'/tasks/'+newId);await page.locator('#script-result').waitFor({state:'visible'});await shot('daily-result');
  await page.locator('#daily-input').click();await page.waitForURL('**/daily?*');assert.equal(await page.locator('#pick-count').textContent(),'1');checks.push('result_returns_to_correct_day_and_timezone');
  await page.goto(base+'/scripts');assert.equal(await page.locator('#daily-generations tr').count(),6);checks.push('history_and_earlier_drafts_remain_available');
  await page.setViewportSize({width:390,height:844});for(const route of ['/daily','/scripts','/tasks/'+newId]){await page.goto(base+route);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,route);}await shot('daily-task-mobile');checks.push('daily_workbench_and_task_fit_mobile');
  assert.deepEqual(errors,[]);checks.push('no_javascript_errors');
  const report={passed:checks.length,checks,browser:browser.version(),errors};await fs.writeFile(path.join(out,'daily-tasks-browser-report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

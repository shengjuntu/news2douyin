// Use tools/smoke_daily.py as the isolated offline fixture server.
const {chromium}=require(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES?process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES+'/playwright':'playwright');
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const base=process.argv[2]||'http://127.0.0.1:18191',out=process.argv[3]||'browser-results';await fs.mkdir(out,{recursive:true});
 const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN||undefined,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[],checks=[];page.on('pageerror',e=>errors.push(String(e)));page.on('dialog',d=>d.accept());
 const waitMessage=(id,text)=>page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});
 try{
  await page.goto(base+'/profiles');await page.locator('#name').fill('中文芯片新闻');await page.locator('#provider').selectOption('mock');await page.locator('#categories').fill('');await page.locator('#keywords_include').fill('芯片');await page.locator('#filter-mode').selectOption('rules');
  await page.locator('#save-profile').click();await waitMessage('profile-message','已保存');checks.push('chinese_profile_saved');
  await page.locator('#keywords_include').fill('芯片,科技');assert.equal(await page.locator('#test-profile').isDisabled(),true);await page.locator('#save-profile').click();await waitMessage('profile-message','已保存');checks.push('trial_requires_saved_config');
  await page.locator('#test-profile').click();await page.waitForURL('**/diagnostics');await waitMessage('diagnostic-summary','试跑保留 1 条');
  assert.match(await page.locator('main').innerText(),/未命中关键词或分类规则/);
  const articleData=await (await page.request.get(base+'/api/articles?paginated=true')).json();assert.equal(articleData.total,0);checks.push('trial_reasons_and_no_news_written');
  await page.screenshot({path:path.join(out,'trial-diagnostics.png'),fullPage:true});
  const trialURL=page.url();await page.getByRole('link',{name:'返回策略与计划'}).click();assert.equal(await page.locator('#name').inputValue(),'中文芯片新闻');
  await page.locator('#tests-list a').first().waitFor();checks.push('recent_trial_link');
  assert.equal(await page.locator('#job-weekdays').isVisible(),false);assert.equal(await page.locator('#job-cron-label').isVisible(),false);
  await page.locator('#job-name').fill('每周芯片早报');await page.locator('#job-profile').selectOption('中文芯片新闻');await page.locator('#job-frequency').selectOption('weekly');
  await page.locator('#job-weekdays input[value="1"]').check();await page.locator('#job-weekdays input[value="3"]').check();await page.locator('#job-time').fill('08:30');await page.locator('#preview-job').click();await waitMessage('job-message','下三次当地时间');
  await page.locator('#save-job').click();await waitMessage('job-message','计划已保存');let jobs=await (await page.request.get(base+'/api/jobs')).json();assert.equal(jobs[0].cron_expr,'30 8 * * 1,3');assert.equal(jobs[0].next_runs.length,3);checks.push('weekly_schedule_preview_and_save');
  await page.locator('#jobs-body button').filter({hasText:'编辑'}).click();await page.locator('#job-frequency').selectOption('weekdays');await page.locator('#job-time').fill('09:45');await page.locator('#save-job').click();await waitMessage('job-message','计划已保存');
  jobs=await (await page.request.get(base+'/api/jobs')).json();assert.equal(jobs[0].cron_expr,'45 9 * * 1-5');checks.push('schedule_edit');
  await page.locator('#delete-profile').click();await waitMessage('profile-message','关联计划');checks.push('referenced_profile_delete_guard');
  await page.locator('#toggle-profile').click();await waitMessage('profile-message','策略已停用');jobs=await (await page.request.get(base+'/api/jobs')).json();assert.equal(jobs[0].enabled,false);assert.equal(await page.locator('#test-profile').isDisabled(),true);checks.push('disable_pauses_associated_schedule');
  await page.goto(base+'/daily');assert.equal(await page.getByRole('button',{name:'采集所选日期',exact:true}).isDisabled(),true);assert.match(await page.locator('main').innerText(),/新建或启用策略/);checks.push('daily_excludes_disabled_profile');
  await page.goto(base+'/profiles?selected='+encodeURIComponent('中文芯片新闻'));await page.locator('#toggle-profile').click();await waitMessage('profile-message','策略已启用');jobs=await (await page.request.get(base+'/api/jobs')).json();assert.equal(jobs[0].enabled,false);
  await page.locator('#jobs-body button').filter({hasText:'启用'}).click();await page.waitForFunction(()=>document.getElementById('jobs-body').textContent.includes('暂停'));checks.push('enable_does_not_resume_without_action');
  await page.locator('#keywords_include').fill('尚未保存的修改');await page.locator('#jobs-body button').filter({hasText:'暂停'}).click();await page.waitForFunction(()=>document.getElementById('jobs-body').textContent.includes('启用'));assert.equal(await page.locator('#keywords_include').inputValue(),'尚未保存的修改');checks.push('schedule_actions_preserve_unsaved_profile_edits');
  await page.reload();await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:path.join(out,'strategy-schedules.png'),fullPage:true});
  await page.locator('#jobs-body button').filter({hasText:'删除'}).click();await page.waitForFunction(()=>document.getElementById('jobs-body').textContent.includes('还没有'));
  await page.locator('#delete-profile').click();await waitMessage('profile-message','策略配置已删除');await page.goto(trialURL);await waitMessage('diagnostic-summary','试跑保留 1 条');checks.push('deletion_preserves_historical_trial');
  assert.deepEqual(errors,[]);checks.push('no_javascript_errors');
  const result={passed:checks.length,checks,browser:browser.version(),errors};await fs.writeFile(path.join(out,'management-browser-report.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

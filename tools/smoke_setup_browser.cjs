const {chromium}=require(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES?process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES+'/playwright':'playwright');
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const base=process.argv[2]||'http://127.0.0.1:18200',out=process.argv[3]||'setup-browser-results';await fs.mkdir(out,{recursive:true});
 const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN||undefined,args:['--no-sandbox']});
 const ctx=await browser.newContext({viewport:{width:1440,height:1000}}),page=await ctx.newPage(),checks=[],errors=[];
 page.on('pageerror',e=>errors.push(String(e)));
 const api=async(url,data)=>{const r=await(data===undefined?ctx.request.get(base+url):ctx.request.post(base+url,{data}));assert.ok(r.ok());return r.json();};
 const ready=()=>page.waitForFunction(()=>document.querySelector('#setup-status').textContent.startsWith('配置检查于'));
 const shot=async name=>{await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:path.join(out,name+'.png'),fullPage:true});};
 try{
  await page.goto(base+'/daily');await page.locator('main a[href="/setup"]').click();await ready();
  assert.match(await page.locator('#news-profiles').textContent(),/还没有策略/);assert.match(await page.locator('#model-config').textContent(),/使用默认值/);
  assert.deepEqual(await api('/fixture'),{model_calls:0,video_calls:0,mode:'error'});checks.push('empty_install_entry_and_no_automatic_external_checks');await shot('setup-empty');
  await api('/fixture/configure',{});await page.locator('#refresh-config').click();await ready();
  assert.match(await page.locator('#news-summary').textContent(),/已启用 3 个/);assert.match(await page.locator('#news-profiles').textContent(),/缺少密钥/);assert.match(await page.locator('#news-profiles').textContent(),/待试跑/);checks.push('demo_missing_key_and_configured_profiles_are_distinct');
  assert.match(await page.locator('#setup-schedules').textContent(),/关联策略不存在/);assert.match(await page.locator('#setup-schedules').textContent(),/当前实例不会执行/);checks.push('schedule_dependencies_and_stopped_scheduler_are_explained');
  assert.equal(await page.evaluate(()=>window.injected),undefined);assert.equal(await page.locator('#news-profiles img').count(),0);assert.equal(await page.locator('#model-config script').count(),0);checks.push('configured_text_is_escaped');
  await page.locator('#probe-model').click();await page.waitForFunction(()=>document.querySelector('#model-result').textContent.includes('拒绝鉴权'));assert.equal((await api('/fixture')).model_calls,1);checks.push('explicit_model_check_reports_authentication_failure');
  await api('/fixture/model-ready',{});await page.locator('#probe-model').click();await page.waitForFunction(()=>document.querySelector('#model-result').textContent.includes('模型已列出'));
  assert.match(await page.locator('#model-result').textContent(),/尚未验证生成能力/);checks.push('model_list_success_does_not_claim_generation_success');
  await page.locator('#check-video').click();await page.waitForFunction(()=>document.querySelector('#video-result').textContent.includes('基础环境有缺项'));assert.match(await page.locator('#video-result').textContent(),/安装 FFmpeg/);assert.match(await page.locator('#video-result').textContent(),/可选上传配音/);checks.push('video_missing_dependencies_have_fixes_and_optional_voice');
  assert.doesNotMatch(await page.locator('body').textContent(),/private-news-key|private-model-key|private-secret/);checks.push('credentials_and_raw_error_details_are_not_displayed');await shot('setup-configured');
  await page.route('**/api/setup/check',r=>r.fulfill({status:503,contentType:'application/json',body:JSON.stringify({message:'无法读取数据库，请检查连接和访问权限。'})}));await page.locator('#refresh-config').click();await page.waitForFunction(()=>document.querySelector('#setup-status').textContent.includes('状态可能已过时'));assert.match(await page.locator('#news-summary').textContent(),/已启用 3 个/);await page.unroute('**/api/setup/check');await page.locator('#refresh-config').click();await ready();checks.push('temporary_failure_keeps_prior_report_and_recovers');
  for(const href of ['/profiles','/profiles#schedules','/daily','/scripts','/videos','/tasks'])assert.ok(await page.locator(`main a[href="${href}"]`).count());checks.push('workflow_and_repair_links_are_present');
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await shot('setup-mobile');checks.push('mobile_has_no_horizontal_overflow');
  assert.deepEqual(errors,[]);checks.push('no_javascript_errors');const report={passed:checks.length,checks,browser:browser.version(),errors};await fs.writeFile(path.join(out,'setup-browser-report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

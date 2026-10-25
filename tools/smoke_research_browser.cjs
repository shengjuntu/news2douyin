const {chromium}=require(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES+'/playwright');
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const base=process.argv[2]||'http://127.0.0.1:18311',out=process.argv[3]||'research-browser-results';await fs.mkdir(out,{recursive:true});
 const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN||undefined,args:['--no-sandbox']});
 const context=await browser.newContext({viewport:{width:1500,height:1050}}),page=await context.newPage(),errors=[],checks=[];
 page.on('pageerror',e=>errors.push(String(e)));
 const screenshot=async name=>{await page.screenshot({path:path.join(out,name+'.png'),fullPage:true});};
 try{
  await page.goto(base+'/research/settings');await page.locator('#rundesk-url').waitFor();
  await page.waitForFunction(()=>document.querySelector('#strategy-name').value.length>0);
  await page.locator('#callback-url').fill(base);await page.locator('#setup-assistant').click();
  await page.waitForFunction(()=>document.querySelector('#research-message').textContent.includes('研究助手已配置'));
  assert.match(await page.locator('#connection-summary').textContent(),/instance-research/);checks.push('instance_skill_mcp_setup');
  await page.locator('#check-assistant').click();await page.waitForFunction(()=>document.querySelector('#research-message').textContent.includes('研究工具：已发现'));checks.push('readiness_check');
  await page.locator('#new-strategy').click();await page.locator('#strategy-id').fill('browser-policy');await page.locator('#strategy-name').fill('浏览器测试策略');await page.locator('#strategy-questions').fill('查找原始政策\n比较新旧版本');await page.locator('#save-strategy').click();await page.waitForFunction(()=>document.querySelector('#research-message').textContent==='策略已保存。');checks.push('strategy_create');await screenshot('research-settings');
  await page.goto(base+'/articles/research-demo');await page.locator('a[href="/research?article_key=research-demo"]').click();
  await page.waitForFunction(()=>document.querySelector('#research-strategy').options.length>0);await page.locator('#research-strategy').selectOption('browser-policy');await page.locator('#create-and-start').click();await page.waitForURL(/\/research\/research_/);
  await page.waitForFunction(()=>document.querySelector('#run-status').textContent==='研究中');assert.equal(await page.locator('#start-research').isDisabled(),true);checks.push('article_launch_and_background_dispatch');
  await page.locator('#followup').fill('重点研究历史背景');await page.locator('#steer-research').click();await page.waitForFunction(()=>document.querySelector('#research-message').textContent.includes('接收'));checks.push('steer_while_running');
  const caseId=page.url().split('/').pop();const finish=await context.request.post(base+'/fixture/research/'+caseId+'/finish');assert.equal(finish.ok(),true);
  await page.locator('#sync-research').click();await page.waitForFunction(()=>document.querySelector('#run-status').textContent==='部分完成');assert.match(await page.locator('#report-content').textContent(),/2023/);checks.push('report_and_gap_persisted');
  await page.locator('#report-content summary').first().click();await page.locator('#report-content [data-source]').first().click();await page.locator('#source-detail .focus').waitFor();assert.match(await page.locator('#source-detail').textContent(),/旧政策于 2023/);checks.push('report_to_exact_evidence');
  await screenshot('research-report');const download=await context.request.get(base+await page.locator('#report-download').getAttribute('href'));assert.equal(download.ok(),true);assert.match(await download.text(),/example.com\/policy/);checks.push('report_export');
  await page.locator('[data-tab="evidence"]').click();assert.match(await page.locator('#timeline').textContent(),/2023/);await screenshot('research-evidence');checks.push('timeline_candidates');
  await page.reload();await page.waitForFunction(()=>document.querySelector('#run-status').textContent==='部分完成');assert.match(await page.locator('#report-content').textContent(),/历史背景/);checks.push('reload_keeps_results');
  await page.locator('#followup').fill('继续查找实施效果');await page.locator('#start-research').click();await page.waitForFunction(()=>document.querySelector('#run-status').textContent==='研究中');await page.locator('#stop-research').click();await page.waitForFunction(()=>document.querySelector('#run-status').textContent==='已暂停');checks.push('resume_and_stop');
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await screenshot('research-mobile');checks.push('mobile_no_horizontal_overflow');
  assert.deepEqual(errors,[]);checks.push('no_javascript_errors');const result={passed:checks.length,checks,errors,browser:browser.version()};await fs.writeFile(path.join(out,'research-browser-report.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

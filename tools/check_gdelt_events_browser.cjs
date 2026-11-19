/* Companion isolated fixture: check_gdelt_events_live.py. */
const {chromium}=require('playwright-core');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
let browser;
(async()=>{
  const base=process.env.N2D_QA_URL,folder=process.env.N2D_QA_OUTPUT;
  assert(base&&folder);assert(['localhost','127.0.0.1'].includes(new URL(base).hostname));
  let executablePath=process.env.N2D_QA_CHROMIUM,args=['--no-sandbox','--disable-dev-shm-usage'];
  if(!executablePath&&process.env.N2D_QA_CHROMIUM_MODULE){const module=require(process.env.N2D_QA_CHROMIUM_MODULE);const support=module.default||module;executablePath=await support.executablePath();args=support.args;}
  assert(executablePath,'Set N2D_QA_CHROMIUM or N2D_QA_CHROMIUM_MODULE');
  browser=await chromium.launch({executablePath,args,headless:true});const page=await browser.newPage();
  const pageErrors=[],consoleErrors=[],failedRequests=[],results={};
  page.on('pageerror',e=>pageErrors.push(e.message));page.on('console',m=>{if(m.type()==='error')consoleErrors.push(m.text());});page.on('requestfailed',r=>failedRequests.push(r.url()));
  async function noOverflow(label){const value=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));assert(value.scroll<=value.width+3,label+' overflow '+JSON.stringify(value));return value;}
  async function counts(){return (await page.request.get(base+'/qa/gdelt-counts')).json();}
  for(const [name,viewport] of Object.entries({desktop:{width:1440,height:1000},mobile:{width:390,height:844}})){
    await page.setViewportSize(viewport);await page.goto(base+'/radar?source=gdelt');
    assert.equal(await page.locator('[data-gdelt-event]').count(),2);
    await page.getByRole('link',{name:'已整理事件',exact:true}).click();await page.waitForURL(/source=local/);assert.equal(await page.locator('[data-radar]').count(),1);
    await page.goto(base+'/radar?source=gdelt');assert.equal(await page.locator('[data-gdelt-event]').count(),2);
    assert.equal(await page.locator('.gdelt-advanced').getAttribute('open'),null);
    const uncovered=page.locator('[data-gdelt-event="987654"]');assert((await uncovered.textContent()).includes('媒体域名'));assert.equal(await uncovered.locator('.gdelt-event-facts strong').first().textContent(),'未知');
    await uncovered.locator('summary').click();await page.locator('#gdelt-detail-dialog').getByRole('link',{name:'官方事件记录的首次来源 ↗'}).waitFor();await page.locator('#gdelt-detail-close').click();
    const initial=await counts();assert.equal(initial.Article,0);assert.equal(initial.Event,0);assert.equal(initial.RadarEntry,0);
    await page.locator('select[name="cameo"]').selectOption('07');await page.locator('#gdelt-filter button[type="submit"]').click();
    await page.waitForURL(/cameo=07/);assert.equal(await page.locator('[data-gdelt-event]').count(),1);
    const source=page.locator('[data-gdelt-sources="123456"]');await source.locator('summary').click();
    await page.locator('#gdelt-detail-dialog').getByRole('link',{name:'one.example ↗'}).waitFor();await page.locator('#gdelt-detail-close').click();
    await page.locator('.gdelt-advanced summary').click();await page.locator('#gdelt-preset-name').fill('浏览器筛选-'+name);
    await page.locator('#gdelt-save-filter').click();await page.getByText('筛选已保存，可直接切换。').waitFor();
    assert(await page.locator('select[name="preset"]').textContent().then(value=>value.includes('浏览器筛选-'+name)));
    await page.screenshot({path:path.join(folder,'gdelt-'+name+'.png'),fullPage:true});
    results[name]={viewport,explicit_official_navigation:true,explicit_local_navigation:true,filters_work:true,preset_saved:true,source_detail_loaded:true,unknown_coverage_and_first_source:true,advanced_collapsed_initially:true,bounds:await noOverflow(name)};
  }
  await page.locator('[data-gdelt-select="123456"]').click();await page.locator('#gdelt-topic-title').fill('浏览器测试选题');await page.locator('#gdelt-topic-angle').fill('核对援助事实与历史变化');
  await page.locator('#gdelt-select-submit').click();await page.waitForURL(/\/workbench\/[^/]+\/selection/);await page.waitForLoadState('networkidle');
  const after=await counts();assert.equal(after.ContentTask,1);assert.equal(after.Event,0);assert.equal(after.Article,0);
  results.explicit_selection={navigates_to_workbench:true,content_tasks:after.ContentTask,local_events:after.Event,articles:after.Article};
  await page.goto(base+'/intake');await page.getByText('GDELT Events English',{exact:true}).first().waitFor();
  assert.equal(await page.locator('[data-intake-feed]').count(),6);results.intake_six_streams=true;
  results.mobile_intake_bounds=await noOverflow('mobile intake');
  assert.deepEqual(pageErrors,[]);assert.deepEqual(consoleErrors,[]);assert.deepEqual(failedRequests,[]);
  const report={ok:true,browser:browser.version(),results,page_errors:pageErrors,console_errors:consoleErrors,failed_requests:failedRequests};
  fs.writeFileSync(path.join(folder,'gdelt-events-browser-results.json'),JSON.stringify(report,null,2)+'\n');await browser.close();browser=null;
})().catch(async error=>{console.error(error);if(browser)await browser.close();process.exitCode=1;});

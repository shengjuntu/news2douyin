/* Companion loopback fixture runner: check_graph_maintenance_live.py. */
const {chromium} = require('playwright-core');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let browser;

(async () => {
  const base = process.env.N2D_QA_URL, folder = process.env.N2D_QA_OUTPUT;
  assert(base && folder, 'N2D_QA_URL and N2D_QA_OUTPUT are required');
  assert(['127.0.0.1','localhost','[::1]'].includes(new URL(base).hostname), 'QA fixture must use loopback');
  let executablePath = process.env.N2D_QA_CHROMIUM;
  let args = process.env.N2D_QA_CHROMIUM_ARGS ? JSON.parse(process.env.N2D_QA_CHROMIUM_ARGS) : ['--no-sandbox','--disable-dev-shm-usage'];
  if (!executablePath && process.env.N2D_QA_CHROMIUM_MODULE) {
    const supportModule = require(process.env.N2D_QA_CHROMIUM_MODULE);
    const support = supportModule.default || supportModule;
    executablePath = await support.executablePath(); args = support.args;
  }
  assert(executablePath, 'Set N2D_QA_CHROMIUM or N2D_QA_CHROMIUM_MODULE');
  browser = await chromium.launch({executablePath,args,headless:true});
  const page = await browser.newPage();
  page.setDefaultTimeout(12000);
  const pageErrors=[],consoleErrors=[],failedRequests=[],results={};
  page.on('pageerror',err=>pageErrors.push(err.message));
  page.on('console',message=>{if(message.type()==='error')consoleErrors.push(message.text());});
  page.on('requestfailed',request=>failedRequests.push(request.url()));
  async function checkpoint() {
    const response=await page.request.get(base+'/qa/maintenance-checkpoint');
    assert.equal(response.status(),200);return response.json();
  }
  async function noOverflow(label) {
    const bounds=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));
    assert(bounds.scroll<=bounds.width+3,label+' horizontal overflow '+JSON.stringify(bounds));return bounds;
  }
  async function expand() {
    const panel=page.locator('details.work-actions').filter({has:page.locator('#knowledge-topic-progress')});
    assert.equal(await panel.count(),1);
    assert.equal(await panel.getAttribute('open'),null,'maintenance details initially collapsed');
    await panel.locator(':scope > summary').click();
    return panel;
  }
  for (const [label,viewport] of [['desktop',{width:1440,height:1000}],['mobile',{width:390,height:844}]]) {
    await page.setViewportSize(viewport);
    await page.goto(base+'/knowledge',{waitUntil:'networkidle'});
    await page.locator('#knowledge-status-config').waitFor({state:'attached'});
    await noOverflow(label+' collapsed');
    await page.screenshot({path:path.join(folder,'maintenance-'+label+'-collapsed.png'),fullPage:true});
    await expand();
    assert.match(await page.locator('#knowledge-projection-event').innerText(),/单次平均 0\.02 秒/);
    assert.match(await page.locator('#knowledge-projection-watch').innerText(),/单次平均 100(?:\.0)? 秒/);
    assert.match(await page.locator('#knowledge-projection-watch').innerText(),/已确认 0 项.*已完成分批 4 次/);
    assert.match(await page.locator('#knowledge-topic-progress').innerText(),/同步事件材料.*已检查 400 项.*完成 12 批/);
    assert.match(await page.locator('#knowledge-gc-counts').innerText(),/累计已检查 500 个节点/);
    const before=await checkpoint();
    await page.locator('#pause-graph-gc').click();
    await page.locator('#knowledge-gc').filter({hasText:'图清理：已暂停'}).waitFor();
    assert.equal(await page.locator('#resume-graph-gc').isVisible(),true);
    assert.deepEqual(await checkpoint(),before,'pause preserves all checkpoints and pending projection');
    await page.screenshot({path:path.join(folder,'maintenance-'+label+'-paused.png'),fullPage:true});
    await page.reload({waitUntil:'networkidle'});await expand();
    assert.match(await page.locator('#knowledge-gc').innerText(),/图清理：已暂停/);
    assert.equal(await page.locator('#pause-graph-gc').isVisible(),false);
    assert.equal(await page.locator('#resume-graph-gc').isVisible(),true);
    await page.locator('#resume-graph-gc').click();
    await page.locator('#knowledge-message').filter({hasText:'已继续自动清理'}).waitFor();
    await page.locator('#pause-graph-gc').waitFor({state:'visible'});
    assert.match(await page.locator('#knowledge-gc').innerText(),/图清理：分批检查中/);
    assert.deepEqual(await checkpoint(),before,'resume preserves scan cursor and projection queue');
    const bounds=await noOverflow(label+' expanded');
    await page.screenshot({path:path.join(folder,'maintenance-'+label+'-expanded.png'),fullPage:true});
    results[label]={viewport,collapsed_initially:true,separate_timing_labels:true,partial_not_acknowledged:true,
      bounded_topic_progress:true,pause_and_resume_clicked:true,pause_persisted_across_reload:true,
      cursor_and_projection_queue_preserved:true,no_horizontal_overflow:true,bounds};
  }
  assert.deepEqual(pageErrors,[],'browser JavaScript errors');
  assert.deepEqual(consoleErrors,[],'browser console errors');
  assert.deepEqual(failedRequests,[],'failed HTTP requests');
  const report={ok:true,browser:await browser.version(),viewports:results,page_errors:pageErrors,
    console_errors:consoleErrors,failed_requests:failedRequests};
  fs.writeFileSync(path.join(folder,'graph-maintenance-browser-results.json'),JSON.stringify(report,null,2)+'\n');
  await browser.close();browser=null;
})().catch(async err=>{console.error(err.stack||String(err));if(browser)await browser.close();process.exitCode=1;});

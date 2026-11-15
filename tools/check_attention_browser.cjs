/* Real Chromium; only check_attention_live.py's loopback synthetic app is used. */
const {chromium} = require('playwright-core');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
let browser;
(async () => {
 const base = process.env.N2D_QA_URL, folder = process.env.N2D_QA_OUTPUT;
 assert(base && folder && new URL(base).hostname === '127.0.0.1');
 let executablePath = process.env.N2D_QA_CHROMIUM, args = ['--no-sandbox', '--disable-dev-shm-usage'];
 if (!executablePath && process.env.N2D_QA_CHROMIUM_MODULE) {
  const module = await import(pathToFileURL(process.env.N2D_QA_CHROMIUM_MODULE).href);
  const runtime = module.default || module; executablePath = await runtime.executablePath(); args = runtime.args;
 }
 assert(executablePath, 'Set N2D_QA_CHROMIUM or N2D_QA_CHROMIUM_MODULE');
 browser = await chromium.launch({executablePath, args, headless: true});
 const page = await browser.newPage(), errors = [], failed = [], remote = [], results = {};
 page.on('pageerror', error => errors.push(error.message));
 page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
 page.on('requestfailed', request => { if (request.failure()?.errorText !== 'net::ERR_ABORTED') failed.push(request.url()); });
 await page.route('**/*', async route => {
  if (new URL(route.request().url()).hostname !== '127.0.0.1') { remote.push(route.request().url()); return route.abort(); }
  return route.continue();
 });
 const config = await (await page.request.get(base+'/qa/config')).json();
 assert(config.synthetic, 'QA fixture must be explicitly synthetic');
 const initialContext = await browser.newContext({javaScriptEnabled:false, viewport:{width:390,height:844}});
 const initialPage = await initialContext.newPage();
 await initialPage.goto(base+'/radar?source=attention&period=all');
 const initialWarnings = initialPage.locator('[data-attention-card="'+config.topic_key+'"] .attention-card-notes');
 assert(await initialWarnings.getByText('含缓存或重复快照，不视为新升温。', {exact:true}).isVisible());
 assert(await initialWarnings.getByText('部分来源失败，缺失观测为未知。', {exact:true}).isVisible());
 const initialBounds = await initialPage.evaluate(() => ({width:innerWidth,scroll:document.documentElement.scrollWidth}));
 assert(initialBounds.scroll <= initialBounds.width+3);
 results.initial_without_javascript = {cached_warning:true,failed_source_unknown:true,bounds:initialBounds};
 // Keep this context until final browser.close(): single-process Chromium
 // cannot reliably dispose one browser context while another is still open.
 async function noOverflow(label) {
  const bounds = await page.evaluate(() => ({width: innerWidth, scroll: document.documentElement.scrollWidth}));
  assert(bounds.scroll <= bounds.width+3, label+' overflow '+JSON.stringify(bounds)); return bounds;
 }
 async function openDetail() {
  await page.locator('[data-attention-card="'+config.topic_key+'"] [data-attention-open]').first().click();
  await page.locator('#attention-detail-dialog[open] .attention-source').first().waitFor();
  assert.equal(await page.locator('#attention-detail-title').textContent(), config.topic_title);
 }
 for (const [name, viewport] of Object.entries({desktop: {width:1440, height:1000}, mobile: {width:390, height:844}})) {
  await page.setViewportSize(viewport);
  const started = Date.now(); await page.goto(base+'/radar?source=attention&period=all');
  await page.locator('#attention-status').filter({hasText:'最近处理'}).waitFor();
  const card = page.locator('[data-attention-card="'+config.topic_key+'"]'); await card.waitFor();
  assert((await card.textContent()).includes('返回列表位置'));
  assert(await card.getByText('含缓存或重复快照，不视为新升温。', {exact:true}).isVisible());
  assert(await card.getByText('部分来源失败，缺失观测为未知。', {exact:true}).isVisible());
  assert.equal(await page.locator('#attention-list').getByText('特朗普与乌克兰总统讨论俄乌停火方案', {exact:true}).count(), 1);
  results[name] = {page_ready_ms:Date.now()-started, list_bounds:await noOverflow(name+' list')};
  await page.screenshot({path:path.join(folder,'attention-list-'+name+'.png'), fullPage:true});
  await openDetail();
  const detail = page.locator('#attention-detail-content'), text = await detail.textContent();
  assert(text.includes('本地关注观测时间线') && text.includes('各平台观测依据') && text.includes('GDELT 事件关联'));
  assert(text.includes('微博') && text.includes('抖音') && text.includes('来源失败'), text);
  assert(text.includes('缓存') && text.includes('未知') && text.includes('原文'));
  assert(await detail.locator('.attention-chart svg circle').count() >= 2, 'Missing historical observation scatter points');
  assert.equal(await detail.locator('.attention-chart svg polyline, .attention-chart svg path').count(), 0, 'Missing gaps were interpolated');
  const sourceLinks = detail.locator('.attention-source h4 a');
  assert(await sourceLinks.count() >= 3);
  assert.equal(await sourceLinks.first().getAttribute('target'), '_blank');
  results[name].detail_bounds = await noOverflow(name+' detail');
  results[name].chart_points = await detail.locator('.attention-chart svg circle').count();
  if (name === 'desktop') {
   const matches = detail.locator('.attention-match'); assert.equal(await matches.count(), 2);
   await matches.first().getByRole('button', {name:'确认关联', exact:true}).click();
   await detail.locator('.attention-match').first().getByText('已确认关联', {exact:true}).waitFor();
   await detail.locator('.attention-match').nth(1).getByRole('button', {name:'拒绝关联', exact:true}).click();
   await detail.getByText('已拒绝关联', {exact:true}).waitFor();
   results[name].manual_confirm_reject = true;
  } else {
   assert.equal(await detail.getByText('已确认关联', {exact:true}).count(), 1);
   assert.equal(await detail.getByText('已拒绝关联', {exact:true}).count(), 1);
   results[name].manual_decisions_persisted = true;
  }
  await page.screenshot({path:path.join(folder,'attention-detail-'+name+'.png'), fullPage:true});
  await page.locator('#attention-detail-close').click();
  await card.locator('[data-attention-select]').click();
  await page.locator('#attention-select-dialog[open]').waitFor();
  assert.equal(await page.locator('#attention-selection-title').inputValue(), config.topic_title);
  await page.locator('#attention-selection-angle').fill('QA合成议题：核查关税原文、历史背景及普通家庭影响（'+name+'）');
  await page.locator('#attention-select-submit').click(); await page.waitForURL(/\/workbench\/[A-Za-z0-9_-]+(?:\/selection)?$/);
  const workbenchText = await page.locator('main').textContent();
  assert(workbenchText.includes('关税') && workbenchText.includes('原文'));
  const key = page.url().match(/\/workbench\/([^/]+)/)[1];
  const task = await (await page.request.get(base+'/api/topics/'+key)).json();
  assert(task.sources.length > 0 && task.sources.every(row => row.requires_original_text));
  assert.equal(task.materials.can_write_from_sources, false);
  results[name].handoff = {url:new URL(page.url()).pathname, lead_count:task.sources.length, original_text_required:true, bounds:await noOverflow(name+' workbench')};
  await page.screenshot({path:path.join(folder,'attention-workbench-'+name+'.png'), fullPage:true});
 }
 await page.goto(base+'/radar?source=attention&period=all&order=recent&q='+encodeURIComponent('停火'));
 await page.locator('[data-attention-card]').first().waitFor();
 assert.equal(await page.locator('[data-attention-card]').count(), 1);
 assert((await page.locator('[data-attention-card]').textContent()).includes('停火'));
 await page.goto(base+'/radar?source=attention&period=all&order=rising&q='+encodeURIComponent('关税'));
 await page.locator('[data-attention-card]').first().waitFor();
 assert.equal(await page.locator('[data-attention-card]').count(), 1);
 results.filter = {query:true, rising_order:true, bounds:await noOverflow('mobile filter')};
 assert.equal((await page.request.post(base+'/qa/backfill-failure')).status(), 200);
 await page.reload();
 await page.locator('#attention-status').filter({hasText:'旧快照回填受阻'}).waitFor();
 await page.locator('#attention-status').getByRole('button', {name:'重新发现旧快照', exact:true}).click();
 await page.waitForFunction(() => !document.getElementById('attention-status').textContent.includes('旧快照回填受阻'));
 const recovery = await (await page.request.get(base+'/api/attention/status')).json();
 assert.deepEqual(recovery.backfill_error, []); assert.equal(recovery.backfill_cursor, '0');
 results.backfill_recovery = {visible_failure:true, explicit_rescan_cleared_error:true, bounds:await noOverflow('mobile backfill recovery')};
 assert.deepEqual(errors, []); assert.deepEqual(failed, []); assert.deepEqual(remote, []);
 const state = await (await page.request.get(base+'/qa/state')).json();
 assert.equal(state.prohibited_calls, 0); assert.equal(state.counts.Event, 0);
 const report = {ok:true, browser:browser.version(), results, state, page_errors:errors, failed_requests:failed, remote_requests:remote};
 fs.writeFileSync(path.join(folder,'attention-browser.json'), JSON.stringify(report,null,2)+'\n');
 console.log(JSON.stringify(report)); await browser.close();
})().catch(async error => { console.error(error); if (browser) await browser.close(); process.exit(1); });

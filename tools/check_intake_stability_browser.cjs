/* Real Chromium against the disposable synthetic intake app only. */
const {chromium}=require('playwright-core');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),{pathToFileURL}=require('node:url');
let browser;
(async()=>{
 const base=process.env.N2D_QA_URL,folder=process.env.N2D_QA_OUTPUT;
 assert(base&&folder&&new URL(base).hostname==='127.0.0.1');
 let executablePath=process.env.N2D_QA_CHROMIUM,args=['--no-sandbox','--disable-dev-shm-usage'];
 if(!executablePath&&process.env.N2D_QA_CHROMIUM_MODULE){const m=await import(pathToFileURL(process.env.N2D_QA_CHROMIUM_MODULE).href);const runtime=m.default||m;executablePath=await runtime.executablePath();args=runtime.args;}
 assert(executablePath,'Configure a QA Chromium executable');
 browser=await chromium.launch({executablePath,args,headless:true});
 const page=await browser.newPage(),errors=[],failed=[],remote=[],results={};
 page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
 page.on('requestfailed',r=>{if(r.failure()?.errorText!=='net::ERR_ABORTED')failed.push(r.url());});
 await page.route('**/*',route=>{if(new URL(route.request().url()).hostname!=='127.0.0.1'){remote.push(route.request().url());return route.abort();}return route.continue();});
 const config=await(await page.request.get(base+'/qa/config')).json();assert(config.synthetic);
 const state=async()=>await(await page.request.get(base+'/qa/state')).json();
 async function bounds(label){const b=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));assert(b.scroll<=b.width+3,label+' '+JSON.stringify(b));return b;}
 for(const [name,viewport] of Object.entries({desktop:{width:1440,height:1000},mobile:{width:390,height:844}})){
  await page.setViewportSize(viewport);await page.goto(base+'/intake');await page.locator('[data-intake-feed]').nth(5).waitFor();
  assert.equal(await page.locator('[data-intake-feed]').count(),6);
  assert((await page.locator('#intake-counters').textContent()).includes('未完成投影的批次'));
  assert((await page.locator('#intake-counts-note').textContent()).includes('历史成功 / 跳过分类未知'));
  assert((await page.locator('#intake-counters').textContent()).includes('137'));
  const before=await state();assert.equal(before.batch_offset,37);assert.equal(before.receiver_cursor,'20261114091500');
  if(name==='desktop')assert.equal(before.calls.raw_search,0,'Page load automatically searched the archive');
  const feed=page.locator('[data-intake-feed="events_english"]');
  assert((await feed.textContent()).includes('GDELT HTTP 404'));
  await feed.locator('summary').click();await feed.locator('.intake-diagnostics').first().waitFor();
  assert((await feed.textContent()).includes('2026-11-14 09:15 UTC'));
  assert((await feed.textContent()).includes('请求批次返回404，原因待核实'));
  assert((await feed.textContent()).includes('https://data.gdeltproject.org/gdeltv2/20261114091500.export.CSV.zip'));
  await feed.getByRole('button',{name:'检查官方源',exact:true}).click();
  await feed.getByText('QA合成检查完成：目标404、最新批次200；断点未移动。',{exact:true}).waitFor();
  const afterProbe=await state();assert.equal(afterProbe.receiver_cursor,before.receiver_cursor);assert.equal(afterProbe.batch_offset,37);
  results[name]={bounds:await bounds(name+' expanded source'),probe_cursor_unchanged:true};
  await page.screenshot({path:path.join(folder,'intake-expanded-'+name+'.png'),fullPage:true});
  const failure=page.locator('[data-intake-failure="'+config.batch_key+'"]');
  assert((await failure.textContent()).includes('未处理')||(await page.locator('#intake-failures-section').textContent()).includes('未处理材料'));
  assert((await failure.textContent()).includes('第 38–62 行，具体受阻位置待核'));
  await failure.getByRole('button',{name:'重试该批次',exact:true}).click();
  await failure.getByText('等待重试',{exact:true}).waitFor();
  const afterRetry=await state();assert.equal(afterRetry.retry_after,0);assert.equal(afterRetry.batch_offset,37);
  results[name].retry_without_skipping=true;
  if(name==='desktop'){
   await feed.getByRole('button',{name:'暂停接收',exact:true}).click();await feed.getByText('已暂停',{exact:true}).waitFor();assert.equal((await state()).receiver_cursor,before.receiver_cursor);
   await feed.getByRole('button',{name:'继续接收',exact:true}).click();await feed.getByText('已启用，自动接收关闭',{exact:true}).waitFor();
   results[name].pause_resume_preserved_cursor=true;
  }
  await page.screenshot({path:path.join(folder,'intake-status-'+name+'.png'),fullPage:true});
 }
 const searchPanel=page.locator('details').filter({has:page.locator('#intake-search')});await searchPanel.locator('summary').click();
 await page.locator('#intake-search button').click();try{await page.locator('.intake-empty').filter({hasText:'当前检索没有匹配的已存记录。'}).waitFor();}catch(e){console.error(JSON.stringify({state:await state(),note:await page.locator('#intake-results-note').textContent(),search_visible:await searchPanel.isVisible(),open:await searchPanel.getAttribute('open')}));throw e;}
 const after=await state();assert.equal(after.calls.raw_search,1);assert.equal(after.calls.forbidden,0);
 results.explicit_search_only=true;
 assert.deepEqual(errors,[]);assert.deepEqual(failed,[]);assert.deepEqual(remote,[]);
 fs.writeFileSync(path.join(folder,'intake-stability-browser.json'),JSON.stringify({ok:true,browser:browser.version(),results,page_errors:errors,failed_requests:failed,remote_requests:remote,state:after},null,2)+'\n');
 await browser.close();
})().catch(async e=>{console.error(e);if(browser)await browser.close();process.exit(1);});

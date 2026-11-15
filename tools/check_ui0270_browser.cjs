/* Companion fixture: check_ui0270_live.py. Screenshots are scratch QA only. */
const {chromium}=require('playwright-core');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
let browser;
(async()=>{
  const base=process.env.N2D_QA_URL,folder=process.env.N2D_QA_OUTPUT;
  assert(base&&folder);assert(['localhost','127.0.0.1'].includes(new URL(base).hostname));
  let executablePath=process.env.N2D_QA_CHROMIUM,args=['--no-sandbox','--disable-dev-shm-usage'];
  if(!executablePath&&process.env.N2D_QA_CHROMIUM_MODULE){
    const module=await import(pathToFileURL(process.env.N2D_QA_CHROMIUM_MODULE).href);
    const support=module.default||module;executablePath=await support.executablePath();args=support.args;
  }
  assert(executablePath,'Set N2D_QA_CHROMIUM or N2D_QA_CHROMIUM_MODULE');
  browser=await chromium.launch({executablePath,args,headless:true});
  const page=await browser.newPage();
  const errors=[],failed=[],countRequests=[],results={};
  page.on('pageerror',e=>errors.push(e.message));
  page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
  page.on('requestfailed',r=>failed.push(r.url()));
  page.on('request',r=>{if(new URL(r.url()).pathname==='/api/gdelt/events/count')countRequests.push(r.url());});
  async function noOverflow(label){const v=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));assert(v.scroll<=v.width+3,label+' overflow '+JSON.stringify(v));return v;}
  async function safeText(){assert.equal(await page.evaluate(()=>window.qaInjected),undefined);}
  const config=await (await page.request.get(base+'/qa/config')).json();
  for(const [name,viewport] of Object.entries({desktop:{width:1440,height:1000},mobile:{width:390,height:844}})){
    await page.setViewportSize(viewport);
    const before=countRequests.length;
    await page.goto(base+'/radar?source=gdelt&period=all&limit=2');
    await page.locator('[data-gdelt-event]').first().waitFor();
    assert.equal(await page.locator('[data-gdelt-event]').count(),2);
    assert.equal(countRequests.length,before,'Initial radar unexpectedly counts full index');
    assert((await page.locator('#gdelt-page-position').textContent()).includes('第 1 页'));
    assert((await page.locator('.gdelt-event-date time').first().textContent()).includes('2026-10-01'));
    await page.locator('#gdelt-count').click();
    await page.locator('#gdelt-total').filter({hasText:'共 3 条'}).waitFor();
    assert((await page.locator('#gdelt-total').textContent()).includes('2 页'));
    await page.getByRole('link',{name:'下一页 →',exact:true}).click();
    await page.waitForURL(/offset=2/);assert.equal(await page.locator('[data-gdelt-event]').count(),1);
    const returnUrl=page.url();
    await page.locator('[data-gdelt-sources="123456"] summary').click();
    await page.locator('#gdelt-detail-dialog[open]').waitFor();
    await page.locator('#gdelt-detail-content').getByRole('link',{name:'one.example ↗',exact:true}).waitFor();
    assert((await page.locator('#gdelt-detail-content').textContent()).includes('Events不提供报道原文语种'));
    await page.locator('#gdelt-translate').click();
    if(name==='desktop'){
      await page.locator('#gdelt-translation-status').filter({hasText:'离线测试模型暂不可用'}).waitFor();
      await page.getByRole('button',{name:'重试翻译',exact:true}).click();
    }
    await page.locator('#gdelt-translation-output').filter({hasText:'中文阅读译文'}).waitFor();
    await safeText();
    await page.getByRole('button',{name:'放大',exact:true}).click();
    await page.getByRole('button',{name:'缩小',exact:true}).click();
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#gdelt-detail-dialog').evaluate(el=>el.open),false);
    assert.equal(page.url(),returnUrl,'Detail close lost the current filter/page');
    await page.locator('select[name="cameo"]').selectOption('07');
    await page.locator('#gdelt-filter button[type="submit"]').click();
    await page.waitForURL(/cameo=07/);assert.equal(await page.locator('[data-gdelt-event]').count(),1);
    await page.locator('.gdelt-advanced summary').click();
    await page.locator('#gdelt-preset-name').fill('援助筛选-'+name);
    await page.locator('#gdelt-save-filter').click();
    await page.locator('#gdelt-message').filter({hasText:'筛选已保存'}).waitFor();
    const saved=await page.locator('select[name="preset"]').inputValue();assert(saved);
    assert((await page.locator('select[name="preset"]').textContent()).includes('援助筛选-'+name));
    await page.locator('select[name="cameo"]').selectOption('19');
    await page.locator('select[name="preset"]').selectOption(saved);
    await page.waitForURL(new RegExp('preset='+saved));
    assert.equal(await page.locator('select[name="cameo"]').inputValue(),'07');
    results[name]={viewport,pagination_total_on_demand:true,filter_and_page_preserved:true,translation_text_safe:true,
      translation_failure_retry:name==='desktop'?'passed':'not_exercised',relation_controls:true,preset_immediate:true,occurrence_date_visible:true,bounds:await noOverflow(name+' radar')};
    await page.screenshot({path:path.join(folder,'radar-'+name+'.png'),fullPage:true});
  }
  await page.locator('[data-gdelt-select="123456"]').click();
  await page.locator('#gdelt-topic-title').fill('UI整合测试选题');
  await page.locator('#gdelt-select-submit').click();
  await page.waitForURL(/\/workbench\/[^/]+\/selection/);
  const task=page.url().match(/\/workbench\/([^/]+)\//)[1];
  await page.locator('#topic-short').waitFor();assert.equal(await page.locator('#topic-short').isDisabled(),true);
  await page.locator('.work-context summary').click();
  await page.locator('[data-work-source]').first().click();
  await page.locator('#work-source-dialog[open]').waitFor();
  await page.locator('#work-source-content').filter({hasText:'GDELT'}).waitFor();
  await page.locator('#work-source-translate').click();
  await page.locator('#work-source-translated').filter({hasText:'中文阅读译文'}).waitFor();await safeText();
  await page.locator('#work-source-close').click();
  await page.goto(base+'/workbench/'+task+'/research');
  await page.locator('[data-work-handoff="materials"]').first().waitFor();
  await page.goto(base+'/radar?source=gdelt&period=all&cameo=07');
  assert.equal(await page.getByRole('link',{name:'继续这项选题 →',exact:true}).count(),1);
  results.workbench={snapshot_readable:true,metadata_only_short_disabled:true,translation_safe:true,research_next_step_present:true,selected_event_continues_task:true};
  await page.goto(base+'/articles/'+encodeURIComponent(config.article));
  await page.locator('[data-translation-start]').first().waitFor();
  assert((await page.locator('main').textContent()).includes('波兰语'));
  await page.locator('[data-translation-start]').first().click();
  await page.locator('[data-translation-text]').first().filter({hasText:'中文阅读译文'}).waitFor();await safeText();
  results.article={declared_language_visible:true,translation_preserves_original:true,bounds:await noOverflow('mobile article')};
  const watch=await (await page.request.post(base+'/api/focus',{data:{name:'UI可恢复专题',kind:'人物',scope:'特朗普'}})).json();
  await page.goto(base+'/focus/'+watch.key);
  await page.locator('#focus-delete').click();
  await page.locator('#focus-delete-dialog[open]').waitFor();
  await page.locator('#focus-delete-cancel').click();assert.equal(await page.locator('#focus-delete-dialog').evaluate(el=>el.open),false);
  await page.locator('#focus-delete').click();await page.locator('#focus-delete-confirm').click();
  await page.waitForURL(base+'/focus');
  await page.locator('#focus-trash summary').click();
  await page.locator('[data-focus-restore="'+watch.key+'"]').click();
  await page.waitForURL(new RegExp('/focus/'+watch.key));
  assert((await page.locator('main').textContent()).includes('已暂停'));
  assert((await page.request.get(base+'/workbench/'+task+'/selection')).ok());
  results.focus={cancel_delete:true,moved_to_trash:true,restored_paused:true,independent_task_preserved:true,bounds:await noOverflow('mobile focus')};
  const state=await (await page.request.get(base+'/qa/state')).json();
  assert.equal(state.counts.ContentTask,1);assert.equal(state.counts.Article,1);assert.equal(state.counts.Event,1);
  assert.deepEqual(errors,[]);assert.deepEqual(failed,[]);
  const report={ok:true,browser:browser.version(),results,state,page_errors:errors,failed_requests:failed};
  fs.writeFileSync(path.join(folder,'ui0270-browser-results.json'),JSON.stringify(report,null,2)+'\n');
  await browser.close();browser=null;
})().catch(async error=>{console.error(error);if(browser)await browser.close();process.exitCode=1;});

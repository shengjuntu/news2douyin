/* Uses check_newsnow_live.py's disposable app and local NewsNow fixture. */
const {chromium}=require('playwright-core');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
let browser;
(async()=>{
 const base=process.env.N2D_QA_URL,folder=process.env.N2D_QA_OUTPUT;
 assert(base&&folder&&new URL(base).hostname==='127.0.0.1');
 let executablePath=process.env.N2D_QA_CHROMIUM,args=['--no-sandbox','--disable-dev-shm-usage'];
 if(!executablePath&&process.env.N2D_QA_CHROMIUM_MODULE){const m=await import(pathToFileURL(process.env.N2D_QA_CHROMIUM_MODULE).href);const support=m.default||m;executablePath=await support.executablePath();args=support.args;}
 assert(executablePath,'Set N2D_QA_CHROMIUM or N2D_QA_CHROMIUM_MODULE');
 browser=await chromium.launch({executablePath,args,headless:true});
 const page=await browser.newPage(),errors=[],failed=[],results={};
 page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
 page.on('requestfailed',r=>{if(r.failure()?.errorText!=='net::ERR_ABORTED')failed.push(r.url());});
 const config=await(await page.request.get(base+'/qa/config')).json();
 async function state(){return(await page.request.get(base+'/qa/state')).json();}
 async function noOverflow(label){const x=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));assert(x.scroll<=x.width+3,label+' overflow '+JSON.stringify(x));return x;}
 async function waitTask(tid){for(let n=0;n<150;n++){const data=await(await page.request.get(base+'/api/tasks/'+tid)).json();if(['succeeded','failed','cancelled'].includes(data.status))return data;await new Promise(r=>setTimeout(r,100));}throw Error('Task did not reach terminal state');}
 if(config.guard_article){
  const key=encodeURIComponent(config.guard_article);
  for(const [name,viewport]of Object.entries({desktop:{width:1440,height:1000},mobile:{width:390,height:844}})){
   await page.setViewportSize(viewport);await page.goto(base+'/articles/'+key+'?revision=1');
   const reader=page.locator('#run-reader');assert(!await reader.count()||!await reader.isVisible()||await reader.isDisabled(),'Metadata revision exposed an active model reader');
   const denied=await page.request.post(base+'/api/desk/articles/'+key+'/1/model',{data:{}});assert.equal(denied.status(),422);
   const oldBounds=await noOverflow(name+' metadata guard');
   await page.screenshot({path:path.join(folder,'newsnow-guard-r1-'+name+'.png'),fullPage:true});
   await page.goto(base+'/articles/'+key+'?revision=2');await page.locator('#run-reader').waitFor();
   assert.equal(await page.locator('#run-reader').isEnabled(),true);
   await page.locator('#run-reader').click();await page.locator('#reader-message').filter({hasText:'已提交'}).waitFor();
   results[name]={fixed_metadata_revision_blocked:true,full_text_revision_allowed:true,metadata_bounds:oldBounds,full_text_bounds:await noOverflow(name+' full text guard')};
  }
  assert.deepEqual(errors,[]);assert.deepEqual(failed,[]);
  const report={ok:true,browser:browser.version(),results,state:await state(),page_errors:errors,failed_requests:failed};
  fs.writeFileSync(path.join(folder,'newsnow-article-guard-browser.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));await browser.close();return;
 }
 let collected=false,articleKey='';
 for(const [name,viewport]of Object.entries({desktop:{width:1440,height:1000},mobile:{width:390,height:844}})){
  await page.setViewportSize(viewport);await page.goto(base+'/profiles');
  await page.locator('#name').fill('NewsNow-browser-'+name);await page.locator('#provider').selectOption('newsnow');
  assert.equal(await page.locator('#newsnow-settings').isVisible(),true);
  assert.equal(await page.locator('#filter-mode').inputValue(),'rules');
  assert.equal(await page.locator('#country').inputValue(),'');assert.equal(await page.locator('#language').inputValue(),'');
  await page.locator('#newsnow-base-url').fill(config.newsnow_base);
  await page.locator('#newsnow-settings summary').click();await page.locator('#newsnow-select-none').click();
  for(const id of ['zhihu','cls','fastbull','weibo'])await page.locator('[data-newsnow-source="'+id+'"]').check();
  await page.locator('#save-profile').click();await page.locator('#profile-message').filter({hasText:'已保存'}).waitFor();
  assert.equal(await page.locator('#profile-select').inputValue(),'NewsNow-browser-'+name);
  assert.equal(await page.locator('#newsnow-base-url').inputValue(),config.newsnow_base);
  await page.screenshot({path:path.join(folder,'newsnow-profile-'+name+'.png'),fullPage:true});
  const before=await state();await page.locator('#test-profile').click();await page.waitForURL(/\/tasks\/[^/]+\/diagnostics$/);
  const tid=page.url().match(/\/tasks\/([^/]+)/)[1],task=await waitTask(tid);assert.equal(task.status,'succeeded');
  await page.reload();await page.locator('main').filter({hasText:'NewsNow'}).waitFor();
  const text=await page.locator('main').textContent();assert(text.includes('zhihu')&&text.includes('weibo')&&text.includes('fastbull'));
  const after=await state();assert.equal(after.counts.Article,before.counts.Article,'Dry run wrote Article');
  assert.equal(after.counts.Event,0);
  results[name]={saved_source_selection:true,default_rules:true,unknown_language_not_fabricated:true,
   source_diagnostics_visible:true,dry_run_no_article:true,bounds:await noOverflow(name+' diagnostics')};
  await page.screenshot({path:path.join(folder,'newsnow-diagnostics-'+name+'.png'),fullPage:true});
  if(!collected){
   const response=await page.request.post(base+'/api/tasks/collect',{data:{profile_name:'NewsNow-browser-'+name}});assert.equal(response.status(),202);
   const id=(await response.json()).task_id;assert.equal((await waitTask(id)).status,'succeeded');
   const next=await state();assert.equal(next.counts.Article,3);assert.equal(next.counts.Event,0);
   articleKey=next.articles.find(row=>row.content).key;collected=true;
  }
 }
 await page.goto(base+'/articles/'+encodeURIComponent(articleKey));await page.locator('main').filter({hasText:'NewsNow'}).waitFor();
 const articleText=await page.locator('main').textContent();assert(articleText.includes('摘要')&&articleText.includes('未知'));
 results.article={excerpt_labelled:true,unknown_publication_visible:true,bounds:await noOverflow('mobile article')};
 await page.screenshot({path:path.join(folder,'newsnow-article-mobile.png'),fullPage:true});
 const failedProfile=await page.request.post(base+'/api/profiles',{data:{name:'NewsNow-all-failed',provider:'newsnow',country:'',language:'',categories:[],keywords_include:[],max_items:1200,extra:{base_url:config.newsnow_base,newsnow_sources:['broken','html'],filter_mode:'rules'}}});
 assert.equal(failedProfile.status(),200);const response=await page.request.post(base+'/api/tasks/collect',{data:{profile_name:'NewsNow-all-failed'}});assert.equal(response.status(),202);
 const tid=(await response.json()).task_id;assert.equal((await waitTask(tid)).status,'failed');
 await page.goto(base+'/tasks/'+tid+'/diagnostics');const failText=await page.locator('main').textContent();assert(failText.includes('NewsNow')&&failText.includes('broken')&&failText.includes('html'));
 results.all_failed={explicit_failure:true,source_diagnostics_visible:true,bounds:await noOverflow('mobile failure')};
 assert.deepEqual(errors,[]);assert.deepEqual(failed,[]);
 const report={ok:true,browser:browser.version(),results,state:await state(),page_errors:errors,failed_requests:failed};
 fs.writeFileSync(path.join(folder,'newsnow-browser.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));
 await browser.close();
})().catch(async error=>{console.error(error);if(browser)await browser.close();process.exit(1);});

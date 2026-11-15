const {chromium}=require('playwright-core');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
let browser;
(async()=>{
  const base=process.env.N2D_QA_URL,folder=process.env.N2D_QA_OUTPUT;
  assert(['127.0.0.1','localhost'].includes(new URL(base).hostname));
  let executablePath=process.env.N2D_QA_CHROMIUM,args=['--no-sandbox','--disable-dev-shm-usage'];
  if(!executablePath&&process.env.N2D_QA_CHROMIUM_MODULE){const support=require(process.env.N2D_QA_CHROMIUM_MODULE);executablePath=await(support.default||support).executablePath();args=(support.default||support).args;}
  assert(executablePath);browser=await chromium.launch({executablePath,args,headless:true});
  const page=await browser.newPage(),errors=[],payloads=[],results={};page.on('pageerror',error=>errors.push(error.message));
  let simulateError=false;
  await page.route('**/api/translations',async route=>{
    payloads.push(route.request().postDataJSON());
    if(simulateError){simulateError=false;return route.fulfill({status:503,json:{detail:'翻译模型暂不可用'}});}
    return route.fulfill({json:{id:'translation_fixture',status:'queued'}});
  });
  await page.route('**/api/translations/translation_fixture',route=>route.fulfill({json:{id:'translation_fixture',status:'succeeded',original:'Ancien titre',translated_text:'旧标题 <img src=x onerror=alert(1)>',revision:1,truncated:false}}));
  for(const[name,viewport]of Object.entries({desktop:{width:1440,height:1000},mobile:{width:390,height:844}})){
    await page.setViewportSize(viewport);await page.goto(base+'/articles');
    assert.equal(await page.locator('[data-translation]').count(),2);assert.equal(payloads.length,name==='desktop'?0:1);
    assert.equal(await page.locator('.article-advanced-filters').getAttribute('open'),null);
    const host=page.locator('[data-translation-key="lang-article"]');await host.getByRole('button',{name:'译标题',exact:true}).click();
    await host.getByText('翻译完成',{exact:true}).waitFor();assert.equal(payloads.at(-1).scope,'title');
    assert.equal(await host.locator('[data-translation-text] img').count(),0);assert((await host.locator('[data-translation-text]').textContent()).includes('<img'));
    const bounds=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));assert(bounds.scroll<=bounds.width+3,name+' overflow '+JSON.stringify(bounds));
    results[name]={viewport,bounds,advanced_collapsed:true,title_translation:true,no_translation_html_execution:true};
  }
  await page.goto(base+'/articles/lang-article?revision=1');
  assert((await page.locator('main').textContent()).includes('法语 · 来源字段'));assert((await page.locator('main').textContent()).includes('Une ancienne version vérifiable.'));
  simulateError=true;await page.getByRole('button',{name:'翻译为中文',exact:true}).click();await page.getByText('翻译模型暂不可用',{exact:false}).waitFor();
  assert.equal(await page.getByRole('link',{name:'检查模型设置'}).count(),1);
  await page.getByRole('button',{name:'重试翻译',exact:true}).click();await page.getByText('翻译完成',{exact:true}).waitFor();
  assert.equal(payloads.at(-1).revision,1);assert.equal(payloads.at(-1).scope,'document');assert.equal(payloads.at(-1).retry,true);
  assert.equal(await page.locator('[data-translation-text] img').count(),0);
  assert((await page.locator('.article-detail-body').textContent()).includes('Une ancienne version vérifiable.'));
  const bounds=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));assert(bounds.scroll<=bounds.width+3,'detail overflow '+JSON.stringify(bounds));
  results.selected_revision={revision:1,correct_frozen_language:true,original_retained:true,error_and_retry:true,bounds};assert.deepEqual(errors,[]);
  const report={ok:true,browser:browser.version(),real_http:true,translation_api:'mocked queued/succeeded/failure; no model or external requests',results,page_errors:errors};
  fs.writeFileSync(path.join(folder,'article-translation-browser-results.json'),JSON.stringify(report,null,2)+'\n');await browser.close();browser=null;
})().catch(async error=>{console.error(error);if(browser)await browser.close();process.exit(1);});

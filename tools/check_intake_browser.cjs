const {chromium}=require('playwright-core');const assert=require('node:assert/strict');
(async()=>{
 const base=process.env.N2D_QA_URL,out=process.env.N2D_QA_OUTPUT;
 const browser=await chromium.launch({executablePath:process.env.N2D_QA_CHROMIUM,headless:true,args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--disable-software-rasterizer']});
 const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 let status;
 for(let i=0;i<250;i++){
  const r=await page.request.get(base+'/api/intake/status');assert(r.ok());status=await r.json();
  if(status.counters.rows===307&&status.projection.waiting===0)break;
  await page.waitForTimeout(200);
 }
 assert.equal(status.counters.rows,307);assert.equal(status.projection.projected,306);assert.equal(status.projection.skipped,1);assert.equal(status.projection.waiting,0);assert(status.receiver.running);
 const cursor=status.feeds.find(f=>f.key==='gkg_english').next_batch;
 await page.goto(base+'/intake');await page.locator('#intake-records .intake-record').first().waitFor();
 assert.match(await page.locator('#intake-counters').innerText(),/307/);
 const feed=page.locator('[data-intake-feed=gkg_english]');await feed.getByRole('button',{name:'暂停接收',exact:true}).click();await feed.getByRole('button',{name:'继续接收',exact:true}).waitFor();
 let check=await (await page.request.get(base+'/api/intake/status')).json();assert.equal(check.feeds.find(f=>f.key==='gkg_english').next_batch,cursor);
 await feed.getByRole('button',{name:'继续接收',exact:true}).click();await feed.getByRole('button',{name:'暂停接收',exact:true}).waitFor();
 await page.locator('#intake-query').fill('english/17');await page.getByRole('button',{name:'查询接收库'}).click();await page.waitForFunction(()=>document.querySelectorAll('#intake-records .intake-record').length===11);
 await page.locator('#intake-records .intake-record').first().getByRole('button',{name:'查看原始记录'}).click();await page.locator('#intake-detail[open]').waitFor();await page.waitForFunction(()=>document.querySelector('#intake-detail-content').textContent.includes('结构化元数据'));
 assert.match(await page.locator('#intake-detail-content').innerText(),/不含原始报道全文|没有|不含/);await page.keyboard.press('Escape');
 check=await (await page.request.get(base+'/api/intake/status')).json();assert.equal(check.counters.rows,307);assert.equal(check.feeds.find(f=>f.key==='gkg_english').next_batch,cursor);
 await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:out+'/intake.png',fullPage:true});
 const auth={Authorization:'Bearer qa-only-token-not-production'},document={provider:'industry.fixture',record_id:'one',uri:'https://industry.example/a',title:'产业网站演示材料',content:'本条为验证用虚构正文。',observed_at:'2026-11-04T10:30:00Z',metadata:{fixture:true}};
 const imported=await page.request.post(base+'/api/intake/documents',{headers:auth,data:document});assert(imported.ok(),await imported.text());const receipt=await imported.json();
 const repeat=await page.request.post(base+'/api/intake/documents',{headers:auth,data:document});assert((await repeat.json()).reused);
 await page.goto(base+'/articles/'+receipt.article_key);assert.match(await page.locator('main').innerText(),/本条为验证用虚构正文/);
 await page.goto(base+'/profiles');assert.equal(await page.locator('#provider').inputValue(),'gkg');assert.equal(await page.locator('#search-query').getAttribute('required'),null);
 let graphs;
 for(let i=0;i<250;i++){graphs=await (await page.request.get(base+'/api/knowledge/graph')).json();if(graphs.nodes.length)break;await page.waitForTimeout(200);}
 assert(graphs.nodes.length>0,'intake projection reaches graph analysis');
 await page.setViewportSize({width:390,height:844});await page.goto(base+'/intake');await page.locator('#intake-counters strong').first().waitFor();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+3),false);await page.screenshot({path:out+'/intake-mobile.png',fullPage:true});
 assert.deepEqual(errors,[]);console.log(JSON.stringify({ok:true,raw_rows:307,projected:306,skipped:1,checks:['two_feeds','over_250','real_background_workers','pause_resume_cursor','post_retention_search','raw_detail','external_api_idempotence','full_body','default_gkg','graph_pipeline','mobile'],pageErrors:errors}));await browser.close();
})().catch(e=>{console.error(e);process.exitCode=1});

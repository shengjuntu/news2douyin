const {chromium}=require(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES+'/playwright');
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const [base,out]=process.argv.slice(2),checks=[],errors=[];
 const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN,args:['--no-sandbox']});
 try{
 const context=await browser.newContext({viewport:{width:1510,height:1050}});
 await context.request.post(base+'/login',{data:{token:process.env.VIDEO_APP_TEST_TOKEN}});
 const page=await context.newPage();page.on('pageerror',e=>errors.push(String(e)));
 await page.goto(base);await page.locator('#app').waitFor();await page.locator('#environment').click();
 await page.waitForFunction(()=>document.querySelector('#env-info').textContent.includes('video-project'));
 assert.match(await page.locator('#env-info').textContent(),/实例/);await page.screenshot({path:path.join(out,'video-configuration.png'),fullPage:true});await page.locator('#close-env').click();checks.push('video_configuration_visible');
 const pid=await page.locator('#projects').inputValue();
 let block=true,first=true;const messages=[];
 await page.route('**/api/projects/*/agent',async route=>{if(block)await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'test transport blocked'})});else await route.continue();});
 await page.route('**/api/projects/*/messages',async route=>{
   messages.push(route.request().postDataJSON());
   if(first){first=false;await route.abort('failed');}else {const reply=await route.fetch();await route.fulfill({response:reply});}
 });
 await page.locator('#prompt').fill('审批：浏览器刷新应保留原指令');await page.locator('#send').click();
 await page.waitForFunction(()=>document.querySelector('#send-state').textContent.includes('待确认'));
 const before=await page.evaluate(pid=>JSON.parse(sessionStorage.getItem('video-message:'+pid)),pid);assert.ok(before.requestId);
 await page.reload();await page.locator('#app').waitFor();
 await page.waitForFunction(()=>document.querySelector('#prompt').value.includes('刷新'));
 const after=await page.evaluate(pid=>JSON.parse(sessionStorage.getItem('video-message:'+pid)),pid);assert.deepEqual(after,before);
 await page.screenshot({path:path.join(out,'video-pending-reload.png'),fullPage:true});checks.push('video_browser_reload_keeps_original_request');
 await page.locator('#send').click();await page.waitForFunction(pid=>sessionStorage.getItem('video-message:'+pid)===null,pid);
 assert.equal(messages.length,2);assert.deepEqual(messages[0],messages[1]);checks.push('video_browser_retry_keeps_original_turn_input');
 block=false;await page.waitForFunction(()=>document.querySelector('#agent-status').textContent==='等待授权',{},{timeout:20000});
 await page.locator('#agent-stop').click();await page.waitForFunction(()=>document.querySelector('#agent-stop').hidden,{},{timeout:20000});checks.push('video_browser_fenced_stop');
 assert.deepEqual(errors,[]);checks.push('video_no_javascript_errors');console.log(JSON.stringify({passed:checks.length,checks}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

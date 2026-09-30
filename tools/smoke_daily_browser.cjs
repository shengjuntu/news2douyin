// Start: python tools/smoke_daily.py --port 18191
const {chromium}=require(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES?process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES+'/playwright':'playwright');
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const base=process.argv[2]||'http://127.0.0.1:18191',out=process.argv[3]||'browser-results';await fs.mkdir(out,{recursive:true});
 const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN||undefined,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1050}});const errors=[],checks=[];page.on('pageerror',e=>errors.push(String(e)));
 try{
  await page.goto(base+'/');await page.getByRole('heading',{name:'每日选题',exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:'采集所选日期',exact:true}).isDisabled(),true);checks.push('new_install_guidance');
  await page.getByRole('link',{name:'管理采集策略',exact:true}).click();
  await page.locator('#name').fill('daily_demo');await page.locator('#provider').selectOption('mock');await page.locator('#categories').fill('');
  await page.locator('summary').click();await page.locator('#extra').fill('{"request_timeout_sec":25}');await page.getByRole('button',{name:'保存策略',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#profile-message').textContent.includes('已保存'));
  await page.getByRole('button',{name:'复制为新策略',exact:true}).click();await page.locator('#name').fill('daily_demo_copy');await page.getByRole('button',{name:'保存策略',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#profile-message').textContent.includes('已保存'));
  const profiles=await (await page.request.get(base+'/api/profiles')).json();assert.equal(profiles.length,2);assert.equal(profiles[1].extra.request_timeout_sec,25);checks.push('profile_create_copy_and_preserve_extra');
  await page.screenshot({path:path.join(out,'profiles.png'),fullPage:true});
  await page.goto(base+'/');await page.getByRole('button',{name:'采集所选日期',exact:true}).click();
  await page.locator('#choose-result').waitFor({state:'visible',timeout:20000});await page.locator('#choose-result').click();
  assert.equal(await page.locator('.choose-news').count(),3);checks.push('collect_to_candidates');
  await page.locator('.choose-news').first().check();await page.waitForFunction(()=>document.querySelector('#pick-count').textContent==='1');
  await page.reload();assert.equal(await page.locator('.choose-news').first().isChecked(),true);checks.push('selection_persists_reload');
  await page.screenshot({path:path.join(out,'daily-selection.png'),fullPage:true});
  const key=await page.locator('.choose-news').first().getAttribute('data-key');
  await page.getByRole('button',{name:'生成所选脚本',exact:true}).click();await page.waitForURL('**/tasks/*');await page.waitForFunction(()=>document.querySelector('#status').textContent==='已完成');await page.locator('#script-result').click();await page.waitForURL('**/scripts/pkg_*');
  await page.waitForFunction(()=>document.querySelector('#script-text').value.length>0);assert.equal(await page.locator('.source-key').count(),1);assert.equal(await page.locator('.source-key').getAttribute('value'),key);checks.push('generate_source_bound_script');
  assert.ok((await page.locator('#visual-notes').inputValue()).length>0);await page.screenshot({path:path.join(out,'generated-script.png'),fullPage:true});
  await page.goto(base+'/scripts');assert.ok((await page.locator('main').innerText()).includes('离线示例'));checks.push('script_list_uses_title');
  await page.goto(base+'/articles');await page.locator('[name=period]').selectOption('today');await page.locator('[name=limit]').fill('1');await page.getByRole('button',{name:'搜索',exact:true}).click();
  assert.equal(await page.locator('table tr').count(),2);await page.getByRole('link',{name:'下一页',exact:true}).click();assert.match(page.url(),/period=today/);checks.push('date_filter_pagination');
  await page.screenshot({path:path.join(out,'article-dates.png'),fullPage:true});
  await page.goto(base+'/');await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);await page.screenshot({path:path.join(out,'daily-mobile.png'),fullPage:true});checks.push('mobile_no_horizontal_overflow');
  assert.deepEqual(errors,[]);checks.push('no_javascript_errors');
  const result={passed:checks.length,checks,browser:browser.version(),errors};await fs.writeFile(path.join(out,'browser-report.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

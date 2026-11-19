/* Real-browser checks. The companion runner supplies isolated fixture data. */
const {chromium} = require('playwright-core');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let activeBrowser;

(async () => {
  const base = process.env.N2D_QA_URL;
  const folder = process.env.N2D_QA_OUTPUT;
  assert(base && folder, 'N2D_QA_URL and N2D_QA_OUTPUT are required');
  const browser = await chromium.launch({executablePath: process.env.N2D_QA_CHROMIUM,
    args: process.env.N2D_QA_CHROMIUM_ARGS ? JSON.parse(process.env.N2D_QA_CHROMIUM_ARGS) :
      ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--disable-software-rasterizer'], headless: true});
  activeBrowser = browser;
  const page = await browser.newPage({viewport: {width: 1440, height: 1050}});
  const errors = [], consoleErrors = [], results = {};
  page.setDefaultTimeout(25000);
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !/Failed to load resource.*503/.test(message.text())) consoleErrors.push(message.text());
  });
  async function until(check, name, timeout = 35000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const value = await check();
      if (value) return value;
      await page.waitForTimeout(250);
    }
    throw new Error('Timed out: ' + name);
  }
  async function state(key) {
    const response = await page.request.get(base + '/qa/focus-state/' + key);
    assert.equal(response.status(), 200);
    return response.json();
  }
  async function noOverflow(label) {
    const data = await page.evaluate(() => ({width: innerWidth, scroll: document.documentElement.scrollWidth}));
    assert(data.scroll <= data.width + 3, 'horizontal overflow ' + label + ': ' + JSON.stringify(data));
  }
  async function build(text) {
    await page.locator('#topic-description').fill(text);
    await page.locator('#topic-generate').click();
    assert.equal(await page.locator('#topic-generate').isDisabled(), true);
    await page.locator('#topic-preview').waitFor({state: 'visible'});
  }
  const description = process.env.N2D_QA_DESCRIPTION || '持续跟踪特朗普政府对乌克兰的军援和停火谈判，少看私人生活，了解政策的历史变化。';
  await page.goto(base + '/focus?new=1');
  assert.equal(await page.locator('main h1').textContent(), '焦点');
  assert.equal(await page.locator('.focus-new-link').getAttribute('href'), '/focus?new=1');
  assert.equal(await page.locator('#desk-editor').getAttribute('open'), null, 'manual form is collapsed');
  assert.equal(await page.locator('[data-topic-example]').count(), 4);
  assert.equal(await page.locator('#topic-description').getAttribute('maxlength'), '4000');
  await noOverflow('desktop new topic');
  await page.screenshot({path: path.join(folder, 'focus-new-desktop.png'), fullPage: true});

  // Editing an in-flight description must invalidate the stale preview.
  await page.locator('#topic-description').fill(description);
  await page.locator('#topic-generate').click();
  await page.locator('#topic-description').fill(description + ' 补充：同时关注官方更正。');
  await page.waitForTimeout(2000);
  assert.equal(await page.locator('#topic-preview').isVisible(), false);
  assert.equal(await page.locator('#topic-generate').isEnabled(), true);
  results.stalePreviewPrevented = true;

  // A failed submission preserves the description and offers a retry.
  await page.route('**/api/focus/interpret', route => route.fulfill({status: 503,
    contentType: 'application/json', body: '{"detail":"演示服务暂不可用，请重试"}'}));
  await page.locator('#topic-generate').click();
  await page.locator('#topic-build-status').filter({hasText: '演示服务暂不可用'}).waitFor();
  assert.match(await page.locator('#topic-description').inputValue(), /官方更正/);
  assert.equal(await page.locator('#topic-generate').isEnabled(), true);
  await page.unroute('**/api/focus/interpret');
  results.failedRequestPreservesDescription = true;

  const began = Date.now();
  await build(description);
  results.interpretMilliseconds = Date.now() - began;
  assert.equal(await page.locator('#topic-method').innerText(), 'AI 生成');
  await page.locator('#topic-name').fill('援助政策跟踪（浏览器验证）');
  await page.locator('#topic-questions').fill('军援政策发生了哪些实质变化？\n这些变化有怎样的历史背景？\n政策变化会如何影响后续谈判？');
  await page.locator('.focus-preview-detail > summary').click();
  const previewGraph = page.locator('[data-intent-graph="preview"]');
  await until(() => previewGraph.evaluate(host => !!host._focusCy && host._focusCy.nodes().length === 4), 'preview graph');
  const zoom = await previewGraph.evaluate(host => host._focusCy.zoom());
  await previewGraph.getByRole('button', {name: '放大关注图'}).click();
  assert((await previewGraph.evaluate(host => host._focusCy.zoom())) > zoom);
  await previewGraph.evaluate(host => host._focusCy.nodes().filter(n => n.id() !== '__focus__').first().emit('tap'));
  assert.match(await previewGraph.locator('.focus-intent-selection').innerText(), /别名/);
  await page.screenshot({path: path.join(folder, 'focus-preview-desktop.png'), fullPage: true});
  await page.locator('#topic-start').click();
  await page.waitForURL(/\/focus\/w_[0-9a-f]{32}$/);
  const focus = new URL(page.url()).pathname.split('/')[2];
  const settled = await until(async () => {
    const value = await state(focus), counts = value.tracking.counts;
    return value.tracking.backfill.status === 'complete' && counts.core === 1 && counts.excluded === 1 && counts.pending === 1 ? value : null;
  }, 'history routing and model judgments');
  assert.equal(settled.links.filter(row => row.choice !== 'exclude').length, 1);
  results.decisions = settled.tracking.counts;
  await until(async () => {
    const value = await state(focus);
    return value.graph.InterestScope === 1 && value.graph.InterestPattern >= 1 ? value : null;
  }, 'real FalkorDB interest graph');
  await page.reload();
  assert.match(await page.locator('.focus-topic-head h2').innerText(), /浏览器验证/);
  assert.equal(await page.locator('.focus-materials [data-hide]').count(), 1);
  const decision = page.locator('[data-focus-decision="focusqa_event_core"]');
  const evidence = decision.locator('[data-decision-body]');
  assert.equal(await evidence.isVisible(), false);
  assert.equal(await page.locator('.focus-materials .desk-pane > .desk-meta').count(), 1);
  await decision.locator('summary').click();
  await evidence.getByText('模型判断 · 核心进展', {exact: true}).waitFor();
  assert.match(await evidence.innerText(), /QA_CORE/);
  assert.match(await evidence.locator('a').first().getAttribute('href'), /revision=1/);
  results.fixedVersionEvidenceVisible = true;
  await page.locator('#tracking-pending-box > summary').click();
  await page.locator('#tracking-pending-list').getByText('演示材料：信息不足的援助线索').waitFor();
  await page.screenshot({path: path.join(folder, 'focus-following-desktop.png'), fullPage: true});
  results.realFalkorInterestGraph = true;

  // Manual exclusion survives subsequent model retries and range edits.
  await page.locator('[data-hide="focusqa_event_core"]').click();
  await page.waitForLoadState('domcontentloaded');
  await until(async () => (await state(focus)).links.some(row => row.event === 'focusqa_event_core' && row.choice === 'exclude'), 'manual exclusion');
  await page.locator('#tracking-pending-box > summary').click();
  await page.locator('#tracking-retry').click();
  await page.locator('#focus-msg').filter({hasText: '已提交后台重试'}).waitFor();
  assert((await state(focus)).links.some(row => row.event === 'focusqa_event_core' && row.choice === 'exclude'));
  results.manualExclusionPreserved = true;

  // Description editing stays inside the existing topic and preserves identity.
  await page.locator('details.focus-settings > summary').click();
  await build(description + ' 同时注意对已有报道的否认与更正。');
  await page.locator('#topic-name').fill('援助政策跟踪（范围更新）');
  await page.locator('#topic-start').click();
  await page.waitForURL(base + '/focus/' + focus);
  await page.locator('.focus-topic-head h2').filter({hasText: '范围更新'}).waitFor();
  assert((await state(focus)).links.some(row => row.event === 'focusqa_event_core' && row.choice === 'exclude'));
  results.definitionEditedInPlace = true;

  // The existing keyword workflow remains available as a secondary path.
  await page.goto(base + '/focus?new=1');
  await page.locator('#desk-editor > summary').click();
  await page.locator('#focus-form input[name="name"]').fill('手动焦点浏览器验证');
  await page.locator('#focus-form input[name="scope"]').fill('特朗普');
  await page.locator('#manual-save').click();
  await page.waitForURL(/\/focus\/w_[0-9a-f]{32}$/);
  const manual = new URL(page.url()).pathname.split('/')[2];
  assert.notEqual(manual, focus);
  await until(async () => (await state(manual)).tracking.counts.core + (await state(manual)).tracking.counts.background >= 3, 'manual history backfill');
  results.legacyManualCreation = true;

  // An unavailable model yields explicitly marked rule drafts and pending data.
  await page.request.post(base + '/qa/focus-model/unavailable');
  await page.goto(base + '/focus?new=1');
  await build('QA_OFFLINE 持续跟踪特朗普对乌克兰的军援和谈判。');
  assert.equal(await page.locator('#topic-method').innerText(), '规则生成');
  assert.match(await page.locator('#topic-method-message').innerText(), /模型暂不可用|基础范围/);
  await page.locator('#topic-name').fill('模型离线待核验证');
  await page.locator('#topic-start').click();
  await page.waitForURL(/\/focus\/w_[0-9a-f]{32}$/);
  const offline = new URL(page.url()).pathname.split('/')[2];
  const pending = await until(async () => {
    const value = await state(offline);
    return value.tracking.backfill.status === 'complete' && value.tracking.counts.pending >= 3 ? value : null;
  }, 'offline candidates retained');
  assert.equal(pending.tracking.counts.core, 0);
  assert.equal(pending.tracking.counts.background, 0);
  assert.equal(pending.links.filter(row => row.choice !== 'exclude').length, 0);
  await page.reload();
  await page.locator('#tracking-pending-box > summary').click();
  assert.match(await page.locator('#tracking-pending-summary').innerText(), /3 项/);
  await page.screenshot({path: path.join(folder, 'focus-offline-desktop.png'), fullPage: true});
  results.offlinePending = pending.tracking.counts;
  await page.request.post(base + '/qa/focus-model/normal');

  await page.setViewportSize({width: 390, height: 844});
  for (const target of ['/focus?new=1', '/focus/' + focus, '/focus/' + offline]) {
    await page.goto(base + target);
    await page.locator('main').waitFor();
    await noOverflow('mobile ' + target);
  }
  await page.goto(base + '/focus/' + focus);
  await page.locator('details.focus-settings > summary').click();
  await page.getByText('当前关注图与问题', {exact: true}).click();
  await page.locator('[data-intent-graph="saved"] canvas').first().waitFor();
  await noOverflow('mobile expanded graph');
  await page.screenshot({path: path.join(folder, 'focus-mobile-expanded.png'), fullPage: true});
  results.mobileNoOverflow = true;
  assert.deepEqual(errors, []);
  assert.deepEqual(consoleErrors, []);
  results.ok = true;
  results.pageErrors = errors;
  results.consoleErrors = consoleErrors;
  results.fixtureNewsAndModel = true;
  fs.writeFileSync(path.join(folder, 'focus-browser-results.json'), JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results));
  await browser.close();
})().catch(async error => { console.error(error); if (activeBrowser) await activeBrowser.close(); process.exitCode = 1; });

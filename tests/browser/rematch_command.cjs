// Fixture-only command routing: never opens real sessions or calls a model.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../..');
const clone = value => JSON.parse(JSON.stringify(value));
const request = { company: { name: '命令路由测试企业' }, documents: [
  { document_id: 'd1', filename: '企业资料.md', content: '测试企业资料。' },
] };
const session = { id: 's1', version: 3, input_revision: 1, request, effective_matches: [],
  runs: [], product_attempts: [], stale: false,
  messages: [{ role: 'assistant', content: '旧轮次', run_id: 'old-run' }],
  run: { id: 'old-run', result: { summary: { evaluated_count: 2, failed_count: 0 } } } };
const history = { id: 'old-run', status: 'COMPLETED', created_at: '2026-10-27T01:00:00Z',
  snapshot: { request: clone(request), knowledge: {} }, effective_matches: [], reviews: [], attempts: [], can_continue: true };
const originalHistory = clone(history);
const matches = [], chats = [], writes = [];
let failNext = false, partialNext = false, holdNext = false, releaseMatch;

(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 1536, height: 960 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://fixture.test/**', async route => {
      const pathname = new URL(route.request().url()).pathname;
      const files = { '/': 'chat.html', '/assets/chat.js': 'chat.js', '/assets/chat.css': 'chat.css' };
      if (files[pathname]) return route.fulfill({ contentType: pathname.endsWith('.js') ? 'text/javascript' :
        pathname.endsWith('.css') ? 'text/css' : 'text/html', body: fs.readFileSync(path.join(root, 'app/static', files[pathname]), 'utf8') });
      if (pathname.endsWith('favicon.svg')) return route.fulfill({ status: 204, body: '' });
      const json = value => route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) });
      if (pathname.endsWith('/match/stream')) {
        const payload = route.request().postDataJSON();
        assert.equal(payload.expected_version, session.version);
        matches.push(payload);
        if (holdNext) { holdNext = false; await new Promise(resolve => { releaseMatch = resolve; }); }
        session.version += 3;
        if (failNext) {
          failNext = false;
          return route.fulfill({ contentType: 'text/event-stream', body: 'event: error\ndata: {"error":{"message":"模拟匹配中断"}}\n\n' });
        }
        const count = payload.product_ids.length || 2;
        session.run = { id: `run-${matches.length}`, result: { summary: {
          evaluated_count: count - Number(partialNext), failed_count: Number(partialNext),
        } } };
        partialNext = false;
        session.messages.push({ role: 'assistant', content: '匹配完成。', run_id: session.run.id });
        return route.fulfill({ contentType: 'text/event-stream', body: `event: result\ndata: ${JSON.stringify(session)}\n\n` });
      }
      if (pathname.endsWith('/messages')) {
        const payload = route.request().postDataJSON();
        chats.push(payload);
        session.version += 3;
        session.messages.push({ role: 'user', content: payload.message }, { role: 'assistant', content: '这是解释，不执行匹配。' });
        return json(session);
      }
      if (route.request().method() === 'POST') {
        writes.push(pathname);
        throw new Error(`Unexpected mutation: ${pathname}`);
      }
      const responses = { '/health': {}, '/api/v1/financial-products': { products: [
        { product_id: 'p1', product_name: '产品一', application_conditions: '条件一' },
        { product_id: 'p2', product_name: '产品二', application_conditions: '条件二' },
      ] }, '/api/v1/knowledge': { items: [] }, '/api/v1/sessions': { sessions: [{ id: 's1', company: request.company }] },
      '/api/v1/sessions/s1': session, '/api/v1/sessions/s1/runs/old-run': history };
      assert.ok(pathname in responses, `Unexpected read: ${pathname}`);
      return json(responses[pathname]);
    });
    const idle = () => page.waitForFunction(() => document.body.getAttribute('aria-busy') === 'false');
    const send = async text => { await page.locator('#chat-input').fill(text); await page.locator('#chat-send').click(); await idle(); };
    await page.goto('http://fixture.test/'); await idle();
    await page.locator('#sessions .session-open').click(); await idle();
    await page.locator('#composer-product').selectOption('p1');
    for (const command of ['重新匹配', '开始匹配', '请重新匹配', '帮我重新匹配一下。']) {
      await send(command);
      assert.deepEqual(matches.at(-1).product_ids, ['p1']);
      assert.equal(await page.locator('#chat-input').inputValue(), '');
    }
    assert.equal(matches.length, 4);
    assert.equal(chats.length, 0, 'Explicit commands must bypass the chat context budget');
    await send('重新匹配全部产品');
    assert.deepEqual(matches.at(-1).product_ids, []);
    assert.equal(await page.locator('#composer-product').inputValue(), 'p1');
    await page.locator('#compose-fact').click();
    await page.locator('#chat-verified').check();
    await send('再匹配一次');
    assert.equal(chats.length, 0);
    assert.deepEqual(writes, [], 'A command in fact mode must never create a document or supplement');
    assert.deepEqual(session.request, request);
    assert.equal(await page.locator('#chat-verified').isChecked(), true, 'Other draft preferences are preserved');

    failNext = true;
    await send('重新匹配');
    assert.match(await page.locator('#notice').innerText(), /模拟匹配中断/);
    assert.equal(await page.locator('#chat-input').inputValue(), '重新匹配');
    await page.locator('#chat-send').click(); await idle();
    assert.equal(await page.locator('#chat-input').inputValue(), '');
    partialNext = true;
    await send('重新匹配');
    assert.equal(await page.locator('#chat-input').inputValue(), '重新匹配', 'Partial failures retain the command for retry');
    await page.locator('#chat-send').click(); await idle();

    holdNext = true;
    await page.locator('#chat-input').fill('重新匹配');
    await page.locator('#chat-send').click();
    await page.waitForFunction(() => document.body.getAttribute('aria-busy') === 'true');
    assert.equal(await page.locator('#chat-input').isDisabled(), true);
    const heldCount = matches.length;
    await page.evaluate(() => { submitComposer(); submitComposer(); });
    assert.equal(matches.length, heldCount);
    releaseMatch(); await idle();

    await page.locator('#compose-question').click();
    const beforeQuestions = matches.length;
    for (const question of ['不要重新匹配', '为什么重新匹配', '重新匹配是什么意思', '重新匹配？', '企业新增两项专利，请重新匹配']) {
      await send(question);
      assert.equal(chats.at(-1).message, question);
    }
    assert.equal(matches.length, beforeQuestions, 'Questions, negations and mixed facts must not trigger matching');
    await page.locator('#composer-product').selectOption('');
    await send('重新匹配当前产品');
    assert.match(await page.locator('#notice').innerText(), /先.*选择/);
    assert.equal(matches.length, beforeQuestions);
    await page.locator('#messages button').first().click(); await idle();
    await send('重新匹配');
    assert.match(await page.locator('#notice').innerText(), /从这轮继续/);
    assert.equal(matches.length, beforeQuestions);
    assert.equal(await page.locator('#chat-input').inputValue(), '重新匹配');
    assert.deepEqual(await page.evaluate(() => state.view.data), originalHistory);
    assert.deepEqual(writes, []);
    assert.deepEqual(errors, []);
    console.log('Rematch commands: scoped/all SSE, both modes, no false chat/fact writes, failure retention, busy guard and historical isolation passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

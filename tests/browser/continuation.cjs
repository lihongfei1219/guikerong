// Uses fixture HTTP/SSE responses; never loads real sessions or calls a model.
// PLAYWRIGHT_MODULE and PLAYWRIGHT_CHROMIUM_EXECUTABLE may point to an existing installation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../..');
const clone = value => JSON.parse(JSON.stringify(value));
const clauses = [
  { condition_id: 'term', source_quote: '实际经营期限长于贷款期限', status: 'UNKNOWN',
    explanation: '企业已经营十年，仍需确认本次贷款期限。', required_material: '请说明本次计划贷款期限。',
    verification_needs: ['MATERIAL_CONFIRMATION'], source_quote_verified: true, evidence_ids: ['e1'] },
  { condition_id: 'ip', source_quote: '拥有I类知识产权', status: 'UNKNOWN',
    explanation: '需要核实企业的知识产权类别和权属。', required_material: '请说明企业拥有的知识产权类别和权属。',
    verification_needs: ['MATERIAL_CONFIRMATION'], source_quote_verified: true, evidence_ids: [] },
];
const product = id => ({ product_id: id, product_name: id === 'p1' ? '科技成长贷' : '其他产品', institution: '测试银行',
  application_conditions: clauses.map(c => c.source_quote).join('；'), decision: 'PENDING_VERIFICATION',
  verification_needs: ['MATERIAL_CONFIRMATION'], match_score: 0, run_id: 'run-1', history_count: 1,
  evaluated_at: '2026-10-26T01:00:00Z', can_review: true, stale: false, human_reviews: {}, validation_warnings: [],
  evidence: { e1: { filename: '企业资料.md', quote: '企业已实际经营十年。', line_start: 1, line_end: 1 } },
  eligibility: { status: 'UNKNOWN', explanation: '请补充贷款期限和知识产权信息。', condition_results: clone(clauses) } });
let session = { id: 's1', version: 3, input_revision: 1, messages: [{role: 'assistant', content: '初次匹配完成。', run_id: 'run-1'}], runs: [], product_attempts: [],
  request: { company: { name: '继续核验测试企业' }, documents: [{ document_id: 'd1', filename: '企业资料.md', content: '企业已实际经营十年。' }] },
  effective_matches: [product('p1'), product('p2')], run: { id: 'run-1', result: { summary: { evaluated_count: 2, failed_count: 0 } } } };
const originalOther = clone(session.effective_matches[1]);
const histories = {};
function snapshot(id) {
  histories[id] = { id, created_at: '2026-10-26T01:00:00Z', status: 'COMPLETED', attempts: [], reviews: [],
    effective_matches: clone(session.effective_matches), snapshot: { request: clone(session.request), knowledge: {} }, can_continue: true };
}
snapshot('run-1');
let saves = [], matches = [], failSave = false;
const chats = [];
let failChat = false;
let originalSession = null;

(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 1536, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://fixture.test/**', async route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname.endsWith('favicon.svg')) return route.fulfill({ status: 204, body: '' });
      const files = { '/': 'chat.html', '/assets/chat.js': 'chat.js', '/assets/chat.css': 'chat.css' };
      if (files[pathname]) return route.fulfill({ contentType: pathname.endsWith('.js') ? 'text/javascript' :
        pathname.endsWith('.css') ? 'text/css' : 'text/html', body: fs.readFileSync(path.join(root, 'app/static', files[pathname]), 'utf8') });
      const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (pathname.endsWith('/messages')) {
        const payload = route.request().postDataJSON();
        assert.equal(payload.expected_version, session.version);
        chats.push(clone(payload));
        session.messages.push({role: 'user', content: payload.message, context_label: payload.run_id ? '历史轮次提问' : '提问'});
        session.version += 2;
        if (failChat) { failChat = false; return json({error: {message: '模拟回复失败，问题已保存。'}}, 502); }
        session.messages.push({role: 'assistant', content: '根据现有资料，企业已实际经营十年。仍需确认本次计划贷款期限。\n你可以选择“补充事实”，保存贷款期限后继续匹配。', context_label: '提问'});
        session.version += 1;
        return json(session);
      }
      if (pathname.endsWith('/continue')) {
        const payload = route.request().postDataJSON();
        const source = histories[pathname.split('/').at(-2)];
        assert.equal(payload.product_id, 'p1');
        originalSession = clone(session);
        session = { ...clone(session), id: 'branch-1', version: 4, messages: [], runs: [],
          request: clone(source.snapshot.request), effective_matches: clone(source.effective_matches),
          origin: { session_id: 's1', run_id: source.id, evaluated_at: source.created_at },
          run: {id: 'baseline-1', result: {summary: {evaluated_count: 2, failed_count: 0}}} };
        for (const product of session.effective_matches) product.run_id = 'baseline-1';
        return json(session, 201);
      }
      if (pathname.endsWith('/documents/edit')) {
        const payload = route.request().postDataJSON();
        assert.equal(payload.expected_version, session.version);
        session.request.documents.find(d => d.document_id === payload.document_id).content = payload.content;
        session.version += 2;
        session.effective_matches[0].stale = true;
        session.effective_matches[0].stale_reasons = ['企业资料已更新'];
        return json(session);
      }
      if (pathname.endsWith('/supplements')) {
        const payload = route.request().postDataJSON();
        if (failSave) { failSave = false; return json({ error: { message: '记录已更新，请刷新后再操作。' } }, 409); }
        assert.equal(payload.expected_version, session.version);
        assert.equal(payload.base_run_id, session.effective_matches[0].run_id);
        saves.push(clone(payload));
        for (const entry of payload.entries) {
          let doc = session.request.documents.find(d => d.document_id === entry.document_id);
          if (!doc) {
            doc = { document_id: `s${session.request.documents.length}`, filename: '补充事实.md',
              human_supplement: { product_id: 'p1', application_conditions: session.effective_matches[0].application_conditions,
                condition_quote: clauses.find(c => c.condition_id === entry.condition_id)?.source_quote || '' } };
            session.request.documents.push(doc);
          }
          doc.content = entry.content; doc.human_supplement.verification = entry.verification;
        }
        session.version += 2; session.effective_matches[0].stale = true;
        session.effective_matches[0].stale_reasons = ['企业资料已更新'];
        return json(session);
      }
      if (pathname.endsWith('/match/stream')) {
        const payload = route.request().postDataJSON(); matches.push(clone(payload));
        assert.deepEqual(payload.product_ids, ['p1']);
        assert.equal(payload.expected_version, session.version);
        session.version += 3;
        if (matches.length === 1) {
          session.effective_matches[0].latest_attempt = { status: 'FAILED' };
          return route.fulfill({ contentType: 'text/event-stream', body: 'event: error\ndata: {"error":{"message":"模拟网络中断"}}\n\n' });
        }
        const current = session.effective_matches[0];
        const base = current.run_id; current.run_id = `run-${matches.length}`;
        current.stale = false; current.latest_attempt = { status: 'SUCCEEDED' };
        current.eligibility.condition_results[0].status = 'PASS';
        current.eligibility.condition_results[0].verification_needs = [];
        current.continuation = { base_run_id: base, before: 'UNKNOWN', after: 'UNKNOWN', unpaired_previous_count: 0,
          added_document_ids: ['s1', 's2'], updated_document_ids: [], removed_document_ids: [],
          changes: current.eligibility.condition_results.map(c => ({ ...c, before: 'UNKNOWN', after: c.status })) };
        current.model_assessment = { supplement_feedback: [{ document_id: 's1', status: 'ADOPTED',
          reason: '已核实本次期限为两年。', impact: '经营期限长于贷款期限。', condition_ids: ['term'] },
          { document_id: 's2', status: 'NEEDS_CONFIRMATION', reason: '知识产权尚未核实。', impact: '仍需核实权属。', condition_ids: ['ip'] }] };
        session.run = { id: current.run_id, result: { summary: { evaluated_count: 1, failed_count: 0 } } };
        session.messages.push({ role: 'assistant', content: '继续匹配完成。', run_id: current.run_id });
        snapshot(current.run_id);
        return route.fulfill({ contentType: 'text/event-stream', body: `event: result\ndata: ${JSON.stringify(session)}\n\n` });
      }
      const responses = {
        '/health': {}, '/api/v1/financial-products': { products: session.effective_matches },
        '/api/v1/knowledge': { items: [] }, '/api/v1/sessions': { sessions: [{ id: 's1', company: session.request.company }] },
        '/api/v1/sessions/s1': originalSession || session,
        '/api/v1/sessions/branch-1': session,
      };
      if (pathname.includes('/runs/')) return json(histories[pathname.split('/').at(-1)]);
      assert.ok(pathname in responses, `Unexpected request: ${pathname}`);
      return json(responses[pathname]);
    });
    const idle = () => page.waitForFunction(() => document.body.getAttribute('aria-busy') === 'false');
    const card = page.locator('[data-product-id="p1"]');
    const clause = id => card.locator(`[data-condition-id="${id}"]`);
    await page.goto('http://fixture.test/'); await idle();
    assert.equal(await page.locator('#chat-input').isDisabled(), true);
    await page.locator('#sessions .session-open').click(); await idle();
    assert.equal(await page.locator('#export, #refresh-session, #match-product, #chat-save-only, .chat-suggestions, .continuation-form').count(), 0);
    assert.equal(await card.locator('button:visible').count(), 2, 'One action per pending clause');
    await page.selectOption('#composer-product', 'p1');
    await page.locator('#chat-input').fill('为什么还需要核验？');
    await page.locator('#chat-input').press('Shift+Enter');
    assert.equal(chats.length, 0);
    await page.locator('#chat-input').dispatchEvent('keydown', {key: 'Enter', isComposing: true});
    assert.equal(chats.length, 0);
    await page.locator('#chat-input').press('Enter'); await idle();
    assert.equal(chats.length, 1);
    assert.equal(chats[0].product_id, 'p1');
    assert.equal(session.request.documents.length, 1);
    assert.match(await page.locator('#messages .assistant').last().innerText(), /仍需确认本次计划贷款期限/);
    await clause('term').getByRole('button', {name: '补充信息', exact: true}).click();
    await page.locator('#chat-input').fill('本次计划贷款期限为两年。');
    await page.locator('#chat-verified').check();
    if (process.env.SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.SCREENSHOT_DIR, {recursive: true});
      await page.screenshot({path: path.join(process.env.SCREENSHOT_DIR, 'simplified-desktop.png')});
      await page.setViewportSize({width: 390, height: 844});
      await page.locator('#chat-form').scrollIntoViewIfNeeded();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({path: path.join(process.env.SCREENSHOT_DIR, 'simplified-mobile.png')});
      await page.setViewportSize({width: 1536, height: 1000});
    }
    await page.locator('#chat-send').click(); await idle();
    assert.equal(saves.length, 1); assert.equal(saves[0].entries.length, 1);
    assert.equal(saves[0].entries[0].verification, 'CONFIRMED');
    assert.equal(saves[0].entries[0].condition_id, 'term');
    assert.match(await page.locator('#notice').innerText(), /事实已保存，匹配未完成/);
    assert.equal(session.request.documents.length, 2);
    assert.equal(await page.locator('#chat-input').inputValue(), '');
    await card.getByRole('button', {name: '重新匹配此产品', exact: true}).click(); await idle();
    assert.equal(saves.length, 1); assert.equal(matches.length, 2);
    assert.equal(await card.locator('.continuation-result').getAttribute('open'), null);
    await card.locator('.continuation-result > summary').click();
    assert.match(await card.locator('.continuation-result').innerText(), /待核验 → 符合/);
    assert.deepEqual(session.effective_matches[1], originalOther);
    await page.locator('#manage-materials').click();
    await page.selectOption('#supplement-mode', 'EDIT');
    await page.selectOption('#edit-document', 's1');
    await page.locator('#document-content').fill('更正：本次计划贷款期限为三年。');
    await page.locator('#save-material').click(); await idle();
    await page.locator('#close-materials').click();
    assert.equal(session.request.documents.length, 2);
    assert.equal(histories['run-2'].snapshot.request.documents[1].content, '本次计划贷款期限为两年。');
    await clause('ip').getByRole('button', {name: '补充信息', exact: true}).click();
    await page.locator('#chat-input').fill('企业拥有一项发明专利，权属信息待核实。');
    failSave = true;
    await page.locator('#chat-send').click(); await idle();
    assert.equal(saves.length, 1);
    assert.equal(await page.locator('#chat-input').inputValue(), '企业拥有一项发明专利，权属信息待核实。');
    await page.locator('#chat-send').click(); await idle();
    assert.equal(saves.length, 2); assert.equal(matches.length, 3);
    assert.equal(saves[1].entries[0].verification, 'UNVERIFIED');
    await page.locator('#compose-question').click();
    failChat = true;
    await page.locator('#chat-input').fill('失败后保留的问题');
    await page.locator('#chat-send').click(); await idle();
    assert.equal(await page.locator('#chat-input').inputValue(), '失败后保留的问题');
    await page.locator('#chat-input').fill('');
    await page.locator('#open-knowledge').click();
    assert.equal(await page.locator('#knowledge-panel').evaluate(d => d.open), true);
    await page.locator('#close-knowledge').click();
    await page.locator('#messages button').first().click(); await idle();
    await page.locator('#chat-input').fill('请解释这轮历史判断。');
    await page.locator('#chat-send').click(); await idle();
    assert.equal(chats.at(-1).run_id, 'run-1');
    assert.equal(await page.evaluate(() => state.view.data.id), 'run-1');
    assert.equal(await page.locator('#match').isDisabled(), true);
    await page.locator('#compose-fact').click();
    assert.equal(await page.locator('#chat-send').isDisabled(), true);
    const callsBeforeBranch = matches.length;
    await clause('term').getByRole('button', {name: '从这里继续补充', exact: true}).click(); await idle();
    assert.equal(matches.length, callsBeforeBranch);
    assert.equal(session.request.documents.length, 1);
    assert.equal(await page.locator('#match').isDisabled(), false);
    assert.match(await page.locator('#composer-target-text').innerText(), /实际经营期限长于贷款期限/);
    await page.locator('#chat-input').fill('从旧评估继续：本次计划贷款期限为一年。');
    await page.locator('#chat-verified').check();
    await page.locator('#chat-send').click(); await idle();
    assert.equal(matches.length, callsBeforeBranch + 1);
    assert.equal(saves.at(-1).base_run_id, 'baseline-1');
    assert.equal(session.request.documents.length, 2);
    assert.equal(originalSession.request.documents.length, 3);
    assert.equal(originalSession.request.documents[1].content, '更正：本次计划贷款期限为三年。');
    await page.getByRole('button', {name: '返回原会话', exact: true}).click(); await idle();
    assert.equal(await page.evaluate(() => state.session.id), 's1');
    assert.deepEqual(errors, []);
    console.log('Simplified UI passed: removed duplicate controls, chat, scoped facts, retry, material editing, knowledge dialog, history continuation and responsive layout.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

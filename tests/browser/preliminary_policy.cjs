// Fixture-only rendering checks; never reads real sessions or calls a model.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../..');
const clone = value => JSON.parse(JSON.stringify(value));
const condition = (id, status = 'PASS', basis) => ({ condition_id: id, status,
  ...(basis ? { assessment_basis: basis } : {}), verification_needs: status === 'UNKNOWN' ? ['MATERIAL_CONFIRMATION'] : [],
  source_quote: id, explanation: '按本轮判断口径解释。', source_quote_verified: true, evidence_ids: [] });
const conditions = [
  condition('policy', 'PASS', 'PRELIMINARY_POLICY'),
  condition('other', 'PASS', 'PRELIMINARY_POLICY'),
  condition('evidence', 'PASS', 'EVIDENCE'),
  condition('legacy'),
  condition('reviewed', 'PASS', 'PRELIMINARY_POLICY'),
  condition('unknown', 'UNKNOWN', 'PRELIMINARY_POLICY'),
  condition('failed', 'FAIL', 'PRELIMINARY_POLICY'),
];
const product = { product_id: 'p1', product_name: '初筛测试产品', institution: '测试银行',
  decision: 'PENDING_VERIFICATION', verification_needs: ['MATERIAL_CONFIRMATION'], application_conditions: '测试条件',
  eligibility: { status: 'UNKNOWN', explanation: '概括条件按初筛处理，其他事实仍待确认。', condition_results: conditions },
  human_reviews: { reviewed: { reviewer: '测试复核员', status: 'PASS', reason: '已人工确认', evidence: '复核记录', created_at: '2026-10-27T02:00:00Z' } },
  validation_warnings: [], stale: false, can_review: true, run_id: 'new-run', evaluated_at: '2026-10-27T02:00:00Z' };
const request = { company: { name: '初筛测试企业' }, documents: [] };
const oldProduct = { ...clone(product), eligibility: { status: 'PASS', explanation: '旧模型判断。', condition_results: [condition('legacy')] },
  decision: 'FULLY_SATISFIED', verification_needs: [], human_reviews: {} };
const run = { id: 'old-run', status: 'COMPLETED', created_at: '2026-10-26T02:00:00Z',
  snapshot: { request, knowledge: {} }, effective_matches: [oldProduct], reviews: [], attempts: [], can_continue: true };
const session = { id: 's1', request, effective_matches: [product], runs: [], product_attempts: [],
  messages: [{ role: 'assistant', content: '旧轮次', run_id: 'old-run' }] };

(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 1536, height: 900 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://fixture.test/**', async route => {
      assert.equal(route.request().method(), 'GET', 'Viewing policy labels must not write data');
      const pathname = new URL(route.request().url()).pathname;
      const files = { '/': 'chat.html', '/assets/chat.js': 'chat.js', '/assets/chat.css': 'chat.css' };
      if (files[pathname]) return route.fulfill({ contentType: pathname.endsWith('.js') ? 'text/javascript' :
        pathname.endsWith('.css') ? 'text/css' : 'text/html', body: fs.readFileSync(path.join(root, 'app/static', files[pathname]), 'utf8') });
      const responses = { '/health': {}, '/api/v1/financial-products': { products: [] }, '/api/v1/knowledge': { items: [] },
        '/api/v1/sessions': { sessions: [{ id: 's1', company: request.company }] }, '/api/v1/sessions/s1': session,
        '/api/v1/sessions/s1/runs/old-run': run };
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(responses[pathname] || {}) });
    });
    const idle = () => page.waitForFunction(() => document.body.getAttribute('aria-busy') === 'false');
    const tag = id => page.locator(`[data-condition-id="${id}"] > .tag`).textContent();
    await page.goto('http://fixture.test/');
    await idle();
    await page.locator('#sessions .session-open').click();
    await idle();
    assert.equal(await page.locator('.preliminary-summary').textContent(), '2项概括性条件按初筛口径通过');
    assert.equal(await page.locator('.product-head > .tag').textContent(), '待核验', 'Main product classification stays intact');
    assert.equal(await tag('policy'), '初筛通过');
    assert.equal(await tag('other'), '初筛通过');
    for (const id of ['evidence', 'legacy', 'reviewed']) assert.equal(await tag(id), '符合');
    assert.equal(await tag('unknown'), '待核验');
    assert.equal(await tag('failed'), '不符合');
    await page.locator('#messages button').click();
    await idle();
    assert.equal(await page.locator('.preliminary-summary').count(), 0, 'Old snapshots do not acquire new policy labels');
    assert.equal(await tag('legacy'), '符合');
    assert.deepEqual(await page.evaluate(() => state.view.data), run, 'History remains unchanged');
    await page.getByRole('button', { name: '返回全部产品总览', exact: true }).click();
    assert.equal(await page.locator('.preliminary-summary').textContent(), '2项概括性条件按初筛口径通过');
    assert.deepEqual(errors, []);
    console.log('Preliminary policy labels: PASS-only, evidence/legacy fallback, human review exclusion, product count and immutable history passed.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

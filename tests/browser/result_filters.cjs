// Run with Node and Playwright available on NODE_PATH; no live server or model is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const MATERIAL = 'MATERIAL_CONFIRMATION';
const PROFESSIONAL = 'PROFESSIONAL_REVIEW';
const root = path.resolve(__dirname, '../..');
function condition(id, needs = [], status = 'UNKNOWN') {
  return { condition_id: id, status, verification_needs: needs, source_quote: id,
    explanation: id, source_quote_verified: true, evidence_ids: [] };
}
function product(id, needs, conditions, decision = 'PENDING_VERIFICATION') {
  return { product_id: id, product_name: id, institution: 'Test Bank', match_score: 50,
    decision, verification_needs: needs, application_conditions: 'Complete source',
    eligibility: { status: decision === 'FULLY_SATISFIED' ? 'PASS' : decision === 'NOT_ELIGIBLE' ? 'FAIL' : 'UNKNOWN',
      explanation: 'Overall assessment', condition_results: conditions },
    human_reviews: {}, validation_warnings: [], evaluated_at: '2026-10-26T01:00:00Z',
    stale: false, history_count: 1, can_review: true, run_id: 'saved-run' };
}
const matches = [
  product('material', [MATERIAL], [condition('material-only', [MATERIAL])]),
  product('professional', [PROFESSIONAL], [condition('professional-only', [PROFESSIONAL])]),
  product('both', [MATERIAL, PROFESSIONAL], [condition('material-clause', [MATERIAL]),
    condition('professional-clause', [PROFESSIONAL]), condition('shared-clause', [MATERIAL, PROFESSIONAL]),
    condition('passed-clause', [], 'PASS')]),
  product('overall-only', [MATERIAL], [condition('passed-clause', [], 'PASS')]),
  product('pass', [], [], 'FULLY_SATISFIED'),
  product('fail', [], [], 'NOT_ELIGIBLE'),
];
const request = { company: { name: 'Test Company' }, documents: [] };
const run = { id: 'saved-run', created_at: '2026-10-26T01:00:00Z', status: 'COMPLETED',
  effective_matches: matches, snapshot: { request, knowledge: {} }, reviews: [],
  attempts: [{ product_id: 'failed-attempt', product_name: 'failed-attempt', status: 'FAILED' }], can_continue: true };
const session = { id: 'test-session', request, effective_matches: matches, runs: [],
  messages: [{ role: 'assistant', content: 'Saved result', run_id: run.id }] };

(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 1536, height: 900 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://fixture.test/**', async route => {
      assert.equal(route.request().method(), 'GET', 'Filtering must never write data');
      const pathname = new URL(route.request().url()).pathname;
      const files = { '/': 'chat.html', '/assets/chat.js': 'chat.js', '/assets/chat.css': 'chat.css' };
      if (files[pathname]) {
        return route.fulfill({ contentType: pathname.endsWith('.js') ? 'text/javascript' :
          pathname.endsWith('.css') ? 'text/css' : 'text/html',
          body: fs.readFileSync(path.join(root, 'app/static', files[pathname]), 'utf8') });
      }
      const responses = {
        '/health': {}, '/api/v1/financial-products': { products: [] },
        '/api/v1/knowledge': { items: [] },
        '/api/v1/sessions': { sessions: [{ id: session.id, company: request.company }] },
        '/api/v1/sessions/test-session': session,
        '/api/v1/sessions/test-session/runs/saved-run': run,
      };
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(responses[pathname] || {}) });
    });
    const idle = () => page.waitForFunction(() => document.body.getAttribute('aria-busy') === 'false');
    const ids = () => page.locator('#results > .product').evaluateAll(cards => cards.map(c => c.dataset.productId));
    const clauses = () => page.locator('[data-product-id="both"] > [data-condition-id]')
      .evaluateAll(items => items.map(c => c.dataset.conditionId));
    await page.goto('http://fixture.test/');
    await idle();
    await page.locator('#sessions .session-open').click();
    await idle();
    // Reproduce the user's exact entry point: the message's saved-run button.
    await page.locator('#messages button').click();
    await idle();
    assert.match(await page.locator('#result-navigation').innerText(), /从这轮继续/);
    // Ensure filtering uses the saved run even if the latest overview has different results.
    await page.evaluate(() => { state.session.effective_matches = []; });
    assert.equal((await ids()).length, 6);
    await page.selectOption('#status-filter', MATERIAL);
    assert.deepEqual(await ids(), ['material', 'both', 'overall-only']);
    assert.equal(await page.locator('#result-count').innerText(), '显示 3 / 6 个产品');
    assert.match(await page.locator('#result-filter-summary').innerText(), /材料待确认/);
    assert.deepEqual(await clauses(), ['material-clause', 'shared-clause']);
    assert.equal(await page.locator('[data-product-id="both"] .other-conditions').getAttribute('open'), null);
    assert.equal(await page.locator('[data-product-id="both"] .other-conditions [data-condition-id]').count(), 2);
    assert.match(await page.locator('[data-product-id="overall-only"]').innerText(), /整体判断/);
    assert.equal(await page.locator('#results > .warning').count(), 0);
    await page.selectOption('#status-filter', PROFESSIONAL);
    assert.deepEqual(await ids(), ['professional', 'both']);
    assert.deepEqual(await clauses(), ['professional-clause', 'shared-clause']);
    assert.equal(await page.locator('#result-count').innerText(), '显示 2 / 6 个产品');
    await page.locator('[data-product-id="both"] .other-conditions > summary').click();
    assert.equal(await page.locator('[data-product-id="both"] .other-conditions [data-condition-id="material-clause"]').isVisible(), true);
    await page.locator('#product-search').fill('no-match');
    assert.deepEqual(await ids(), []);
    assert.equal(await page.locator('#results .empty').innerText(), '当前筛选下没有产品。');
    await page.locator('#product-search').fill('Test Bank');
    assert.deepEqual(await ids(), ['professional', 'both']);
    await page.locator('#product-search').fill('');
    await page.selectOption('#status-filter', 'PENDING_VERIFICATION');
    assert.equal((await ids()).length, 4);
    assert.equal((await clauses()).length, 3);
    for (const [filter, expected] of [['FULLY_SATISFIED', ['pass']], ['NOT_ELIGIBLE', ['fail']]]) {
      await page.selectOption('#status-filter', filter);
      assert.deepEqual(await ids(), expected);
    }
    await page.selectOption('#status-filter', 'all');
    assert.equal((await ids()).length, 6);
    assert.equal(await page.locator('#results > .warning').count(), 1);
    assert.equal(await page.locator('#result-filter-summary').isVisible(), false);
    assert.ok(await page.locator('#results .history-continue:visible').count());
    assert.equal(await page.locator('#results button:not(.history-continue):not(.conversation-link):visible').count(), 0);
    assert.deepEqual(await page.evaluate(() => state.view.data), run, 'Saved history stays unchanged');
    await page.evaluate(products => { state.session.effective_matches = products; }, matches);
    await page.getByRole('button', { name: '返回全部产品总览', exact: true }).click();
    await page.selectOption('#status-filter', MATERIAL);
    assert.deepEqual(await ids(), ['material', 'both', 'overall-only']);
    assert.deepEqual(await clauses(), ['material-clause', 'shared-clause']);
    assert.ok(await page.locator('#results button:visible').count());
    assert.deepEqual(errors, []);
    console.log('Result filters: saved-run entry, overlapping needs, clause focus, counts, search, empty state, history continuation actions and overview passed.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../app/static/admin.html'), 'utf8');
const usageSource = html.split('// ==================== Usage ====================')[1]
  .split('// ==================== Request Logs ====================')[0];

function createHarness() {
  const elements = new Map();
  const timers = new Map();
  const requests = [];
  let timerId = 0;
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: '', textContent: '', innerHTML: '', disabled: false, style: {},
      parentElement: { scrollLeft: 0 },
      classList: { toggle() {} },
      replaceChildren() {}, appendChild() {}, addEventListener() {},
      querySelectorAll() { return []; },
    });
    return elements.get(id);
  }
  element('usagePeriod').value = 'today';
  element('usageResultFilter').value = 'all';
  element('usageSort').value = 'cost_cents';
  element('usageTrendMetric').value = 'cost_cents';
  const context = vm.createContext({
    document: { getElementById: element, querySelectorAll: () => [] },
    URLSearchParams, AbortController, Intl, Date,
    adminHeaders: () => ({}),
    formatMoney: value => String(value),
    formatNum: value => String(value),
    formatPct: value => String(value),
    escapeHtml: value => String(value),
    setTimeout(callback) { timers.set(++timerId, callback); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    // Deliberately allow responses after abort to exercise the stale-response guards.
    fetch(url, options) {
      return new Promise(resolve => requests.push({
        url: new URL(url, 'http://localhost'), signal: options.signal,
        respond: data => resolve({ ok: true, json: async () => data }),
      }));
    },
  });
  vm.runInContext(usageSource, context, { filename: 'admin.html usage section' });
  return { context, element, requests, timers, state: () => vm.runInContext('usageState', context) };
}

function overview(period, cost = 100) {
  return {
    period_label: period, as_of: '2026-09-17T04:00:00+00:00',
    window_start: '2026-09-16T16:00:00+00:00', window_end: '2026-09-17T04:00:00+00:00',
    summary: { cost_cents: cost, records: 0, users: 0, models: 0, tokens: 0, requests: 0 },
    trend: [], granularity: 'hour',
  };
}

const flush = () => new Promise(resolve => setImmediate(resolve));

async function finishLoad(harness, loading, requestIndex, period, cost = 100) {
  harness.requests[requestIndex].respond(overview(period, cost));
  await flush();
  assert.equal(harness.requests[requestIndex + 1].url.pathname, '/admin/usage/groups');
  harness.requests[requestIndex + 1].respond({ data: [], total: 0 });
  await loading;
}

test('cancelled filter debounce reloads the selected period on reentry', async () => {
  const h = createHarness();
  await finishLoad(h, h.context.loadFullUsage(), 0, 'today');
  h.element('usagePeriod').value = '24h';
  h.context.usagePeriodChanged();
  assert.equal(h.timers.size, 1);
  h.context.suspendUsageRequests();
  assert.equal(h.timers.size, 0);

  const returning = h.context.enterUsagePage();
  assert.equal(h.requests.length, 3, 'returning must request a new overview');
  assert.equal(h.requests[2].url.searchParams.get('period'), '24h');
  await finishLoad(h, returning, 2, '24h');
  assert.equal(h.state().params.get('period'), '24h');
});

test('unchanged fresh results survive navigation without additional requests', async () => {
  const h = createHarness();
  await finishLoad(h, h.context.loadFullUsage(), 0, 'today');
  h.context.suspendUsageRequests();
  await h.context.enterUsagePage();
  assert.equal(h.requests.length, 2);
  assert.equal(h.state().tableReady, true);
});

test('an old overview arriving last cannot replace the latest selected filter', async () => {
  const h = createHarness();
  const oldLoading = h.context.loadFullUsage();
  h.element('usagePeriod').value = '24h';
  const newLoading = h.context.loadFullUsage();
  assert.equal(h.requests[0].signal.aborted, true);
  await finishLoad(h, newLoading, 1, '24h', 200);

  h.requests[0].respond(overview('today', 999));
  await oldLoading;
  assert.equal(h.requests.length, 3, 'the stale overview must not start another table request');
  assert.equal(h.state().params.get('period'), '24h');
  assert.equal(h.element('usageStatCost').textContent, '200');
});

test('an old table arriving last cannot replace rows for the latest filter', async () => {
  const h = createHarness();
  const oldLoading = h.context.loadFullUsage();
  h.requests[0].respond(overview('today'));
  await flush();
  h.element('usagePeriod').value = '24h';
  const newLoading = h.context.loadFullUsage();
  assert.equal(h.requests[1].signal.aborted, true);
  await finishLoad(h, newLoading, 2, '24h', 200);

  h.requests[1].respond({ data: [{ key: 'stale-user' }], total: 1 });
  await oldLoading;
  assert.equal(h.state().rows.length, 0);
  assert.equal(h.state().params.get('period'), '24h');
  assert.equal(h.element('usagePageInfo').textContent, '第 0–0 条，共 0 条');
  assert.equal(h.element('usageStatCost').textContent, '200');
});

test('an overview completing during debounce cannot restore the old cached page', async () => {
  const h = createHarness();
  const oldLoading = h.context.loadFullUsage();
  h.element('usagePeriod').value = '24h';
  h.context.usagePeriodChanged();
  assert.equal(h.requests[0].signal.aborted, true);

  h.requests[0].respond(overview('today', 999));
  await oldLoading;
  assert.equal(h.requests.length, 1, 'the obsolete overview must not load its table during debounce');
  assert.equal(h.state().loadedAt, 0);
  assert.equal(h.element('usageStatCost').textContent, '—');

  h.context.suspendUsageRequests();
  assert.equal(h.timers.size, 0);
  const returning = h.context.enterUsagePage();
  assert.equal(h.requests[1].url.searchParams.get('period'), '24h');
  await finishLoad(h, returning, 1, '24h', 200);
  assert.equal(h.element('usageStatCost').textContent, '200');
});

test('a table completing during debounce cannot mark obsolete results ready', async () => {
  const h = createHarness();
  const oldLoading = h.context.loadFullUsage();
  h.requests[0].respond(overview('today'));
  await flush();
  h.element('usagePeriod').value = '24h';
  h.context.usagePeriodChanged();
  assert.equal(h.requests[1].signal.aborted, true);

  h.requests[1].respond({ data: [{ key: 'stale-user' }], total: 1 });
  await oldLoading;
  assert.equal(h.state().tableReady, false);
  assert.equal(h.state().rows.length, 0);
  assert.equal(h.state().loadedAt, 0);

  h.context.suspendUsageRequests();
  const returning = h.context.enterUsagePage();
  assert.equal(h.requests[2].url.searchParams.get('period'), '24h');
  await finishLoad(h, returning, 2, '24h', 200);
  assert.equal(h.element('usageStatCost').textContent, '200');
});

test('model drilldown opens its user distribution before request records', () => {
  const h = createHarness();
  const target = h.context.applyUsageDrilldown({ key: 'public-a' }, 'models');

  assert.equal(target, 'users');
  assert.equal(h.state().model, 'public-a');
  assert.equal(h.state().user, null);
  h.context.renderUsageTabState();
  assert.match(h.element('usageTableHint').textContent, /模型「public-a」的用户消耗/);
});

test('cross drilldowns preserve both user and model in request filters', () => {
  const h = createHarness();

  h.context.applyUsageDrilldown({ key: 'public-a' }, 'models');
  h.context.applyUsageDrilldown({ key: 'u1', display_name: 'Alice' }, 'users');
  let params = h.context.usageQueryParams();
  assert.equal(h.state().tab, 'records');
  assert.equal(params.get('model_exact'), 'public-a');
  assert.equal(params.get('user_id'), 'u1');

  h.state().user = null;
  h.state().model = '';
  h.state().tab = 'users';
  h.context.applyUsageDrilldown({ key: 'u2', display_name: 'Bob' }, 'users');
  assert.equal(h.state().tab, 'models');
  h.context.renderUsageTabState();
  assert.match(h.element('usageTableHint').textContent, /用户「Bob」的模型消耗/);
  h.context.applyUsageDrilldown({ key: 'public-b' }, 'models');
  params = h.context.usageQueryParams();
  assert.equal(h.state().tab, 'records');
  assert.equal(params.get('user_id'), 'u2');
  assert.equal(params.get('model_exact'), 'public-b');
});

test('user model pairs open the matching request records', () => {
  const h = createHarness();
  const target = h.context.applyUsageDrilldown({ user_id: 'u1', display_name: 'Alice', model: 'public-a' }, 'pairs');

  assert.equal(target, 'records');
  assert.equal(h.state().user.id, 'u1');
  assert.equal(h.state().model, 'public-a');
  const params = h.context.usageQueryParams();
  assert.equal(params.get('user_id'), 'u1');
  assert.equal(params.get('model_exact'), 'public-a');
});

test('user model tab loads the pair endpoint without a dimension parameter', async () => {
  const h = createHarness();
  await finishLoad(h, h.context.loadFullUsage(), 0, 'today');

  const loading = h.context.setUsageTab('pairs');
  assert.equal(h.requests[2].url.pathname, '/admin/usage/pairs');
  assert.equal(h.requests[2].url.searchParams.has('dimension'), false);
  assert.equal(h.requests[2].url.searchParams.get('include_total'), 'false');
  h.requests[2].respond({ data: [], total: null, has_more: false });
  await loading;
  assert.match(h.element('usageTableHint').textContent, /每一行就是一组用户 × 模型/);
});

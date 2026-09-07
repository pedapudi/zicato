// Saved health evidence and unreadable reasons participate in repaint decisions.
import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();
const { freshState, installFetch, data, router, EPOCH_ID } = await import('./fixtures.mjs');
const home = await import('../js/views/home.js');

test('home health reports corruption, repaints changed evidence, and preserves unchanged DOM', async () => {
  freshState(); installFetch();
  const fallback = globalThis.fetch;
  let health = { epoch_id: EPOCH_ID, healthy: null, findings: [], unreadable: 'Invalid report object' };
  globalThis.fetch = async (path) => String(path).startsWith('/api/health-report')
    ? { ok: true, json: async () => health } : fallback(path);
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  const render = async () => { data.invalidate(); await home.render(host, ctx, {}); };
  await render();
  assert(host.textContent.includes('Health report unreadable: Invalid report object'));
  assert(!host.textContent.includes('loop is healthy'));
  const first = host.firstChild;
  await render();
  assertEqual(host.firstChild, first, 'identical evidence preserves DOM identity');
  health = { ...health, unreadable: 'Epoch coordinate differs' };
  await render();
  assert(host.textContent.includes('Epoch coordinate differs'));
  assert(!host.textContent.includes('Invalid report object'));
  health = { epoch_id: EPOCH_ID, healthy: false, findings: [
    { code: 'tree_never_imported', severity: 'warning', summary: 'helper was never imported' },
  ] };
  await render();
  assert(host.textContent.includes('tree_never_imported'));
  assert(host.textContent.includes('helper was never imported'));
  health.findings = [{ code: 'outage', severity: 'critical', summary: 'No measured runs' }];
  await render();
  assert(host.textContent.includes('No measured runs'));
  assert(!host.textContent.includes('helper was never imported'));
  health = { epoch_id: EPOCH_ID, healthy: true, findings: [] };
  await render();
  assert(host.textContent.includes('loop is healthy'));
  assert(!host.textContent.includes('unreadable'));
});

run();

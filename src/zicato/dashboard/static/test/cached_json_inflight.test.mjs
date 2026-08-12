// test/cached_json_inflight.test.mjs — cachedJson shares ONE in-flight request.
//
// data.js caches the PROMISE, not the resolved value. These pins guard the four
// rules that follow from it: concurrent callers for one URL issue ONE GET, a
// settled entry still serves from cache, a failure caches null (the honest
// "unavailable" paint) without rejecting, and an invalidate() that lands
// mid-flight is not clobbered by the resolving fetch writing a stale payload.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const data = await import('../js/data.js');

// A fetch stub that counts calls per path and resolves only when released, so a
// test can hold a request open and interleave a second caller or an invalidate.
function installGatedFetch() {
  const calls = [];
  const gates = [];
  globalThis.fetch = (path) => {
    calls.push(path);
    let release;
    const p = new Promise((resolve) => { release = resolve; });
    gates.push({ path, release });
    return p.then((body) => ({ ok: true, json: async () => body }));
  };
  return {
    calls,
    countFor: (path) => calls.filter((c) => c === path).length,
    releaseAll: (body) => { for (const g of gates.splice(0)) g.release(body); },
  };
}

function fresh() {
  data.invalidate();
}

test('cachedJson: two concurrent callers for one URL issue ONE request', async () => {
  fresh();
  const f = installGatedFetch();
  const a = data.cachedJson('/api/workspace');
  const b = data.cachedJson('/api/workspace');
  assertEqual(f.countFor('/api/workspace'), 1, 'exactly one GET went out');
  f.releaseAll({ root: '/ws' });
  const [ra, rb] = await Promise.all([a, b]);
  assertEqual(ra.root, '/ws', 'first caller got the payload');
  assertEqual(rb.root, '/ws', 'second caller got the payload');
  assert(ra === rb, 'both callers share the SAME payload object (one fetch, one body)');
});

test('cachedJson: many concurrent callers still issue ONE request', async () => {
  fresh();
  const f = installGatedFetch();
  const waiters = Array.from({ length: 12 }, () => data.cachedJson('/api/epoch'));
  assertEqual(f.countFor('/api/epoch'), 1, '12 callers → 1 GET');
  f.releaseAll({ epoch_id: 'e0' });
  const all = await Promise.all(waiters);
  assert(all.every((r) => r.epoch_id === 'e0'), 'every caller resolved to the payload');
});

test('cachedJson: a SETTLED entry still serves from cache without refetching', async () => {
  fresh();
  const f = installGatedFetch();
  const first = data.cachedJson('/api/lineage');
  f.releaseAll({ generations: [] });
  await first;
  assertEqual(f.countFor('/api/lineage'), 1, 'the first call fetched');
  await data.cachedJson('/api/lineage');
  assertEqual(f.countFor('/api/lineage'), 1, 'the second call did NOT refetch');
});

test('cachedJson: a failed fetch resolves to null and does not reject', async () => {
  fresh();
  globalThis.fetch = async () => ({ ok: false, status: 500, json: async () => ({}) });
  const a = data.cachedJson('/api/health-report');
  const b = data.cachedJson('/api/health-report');
  // Both share the one caught promise — neither may reject.
  assertEqual(await a, null, 'the failure resolves to null');
  assertEqual(await b, null, 'the shared entry resolves to null too, never rejecting');
});

test('cachedJson: invalidate() mid-flight is NOT clobbered by the resolving fetch', async () => {
  fresh();
  const f = installGatedFetch();
  const stale = data.cachedJson('/api/tournaments');
  data.invalidate('/api/tournaments');   // bust while the GET is still open
  f.releaseAll({ matchups: ['stale'] });
  await stale;
  // The abandoned promise must not have written itself back into the cache, so
  // the next read is a genuine refetch rather than a cache hit on stale data.
  const f2 = installGatedFetch();
  const next = data.cachedJson('/api/tournaments');
  assertEqual(f2.countFor('/api/tournaments'), 1, 'the bust survived — a fresh GET went out');
  f2.releaseAll({ matchups: ['fresh'] });
  const payload = await next;
  assertEqual(payload.matchups[0], 'fresh', 'the fresh payload won, not the stale one');
});

run();

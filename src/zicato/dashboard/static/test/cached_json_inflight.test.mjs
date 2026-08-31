// test/cached_json_inflight.test.mjs — cachedJson shares ONE in-flight request.
//
// data.js caches the PROMISE rather than the resolved value. These pins guard the
// rules that follow from it: concurrent callers for one URL issue ONE GET, a
// settled entry still serves from cache, a failure caches null (the honest
// "unavailable" paint) without rejecting, and an invalidate() that lands
// mid-flight is not clobbered by the resolving fetch writing a stale payload.
//
// The last test pins the PREMISE rather than the unit: shell.dispatch() fires
// renderTree() WITHOUT awaiting it and then renders the view, so the tree's
// five-endpoint fan-out and the home view's two-endpoint fan-out overlap in one
// tick. That is where the duplicate /api/workspace came from, and it is what
// would silently come back if the promise cache were ever reverted.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const data = await import('../js/data.js');

// globalThis.fetch is process-wide and the suite shares one process, so a stub
// that outlives its test leaks into the next FILE. That matters more here than
// elsewhere: the gated stub below never resolves until released, so a leaked
// one would HANG an unsuspecting caller rather than fail it. Every test
// restores what it found.
const REAL_FETCH = globalThis.fetch;

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
  globalThis.fetch = REAL_FETCH;
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
  fresh();
});

test('cachedJson: many concurrent callers still issue ONE request', async () => {
  fresh();
  const f = installGatedFetch();
  const waiters = Array.from({ length: 12 }, () => data.cachedJson('/api/epoch'));
  assertEqual(f.countFor('/api/epoch'), 1, '12 callers → 1 GET');
  f.releaseAll({ epoch_id: 'e0' });
  const all = await Promise.all(waiters);
  assert(all.every((r) => r.epoch_id === 'e0'), 'every caller resolved to the payload');
  fresh();
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
  fresh();
});

test('cachedJson: a failed fetch resolves to null, does not reject, and RETRIES after invalidate()', async () => {
  fresh();
  let gets = 0;
  let failing = true;
  globalThis.fetch = async () => {
    gets += 1;
    return failing
      ? { ok: false, status: 500, json: async () => ({}) }
      : { ok: true, json: async () => ({ ok: 'recovered' }) };
  };
  const a = data.cachedJson('/api/health-report');
  const b = data.cachedJson('/api/health-report');
  // Both share the one caught promise — neither may reject.
  assertEqual(await a, null, 'the failure resolves to null');
  assertEqual(await b, null, 'the shared entry resolves to null too, never rejecting');
  assertEqual(gets, 1, 'the two concurrent callers shared the ONE failing GET');
  // The cached null is sticky (the view paints "unavailable" instead of
  // spinning) — but a bust must actually retry, or a single transient 500
  // would wedge the pane until a hard refresh.
  assertEqual(await data.cachedJson('/api/health-report'), null, 'the null stays cached — no retry storm on every read');
  assertEqual(gets, 1, 'a repeat read after a failure does NOT refetch');
  failing = false;
  data.invalidate('/api/health-report');
  const recovered = await data.cachedJson('/api/health-report');
  assertEqual(recovered.ok, 'recovered', 'invalidate() clears the cached null and the retry succeeds');
  fresh();
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
  fresh();
});

// The ordering above lets the pre-bust GET settle before the next read starts.
// The harder interleaving is the one production actually produces: the bust
// lands, the next dispatch re-reads and starts a SECOND GET, and only THEN does
// the abandoned first GET resolve. If settling ever wrote the cache again, that
// late resolve would overwrite a newer in-flight entry — the same stale-write
// bug, just one beat later and much harder to see.
test('cachedJson: a pre-bust GET settling AFTER the re-read does not overwrite the newer entry', async () => {
  fresh();
  const calls = [];
  const gates = [];
  globalThis.fetch = (path) => {
    calls.push(path);
    let release;
    const p = new Promise((resolve) => { release = resolve; });
    gates.push(release);
    return p.then((body) => ({ ok: true, json: async () => body }));
  };
  const abandoned = data.cachedJson('/api/lineage');
  data.invalidateLive();                     // the live bust drops /api/lineage
  const reread = data.cachedJson('/api/lineage');
  assertEqual(calls.length, 2, 'the bust forced a second, genuinely fresh GET');
  gates[1]({ generations: ['fresh'] });      // the NEW read lands first
  assertEqual((await reread).generations[0], 'fresh', 'the re-read got the fresh payload');
  gates[0]({ generations: ['stale'] });      // the abandoned pre-bust read lands LAST
  await abandoned;
  const after = await data.cachedJson('/api/lineage');
  assertEqual(calls.length, 2, 'no third GET — the fresh entry is still cached');
  assertEqual(after.generations[0], 'fresh', 'the late pre-bust resolve did NOT overwrite the newer entry');
  fresh();
});

// invalidateRunTranscript() is the bust that fires MOST often mid-flight: a
// candidate running on the watched board re-reads its transcript every beat,
// so a beat landing during a slow transcript GET is routine rather than a corner case.
test('cachedJson: invalidateRunTranscript() mid-flight also survives the resolving fetch', async () => {
  fresh();
  const f = installGatedFetch();
  const url = '/api/run/e0/v1/b1/transcript';
  const inflight = data.cachedJson(url);
  data.invalidateRunTranscript('e0', 'v1', 'b1', null);
  f.releaseAll({ turns: ['old'] });
  await inflight;
  const f2 = installGatedFetch();
  const next = data.cachedJson(url);
  assertEqual(f2.countFor(url), 1, 'the transcript bust survived — the next beat re-reads events.jsonl');
  f2.releaseAll({ turns: ['old', 'new'] });
  assertEqual((await next).turns.length, 2, 'the re-read saw the turn that landed during the first GET');
  fresh();
});

// ---- the premise, end to end ---------------------------------------------

test('dispatch shape: the shell tree and the home view issue ONE /api/workspace between them', async () => {
  fresh();
  const fixtures = await import('./fixtures.mjs');
  const shell = await import('../js/shell.js');
  const home = await import('../js/views/home.js');
  fixtures.freshState();

  const counts = new Map();
  globalThis.fetch = async (path) => {
    counts.set(path, (counts.get(path) || 0) + 1);
    // a real GET is not instantaneous; resolving on a later macrotask keeps the
    // two fan-outs overlapped rather than accidentally serialised.
    await new Promise((r) => setTimeout(r, 1));
    const v = fixtures.lookupFixture(fixtures.FIXTURE, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
  };

  const route = { view: 'home', params: {} };
  const ctx = { navigate() {}, href: fixtures.router.href };
  // exactly what dispatch() does: renderTree(route) is NOT awaited.
  const tree = shell.buildTreeModel(route);
  const view = home.render(document.createElement('div'), ctx, {}, route);
  await Promise.all([tree, view]);

  assertEqual(counts.get('/api/workspace'), 1,
    'the tree and the home view shared ONE /api/workspace GET (both read it in the same tick)');
  const duplicated = [...counts.entries()].filter(([, n]) => n > 1);
  assertEqual(duplicated.length, 0,
    'no URL was fetched twice in one refresh cycle, got: ' + JSON.stringify(duplicated));
  fresh();
});

await run();

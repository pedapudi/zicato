// test/_trace_render_probe.mjs — the TERMINATION probe for the Traces surface.
//
// Not a *.test.mjs file (run-all.mjs skips it): it is the CHILD process the
// traces suite spawns under a hard wall-clock timeout, so a non-terminating
// render (an unbounded loop in the strip figure, the detail builders or the
// episode-anchor wiring) FAILS BY TIMEOUT instead of hanging the suite forever.
// A pure-JS spin hangs node identically to a browser, so this is the honest pin.
//
// It drives the REAL committed reader fixtures (the composition-check payloads)
// through the list + detail + strip builders, then a DENSE synthetic lane (the
// 500-turn stress) — printing `ok` and exiting 0 when every render terminates.

import { installDom, makeEvent } from './harness.mjs';

installDom();

const fs = await import('node:fs');
const { router, svg, freshState, installFixtureMap } = await import('./fixtures.mjs');
const traces = await import('../js/views/traces.js');

const load = (name) =>
  JSON.parse(fs.readFileSync(new URL('./fixtures/trace_view/' + name + '.json', import.meta.url), 'utf8'));
const LIST = load('list');
const DETAIL = load('detail');
const EPOCH_ID = LIST.epoch_id;
const REFL_ID = LIST.reflection_id;
const TRACE_ID = DETAIL.trace_id;
const CTX = { navigate() {}, href: router.href };

freshState();
installFixtureMap({
  '/api/epoch': { epoch_id: EPOCH_ID, closed: false, goal: 'boot' },
  '/api/reflections': { reflections: [{ reflection_id: REFL_ID, epoch_id: EPOCH_ID, created_at: '2020-01-01T00:00:00Z', mode: 'mint', executed: true }] },
  [`/api/reflection/${REFL_ID}/traces`]: LIST,
  [`/api/reflection/${REFL_ID}/trace/${TRACE_ID}`]: DETAIL,
});

// the LIST route, then the DETAIL route (the route the freeze was reported on),
// then a second identical detail render (the digest-gated no-op path).
const listHost = document.createElement('div');
await traces.render(listHost, CTX, { epochId: EPOCH_ID, reflectionId: REFL_ID });
const host = document.createElement('div');
await traces.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFL_ID, traceId: TRACE_ID });
await traces.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFL_ID, traceId: TRACE_ID });

// every episode focus toggle (the anchor ↔ conversation cross-link) — a focus
// pass that failed to advance over an UNPOSITIONED (anchor:"signal") episode is
// exactly the shape of loop this probe exists to catch.
for (const row of host.querySelectorAll('[data-episode-id]')) {
  row.dispatchEvent(makeEvent('click'));
}

// the DENSE lane stress: 500 marks tiling the lane, both sizes.
const dense = {
  trace_id: 'stress',
  lane: { turn_count: 500, marks: Array.from({ length: 500 }, (_, i) => ({
    i, role: i % 2 ? 'agent' : 'user', x0: i / 500, x1: (i + 1) / 500, size: (i % 17) / 16, chars: i,
  })) },
  signals: [], budget: { shaded: true, fill: 0.5, over: false, label: 'stress' }, episodes: [],
};
svg.trajectoryStrip(dense, {});
svg.trajectoryStrip(dense, { compact: true });
svg.trajectoryStripDigest(dense, {});

process.stdout.write('ok\n');

// test/variant_t_home_epoch.test.mjs — Variant T ("Console IV") unit tests:
// Console IV round 7: the epoch-view round timeline (slim reel), compact
// match cards on the generations page, and the density baseline.
//
// Split mechanically from the former variant_t.test.mjs (assertions
// verbatim); shared fixtures + helpers live in ./fixtures.mjs.

import { installDom, test, run, assert, assertEqual, assertDeep, makeEvent } from './harness.mjs';

installDom();

const {
  router, svg, ui, shell, data, coreState,
  rounds, hovercard, live, EPOCH_ID, lookupFixture, installFetch,
  freshState, allByClass, readCss, svgsByClass, mountLiveShell,
} = await import('./fixtures.mjs');

// ====================================================================
// Console IV folds (round 7): the SLIM REEL on the epoch view, the
// compact MATCH CARDS on the generations page, and a DENSITY picker.
// ====================================================================


// ---- (a) the epoch view leads with the CHAMPION-SPINE ROUND TIMELINE ----

test('epoch view: leads with the CHAMPION-SPINE ROUND TIMELINE (one episode per round), NOT the old reel/bumps', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await epoch.render(host, ctx, { epochId: EPOCH_ID });

  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline rendered on the epoch view');
  assert(allByClass(host, 'dn-roundtl-spine')[0], 'the timeline has the champion spine');
  // a gauntlet epoch with 2 rejected challengers → a SINGLE round 0 episode (the
  // matchups fall under one round when there is no round_index stamp).
  const episodes = allByClass(host, 'dn-roundtl-ep');
  assert(episodes.length >= 1, 'the timeline renders ≥1 episode');
  // the old reel + the lineage-bumps are GONE (subsumed by the timeline).
  assert(allByClass(host, 'tr-reel').length === 0, 'the old slim reel is GONE (subsumed by the timeline)');
  assert(allByClass(host, 'dn-bumps').length === 0, 'the old lineage-bumps chart is GONE');
  // the champion-loss annotation reads on the spine.
  assert(host.textContent.includes('loss floor'), 'the spine annotates the loss floor');
  // the heatmap stays on the epoch view (carried forward).
  assert(allByClass(host, 'dn-heatmap')[0], 'the board×generation heatmap is still present on the epoch view');
});

// ---- (a2) the IN-FLIGHT round on the EPOCH VIEW (issue #16) ----------
// A multi-round gauntlet epoch where round 0 has SETTLED (v0 → v1 promoted) and
// round 1 is now PROPOSING its field (v5/v6/v7 via the live envelope, not yet in
// the journal/lineage). The epoch view must surface round 1 as its OWN in-flight
// round with a LIVE badge + an incrementing "N proposed · M applied" banner —
// NOT fold v5/v6/v7 under round 0.

const INFLIGHT_EPOCH = '2026-06-09_inflight';
function installInflightFetch(fieldStatus) {
  const gens = [
    { generation_id: 'v0', epoch_id: INFLIGHT_EPOCH, parent_generation_id: '', promoted: false, round_index: 0 },
    { generation_id: 'v1', epoch_id: INFLIGHT_EPOCH, parent_generation_id: 'v0', promoted: true, round_index: 0 },
  ];
  const F = {
    '/api/epoch': { epoch_id: INFLIGHT_EPOCH, closed: false, goal: 'In-flight round.',
      tournament: { structure: 'gauntlet', params: { field_size: 3 } },
      experiments: gens.map((g) => ({ generation_id: g.generation_id, parent_generation_id: g.parent_generation_id,
        round_index: g.round_index, outcome: { decision: g.promoted ? 'promoted' : 'rejected' } })),
      board: [{ entry_id: 'b1', kind: 'single_turn' }] },
    '/api/lineage': { generations: gens },
    '/api/tournaments': { epoch_id: INFLIGHT_EPOCH, champion_lineage: ['v0', 'v1'],
      matchups: [{ champion: 'v0', challenger: 'v1', decision: 'promoted', delta_scalar: -20, ran_at: 'a' }],
      tournaments: [] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 100 }, { generation_id: 'v1', scalar: 80 }] },
    // the LIVE active-tournament envelope for round 1, still proposing.
    '/api/active-tournament': { epoch_id: INFLIGHT_EPOCH, structure: 'gauntlet', phase: 'proposing',
      round_index: 1, total_rounds: 2, structure_params: { field_size: 3 },
      competitors: [{ generation_id: 'v1', seed: 1, role: 'champion' }],
      field_status: fieldStatus, projected: {} },
  };
  for (const g of gens) F[`/api/generation/${INFLIGHT_EPOCH}/${g.generation_id}/per-entry`] = { entries: [{ entry_id: 'b1', drift_loss: 50 }] };
  globalThis.fetch = async (path) => {
    const v = lookupFixture(F, path);
    return v !== undefined ? { ok: true, json: async () => v } : { ok: false, status: 404, json: async () => ({ error: 'nf: ' + path }) };
  };
}

test('epoch view (issue #16): a round still PROPOSING shows as its OWN in-flight round (not folded under round 0) + a LIVE proposed/applied banner', async () => {
  freshState();
  installInflightFetch([
    { generation_id: 'v5', status: 'applied' },
    { generation_id: 'v6', status: 'applied' },
    { generation_id: 'v7', status: 'proposing' },
  ]);
  // the live signals the epoch view reads to decide a run is active for this epoch.
  coreState.state.activeTournament = { epoch_id: INFLIGHT_EPOCH, structure: 'gauntlet', phase: 'proposing', round_index: 1 };
  coreState.state.heartbeat = { phase: 'proposing:round_1:v7', epoch_id: INFLIGHT_EPOCH, ts: Date.now() };
  coreState.state.activeRuns = [];

  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: INFLIGHT_EPOCH });

  // TWO episodes now: the settled round 0 + the in-flight round 1.
  const episodes = allByClass(host, 'dn-roundtl-ep');
  assertEqual(episodes.length, 2, 'the settled round 0 + the in-flight round 1 (NOT one folded round)');
  // the in-flight round wears a LIVE badge.
  assert(allByClass(host, 'dn-roundtl-eplive').length >= 1, 'the in-flight round wears a LIVE badge');
  // the incrementing banner reads 3 proposed · 2 applied · 1 proposing.
  assert(/3 proposed/.test(host.textContent), 'the live banner reads the in-flight proposed count (3)');
  assert(/2 applied/.test(host.textContent), 'the live banner reads the in-flight applied count (2)');
  // round 0 keeps ONLY its settled field — v5/v6/v7 are NOT mis-attributed to it.
  const round0 = episodes[0];
  assert(!/v5|v6|v7/.test(round0.textContent), 'the SETTLED round 0 does NOT show the new round’s proposed gens (no mis-attribution)');
  // the in-flight round carries the freshly-proposed field.
  const round1 = episodes[1];
  assert(/v5/.test(round1.textContent) && /v6/.test(round1.textContent) && /v7/.test(round1.textContent),
    'the in-flight round 1 shows the freshly-proposed field v5/v6/v7');

  coreState.state.activeTournament = null; coreState.state.heartbeat = null; coreState.state.activeRuns = [];
});

test('epoch view (issue #16): the banner INCREMENTS as the field mints (applied count climbs) + a no-op beat does NOT churn the round DOM', async () => {
  freshState();
  installInflightFetch([{ generation_id: 'v5', status: 'applied' }, { generation_id: 'v6', status: 'proposing' }]);
  coreState.state.activeTournament = { epoch_id: INFLIGHT_EPOCH, structure: 'gauntlet', phase: 'proposing', round_index: 1 };
  coreState.state.heartbeat = { phase: 'proposing:round_1:v6', epoch_id: INFLIGHT_EPOCH, ts: Date.now() };
  coreState.state.activeRuns = [];
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: INFLIGHT_EPOCH });
  assert(/2 proposed/.test(host.textContent) && /1 applied/.test(host.textContent), 'early: 2 proposed · 1 applied');
  const tl1 = allByClass(host, 'dn-roundtl')[0];

  // a NO-OP heartbeat (same field_status) → the round timeline node is preserved
  // (digest-gated, no rebuild, no flash).
  freshState();
  installInflightFetch([{ generation_id: 'v5', status: 'applied' }, { generation_id: 'v6', status: 'proposing' }]);
  coreState.state.activeTournament = { epoch_id: INFLIGHT_EPOCH, structure: 'gauntlet', phase: 'proposing', round_index: 1 };
  coreState.state.heartbeat = { phase: 'proposing:round_1:v6', epoch_id: INFLIGHT_EPOCH, ts: Date.now() };
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: INFLIGHT_EPOCH });
  assert(allByClass(host, 'dn-roundtl')[0] === tl1, 'a no-op heartbeat preserves the timeline node (digest-gated, zero rebuild)');

  // now v6 APPLIES → the banner increments to 2 applied + the DOM repaints.
  freshState();
  installInflightFetch([{ generation_id: 'v5', status: 'applied' }, { generation_id: 'v6', status: 'applied' }]);
  coreState.state.activeTournament = { epoch_id: INFLIGHT_EPOCH, structure: 'gauntlet', phase: 'proposing', round_index: 1 };
  coreState.state.heartbeat = { phase: 'proposing:round_1:v6', epoch_id: INFLIGHT_EPOCH, ts: Date.now() };
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: INFLIGHT_EPOCH });
  assert(/2 proposed/.test(host.textContent) && /2 applied/.test(host.textContent), 'after v6 applies: 2 proposed · 2 applied (the banner incremented)');

  coreState.state.activeTournament = null; coreState.state.heartbeat = null; coreState.state.activeRuns = [];
});

// ---- (b) the timeline stays fit-to-width under a MANY-round fixture ----

const MANY_EPOCH = '2026-05-31_many';
function installManyFetch(roundN) {
  const n = roundN || 11;                       // 11 rounds → 12 generations
  const gens = [{ generation_id: 'v0', epoch_id: MANY_EPOCH, parent_generation_id: '', promoted: true }];
  const matchups = [];
  const points = [{ generation_id: 'v0', scalar: 100 }];
  for (let i = 1; i <= n; i++) {
    const id = 'v' + i;
    // a round_index stamp per challenger → one round per challenger (a deep spine).
    gens.push({ generation_id: id, epoch_id: MANY_EPOCH, parent_generation_id: 'v0', promoted: false, round_index: i - 1 });
    matchups.push({ champion: 'v0', challenger: id, decision: 'rejected', delta_scalar: i * 1.5,
      ran_at: '2026-05-31T00:' + String(i).padStart(2, '0') + ':00', hypothesis_core_idea: 'Idea ' + i + '.' });
    points.push({ generation_id: id, scalar: 100 + i });
  }
  const MANY = {
    '/api/epoch': { epoch_id: MANY_EPOCH, closed: false, goal: 'Many rounds.',
      experiments: gens.map((g) => ({ generation_id: g.generation_id, parent_generation_id: g.parent_generation_id,
        outcome: { decision: g.promoted ? 'baseline' : 'rejected' } })), board: [{ entry_id: 'b1', kind: 'single_turn' }] },
    '/api/lineage': { generations: gens },
    '/api/tournaments': { epoch_id: MANY_EPOCH, champion_lineage: ['v0'], matchups },
    '/api/score-trajectory': { points },
    '/api/workspace': { current_epoch_id: MANY_EPOCH, epochs: [{ epoch_id: MANY_EPOCH }], sparkline: [] },
    '/api/health-report': { epoch_id: MANY_EPOCH, healthy: true, findings: [] },
  };
  MANY[`/api/generation/${MANY_EPOCH}/v0/per-entry`] = { entries: [{ entry_id: 'b1', drift_loss: 50 }] };
  for (const g of gens) MANY[`/api/generation/${MANY_EPOCH}/${g.generation_id}/per-entry`] =
    MANY[`/api/generation/${MANY_EPOCH}/${g.generation_id}/per-entry`] || { entries: [{ entry_id: 'b1', drift_loss: 50 }] };
  globalThis.fetch = async (path) => {
    const v = lookupFixture(MANY, path);
    return v !== undefined
      ? { ok: true, json: async () => v }
      : { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}

function buildSpineRounds(n) {
  const rs = [];
  let champ = 'v0';
  for (let i = 0; i < n; i++) {
    rs.push({ round_index: i, champion: { id: champ, scalar: 100 - i }, structure: 'gauntlet',
      challengers: [{ id: 'v' + (i + 1), scalar: 101 + i, promoted: false }], gateOutcome: { kind: 'held', gen: null } });
  }
  return rs;
}

test('round timeline: fit-to-width — a fixed-width viewBox; many-round spine nodes compress and never exceed the viewBox', () => {
  // 11 rounds → 11 spine nodes. Build the timeline directly to read the SVG.
  const node = svg.roundTimeline({ rounds: buildSpineRounds(11), onRound() {}, onCompetitor() {} });
  const svgs = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-roundtl-spine') && n.localName === 'svg');
  const strip = svgs[0];
  assert(strip, 'the timeline SVG spine rendered');
  assertEqual(strip.getAttribute('viewBox'), '0 0 1000 96', 'the spine uses a FIXED-width viewBox (fit-to-width, no pan/zoom)');
  const VBW = 1000;

  // every positioned element (x / cx / x2) stays within the fixed viewBox width.
  let maxX = 0;
  for (const elx of strip.querySelectorAll('[class]')) {
    const cx = elx.getAttribute('cx'); const x = elx.getAttribute('x'); const x2 = elx.getAttribute('x2');
    for (const v of [cx, x, x2]) { if (v != null && isFinite(+v)) { assert(+v <= VBW, 'no element exceeds the viewBox width (' + v + ' ≤ ' + VBW + ')'); maxX = Math.max(maxX, +v); } }
  }
  assert(maxX > 0 && maxX <= VBW, 'positions are bounded by the fixed viewBox');

  // node spacing COMPRESSES with more rounds: 11 nodes sit closer than 3.
  const xsOf = (k) => {
    const nd = svg.roundTimeline({ rounds: buildSpineRounds(k), onRound() {} });
    const s = nd.querySelectorAll('[class]').filter((q) => (q.getAttribute('class') || '').includes('dn-roundtl-disc') && q.localName === 'circle');
    return s.map((c) => +c.getAttribute('cx')).sort((a, b) => a - b);
  };
  const few = xsOf(3); const many = xsOf(11);
  assert((many[1] - many[0]) < (few[1] - few[0]), 'with more rounds the node spacing compresses');
});

test('epoch view: the round timeline fits to width with ~11 rounds (one episode per round, no overflow)', async () => {
  freshState(); installManyFetch(11);
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: MANY_EPOCH });
  const episodes = allByClass(host, 'dn-roundtl-ep');
  assertEqual(episodes.length, 11, 'one episode per round (11 round_index stamps → 11 rounds)');
  const strip = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-roundtl-spine') && n.localName === 'svg')[0];
  assertEqual(strip.getAttribute('viewBox'), '0 0 1000 96', 'still a fixed-width viewBox under many rounds');
  for (const c of strip.querySelectorAll('[class]').filter((q) => (q.getAttribute('class') || '').includes('dn-roundtl-disc'))) {
    assert(+c.getAttribute('cx') <= 1000, 'every spine node stays within the viewBox width');
  }
});

test('epoch view: the proposer brief KEEPS its expanded state across a data-changing re-render (no auto-collapse)', async () => {
  freshState();
  const EP = 'ep_brief_persist';
  // A long (>1200 char) brief defaults CLOSED, so a manual expand is the only
  // way it is open — the perfect probe for the gatedSwap-resets-state bug.
  const LONG_BRIEF = '# Long brief\n\n' + 'detail '.repeat(220);
  const F = {
    '/api/epoch': { epoch_id: EP, closed: false, goal: 'g', brief: LONG_BRIEF, experiments: [], board: [] },
    '/api/lineage': { generations: [] },
    '/api/tournaments': { epoch_id: EP, champion_lineage: [], matchups: [] },
    '/api/score-trajectory': { points: [] },
    '/api/workspace': { current_epoch_id: EP, epochs: [{ epoch_id: EP }], sparkline: [] },
    '/api/health-report': { epoch_id: EP, healthy: true, findings: [] },
  };
  globalThis.fetch = async (path) => {
    const v = lookupFixture(F, path);
    return v !== undefined ? { ok: true, json: async () => v } : { ok: false, status: 404, json: async () => ({}) };
  };
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };

  await epoch.render(host, ctx, { epochId: EP });
  let details = allByClass(host, 'dn-brief')[0];
  assert(details, 'the proposer-brief <details> rendered');
  assert(!details.hasAttribute('open'), 'a long brief starts collapsed');

  // The user expands it (the browser flips `.open` and fires `toggle`).
  details.open = true;
  details.dispatchEvent({ type: 'toggle' });

  // A live heartbeat moves data → the epoch digest changes → gatedSwap REBUILDS
  // the DOM. The brief must STAY expanded, not snap shut to its length default.
  F['/api/epoch'].closed = true;
  await epoch.render(host, ctx, { epochId: EP });
  details = allByClass(host, 'dn-brief')[0];
  assert(details.hasAttribute('open'), 'the brief stays EXPANDED across a data-changing re-render (no auto-collapse)');
});

// ---- (c) the generations page renders the banner + match-card grid ----

test('generations view: the FIELD renders as the structure-flow graphic (duelFlow lanes) — NO dt-match-card / dt-champ-banner boxes', async () => {
  freshState(); installFetch();
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  // the boxed banner + match cards are RETIRED — the field is a data-graphic.
  assertEqual(allByClass(host, 'dt-champ-banner').length, 0, 'NO dt-champ-banner box remains');
  assertEqual(allByClass(host, 'dt-match-card').length, 0, 'NO dt-match-card boxes remain');
  assertEqual(allByClass(host, 'dt-matchcards').length, 0, 'NO match-cards grid remains');

  // the integrated, compact accent CHAMPION header (not a boxed banner).
  const head = allByClass(host, 'dt-fieldflow-champ')[0];
  assert(head, 'the integrated champion header rendered');
  assert(host.textContent.includes('v0'), 'the header shows the champion id (v0)');
  assert(host.textContent.includes('defending'), 'the header reads defending');

  // the duel-flow figure: one challenger LANE per match-up (v0→v1, v0→v2).
  const flow = svgsByClass(host, 'dn-duelflow')[0];
  assert(flow, 'the field renders as the duel-flow structure-graphic (dn-duelflow)');
  const lanes = allByClass(flow, 'dn-duelflow-lane');
  assertEqual(lanes.length, 2, 'one lane per challenger (v0→v1, v0→v2)');
  // a Δ=0 champion reference rule + a crowned champion-gate.
  assert(allByClass(flow, 'dn-duelflow-ref').length >= 1, 'the Δ=0 champion reference rule is drawn');
  assert(allByClass(flow, 'dn-duelflow-gate').length >= 1, 'a crowned champion-gate node is drawn');
  // the per-challenger hypothesis lives ON HOVER (the hovercard), not in a box:
  // the dot is hovercard-wired and the hypothesis text is NOT in the visible DOM.
  const dots = allByClass(flow, 'dn-duelflow-dot');
  assert(dots.length >= 2 && dots.every((d) => d.getAttribute('data-hovercard') === '1'), 'each challenger dot is hovercard-wired (hypothesis + Δ on hover)');
  assert(!host.textContent.includes('Enforce explicit slide-structure output'), 'the hypothesis is NOT a visible box/label — it lives on the hovercard');
});

// ---- (d) match cards must NOT appear on the environment / workspace view ----

test('match cards: do NOT render on the environment / workspace (home) view', async () => {
  freshState(); installFetch();
  const home = await import('../js/views/home.js');
  const host = document.createElement('div');
  await home.render(host, { navigate() {}, href: router.href }, {});
  assert(host.textContent.includes('Environment'), 'the home/environment view rendered');
  assertEqual(allByClass(host, 'dt-match-card').length, 0, 'NO match cards on the environment view');
  assertEqual(allByClass(host, 'dt-champ-banner').length, 0, 'NO champion-defends banner on the environment view');
  assertEqual(allByClass(host, 'dn-roundtl').length, 0, 'NO round timeline on the environment view');
});

// ---- (e) CHANGE 2: the density picker is GONE; cozy is the baseline ----

test('density removed: no picker, no density APIs; cozy is the permanent baseline', () => {
  freshState();
  // the density picker + its read/persist/normalise/applyDensity surface is gone.
  assert(ui.DENSITY_THEMES === undefined, 'no DENSITY_THEMES table (the picker is removed)');
  assert(typeof ui.readDensity !== 'function', 'no readDensity (density is not a setting anymore)');
  assert(typeof ui.persistDensity !== 'function', 'no persistDensity');
  assert(typeof ui.applyDensity !== 'function' && typeof shell.applyDensity !== 'function', 'no applyDensity picker plumbing');
  assert(shell.DENSITIES === undefined, 'the shell no longer exposes density ids');
  // cozy is the one permanent baseline.
  assertEqual(ui.DENSITY, 'cozy', 'the active density constant is cozy');

  // the SIZE tokens are FIXED at the cozy values regardless of any argument.
  const cozy = ui.densityTokens();
  assertEqual(cozy.sizeScale, 1, 'cozy sizeScale baseline');
  assertEqual(cozy.heatCell, 16, 'cozy heatmap cell baseline');
  assertEqual(cozy.dagRowStep, 34, 'cozy DAG row-step baseline');
  assertEqual(cozy.reelScale, 1.18, 'cozy reel-scale baseline');
  // an (ignored) argument cannot change the baseline.
  assertEqual(ui.densityTokens('compact').sizeScale, 1, 'a compact arg is ignored — still cozy');
  assertEqual(ui.densityTokens('roomy').heatCell, 16, 'a roomy arg is ignored — still cozy');

  // the shell stamps the cozy baseline (never changes) on mount.
  const root = mountLiveShell('#/');
  assertEqual(root.getAttribute('data-t-density'), 'cozy', 'the mounted root carries the cozy baseline');

  // and the CSS bakes the cozy --dt-* spacing tokens unconditionally on the root,
  // with NO density-conditional selectors left.
  const css = readCss();
  assert(!/\[data-t-density="compact"\]/.test(css), 'no compact density selector in the CSS');
  assert(!/\[data-t-density="roomy"\]/.test(css), 'no roomy density selector in the CSS');
  assert(/#variant-root\[data-variant="T"\]\s*\{[^}]*--dt-rail:\s*288px/.test(css.replace(/\n/g, ' ')),
    'the cozy --dt-rail (288px) is the unconditional baseline on the root');
});

await run();

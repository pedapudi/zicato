// test/variant_t_candidate.test.mjs — Variant T ("Console IV") unit tests:
// the round-6 anchor wave: router/tree navigation, the candidate dossier
// (promote gate, provenance, radar), side-by-side compare + diff, the board
// transcript, live transcript, pickers + wordmark, and the under-render fix.
//
// Split mechanically from the former variant_t.test.mjs (assertions
// verbatim); shared fixtures + helpers live in ./fixtures.mjs.

import { installDom, test, run, assert, assertEqual, assertDeep, makeEvent } from './harness.mjs';

installDom();

const {
  router, svg, ui, shell, data, tree,
  compare, coreState, bus, rounds, dag, live,
  roundTimelineFromFixtures, EPOCH_ID, FIXTURE, lookupFixture, installFetch, freshState,
  allByClass, svgsByClass, mountLiveShell, installFixtureMap,
} = await import('./fixtures.mjs');

// ---- router: hierarchical path + the compare (cmp) target ----------

test('router: hierarchical views parse with the epoch + the compare target', () => {
  // CHANGE 1: the vestigial `/T` hash-route prefix is dropped — routes are bare `#/`.
  assertEqual(router.PREFIX, '#', 'the route prefix is bare `#` (no `/T`)');
  assert(!router.href('home', {}).includes('/T'), 'a home href carries no `/T` prefix');
  assert(!router.href('epoch', { epochId: EPOCH_ID }).includes('/T'), 'an epoch href carries no `/T` prefix');
  assertEqual(router.href('home', {}), '#/', 'home is the bare `#/` route');
  assertEqual(router.href('epoch', { epochId: EPOCH_ID }), `#/e/${EPOCH_ID}`, 'epoch href round-trips under the bare prefix');
  // an old `#/T/...` link no longer resolves to an app view (the prefix is gone).
  assertEqual(router.parseRoute(`#/T/e/${EPOCH_ID}`).view, 'home', 'a legacy `#/T/` link falls back to home');
  assertEqual(router.parseRoute('').view, 'home');
  assertEqual(router.parseRoute('#/').view, 'home');
  assertEqual(router.parseRoute('#/bogus').view, 'home');
  const ep = router.parseRoute(`#/e/${EPOCH_ID}`);
  assertEqual(ep.view, 'epoch'); assertEqual(ep.params.epochId, EPOCH_ID);
  // a representative DEEP route parses + its href round-trips.
  const deep = router.parseRoute(`#/e/${EPOCH_ID}/gen/v1/diff/coordinator_prompt`);
  assertEqual(deep.view, 'diff'); assertEqual(deep.params.gen, 'v1'); assertEqual(deep.params.mutId, 'coordinator_prompt');
  assertEqual(router.href('diff', { epochId: EPOCH_ID, gen: 'v1', mutId: 'coordinator_prompt' }),
    `#/e/${EPOCH_ID}/gen/v1/diff/coordinator_prompt`, 'the deep diff href round-trips under the bare prefix');
  const cand = router.parseRoute(`#/e/${EPOCH_ID}/gen/v1`);
  assertEqual(cand.view, 'candidate'); assertEqual(cand.params.gen, 'v1');
  // the side-by-side compare target rides as a ~cmp= suffix and deep-links.
  const cmp = router.parseRoute(`#/e/${EPOCH_ID}/gen/v1~cmp=v2`);
  assertEqual(cmp.view, 'candidate'); assertEqual(cmp.params.gen, 'v1'); assertEqual(cmp.cmp, 'v2');
  assertEqual(router.href('candidate', { epochId: EPOCH_ID, gen: 'v1' }, { cmp: 'v2' }), `#/e/${EPOCH_ID}/gen/v1~cmp=v2`);
  const brd = router.parseRoute(`#/e/${EPOCH_ID}/board/waffles_single/v1`);
  assertEqual(brd.view, 'board'); assertEqual(brd.params.entry, 'waffles_single'); assertEqual(brd.params.gen, 'v1');
  assertEqual(router.parseRoute(`#/e/${EPOCH_ID}/mutations/coordinator_prompt`).params.mutId, 'coordinator_prompt');
  assertEqual(router.parseRoute(`#/e/${EPOCH_ID}/paper`).view, 'publication');
  assertEqual(router.parseRoute(`#/e/${EPOCH_ID}/boards`).view, 'boards');
});

// ---- router.up(): the back/up destination -------------------------

test('router.up: navigates UP the selection hierarchy (incl. collapsing a compare split)', () => {
  assertEqual(router.up(router.parseRoute('#/')), null, 'environment has no parent');
  assertEqual(router.up(router.parseRoute(`#/e/${EPOCH_ID}`)).view, 'home', 'epoch → environment');
  assertEqual(router.up(router.parseRoute(`#/e/${EPOCH_ID}/gens`)).view, 'epoch', 'gens → epoch');
  assertEqual(router.up(router.parseRoute(`#/e/${EPOCH_ID}/gen/v1`)).view, 'gens', 'candidate → generations');
  // a compare split collapses to the bare candidate FIRST (it is a deeper state).
  const upFromCmp = router.up(router.parseRoute(`#/e/${EPOCH_ID}/gen/v1~cmp=v2`));
  assertEqual(upFromCmp.view, 'candidate'); assert(!upFromCmp.cmp, 'back clears the comparison first');
  assertEqual(router.up(router.parseRoute(`#/e/${EPOCH_ID}/board/waffles_single/v1`)).view, 'board', 'board+gen → bare board');
});

// ---- HEADLINE: the data-model TREE sidebar -------------------------

test('tree sidebar: renders Environment → Epoch → {Generations, Boards, Mutation surface, Publication}', () => {
  const host = document.createElement('div');
  const model = {
    epochs: [{ id: EPOCH_ID, current: true }],
    byEpoch: { [EPOCH_ID]: {
      gens: [{ id: 'v0', promoted: true, parent: null }, { id: 'v1', promoted: false, parent: 'v0' }, { id: 'v2', promoted: false, parent: 'v0' }],
      boards: [{ id: 'waffles_single' }, { id: 'picky_stakeholder_emulated' }],
    } },
  };
  const toggles = new Set(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + '/gens', 'e:' + EPOCH_ID + '/boards']);
  const route = router.parseRoute(`#/e/${EPOCH_ID}`);
  const ctx = { navigate() {}, href: router.href };
  tree.buildTree(host, model, route, toggles, ctx, () => {});

  assert(allByClass(host, 'dt-tree')[0], 'the tree root rendered');
  const txt = host.textContent;
  assert(txt.includes('Environment'), 'Environment root present');
  assert(txt.includes(EPOCH_ID), 'the epoch node present');
  assert(txt.includes('Generations'), 'Generations group present');
  assert(txt.includes('Boards'), 'Boards group present');
  assert(txt.includes('Mutation surface'), 'Mutation surface node present');
  assert(txt.includes('Publication'), 'Publication node present');
  assert(txt.includes('v0') && txt.includes('v1') && txt.includes('v2'), 'every generation is a tree leaf');
  assert(txt.includes('waffles_single') && txt.includes('picky_stakeholder_emulated'), 'every board entry is a tree leaf');
  assert(allByClass(host, 'dt-glyph-gen-champ').length >= 1, 'the champion generation carries a champion glyph');
});

// ---- multi-candidate navigation ------------------------------------

test('candidate view: navigating to a SECOND generation works (multi-candidate nav)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  assert(host.textContent.includes('Candidate v1'), 'v1 rendered');
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v2' });
  assert(host.textContent.includes('Candidate v2'), 'v2 rendered after switching');
  assert(!host.textContent.includes('Candidate v1'), 'the previous candidate was replaced (digest changed)');
});

// ---- FIX #1: promote gate ON the candidate page --------------------

test('candidate view: the promote gate is ON the candidate page, stacked, no overlap', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  const gate = allByClass(host, 'dn-gate')[0];
  assert(gate, 'a promote-gate panel rendered on the candidate page');
  const rules = allByClass(host, 'dn-rule');
  assert(rules.length >= 3, 'each gate rule is its own row (3 short-circuiting rules)');
  // The FINAL liked study (single-generation opt 2) DROPPED the scalar-component
  // bars as redundant with the radar silhouette — they must be GONE.
  assertEqual(allByClass(host, 'dn-sc-table').length, 0, 'the scalar-components block is REMOVED (folded into the radar)');
  assert(host.textContent.includes('Scalar margin'), 'a rule label present');
});

// ---- #19: scalar-provenance decomposition on the gate panel -------------

test('#19 candidate gate: the scalar decomposition names the transform that shaped each side', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  // the decomposition block rendered (the v0/v1 gate fixture carries transforms).
  const decomp = allByClass(host, 'dn-scalar-decomp')[0];
  assert(decomp, 'the scalar-provenance decomposition rendered when a transform fired');
  assert(decomp.textContent.includes('Scalar provenance'), 'the decomposition is headed');
  // the challenger pass term came from pow(2.0); its drift from a drift transform.
  assert(decomp.textContent.includes('pow(2.0)'), 'the pass transform token is named');
  assert(decomp.textContent.includes('drift transform') || decomp.textContent.includes('looping_reasoning'),
    'the drift transform is named');
  // no fail-open here → no caution banner.
  assertEqual(allByClass(host, 'dn-decomp-banner').length, 0, 'no fail-open banner when nothing failed open');
  assertEqual(allByClass(host, 'dn-decomp-failopen').length, 0, 'no fail-open row when nothing failed open');
});

test('#19 candidate gate: a FAIL-OPEN plugin is flagged prominently (banner + caution row)', async () => {
  freshState();
  const F = { ...FIXTURE };
  // Override the v0/v1 gate so the challenger's Seam-2 plugin FAILED OPEN.
  F[`/api/round/${EPOCH_ID}/v0/v1/gate`] = {
    ...FIXTURE[`/api/round/${EPOCH_ID}/v0/v1/gate`],
    scalar_decomposition: { present: true, fail_open: true,
      champion: { scalar: { present: true, kind: 'builtin', source: 'built-in formula', transforms: [], fail_open: false, fallback_reason: null },
                  drift: { present: true, kind: 'builtin', source: 'built-in formula', transforms: [], fail_open: false, fallback_reason: null } },
      challenger: { scalar: { present: true, kind: 'builtin', source: 'built-in formula', transforms: [], fail_open: true, fallback_reason: 'raised ValueError' },
                    drift: { present: true, kind: 'builtin', source: 'built-in formula', transforms: [], fail_open: false, fallback_reason: null } } },
  };
  installFixtureMap(F);
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  // The fail-open caution banner is FIRST-CLASS — present + names the failure.
  const banner = allByClass(host, 'dn-decomp-banner')[0];
  assert(banner, 'a fail-open caution banner rendered prominently');
  assert(banner.textContent.includes('FAILED OPEN'), 'the banner calls out the fail-open event');
  // the offending seam row is caution-flagged and carries the reason.
  const failRow = allByClass(host, 'dn-decomp-failopen')[0];
  assert(failRow, 'the failed-open seam row is caution-flagged');
  assert(failRow.textContent.includes('raised ValueError'), 'the fallback reason is surfaced on the row');
});

test('#19 candidate gate: a pre-#19 / built-in round renders NO decomposition (back-compat clean)', async () => {
  freshState();
  const F = { ...FIXTURE };
  // A pre-#19 gate payload: no scalar_decomposition key at all.
  const { scalar_decomposition: _drop, ...noProv } = FIXTURE[`/api/round/${EPOCH_ID}/v0/v1/gate`];
  F[`/api/round/${EPOCH_ID}/v0/v1/gate`] = noProv;
  installFixtureMap(F);
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  // the gate still renders, but the provenance block is absent (nothing new).
  assert(allByClass(host, 'dn-gate')[0], 'the gate panel still renders on a pre-#19 round');
  assertEqual(allByClass(host, 'dn-scalar-decomp').length, 0, 'no decomposition block on a pre-#19 / built-in round');
  assertEqual(allByClass(host, 'dn-decomp-banner').length, 0, 'no caution banner on a pre-#19 round');
});

test('#19 candidate digest: a no-op heartbeat over a gate WITH provenance churns NO DOM', async () => {
  freshState(); installFetch();   // the BASE fixture carries the v1 decomposition.
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  const digest1 = host.getAttribute('data-t-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  assertEqual(host.getAttribute('data-t-digest'), digest1, 'digest unchanged on a no-op beat over a provenance-bearing gate');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op beat');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op beat (provenance folded, not churned)');
});

// ---- the FINAL liked dossier: radar silhouette folded in, scalar-bars out ----

test('candidate view: the RADAR SILHOUETTE is folded in (candidate vs champion across the gate axes); scalar-bars GONE', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  // the radar silhouette renders (svg.radarSilhouette → .dn-radar) inside its pane.
  assert(allByClass(host, 'dn-radar')[0], 'the radar silhouette SVG rendered on the candidate page');
  assert(allByClass(host, 'dn-radarpane')[0], 'the radar sits in its width-capped pane');
  // each axis carries a hover hit-target so the operator can read the value.
  assert(allByClass(host, 'dn-radar-hot').length >= 3, 'the radar exposes ≥3 hover-able axis vertices');
  // the removed scalar-component bars must not reappear anywhere on the page.
  assertEqual(allByClass(host, 'dn-sc-table').length, 0, 'no scalar-component table anywhere on the dossier');
  assert(!host.textContent.includes('Scalar components'), 'no "Scalar components" heading');
});

// ---- the radar carries MEANINGFUL axis LABELS (not 1–9 indices) ----
// The operator flagged "the radar chart is missing labels" (it showed axis
// indices). candidate.js builds + passes `axes[].label` — scalar / pass-rate /
// each per-judge (gate scalar_components) — so the silhouette names its axes.
test('candidate view: the radar silhouette names its axes (scalar / pass-rate / per-component), NOT numeric 1–9 indices', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  const radar = svgsByClass(host, 'dn-radar')[0];
  assert(radar, 'the radar silhouette SVG rendered');
  // the axis-label texts (dn-radar-axislab) — the meaningful names, not ticks.
  const labelEls = allByClass(radar, 'dn-radar-axislab');
  const labels = labelEls.map((n) => (n.textContent || '').trim()).filter(Boolean);
  assert(labels.length >= 3, 'the radar paints text axis labels (≥3 named axes), not bare index ticks');
  // the gate-weighed axes the model builds: scalar (inverse), pass-rate, + each
  // per-component from gate.scalar_components (here: drift, schema). At least the
  // scalar + pass-rate axes must carry their real names.
  assert(labels.includes('scalar'), 'the scalar axis is labeled "scalar"');
  assert(labels.includes('pass-rate'), 'the pass-rate axis is labeled "pass-rate"');
  assert(labels.includes('drift') || labels.includes('schema'),
    'a per-component (gate scalar_components) axis carries its component name');
  // none of the rendered axis LABELS is a bare numeric index (the 1–9 bug).
  assert(!labels.some((l) => /^\d+$/.test(l)), 'no axis label is a bare numeric index (1–9)');
  // and no numeric index-tick fallback is used while there are ≤8 named axes.
  assertEqual(allByClass(radar, 'dn-radar-axistick').length, 0,
    'no numeric index-tick fallback while the axes are within the labeled range');
});

// ---- the dossier is REORGANISED per the study (coordinated, not sprawling) ----
// The study folds the per-board read + gate ladder + labeled radar into ONE
// coordinated grid beneath the full-width lifecycle spine. Assert the sections
// are present AND arranged: a 2-column dossier grid (per-board + gate LEFT,
// silhouette RIGHT), with the lifecycle spine above and generalization below.
test('candidate view: the dossier reads as one organized layout — coordinated grid (per-board + gate | radar), spine above, generalization below', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  // the coordinated grid exists with a main (per-board + gate) + side (radar) column.
  const grid = allByClass(host, 'dn-dossier-grid')[0];
  assert(grid, 'the dossier body is a coordinated grid (not a flat full-bleed stack)');
  assert(allByClass(host, 'dn-dossier-col--main')[0], 'the grid has a MAIN column (per-board + gate ladder)');
  assert(allByClass(host, 'dn-dossier-col--side')[0], 'the grid has a SIDE column (the radar silhouette)');
  // single (non-compare) view → the WIDE grid (not the narrow compare collapse).
  assertEqual(allByClass(host, 'dn-dossier-grid--narrow').length, 0,
    'the single-candidate dossier uses the wide 2-column grid (not the narrow collapse)');
  // the SIDE column holds the radar; the MAIN column holds the per-board DUMBBELL
  // (responsive, width-filling) + the gate ladder.
  const side = allByClass(host, 'dn-dossier-col--side')[0];
  assert(svgsByClass(side, 'dn-radar')[0], 'the radar sits in the side column');
  const main = allByClass(host, 'dn-dossier-col--main')[0];
  const dot = svgsByClass(main, 'dn-dumbbell')[0];
  assert(dot, 'the per-board champion○ → candidate● dumbbell sits in the main column');
  assertEqual(dot.getAttribute('width'), '100%', 'the per-board dumbbell is width-filling (responsive, not crammed right)');
  assert(allByClass(main, 'dn-gate')[0], 'the promote-gate ladder sits in the main column beside the per-board read');
  // the lifecycle spine reads ABOVE the grid; the generalization slope BELOW it.
  assert(host.textContent.includes('Lifecycle · cause → effect → verdict'), 'the lifecycle spine section reads above the grid');
  assert(allByClass(host, 'dn-genpane')[0] || !host.textContent.includes('Generalization'),
    'the generalization slope is a small width-capped supporting panel when present');
});

// ---- a RACING / in-flight candidate shows a PROJECTED radar + the affordance ----
// While a candidate is racing (only a projected scalar / partial board slice) the
// dossier must not read bare: it shows a clearly-marked projected/ghosted radar
// and a "settled comparisons appear once boards finish" affordance, with the
// settled dumbbell/gate comparisons gated on landed data.
test('candidate view (RACING): an in-flight candidate ghosts a PROJECTED radar + surfaces the racing affordance (not a bare dossier)', async () => {
  freshState(); installFetch();
  // an in-flight racer v3 (champion v0) with NO settled scalar yet — only a live
  // PROJECTED standing — but a recorded gate (scalar_components) so the silhouette
  // forms ≥3 axes and can ghost. Per-entry has ONE landed board (pass_fail) so a
  // pass-rate axis lands too; the rest stream.
  const F = { ...FIXTURE };
  F['/api/epoch'] = { ...FIXTURE['/api/epoch'],
    tournament: { structure: 'racing', params: {} },
    experiments: [...FIXTURE['/api/epoch'].experiments, { generation_id: 'v3', parent_generation_id: 'v0', outcome: {}, decision: null, promoted: null }] };
  F['/api/lineage'] = { generations: [...FIXTURE['/api/lineage'].generations,
    { generation_id: 'v3', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false }] };
  // v3 is NOT in the score-trajectory → no settled scalar (it is racing).
  F[`/api/generation/${EPOCH_ID}/v3/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v3', entries: [
    { entry_id: 'waffles_single', run_id: 'run_v3_waffles', drift_loss: 58.0, pass_fail: true, runtime_ms: 120000, wall_clock_budget_exceeded: false },
  ] };
  F[`/api/round/${EPOCH_ID}/v0/v3/gate`] = { decision: 'pending', delta_scalar: -2.0, delta_pass_rate: 0.5,
    rules: [{ id: 'scalar_margin', label: 'Scalar margin', status: 'not_reached', fired: false }],
    scalar_components: { champion: { drift: 68.5, schema: 1.43 }, challenger: { drift: 60.0, schema: 1.0 } } };
  globalThis.fetch = async (path) => {
    const v = lookupFixture(F, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
  };
  // the LIVE active tournament (racing) for THIS epoch with a projected standing
  // for v3 — boards still streaming (3 of 8 scored).
  coreState.state.activeTournament = { epoch_id: EPOCH_ID, structure: 'racing',
    projected: { v3: { scalar: 60.0, boards_done: 3, boards_total: 8 } } };
  coreState.state.heartbeat = { phase: 'tournament:running', epoch_id: EPOCH_ID, ts: Date.now() };
  coreState.state.activeRuns = [{ generation_id: 'v3', entry_id: 'picky_stakeholder_emulated', run_id: 'run_v3_picky' }];
  try {
    const candidate = await import('../js/views/candidate.js');
    const host = document.createElement('div');
    await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v3' });
    // the radar still renders (the projected silhouette), GHOSTED via dn-proj.
    const radar = svgsByClass(host, 'dn-radar')[0];
    assert(radar, 'a projected radar silhouette renders for the in-flight candidate (not omitted)');
    const ghosted = allByClass(radar, 'dn-radar-cand').some((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-proj'));
    assert(ghosted, 'the candidate polygon is GHOSTED (dn-proj) — clearly marked projected');
    // it still names its axes (labels, not indices).
    const labels = allByClass(radar, 'dn-radar-axislab').map((n) => (n.textContent || '').trim()).filter(Boolean);
    assert(labels.includes('scalar'), 'the projected radar still labels its scalar axis');
    // the racing affordance surfaces so the dossier is not bare.
    assert(allByClass(host, 'dn-racing-affordance')[0], 'the "settled comparisons appear once boards finish" affordance is shown');
    assert(/settled comparisons/i.test(host.textContent), 'the affordance names what is pending (settled comparisons)');
    // the headline reads a PROJECTED (not settled) scalar.
    assert(allByClass(host, 'dt-proj')[0], 'the dossier marks the projected (in-flight) treatment');
    // the live in-flight board panel still reads ("N board running").
    assert(/board running/i.test(host.textContent), 'the live in-flight board panel still reads for the racing candidate');
  } finally {
    coreState.state.activeTournament = null;
    coreState.state.heartbeat = null;
    coreState.state.activeRuns = [];
  }
});

// ---- FIX #2: patch node → per-candidate side-by-side diff ----------

test('candidate view: the lifecycle PATCH node is clickable → the per-candidate diff route', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  const patch = allByClass(host, 'ezn-clickable')[0];
  assert(patch, 'the lifecycle patch node is clickable (fix #2)');
  patch.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'diff' && navTo.p.gen === 'v1' && navTo.p.epochId === EPOCH_ID, 'patch click routes to this candidate’s diff');
});

test('diff view: the per-candidate side-by-side diff renders REAL strings (not "[object Object]")', async () => {
  freshState(); installFetch();
  const diff = await import('../js/views/diff.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await diff.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  assert(host.textContent.includes('Patch diff · v1'), 'the per-candidate diff heading');
  const sxs = allByClass(host, 'dn-sxs')[0];
  assert(sxs, 'a side-by-side diff component rendered (reused from the mutation viewer)');
  assert(host.textContent.includes('Draft an outline'), 'baseline.content (LEFT) — the real STRING');
  assert(host.textContent.includes('Always emit an explicit slide structure'), 'challenger new_content (RIGHT) — the real STRING');
  assert(!host.textContent.includes('[object Object]'), 'never the baseline OBJECT');
});

// ---- FIX #3: ALL match-ups for a candidate -------------------------

test('candidate view: v0 shows ALL its match-ups (v0→v1 AND v0→v2), not just one', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v0' });
  const txt = host.textContent;
  assert(txt.includes('v0 → v1'), 'the v0→v1 round shown');
  assert(txt.includes('v0 → v2'), 'the v0→v2 round shown');
});

// ---- NEW (round 6): side-by-side COMPARE splits the detail ---------

test('candidate view: "compare with…" SPLITS the detail into TWO candidates side by side', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');

  // single candidate first — the compare affordance is present, no split yet.
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  assert(allByClass(host, 'dt-cmp-picker')[0], 'the "compare with…" picker is present');
  assert(allByClass(host, 'dt-split-single')[0], 'no compare target → the frame is single-column');

  // now pass the compare target — the detail splits into two candidate panels.
  freshState(); installFetch();
  const host2 = document.createElement('div');
  await candidate.render(host2, ctx, { epochId: EPOCH_ID, gen: 'v1' }, { params: { epochId: EPOCH_ID, gen: 'v1' }, cmp: 'v2' });
  assert(host2.textContent.includes('Candidate v1') && host2.textContent.includes('vs  v2'), 'the page head names both candidates');
  const split = allByClass(host2, 'dt-split')[0];
  assert(split && !(split.getAttribute('class') || '').includes('dt-split-single'), 'the frame is a two-column split');
  const sides = allByClass(host2, 'dt-split-side');
  assert(sides.length === 2, 'TWO candidate panels (A and B) side by side');
  // each side carries its own lifecycle + gate (S's comparison-first detail).
  assert(allByClass(host2, 'dn-gate').length >= 1, 'a promote gate appears within the split');
});

// the compare panes are EQUAL-WIDTH columns, so BOTH lifecycle DAGs must use the
// SAME (narrow) viewBox width — otherwise the fit-to-width B pane scales down vs
// A and renders smaller with an empty top band. The DAG width is keyed on the
// SPLIT-LAYOUT flag (true for both A and B), not the per-side cmpId (null on B).
function dagViewBoxWidths(host) {
  return allByClass(host, 'ezn-dag').map((svg) => {
    const vb = (svg.getAttribute('viewBox') || '').split(/\s+/);
    return Number(vb[2]);
  });
}

test('candidate COMPARE view: BOTH lifecycle DAGs share the SAME (narrow, 560) viewBox width', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' }, { params: { epochId: EPOCH_ID, gen: 'v1' }, cmp: 'v2' });

  const widths = dagViewBoxWidths(host);
  assert(widths.length === 2, 'two lifecycle DAGs (A and B) in the compare view');
  assertEqual(widths[0], widths[1], 'the A and B DAGs share an identical viewBox width (equal scale, no shrunken B pane)');
  assertEqual(widths[0], 560, 'both compare panes use the NARROW 560-unit viewBox');
});

test('candidate SINGLE view: the lone lifecycle DAG uses the WIDE (900) viewBox width', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });

  const widths = dagViewBoxWidths(host);
  assert(widths.length === 1, 'a single lifecycle DAG in the non-compare view');
  assertEqual(widths[0], 900, 'the single-candidate view keeps the WIDE 900-unit viewBox');
});

// ---- FIX #4 + #5: board reachable from tree; INLINE side-by-side transcript ----

test('board view: reachable from the tree and selecting a run shows the transcript INLINE side by side', async () => {
  freshState(); installFetch();
  const host = document.createElement('div');
  const model = { epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: { gens: [{ id: 'v0', promoted: true, parent: null }], boards: [{ id: 'waffles_single' }] } } };
  let navTo = null;
  const treeCtx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  const toggles = new Set(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + '/boards']);
  tree.buildTree(host, model, router.parseRoute(`#/e/${EPOCH_ID}/boards`), toggles, treeCtx, () => {});
  const boardLeaf = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-leaf') && n.getAttribute('data-kind') === 'board')[0];
  assert(boardLeaf, 'a Boards leaf exists in the tree');
  const leafBtn = boardLeaf.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-label'))[0];
  leafBtn.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'board' && navTo.p.entry === 'waffles_single', 'the tree Boards leaf routes to the per-board view by entry id');

  freshState(); installFetch();
  const board = await import('../js/views/board.js');
  const bhost = document.createElement('div');
  await board.render(bhost, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  assert(bhost.textContent.includes('Board · waffles_single'), 'the per-board heading (still the board view)');
  const xgrid = allByClass(bhost, 'dt-split')[0];
  assert(xgrid, 'the INLINE side-by-side transcript pane rendered within the board view');
  const cols = allByClass(bhost, 'dn-xscript-col');
  assert(cols.length === 2, 'two candidates’ transcripts side by side');
  assert(bhost.textContent.includes('Drafting an outline'), 'the selected run’s transcript turn rendered INLINE (no route away)');
});

test('board view: a candidate row links INLINE (to board+gen), never to a separate run page', async () => {
  freshState(); installFetch();
  const board = await import('../js/views/board.js');
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });
  const runLink = allByClass(host, 'dn-board-run')[0];
  assert(runLink, 'a per-candidate transcript link exists');
  const href = runLink.getAttribute('href') || '';
  assert(href.includes('/board/'), 'the link stays on the board view (inline), not a /run/ page');
  assert(!href.includes('/run/'), 'no navigation to a separate run page');
});

// ---- successive-halving REUSE champion transcript: gen×entry fallback ----

// Install a fetch that serves the base FIXTURE but lets a test OVERRIDE or
// SUPPRESS specific paths — the reuse-champion case needs the champion's
// /api/conversation to come back empty (the score-reuse run_id has no events
// of its own) while a by-(epoch, gen, entry) transcript exists.
function installFetchWith(overrides, suppress) {
  const sup = new Set(suppress || []);
  globalThis.fetch = async (path) => {
    const base = path.indexOf('?') >= 0 ? path.slice(0, path.indexOf('?')) : path;
    if (Object.prototype.hasOwnProperty.call(overrides, base)) {
      return { ok: true, json: async () => overrides[base] };
    }
    if (sup.has(base)) return { ok: false, status: 404, json: async () => ({ error: 'suppressed: ' + base }) };
    const v = lookupFixture(FIXTURE, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}

test('board view: a REUSED champion run (no own transcript) falls back to the gen×entry transcript', async () => {
  freshState();
  // The champion v0's per-entry run_id is a successive-halving REUSE record:
  // /api/conversation/run_v0_waffles yields NO transcript. But the gen×entry
  // /api/run/<epoch>/v0/waffles_single/transcript resolves the one real
  // events.jsonl on disk. The champion side must render THAT, not "unavailable".
  installFetchWith(
    {
      [`/api/run/${EPOCH_ID}/v0/waffles_single/transcript`]: {
        epoch_id: EPOCH_ID, generation_id: 'v0', entry_id: 'waffles_single', run_id: 'real_v0_run',
        turns: [
          { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
          { seq: 1, role: 'agent', agent: 'coordinator', text: 'Champion reused-rung transcript recovered.' },
        ],
        annotations: [], event_count: 31, complete: true,
      },
    },
    ['/api/conversation/run_v0_waffles'],
  );
  const board = await import('../js/views/board.js');
  const bhost = document.createElement('div');
  await board.render(bhost, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  const cols = allByClass(bhost, 'dn-xscript-col');
  assert(cols.length === 2, 'two transcript columns (challenger + champion)');
  // Challenger (v1) side unchanged — its own /api/conversation still resolves.
  assert(bhost.textContent.includes('Drafting an outline'), 'challenger transcript still renders from its own run_id');
  // Champion (v0) side recovered via the gen×entry fallback, NOT "unavailable".
  assert(bhost.textContent.includes('Champion reused-rung transcript recovered'),
    'champion transcript recovered via the gen×entry fallback');
  assert(!bhost.textContent.includes('could not be reconstructed'),
    'the honest "unavailable" message is NOT shown when a gen×entry transcript exists');
});

test('board view: a GENUINELY-absent champion transcript still shows the honest "unavailable" message', async () => {
  freshState();
  // Both the reuse run_id AND the gen×entry transcript are absent — the
  // honest "unavailable" message must remain (no false recovery).
  installFetchWith(
    {},
    ['/api/conversation/run_v0_waffles', `/api/run/${EPOCH_ID}/v0/waffles_single/transcript`],
  );
  const board = await import('../js/views/board.js');
  const bhost = document.createElement('div');
  await board.render(bhost, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  const cols = allByClass(bhost, 'dn-xscript-col');
  assert(cols.length === 2, 'two transcript columns');
  assert(bhost.textContent.includes('Drafting an outline'), 'challenger side unchanged');
  assert(bhost.textContent.includes('could not be reconstructed'),
    'the honest "unavailable" message is preserved for a genuinely-absent gen×entry');
});

test('board view: BOTH sides resolve by (epoch, gen, entry) PRIMARY even when the per-record run_id has no events', async () => {
  freshState();
  // The deterministic triple is the primary key: BOTH the challenger (v1)
  // and the champion (v0) resolve via /api/run/<epoch>/<gen>/<entry>/transcript
  // even though NEITHER per-entry run_id resolves through /api/conversation
  // (both are reuse / index-only records with no events of their own). The
  // panes must render both transcripts from the gen×entry events.jsonl —
  // never the run_id-first path.
  installFetchWith(
    {
      [`/api/run/${EPOCH_ID}/v1/waffles_single/transcript`]: {
        epoch_id: EPOCH_ID, generation_id: 'v1', entry_id: 'waffles_single', run_id: 'real_v1_run',
        turns: [
          { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
          { seq: 1, role: 'agent', agent: 'coordinator', text: 'Challenger by-triple transcript.' },
        ],
        annotations: [], event_count: 12, complete: true,
      },
      [`/api/run/${EPOCH_ID}/v0/waffles_single/transcript`]: {
        epoch_id: EPOCH_ID, generation_id: 'v0', entry_id: 'waffles_single', run_id: 'real_v0_run',
        turns: [
          { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
          { seq: 1, role: 'agent', agent: 'coordinator', text: 'Champion by-triple transcript.' },
        ],
        annotations: [], event_count: 31, complete: true,
      },
    },
    // Both run_id-keyed lookups are suppressed — the run_id-first path would 404.
    ['/api/conversation/run_v1_waffles', '/api/conversation/run_v0_waffles'],
  );
  const board = await import('../js/views/board.js');
  const bhost = document.createElement('div');
  await board.render(bhost, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  const cols = allByClass(bhost, 'dn-xscript-col');
  assert(cols.length === 2, 'two transcript columns');
  assert(bhost.textContent.includes('Challenger by-triple transcript'),
    'challenger side resolved by the (epoch, gen, entry) triple, not its run_id');
  assert(bhost.textContent.includes('Champion by-triple transcript'),
    'champion side resolved by the (epoch, gen, entry) triple, not its run_id');
  assert(!bhost.textContent.includes('could not be reconstructed'),
    'no honest-absence message when the gen×entry transcript exists for both sides');
});

test('board view: the per-pane transcript host split (live-beat scroll fix) is preserved', async () => {
  freshState(); installFetch();
  const board = await import('../js/views/board.js');
  const bhost = document.createElement('div');
  await board.render(bhost, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  // The two persistent sub-hosts (upper + transcript) must exist — the digest
  // split that keeps a live beat from resetting the transcript scroll.
  assert(bhost.querySelectorAll('[data-node]').filter((n) => n.getAttribute('data-node') === 'board-upper').length === 1,
    'the upper (live) sub-host exists');
  assert(bhost.querySelectorAll('[data-node]').filter((n) => n.getAttribute('data-node') === 'board-xscript').length === 1,
    'the transcript sub-host exists (separate from the upper host)');
});

// ---- LIVE TRANSCRIPT: a RUNNING candidate streams its transcript inline ----
//
// In every tournament mode the operator can select a candidate that is
// currently RUNNING on a board entry and read its transcript as the run
// produces turns. The active-runs feed (structure-agnostic) carries the
// running candidate's run_id / generation_id; its events.jsonl is already
// growing on disk, so the gen×entry transcript resolves PARTIALLY mid-flight.

// A fetch whose run-transcript / conversation response can be SWAPPED between
// renders, so a test can simulate a live transcript GROWING a turn. `getRun`
// returns the current run-transcript payload (keyed by the base path).
function installGrowableFetch(runPayloads, suppress) {
  const sup = new Set(suppress || []);
  globalThis.fetch = async (path) => {
    const base = path.indexOf('?') >= 0 ? path.slice(0, path.indexOf('?')) : path;
    if (Object.prototype.hasOwnProperty.call(runPayloads, base)) {
      const v = runPayloads[base];
      return { ok: true, json: async () => (typeof v === 'function' ? v() : v) };
    }
    if (sup.has(base)) return { ok: false, status: 404, json: async () => ({ error: 'suppressed: ' + base }) };
    const v = lookupFixture(FIXTURE, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}

// (a) A RUNNING candidate (active-run carries run_id; NO loss.json / no
// per-entry row) resolves a PARTIAL transcript and is SELECTABLE.
test('board view (LIVE): a RUNNING candidate with no scored row resolves a PARTIAL transcript and is selectable', async () => {
  freshState();
  // v3 is RUNNING on waffles_single: it is in active-runs (carrying its
  // run_id) but has NO per-entry record in ANY generation's pivot — no
  // loss.json yet. Its partial transcript resolves by the (epoch, gen, entry)
  // triple from the still-growing events.jsonl.
  installGrowableFetch({
    [`/api/run/${EPOCH_ID}/v3/waffles_single/transcript`]: {
      epoch_id: EPOCH_ID, generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live',
      turns: [
        { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
        { seq: 1, role: 'agent', agent: 'coordinator', text: 'Live partial turn so far.' },
      ],
      annotations: [], event_count: 4, complete: false,  // PARTIAL: no terminal event yet
    },
  });
  // lineage carries v3 as a running challenger (so role/parent resolve).
  FIXTURE['/api/lineage'].generations.push({ generation_id: 'v3', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false });
  coreState.state.activeRuns = [{ generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live', progress: 0.4, epoch_id: EPOCH_ID }];

  const board = await import('../js/views/board.js');
  const ctx = { navigate() {}, href: router.href };

  // FIRST: render WITHOUT a selection — the running candidate is a SELECTABLE
  // breakdown row ("watch live →" linking to board+v3), not "no run".
  const unsel = document.createElement('div');
  await board.render(unsel, ctx, { epochId: EPOCH_ID, entry: 'waffles_single' });
  const links = allByClass(unsel, 'dn-board-run').map((a) => a.getAttribute('href') || '');
  assert(links.some((h) => h.includes('/board/') && h.includes('v3')), 'the RUNNING candidate v3 is a selectable transcript row');
  assert(unsel.textContent.includes('watch live'), 'the running candidate reads "watch live →"');

  // THEN: select it — its PARTIAL transcript renders (the still-growing
  // events.jsonl), not "unavailable", and reads as a live/streaming column.
  const bhost = document.createElement('div');
  await board.render(bhost, ctx, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v3' });
  assert(bhost.textContent.includes('Live partial turn so far'), 'the partial transcript of the running candidate rendered');
  assert(!bhost.textContent.includes('could not be reconstructed'), 'no honest-absence message for a running candidate with a partial transcript');
  assert(allByClass(bhost, 'dn-xscript-live')[0], 'the running candidate column carries a live marker');

  // cleanup the shared fixture mutation.
  FIXTURE['/api/lineage'].generations = FIXTURE['/api/lineage'].generations.filter((g) => g.generation_id !== 'v3');
  coreState.state.activeRuns = [];
});

// (b) LIVE GROWTH. A new turn APPENDS one node into the persistent scroller
// (never a thread rebuild); a progress-only / no-op beat writes ZERO DOM; and
// the refetch is GATED on a genuine progress-seq advance.
function leftScroller(host) {
  return host.querySelectorAll('[data-scroll-side]').filter((n) => n.getAttribute('data-scroll-side') === 'left')[0];
}
function growableRun(getCount) {
  return () => ({
    epoch_id: EPOCH_ID, generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live',
    turns: Array.from({ length: getCount() }, (_, i) => ({ seq: i, role: i % 2 ? 'agent' : 'user', agent: 'coordinator', text: 'turn #' + i })),
    annotations: [], event_count: getCount() * 2, complete: false,
  });
}

test('board view (LIVE): a NEW TURN appends one node (no thread rebuild); a no-op beat writes ZERO DOM', async () => {
  freshState();
  let turnCount = 2;
  installGrowableFetch({ [`/api/run/${EPOCH_ID}/v3/waffles_single/transcript`]: growableRun(() => turnCount) });
  FIXTURE['/api/lineage'].generations.push({ generation_id: 'v3', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false });
  coreState.state.activeRuns = [{ generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live', progress: 0.4, epoch_id: EPOCH_ID }];
  coreState.state.lastSeq = 10;  // a real orchestrator progress cursor.

  const board = await import('../js/views/board.js');
  const bhost = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  const params = { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v3' };

  await board.render(bhost, ctx, params);
  const scroller = leftScroller(bhost);
  assert(scroller, 'the live transcript scroller exists');
  const first = allByClass(scroller, 'dn-turn');
  assertEqual(first.length, 2, 'both partial turns rendered on first paint');
  const firstNode = first[0];
  const xhost = bhost.querySelectorAll('[data-node]').filter((n) => n.getAttribute('data-node') === 'board-xscript')[0];
  const structDigest = xhost.getAttribute('data-t-digest');

  // NO-OP beat: same seq, same turns. Reconcile finds no new turns → ZERO DOM;
  // the structure digest is unchanged (turn CONTENT is not folded into it).
  coreState.state.activeRuns = [{ generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live', progress: 0.7, epoch_id: EPOCH_ID }];
  await board.render(bhost, ctx, params);
  const afterBeat = allByClass(scroller, 'dn-turn');
  assertEqual(afterBeat.length, 2, 'a no-op beat appends NOTHING (zero DOM)');
  assert(afterBeat[0] === firstNode, 'the existing turn node is the SAME instance (no rebuild)');
  assertEqual(xhost.getAttribute('data-t-digest'), structDigest, 'the structure digest is unchanged on a no-op beat');

  // NEW-TURN beat: seq advances AND the transcript gains a turn → APPEND one node.
  turnCount = 3;
  coreState.state.lastSeq = 11;
  await board.render(bhost, ctx, params);
  const grown = allByClass(scroller, 'dn-turn');
  assertEqual(grown.length, 3, 'exactly ONE node appended for the new turn');
  assert(grown[0] === firstNode, 'pre-existing turn nodes were NOT rebuilt (append, not replace)');
  assert(scroller.textContent.includes('turn #2'), 'the newly-arrived turn rendered');

  FIXTURE['/api/lineage'].generations = FIXTURE['/api/lineage'].generations.filter((g) => g.generation_id !== 'v3');
  coreState.state.activeRuns = [];
});

// (b2) The refetch is GATED on a progress-seq advance: a beat at a STABLE seq
// re-reads nothing (the cached partial is reused), so a transcript that grew on
// disk does NOT surface until the orchestrator's liveness cursor moves.
test('board view (LIVE): the live transcript refetch is gated on a progress-seq ADVANCE', async () => {
  freshState();
  let turnCount = 1;
  installGrowableFetch({ [`/api/run/${EPOCH_ID}/v3/waffles_single/transcript`]: growableRun(() => turnCount) });
  FIXTURE['/api/lineage'].generations.push({ generation_id: 'v3', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false });
  coreState.state.activeRuns = [{ generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live', progress: 0.3, epoch_id: EPOCH_ID }];
  coreState.state.lastSeq = 20;

  const board = await import('../js/views/board.js');
  const bhost = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  const params = { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v3' };

  await board.render(bhost, ctx, params);
  const scroller = leftScroller(bhost);
  assertEqual(allByClass(scroller, 'dn-turn').length, 1, 'one turn on first paint');

  // The events file grew on disk, but the SEQ did not advance → no refetch; the
  // cached one-turn partial is reused (no growth).
  turnCount = 4;
  await board.render(bhost, ctx, params);
  assertEqual(allByClass(scroller, 'dn-turn').length, 1, 'a stable seq re-reads NOTHING (cached partial reused)');

  // The cursor advances → the refetch fires → the grown transcript surfaces.
  coreState.state.lastSeq = 21;
  await board.render(bhost, ctx, params);
  assertEqual(allByClass(scroller, 'dn-turn').length, 4, 'a seq advance refetches and the new turns append');

  FIXTURE['/api/lineage'].generations = FIXTURE['/api/lineage'].generations.filter((g) => g.generation_id !== 'v3');
  coreState.state.activeRuns = [];
});

// (b3) The LIVE caption lifecycle: "streaming — through turn N" tracks the turn
// count while running, then DISAPPEARS cleanly when the run completes and the
// final transcript replaces the partial.
test('board view (LIVE): the "streaming — through turn N" caption tracks the count and vanishes on completion', async () => {
  freshState();
  let turnCount = 2;
  installGrowableFetch({ [`/api/run/${EPOCH_ID}/v3/waffles_single/transcript`]: growableRun(() => turnCount) });
  FIXTURE['/api/lineage'].generations.push({ generation_id: 'v3', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false });
  coreState.state.activeRuns = [{ generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live', progress: 0.4, epoch_id: EPOCH_ID }];
  coreState.state.lastSeq = 30;

  const board = await import('../js/views/board.js');
  const ctx = { navigate() {}, href: router.href };
  const params = { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v3' };

  const bhost = document.createElement('div');
  await board.render(bhost, ctx, params);
  assert(allByClass(bhost, 'dn-xscript-live')[0], 'the running column carries the LIVE badge');
  const cap = bhost.querySelectorAll('[data-stream-count]')[0];
  assert(cap, 'the streaming caption is present while running');
  assertEqual((cap.textContent || '').trim(), 'streaming — through turn 2', 'the caption reads the current turn count');

  // a new turn lands → the caption count advances (same element, updated text).
  turnCount = 3;
  coreState.state.lastSeq = 31;
  await board.render(bhost, ctx, params);
  assertEqual((bhost.querySelectorAll('[data-stream-count]')[0].textContent || '').trim(), 'streaming — through turn 3', 'the caption tracks the new turn');

  // COMPLETION: the run leaves active-runs and lands a scored per-entry row +
  // a final (complete) transcript. The column is now settled → the caption is
  // gone and the LIVE badge is gone; the final transcript replaces the partial.
  coreState.state.activeRuns = [];
  const F = { [`/api/run/${EPOCH_ID}/v3/waffles_single/transcript`]: {
    epoch_id: EPOCH_ID, generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_final',
    turns: Array.from({ length: 3 }, (_, i) => ({ seq: i, role: i % 2 ? 'agent' : 'user', agent: 'coordinator', text: 'turn #' + i })),
    annotations: [], event_count: 6, complete: true,
  } };
  FIXTURE[`/api/generation/${EPOCH_ID}/v3/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v3',
    entries: [{ entry_id: 'waffles_single', run_id: 'run_v3_final', drift_loss: 58.0, pass_fail: true, wall_clock_budget_exceeded: false }] };
  installGrowableFetch(F);
  const bhost2 = document.createElement('div');
  await board.render(bhost2, ctx, params);
  assert(!bhost2.querySelectorAll('[data-stream-count]')[0], 'the streaming caption disappears once the run completes');
  assert(!allByClass(bhost2, 'dn-xscript-live')[0], 'the LIVE badge is gone on the settled column');
  assert(bhost2.textContent.includes('turn #2'), 'the final transcript renders cleanly (same turn structure)');

  delete FIXTURE[`/api/generation/${EPOCH_ID}/v3/per-entry`];
  FIXTURE['/api/lineage'].generations = FIXTURE['/api/lineage'].generations.filter((g) => g.generation_id !== 'v3');
  coreState.state.activeRuns = [];
});

// (b4) BACK-COMPAT: with NO in-flight run, a settled transcript view is
// byte-stable across a no-op beat — no caption chrome, no DOM churn.
test('board view (LIVE): a SETTLED transcript (no in-flight run) is unchanged across a no-op beat', async () => {
  freshState();
  installGrowableFetch({ [`/api/run/${EPOCH_ID}/v1/waffles_single/transcript`]: {
    epoch_id: EPOCH_ID, generation_id: 'v1', entry_id: 'waffles_single', run_id: 'run_v1',
    turns: [
      { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
      { seq: 1, role: 'agent', agent: 'coordinator', text: 'Settled final turn.' },
    ], annotations: [], event_count: 4, complete: true,
  } });
  coreState.state.activeRuns = [];  // NOTHING in flight.
  coreState.state.lastSeq = 5;

  const board = await import('../js/views/board.js');
  const ctx = { navigate() {}, href: router.href };
  const params = { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' };

  const bhost = document.createElement('div');
  await board.render(bhost, ctx, params);
  const scroller = leftScroller(bhost);
  assert(scroller, 'the settled transcript scroller exists');
  const turns0 = allByClass(scroller, 'dn-turn');
  assertEqual(turns0.length, 2, 'the settled transcript rendered its turns');
  assert(!bhost.querySelectorAll('[data-stream-count]')[0], 'a settled (non-running) column carries NO streaming caption');
  assert(!allByClass(bhost, 'dn-xscript-live')[0], 'a settled column carries NO LIVE badge');
  const xhost = bhost.querySelectorAll('[data-node]').filter((n) => n.getAttribute('data-node') === 'board-xscript')[0];
  const digest0 = xhost.getAttribute('data-t-digest');

  // a no-op beat (a heartbeat with nothing in flight) → byte-identical: same
  // turn nodes (same instances), same structure digest, zero DOM.
  await board.render(bhost, ctx, params);
  const turns1 = allByClass(scroller, 'dn-turn');
  assertEqual(turns1.length, 2, 'no turns added on a no-op beat');
  assert(turns1[0] === turns0[0] && turns1[1] === turns0[1], 'the settled turn nodes are the SAME instances (no rebuild)');
  assertEqual(xhost.getAttribute('data-t-digest'), digest0, 'the structure digest is byte-stable across the no-op beat');

  coreState.state.activeRuns = [];
});

// (b5) THE LAST TURN GROWING (the routine streaming case) + SCROLL DISCIPLINE.
// A merged reasoning turn (goldfive's llmCallStart→llmCallEnd folded into ONE
// turn) grows its text across two seqs, so the last rendered turn's signature
// flips while every earlier turn is byte-stable. That must update ONLY that node
// in place (never a full clear that clamps scrollTop to 0), and — on both the in-
// place path and a genuine rebuild — a bottom-pinned reader stays pinned while a
// scrolled-up reader keeps their offset.

// A running v3/waffles_single transcript whose turn texts a test controls; the
// first + last turn strings are read fresh on every render so a growth is
// simulated between renders. lastSeq must advance to clear the seq gate.
function installLastTurnGrowFetch(getFirst, getLast) {
  installGrowableFetch({ [`/api/run/${EPOCH_ID}/v3/waffles_single/transcript`]: () => ({
    epoch_id: EPOCH_ID, generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live',
    turns: [
      { seq: 0, role: 'user', agent: 'operator', text: getFirst() },
      { seq: 1, role: 'agent', agent: 'coordinator', text: getLast() },
    ], annotations: [], event_count: 4, complete: false,
  }) });
  FIXTURE['/api/lineage'].generations.push({ generation_id: 'v3', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false });
  coreState.state.activeRuns = [{ generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live', progress: 0.4, epoch_id: EPOCH_ID }];
}
function cleanupV3Live() {
  FIXTURE['/api/lineage'].generations = FIXTURE['/api/lineage'].generations.filter((g) => g.generation_id !== 'v3');
  coreState.state.activeRuns = [];
}
// The headless DOM has no layout, so the scroll metrics are absent (nearBottom
// then defaults to tail). Define them on the scroller INSTANCE via
// Object.defineProperty (writable so the view's `scrollTop = scrollHeight` pin
// still lands) to drive a genuine pinned / scrolled-up decision.
function fakeScrollMetrics(scroller, scrollHeight, clientHeight, scrollTop) {
  Object.defineProperty(scroller, 'scrollHeight', { value: scrollHeight, configurable: true, writable: true });
  Object.defineProperty(scroller, 'clientHeight', { value: clientHeight, configurable: true, writable: true });
  Object.defineProperty(scroller, 'scrollTop', { value: scrollTop, configurable: true, writable: true });
}

test('board view (LIVE): the LAST turn GROWING updates that node IN PLACE (prefix preserved, no full clear)', async () => {
  freshState();
  let lastText = 'partial reasoning so far…';
  installLastTurnGrowFetch(() => 'Make a presentation about waffles.', () => lastText);
  coreState.state.lastSeq = 40;

  const board = await import('../js/views/board.js');
  const bhost = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  const params = { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v3' };

  await board.render(bhost, ctx, params);
  const scroller = leftScroller(bhost);
  const before = allByClass(scroller, 'dn-turn');
  assertEqual(before.length, 2, 'both partial turns rendered on first paint');
  const prefixNode = before[0];
  const lastNodeOld = before[1];

  // the merged reasoning turn grows (same seq/role, longer text) → only the last
  // turn's signature flips. seq advances so the refetch fires.
  lastText = 'partial reasoning so far… now the completed, much longer reasoning turn.';
  coreState.state.lastSeq = 41;
  await board.render(bhost, ctx, params);
  const after = allByClass(scroller, 'dn-turn');
  assertEqual(after.length, 2, 'still exactly two turns (last replaced in place, not appended)');
  assert(after[0] === prefixNode, 'the PREFIX turn node is the SAME instance (no wholesale clear)');
  assert(after[1] !== lastNodeOld, 'the grown last turn is a FRESH node (re-rendered in place)');
  assert(scroller.textContent.includes('completed, much longer reasoning turn'), 'the grown text rendered');

  cleanupV3Live();
});

test('board view (LIVE): a BOTTOM-PINNED reader stays pinned across in-place growth AND a genuine rebuild', async () => {
  freshState();
  let firstText = 'Make a presentation about waffles.';
  let lastText = 'partial';
  installLastTurnGrowFetch(() => firstText, () => lastText);
  coreState.state.lastSeq = 50;

  const board = await import('../js/views/board.js');
  const bhost = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  const params = { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v3' };

  await board.render(bhost, ctx, params);
  const scroller = leftScroller(bhost);
  // pinned: (1000 - 800 - 200) = 0 ≤ slop.
  fakeScrollMetrics(scroller, 1000, 200, 800);

  // IN-PLACE growth path → the pin re-lands at the (fixed) scrollHeight.
  lastText = 'partial then a much longer grown reasoning turn.';
  coreState.state.lastSeq = 51;
  await board.render(bhost, ctx, params);
  assertEqual(scroller.scrollTop, 1000, 'bottom-pinned reader is re-pinned to the bottom after in-place growth');

  // GENUINE rebuild path (an EARLIER turn changes) → still re-pinned.
  fakeScrollMetrics(scroller, 1200, 200, 1000); // pinned again at the new height
  firstText = 'A completely different opening prompt turn.';
  coreState.state.lastSeq = 52;
  await board.render(bhost, ctx, params);
  assertEqual(scroller.scrollTop, 1200, 'bottom-pinned reader is re-pinned to the bottom after a genuine rebuild');

  cleanupV3Live();
});

test('board view (LIVE): a SCROLLED-UP reader is NOT yanked across in-place growth NOR a genuine rebuild', async () => {
  freshState();
  let firstText = 'Make a presentation about waffles.';
  let lastText = 'partial';
  installLastTurnGrowFetch(() => firstText, () => lastText);
  coreState.state.lastSeq = 60;

  const board = await import('../js/views/board.js');
  const bhost = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  const params = { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v3' };

  await board.render(bhost, ctx, params);
  const scroller = leftScroller(bhost);
  // scrolled up: (1000 - 100 - 200) = 700 > slop → NOT pinned.
  fakeScrollMetrics(scroller, 1000, 200, 100);

  // IN-PLACE growth path → scrollTop untouched.
  lastText = 'partial then a much longer grown reasoning turn.';
  coreState.state.lastSeq = 61;
  await board.render(bhost, ctx, params);
  assertEqual(scroller.scrollTop, 100, 'scrolled-up reader keeps their exact offset across in-place growth');

  // GENUINE rebuild path → prior offset restored, not clamped to 0.
  fakeScrollMetrics(scroller, 1200, 200, 100);
  firstText = 'A completely different opening prompt turn.';
  coreState.state.lastSeq = 62;
  await board.render(bhost, ctx, params);
  assertEqual(scroller.scrollTop, 100, 'scrolled-up reader keeps their offset across a genuine rebuild (never yanked to 0)');

  cleanupV3Live();
});

// (b6) The PER-ENTRY seq tracker. When the seq ADVANCES while a DIFFERENT entry
// is being viewed, returning to the first entry must still bust its cache and
// refetch — the seq moved since we last read THAT entry. A bare module scalar
// leaks the other entry's now-current seq into the returned entry, so the return
// sees "seq unchanged" and serves the warm-but-stale partial (the bug).
test('board view (LIVE): a seq advance while viewing another entry busts on return (per-entry seq tracker)', async () => {
  freshState();
  let aCount = 1;
  installGrowableFetch({
    [`/api/run/${EPOCH_ID}/v3/waffles_single/transcript`]: growableRun(() => aCount),
    [`/api/run/${EPOCH_ID}/v3/picky_stakeholder_emulated/transcript`]: () => ({
      epoch_id: EPOCH_ID, generation_id: 'v3', entry_id: 'picky_stakeholder_emulated', run_id: 'run_v3_live_b',
      turns: [{ seq: 0, role: 'user', agent: 'coordinator', text: 'B turn #0' }],
      annotations: [], event_count: 2, complete: false,
    }),
  });
  FIXTURE['/api/lineage'].generations.push({ generation_id: 'v3', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false });
  coreState.state.activeRuns = [
    { generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live', progress: 0.4, epoch_id: EPOCH_ID },
    { generation_id: 'v3', entry_id: 'picky_stakeholder_emulated', run_id: 'run_v3_live_b', progress: 0.4, epoch_id: EPOCH_ID },
  ];
  coreState.state.lastSeq = 70;

  const board = await import('../js/views/board.js');
  const ctx = { navigate() {}, href: router.href };

  // View entry A at seq 70 → fetches + caches its one-turn partial. (A's tracker = 70.)
  const hostA = document.createElement('div');
  await board.render(hostA, ctx, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v3' });
  assertEqual(allByClass(leftScroller(hostA), 'dn-turn').length, 1, 'entry A shows its one partial turn');

  // Switch to entry B while the cursor ADVANCES to 71. (A scalar tracker is now 71.)
  coreState.state.lastSeq = 71;
  const hostB = document.createElement('div');
  await board.render(hostB, ctx, { epochId: EPOCH_ID, entry: 'picky_stakeholder_emulated', gen: 'v3' });
  assertEqual(allByClass(leftScroller(hostB), 'dn-turn').length, 1, 'entry B shows its own partial turn');

  // Entry A grew on disk. Return to A — the seq is still 71 but A has NOT been
  // read since it was 70. A per-entry tracker (A=70 ≠ 71) BUSTS and refetches the
  // grown transcript; a scalar (71 === 71) would wrongly reuse A's stale cache.
  aCount = 3;
  const hostA2 = document.createElement('div');
  await board.render(hostA2, ctx, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v3' });
  assertEqual(allByClass(leftScroller(hostA2), 'dn-turn').length, 3,
    'returning to A after the seq advanced elsewhere refetches its grown transcript (per-entry bust)');

  cleanupV3Live();
});

// (c) The live card + transcript path is STRUCTURE-AGNOSTIC — it is driven by
// active-runs, not by any tournament structure. Verify for swiss + elim.
for (const structure of ['swiss', 'single_elim']) {
  test(`board view (LIVE): the live transcript path is structure-agnostic (${structure})`, async () => {
    freshState();
    const F = {
      '/api/epoch': {
        epoch_id: EPOCH_ID, closed: false, goal: 'g',
        tournament: { structure, params: {} },
        experiments: [{ generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } }],
        board: [{ entry_id: 'waffles_single', kind: 'single_turn', input_preview: 'x', budget_s: 180, weight: 1 }],
      },
      '/api/lineage': { generations: [
        { generation_id: 'v0', epoch_id: EPOCH_ID, parent_generation_id: '', promoted: true },
        { generation_id: 'v9', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
      ] },
      '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 70.94 }] },
      '/api/tournaments': { epoch_id: EPOCH_ID, structure, champion_lineage: ['v0'], matchups: [] },
      [`/api/run/${EPOCH_ID}/v9/waffles_single/transcript`]: {
        epoch_id: EPOCH_ID, generation_id: 'v9', entry_id: 'waffles_single', run_id: 'run_v9_live',
        turns: [{ seq: 0, role: 'agent', agent: 'coordinator', text: `${structure} live turn` }],
        annotations: [], event_count: 2, complete: false,
      },
    };
    installFixtureMap(F);
    coreState.state.activeRuns = [{ generation_id: 'v9', entry_id: 'waffles_single', run_id: 'run_v9_live', progress: 0.3, epoch_id: EPOCH_ID }];

    const board = await import('../js/views/board.js');
    const bhost = document.createElement('div');
    await board.render(bhost, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v9' });

    // C4: the running candidate surfaces via the breakdown's "N running" banner
    // + its live progress column (the separate in-flight table was merged).
    assert(bhost.textContent.includes('1 running'), `the breakdown surfaces the running candidate under ${structure}`);
    assert(bhost.textContent.includes(`${structure} live turn`), `the running candidate's live transcript renders under ${structure} (structure-agnostic)`);
    assert(allByClass(bhost, 'dn-xscript-live')[0], `the live marker renders under ${structure}`);

    coreState.state.activeRuns = [];
  });
}

// ---- FIX #6: trellis in the Boards view, NOT the epoch overview ----

test('de-dup: the trellis lives in the Boards view; the epoch overview has the heatmap only', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/views/epoch.js');
  const boards = await import('../js/views/boards.js');
  const ctx = { navigate() {}, href: router.href };

  const ehost = document.createElement('div');
  await epoch.render(ehost, ctx, { epochId: EPOCH_ID });
  assert(allByClass(ehost, 'dn-heatmap')[0], 'the epoch overview keeps the heatmap');
  assert(allByClass(ehost, 'dn-trellis').length === 0, 'the epoch overview has NO trellis (moved to Boards)');

  const bhost = document.createElement('div');
  await boards.render(bhost, ctx, { epochId: EPOCH_ID });
  assert(allByClass(bhost, 'dn-trellis')[0], 'the Boards view carries the trellis (small-multiples)');
  assert(allByClass(bhost, 'dn-heatmap').length === 0, 'the Boards view has NO heatmap (never both on one page)');
});

// ---- NEW (round 6): the FIXED back button renders into the MAIN pane ----

test('back button: navigates UP and renders the destination into the MAIN detail pane (rail unchanged)', async () => {
  freshState(); installFetch();
  // The shell uses the bare globals `location` / `window` / `HashChangeEvent`
  // (browser globals). Wire a live `location` whose hash setter re-fires the
  // registered hashchange listeners, so driving the back control behaves as in
  // a browser. This is test-harness plumbing only — the shell code is unchanged.
  const listeners = { hashchange: [] };
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  // a no-op EventSource so connectSSE() does not enter an endless reconnect
  // loop (which would keep the node event loop alive); harness plumbing only.
  globalThis.EventSource = function EventSource() { this.readyState = 0; this.addEventListener = () => {}; this.close = () => {}; };
  globalThis.EventSource.CLOSED = 2;
  globalThis.window = globalThis.window || {};
  globalThis.window.localStorage = globalThis.window.localStorage || { getItem() { return null; }, setItem() {} };
  globalThis.window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  const loc = { _hash: '' };
  Object.defineProperty(loc, 'hash', {
    get() { return this._hash; },
    set(v) { this._hash = v; for (const fn of (listeners.hashchange || [])) fn(); },
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => { for (const fn of (listeners.hashchange || [])) fn(); };

  // mount the real shell so the back button + the rail/detail hosts exist.
  const root = document.createElement('div');
  document.body.appendChild(root);
  loc._hash = `#/e/${EPOCH_ID}/gen/v1`;
  shell.mountShell(root);
  // let the async dispatch settle.
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  const rail = allByClass(root, 'dt-sidebar')[0];
  const detail = allByClass(root, 'dt-viewhost')[0];
  const backBtn = allByClass(root, 'dt-back')[0];
  assert(rail && detail && backBtn, 'the shell painted a rail, a detail pane, and a back button');

  // THE RESEARCH-PREVIEW PILL — a quiet product-status tag pinned NEXT TO the
  // "zıcato console" wordmark in the top bar (NOT a Settings card, NOT a lower-
  // right corner note). It lives inside the brand block, carries the "research"
  // / "preview" label STACKED on two lines, and is the OPPOSITE of the retired
  // light-up card: NO accent-tinted pulsing `dn-respreview` banner exists.
  const pill = allByClass(root, 'dt-respreview')[0];
  assert(pill, 'the research-preview pill is mounted in the shell (top-bar chrome)');
  const brand = allByClass(root, 'dt-brand')[0];
  assert(brand && allByClass(brand, 'dt-respreview')[0],
    'the research-preview pill sits next to the wordmark inside the brand block');
  assert((pill.textContent || '').toLowerCase().includes('research'),
    'the pill carries the "research" label');
  assert((pill.textContent || '').toLowerCase().includes('preview'),
    'the pill carries the "preview" label');
  const lines = allByClass(pill, 'dt-respreview-line');
  assert(lines.length === 2, 'the pill stacks "research" / "preview" on two lines');
  assert(allByClass(root, 'dn-respreview').length === 0,
    'the old light-up Settings research-preview banner is gone');
  const railBefore = rail.innerHTML !== undefined ? rail.textContent : '';
  assert(detail.textContent.includes('Candidate v1'), 'the detail pane starts on the candidate (v1)');

  // drive the back control: candidate → generations.
  shell.goBack(router.parseRoute(location.hash));
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  assertEqual(location.hash, `#/e/${EPOCH_ID}/gens`, 'back navigated UP to the generations group');
  // THE FIX: the destination view lands in the MAIN DETAIL pane, NOT the rail.
  assert(detail.textContent.toLowerCase().includes('generation'), 'the destination view rendered into the MAIN detail pane');
  // the rail host still holds the tree (it was not used as the back destination).
  assert(allByClass(rail, 'dt-tree')[0], 'the rail host is unchanged — still the navigation tree, not the destination view');
  assert(railBefore !== undefined, 'rail content captured');
});

// ---- pickers + digest no-op ----------------------------------------

test('pickers: typeface (T7 default, 12 finalized faces / 4 per mode) + colour (monokai default) switch + persist', () => {
  freshState();
  const root = document.createElement('div');
  assertEqual(ui.DEFAULT_COLOR, 'monokai', 'monokai is the default colour theme');
  // The DEFAULT typeface is now T7 · Google Sans Mono (the first Technical face).
  assertEqual(ui.DEFAULT_TYPE, 'T7', 'T7 (Google Sans Mono) is the default typeface');
  // TWELVE finalized options — FOUR per mode across THREE modes.
  assertEqual(ui.TYPE_OPTIONS.length, 12, 'exactly 12 typeface options');
  assertDeep(ui.TYPE_MODE_ORDER, ['technical', 'editorial', 'display'], 'three mode groups in order');
  for (const mode of ui.TYPE_MODE_ORDER) {
    assertEqual(ui.TYPE_OPTIONS.filter((o) => o.mode === mode).length, 4, 'four options in the ' + mode + ' group');
  }
  // the exact id roster lifted from the study.
  const typeIds = ui.TYPE_OPTIONS.map((o) => o.id);
  assertDeep(typeIds,
    ['T7', 'T9', 'T12', 'T14', 'E5', 'E7', 'E8', 'E15', 'D2', 'D12', 'D14', 'D5'],
    'the 12 ids match the operator\'s finalized picks');
  // every option carries the four font-role stacks.
  for (const o of ui.TYPE_OPTIONS) {
    for (const role of ['head', 'prose', 'data', 'code']) {
      assert(typeof o[role] === 'string' && o[role].length > 0, o.id + ' has a ' + role + ' font stack');
    }
  }
  // TYPE_THEMES keeps the back-compat [id, label] shape over the 12 options.
  assertEqual(ui.TYPE_THEMES.length, 12, 'TYPE_THEMES exposes all 12 as [id,label] pairs');
  assertEqual(ui.TYPE_THEMES[0][0], 'T7', 'TYPE_THEMES first id is the default T7');

  const colorIds = ui.COLOR_THEMES.map((t) => t[0]);
  assert(['monokai', 'solarized-dark', 'solarized-light'].every((c) => colorIds.includes(c)), 'the three original colour themes are kept');
  shell.applyTheme('solarized-dark', root);
  assertEqual(root.getAttribute('data-t-theme'), 'solarized-dark', 'colour applied to the T root');
  assertEqual(ui.readColor(), 'solarized-dark', 'colour persisted');
  // apply a finalized option id — it stamps data-t-type="<id>" and persists.
  shell.applyTypeface('E5', root);
  assertEqual(root.getAttribute('data-t-type'), 'E5', 'typeface option applied to the T root');
  assertEqual(ui.readType(), 'E5', 'typeface persisted');
  assertEqual(ui.normaliseColor('nonsense'), 'monokai', 'unknown colour → monokai');
  assertEqual(ui.normaliseType('nonsense'), 'T7', 'unknown typeface → T7 default');
  // LEGACY MIGRATION: the old mode ids map to a sensible finalized id in-group.
  assertEqual(ui.normaliseType('technical'), 'T7', 'legacy "technical" migrates to T7');
  assertEqual(ui.normaliseType('editorial'), 'E5', 'legacy "editorial" migrates to E5');
  assertEqual(ui.normaliseType('display'), 'D2', 'legacy "display" migrates to D2');
  assertEqual(ui.normaliseType('sans'), 'T7', 'the long-dropped Sans id falls back to T7');
  // typeOption resolves to the full option object (real faces).
  assertEqual(ui.typeOption('T7').label, 'T7 · Google Sans Mono', 'typeOption resolves the option object');
});

// ---- the brand wordmark: dotless ı + the accent dot CENTRED on its stem ----

test('brand wordmark: renders "zıcato" with a dotless ı (U+0131) and the accent dot is centred over its stem', async () => {
  freshState(); installFetch();
  const listeners = { hashchange: [] };
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  globalThis.EventSource = function EventSource() { this.readyState = 0; this.addEventListener = () => {}; this.close = () => {}; };
  globalThis.EventSource.CLOSED = 2;
  globalThis.window = globalThis.window || {};
  globalThis.window.localStorage = globalThis.window.localStorage || { getItem() { return null; }, setItem() {} };
  globalThis.window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  const loc = { _hash: '#/' };
  Object.defineProperty(loc, 'hash', {
    get() { return this._hash; },
    set(v) { this._hash = v; for (const fn of (listeners.hashchange || [])) fn(); },
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => { for (const fn of (listeners.hashchange || [])) fn(); };

  const root = document.createElement('div');
  document.body.appendChild(root);
  shell.mountShell(root);
  await new Promise((r) => setTimeout(r, 0));

  // the wordmark is an inline SVG (.dt-brand-name) — not a styled text span —
  // so the dot can be pinned to the glyph stem + inherit theme tokens.
  const mark = svgsByClass(root, 'dt-brand-name')[0];
  assert(mark && mark.localName === 'svg', 'the wordmark renders as an inline SVG');

  // the letters render the DOTLESS ı (U+0131), never a dotted "i".
  const text = allByClass(mark, 'dt-brand-letters')[0];
  assert(text, 'the wordmark has a letters <text> element');
  assertEqual(text.textContent, shell.WORDMARK_TEXT, 'the wordmark text is the brand string');
  assert(text.textContent.includes('ı'), 'the wordmark uses the dotless ı (U+0131)');
  assert(!text.textContent.includes('i'), 'no dotted "i" in the wordmark');
  // the letters fill with currentColor (theme-adaptive), not a hardcoded colour.
  assertEqual(text.getAttribute('fill'), 'currentColor', 'the letters fill with currentColor (theme token)');

  // THE CENTERING GUARANTEE: the accent dot's cx EQUALS the computed ı stem
  // centre (pinned to the monospace advance grid). This is the exact assertion
  // the prior centering pain point demands — a number-equality, not an eyeball.
  const dot = allByClass(mark, 'dt-brand-dot')[0];
  assert(dot, 'the wordmark has the accent dot');
  assertEqual(Number(dot.getAttribute('cx')), shell.wordmarkDotCx(), 'the dot cx equals the ı stem centre');
  assertEqual(dot.getAttribute('fill'), 'var(--zicato-accent)', 'the dot fills with the accent token');
});

// The TYPEFACE picker has been REMOVED from the top-bar chrome (it lives ONLY in
// Settings → Appearance now). The top bar keeps the colour SWATCH DROPDOWN, the
// page-scale pill, and the status pill; and the wordmark dot stays centred for
// the FIXED brand mono regardless of the selected typeface (applyTypeface still
// works via the shared store + the Settings dropdown).
test('top bar: NO typeface picker (removed → Settings only); colour dropdown + scale + status remain; wordmark dot centred for the fixed brand mono', async () => {
  freshState(); installFetch();
  const listeners = { hashchange: [] };
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  globalThis.EventSource = function EventSource() { this.readyState = 0; this.addEventListener = () => {}; this.close = () => {}; };
  globalThis.EventSource.CLOSED = 2;
  globalThis.window = globalThis.window || {};
  globalThis.window.localStorage = globalThis.window.localStorage || { getItem() { return null; }, setItem() {} };
  globalThis.window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  const loc = { _hash: '#/' };
  Object.defineProperty(loc, 'hash', {
    get() { return this._hash; },
    set(v) { this._hash = v; for (const fn of (listeners.hashchange || [])) fn(); },
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => { for (const fn of (listeners.hashchange || [])) fn(); };

  const root = document.createElement('div');
  document.body.appendChild(root);
  shell.mountShell(root);
  await new Promise((r) => setTimeout(r, 0));

  const topbar = allByClass(root, 'dt-topbar')[0];
  assert(topbar, 'the top bar painted');
  // the TYPEFACE picker (grouped popover OR the old button group) is GONE from
  // the top bar — it lives ONLY in Settings → Appearance now.
  assertEqual(allByClass(topbar, 'dt-tf').length, 0, 'no typeface picker in the top bar (moved to Settings)');
  assertEqual(allByClass(topbar, 'dt-tf-trigger').length, 0, 'no typeface popover trigger in the top bar');
  assertEqual(allByClass(topbar, 'dt-tf-option').length, 0, 'no typeface option rows in the top bar');
  assertEqual(allByClass(topbar, 'dt-type-switch').length, 0, 'no legacy typeface button group in the top bar');
  assertEqual(allByClass(topbar, 'dt-type-btn').length, 0, 'no legacy typeface buttons in the top bar');
  // the COLOUR swatch dropdown is the SOLE dt-cd popover left in the top bar.
  const cds = allByClass(topbar, 'dt-cd');
  assertEqual(cds.length, 1, 'only the colour swatch dropdown (dt-cd) remains in the top bar');
  assert(allByClass(topbar, 'dt-cd-trigger')[0], 'the colour dropdown trigger is present');
  // the page-scale pill, the status pill, the settings link, and the brand stay.
  assert(allByClass(topbar, 'dt-scale-pill')[0], 'the page-scale pill is still in the top bar');
  assert(allByClass(topbar, 'dt-status')[0], 'the live-status pill is still in the top bar');
  assert(allByClass(topbar, 'dt-nav-build')[0], 'the settings link is still in the top bar');
  // the TOURNAMENT BUILDER is its own top-level view now — a discoverable nav
  // entry sits beside the ⚙ settings chip and links to the standalone `#/builder`.
  const navBuilder = allByClass(topbar, 'dt-nav-builder')[0];
  assert(navBuilder, 'the tournament-builder nav entry is in the top bar (beside settings)');
  assertEqual(navBuilder.getAttribute('href'), '#/builder', 'the builder nav entry links to the standalone builder view');
  assertEqual(navBuilder.getAttribute('href'), router.href('builder', {}), 'the builder nav href is the router-canonical link (single source of truth)');
  assert(allByClass(topbar, 'dt-brand')[0], 'the brand is still in the top bar');

  // applyTypeface still applies live (the shared store path is intact even with
  // no top-bar dropdown) — stamps data-t-type on the root.
  shell.applyTypeface('T9', root);
  assertEqual(root.getAttribute('data-t-type'), 'T9', 'applyTypeface("T9") still stamps data-t-type="T9" on the root');

  // the wordmark dot stays centred on the FIXED brand mono — switching the UI
  // typeface (the swappable --v2-mono) must NOT move the geometrically-pinned dot.
  const wm = svgsByClass(root, 'dt-brand-name')[0];
  const dot = allByClass(wm, 'dt-brand-dot')[0];
  const text = allByClass(wm, 'dt-brand-letters')[0];
  assertEqual(text.getAttribute('font-family'), 'var(--v2-brand-mono)', 'the wordmark pins to the FIXED brand mono');
  const cxBefore = Number(dot.getAttribute('cx'));
  shell.applyTypeface('display', root);
  shell.applyTypeface('editorial', root);
  assertEqual(Number(dot.getAttribute('cx')), cxBefore, 'the dot cx is unchanged across typeface switches');
  assertEqual(Number(dot.getAttribute('cx')), shell.wordmarkDotCx(), 'the dot cx still equals the computed brand-mono stem centre');
});

// THE STANDALONE BUILDER VIEW. `#/builder` is its own first-class view now
// (promoted out of Settings): the shell's view dispatcher renders it FULL-WIDTH
// in the main detail host (.dt-viewhost), NOT nested in the settings
// section-host. We mount the real shell, navigate to `#/builder`, and assert
// the builder's own chrome (.dn-builder) lands in the main view host with NO
// settings section-host wrapping it (the un-nesting / clutter fix).
test('view dispatcher: #/builder renders the builder full-width in the main view host (un-nested from settings)', async () => {
  freshState();
  const listeners = { hashchange: [] };
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  globalThis.EventSource = function EventSource() { this.readyState = 0; this.addEventListener = () => {}; this.close = () => {}; };
  globalThis.EventSource.CLOSED = 2;
  globalThis.window = globalThis.window || {};
  globalThis.window.localStorage = globalThis.window.localStorage || { getItem() { return null; }, setItem() {} };
  globalThis.window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  // a fetch that serves the env fixtures PLUS the builder's config + draft so
  // its render() resolves its panes (and a steady draft so the chrome paints).
  globalThis.fetch = async (path) => {
    if (String(path).startsWith('/builder/config')) {
      return { ok: true, json: async () => ({ chat_enabled: false, agent: {}, skills: [] }) };
    }
    if (String(path).startsWith('/builder/draft')) {
      return { ok: true, json: async () => ({ session: 'dashboard', draft: { scoring: { tournament: { structure: 'gauntlet', params: {} } }, board: [], holdout: { train_ids: [], holdout_ids: [] }, proposer: {} }, cost: { board_runs_per_round: 0, breakdown: [] }, warnings: [], diff: { changed_components: [], rolls_epoch: false } }) };
    }
    const v = lookupFixture(FIXTURE, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
  const loc = { _hash: '#/builder', search: '' };
  Object.defineProperty(loc, 'hash', {
    get() { return this._hash; },
    set(v) { this._hash = v; for (const fn of (listeners.hashchange || [])) fn(); },
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => { for (const fn of (listeners.hashchange || [])) fn(); };

  const root = document.createElement('div');
  document.body.appendChild(root);
  shell.mountShell(root);
  // let the async dispatch + the builder's config/draft fetch settle.
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  const viewhost = allByClass(root, 'dt-viewhost')[0];
  assert(viewhost, 'the main view host exists');
  // the builder mounted INSIDE the main view host (full-width), not a settings host.
  const builderRoot = allByClass(viewhost, 'dn-builder')[0];
  assert(builderRoot, 'the builder chrome (.dn-builder) rendered in the main view host');
  // it is NOT wrapped in the settings section-host (the un-nesting / clutter fix):
  // no .dn-settings surface and no settings section-rail in the view host.
  assertEqual(allByClass(viewhost, 'dn-settings').length, 0, 'the builder is NOT nested inside the settings surface');
  assertEqual(allByClass(viewhost, 'dn-set-rail').length, 0, 'no settings section-rail wraps the builder (no double rail)');
  // the builder kept its own four-pane chrome (its own rail + preview pane).
  assert(allByClass(viewhost, 'dn-bld-preview')[0], 'the builder live-preview pane rendered full-width');
  // the breadcrumb reads environment › tournament builder (no settings crumb).
  const crumbs = allByClass(root, 'dt-crumbs')[0];
  assert(crumbs && (crumbs.textContent || '').toLowerCase().includes('tournament builder'), 'the breadcrumb names the tournament builder');
  assert(crumbs && !(crumbs.textContent || '').toLowerCase().includes('settings'), 'the builder breadcrumb does NOT pass through settings');
});

// THE SETTINGS DRAWER OVERLAY (Change 1). Settings is no longer a full-page
// view: `#/settings[/<section>]` opens a routed RIGHT-SIDE DRAWER that paints
// OVER the current view (the underlying view stays rendered in `.dt-viewhost`
// behind a scrim, so an Appearance change applies live to the page behind it).
// Esc / a scrim click / the × close it by navigating back to the underlying
// route. We mount the real shell, drive the hash, and assert the overlay model.

// Build a shell-mount harness with a live `location` whose hash setter re-fires
// the registered hashchange listeners. Returns { root, loc, listeners }.
function mountShellHarness(initialHash) {
  const listeners = { hashchange: [], keydown: [] };
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  globalThis.EventSource = function EventSource() { this.readyState = 0; this.addEventListener = () => {}; this.close = () => {}; };
  globalThis.EventSource.CLOSED = 2;
  globalThis.window = globalThis.window || {};
  globalThis.window.localStorage = globalThis.window.localStorage || { getItem() { return null; }, setItem() {} };
  globalThis.window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  globalThis.window.removeEventListener = (t, fn) => { listeners[t] = (listeners[t] || []).filter((f) => f !== fn); };
  const loc = { _hash: initialHash || '', search: '' };
  Object.defineProperty(loc, 'hash', {
    get() { return this._hash; },
    set(v) { this._hash = v; for (const fn of (listeners.hashchange || [])) fn(); },
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => { for (const fn of (listeners.hashchange || [])) fn(); };
  const root = document.createElement('div');
  document.body.appendChild(root);
  return { root, loc, listeners };
}
const settleTicks = async (n) => { for (let i = 0; i < (n || 4); i += 1) await new Promise((r) => setTimeout(r, 0)); };

test('settings overlay: #/settings opens a DRAWER over the current view (underlying view stays painted)', async () => {
  freshState(); installFetch();
  const { root, loc } = mountShellHarness(`#/e/${EPOCH_ID}`);
  shell.mountShell(root);
  await settleTicks();
  // the underlying epoch view painted into the main host.
  const viewhost = allByClass(root, 'dt-viewhost')[0];
  assert(viewhost && viewhost.firstChild, 'the underlying epoch view painted into the main host');
  // the drawer overlay exists but is CLOSED (no settings route yet).
  const drawer = allByClass(root, 'dt-drawer')[0];
  assert(drawer, 'the settings drawer overlay is mounted in the shell');
  assertEqual(drawer.getAttribute('data-open'), '0', 'the drawer is closed before the settings route');

  // navigate to #/settings — the drawer OPENS over the still-painted view.
  loc.hash = '#/settings';
  await settleTicks();
  assertEqual(drawer.getAttribute('data-open'), '1', 'the drawer opens on the settings route');
  // the underlying view is STILL rendered in the main host (painted behind the scrim).
  assert(viewhost.firstChild, 'the underlying view stays painted behind the scrim (not torn down)');
  // the settings surface rendered INTO the drawer body, NOT the main view host.
  const drawerBody = allByClass(root, 'dt-drawer-body')[0];
  assert(drawerBody && allByClass(drawerBody, 'dn-settings')[0], 'the settings surface painted into the drawer body');
  assertEqual(allByClass(viewhost, 'dn-settings').length, 0, 'settings is NOT painted into the main view host (it is an overlay)');
  // a scrim + a close affordance exist.
  assert(allByClass(root, 'dt-drawer-scrim')[0], 'the drawer has a click-to-close scrim');
  assert(allByClass(root, 'dt-drawer-x')[0], 'the drawer has a close (×) affordance');
});

test('settings overlay: a section deep-link opens the overlay over home when loaded cold', async () => {
  freshState(); installFetch();
  // cold load straight onto a settings section deep-link — opens over home.
  const { root } = mountShellHarness('#/settings/contract');
  shell.mountShell(root);
  await settleTicks();
  const drawer = allByClass(root, 'dt-drawer')[0];
  assertEqual(drawer.getAttribute('data-open'), '1', 'the overlay is open on a cold settings deep-link');
  const drawerBody = allByClass(root, 'dt-drawer-body')[0];
  assert(allByClass(drawerBody, 'dn-settings')[0], 'the settings surface painted into the drawer');
  // the underlying view is HOME (the environment fleet) — painted behind the scrim.
  const viewhost = allByClass(root, 'dt-viewhost')[0];
  assert(viewhost && viewhost.firstChild, 'home (the underlying view) painted behind the overlay on a cold deep-link');
});

test('settings overlay: Esc closes the overlay (returns to the underlying route)', async () => {
  freshState(); installFetch();
  const { root, loc, listeners } = mountShellHarness(`#/e/${EPOCH_ID}`);
  shell.mountShell(root);
  await settleTicks();
  loc.hash = '#/settings';
  await settleTicks();
  const drawer = allByClass(root, 'dt-drawer')[0];
  assertEqual(drawer.getAttribute('data-open'), '1', 'the overlay is open');
  // fire an Escape keydown — the shell's window keydown handler closes it.
  for (const fn of (listeners.keydown || [])) fn({ key: 'Escape', preventDefault() {} });
  await settleTicks();
  // Esc navigated back to the underlying epoch route + hid the overlay.
  assertEqual(loc.hash, `#/e/${EPOCH_ID}`, 'Esc returned to the underlying route');
  assertEqual(drawer.getAttribute('data-open'), '0', 'the overlay is closed after Esc');
});

test('settings overlay: a scrim click closes the overlay (returns to the underlying route)', async () => {
  freshState(); installFetch();
  const { root, loc } = mountShellHarness(`#/e/${EPOCH_ID}`);
  shell.mountShell(root);
  await settleTicks();
  loc.hash = '#/settings/appearance';
  await settleTicks();
  const drawer = allByClass(root, 'dt-drawer')[0];
  assertEqual(drawer.getAttribute('data-open'), '1', 'the overlay is open');
  const scrim = allByClass(root, 'dt-drawer-scrim')[0];
  scrim.dispatchEvent({ type: 'click', target: scrim });
  await settleTicks();
  assertEqual(loc.hash, `#/e/${EPOCH_ID}`, 'the scrim click returned to the underlying route');
  assertEqual(drawer.getAttribute('data-open'), '0', 'the overlay is closed after the scrim click');
});

test('compare primitives: comparePicker reflects the value; splitFrame yields two sides only when B is given', () => {
  let chosen = '__unset__';
  const picker = compare.comparePicker({
    label: 'compare with…', current: 'v1', value: 'v2',
    options: [{ id: 'v0' }, { id: 'v1' }, { id: 'v2' }],
    onChange: (v) => { chosen = v; },
  });
  const sel = picker.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-cmp-select'))[0];
  assert(sel, 'a select rendered');
  assertEqual(sel.value, 'v2', 'the picker reflects the current compare value');

  const single = compare.splitFrame({ a: { title: 'A', build() {} } });
  assert((single.getAttribute('class') || '').includes('dt-split-single'), 'no B → single column');
  const dual = compare.splitFrame({ a: { title: 'A', build() {} }, b: { title: 'B', build() {} } });
  assert(!(dual.getAttribute('class') || '').includes('dt-split-single'), 'B given → two columns');
});

test('candidate view: digest-gated — identical data does NOT rebuild the DOM (heartbeat no-op)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  const digest1 = host.getAttribute('data-t-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'candidate painted');
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  assertEqual(host.getAttribute('data-t-digest'), digest1, 'digest unchanged on the no-op repaint');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

test('tree sidebar: digest-gated — same model + route + toggles yields the same digest (heartbeat no-op)', () => {
  const model = { epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: { gens: [{ id: 'v0', promoted: true, parent: null }], boards: [{ id: 'waffles_single' }] } } };
  const route = router.parseRoute(`#/e/${EPOCH_ID}/gen/v0`);
  const toggles = new Set();
  const d1 = tree.treeDigest(model, route, toggles);
  const d2 = tree.treeDigest(model, route, toggles);
  assertEqual(d1, d2, 'a steady heartbeat (identical model/route) is a true digest no-op');
});

// ---- THE UNDER-RENDER FIX: a NEW candidate landing mid-round repaints ----
//
// The recurring counterpart of the flashing bug class: a real state change (a
// new candidate minted during a round) failed to repaint, so the operator had
// to HARD-REFRESH to see new candidates. Root cause: the tree + every
// candidate-listing view read through data.js's module cache, which was busted
// ONLY on a VIEW change — never when SSE folded a new generation into AppState.
// These tests pin (1) the signature flips on an add, (2) a real add repaints the
// tree to include the new candidate, and (3) a no-op beat does NOT (no flash).

// liveDataSignature() flips on an add / status change but is stable on a no-op.
test('under-render: liveDataSignature flips when a generation is added (and stays stable on a no-op beat)', () => {
  coreState.state.lineage = { generations: [
    { generation_id: 'v0', epoch_id: EPOCH_ID, promoted: true },
    { generation_id: 'v1', epoch_id: EPOCH_ID, promoted: false },
  ] };
  coreState.state.epochs = [{ epoch_id: EPOCH_ID }];
  coreState.state.workspace = { current_epoch_id: EPOCH_ID };
  const sig0 = data.liveDataSignature();
  // a no-op beat: the SAME generations re-folded (order swapped to prove the
  // signature is order-independent) must yield the IDENTICAL signature.
  coreState.state.lineage = { generations: [
    { generation_id: 'v1', epoch_id: EPOCH_ID, promoted: false },
    { generation_id: 'v0', epoch_id: EPOCH_ID, promoted: true },
  ] };
  assertEqual(data.liveDataSignature(), sig0, 'a no-op beat (same gen set) leaves the signature identical');
  // a NEW candidate landing flips the signature.
  coreState.state.lineage.generations.push({ generation_id: 'v2', epoch_id: EPOCH_ID, promoted: null });
  assert(data.liveDataSignature() !== sig0, 'adding a candidate flips the live-data signature');
  // a pending→settled status transition on an existing candidate flips it too.
  const sigPending = data.liveDataSignature();
  coreState.state.lineage.generations[2].promoted = false;
  assert(data.liveDataSignature() !== sigPending, 'a pending→settled status change flips the signature');
});

// END-TO-END through the shell: a new candidate folded into AppState (the SSE
// path) busts the stale cache + repaints the tree to include the new candidate;
// a no-op state:changed beat writes ZERO new tree DOM (no flash).
test('under-render: a NEW candidate folded into AppState repaints the tree (no hard-refresh); a no-op beat does NOT', async () => {
  // drain any pending re-dispatch timer a prior shell-mount test left scheduled
  // (the shell's re-render debounce is module-scoped) so it cannot race our mount.
  await new Promise((r) => setTimeout(r, 500));
  freshState();
  bus._reset();
  // a MUTABLE lineage fixture so the post-invalidation re-fetch sees the add —
  // exactly what /api/lineage returns once the backend surfaces the new gen.
  const liveLineage = { generations: [
    { generation_id: 'v0', epoch_id: EPOCH_ID, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
  ] };
  const liveWs = { current_epoch_id: EPOCH_ID, epochs: [{ epoch_id: EPOCH_ID, generation_count: 2, promoted_count: 1, best_scalar: 70.94, closed: false, goal: 'crisper' }], sparkline: [] };
  // a SELF-CONSISTENT, FLAT (no round_index) gauntlet epoch so the tree lists
  // gen LEAVES directly under Generations — NOT collapsed under round nodes (the
  // global FIXTURE carries round structure + a v2 we must NOT pre-seed here).
  const liveEpoch = { epoch_id: EPOCH_ID, closed: false, goal: 'crisper', board: [], experiments: [
    { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } },
    { generation_id: 'v1', parent_generation_id: 'v0', outcome: { decision: 'rejected' } },
  ] };
  const liveBracket = { epoch_id: EPOCH_ID, champion_lineage: ['v0'], matchups: [] };
  globalThis.fetch = async (path) => {
    const base = path.indexOf('?') >= 0 ? path.slice(0, path.indexOf('?')) : path;
    if (base === '/api/lineage') return { ok: true, json: async () => liveLineage };
    if (base === '/api/workspace') return { ok: true, json: async () => liveWs };
    if (base === '/api/epoch') return { ok: true, json: async () => liveEpoch };
    if (base === '/api/tournaments') return { ok: true, json: async () => liveBracket };
    if (base === '/api/score-trajectory') return { ok: true, json: async () => ({ points: [] }) };
    // the SERVED round timeline derives from THIS test's live fixtures (the
    // global FIXTURE's rounds must not leak into the flat under-render epoch).
    const mTl = /^\/api\/epoch\/([^/?]+)\/round-timeline$/.exec(path);
    if (mTl) {
      const F = { '/api/lineage': liveLineage, '/api/epoch': liveEpoch,
        '/api/tournaments': liveBracket, '/api/score-trajectory': { points: [] } };
      return { ok: true, json: async () => roundTimelineFromFixtures(F, decodeURIComponent(mTl[1])) };
    }
    const v = lookupFixture(FIXTURE, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };

  // mount the real shell on the generations view, branch OPEN so the gen leaves
  // render in the tree (the under-rendered surface).
  const root = mountLiveShell(`#/e/${EPOCH_ID}/gens`);
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 500));   // let the initial dispatch settle.

  const rail = allByClass(root, 'dt-sidebar')[0];
  assert(rail, 'the shell painted a tree rail');
  const genRows0 = allByClass(rail, 'dt-node').filter((n) => /^gen/.test(n.getAttribute('data-kind') || ''));
  assert(genRows0.some((n) => n.textContent.includes('v1')), 'the tree initially lists v1');
  assert(!genRows0.some((n) => n.textContent.includes('v2')), 'the tree does NOT yet list the not-yet-minted v2');
  const treeWrites0 = root.innerHTMLWriteCount ? root.innerHTMLWriteCount() : 0;

  // ── (1) a NO-OP beat: identical state re-folded. The signature is unchanged,
  // so the cache is NOT busted and the tree must NOT repaint (no flash). ──
  coreState.state.applyEnvironment({ generations: liveLineage, workspace: liveWs, epochs: liveWs.epochs });
  await new Promise((r) => setTimeout(r, 500));
  const railNodesAfterNoop = allByClass(allByClass(root, 'dt-sidebar')[0], 'dt-node').filter((n) => /^gen/.test(n.getAttribute('data-kind') || ''));
  assert(!railNodesAfterNoop.some((n) => n.textContent.includes('v2')), 'a no-op beat does not invent a candidate');
  assertEqual(railNodesAfterNoop.length, genRows0.length, 'a no-op beat repaints NO new tree rows (no flash)');

  // ── (2) a NEW candidate lands: the backend now surfaces v2; SSE folds it into
  // AppState (applyEnvironment, exactly what loadEnvironment does post-fetch). ──
  liveLineage.generations.push({ generation_id: 'v2', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: null });
  liveWs.epochs[0].generation_count = 3;
  coreState.state.applyEnvironment({ generations: liveLineage, workspace: liveWs, epochs: liveWs.epochs });
  // wait out the re-dispatch debounce so renderTree re-reads the busted cache.
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 600));

  const railAfter = allByClass(root, 'dt-sidebar')[0];
  const genRows1 = allByClass(railAfter, 'dt-node').filter((n) => /^gen/.test(n.getAttribute('data-kind') || ''));
  assert(genRows1.some((n) => n.textContent.includes('v2')),
    'THE FIX: the new candidate v2 appears in the tree live — no hard-refresh needed');
  assert(genRows1.length > genRows0.length, 'the tree grew by the newly-minted candidate');
});

await run();

// ---- the FACET table: this candidate re-scored per board tag -------------
// The dossier READS `facet_scores` off the per-entry feed; the view never
// computes a facet. Each row carries the SAME two quantities the candidate's
// own aggregate carries, so a facet row is comparable to the overall row.

// The base fixture carries no facet_scores; this adds them to v1.
function withFacets(facets, overall) {
  const F = { ...FIXTURE };
  F[`/api/generation/${EPOCH_ID}/v1/per-entry`] = {
    ...FIXTURE[`/api/generation/${EPOCH_ID}/v1/per-entry`],
    facet_scores: { facets, overall: overall === undefined ? null : overall },
  };
  return F;
}

const OVERALL = { scalar: 0.69, mean_score: 0.61, scored_count: 3, entry_count: 4 };

// The harness DOM only implements ATTRIBUTE selectors, so walk the table's
// children (thead > tr, tbody > tr) rather than querying by tag name.
function facetCells(host) {
  const tables = allByClass(host, 'dn-facet-table');
  if (!tables.length) return [];
  const out = [];
  for (const part of tables[0].children) {
    for (const tr of part.children) {
      out.push([...tr.children].map((c) => (c.textContent || '').trim()));
    }
  }
  return out;
}

test('candidate view: the facet table reports scalar + mean score per tag, against the overall row', async () => {
  freshState();
  installFixtureMap(withFacets({
    data_cleaning: { scalar: 0.89, mean_score: 0.41, scored_count: 2, entry_count: 2 },
    extraction: { scalar: 0.49, mean_score: 0.81, scored_count: 1, entry_count: 4 },
  }, OVERALL));
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });

  const cells = facetCells(host);
  // Header names BOTH directions: the two columns run opposite ways, so the
  // reader must not have to know which is which.
  assertDeep(cells[0], ['facet', 'scalar ↓', 'mean score ↑', 'scored'], 'header states each direction');
  // Facet rows, sorted by name, then the candidate's own aggregate LAST so
  // every facet can be read against it.
  assertDeep(cells[1], ['data_cleaning', '0.89', '0.41', '2'], 'a full slice hides the denominator');
  assertDeep(cells[2], ['extraction', '0.49', '0.81', '1/4'], 'a partial slice exposes its denominator');
  assertDeep(cells[3], ['candidate overall', '0.69', '0.61', '3/4'], 'the overall row is the comparison');
  assertEqual(allByClass(host, 'dn-facet-overall').length, 1, 'the overall row is marked as the reference');
});

test('candidate view: the scalar + mean score headers explain themselves on hover', async () => {
  freshState();
  installFixtureMap(withFacets(
    { data_cleaning: { scalar: 0.89, mean_score: 0.41, scored_count: 2, entry_count: 2 } },
    OVERALL));
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });

  const table = allByClass(host, 'dn-facet-table')[0];
  const heads = table.children[0].children[0].children;
  // Both number columns carry a hovercard; the facet-name column does not.
  assertEqual(heads[0].getAttribute('data-hovercard'), null, 'the name column needs no explanation');
  assertEqual(heads[1].getAttribute('data-hovercard'), '1', 'scalar explains itself');
  assertEqual(heads[2].getAttribute('data-hovercard'), '1', 'mean score explains itself');
  // attachHovercard makes them keyboard-reachable, so the explanation is not
  // mouse-only.
  assertEqual(heads[1].getAttribute('tabindex'), '0', 'the scalar header is focusable');
  assertEqual(heads[2].getAttribute('tabindex'), '0', 'the mean-score header is focusable');
});

test('candidate view: a facet nobody scored reads as an em dash, and keeps its scalar', async () => {
  freshState();
  // An unscored entry still produced drift, so the scalar is real even though
  // there is no outcome to average.
  installFixtureMap(withFacets(
    { schema_validation: { scalar: 0.30, mean_score: null, scored_count: 0, entry_count: 1 } },
    OVERALL));
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });

  const row = facetCells(host)[1];
  assertDeep(row, ['schema_validation', '0.30', '—', '0/1'], 'an absent outcome is not a failing one');
});

test('candidate view: no facet table when the board declares no facet tags', async () => {
  freshState(); installFetch();   // the BASE fixture carries no facet_scores.
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });

  assertEqual(allByClass(host, 'dn-facets').length, 0, 'the table does not paint on a facet-less board');
});

test('candidate digest: a no-op heartbeat over a facet-bearing dossier churns NO DOM', async () => {
  freshState();
  installFixtureMap(withFacets(
    { data_cleaning: { scalar: 0.89, mean_score: 0.41, scored_count: 2, entry_count: 2 } },
    OVERALL));
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  const digest1 = host.getAttribute('data-t-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  assertEqual(host.getAttribute('data-t-digest'), digest1, 'digest unchanged on a no-op beat over a facet-bearing dossier');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op beat');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op beat (facets folded, not churned)');
});

test('candidate digest: a MOVED facet scalar repaints the dossier', async () => {
  freshState();
  installFixtureMap(withFacets(
    { data_cleaning: { scalar: 0.89, mean_score: 0.41, scored_count: 2, entry_count: 2 } },
    OVERALL));
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  const digest1 = host.getAttribute('data-t-digest');

  // The data layer caches by path, so the new payload only reaches the view
  // once the cache is invalidated — exactly what a live round does when it
  // writes fresh run files.
  freshState();
  installFixtureMap(withFacets(
    { data_cleaning: { scalar: 1.40, mean_score: 0.41, scored_count: 2, entry_count: 2 } },
    OVERALL));
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  assert(host.getAttribute('data-t-digest') !== digest1, 'a real facet move changes the digest');
  assertDeep(facetCells(host)[1], ['data_cleaning', '1.40', '0.41', '2'], 'the table shows the new scalar');
});

test('candidate view: the count cell SURFACES ran_count, the scalar’s own denominator', async () => {
  freshState();
  // Two slices that would print identically if the payload's `ran_count` went
  // unread — `1/3` in both cases. They mean opposite things: `thin_checks`
  // ran its whole slice and only one entry carried an outcome check;
  // `mostly_skipped` scored everything that ran, and two entries never ran at
  // all. The second is a measurement gap, the first is a board property.
  installFixtureMap(withFacets({
    thin_checks: { scalar: 0.50, mean_score: 0.70, scored_count: 1, ran_count: 3, entry_count: 3 },
    mostly_skipped: { scalar: 0.50, mean_score: 0.70, scored_count: 1, ran_count: 1, entry_count: 3 },
  }, OVERALL));
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });

  const cells = facetCells(host);
  assertDeep(cells[1], ['mostly_skipped', '0.50', '0.70', '1/1/3'], 'a skipped slice shows what ran');
  assertDeep(cells[2], ['thin_checks', '0.50', '0.70', '1/3'], 'a fully-run slice collapses the middle');
  // The three-number form needs its own explanation, so the column carries one.
  const heads = allByClass(host, 'dn-facet-table')[0].children[0].children[0].children;
  assertEqual(heads[3].getAttribute('data-hovercard'), '1', 'the count column explains itself');
  assertEqual(heads[3].getAttribute('tabindex'), '0', 'and is keyboard-reachable');
});

test('candidate digest: a slice that GAINED runs repaints, even at an unchanged scalar', async () => {
  freshState();
  installFixtureMap(withFacets(
    { data_cleaning: { scalar: 0.89, mean_score: 0.41, scored_count: 2, ran_count: 2, entry_count: 4 } },
    OVERALL));
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  const digest1 = host.getAttribute('data-t-digest');

  // The remaining two entries run and land on the same numbers. Nothing the
  // old digest folded moved — but the CELL did, from `2/2/4` to `2/4`, and a
  // digest blind to `ran_count` would pin the stale denominator on screen.
  freshState();
  installFixtureMap(withFacets(
    { data_cleaning: { scalar: 0.89, mean_score: 0.41, scored_count: 2, ran_count: 4, entry_count: 4 } },
    OVERALL));
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });

  assert(host.getAttribute('data-t-digest') !== digest1, 'a coverage change is a real change');
  assertDeep(facetCells(host)[1], ['data_cleaning', '0.89', '0.41', '2/4'], 'the cell shows the new coverage');
});

// test/variant_t_epoch_scoping.test.mjs — Variant T ("Console IV") unit tests:
// drill-down views scoped to the viewed epoch, pending (unscored)
// candidates, fleet cards, and cross-epoch tree/gens gating.
//
// Split mechanically from the former variant_t.test.mjs (assertions
// verbatim); shared fixtures + helpers live in ./fixtures.mjs.

import { installDom, test, run, assert, assertEqual, assertDeep, makeEvent } from './harness.mjs';

installDom();

const {
  router, svg, ui, shell, data, tree,
  compare, coreState, rounds, live, EPOCH_ID, FIXTURE,
  lookupFixture, installFetch, freshState, allByClass, readCss, svgsByClass,
  installFixtureMap, TWO_EP_OLD, TWO_EP_NEW, twoEpochFixture,
} = await import('./fixtures.mjs');

// ====================================================================
// TIER 1 (Class A) — the DRILL-DOWN views are scoped to the VIEWED epoch.
// TIER 2 (Class B) — an unscored candidate (promoted:null) renders PENDING,
//                    never rejected / dead-branch.
//
// Two epochs share /api/lineage with COLLIDING gen ids: a COMPLETED e0
// (v0..v2, v1 promoted) and an in-flight e1 (v0 + an UNSCORED v1 with
// promoted:null). The `?epoch=<id>` backend reads return the SCOPED contract /
// trajectory / bracket per epoch; /api/lineage is global, so the views must
// filter it by epoch_id (generationsForEpoch) and dedupe by gen id.
// ====================================================================
const SC_OLD = '2026-06-01_e0';
const SC_NEW = '2026-06-02_e1';
function scopeFixture() {
  const lineage = [
    { generation_id: 'v0', epoch_id: SC_OLD, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: SC_OLD, parent_generation_id: 'v0', promoted: true },
    { generation_id: 'v2', epoch_id: SC_OLD, parent_generation_id: 'v1', promoted: false },
    // e1: a seed v0 + an UNSCORED challenger v1 (promoted == null → pending).
    { generation_id: 'v0', epoch_id: SC_NEW, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: SC_NEW, parent_generation_id: 'v0', promoted: null },
  ];
  const F = { '/api/lineage': { generations: lineage } };
  // per-epoch scoped contract / trajectory / bracket (keyed by the ?epoch= path).
  const contract = (id, gens) => ({
    epoch_id: id, closed: id === SC_OLD, goal: 'g',
    experiments: gens.map((g) => ({ generation_id: g.generation_id, parent_generation_id: g.parent_generation_id,
      outcome: g.promoted === true ? { decision: 'promoted' } : g.promoted === false ? { decision: 'rejected' } : {} })),
    board: [{ entry_id: 'waffles_single', kind: 'single_turn', budget_s: 180, weight: 1 }],
  });
  const traj = (gens) => ({ points: gens.map((g, i) => ({ generation_id: g.generation_id, scalar: 40 + i })) });
  const oldGens = lineage.filter((g) => g.epoch_id === SC_OLD);
  const newGens = lineage.filter((g) => g.epoch_id === SC_NEW);
  F[`/api/epoch?epoch=${SC_OLD}`] = contract(SC_OLD, oldGens);
  F[`/api/epoch?epoch=${SC_NEW}`] = contract(SC_NEW, newGens);
  F[`/api/score-trajectory?epoch=${SC_OLD}`] = traj(oldGens);
  F[`/api/score-trajectory?epoch=${SC_NEW}`] = traj(newGens);
  F[`/api/tournaments?epoch=${SC_OLD}`] = { epoch_id: SC_OLD, champion_lineage: ['v0', 'v1'], matchups: [
    { champion: 'v0', challenger: 'v1', decision: 'promoted', delta_scalar: -5 },
    { champion: 'v1', challenger: 'v2', decision: 'rejected', delta_scalar: 4 },
  ] };
  // e1: the challenger v1 has run no gate yet → NO decision (pending).
  F[`/api/tournaments?epoch=${SC_NEW}`] = { epoch_id: SC_NEW, champion_lineage: ['v0'], matchups: [
    { champion: 'v0', challenger: 'v1' },
  ] };
  // per-entry profiles for every (epoch, gen) so a leak would surface as columns.
  for (const g of lineage) {
    F[`/api/generation/${g.epoch_id}/${g.generation_id}/per-entry`] = {
      epoch_id: g.epoch_id, generation_id: g.generation_id,
      entries: [{ entry_id: 'waffles_single', run_id: `r_${g.epoch_id}_${g.generation_id}`, drift_loss: 50, pass_fail: false }],
    };
  }
  F[`/api/epoch/${SC_NEW}/analysis`] = { analysis_md: '' };
  F[`/api/epoch/${SC_OLD}/analysis`] = { analysis_md: '' };
  return F;
}

test('Tier1 (cross-epoch): candidate view scopes to the viewed epoch (only e1 gens; correct champion)', async () => {
  freshState();
  installFixtureMap(scopeFixture());
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: SC_NEW, gen: 'v1' });
  assert(host.textContent.includes('Candidate v1'), 'e1 v1 rendered');
  // the compare picker lists ONLY e1's field {v0, v1} — never e0's v2.
  const opts = allByClass(host, 'dt-cmp-opt');
  const optText = host.textContent;
  assert(!optText.includes('v2'), 'no leaked e0-only generation (v2) on the e1 candidate view');
});

test('Tier2 (Class B): an UNSCORED e1 candidate (promoted:null) renders PENDING, not dead-branch', async () => {
  freshState();
  installFixtureMap(scopeFixture());
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: SC_NEW, gen: 'v1' });
  // the verdict pill reads pending (racing…), never rejected.
  assert(allByClass(host, 'dn-pending').length >= 1, 'a pending verdict pill rendered for the unscored challenger');
  assert(!allByClass(host, 'dn-rejected').some((n) => /seed|v1/.test(n.textContent)), 'no rejected pill');
  // the lifecycle DAG terminal must NOT say "dead branch / champion stands".
  assert(!host.textContent.includes('dead branch'), 'the DAG terminal is NOT "✕ dead branch" for a pending candidate');
  assert(host.textContent.includes('racing') || host.textContent.includes('awaiting gate'), 'the DAG terminal reads a racing/awaiting-gate state');
});

test('Tier1 (cross-epoch): gens view scopes to the viewed epoch; pending candidate in the roster', async () => {
  freshState();
  installFixtureMap(scopeFixture());
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: SC_NEW });
  assert(host.textContent.includes(`Generations · ${SC_NEW}`), 'the gens page heads with e1');
  // the roster lists e1's {v0, v1} only — not e0's v2.
  const monos = allByClass(host, 'dn-mono').map((n) => n.textContent);
  assert(!host.textContent.includes('v2'), 'no leaked e0 generation v2 in the e1 roster');
  // the unscored v1 row carries a PENDING pill (not rejected).
  assert(allByClass(host, 'dn-pending').length >= 1, 'the unscored challenger reads pending in the roster');
});

test('Tier1 (cross-epoch): switching the epoch param changes the data (e0 ↔ e1)', async () => {
  freshState();
  installFixtureMap(scopeFixture());
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await gens.render(host, ctx, { epochId: SC_OLD });
  assert(host.textContent.includes('v2'), 'e0 view shows its own generation v2');
  assert(host.textContent.includes(`Generations · ${SC_OLD}`), 'heads with e0');
  await gens.render(host, ctx, { epochId: SC_NEW });
  assert(host.textContent.includes(`Generations · ${SC_NEW}`), 'switched to e1');
  assert(!host.textContent.includes('v2'), 'e2-only generation gone after switching to e1');
});

test('Tier1 (cross-epoch): boards view scopes to the viewed epoch (no e0 candidate columns)', async () => {
  freshState();
  installFixtureMap(scopeFixture());
  const boards = await import('../js/views/boards.js');
  const host = document.createElement('div');
  await boards.render(host, { navigate() {}, href: router.href }, { epochId: SC_NEW });
  assert(host.textContent.includes(`Boards · ${SC_NEW}`), 'the boards page heads with e1');
  // e1 has exactly 2 candidates (v0, v1); a leak would report e0's 3.
  assert(host.textContent.includes('2') && !host.textContent.includes('field of 8'), 'e1 candidate count is its own (2), not leaked');
});

test('Tier1 (cross-epoch): board (per-entry) view scopes to the viewed epoch', async () => {
  freshState();
  installFixtureMap(scopeFixture());
  const board = await import('../js/views/board.js');
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: SC_NEW, entry: 'waffles_single' });
  assert(host.textContent.includes('Board · waffles_single'), 'the board entry view rendered for e1');
  // the per-candidate breakdown lists e1's {v0, v1} only — never e0's v2.
  assert(!host.textContent.includes('v2'), 'no leaked e0 generation v2 in the e1 board breakdown');
});

test('Tier1 (cross-epoch): publication view scopes lineage/figures to the viewed epoch', async () => {
  freshState();
  installFixtureMap(scopeFixture());
  const publication = await import('../js/views/publication.js');
  const host = document.createElement('div');
  await publication.render(host, { navigate() {}, href: router.href }, { epochId: SC_NEW });
  // the aggregate-scores table lists e1's own gens only; v2 belongs to e0.
  assert(!host.textContent.includes('v2'), 'no leaked e0 generation v2 in the e1 publication figures');
  // the unscored challenger reads "racing…", never "rejected".
  assert(!/rejected/.test(host.textContent) || host.textContent.includes('racing'), 'an unscored gen reads racing, not a default rejected');
});

// ---- HEADER SCOPING: the epoch view's H1 + STATE pill read the ROUTED epoch.
//
// THE BUG. Viewing a NON-current epoch (e0, closed) while e1 is the live/current
// epoch leaked the CURRENT epoch into the epoch view's HEADER: the `Epoch <id>`
// H1 read e1's id and the STATE pill read e1's "open" — even though the
// breadcrumb, tree, structure ladder, heatmap and gen-derived stats correctly
// showed e0. Root cause: the header read `D.epoch()` (always the current epoch)
// instead of the routed `D.epoch(epochId)`. With per-epoch `?epoch=<id>`
// contracts (e0 closed → "closed" + e0's objective; e1 open → "open"), the H1
// and STATE pill must now match the ROUTED epoch, not the current one.
test('Tier1 (header scoping): the epoch view H1 + STATE pill read the ROUTED epoch, not the current one', async () => {
  freshState();
  // distinct per-epoch contracts: e0 (SC_OLD) is closed with its own objective;
  // e1 (SC_NEW) is the live/current epoch, open. The scoped `?epoch=<id>` reads
  // return each epoch's own contract; bare `D.epoch()` would return e1 (current).
  const F = scopeFixture();
  F[`/api/epoch?epoch=${SC_OLD}`] = { ...F[`/api/epoch?epoch=${SC_OLD}`], closed: true, goal: 'Sharpen e0’s drift floor.' };
  F[`/api/epoch?epoch=${SC_NEW}`] = { ...F[`/api/epoch?epoch=${SC_NEW}`], closed: false, goal: 'e1 live objective.' };
  // bare `/api/epoch` (the CURRENT epoch) resolves to e1 — a leak would surface
  // e1's id/state/objective in the e0 header.
  F['/api/epoch'] = F[`/api/epoch?epoch=${SC_NEW}`];
  installFixtureMap(F);

  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  // route AT e0 (the NON-current epoch) while e1 is current.
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: SC_OLD });

  const h1 = allByClass(host, 'dn-h1')[0];
  assert(h1, 'the epoch H1 rendered');
  assertEqual(h1.textContent, `Epoch ${SC_OLD}`, 'the H1 reads the ROUTED epoch (e0), not the current one (e1)');
  assert(!h1.textContent.includes(SC_NEW), 'the current epoch id (e1) does NOT leak into the e0 header');

  // the STATE pill (the stat row's "state" tile) reads e0's "closed", not e1's "open".
  const stats = allByClass(host, 'dn-stat').map((n) => n.textContent);
  assert(stats.some((t) => t.includes('closed') && t.includes('state')), 'the STATE pill reads e0’s "closed"');
  assert(!stats.some((t) => t.includes('open') && t.includes('state')), 'the STATE pill is NOT e1’s "open"');

  // the OBJECTIVE is e0's, not e1's.
  assert(host.textContent.includes('Sharpen e0’s drift floor.'), 'the objective is e0’s');
  assert(!host.textContent.includes('e1 live objective.'), 'e1’s objective does NOT leak into the e0 header');

  // and switching the route to e1 flips the header to e1 / open (the converse).
  const host2 = document.createElement('div');
  await epoch.render(host2, { navigate() {}, href: router.href }, { epochId: SC_NEW });
  assertEqual(allByClass(host2, 'dn-h1')[0].textContent, `Epoch ${SC_NEW}`, 'routing to e1 heads with e1');
  const stats2 = allByClass(host2, 'dn-stat').map((n) => n.textContent);
  assert(stats2.some((t) => t.includes('open') && t.includes('state')), 'e1’s STATE pill reads "open"');
  assert(!stats2.some((t) => t.includes('closed') && t.includes('state')), 'e1 is not "closed"');
});

test('Tier2 (Class B): the tree tags an unscored child PENDING, not rejected', () => {
  const host = document.createElement('div');
  const model = {
    epochs: [{ id: SC_NEW, current: true }],
    byEpoch: { [SC_NEW]: {
      gens: [{ id: 'v0', promoted: true, parent: null }, { id: 'v1', promoted: null, parent: 'v0' }],
      boards: [{ id: 'waffles_single' }],
    } },
  };
  const toggles = new Set(['e:' + SC_NEW, 'e:' + SC_NEW + '/gens']);
  const route = router.parseRoute(`#/e/${SC_NEW}`);
  tree.buildTree(host, model, route, toggles, { navigate() {}, href: router.href }, () => {});
  const tags = allByClass(host, 'dt-tag').map((n) => n.textContent);
  assert(tags.includes('pending'), 'the unscored child v1 is tagged "pending"');
  assert(!tags.includes('rejected'), 'the unscored child v1 is NOT tagged "rejected"');
});

// ---- per-board dot-plot: tournament-context label + click → run drill-down ─

test('candidate: tournamentContext derives rung/round/matchup labels (racing/gauntlet/swiss)', async () => {
  const candidate = await import('../js/views/candidate.js');
  const tc = candidate.tournamentContext;
  // racing: pre-formatted rung wins; raw rungN_* match_id → "rung N".
  assertEqual(tc({ match_id: 'rung0_m2', rung: 'rung 0' }), 'rung 0', 'pre-formatted rung string is reused');
  assertEqual(tc({ match_id: 'rung1_m1' }), 'rung 1', 'rung parsed from match_id when no pre-format');
  // racing final → the champion gate.
  assertEqual(tc({ match_id: 'racing-final' }), 'champion-gate', 'racing-final maps to champion-gate');
  // gauntlet: roundN / gN → "round N".
  assertEqual(tc({ match_id: 'round2' }), 'round 2', 'gauntlet round parsed');
  assertEqual(tc({ match_id: 'g3' }), 'round 3', 'gauntlet gN parsed');
  // swiss: roundN_mM → "round N · match M".
  assertEqual(tc({ match_id: 'round1_m2' }), 'round 1 · match 2', 'swiss round·match parsed');
  assertEqual(tc({ match_id: 'swiss_r0_m4' }), 'round 0 · match 4', 'swiss r/m prefix parsed');
  // no context at all → null (row renders name-only).
  assertEqual(tc({}), null, 'no match_id / rung → null');
});

test('svg.valueDotPlot: duplicate board rows get DISTINCT context lines + onClick carries the full item', () => {
  let clicked = null;
  const items = [
    { label: 'q3_metrics_outline', value: 80, id: 'q3_metrics_outline', context: 'rung 0', entry_id: 'q3_metrics_outline', run_id: 'run_a', gen: 'v1' },
    { label: 'q3_metrics_outline', value: 40, id: 'q3_metrics_outline', context: 'rung 1', entry_id: 'q3_metrics_outline', run_id: 'run_b', gen: 'v1' },
  ];
  const plot = svg.valueDotPlot({ items, reference: { value: 60, label: 'champion v0' }, onClick: (it) => { clicked = it; } });
  // both rows render their board name…
  const names = allByClass(plot, 'dn-dot-label').map((n) => n.textContent);
  assertEqual(names.filter((t) => t === 'q3_metrics_outline').length, 2, 'BOTH duplicate board rows rendered');
  // …with DISTINCT context tags (not two identical labels).
  const ctxs = allByClass(plot, 'dn-dot-ctx').map((n) => n.textContent);
  assert(ctxs.includes('rung 0') && ctxs.includes('rung 1'), 'each duplicate carries its own rung tag');
  assertEqual(new Set(ctxs).size, 2, 'the two context tags are distinct');
  // reference rule still drawn (existing behaviour unchanged).
  assert(allByClass(plot, 'dn-ref-rule').length === 1, 'the champion reference line is still drawn');
  // clicking a row fires onClick with the FULL item (entry_id/run_id/gen intact).
  const rows = allByClass(plot, 'dn-dotrow');
  assertEqual(rows.length, 2, 'two clickable dot rows');
  rows[1].dispatchEvent({ type: 'click' });
  assert(clicked && clicked.entry_id === 'q3_metrics_outline' && clicked.run_id === 'run_b' && clicked.gen === 'v1',
    'onClick receives the specific run (entry_id + run_id + gen)');
});

test('svg.heatmap: higher-contrast theme-aware cell scale — wider range, monotonic, low≠empty', () => {
  // A 1×4 board×gen matrix: one EMPTY cell + three valued cells spanning the
  // drift range (low / mid / high). value(rowId,colId) returns the drift loss.
  const cellVal = { 'b/lo': 10, 'b/mid': 55, 'b/hi': 100 }; // 'b/empty' → null
  const rows = [{ id: 'b', label: 'board' }];
  const cols = [
    { id: 'empty', label: 'g-empty' },
    { id: 'lo', label: 'g-lo' },
    { id: 'mid', label: 'g-mid' },
    { id: 'hi', label: 'g-hi' },
  ];
  let clicked = null;
  const hm = svg.heatmap({
    rows, cols,
    value: (r, c) => (c === 'empty' ? null : cellVal[`${r}/${c}`]),
    onClick: (r, c) => { clicked = [r, c]; },
  });
  const cells = allByClass(hm, 'dn-hm-cell');
  assertEqual(cells.length, 4, 'four cells rendered (1 empty + 3 valued)');
  const empty = cells.find((c) => c.classList.contains('dn-hm-empty'));
  const valued = cells.filter((c) => !c.classList.contains('dn-hm-empty'));
  assert(empty, 'the null cell carries the dn-hm-empty token');
  assertEqual(valued.length, 3, 'three valued cells (lo/mid/hi)');

  // helpers to read the two contrast axes off a cell
  const opOf = (c) => parseFloat(c.getAttribute('fill-opacity'));
  const mixOf = (c) => parseFloat(c.getAttribute('data-hm-mix'));
  const [lo, mid, hi] = valued; // rendered in col order lo,mid,hi

  // (1) MONOTONIC in drift on BOTH axes (opacity density AND cool→hot mix).
  assert(opOf(lo) < opOf(mid) && opOf(mid) < opOf(hi), 'fill-opacity is monotonic in drift');
  assert(mixOf(lo) < mixOf(mid) && mixOf(mid) < mixOf(hi), 'cool→hot mix is monotonic in drift');

  // (2) WIDER contrast than the OLD opacity-only ramp. The old scale was a
  // SINGLE ink at op = 0.18 + 0.82*t with NO colour axis (mix spread = 0). The
  // new scale adds a cool→hot mix spanning a wide range, so the combined
  // high-vs-low contrast metric is strictly greater than the old one.
  const OLD_op = (t) => 0.18 + 0.82 * t; // the previous mapping, for reference
  const tLo = 0, tHi = 1; // lo is the min (t=0), hi is the max (t=1)
  const oldContrast = OLD_op(tHi) - OLD_op(tLo);            // = 0.82, opacity only
  const newOpContrast = opOf(hi) - opOf(lo);                // density axis
  const newMixContrast = (mixOf(hi) - mixOf(lo)) / 100;     // hue axis, normalised
  const newContrast = newOpContrast + newMixContrast;       // combined two-axis metric
  assert(newContrast > oldContrast,
    `new combined contrast ${newContrast.toFixed(3)} > old opacity-only ${oldContrast.toFixed(3)}`);
  assert(newMixContrast > 0.5, 'the cool→hot hue axis alone spans a wide range (>0.5)');

  // (3) the densest cell reads as clearly "most drift" — near-full opacity and
  // (almost) fully the HOT token.
  assert(opOf(hi) > 0.95, 'the highest-drift cell is near-opaque');
  assert(mixOf(hi) > 95, 'the highest-drift cell is almost entirely the HOT token');

  // (4) the LOWEST non-empty cell stays clearly distinct from an EMPTY one:
  // it carries a value-driven mix + opacity (the cool token at a visible
  // floor), whereas the empty cell has NO mix and uses the flat empty token.
  assert(opOf(lo) >= 0.28, 'the lowest valued cell sits at a visible opacity floor (≠ near-invisible)');
  assert(empty.getAttribute('data-hm-mix') == null, 'the empty cell carries NO cool→hot mix');
  assert(empty.getAttribute('fill-opacity') == null, 'the empty cell carries NO value-driven opacity');
  // the inline fill on a valued cell is a theme-token color-mix (no hardcoded hex).
  assert(/color-mix\(in srgb, var\(--v2-hm-hot\)/.test(lo.style.cssText || ''),
    'valued cells fill via a theme-token cool→hot color-mix (theme-aware, no hex)');

  // (5) the onClick affordance + tooltip survive.
  hi.dispatchEvent({ type: 'click' });
  assertDeep(clicked, ['b', 'hi'], 'cell onClick fires with (rowId, colId)');
  assertEqual(hi.style.cursor, 'pointer', 'clickable cells show a pointer cursor');
});

test('candidate view: per-board dumbbell click → board drill-down for THAT run; duplicate rungs disambiguated', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  // same entry raced in TWO rungs (different match_id + rung) → two rows.
  const path = `/api/generation/${EPOCH_ID}/v1/per-entry`;
  const saved = FIXTURE[path];
  FIXTURE[path] = { epoch_id: EPOCH_ID, generation_id: 'v1', entries: [
    { entry_id: 'waffles_single', run_id: 'run_v1_w_r0', drift_loss: 80.0, pass_fail: false, runtime_ms: 180000, wall_clock_budget_exceeded: false, match_id: 'rung0_m1', rung: 'rung 0' },
    { entry_id: 'waffles_single', run_id: 'run_v1_w_r1', drift_loss: 40.0, pass_fail: true, runtime_ms: 180000, wall_clock_budget_exceeded: false, match_id: 'rung1_m1', rung: 'rung 1' },
  ] };
  try {
    const host = document.createElement('div');
    let navTo = null;
    const ctx = { navigate: (v, p, o) => { navTo = { v, p, o }; }, href: router.href };
    await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
    // both rungs show as distinct context tags on the dumbbell.
    const ctxs = allByClass(host, 'dn-dumbbell-ctx').map((n) => n.textContent);
    assert(ctxs.includes('rung 0') && ctxs.includes('rung 1'), 'the duplicate board rows show "rung 0" vs "rung 1"');
    // clicking a dumbbell row routes to the board drill-down for this entry + gen.
    const rows = allByClass(host, 'dn-dumbbell-row');
    assert(rows.length >= 2, 'at least the two re-raced rows are clickable');
    rows[0].dispatchEvent({ type: 'click' });
    assert(navTo && navTo.v === 'board' && navTo.p.entry === 'waffles_single' && navTo.p.gen === 'v1' && navTo.p.epochId === EPOCH_ID,
      'a dumbbell row click opens the board drill-down for that exact run (entry + gen)');
  } finally {
    FIXTURE[path] = saved;
  }
});

// ---- Task A: the per-board figure is the study's champ○ → candidate● DUMBBELL ----
// The study's opt-2 per-board figure is an explicit per-row dumbbell: a hollow
// champion ○ and a filled candidate ● JOINED by a connector, with the Δ + the
// pass/fail marker — NOT a single-series dot-plot against one aggregate champion
// reference rule. v1 (parent v0 = champion) shares its slice with v0, so each
// board row carries a real per-board champion value to draw the ○.
test('candidate view: the per-board figure renders the champion○ → candidate● DUMBBELL (paired per row, not a single-series dot-plot)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  // the dumbbell SVG is present + responsive (width-filling); the OLD single-series
  // valueDotPlot (dn-valdot + its aggregate reference rule) is GONE from the dossier.
  const dumbbell = svgsByClass(host, 'dn-dumbbell')[0];
  assert(dumbbell, 'the per-board champion○ → candidate● dumbbell SVG rendered');
  assertEqual(dumbbell.getAttribute('width'), '100%', 'the dumbbell is responsive (fills its dossier column)');
  assertEqual(svgsByClass(host, 'dn-valdot').length, 0, 'the old single-series dot-plot is gone from the dossier (replaced by the dumbbell)');
  // v1 has TWO scored boards, both shared with the champion v0 → two paired rows,
  // each with a hollow champion ○, a filled candidate ●, a connector, AND a Δ.
  const champDots = allByClass(dumbbell, 'dn-dumbbell-champ');
  const candDots = allByClass(dumbbell, 'dn-dumbbell-cand');
  const conns = allByClass(dumbbell, 'dn-dumbbell-conn');
  const deltas = allByClass(dumbbell, 'dn-dumbbell-delta');
  assertEqual(champDots.length, 2, 'a hollow champion ○ per board (one per paired row)');
  assertEqual(candDots.length, 2, 'a filled candidate ● per board');
  assertEqual(conns.length, 2, 'a champ→candidate connector per board (the dumbbell bar)');
  assertEqual(deltas.length, 2, 'a per-board Δ (candidate − champion) per board');
  // the champion ○ uses the REAL per-board champion value (v0: waffles 60.5,
  // picky 105.5 — both come through s.championLoss, so the ○ is positioned by
  // the actual champion-on-this-board loss, recoverable as cand − Δ).
  const champCx = champDots.map((n) => parseFloat(n.getAttribute('cx')));
  const candCx = candDots.map((n) => parseFloat(n.getAttribute('cx')));
  assert(champCx.every((v) => Number.isFinite(v)) && candCx.every((v) => Number.isFinite(v)),
    'both the ○ and ● are positioned on the shared per-row value axis');
  // worst-first sort: picky (cand 642.5) is far worse than its champ (105.5) → a
  // regressed (dn-bad) row; both connectors here are regressions vs the champion.
  assert(conns.some((n) => (n.getAttribute('class') || '').includes('dn-bad')),
    'a regressed board (candidate worse than champion on that board) colours its connector dn-bad');
  // the rows are clickable → that board's drill-down (keeps the drill affordance).
  const rows = allByClass(dumbbell, 'dn-dumbbell-row');
  assertEqual(rows.length, 2, 'each board row is its own clickable group');
  let navTo = null;
  const host2 = document.createElement('div');
  await candidate.render(host2, { navigate: (v, p) => { navTo = { v, p }; }, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  allByClass(host2, 'dn-dumbbell-row')[0].dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'board' && navTo.p.gen === 'v1', 'clicking a dumbbell row opens that board\'s drill-down');
});

// ---- Task B: the generalization train→holdout slope is correctly GATED ----
// The study's "(5) generalization · train → holdout" slope renders the train dot
// → holdout dot, the gap, and the OK/over-tolerance verdict when the candidate's
// experiment carries holdout data, and is cleanly ABSENT when there's none.
test('candidate view (Task B): the train→holdout generalization slope RENDERS for a holdout-bearing candidate (slope + gap + verdict)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  // give v1's experiment a holdout triplet within tolerance (gap 0.02 ≤ tol 0.05).
  const saved = FIXTURE['/api/epoch'];
  FIXTURE['/api/epoch'] = {
    ...saved,
    experiments: saved.experiments.map((x) => x.generation_id === 'v1'
      ? { ...x, train_loss: 0.60, holdout_loss: 0.62, generalization_gap: 0.02, generalization_tolerance: 0.05 }
      : x),
  };
  try {
    const host = document.createElement('div');
    await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
    // the shrunk supporting panel + its section heading are present.
    assert(host.textContent.includes('Generalization · train → holdout'), 'the generalization section heading renders');
    const pane = allByClass(host, 'dn-genpane')[0];
    assert(pane, 'the shrunk train→holdout slope pane renders');
    // the slope itself: a train point → a holdout point joined by a slope line.
    assert(allByClass(pane, 'dn-gen-train')[0], 'the train point renders');
    assert(allByClass(pane, 'dn-gen-holdout')[0], 'the holdout point renders');
    assert(allByClass(pane, 'dn-gen-slope')[0], 'the train→holdout slope line renders');
    // the gap label carries the gap + the within-tolerance OK verdict.
    const gap = allByClass(pane, 'dn-gen-gap')[0];
    assert(gap, 'the gap label renders');
    const gt = gap.textContent || '';
    assert(gt.includes('gap') && gt.includes('OK'), 'the gap label reads the gap + the within-tolerance OK verdict');
    // within tolerance → the caution tone, NOT the over-tolerance bad tone.
    assert((gap.getAttribute('class') || '').includes('dn-caution'), 'a within-tolerance gap reads the caution tone (not over-tolerance bad)');
  } finally {
    FIXTURE['/api/epoch'] = saved;
  }
});

test('candidate view (Task B): the generalization slope is ABSENT when the candidate has NO holdout data (cleanly gated)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  // the default v1 experiment carries NO train/holdout/gap fields → no panel.
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  assert(!host.textContent.includes('Generalization · train → holdout'), 'no generalization heading when there is no holdout data');
  assertEqual(allByClass(host, 'dn-genpane').length, 0, 'no generalization slope pane when there is no holdout data');
  assertEqual(svgsByClass(host, 'dn-gen-svg').length, 0, 'no generalization slope SVG when there is no holdout data');
});

// the over-tolerance verdict: a holdout gap that EXCEEDS tolerance reads the bad
// tone + the "> tol" / memorization caption (the other verdict branch).
test('candidate view (Task B): an over-tolerance holdout gap reads the over-tolerance (memorization) verdict', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const saved = FIXTURE['/api/epoch'];
  FIXTURE['/api/epoch'] = {
    ...saved,
    experiments: saved.experiments.map((x) => x.generation_id === 'v1'
      ? { ...x, train_loss: 0.40, holdout_loss: 0.95, generalization_gap: 0.55, generalization_tolerance: 0.05 }
      : x),
  };
  try {
    const host = document.createElement('div');
    await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
    const pane = allByClass(host, 'dn-genpane')[0];
    assert(pane, 'the slope pane renders for the over-tolerance candidate too');
    const gap = allByClass(pane, 'dn-gen-gap')[0];
    assert(gap && (gap.getAttribute('class') || '').includes('dn-bad'), 'an over-tolerance gap reads the bad tone');
    assert((gap.textContent || '').includes('> tol'), 'the gap label flags it exceeds tolerance');
    assert(pane.textContent.includes('memorization'), 'the caption flags possible memorization');
  } finally {
    FIXTURE['/api/epoch'] = saved;
  }
});

test('Tier2 (Class B): decisionFor never defaults null/absent → rejected', () => {
  assertEqual(ui.decisionFor({ promoted: null, parent: 'v0' }), 'pending', 'null + no resolved decision → pending');
  assertEqual(ui.decisionFor({ promoted: true, parent: 'v0' }), 'promoted', 'promoted:true → promoted');
  assertEqual(ui.decisionFor({ promoted: false, parent: 'v0' }), 'rejected', 'promoted:false → rejected');
  assertEqual(ui.decisionFor({ parent: null }), 'baseline', 'no parent → baseline');
  assertEqual(ui.decisionFor({ promoted: null, parent: 'v0', exp: { decision: 'rejected' } }), 'rejected', 'null + stamped negative decision → rejected');
  assertEqual(ui.decisionFor({ promoted: null, parent: 'v0', gate: { decision: 'promoted' } }), 'promoted', 'null + resolved gate promote → promoted');
  assertEqual(ui.decisionFor({}), 'baseline', 'empty (no parent) → baseline, never rejected');
});

// ====================================================================
// Fleet cards (Environment view): each epoch card's hero sparkline shows
// that epoch's OWN real per-generation trajectory — NEVER a fabricated
// `[best×1.18, best×1.06, best]` curve (which renders shape-identical for
// every epoch and would surface fabricated numbers).
// ====================================================================

// A two-epoch workspace whose two epochs have DIFFERENT real trajectories
// (different scalars AND different lengths). scoreTrajectory is scoped per
// epoch via `?epoch=<id>`, so each card must source ITS id's points.
const FLEET_E0 = '2026-06-01_e0';
const FLEET_E1 = '2026-06-02_e1';
const FLEET_FIXTURE = {
  '/api/workspace': { current_epoch_id: FLEET_E1, epochs: [
    { epoch_id: FLEET_E0, generation_count: 5, promoted_count: 1, best_scalar: 46.813, closed: true, goal: 'e0 goal' },
    { epoch_id: FLEET_E1, generation_count: 9, promoted_count: 0, best_scalar: 20.500, closed: false, goal: 'e1 goal' },
  ], sparkline: [{ epoch_id: FLEET_E0, scalar: 46.813 }, { epoch_id: FLEET_E1, scalar: 20.500 }] },
  '/api/health-report': { healthy: true, findings: [] },
  // distinct scalars; e0 has 3 points, e1 has 5 — different series AND length.
  [`/api/score-trajectory?epoch=${FLEET_E0}`]: { epoch_id: FLEET_E0, points: [
    { generation_id: 'v0', scalar: 55.9 }, { generation_id: 'v1', scalar: 50.0 }, { generation_id: 'v4', scalar: 46.813 },
  ] },
  [`/api/score-trajectory?epoch=${FLEET_E1}`]: { epoch_id: FLEET_E1, points: [
    { generation_id: 'v0', scalar: 56.2 }, { generation_id: 'v1', scalar: 53.5 }, { generation_id: 'v3', scalar: 50.07 },
    { generation_id: 'v7', scalar: 40.5 }, { generation_id: 'v8', scalar: 20.500 },
  ] },
};
function installFleetFetch(F) {
  globalThis.fetch = async (path) => {
    const v = lookupFixture(F || FLEET_FIXTURE, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}
// the value series each card's sparkline drew, read back from the SVG path's
// M/L vertices (one vertex per finite value) — the only DOM-visible proof of
// the series, and enough to compare length + shape across cards.
function sparkPointCount(card) {
  const path = card.querySelectorAll('[class]').filter((n) =>
    n.localName === 'path' && (n.getAttribute('class') || '').includes('dn-spark-line'))[0];
  if (!path) return 0;
  const d = path.getAttribute('d') || '';
  return (d.match(/[ML]/g) || []).length;
}

test('fleet cards: two epochs with DIFFERENT real trajectories render DIFFERENT sparklines (per-epoch, keyed on epoch_id)', async () => {
  freshState(); installFleetFetch();
  const home = await import('../js/views/home.js');
  const host = document.createElement('div');
  await home.render(host, { navigate() {}, href: router.href }, {});

  const cards = allByClass(host, 'dn-fleet-card');
  assertEqual(cards.length, 2, 'one fleet card per epoch');
  const c0 = sparkPointCount(cards[0]);
  const c1 = sparkPointCount(cards[1]);
  assertEqual(c0, 3, 'e0 card sparkline draws its 3 REAL generation points');
  assertEqual(c1, 5, 'e1 card sparkline draws its 5 REAL generation points');
  assert(c0 !== c1, 'the two cards render visibly different series (different length) — not one shared synthetic curve');

  // and the series are sourced from the PER-EPOCH endpoint (?epoch=<id>), so
  // they reflect each epoch's real data rather than the single current contract.
  assert(host.textContent.includes(FLEET_E0) && host.textContent.includes(FLEET_E1), 'both epoch cards rendered');
});

test('fleet cards: NO fabricated [best×1.18, best×1.06, best] fallback — an epoch with <2 real points shows the honest placeholder', async () => {
  freshState();
  // e0 keeps a real 3-point trajectory; e1 has only ONE real point (<2).
  const F = JSON.parse(JSON.stringify(FLEET_FIXTURE));
  F[`/api/score-trajectory?epoch=${FLEET_E1}`] = { epoch_id: FLEET_E1, points: [{ generation_id: 'v0', scalar: 56.2 }] };
  installFleetFetch(F);
  const home = await import('../js/views/home.js');
  const host = document.createElement('div');
  await home.render(host, { navigate() {}, href: router.href }, {});

  const cards = allByClass(host, 'dn-fleet-card');
  assertEqual(cards.length, 2, 'one fleet card per epoch');
  // e1 (one real point) → the honest "no trajectory yet" placeholder, NO path.
  assertEqual(sparkPointCount(cards[1]), 0, 'an epoch with <2 real points draws NO sparkline path');
  const placeholder = cards[1].querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').includes('dn-faint') && (n.textContent || '').includes('no trajectory yet'))[0];
  assert(placeholder, 'it shows the existing honest "no trajectory yet" placeholder');

  // e1 best_scalar is 20.500; the FABRICATED fallback would have produced the
  // descending [20.5×1.18, 20.5×1.06, 20.5] curve (3 points). Prove it is GONE.
  assert(sparkPointCount(cards[1]) !== 3, 'no synthetic 3-point [×1.18,×1.06,×1] curve is produced');
  // e0 still draws its real 3-point series unchanged.
  assertEqual(sparkPointCount(cards[0]), 3, 'the other epoch still draws its real trajectory');
});

test('fleet cards: existing rendering preserved — stats, epoch links, current-epoch highlight (full-width cross-epoch trajectory removed)', async () => {
  freshState(); installFleetFetch();
  const home = await import('../js/views/home.js');
  const host = document.createElement('div');
  await home.render(host, { navigate() {}, href: router.href }, {});

  const cards = allByClass(host, 'dn-fleet-card');
  // stats: best / gens / promoted are still on each card.
  assert(cards[0].textContent.includes('46.813') && cards[0].textContent.includes('5'), 'e0 card keeps its best + gen stats');
  // each card links to its epoch view.
  assertEqual(cards[0].getAttribute('href'), router.href('epoch', { epochId: FLEET_E0 }), 'e0 card links to the e0 epoch view');
  assertEqual(cards[1].getAttribute('href'), router.href('epoch', { epochId: FLEET_E1 }), 'e1 card links to the e1 epoch view');
  // the current epoch (e1) is highlighted.
  assert((cards[1].getAttribute('class') || '').includes('dn-is-current'), 'the current epoch card is highlighted');
  assert(!(cards[0].getAttribute('class') || '').includes('dn-is-current'), 'the non-current epoch card is not highlighted');

  // the full-width "Cross-epoch trajectory" sparkline was removed — the
  // composed meta-loop ledger (tested separately) is the cross-epoch overview.
  assert(!host.textContent.includes('Cross-epoch trajectory'), 'the full-width cross-epoch trajectory panel is gone');
});

test('fleet cards: digest-gated — identical workspace + trajectories do NOT rebuild the DOM (heartbeat no-op)', async () => {
  freshState(); installFleetFetch();
  const home = await import('../js/views/home.js');
  const host = document.createElement('div');
  await home.render(host, { navigate() {}, href: router.href }, {});
  const digest1 = host.getAttribute('data-t-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'environment painted');
  await home.render(host, { navigate() {}, href: router.href }, {});
  assertEqual(host.getAttribute('data-t-digest'), digest1, 'digest unchanged on the no-op repaint');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

// CROSS-EPOCH BUG 1 — the tree lists EVERY epoch's generations.
//
// buildTreeModel used a SINGLE current-epoch `bundleId`, so expanding a
// non-current epoch (e0) showed an EMPTY "Generations" node even though
// /api/lineage carries e0's rows. Now each epoch node fills its OWN gens from
// the lineage filtered by THAT node's epoch_id — neither epoch empty, no
// cross-contamination.
// ====================================================================

test('tree model (cross-epoch): EVERY epoch node lists its OWN generations (e0 not empty, e1 not empty, no leak)', async () => {
  freshState();
  // the WHOLE-workspace lineage spans BOTH epochs; the contract is the CURRENT
  // (e1) epoch. /api/workspace names both so both become tree nodes.
  const F = twoEpochFixture(TWO_EP_NEW);
  F['/api/workspace'] = {
    current_epoch_id: TWO_EP_NEW,
    epochs: [
      { epoch_id: TWO_EP_OLD, generation_count: 5, promoted_count: 2, closed: true, goal: 'e0' },
      { epoch_id: TWO_EP_NEW, generation_count: 3, promoted_count: 1, closed: false, goal: 'e1' },
    ],
    sparkline: [],
  };
  installFixtureMap(F);
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  // route at the CURRENT epoch (e1) — the OLD epoch (e0) is the non-current one.
  const model = await shell.buildTreeModel(router.parseRoute(`#/e/${TWO_EP_NEW}`));

  // both epochs are nodes.
  const ids = model.epochs.map((e) => e.id).sort();
  assert(ids.includes(TWO_EP_OLD) && ids.includes(TWO_EP_NEW), 'both epochs are tree nodes');

  // e0 (NON-current) lists ITS OWN 5 generations — NOT empty.
  const e0 = model.byEpoch[TWO_EP_OLD];
  assert(e0 && Array.isArray(e0.gens), 'the non-current e0 node has a gens bundle');
  assertEqual(e0.gens.length, 5, 'e0 lists its OWN 5 generations (not an empty Generations node)');
  assertDeep(e0.gens.map((g) => g.id).sort(), ['v0', 'v1', 'v2', 'v3', 'v4'], 'e0’s gens are exactly its own field {v0..v4}');

  // e1 (current) lists ITS OWN 3 generations — no cross-contamination from e0.
  const e1 = model.byEpoch[TWO_EP_NEW];
  assert(e1 && Array.isArray(e1.gens), 'the current e1 node has a gens bundle');
  assertEqual(e1.gens.length, 3, 'e1 lists its OWN 3 generations');
  assertDeep(e1.gens.map((g) => g.id).sort(), ['v0', 'v1', 'v2'], 'e1’s gens are exactly its own field {v0,v1,v2} (no e0 leak)');

  // the current-epoch marker stays on e1; e0’s board node is empty (its board
  // resolves when e0 is viewed — the boards/mutation/publication children are
  // not regressed).
  assert(model.epochs.find((e) => e.id === TWO_EP_NEW).current, 'e1 keeps the current marker');
  assert(Array.isArray(e1.boards) && e1.boards.length >= 1, 'the contract (e1) node still lists its boards');
});

// ====================================================================
// CROSS-EPOCH BUG 2 — Match-ups live state is gated to the ACTIVE epoch.
//
// gens.js read deriveLiveStatus() from the GLOBAL state and adopted the LIVE
// topology regardless of which epoch was viewed — so a CLOSED e0's Match-ups
// showed e1's live "being seeded" ladder. The live topology is now adopted
// ONLY when the viewed epoch IS the active one (state.activeTournament.epoch_id).
// ====================================================================

test('gens (cross-epoch): a NON-active epoch’s Match-ups renders the COMPLETED structure, NOT the active epoch’s live ladder', async () => {
  freshState();
  // e1 is racing LIVE (a running active-tournament tagged epoch_id=e1); we VIEW
  // the CLOSED e0. e0 must show its COMPLETED racing ladder, never e1's live
  // "being seeded" empty state and never the LIVE pill.
  const F = twoEpochFixture(TWO_EP_OLD);
  installFixtureMap(F);
  coreState.state.setHeartbeat({ phase: 'tournament:round_0:rung0_m0', generation_id: 'v1', epoch_id: TWO_EP_NEW });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.3 }];
  coreState.state.activeTournament = { epoch_id: TWO_EP_NEW, structure: 'racing', phase: 'running' };

  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: TWO_EP_OLD });

  // NO live leak from e1 onto e0.
  assertEqual(allByClass(host, 'dt-live-pill').length, 0, 'NO LIVE pill on the closed e0 view (e1’s live run does not leak)');
  assert(!/being seeded|is being seeded|run is starting/i.test(host.textContent), 'NOT e1’s live "being seeded"/"starting" empty state under e0');
  // e0 renders its OWN completed survival funnel (reconstructed from its records).
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'e0 renders its OWN completed survival funnel (not the live topology)');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

test('gens (cross-epoch): the ACTIVE epoch’s Match-ups still shows the live progressive ladder (no regression)', async () => {
  freshState();
  // VIEW the ACTIVE e1 while it races live — the live progressive racing ladder
  // must still render (the racing-ladder redesign is preserved).
  const F = twoEpochFixture(TWO_EP_NEW);
  F['/api/active-tournament'] = {
    epoch_id: TWO_EP_NEW, tournament_id: `tourn_${TWO_EP_NEW}_v1`, structure: 'racing', phase: 'running',
    structure_params: { field_size: 3, eta: 2, board_fraction: 0.25 },
    round_index: 0, total_rounds: 2,
    competitors: [
      { generation_id: 'v0', seed: 1, role: 'champion' },
      { generation_id: 'v1', seed: 2, role: 'challenger' },
      { generation_id: 'v2', seed: 3, role: 'challenger' },
    ],
    // NEW contract: the backend publishes the active rung-0 + the pending gate.
    rounds: [
      { round_index: 0, label: 'Rung 0', matches: [{ match_id: 'rung0', competitors: ['v1', 'v2'], survivors: [], cut: [], board_fraction: 0.25, pending: true }] },
      { round_index: 1, label: 'Champion gate', matches: [{ match_id: 'racing-final', competitors: ['v0'], board_fraction: 1.0, winner: null, pending: true }] },
    ],
    standings: [], champion_lineage: ['v0'],
  };
  installFixtureMap(F);
  coreState.state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', generation_id: 'v1', epoch_id: TWO_EP_NEW });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = { epoch_id: TWO_EP_NEW, structure: 'racing', phase: 'running' };

  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: TWO_EP_NEW });

  assert(allByClass(host, 'dt-live-pill')[0], 'the active e1 view carries the LIVE pill');
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'the live progressive survival funnel renders for the active epoch');
  assert(!/being seeded/i.test(host.textContent), 'NOT the "being seeded" empty state once the live field exists');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

// ---- typeface OPTIONS: the operator's finalized 12 faces (4 per mode) -----
//
// Each finalized id (T7 / T9 / … / D5) has a per-id CSS rule that swaps the four
// font-role tokens (--v2-sans / --v2-mono / --n-font-head / --n-font-paper) to
// the option's stacks, lifted byte-for-byte from the study. We assert each id's
// block exists AND that its head/prose/data faces match the study mapping. The
// JS model (ui.TYPE_OPTIONS) is the source of truth; the CSS must agree with it.
test('typeface options: each of the 12 finalized ids has a CSS rule whose font-role tokens match the study stacks', () => {
  const css = readCss();
  function typeBlock(id) {
    const m = css.match(new RegExp('#variant-root\\[data-variant="T"\\]\\[data-t-type="' + id + '"\\]\\s*\\{([^}]*)\\}'));
    assert(m, 'the ' + id + ' typeface block exists');
    return m[1];
  }
  function declIn(block, name) {
    const m = block.match(new RegExp('--' + name + '\\s*:\\s*([^;]+);'));
    return m ? m[1].trim() : null;
  }
  // the primary family inside a stack (between the first pair of quotes).
  function primary(stack) {
    const m = String(stack || '').match(/'([^']+)'/);
    return m ? m[1] : null;
  }
  // EXPECTED head/prose/data primaries per the study mapping.
  const expect = {
    T7:  { head: 'Google Sans Mono', prose: 'Google Sans Mono', data: 'Google Sans Mono' },
    T9:  { head: 'Source Sans 3',    prose: 'Source Sans 3',    data: 'Source Code Pro' },
    T12: { head: 'Inconsolata',      prose: 'Inconsolata',      data: 'Inconsolata' },
    T14: { head: 'Ubuntu',           prose: 'Ubuntu',           data: 'Ubuntu Mono' },
    E5:  { head: 'Fraunces',         prose: 'Fraunces',         data: 'Fraunces' },
    E7:  { head: 'Bitter',           prose: 'Bitter',           data: 'Bitter' },
    E8:  { head: 'Literata',         prose: 'Literata',         data: 'Literata' },
    E15: { head: 'Domine',           prose: 'Domine',           data: 'Domine' },
    D2:  { head: 'Archivo Narrow',   prose: 'Space Grotesk',    data: 'Space Grotesk' },
    D12: { head: 'Hanken Grotesk',   prose: 'Hanken Grotesk',   data: 'Hanken Grotesk' },
    D14: { head: 'Barlow Condensed', prose: 'Space Grotesk',    data: 'Space Grotesk' },
    D5:  { head: 'Bricolage Grotesque', prose: 'Bricolage Grotesque', data: 'Bricolage Grotesque' },
  };
  for (const id of Object.keys(expect)) {
    const b = typeBlock(id);
    // role → token mapping: head→--n-font-head, prose→--v2-sans (+ --n-font-paper),
    // data→--v2-mono.
    assertEqual(primary(declIn(b, 'n-font-head')), expect[id].head, id + ' head face → --n-font-head');
    assertEqual(primary(declIn(b, 'v2-sans')), expect[id].prose, id + ' prose face → --v2-sans');
    assertEqual(primary(declIn(b, 'n-font-paper')), expect[id].prose, id + ' prose face → --n-font-paper');
    assertEqual(primary(declIn(b, 'v2-mono')), expect[id].data, id + ' data/code face → --v2-mono');
    // the CSS must agree with the JS model for the same id.
    const opt = ui.TYPE_OPTIONS.find((o) => o.id === id);
    assertEqual(primary(opt.head), expect[id].head, id + ' JS model head matches');
    assertEqual(primary(opt.prose), expect[id].prose, id + ' JS model prose matches');
    assertEqual(primary(opt.data), expect[id].data, id + ' JS model data matches');
  }
  // the DEFAULT block (no data-t-type) lands on the T7 voice (Google Sans Mono).
  const baseM = css.match(/#variant-root\[data-variant="T"\]\s*\{([^}]*--v2-sans[^}]*)\}/);
  assert(baseM, 'the base [data-variant="T"] token block declares the default font roles');
  assert(/Google Sans Mono/.test(baseM[1]), 'the default (no data-t-type) voice is Google Sans Mono (T7)');
});

// the brand wordmark pins to a FIXED brand mono, INDEPENDENT of the user's
// typeface — so its dot stays centred regardless of the selected typeface.
test('brand mono: --v2-brand-mono is a FIXED monospace, distinct from the swappable --v2-mono token', async () => {
  const css = readCss();
  const baseM = css.match(/#variant-root\[data-variant="T"\]\s*\{([^}]*)\}/);
  assert(baseM, 'the base token block exists');
  const base = baseM[1];
  const brand = (base.match(/--v2-brand-mono\s*:\s*([^;]+);/) || [])[1];
  assert(brand, 'the base block declares a fixed --v2-brand-mono token');
  assert(/monospace\s*$/.test(brand.trim()), 'the brand mono stack ends in the generic monospace keyword');
  // it is NOT declared inside any per-OPTION block, so it never swaps with the UI.
  for (const id of ['T7', 'T9', 'T12', 'T14', 'E5', 'E7', 'E8', 'E15', 'D2', 'D12', 'D14', 'D5']) {
    const m = css.match(new RegExp('#variant-root\\[data-variant="T"\\]\\[data-t-type="' + id + '"\\]\\s*\\{([^}]*)\\}'));
    assert(m && !/--v2-brand-mono/.test(m[1]), 'the ' + id + ' typeface block does NOT re-declare the brand mono (it stays fixed)');
  }
  // the wordmark <text> pins to the fixed brand mono (not the swappable mono).
  const fsmod = await import('node:fs');
  const src = fsmod.readFileSync(new URL('../js/shell.js', import.meta.url), 'utf8');
  assert(/var\(--v2-brand-mono\)/.test(src), 'the wordmark text font-family is var(--v2-brand-mono)');
});

// FONTS — a SPLIT loading strategy:
//   * The two self-hosted monos (iA Writer Mono + JetBrains Mono) stay SELF-
//     HOSTED woff2 declared via @font-face in the scoped CSS (JetBrains Mono
//     still backs the fixed brand mono) — those never touch a CDN.
//   * The typeface picker's finalized 12 faces load from the Google-Fonts loader
//     in app_T.js (preconnect + a single css2 request, display=swap). Every
//     family the 12 options reference must be in that request.
test('fonts: the two self-hosted monos stay woff2; the 12 finalized faces load via the Google-Fonts loader (preconnect + display=swap)', async () => {
  const css = readCss();
  // the two self-hosted monos are still declared via @font-face from local woff2.
  for (const fam of ['iA Writer Mono', 'JetBrains Mono']) {
    const re = new RegExp('@font-face[^}]*font-family:\\s*"' + fam + '"[^}]*url\\([^)]*\\.woff2[^)]*\\)\\s*format\\("woff2"\\)', 's');
    assert(re.test(css), '@font-face declares ' + fam + ' from a local .woff2');
  }
  assert(!/Space Mono/.test(css), 'Space Mono is no longer referenced in the CSS');
  assert(/font-display:\s*swap/.test(css), 'self-hosted faces load with font-display: swap');
  // every @font-face src is LOCAL (no external host) — the self-hosted monos.
  const faces = css.match(/@font-face\s*\{[^}]*\}/gs) || [];
  assert(faces.length >= 2, 'at least two @font-face blocks declared (iA Writer Mono + JetBrains Mono)');
  for (const f of faces) assert(!/url\(\s*['"]?https?:/.test(f), 'a face src is a LOCAL url (no http/https CDN)');

  const fs = await import('node:fs');
  const appJs = fs.readFileSync(new URL('../app_T.js', import.meta.url), 'utf8');
  // EVERY family the 12 finalized options reference loads from the Google-Fonts
  // request; the self-hosted monos must NOT be in it.
  const loaded = [...appJs.matchAll(/family=([A-Za-z0-9+]+)/g)].map((m) => m[1].replace(/\+/g, ' '));
  const NEEDED = [
    'Google Sans Mono', 'Noto Sans Mono', 'Source Sans 3', 'Source Code Pro',
    'Inconsolata', 'Ubuntu', 'Ubuntu Mono',
    'Fraunces', 'Bitter', 'Literata', 'Domine',
    'Archivo Narrow', 'Space Grotesk', 'Hanken Grotesk', 'Barlow Condensed',
    'Bricolage Grotesque',
  ];
  for (const fam of NEEDED) {
    assert(loaded.includes(fam), 'app_T.js loads the ' + fam + ' family (display=swap)');
  }
  assert(!loaded.includes('JetBrains Mono'), 'JetBrains Mono is self-hosted, NOT requested from the CDN');
  assert(!loaded.includes('iA Writer Mono'), 'iA Writer Mono is self-hosted, NOT requested from the CDN');
  assert(/display=swap/.test(appJs), 'CDN fonts are requested with display=swap');
  // a preconnect to the Google-Fonts origins is set up before the stylesheet.
  assert(/rel\s*=\s*['"]preconnect['"]/.test(appJs), 'app_T.js preconnects to the font origins');
  assert(/fonts\.gstatic\.com/.test(appJs), 'app_T.js preconnects to the gstatic woff2 host');

  // the self-hosted woff2 files actually ship on disk under static/fonts/.
  const path = await import('node:path');
  const fontsDir = path.dirname(new URL('../app_T.js', import.meta.url).pathname) + '/fonts';
  for (const f of ['JetBrainsMono-Regular.woff2', 'iAWriterMonoS-Regular.woff2']) {
    assert(fs.existsSync(fontsDir + '/' + f) && fs.statSync(fontsDir + '/' + f).size > 0, 'ships ' + f);
  }
});

// ── A18 · the frozen CONTRACT HASH · and A11 · the Δscalar tiles ─────────────
//
// `contract_hash` (build_epoch_view, off config.json) named WHICH contract the
// epoch froze and was read by NO js and no CLI. `delta_scalar_summary`
// ({champion_spine, gross}) is documented in build_epoch_view as tiles the
// caller renders — and had no caller.

const epochViewMod = await import('../js/views/epoch.js');

const CONTRACT_HASH = 'feedfacecafebabe0123456789abcdef0123456789abcdef';

function epochHeaderFixture(over) {
  const ep = Object.assign({}, FIXTURE['/api/epoch'], {
    contract_hash: CONTRACT_HASH,
    delta_scalar_summary: { champion_spine: -12.5, gross: 3.25 },
  }, over || {});
  const F = Object.assign({}, FIXTURE);
  F['/api/epoch'] = ep;
  F[`/api/epoch?epoch=${EPOCH_ID}`] = ep;
  return F;
}

test('A18 · epoch.shortHash: the builder short-hash idiom (12 chars + ellipsis)', () => {
  assertEqual(epochViewMod.shortHash(CONTRACT_HASH), 'feedfacecafe…', 'a long hash is shortened');
  assertEqual(epochViewMod.shortHash('abc'), 'abc', 'a short hash passes through whole');
  assertEqual(epochViewMod.shortHash(null), '', 'an absent hash reads empty');
});

test('A18 · the epoch header names WHICH contract the epoch froze (shortened, full on hover)', async () => {
  freshState(); installFixtureMap(epochHeaderFixture());
  const host = document.createElement('div');
  await epochViewMod.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  const el = allByClass(host, 'dt-contract-hash')[0];
  assert(el, 'the epoch header carries the contract hash');
  assert(String(el.textContent).includes('feedfacecafe'), 'it renders the shortened hash');
  assertEqual(el.getAttribute('title'), CONTRACT_HASH, 'the FULL hash is on hover — nothing is lost');
});

test('A18 · an epoch with no contract_hash renders no hash chip (byte-identical to before)', async () => {
  freshState(); installFixtureMap(epochHeaderFixture({ contract_hash: undefined }));
  const host = document.createElement('div');
  await epochViewMod.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assertEqual(allByClass(host, 'dt-contract-hash').length, 0, 'no chip when the epoch froze no recorded hash');
});

test('A11 · the epoch header renders BOTH Δscalar tiles, spine first, gross labelled secondary', async () => {
  freshState(); installFixtureMap(epochHeaderFixture());
  const host = document.createElement('div');
  await epochViewMod.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  const txt = host.textContent;
  assert(txt.includes('Δ scalar · champion spine'), 'the champion-spine tile is labelled');
  assert(txt.includes('Δ scalar · gross (all experiments)'), 'the gross tile is labelled as the all-experiments sum');
  assert(txt.includes('-12.50'), 'the spine sum renders signed');
  assert(txt.includes('+3.25'), 'the gross sum renders signed');
  assert(txt.indexOf('champion spine') < txt.indexOf('gross (all experiments)'),
    'the spine number leads — it is the meta-loop’s actual progress');
  assert(txt.includes('the champion-spine sum counts PROMOTED hops only'),
    'the caption says why gross is not the headline');
});

test('A11 · an absent delta reads "—" (exactly as build_epoch_view documents)', async () => {
  freshState(); installFixtureMap(epochHeaderFixture({ delta_scalar_summary: { champion_spine: null, gross: null } }));
  const host = document.createElement('div');
  await epochViewMod.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(host.textContent.includes('Δ scalar · champion spine'), 'the tile still renders');
  assert(host.textContent.includes('—'), 'an unrecorded delta reads as a dash, never a fabricated 0');
});

test('A18 + A11 · both are FOLDED into the epoch digest (a re-freeze / a new promotion repaints)', async () => {
  freshState(); installFixtureMap(epochHeaderFixture());
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await epochViewMod.render(host, ctx, { epochId: EPOCH_ID });
  const first = host.getAttribute('data-t-digest');
  assert(first.includes('contractHash'), 'contractHash is folded into the epoch digest');
  assert(first.includes('deltaSummary'), 'deltaSummary is folded into the epoch digest');

  // a no-op beat: identical payload → zero DOM.
  const node = host.firstChild;
  freshState(); installFixtureMap(epochHeaderFixture());
  await epochViewMod.render(host, ctx, { epochId: EPOCH_ID });
  assertEqual(host.getAttribute('data-t-digest'), first, 'an identical epoch payload is a digest no-op');
  assert(host.firstChild === node, 'no rebuild on the no-op beat');

  // the contract re-frozen — everything else identical — must repaint.
  freshState(); installFixtureMap(epochHeaderFixture({ contract_hash: 'ffffffffffffffffffffffffffffffff' }));
  await epochViewMod.render(host, ctx, { epochId: EPOCH_ID });
  assert(host.getAttribute('data-t-digest') !== first, 'a re-frozen contract flips the epoch digest');

  // and a moved spine sum alone must repaint too.
  freshState(); installFixtureMap(epochHeaderFixture());
  await epochViewMod.render(host, ctx, { epochId: EPOCH_ID });
  const base = host.getAttribute('data-t-digest');
  freshState(); installFixtureMap(epochHeaderFixture({ delta_scalar_summary: { champion_spine: -20.0, gross: 3.25 } }));
  await epochViewMod.render(host, ctx, { epochId: EPOCH_ID });
  assert(host.getAttribute('data-t-digest') !== base, 'a moved champion-spine Δ flips the epoch digest');
});

await run();

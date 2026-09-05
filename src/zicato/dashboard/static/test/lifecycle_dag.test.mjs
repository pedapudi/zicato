// test/lifecycle_dag.test.mjs — console unit tests covering
// mutation-surface click semantics, the lifecycle DAG (board column,
// normalized layout, expandable runs, per-board loss), the survival funnel,
// the hovercard, rail sizing, and the up button.
//
// Shared fixtures and helpers live in ./fixtures.mjs.

import { installDom, test, run, assert, assertEqual, assertDeep, makeEvent } from './harness.mjs';

installDom();

const {
  router, svg, ui, shell, data, tree,
  coreState, rounds, dag, hovercard, live, STRUCT,
  recorded, recordedRoutes, racingLadderFixture, structureFixture, EPOCH_ID, FIXTURE, lookupFixture, installFetch, freshState,
  allByClass, readCss, hovercardTextOf, hasNativeTitle, svgsByClass, hasScrollWrapperAncestor,
  mountLiveShell, installFixtureMap, LIVE_RACING, RC_EPOCH,
} = await import('./fixtures.mjs');

// ====================================================================
// Two behaviours the mutation surface and the tree must hold:
//   * mutation-surface click semantics. A CELL (site × generation) opens
//     ONLY that one generation's side-by-side diff for the site; the SITE
//     row label opens ALL generations that patched the site, stacked.
//   * the tree lists an existing epoch on EVERY route; the "No epochs"
//     empty state shows only when the workspace holds zero epochs.
// ====================================================================

// ---- the router carries the per-cell generation ----------------------

test('mutations route: a CELL pins mutId + gen; the SITE pins mutId only (round-trips)', () => {
  // a bare mutId → the SITE (all generations) selection.
  const site = router.parseRoute(`#/e/${EPOCH_ID}/mutations/oversight_policy`);
  assertEqual(site.view, 'mutations');
  assertEqual(site.params.mutId, 'oversight_policy');
  assert(!site.params.gen, 'a bare mutId carries NO generation (the all-gens SITE view)');
  // a mutId + gen → ONE site×generation CELL selection.
  const cell = router.parseRoute(`#/e/${EPOCH_ID}/mutations/oversight_policy/v2`);
  assertEqual(cell.params.mutId, 'oversight_policy');
  assertEqual(cell.params.gen, 'v2', 'the trailing segment is the pinned cell generation');
  // both hrefs round-trip.
  assertEqual(router.href('mutations', { epochId: EPOCH_ID, mutId: 'oversight_policy' }),
    `#/e/${EPOCH_ID}/mutations/oversight_policy`, 'the SITE href omits the gen');
  assertEqual(router.href('mutations', { epochId: EPOCH_ID, mutId: 'oversight_policy', gen: 'v2' }),
    `#/e/${EPOCH_ID}/mutations/oversight_policy/v2`, 'the CELL href appends the gen');
  // back/up: a cell steps up to the site (all-gens) view; the site steps to the epoch.
  assertEqual(router.up(cell).view, 'mutations');
  assertEqual(router.up(cell).params.gen, undefined, 'a cell steps up to the SITE (all gens) — gen dropped');
  assertEqual(router.up(cell).params.mutId, 'oversight_policy', 'the site view keeps the mutId');
  // the bare-mutId SITE view steps up to the mutation-surface root (mutId dropped).
  const upSite = router.up(site);
  assertEqual(upSite.view, 'mutations', 'the site steps up to the mutation-surface root');
  assert(!upSite.params.mutId, 'the mutation-surface root drops the mutId');
});

// ---- clicking a CELL renders exactly ONE generation's diff -----------

test('mutation surface: clicking a CELL renders exactly ONE generation’s side-by-side diff for that site (not all)', async () => {
  freshState(); installFetch();
  const mutations = await import('../js/views/mutations.js');

  // oversight_policy was patched by BOTH v1 and v2 with DIFFERENT content.
  // Pin the v2 CELL → only v2's diff appears (not v1's).
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await mutations.render(host, ctx, { epochId: EPOCH_ID, mutId: 'oversight_policy', gen: 'v2' });

  const blocks = allByClass(host, 'dn-patch-block');
  assertEqual(blocks.length, 1, 'exactly ONE generation diff block for the pinned cell (v2 only — not v1+v2)');
  const sxs = allByClass(host, 'dn-sxs');
  assertEqual(sxs.length, 1, 'exactly one side-by-side diff component');
  // it is v2's content, with REAL strings (never the baseline object).
  assert(host.textContent.includes('Loosen coordinator oversight'), 'the v2 challenger new_content (the pinned cell) is shown');
  assert(!host.textContent.includes('Tighten coordinator oversight'), 'v1’s patch is NOT shown when only the v2 cell is pinned');
  assert(host.textContent.includes('Default oversight'), 'the champion baseline string (LEFT) is shown');
  assert(!host.textContent.includes('[object Object]'), 'never the baseline OBJECT');
  // the cells carry both identities so the click can pin one generation.
  const cells = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-mtx-cell') && n.getAttribute('data-gen'));
  assert(cells.length >= 1, 'matrix cells carry data-gen + data-site (the cell’s generation + site identity)');
  assert(cells.every((c) => c.getAttribute('data-site')), 'every cell carries its data-site');
  // the cell link targets the mutId+gen route (one generation) rather than the site.
  const cellLink = allByClass(host, 'dn-mtx-celllink')[0];
  assert(cellLink && (cellLink.getAttribute('href') || '').endsWith('/mutations/coordinator_prompt/v1')
    || (cellLink.getAttribute('href') || '').includes('/mutations/'), 'a cell link routes to mutId/gen');
  const anyCellHrefHasGen = allByClass(host, 'dn-mtx-celllink')
    .some((a) => /\/mutations\/[^/]+\/v\d+$/.test(a.getAttribute('href') || ''));
  assert(anyCellHrefHasGen, 'at least one cell link carries the trailing /<gen> (one-generation affordance)');
});

test('mutation surface: generation columns order by CREATION order (v0,v1,v2,…,v10,v11), not lexically (v0,v1,v10,v2)', async () => {
  freshState();
  installFixtureMap({
    '/api/epoch': { epoch_id: EPOCH_ID, closed: false, goal: 'g' },
    // the API returns the generations in LEXICAL string order — the view must
    // re-sort them to creation (numeric vN) order for the columns.
    [`/api/mutations/${EPOCH_ID}`]: {
      generations: ['v0', 'v1', 'v10', 'v11', 'v2'],
      mutations: [
        { mutation_id: 'm', kind: 'prompt', file: 'a.py', role: 'r', line_start: 1, line_end: 2, patched_generation_ids: ['v10'] },
      ],
    },
  });
  const mutations = await import('../js/views/mutations.js');
  const host = document.createElement('div');
  await mutations.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  const cols = allByClass(host, 'dn-mtx-gen').map((th) => th.textContent.trim());
  assertEqual(cols.join(','), 'v0,v1,v2,v10,v11',
    `generation columns in creation order, not lexical (got ${JSON.stringify(cols)})`);
});

// ---- §9.15-step-7 no-op identity: the renderView scaffold is digest-gated ----

test('mutation surface: a no-op re-render does NOT clear-and-rebuild the DOM (renderView digest gate)', async () => {
  freshState(); installFetch();
  const mutations = await import('../js/views/mutations.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await mutations.render(host, ctx, { epochId: EPOCH_ID, mutId: 'oversight_policy' });
  const digest1 = host.getAttribute('data-t-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'the mutation surface painted');
  await mutations.render(host, ctx, { epochId: EPOCH_ID, mutId: 'oversight_policy' });
  assertEqual(host.getAttribute('data-t-digest'), digest1, 'digest unchanged on the no-op repaint');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint (firstChild identity)');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

// ---- clicking the SITE row label renders ALL generations -------------

test('mutation surface: clicking the SITE row label renders ALL generations that patched the site, stacked', async () => {
  freshState(); installFetch();
  const mutations = await import('../js/views/mutations.js');

  // pin the SITE (no gen) → BOTH v1 and v2 diffs for oversight_policy stack.
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await mutations.render(host, ctx, { epochId: EPOCH_ID, mutId: 'oversight_policy' });

  const blocks = allByClass(host, 'dn-patch-block');
  assertEqual(blocks.length, 2, 'BOTH generations (v1, v2) that patched the site are stacked');
  const sxs = allByClass(host, 'dn-sxs');
  assertEqual(sxs.length, 2, 'two side-by-side diff components (one per generation)');
  assert(host.textContent.includes('Tighten coordinator oversight'), 'v1’s patch is shown in the all-gens view');
  assert(host.textContent.includes('Loosen coordinator oversight'), 'v2’s patch is shown in the all-gens view');
  assert(host.textContent.includes('Default oversight'), 'the champion baseline string (LEFT) is shown');
  assert(!host.textContent.includes('[object Object]'), 'never the baseline OBJECT');

  // the SITE row label is the all-gens affordance — it links to the BARE mutId
  // (no trailing gen), distinct from the per-cell links.
  const siteLink = allByClass(host, 'dn-mtx-sitelink')[0];
  assert(siteLink, 'the site row label is a link');
  const siteHref = siteLink.getAttribute('href') || '';
  assert(/\/mutations\/[^/]+$/.test(siteHref), 'the site link routes to the BARE mutId (all generations) — no trailing gen');
});

// ---- the tree lists an existing epoch on every route -----------------

test('tree (BUG 2): /api/lineage generations across an epoch make the tree LIST that epoch — never "No epochs"', async () => {
  freshState();
  // A workspace feed with NO epochs roster (the sparse-route case that produced
  // the bug), an /api/epoch that 404s, but /api/lineage plainly carries the
  // epoch's generations grouped by epoch_id. The tree must STILL list the epoch.
  const PUB_EPOCH = '2026-06-01_e0';
  const F = {
    '/api/workspace': { current_epoch_id: null, sparkline: [] },   // no `epochs` array
    '/api/lineage': { generations: [
      { generation_id: 'v0', epoch_id: PUB_EPOCH, parent_generation_id: '', promoted: true },
      { generation_id: 'v1', epoch_id: PUB_EPOCH, parent_generation_id: 'v0', promoted: false },
    ] },
    '/api/tournaments': { epoch_id: PUB_EPOCH, champion_lineage: ['v0'], matchups: [] },
    // /api/epoch is absent here → 404, which is the publication-route case.
  };
  globalThis.fetch = async (path) => {
    const v = lookupFixture(F, path);
    return v !== undefined
      ? { ok: true, json: async () => v }
      : { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };

  // mount the live shell on the PUBLICATION route for this epoch.
  const root = mountLiveShell(`#/e/${PUB_EPOCH}/paper`);
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  const rail = allByClass(root, 'dt-sidebar')[0];
  assert(rail, 'the rail mounted');
  assert(!rail.textContent.includes('No epochs in this workspace yet'),
    'the tree does NOT show the empty state when /api/lineage carries the epoch');
  assert(rail.textContent.includes(PUB_EPOCH), 'the tree LISTS the existing epoch (from /api/lineage, on the publication route)');
  // and the epoch's generations resolve into its bundle.
  const epochs2 = [{ id: PUB_EPOCH, current: true }];
  assert(epochs2.length === 1, 'fixture sanity');
});

test('tree (BUG 2): the "No epochs" empty state shows ONLY when there are genuinely zero epochs', async () => {
  freshState();
  // every authoritative source is empty: no workspace epochs, no lineage gens,
  // no /api/epoch, and the route is the bare environment root (no routed epoch).
  const F = {
    '/api/workspace': { current_epoch_id: null, epochs: [], sparkline: [] },
    '/api/lineage': { generations: [] },
    '/api/tournaments': { matchups: [] },
  };
  globalThis.fetch = async (path) => {
    const v = lookupFixture(F, path);
    return v !== undefined
      ? { ok: true, json: async () => v }
      : { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };

  const root = mountLiveShell('#/');
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  const rail = allByClass(root, 'dt-sidebar')[0];
  assert(rail, 'the rail mounted');
  assert(rail.textContent.includes('No epochs in this workspace yet'),
    'with genuinely zero epochs the empty state IS shown');
});

// ====================================================================
// Lifecycle DAG · BOARD column: dedupe per ENTRY (rung multiplicity),
// and label/circle text-spacing. A RACING candidate re-runs the SAME
// board entry across rungs (rung0 slice → rung1 → racing-final full
// board), so the raw per-entry stream repeats an entry_id N times.
// ====================================================================

// collect the BOARD-column node groups of a freshly built lifecycle DAG.
function boardNodesOf(svgNode) {
  return svgNode.querySelectorAll('[class]').filter((n) =>
    n.localName === 'g' && (n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-node'));
}
function childByClass(g, cls) {
  return g.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls))[0] || null;
}

test('lifecycle BOARD column: a RACING candidate dedupes to ONE node per distinct entry (count == distinct entries, not total runs) + annotates rung multiplicity', () => {
  // v3: a racing candidate. The SAME entries recur across rungs:
  //   q3_metrics_outline ×3, waffles_single ×2, picky_stakeholder_emulated (once).
  // The last run per entry is the racing-final / full-board run (representative).
  const entries = [
    { entry_id: 'q3_metrics_outline', run_id: 'r0a', drift_loss: 90.0, pass_fail: false, wall_clock_budget_exceeded: false },
    { entry_id: 'q3_metrics_outline', run_id: 'r1a', drift_loss: 85.0, pass_fail: false, wall_clock_budget_exceeded: false },
    { entry_id: 'q3_metrics_outline', run_id: 'r2a', drift_loss: 80.0, pass_fail: true, wall_clock_budget_exceeded: false },
    { entry_id: 'waffles_single', run_id: 'r0b', drift_loss: 60.0, pass_fail: false, wall_clock_budget_exceeded: false },
    { entry_id: 'waffles_single', run_id: 'r1b', drift_loss: 61.0, pass_fail: false, wall_clock_budget_exceeded: true },
    { entry_id: 'picky_stakeholder_emulated', run_id: 'r0c', drift_loss: 105.0, pass_fail: false, wall_clock_budget_exceeded: false },
  ];
  const distinct = new Set(entries.map((e) => e.entry_id));
  const svgNode = dag.lifecycleDag({ genId: 'v3', parentId: 'v0', entries, decision: 'rejected', height: 360 });
  const nodes = boardNodesOf(svgNode);

  assertEqual(nodes.length, distinct.size, 'ONE board node per DISTINCT entry (3), not one per run (6)');
  const keys = nodes.map((n) => n.getAttribute('data-key')).sort();
  assertDeep(keys, ['picky_stakeholder_emulated', 'q3_metrics_outline', 'waffles_single'], 'each distinct entry appears exactly once');

  // multiplicity: q3 (×3) and waffles (×2) carry a badge; picky (×1) does not.
  const byKey = {}; for (const n of nodes) byKey[n.getAttribute('data-key')] = n;
  assertEqual(byKey['q3_metrics_outline'].getAttribute('data-mult'), '3', 'q3 ran across 3 rungs');
  assertEqual(byKey['waffles_single'].getAttribute('data-mult'), '2', 'waffles ran across 2 rungs');
  assertEqual(byKey['picky_stakeholder_emulated'].getAttribute('data-mult'), '1', 'picky ran once');

  const q3mult = childByClass(byKey['q3_metrics_outline'], 'ezn-board-mult');
  assert(q3mult && q3mult.textContent === '×3', 'a re-raced entry carries a "×N rungs" multiplicity badge (×3)');
  assert(childByClass(byKey['waffles_single'], 'ezn-board-mult'), 'waffles (raced ×2) carries a multiplicity badge');
  assert(!childByClass(byKey['picky_stakeholder_emulated'], 'ezn-board-mult'), 'a once-run entry carries NO multiplicity badge');

  // representative loss = the LAST (racing-final / full-board) run, never rung0.
  const q3loss = childByClass(byKey['q3_metrics_outline'], 'ezn-board-loss');
  assertEqual(q3loss.textContent, svg.fmt(80.0, 0), 'the node shows the representative (final full-board) loss, not the rung0 loss');

  // the raced nodes carry the marker class so the disc renders distinctly.
  assert((byKey['q3_metrics_outline'].getAttribute('class') || '').includes('ezn-board-raced'), 'a re-raced node is marked ezn-board-raced');
  assert(!(byKey['picky_stakeholder_emulated'].getAttribute('class') || '').includes('ezn-board-raced'), 'a once-run node is NOT marked raced');
});

test('lifecycle BOARD column: labels never overlap the loss disc + rows are vertically spaced', () => {
  const entries = [
    { entry_id: 'q3_metrics_outline', drift_loss: 80, pass_fail: false },
    { entry_id: 'q3_metrics_outline', drift_loss: 80, pass_fail: false },
    { entry_id: 'waffles_single', drift_loss: 60, pass_fail: false },
    { entry_id: 'picky_stakeholder_emulated', drift_loss: 105, pass_fail: false },
    { entry_id: 'every_expectation_kind', drift_loss: 40, pass_fail: true },
  ];
  const h = 360;
  const svgNode = dag.lifecycleDag({ genId: 'v3', parentId: 'v0', entries, decision: 'rejected', height: h });
  const nodes = boardNodesOf(svgNode);

  // (b) the entry label is offset from the disc — anchored at its END and placed
  // to the LEFT of the circle (x < disc cx) — so a label can NEVER sit on the disc.
  for (const g of nodes) {
    const disc = childByClass(g, 'ezn-board-disc');
    const label = childByClass(g, 'ezn-board-label');
    const cx = +disc.getAttribute('cx');
    const r = +disc.getAttribute('r');
    const lx = +label.getAttribute('x');
    assertEqual(label.getAttribute('text-anchor'), 'end', 'the label is end-anchored (grows leftward, away from the disc)');
    assert(lx <= cx - r, `the label x (${lx}) is left of the disc’s left edge (${cx - r}) — no overlap with the circle`);
    // long ids are clipped with an ellipsis so the label never runs into the disc.
    assert(label.textContent.length <= 18, 'a long entry id is truncated for the node label');
  }

  // adjacent rows are spaced by a comfortable vertical gap (legible, no overlap).
  const cys = nodes.map((g) => +childByClass(g, 'ezn-board-disc').getAttribute('cy')).sort((a, b) => a - b);
  for (let i = 1; i < cys.length; i++) {
    assert(cys[i] - cys[i - 1] >= 24, `row gap (${cys[i] - cys[i - 1]}) is at least a 24px minimum so labels never collide`);
  }
});

test('lifecycle BOARD column: a GAUNTLET candidate (one run per entry) renders unchanged — one node per entry, NO spurious multiplicity badge', () => {
  // the gauntlet path: each board entry is run exactly once. Dedupe is a no-op.
  const entries = [
    { entry_id: 'waffles_single', run_id: 'g1', drift_loss: 60.5, pass_fail: false, wall_clock_budget_exceeded: false },
    { entry_id: 'picky_stakeholder_emulated', run_id: 'g2', drift_loss: 105.5, pass_fail: false, wall_clock_budget_exceeded: true },
  ];
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: 360 });
  const nodes = boardNodesOf(svgNode);
  assertEqual(nodes.length, entries.length, 'one node per entry (gauntlet: dedupe is a no-op)');
  for (const g of nodes) {
    assertEqual(g.getAttribute('data-mult'), '1', 'each gauntlet entry has multiplicity 1');
    assert(!childByClass(g, 'ezn-board-mult'), 'no spurious multiplicity badge on a gauntlet node');
    assert(!(g.getAttribute('class') || '').includes('ezn-board-raced'), 'a gauntlet node is not marked raced');
  }
});

// ====================================================================
// Lifecycle DAG · NORMALIZED vertical layout. The seed/baseline (full
// board, MORE entries) and a racing challenger (deduped slice, FEWER)
// must render with the SAME per-node row pitch and a structural spine
// centred on the board fan — neither side stretched/compressed, and no
// large empty top band on the seed side.
// ====================================================================

// the y-centre of a board fan = the disc cy's; the spine y-centre = the centre
// of the PARENT structural node (the first non-board ezn-node rect).
function boardCysOf(svgNode) {
  return boardNodesOf(svgNode)
    .map((g) => +childByClass(g, 'ezn-board-disc').getAttribute('cy'))
    .sort((a, b) => a - b);
}
function spineCenterY(svgNode) {
  // the PARENT node carries the text "champion" / "no parent"; grab its rect.
  const nodes = svgNode.querySelectorAll('[class]').filter((n) =>
    n.localName === 'g' && (n.getAttribute('class') || '').split(/\s+/).includes('ezn-node')
    && !(n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-node'));
  const rect = nodes[0].querySelectorAll('[class]').filter((n) => n.localName === 'rect')[0];
  return +rect.getAttribute('y') + +rect.getAttribute('height') / 2;
}
function rowPitchOf(svgNode) {
  const cys = boardCysOf(svgNode);
  return cys.length >= 2 ? cys[1] - cys[0] : null;
}

test('lifecycle DAG (normalized): the seed/baseline (N entries) and a challenger (M entries) share the SAME board-node row pitch', () => {
  // a seed/baseline ran the FULL board (7 entries) — no parent.
  const seedEntries = Array.from({ length: 7 }, (_, i) => ({ entry_id: 'b' + i, drift_loss: 10 + i, pass_fail: i % 2 }));
  // a challenger ran a deduped slice (4 distinct entries).
  const challEntries = Array.from({ length: 4 }, (_, i) => ({ entry_id: 'c' + i, drift_loss: 20 + i, pass_fail: i % 2 }));

  const seed = dag.lifecycleDag({ genId: 'v0', parentId: '', baseline: true, entries: seedEntries });
  const chall = dag.lifecycleDag({ genId: 'v8', parentId: 'v0', decision: 'rejected', entries: challEntries });

  const seedPitch = rowPitchOf(seed);
  const challPitch = rowPitchOf(chall);
  assert(seedPitch != null && challPitch != null, 'both DAGs have a measurable multi-row board fan');
  // the SAME constant pitch on both sides — the bug was the seed fan stretching.
  assert(Math.abs(seedPitch - challPitch) < 0.5,
    `the seed pitch (${seedPitch}) matches the challenger pitch (${challPitch}) — not stretched/compressed`);

  // every adjacent gap on the SEED side is itself the same constant pitch (no
  // divergent vertical spread among the seed's own rows).
  const seedCys = boardCysOf(seed);
  for (let i = 1; i < seedCys.length; i++) {
    assert(Math.abs((seedCys[i] - seedCys[i - 1]) - seedPitch) < 0.5,
      `seed row gap ${i} (${seedCys[i] - seedCys[i - 1]}) equals the constant pitch (${seedPitch})`);
  }
});

test('lifecycle DAG (normalized): the structural spine is centred on the board fan’s TRUE centre for BOTH seed and challenger (no floating spine, no empty top band)', () => {
  const seedEntries = Array.from({ length: 7 }, (_, i) => ({ entry_id: 'b' + i, drift_loss: 10 + i, pass_fail: i % 2 }));
  const challEntries = Array.from({ length: 4 }, (_, i) => ({ entry_id: 'c' + i, drift_loss: 20 + i, pass_fail: i % 2 }));

  for (const [label, spec] of [
    ['seed', { genId: 'v0', parentId: '', baseline: true, entries: seedEntries }],
    ['challenger', { genId: 'v8', parentId: 'v0', decision: 'rejected', entries: challEntries }],
  ]) {
    const svgNode = dag.lifecycleDag(spec);
    const cys = boardCysOf(svgNode);
    const fanCenter = (cys[0] + cys[cys.length - 1]) / 2;
    const spineY = spineCenterY(svgNode);
    assert(Math.abs(spineY - fanCenter) < 1.0,
      `${label}: the spine y-centre (${spineY}) equals the board fan's centre (${fanCenter}) — spine aligned with the fan`);

    // NO large empty top band: the first board row sits a small fixed distance
    // below the column heads (one half-pitch + the header pad), rather than at
    // some proportion of an inflated height.
    const h = +svgNode.getAttribute('height');
    assert(cys[0] < h * 0.5, `${label}: the first board row (${cys[0]}) is in the UPPER half — no big top gap (h=${h})`);
    // and the figure's height closely fits the fan (top pad + fan + bottom pad),
    // so it is never inflated well beyond the fan span.
    const fanSpan = cys[cys.length - 1] - cys[0];
    assert(h - fanSpan < 120, `${label}: height (${h}) fits the fan span (${fanSpan}) closely — figure not inflated`);
  }
});

test('lifecycle DAG (normalized): the seed is NOT laid out with a divergent vertical spread — adding height does not stretch it', () => {
  const entries = Array.from({ length: 6 }, (_, i) => ({ entry_id: 'b' + i, drift_loss: 10 + i, pass_fail: i % 2 }));
  const a = dag.lifecycleDag({ genId: 'v0', parentId: '', baseline: true, entries });
  const b = dag.lifecycleDag({ genId: 'v0', parentId: '', baseline: true, entries, height: 1200 });
  // a passed height cannot stretch the fan: identical pitch regardless.
  assert(Math.abs(rowPitchOf(a) - rowPitchOf(b)) < 0.5, 'a passed height does NOT stretch the seed fan (constant pitch)');
  assertEqual(a.getAttribute('height'), b.getAttribute('height'), 'the derived height is identical regardless of any passed height');
});

test('lifecycle BOARD column: the multiplicity badge style + raced disc marker are themed in the scoped stylesheet', () => {
  const css = readCss();
  assert(/\.ezn-board-mult\s*\{/.test(css), '.ezn-board-mult is styled (themed via CSS vars)');
  assert(/\.ezn-board-mult[^}]*var\(--v2-/.test(css), '.ezn-board-mult uses a theme variable (theme-aware across the 13 themes)');
  assert(/\.ezn-board-raced\s+\.ezn-board-disc\s*\{/.test(css), 'a raced node’s disc carries a distinct marker style');
});

// ====================================================================
// SURVIVAL FUNNEL — the racing epoch's structure-strip hero.
//
// For a RACING epoch the epoch-overview structure strip renders an
// interactive survival FUNNEL: the field flows N → N/2 → … → 1 →
// champion-gate, the flow narrowing at each cut; eliminated competitors peel
// off as ✕ dead-end branches, survivors (↑) ride the thickening flow into the
// gate, which crowns the promoted survivor (♚). It REUSES reconstructRacing()
// (idle) / the LIVE /api/active-tournament (in-flight), degrades to the static
// "field of N" summary when no rungs have raced, and is racing-specific
// (gauntlet keeps its reel; other structures keep their strip).
// ====================================================================

// the survival-funnel SVG primitive renders the field → cuts → survivor → gate.
test('survival funnel: the SVG narrows N→…→1, marks cuts ✕ / survivors ↑, and crowns the gate ♛', () => {
  const rungs = [
    { label: 'Rung 0', competitors: ['v1', 'v2', 'v3', 'v4'], survivors: ['v3', 'v4'], cut: ['v1', 'v2'], board_fraction: 0.25, deltas: { v1: 25, v2: 3.3, v3: -0.16, v4: 0.002 } },
    { label: 'Rung 1', competitors: ['v3', 'v4'], survivors: ['v3'], cut: ['v4'], board_fraction: 0.5, deltas: { v3: 1.0, v4: 1.25 } },
  ];
  const node = svg.survivalFunnel({ rungs, championId: 'v3', gateState: 'crowned', gateDelta: -32.19, onCompetitor() {} });
  assertEqual(node.localName, 'svg', 'the funnel is an SVG');
  assertEqual(node.getAttribute('width'), '100%', 'fit-to-width (width:100%)');
  assert((node.getAttribute('viewBox') || '').startsWith('0 0 '), 'carries a responsive viewBox (no pan/zoom)');

  // the DOT LADDER: a dot per competitor alive entering each rung (rung0 4 +
  // rung1 2). No per-rung band polygon is drawn.
  const dots = node.querySelectorAll('[class]').filter((n) => n.localName === 'circle' && (n.getAttribute('class') || '').split(/\s+/).includes('dn-funnel-dot'));
  assert(dots.length >= 6, `a dot per competitor entering each rung (rung0 4 + rung1 2) — got ${dots.length}`);
  const txt = node.textContent;
  assert(txt.includes('Rung 0') && txt.includes('Rung 1'), 'each stage is labelled by rung');
  assert(txt.includes('25/100 board') || txt.includes('25'), 'a stage encodes its board fraction (successive halving reads)');
  assert(txt.includes('✕'), 'eliminated competitors are marked cut (✕)');
  assert(txt.includes('↑'), 'survivors are marked (↑)');
  // every cut competitor drops a ✕ at its cut rung (the dead-end branch idiom).
  const cutMarks = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-funnel-cut'));
  assert(cutMarks.length >= 3, 'each eliminated competitor drops a ✕ (v1,v2 at rung0 + v4 at rung1)');
  // the terminal champion-gate crowns the survivor.
  assert(txt.includes('champion-gate'), 'a terminal champion-gate stage rendered');
  assert(txt.includes('♛ v3'), 'the crowned survivor is shown (♛ v3)');
  assert(!txt.includes('tbd'), 'a settled gate is not the empty tbd skeleton');
});

// The DOT LADDER: the WINNER lane is emphasised end-to-end (dn-funnel-win)
// and its spline reaches the champion-gate; each cut drops a ✕ at its rung dot
// and grows no forward spline; the ladder draws NO band polygons. The survival
// signal is the continuing, converging spline.
test('survival funnel: the WINNER lane is emphasised end-to-end + reaches the gate; cuts drop a ✕ and grow no forward spline; NO band polygons', () => {
  const rungs = [
    { label: 'Rung 0', competitors: ['v1', 'v2', 'v3', 'v4'], survivors: ['v3', 'v4'], cut: ['v1', 'v2'], board_fraction: 0.25, deltas: { v1: 25, v2: 3.3, v3: -0.16, v4: 0.002 } },
    { label: 'Rung 1', competitors: ['v3', 'v4'], survivors: ['v3'], cut: ['v4'], board_fraction: 0.5, deltas: { v3: 1.0, v4: 1.25 } },
  ];
  const node = svg.survivalFunnel({ rungs, championId: 'v3', gateState: 'crowned', gateDelta: -32.19, onCompetitor() {} });

  // the champion-gate seat x, mirrored from the renderer (svg.js survivalFunnel).
  const stageW = 150, stageGap = 20;
  const gx = rungs.length * stageW + Math.max(0, rungs.length - 1) * stageGap + stageGap + 2;
  const top = 56, laneH = 132, midY = top + laneH / 2;

  // the winner v3's lane carries the emphasis class end-to-end.
  const lanes = node.querySelectorAll('[class]')
    .filter((n) => n.localName === 'g' && (n.getAttribute('class') || '').split(/\s+/).includes('dn-funnel-runner'));
  const win = lanes.find((g) => (g.getAttribute('class') || '').split(/\s+/).includes('dn-funnel-win'));
  assert(win, 'the crowned survivor v3 lane carries the dn-funnel-win emphasis');
  assert((win.textContent || '').trim().startsWith('v3'), 'the emphasised lane is v3');

  // the winner's spline reaches the champion-gate seat (its last point x ≈ gx).
  const winSplines = win.querySelectorAll('[class]')
    .filter((n) => n.localName === 'path' && (n.getAttribute('class') || '').split(/\s+/).includes('dn-funnel-spline'));
  assert(winSplines.length >= 1, 'the winner lane draws converging splines');
  const reachesGate = winSplines.some((p) => {
    const m = (p.getAttribute('d') || '').match(/([-\d.]+),([-\d.]+)\s*$/);
    return m && Math.abs(parseFloat(m[1]) - gx) < 1.0 && Math.abs(parseFloat(m[2]) - midY) < 1.0;
  });
  assert(reachesGate, `the winner's spline reaches the champion-gate seat at (x≈${gx}, y≈${midY})`);

  // each cut drops exactly one ✕ (v1,v2 @ rung0 + v4 @ rung1) and NO band polygon
  // is drawn (the redesign removed the trapezoid bands).
  const cutMarks = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-funnel-cut'));
  assertEqual(cutMarks.length, 3, 'one ✕ per cut (v1,v2 @ rung0 + v4 @ rung1)');
  const bands = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-funnel-band'));
  assertEqual(bands.length, 0, 'the dot ladder draws NO band polygons');

  // the dots converge toward the vertical centre: the entering rung spreads its
  // dots off midY rather than stacking them.
  const dots = node.querySelectorAll('[class]').filter((n) => n.localName === 'circle' && (n.getAttribute('class') || '').split(/\s+/).includes('dn-funnel-dot'));
  const spread = Math.max(...dots.map((c) => Math.abs(parseFloat(c.getAttribute('cy')) - midY)));
  assert(spread > 6, `the entering rung spreads its dots vertically off the centre line (max |Δy|=${spread.toFixed(1)})`);
});

// (a) the racing epoch strip renders the funnel from the per-challenger records.
test('survival funnel: the racing epoch strip renders the funnel (stages narrow N→…→1, cuts ✕, gate crowns v3)', async () => {
  freshState();
  const F = racingLadderFixture();
  installFixtureMap(F);
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: RC_EPOCH });

  // racing-specific: the round timeline (no old gauntlet reel) embeds the funnel.
  assertEqual(allByClass(host, 'tr-reel').length, 0, 'NO gauntlet reel for a racing epoch');
  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline renders for the racing epoch');
  const funnel = svgsByClass(host, 'dn-funnel')[0];
  assert(funnel, 'the survival funnel rendered as the racing episode figure');
  assert(funnel.getAttribute('width') === '100%' && (funnel.getAttribute('viewBox') || '').startsWith('0 0 '), 'the funnel is fit-to-width + responsive');
  assert(!hasScrollWrapperAncestor(funnel, host), 'no horizontal-scroll wrapper around the funnel (no pan/zoom)');

  const txt = funnel.textContent;
  assert(txt.includes('Rung 0') && txt.includes('Rung 1'), 'both reconstructed rungs render as stages');
  for (const id of ['v1', 'v2', 'v3', 'v4']) assert(txt.includes(id), 'rung0 names the full field — ' + id);
  assert(txt.includes('✕'), 'eliminated competitors marked cut (✕) at their rung');
  assert(txt.includes('↑'), 'survivors marked (↑)');
  assert(txt.includes('♛ v3'), 'the champion-gate crowns the survivor v3 (♛)');
  // the episode drills into the round's full Match-ups (the ladder lives there).
  assert(host.textContent.includes('open round'), 'the episode keeps the "open round →" drill affordance');
  assertEqual(svgsByClass(host, 'dn-funnel').length, 1, 'the epoch hero is a SINGLE survival-funnel figure (the unified racing visual)');
});

// (b) a competitor is clickable → its candidate.
test('survival funnel: a competitor is clickable → its candidate page', async () => {
  freshState();
  const F = racingLadderFixture();
  installFixtureMap(F);
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await epoch.render(host, ctx, { epochId: RC_EPOCH });
  const runner = allByClass(host, 'dn-funnel-runner')[0];
  assert(runner, 'a clickable competitor exists on the funnel');
  runner.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'candidate' && navTo.p.epochId === RC_EPOCH, 'clicking a funnel competitor routes to its candidate page');
  assert(/^v\d+$/.test(navTo.p.gen), 'the navigation carries the competitor generation id');
});

// (c) the live path shows a pending stage + "deciding…".
test('survival funnel: a LIVE racing run shows the in-progress funnel (pending stage neutral, gate "deciding…")', async () => {
  freshState();
  const F = structureFixture('racing_round_live');
  installFixtureMap(F);
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1', generation_id: 'v1', epoch_id: EPOCH_ID });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = F['/api/active-tournament'];

  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  const funnel = svgsByClass(host, 'dn-funnel')[0];
  assert(funnel, 'the LIVE racing funnel rendered from /api/active-tournament');
  assert(allByClass(host, 'dn-roundtl')[0], 'the live funnel is embedded in the round timeline');
  // the not-yet-decided rung stays neutral (a pending band, nobody struck).
  assert(allByClass(funnel, 'dn-funnel-pending').length >= 1, 'the pending (still-racing) stage renders neutral (no premature cut)');
  const struck = allByClass(host, 'dn-out');
  for (const n of struck) assert((n.textContent || '').indexOf('v1') < 0, 'the leader v1 is never struck (cut) mid-run');
  assert(funnel.textContent.includes('deciding'), 'the live champion-gate reads "deciding…" — no premature crown');
  assert(!funnel.textContent.includes('♚'), 'no champion is crowned ♚ while the race is live');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

// (d) with no rung records, degrade to the static summary (no empty funnel).
test('survival funnel: with NO rung records the strip degrades to the static "field of N" summary (no empty funnel)', async () => {
  freshState();
  const F = recordedRoutes('racing_no_records');
  installFixtureMap(F);
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: RC_EPOCH });

  assertEqual(svgsByClass(host, 'dn-funnel').length, 0, 'NO empty funnel when there are no rung records');
  // the timeline degrades in place: a single round 0 episode (v0 → v1), no figure.
  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline degrades in place when there are no rung records');
  assert(host.textContent.includes('Racing'), 'the timeline still names the racing structure');
  assert(allByClass(host, 'dn-roundtl-ep').length >= 1, 'a single-round episode stands in for the empty race');
  assert(host.textContent.includes('open round'), 'the episode keeps the "open round →" drill affordance');
});

// (e) a gauntlet epoch's timeline has no embedded funnel (the funnel is racing-specific).
test('survival funnel: a GAUNTLET epoch renders the round timeline with NO embedded funnel', async () => {
  freshState(); installFetch();  // the default gauntlet fixture (no tournament block)
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(allByClass(host, 'dn-roundtl')[0], 'the gauntlet epoch renders the round timeline');
  assertEqual(svgsByClass(host, 'dn-funnel').length, 0, 'NO survival funnel for a gauntlet epoch (racing-specific)');
});

// (f) the funnel marks are themed via CSS tokens (legible across all 13 themes).
test('survival funnel: marks are token-themed in the scoped stylesheet (legible across the 13 themes)', () => {
  const css = readCss();
  assert(/\.dn-funnel-dot\s*\{/.test(css), '.dn-funnel-dot is styled');
  assert(/\.dn-funnel-dot[^}]*var\(--v2-/.test(css), 'the ladder dot reads a --v2-* token (theme-aware)');
  assert(/\.dn-funnel-spline[^}]*var\(--v2-/.test(css), 'the converging spline reads a --v2-* token (theme-aware)');
  assert(/\.dn-funnel-name\.dn-good[^}]*var\(--v2-good\)/.test(css), 'survivors use the --v2-good token');
  assert(/\.dn-funnel-name\.dn-bad[^}]*var\(--v2-bad\)/.test(css), 'cuts use the --v2-bad token');
  assert(/\.dn-funnel-gatebox\.dn-good[^}]*var\(--v2-good\)/.test(css), 'the crowned gate uses the --v2-good token');
  assert(/\.dn-funnel-pending[^}]*var\(--v2-rule/.test(css), 'a pending (live) stage uses a neutral rule token');
});

// (f2) the swiss-ladder + elim-FLOW marks are token-themed (all 16 themes)
// with NO hardcoded hex — and the live transitions are reduced-motion gated.
test('swiss ladder + elim flow: token-themed in the scoped stylesheet, no hardcoded hex, reduced-motion gated', () => {
  const css = readCss();
  // swiss ladder
  assert(/\.dn-swissladder-head\s*\{/.test(css), '.dn-swissladder-head is styled');
  assert(/\.dn-swissladder-standlab\.dn-good[^}]*var\(--v2-good\)/.test(css), 'the swiss leader uses the --v2-good token');
  assert(/\.dn-swissladder-gatebox\.dn-good[^}]*var\(--v2-good\)/.test(css), 'the crowned swiss gate uses the --v2-good token');
  assert(/\.dn-swissladder-bar[^}]*var\(--v2-accent\)/.test(css), 'the live swiss progress bar uses the accent token');
  // the radial bracket (the one elimination figure; the seat/box tree and the
  // lane flow are gone).
  assert(!/\.dn-elimbracket/.test(css), 'the retired seat/box bracket CSS is gone');
  assert(!/\.dn-elimflow/.test(css), 'the deleted lane-flow CSS is gone');
  assert(/\.dn-elimradial-seg\.dn-good[^}]*var\(--v2-good\)/.test(css), 'a surviving radial segment uses the --v2-good token');
  assert(/\.dn-elimradial-node\.dn-bad[^}]*var\(--v2-bad\)/.test(css), 'an eliminated radial node uses the --v2-bad token');
  assert(/\.dn-elimradial-seat\.dn-good[^}]*var\(--v2-good\)/.test(css), 'a crowned radial seat uses the --v2-good token');
  // NO hardcoded hex in the swiss/elim rules (token-only).
  const swissSlice = css.slice(css.indexOf('.dn-swissladder-head'), css.indexOf('.dn-swissladder-bench') + 120);
  const radialSlice = css.slice(css.indexOf('.dn-elimradial-ring'), css.indexOf('.dn-elimradial-seg.dn-elimradial-pending.dn-proj') + 80);
  assert(!/#[0-9a-fA-F]{3,6}\b/.test(swissSlice + radialSlice), 'the swiss/elim mark rules carry NO hardcoded hex (theme-token only)');
  // the reduced-motion gate covers the live transitions (the radial declares none).
  const rm = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
  assert(/\.dn-swissladder/.test(rm), 'the swiss live transitions are suppressed under reduced motion');
  assert(!/\.dn-elimradial[^{]*transition/.test(css), 'the radial bracket declares no transition to gate');
});

// ====================================================================
// LIFECYCLE relates board runs to rungs/matchups:
//   * a deduped board node is EXPANDABLE — it reveals its N per-run losses
//     as an inline stack plus a sparkline, so no value is dropped;
//   * when the per-entry records carry `match_id`/`rung`, each run is LABELLED
//     by its rung/matchup; when those fields are absent, no rung labels are
//     fabricated;
//   * a CANDIDATE RUNG-PROGRESSION strip (rung0→rung1→final, each Δ + won/cut)
//     relates the candidate to the rounds even without per-run tags;
//   * a gauntlet candidate (one run per entry) renders unchanged.
// ====================================================================

function runRowsOf(boardNode) {
  return boardNode.querySelectorAll('[class]').filter((n) =>
    n.localName === 'g' && (n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-run'));
}

test('lifecycle BOARD node: a re-raced entry is EXPANDABLE and reveals each run’s loss (no longer lossy on the values)', () => {
  // q3_metrics_outline raced 3× (rung0/rung1/final) with losses 4.0 / 64.0 / 63.5.
  const entries = [
    { entry_id: 'q3_metrics_outline', run_id: 'r0', drift_loss: 4.0, pass_fail: true, wall_clock_budget_exceeded: false },
    { entry_id: 'q3_metrics_outline', run_id: 'r1', drift_loss: 64.0, pass_fail: false, wall_clock_budget_exceeded: false },
    { entry_id: 'q3_metrics_outline', run_id: 'r2', drift_loss: 63.5, pass_fail: false, wall_clock_budget_exceeded: false },
    { entry_id: 'waffles_single', run_id: 'g1', drift_loss: 60.5, pass_fail: false, wall_clock_budget_exceeded: false },
  ];
  let navTo = null;
  const svgNode = dag.lifecycleDag({ genId: 'v3', parentId: 'v0', entries, decision: 'rejected', height: 360,
    onRun: (eid, runId) => { navTo = { eid, runId }; } });
  const nodes = boardNodesOf(svgNode);
  const byKey = {}; for (const n of nodes) byKey[n.getAttribute('data-key')] = n;

  // the raced node is marked expandable + carries its per-run stack.
  const q3 = byKey['q3_metrics_outline'];
  assert((q3.getAttribute('class') || '').includes('ezn-board-expandable'), 'a re-raced node is marked expandable');
  const stack = q3.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-board-runs'))[0];
  assert(stack, 'the expandable node carries a per-run expansion panel');
  const rows = runRowsOf(q3);
  assertEqual(rows.length, 3, 'the panel reveals ONE row per run (3 runs)');
  // every per-run loss value is shown (4.0 / 64.0 / 63.5); none is dropped.
  const losses = rows.map((r) => r.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-board-run-loss'))[0].textContent);
  assertDeep(losses, [svg.fmt(4.0, 1), svg.fmt(64.0, 1), svg.fmt(63.5, 1)], 'each run’s loss is revealed in order (rung0→final)');
  // a sparkline of the per-run losses renders too.
  assert(q3.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-board-spark')).length === 1, 'an inline sparkline of the run losses renders');

  // clicking a run row drills into that run/transcript.
  rows[1].dispatchEvent({ type: 'click', stopPropagation() {} });
  assert(navTo && navTo.eid === 'q3_metrics_outline', 'clicking a per-run row drills into that run (onRun fired)');

  // a once-run (gauntlet-style) entry in the same set carries NO expansion.
  assert(!(byKey['waffles_single'].getAttribute('class') || '').includes('ezn-board-expandable'), 'a once-run entry is not expandable');
  assertEqual(runRowsOf(byKey['waffles_single']).length, 0, 'a once-run entry has no per-run rows');
});

test('lifecycle BOARD node: per-run rows are LABELLED by rung when records carry match_id/rung', () => {
  const entries = [
    { entry_id: 'q3_metrics_outline', run_id: 'r0', drift_loss: 4.0, pass_fail: true, match_id: 'rung0_m2', rung: 'rung 0' },
    { entry_id: 'q3_metrics_outline', run_id: 'r1', drift_loss: 64.0, pass_fail: false, match_id: 'rung1_m0', rung: 'rung 1' },
    { entry_id: 'q3_metrics_outline', run_id: 'r2', drift_loss: 63.5, pass_fail: false, match_id: 'racing-final', rung: 'final' },
  ];
  const svgNode = dag.lifecycleDag({ genId: 'v3', parentId: 'v0', entries, decision: 'rejected', height: 360 });
  const q3 = boardNodesOf(svgNode)[0];
  assertEqual(q3.getAttribute('data-tagged'), '1', 'the node is flagged as carrying rung-tagged runs');
  const rungs = q3.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-board-run-rung')).map((n) => n.textContent);
  assertDeep(rungs, ['rung 0', 'rung 1', 'final'], 'each run is labelled by its rung/matchup (rung 0 / rung 1 / final)');
});

test('lifecycle BOARD node: with NO rung tags (legacy data) the per-run losses still show but NO rung labels are fabricated', () => {
  // the untagged shape: repeated entries carrying NO match_id/rung fields.
  const entries = [
    { entry_id: 'q3_metrics_outline', run_id: 'r0', drift_loss: 4.0, pass_fail: true },
    { entry_id: 'q3_metrics_outline', run_id: 'r1', drift_loss: 64.0, pass_fail: false },
    { entry_id: 'q3_metrics_outline', run_id: 'r2', drift_loss: 63.5, pass_fail: false },
  ];
  const svgNode = dag.lifecycleDag({ genId: 'v3', parentId: 'v0', entries, decision: 'rejected', height: 360 });
  const q3 = boardNodesOf(svgNode)[0];
  assertEqual(q3.getAttribute('data-tagged'), '0', 'legacy runs are NOT flagged as rung-tagged');
  // the per-run losses still render (not lossy)…
  const rows = runRowsOf(q3);
  assertEqual(rows.length, 3, 'all three per-run losses still render on legacy data');
  // …but NO rung labels are fabricated.
  assertEqual(q3.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-board-run-rung')).length, 0,
    'no rung labels are fabricated when the records carry no match_id/rung');
});

// ====================================================================
// Lifecycle DAG — SELF-EXPLANATORY per-board loss, Σ aggregation, and the
// gate Δ-vs-champion decision. The motivating confusion: a candidate whose
// Σ "looks smaller" still gets rejected because the gate compares
// challenger-vs-champion (Δ, positive = worse) on the SAME boards and applies
// a 3-rule test. We surface the champion comparison + the deciding rule.
// ====================================================================

test('lifecycle BOARD circle: exposes the champion comparison (champion loss + signed Δ) for its board', () => {
  const entries = [
    { entry_id: 'waffles_single', drift_loss: 60.5, pass_fail: false },
    { entry_id: 'picky_stakeholder_emulated', drift_loss: 642.5, pass_fail: false },
  ];
  // the champion scored LOWER on both boards → the challenger's Δ is positive
  // (worse) on each, even though one of its raw losses (60.5) is identical. The
  // per-entry comparison is the SERVER's join (`/api/matchup-grid`), handed to
  // the figure as `compare`; nothing here is derived from a second fetch.
  const compare = {
    waffles_single: { champDrift: 60.5 },
    picky_stakeholder_emulated: { champDrift: 105.5 },
  };
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: 360,
    championId: 'v0', compare });
  const nodes = boardNodesOf(svgNode);
  const byKey = {}; for (const n of nodes) byKey[n.getAttribute('data-key')] = n;

  // each circle carries a candidate-vs-champion sublabel with the champion loss
  // and the signed Δ (challenger − champion).
  const picky = childByClass(byKey['picky_stakeholder_emulated'], 'ezn-board-cmp');
  assert(picky, 'a board circle carries a champion-comparison sublabel');
  assertEqual(picky.getAttribute('data-champ-loss'), svg.fmt(105.5, 1), 'the sublabel exposes the champion’s loss on this board');
  assertEqual(picky.getAttribute('data-delta'), svg.fmtSigned(642.5 - 105.5, 1), 'the sublabel exposes the signed Δ (challenger − champion)');
  assert((picky.getAttribute('class') || '').includes('ezn-cmp-worse'), 'a positive Δ (worse than champion) is coloured with the worse token');
  assert(/champ/.test(picky.textContent) && /Δ/.test(picky.textContent), 'the sublabel reads "champ N · Δ ±X"');

  // the detail now lives in the styled HOVERCARD (not a native <title>): the
  // board node is hovercard-wired and surfaces the comparison + "lower is
  // better" cue on hover.
  const boardNode = byKey['waffles_single'];
  assert(hovercard.hasHovercard(boardNode), 'the board circle is wired with the hovercard (not a native <title>)');
  assert(!hasNativeTitle(boardNode), 'the board circle carries NO native <title> tooltip');
  const tipText = hovercardTextOf(boardNode);
  assert(/lower is better/.test(tipText), 'the hovercard states drift loss is lower-is-better');
  assert(/champion v0/.test(tipText) && /Δ/.test(tipText), 'the hovercard names the champion + the Δ');

  // an EVEN board (identical loss) is neither worse nor better.
  const even = dag.lifecycleDag({ genId: 'v1', parentId: 'v0',
    entries: [{ entry_id: 'b', drift_loss: 60.5, pass_fail: false }], decision: 'rejected',
    championId: 'v0', compare: { b: { champDrift: 60.5 } } });
  const evCmp = childByClass(boardNodesOf(even)[0], 'ezn-board-cmp');
  assert((evCmp.getAttribute('class') || '').includes('ezn-cmp-even'), 'an equal-loss board is coloured even (neither worse nor better)');
});

test('lifecycle Σ node: exposes candidate-Σ vs champion-Σ and the Δ between them (what the gate sees)', () => {
  const entries = [
    { entry_id: 'waffles_single', drift_loss: 60.5, pass_fail: false },
    { entry_id: 'picky_stakeholder_emulated', drift_loss: 642.5, pass_fail: false },
  ];
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: 360,
    championId: 'v0', candidateSigma: 703.0, championSigma: 166.0, deltaSigma: 537.0 });
  // the Σ node carries the candidate Σ, the champion Σ, and the Δ.
  const agg = svgNode.querySelectorAll('[class]').filter((n) =>
    n.localName === 'g' && n.getAttribute('data-cand-sigma') != null)[0];
  assert(agg, 'the Σ node renders');
  assertEqual(agg.getAttribute('data-cand-sigma'), svg.fmt(703.0, 1), 'the Σ node exposes the candidate Σ over the slice');
  assertEqual(agg.getAttribute('data-champ-sigma'), svg.fmt(166.0, 1), 'the Σ node exposes the champion Σ over the same slice');
  assertEqual(agg.getAttribute('data-delta-sigma'), svg.fmtSigned(537.0, 1), 'the Σ node exposes the Δ (candidate − champion) the gate acts on');
  assert((agg.getAttribute('class') || '').includes('ezn-cmp-worse'), 'a positive Σ Δ tints the node as worse');
  // the Σ explanation lives in the styled hovercard rather than a native <title>.
  assert(hovercard.hasHovercard(agg), 'the Σ node is wired with the hovercard');
  assert(!hasNativeTitle(agg), 'the Σ node carries NO native <title>');
  const sigmaTip = hovercardTextOf(agg);
  assert(/summed over this rung’s board slice/.test(sigmaTip), 'the Σ hovercard explains the aggregation over the slice');
  assert(/SAME boards/.test(sigmaTip), 'the Σ hovercard links Σ→GATE: the gate compares these scalars on the same boards');
});

test('lifecycle GATE node: names the deciding rule + the Δ — a POSITIVE Δ rejection explains "worse than champion"', () => {
  const entries = [{ entry_id: 'b', drift_loss: 100, pass_fail: false }];
  const gateExplain = { decision: 'rejected', decidingRule: 'scalar_margin', decidingLabel: 'Scalar margin',
    deltaScalar: 75.71, margin: -0.01, regressed: null, reason: 'challenger regressed: loss rose by 75.71' };
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: 360,
    deltaScalar: 75.71, gateExplain });
  const gate = svgNode.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('ezn-gate-node'))[0];
  assert(gate, 'the GATE node carries the gate-node marker');
  assertEqual(gate.getAttribute('data-deciding-rule'), 'scalar_margin', 'the GATE node names the deciding rule');
  assertEqual(gate.getAttribute('data-delta-scalar'), svg.fmtSigned(75.71, 2), 'the GATE node carries the decisive Δ scalar');
  assertEqual(gate.getAttribute('data-margin'), svg.fmt(-0.01, 2), 'the GATE node carries the promote margin');
  // the GATE explanation lives in the styled hovercard rather than a native <title>.
  assert(hovercard.hasHovercard(gate), 'the GATE node is wired with the hovercard');
  assert(!hasNativeTitle(gate), 'the GATE node carries NO native <title> tooltip');
  const gateTip = hovercardTextOf(gate);
  assert(gateTip, 'the GATE node exposes an explanation via the hovercard');
  assert(/3-rule/.test(gateTip), 'the hovercard frames the gate as a 3-rule test');
  assert(/SCALAR-MARGIN rule/i.test(gateTip), 'the hovercard names the scalar-margin rule as the decider');
  assert(/worse than champion/.test(gateTip), 'a positive-Δ rejection explains it is WORSE than the champion');
  assert(/\+75\.7/.test(gateTip), 'the hovercard shows the decisive +Δ');
});

test('lifecycle GATE node: a MONOTONICITY rejection explains the regressed predicate even when the scalar is BETTER', () => {
  const entries = [{ entry_id: 'b', drift_loss: 10, pass_fail: false }];
  // scalar is BETTER (Δ negative) yet the candidate is rejected because it
  // regressed a predicate that had been passing (pass-rate monotonicity). This
  // is the "smaller Σ but rejected" case made legible.
  const gateExplain = { decision: 'rejected', decidingRule: 'pass_rate_monotonicity', decidingLabel: 'Pass-rate monotonicity',
    deltaScalar: -5.0, margin: null, regressed: 'no_fabricated_numbers', reason: 'regressed a passing predicate' };
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: 360,
    deltaScalar: -5.0, gateExplain });
  const gate = svgNode.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('ezn-gate-node'))[0];
  assertEqual(gate.getAttribute('data-deciding-rule'), 'pass_rate_monotonicity', 'the deciding rule is the monotonicity rule');
  assertEqual(gate.getAttribute('data-regressed'), 'no_fabricated_numbers', 'the GATE node carries the regressed predicate');
  const monoTip = hovercardTextOf(gate);
  assert(!hasNativeTitle(gate), 'the GATE node carries NO native <title>');
  assert(/Scalar may be better, BUT/.test(monoTip), 'the hovercard says the scalar is better BUT it still failed a rule');
  assert(/no_fabricated_numbers/.test(monoTip), 'the hovercard names the regressed predicate');
  assert(/rule 2/.test(monoTip), 'the hovercard identifies it as the pass-rate-monotonicity rule (rule 2)');
});

test('lifecycle DAG: de-crowded to ONE concise key line + a "?" info hovercard (the verbose how-to is gone from the figure), omitted for a baseline', () => {
  const entries = [{ entry_id: 'b', drift_loss: 10, pass_fail: false }];
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: 360, championId: 'v0' });

  // exactly ONE always-on key line, short and uncrowded: the detail lives in
  // this single line plus the "?" hovercard.
  const keys = svgNode.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-dag-key'));
  assertEqual(keys.length, 1, 'the DAG carries exactly ONE concise key line');
  const t = keys[0].textContent;
  assert(/Δ vs champion/.test(t) && /\+ = worse/.test(t), 'the key line states "Δ vs champion · + = worse"');
  assert(/lower loss better/.test(t) && /hover nodes for detail/.test(t), 'the key line states lower-loss-better + the hover-for-detail cue');
  // no verbose two-block prose crowds the figure as a key.
  assert(!/Σ = their sum on the slice/.test(t), 'the verbose "Σ = their sum on the slice" prose is no longer in the always-on key');
  assert(!/no pass-rate\/namespace regression/.test(t), 'the verbose pass-rate/namespace prose is no longer in the always-on key');

  // the full how-to walkthrough lives in the focusable "?" info affordance,
  // surfaced via the hovercard: detail on demand rather than always-on prose.
  const info = svgNode.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('ezn-dag-info'))[0];
  assert(info, 'the DAG carries a "?" info affordance');
  assert(hovercard.hasHovercard(info), 'the "?" affordance is wired with the hovercard');
  assertEqual(info.getAttribute('tabindex'), '0', 'the "?" affordance is keyboard-focusable');
  const howto = hovercardTextOf(info);
  assert(/parent → patch → board/.test(howto), 'the hovercard carries the parent→patch→board walkthrough');
  assert(/3-rule test/.test(howto), 'the hovercard carries the 3-rule gate detail');
  assert(/per-run values/.test(howto), 'the hovercard carries the hover/click affordance detail');

  // a baseline (seed) has no gate, so no key + no info affordance.
  const seed = dag.lifecycleDag({ genId: 'v0', parentId: null, baseline: true, entries, decision: 'baseline', height: 360 });
  assert(!seed.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-dag-key'))[0],
    'the baseline DAG omits the gate key (it has no gate)');
});

// the key line's text baseline y, and the LOWEST node-box bottom edge across
// the whole figure (rect boxes: y + height; board circles: cy + r, plus the
// `champ N · Δ` cmp sublabel below a circle when present).
function keyLineYOf(svgNode) {
  const k = svgNode.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('ezn-dag-key'))[0];
  return k ? +k.getAttribute('y') : null;
}
function lowestNodeBottomOf(svgNode) {
  let bottom = -Infinity;
  // every rect node box (PARENT/PATCH/Σ/GATE/TERMINAL and the "no board" box).
  for (const r of svgNode.querySelectorAll('[class]').filter((n) =>
    n.localName === 'rect' && (n.getAttribute('class') || '').split(/\s+/).includes('ezn-node-box'))) {
    bottom = Math.max(bottom, +r.getAttribute('y') + +r.getAttribute('height'));
  }
  // every board circle (+ its radius), and any cmp sublabel below it.
  for (const c of svgNode.querySelectorAll('[class]').filter((n) =>
    n.localName === 'circle' && (n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-disc'))) {
    bottom = Math.max(bottom, +c.getAttribute('cy') + +c.getAttribute('r'));
  }
  for (const t of svgNode.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-cmp'))) {
    bottom = Math.max(bottom, +t.getAttribute('y'));
  }
  return bottom;
}

test('lifecycle DAG: the key line clears the node row at a SINGLE board node (no overlap)', () => {
  // the not-yet-run / "no board entries scored" state — a single neutral box.
  // The fan span is 0 here, the worst case for a flat key pad, where a key line
  // can render right through the node boxes. It must sit strictly below the
  // lowest node box's bottom edge, with a readable margin.
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries: [], decision: 'rejected', championId: 'v0' });
  const ky = keyLineYOf(svgNode);
  assert(ky != null, 'a single-node DAG still carries the key line');
  const lowest = lowestNodeBottomOf(svgNode);
  assert(ky > lowest + 6, `the key line y (${ky}) is strictly below the lowest node box bottom (${lowest}) with a margin`);
  // and it stays within the figure's derived viewBox height.
  const h = +svgNode.getAttribute('height');
  assert(ky <= h, `the key line y (${ky}) is within the derived viewBox height (${h})`);
});

test('lifecycle DAG: the key line clears the node row with MANY board nodes (no overlap at the bottom-most node)', () => {
  const entries = Array.from({ length: 7 }, (_, i) => ({ entry_id: 'b' + i, drift_loss: 10 + i, pass_fail: i % 2 }));
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected',
    championId: 'v0', compare: { b0: { champDrift: 5 }, b6: { champDrift: 9 } } });
  const ky = keyLineYOf(svgNode);
  assert(ky != null, 'a many-node DAG carries the key line');
  const lowest = lowestNodeBottomOf(svgNode);
  assert(ky > lowest + 6, `the key line y (${ky}) is strictly below the bottom-most node (${lowest}) — including its cmp sublabel`);
  const h = +svgNode.getAttribute('height');
  assert(ky <= h, `the key line y (${ky}) is within the derived viewBox height (${h})`);
});

// ====================================================================
// HOVERCARD — the styled, theme-aware replacement for native <title>.
// ====================================================================

test('hovercard: the heatmap cell uses the hovercard (NOT a native <title>), and surfaces "row × col: value"', () => {
  const node = svg.heatmap({
    rows: [{ id: 'r1', label: 'board one' }],
    cols: [{ id: 'c1', label: 'gen one' }],
    value: (r, c) => (r === 'r1' && c === 'c1' ? 12.5 : null),
  });
  const cell = node.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('dn-hm-cell'))[0];
  assert(cell, 'a heatmap cell rendered');
  assert(hovercard.hasHovercard(cell), 'the heatmap cell is wired with the hovercard');
  assert(!hasNativeTitle(cell), 'the heatmap cell carries NO native <title>');
  const tip = hovercardTextOf(cell);
  assert(/board one × gen one/.test(tip), 'the hovercard reads "row × col"');
  assert(/12\.5/.test(tip), 'the hovercard carries the cell value');
});

test('hovercard: the per-board dot-plot dot + reference rule use the hovercard, not a native <title>', () => {
  const node = svg.valueDotPlot({
    items: [{ label: 'waffles', value: 60.5, id: 'waffles' }],
    reference: { value: 50, label: 'champion v0' },
  });
  // the per-board dot.
  const dot = node.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('dn-dot'))[0];
  assert(dot, 'a dot-plot dot rendered');
  assert(hovercard.hasHovercard(dot) && !hasNativeTitle(dot), 'the dot uses the hovercard, not a native <title>');
  assert(/waffles/.test(hovercardTextOf(dot)), 'the dot hovercard names the board');
  // the reference rule.
  const ref = node.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').includes('dn-ref-rule'))[0];
  assert(ref, 'a reference rule rendered');
  assert(hovercard.hasHovercard(ref) && !hasNativeTitle(ref), 'the reference rule uses the hovercard, not a native <title>');
  assert(/champion v0/.test(hovercardTextOf(ref)), 'the reference-rule hovercard names the champion reference');
});

test('hovercard: NO native <title> remains on the interactive marks of the lifecycle DAG, heatmap, or dot-plot', () => {
  const dagSvg = dag.lifecycleDag({ genId: 'v1', parentId: 'v0',
    entries: [{ entry_id: 'b', drift_loss: 10, pass_fail: false }], decision: 'rejected',
    championId: 'v0', compare: { b: { champDrift: 5 } }, candidateSigma: 10, championSigma: 5, deltaSigma: 5 });
  const hm = svg.heatmap({ rows: [{ id: 'r', label: 'r' }], cols: [{ id: 'c', label: 'c' }], value: () => 1 });
  const dp = svg.valueDotPlot({ items: [{ label: 'x', value: 1 }], reference: { value: 2, label: 'ref' } });
  for (const [name, root] of [['DAG', dagSvg], ['heatmap', hm], ['dot-plot', dp]]) {
    const titles = root.querySelectorAll('[class]').filter((n) => n.localName === 'title')
      .concat(root.childNodes ? [] : []);
    // walk for any <title> descendant.
    const anyTitle = (function find(n) {
      if (!n || !n.childNodes) return false;
      for (const c of n.childNodes) { if (c.localName === 'title') return true; if (find(c)) return true; }
      return false;
    })(root);
    assert(!anyTitle, `the ${name} has NO native <title> left (replaced by the hovercard)`);
  }
});

test('hovercard: show on mouseenter/focus, hide on mouseleave/blur/Escape — and the card is theme-token styled', () => {
  // build any wired mark.
  const node = svg.heatmap({ rows: [{ id: 'r', label: 'row' }], cols: [{ id: 'c', label: 'col' }], value: () => 7 })
    .querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-hm-cell'))[0];
  hovercard.hide();
  assert(!hovercard.isShown(), 'the hovercard starts hidden');
  // mouseenter shows it.
  node.dispatchEvent({ type: 'mouseenter', target: node });
  assert(hovercard.isShown(), 'mouseenter shows the hovercard');
  assert(/row × col: 7/.test(hovercard.cardText()), 'the shown card carries the mark detail');
  // mouseleave hides it.
  node.dispatchEvent({ type: 'mouseleave', target: node });
  assert(!hovercard.isShown(), 'mouseleave hides the hovercard');
  // focus shows; blur hides (keyboard path).
  node.dispatchEvent({ type: 'focus', target: node });
  assert(hovercard.isShown(), 'focus shows the hovercard (keyboard-accessible)');
  node.dispatchEvent({ type: 'blur', target: node });
  assert(!hovercard.isShown(), 'blur hides the hovercard');

  // the card is THEME-TOKEN styled (no hardcoded hex) — assert the CSS contract.
  const css = readCss();
  assert(/\.dn-hovercard\b/.test(css), 'the stylesheet defines the .dn-hovercard');
  const block = css.slice(css.indexOf('.dn-hovercard {'), css.indexOf('.dn-hovercard-line'));
  assert(/var\(--v2-panel\)/.test(block), 'the hovercard background uses the --v2-panel token');
  assert(/var\(--v2-ink\)/.test(block), 'the hovercard text uses the --v2-ink token');
  assert(/var\(--v2-rule\)/.test(block), 'the hovercard border uses the --v2-rule token');
  assert(/var\(--v2-mono\)/.test(block), 'the hovercard uses the mono font token');
  assert(!/#[0-9a-fA-F]{3,6}\b/.test(block), 'the hovercard block carries NO hardcoded hex colour');
  assert(/prefers-reduced-motion/.test(css), 'the hovercard honours prefers-reduced-motion');
});

test('hovercard: the target is keyboard-accessible (focusable + aria-describedby links the card)', () => {
  const cell = svg.heatmap({ rows: [{ id: 'r', label: 'row' }], cols: [{ id: 'c', label: 'col' }], value: () => 1 })
    .querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-hm-cell'))[0];
  assertEqual(cell.getAttribute('tabindex'), '0', 'a wired mark with no tabindex is made focusable');
  assert((cell.getAttribute('aria-describedby') || '').length > 0, 'the mark links the hovercard via aria-describedby');
});

test('lifecycle DAG (integration): the candidate view feeds the champion comparison + gate-rule explanation into the DAG — "smaller-looking" rejected v1 explains worse-than-champion', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  // v1 is the rejected challenger vs champion v0; gate fired the scalar-margin
  // rule with Δ +75.71 (needs ≤ -0.01).
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });

  const dagSvg = svgsByClass(host, 'ezn-dag')[0];
  assert(dagSvg, 'the lifecycle DAG rendered for v1');
  // a board circle shows the champion comparison (waffles: both 60.5 → Δ 0;
  // picky: 642.5 vs 105.5 → +537).
  const cmps = dagSvg.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-board-cmp'));
  assert(cmps.length >= 1, 'a board circle in the rendered DAG carries the champion comparison');
  const pickyCmp = dagSvg.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').includes('ezn-board-cmp') && n.getAttribute('data-champ-loss') === svg.fmt(105.5, 1))[0];
  assert(pickyCmp && pickyCmp.getAttribute('data-delta') === svg.fmtSigned(642.5 - 105.5, 1),
    'the rendered circle exposes the champion loss + Δ for the picky board');

  // the GATE node explains the scalar-margin rejection with the +Δ.
  const gate = dagSvg.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('ezn-gate-node'))[0];
  assert(gate, 'the rendered DAG GATE node carries the gate-node marker');
  assertEqual(gate.getAttribute('data-deciding-rule'), 'scalar_margin', 'the rendered GATE node names the scalar-margin rule');
  assert(hovercard.hasHovercard(gate) && !hasNativeTitle(gate), 'the rendered GATE uses the hovercard, not a native <title>');
  const gtitle = hovercardTextOf(gate);
  assert(/worse than champion/.test(gtitle), 'the rendered GATE explains the rejection as worse-than-champion (resolves "smaller Σ but rejected")');

  // the Σ node carries the candidate-vs-champion Σ Δ.
  const agg = dagSvg.querySelectorAll('[class]').filter((n) =>
    n.localName === 'g' && (n.getAttribute('class') || '').includes('ezn-node') && n.getAttribute('data-cand-sigma'))[0];
  assert(agg, 'the rendered Σ node carries the candidate Σ');
  assert(agg.getAttribute('data-champ-sigma') && agg.getAttribute('data-delta-sigma'), 'the rendered Σ node carries the champion Σ + the Δ');
});

test('lifecycle RUNG-PROGRESSION strip: projects rung0→rung1→final (Δ + survived/cut) off the SERVED racing field', () => {
  // v3’s racing path off the SERVED field: rung0 survived → rung1 → final promoted.
  const RACING_FIELD_SERVED = STRUCT.normalizeStructure(recorded('racing_ladder/racing_field'), false);
  const prog = STRUCT.candidateProgression(RACING_FIELD_SERVED, 'v3');
  assert(prog && Array.isArray(prog.stages), 'a progression was reconstructed for the racing candidate v3');
  assertDeep(prog.stages.map((s) => s.label), ['rung 0', 'rung 1', 'final'], 'the path is rung0 → rung1 → final');
  assertDeep(prog.stages.map((s) => s.kind), ['rung', 'rung', 'final'], 'the final stage is flagged kind=final');
  assertEqual(prog.stages[0].verdict, 'survived', 'v3 survived rung0 (it reached rung1)');
  assertEqual(prog.stages[2].verdict, 'promoted', 'v3 was promoted at the champion gate');
  assertEqual(prog.stages[2].delta, -32.19, 'the final stage carries the Δ-vs-champion');

  // a cut candidate (v4: rung0 → rung1, no final) ends "cut".
  const prog4 = STRUCT.candidateProgression(STRUCT.normalizeStructure(recorded('racing_ladder/racing_field'), false), 'v4');
  assert(prog4, 'v4 has a progression');
  assertEqual(prog4.stages[prog4.stages.length - 1].verdict, 'cut', 'v4 was cut at its last rung (no final reached)');

  // a gauntlet / non-racing payload has NO progression (strip suppressed).
  assertEqual(STRUCT.candidateProgression(FIXTURE['/api/tournaments'], 'v1'), null, 'a gauntlet candidate has no rung progression');
  // and an UNSERVED racing field (null) reads as unknown — never re-derived.
  assertEqual(STRUCT.candidateProgression(null, 'v3'), null, 'no served field → no progression');

  // the builder renders a fit-to-width SVG with a stage per rung.
  const node = dag.rungProgression({ stages: prog.stages });
  assertEqual(node.localName, 'svg', 'the progression strip is an SVG');
  assertEqual(node.getAttribute('width'), '100%', 'the progression strip is fit-to-width (width:100%)');
  assert((node.getAttribute('viewBox') || '').startsWith('0 0 '), 'it carries a responsive viewBox (theme-aware, scaled by the page pill)');
  const stages = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-rungprog-stage'));
  assertEqual(stages.length, 3, 'one stage per rung/final');
  assert(node.textContent.includes('rung 0') && node.textContent.includes('final'), 'the stages are labelled by rung');
});

test('lifecycle RUNG-PROGRESSION strip: a racing candidate page renders the strip; a gauntlet candidate page does NOT', async () => {
  // a racing candidate (v3) on the live reconstruction fixture.
  freshState();
  const F = racingLadderFixture();
  installFixtureMap(F);
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: RC_EPOCH, gen: 'v3' });
  assert(allByClass(host, 'dn-rungprog-strip')[0], 'the racing candidate page renders the rung-progression strip');
  const strip = svgsByClass(host, 'ezn-rungprog')[0];
  assert(strip, 'the progression SVG rendered');
  assert(strip.textContent.includes('rung 0') && strip.textContent.includes('final'), 'the strip shows rung0 → … → final');

  // a gauntlet candidate (default fixture) renders NO progression strip.
  freshState(); installFetch();
  const host2 = document.createElement('div');
  await candidate.render(host2, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  assertEqual(allByClass(host2, 'dn-rungprog-strip').length, 0, 'a gauntlet candidate page renders NO rung-progression strip');
  // and its board nodes are not expandable (one run per entry).
  const gnodes = host2.querySelectorAll('[class]').filter((n) => n.localName === 'g'
    && (n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-node'));
  for (const g of gnodes) assert(!(g.getAttribute('class') || '').includes('ezn-board-expandable'), 'a gauntlet board node is not expandable');
});

test('lifecycle per-run stack + progression strip are themed in the scoped stylesheet (theme-aware across the 13 themes)', () => {
  const css = readCss();
  assert(/\.ezn-board-runs-box\s*\{/.test(css), '.ezn-board-runs-box is styled');
  assert(/\.ezn-board-runs-box[^}]*var\(--v2-/.test(css), 'the per-run panel uses a theme variable');
  assert(/\.ezn-board-spark[^}]*var\(--v2-/.test(css), 'the per-run sparkline uses a theme variable');
  assert(/\.ezn-rungprog-dot[^}]*var\(--v2-/.test(css), 'the progression dots are token-themed');
  // the expansion is hidden until hover / focus-within / open (no-flash, reveal-on-demand).
  assert(/\.ezn-board-node:hover\s+\.ezn-board-runs/.test(css), 'the per-run panel reveals on hover');
});

// ====================================================================
// CHANGE 2 — the resizable LEFT side-panel (rail) sizing handle.
// ====================================================================

test('rail sizing: ui exposes a clamped rail-width range with a default + normalisation', () => {
  freshState();
  assertEqual(ui.DEFAULT_RAIL, 288, 'the rail defaults to the cozy 288px baseline');
  assert(ui.RAIL_MIN >= 120 && ui.RAIL_MIN < ui.DEFAULT_RAIL, 'a sensible minimum below the default');
  assert(ui.RAIL_MAX > ui.DEFAULT_RAIL, 'a sensible maximum above the default');
  assertEqual(ui.normaliseRail(10), ui.RAIL_MIN, 'below-range clamps up to the min');
  assertEqual(ui.normaliseRail(9999), ui.RAIL_MAX, 'above-range clamps down to the max');
  assertEqual(ui.normaliseRail('nonsense'), ui.DEFAULT_RAIL, 'a non-numeric value falls back to the default');
  // the shell re-exports the surface.
  assertEqual(shell.DEFAULT_RAIL, 288, 'the shell exposes the default rail width');
});

test('rail sizing: a draggable handle on the rail edge changes the rail width, persists + restores; the detail pane reflows', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const root = mountLiveShell('#/');

  // the handle is a focusable separator on the rail's right edge.
  const handle = allByClass(root, 'dt-rail-handle')[0];
  assert(handle, 'a rail-resize handle rendered on the rail edge');
  assertEqual(handle.getAttribute('role'), 'separator', 'the handle is a separator (keyboard-accessible)');
  assert((handle.getAttribute('aria-label') || '').length > 0, 'the handle carries an aria-label');

  // default rail width is stamped on the root as the --dt-rail token.
  assertEqual(root.getAttribute('data-t-rail'), String(ui.DEFAULT_RAIL), 'the rail starts at the default width');
  assert(root.style.cssText.includes('--dt-rail:' + ui.DEFAULT_RAIL + 'px'), 'the --dt-rail token is on the root (the detail pane’s 1fr column reflows around it)');

  // keyboard: ArrowRight widens, ArrowLeft narrows (the handle is accessible).
  handle.dispatchEvent({ type: 'keydown', key: 'ArrowRight', preventDefault() {} });
  const wider = +root.getAttribute('data-t-rail');
  assert(wider > ui.DEFAULT_RAIL, 'ArrowRight widened the rail');
  handle.dispatchEvent({ type: 'keydown', key: 'ArrowLeft', preventDefault() {} });
  handle.dispatchEvent({ type: 'keydown', key: 'ArrowLeft', preventDefault() {} });
  assert(+root.getAttribute('data-t-rail') < wider, 'ArrowLeft narrowed the rail');
  // Home/End jump to the bounds.
  handle.dispatchEvent({ type: 'keydown', key: 'End', preventDefault() {} });
  assertEqual(+root.getAttribute('data-t-rail'), ui.RAIL_MAX, 'End jumps to the max width');
  handle.dispatchEvent({ type: 'keydown', key: 'Home', preventDefault() {} });
  assertEqual(+root.getAttribute('data-t-rail'), ui.RAIL_MIN, 'Home jumps to the min width');

  // the programmatic applyRail() clamps + persists.
  shell.applyRail(360, root);
  assertEqual(root.getAttribute('data-t-rail'), '360', 'applyRail set the rail width');
  assert(root.style.cssText.includes('--dt-rail:360px'), 'the --dt-rail token reflects the chosen width');
  assertEqual(ui.readRail(), 360, 'the chosen rail width persisted to localStorage');

  // RESTORE: a fresh mount reads it back and re-applies it.
  const root2 = mountLiveShell('#/');
  assertEqual(root2.getAttribute('data-t-rail'), '360', 'a fresh mount restores the persisted rail width');
  assert(root2.style.cssText.includes('--dt-rail:360px'), 'the restored rail width is re-applied to the root');

  // it is page-CHROME sizing, distinct from the page-scale pill (separate axes).
  shell.applyScale(120, root2);
  assertEqual(root2.getAttribute('data-t-rail'), '360', 'a page-scale change leaves the rail width untouched');
});

// A pointer DRAG — pointerdown at X0, pointermove by Δx, pointerup — drives the
// rail width to start+Δ (within the clamp). This is the smooth-drag spine: the
// width tracks the pointer delta rather than snapping. Driven on the handle so
// the captured-pointer path is exercised (the harness ignores the unsupported
// setPointerCapture but the same handlers fire).
function dragRail(handle, x0, dx, extra) {
  handle.dispatchEvent({ type: 'pointerdown', pointerId: 1, clientX: x0, preventDefault() {} });
  if (typeof extra === 'function') extra();
  handle.dispatchEvent({ type: 'pointermove', pointerId: 1, clientX: x0 + dx, preventDefault() {} });
  handle.dispatchEvent({ type: 'pointerup', pointerId: 1, clientX: x0 + dx, preventDefault() {} });
}

test('rail sizing: a pointer DRAG tracks the pointer delta (start+Δ), captures the pointer, persists on pointerup', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const root = mountLiveShell('#/');
  const handle = allByClass(root, 'dt-rail-handle')[0];
  assert(handle, 'a rail-resize handle rendered');

  // start at the default width; drag the pointer +40px to the RIGHT.
  shell.applyRail(300, root);
  assertEqual(+root.getAttribute('data-t-rail'), 300, 'rail starts at 300');
  dragRail(handle, 500, 40);
  assertEqual(+root.getAttribute('data-t-rail'), 340, 'a +40px pointer drag widened the rail by exactly 40 (start+Δ — no jump)');
  // it adds the dragging class while in flight + removes it on release.
  assert(!(handle.getAttribute('class') || '').includes('dt-rail-dragging'), 'the dragging class is cleared on pointerup');
  // it persisted the final width (so a reload restores the dragged width).
  assertEqual(ui.readRail(), 340, 'the dragged width persisted to localStorage on pointerup');

  // dragging LEFT narrows by the delta magnitude.
  dragRail(handle, 500, -50);
  assertEqual(+root.getAttribute('data-t-rail'), 290, 'a −50px pointer drag narrowed the rail by exactly 50');
});

test('rail sizing: the REGRESSION — with a non-100% page scale, the drag tracks the pointer in LAYOUT space (no jump)', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const root = mountLiveShell('#/');
  const handle = allByClass(root, 'dt-rail-handle')[0];

  // apply a 120% page scale (zoom=1.2 on the root) — the handle lives INSIDE
  // the zoomed root, so a viewport-px pointer delta is 1.2× the layout-px delta.
  shell.applyScale(120, root);
  assert((root.style.cssText.includes('--dt-page-scale:1.2') || root.getAttribute('data-t-scale') === '120'),
    'the 120% scale is reflected on the root');
  shell.applyRail(300, root);

  // drag the pointer +120 VIEWPORT px. In layout space that is +120/1.2 = +100,
  // so the rail must end at 400 rather than 420. Tracking raw clientX would
  // over-track, because --dt-rail is laid out unscaled.
  dragRail(handle, 600, 120);
  assertEqual(+root.getAttribute('data-t-rail'), 400,
    'the rail tracked the pointer in LAYOUT space (Δx/scale = 100), not raw viewport px (the jumpiness fix)');

  // and at scale 80% a +80 viewport-px drag is +100 layout px.
  shell.applyScale(80, root);
  shell.applyRail(300, root);
  dragRail(handle, 600, 80);
  assertEqual(+root.getAttribute('data-t-rail'), 400, 'at 80% scale the drag still maps Δx/scale into layout space');
});

test('rail sizing: a pointer drag CLAMPS at the min/max (a big drag cannot collapse or overrun the rail)', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const root = mountLiveShell('#/');
  const handle = allByClass(root, 'dt-rail-handle')[0];

  shell.applyRail(ui.DEFAULT_RAIL, root);
  // a huge rightward drag clamps to the max.
  dragRail(handle, 500, 5000);
  assertEqual(+root.getAttribute('data-t-rail'), ui.RAIL_MAX, 'an oversize drag clamps at RAIL_MAX');
  // a huge leftward drag clamps to the min.
  shell.applyRail(ui.DEFAULT_RAIL, root);
  dragRail(handle, 500, -5000);
  assertEqual(+root.getAttribute('data-t-rail'), ui.RAIL_MIN, 'an undersize drag clamps at RAIL_MIN');
});

test('rail sizing: a re-render MID-DRAG does NOT snap the width back to the persisted value', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const root = mountLiveShell('#/');
  const handle = allByClass(root, 'dt-rail-handle')[0];

  // persisted width is 300; the live drag will move past it.
  shell.applyRail(300, root);
  assertEqual(ui.readRail(), 300, 'the persisted width is 300');

  // start the drag and move +40 → 340 (live, and not yet persisted).
  handle.dispatchEvent({ type: 'pointerdown', pointerId: 1, clientX: 500, preventDefault() {} });
  handle.dispatchEvent({ type: 'pointermove', pointerId: 1, clientX: 540, preventDefault() {} });
  assertEqual(+root.getAttribute('data-t-rail'), 340, 'mid-drag the rail tracks the pointer (340)');
  assertEqual(ui.readRail(), 300, 'the live drag has NOT persisted yet (still 300 in storage)');

  // MID-DRAG re-render: a competing caller (a state:changed tick) re-applies the
  // PERSISTED width — the guard makes it a no-op so the rail does not snap back.
  shell.applyRail(ui.readRail(), root);
  assertEqual(+root.getAttribute('data-t-rail'), 340, 'the mid-drag re-render did NOT snap the width back to 300');

  // continue dragging then release — the final dragged width stands + persists.
  handle.dispatchEvent({ type: 'pointermove', pointerId: 1, clientX: 560, preventDefault() {} });
  handle.dispatchEvent({ type: 'pointerup', pointerId: 1, clientX: 560, preventDefault() {} });
  assertEqual(+root.getAttribute('data-t-rail'), 360, 'the final dragged width (start+60) survived the mid-drag re-render');
  assertEqual(ui.readRail(), 360, 'the final dragged width persisted on pointerup');
});

test('rail sizing: ui.pageScaleOf reads the live page-scale factor (zoom / --dt-page-scale / data-t-scale)', () => {
  const root = document.createElement('div');
  assertEqual(ui.pageScaleOf(root), 1, 'no scale set → identity factor 1');
  root.style.setProperty('--dt-page-scale', '1.25');
  assertEqual(ui.pageScaleOf(root), 1.25, 'reads the --dt-page-scale ratio');
  root.style.zoom = '0.8';
  assertEqual(ui.pageScaleOf(root), 0.8, 'prefers the inline zoom when present');
  const root2 = document.createElement('div');
  root2.setAttribute('data-t-scale', '150');
  assertEqual(ui.pageScaleOf(root2), 1.5, 'falls back to the data-t-scale percent attribute');
  assertEqual(ui.pageScaleOf(null), 1, 'a null root is the identity factor');
});

test('rail sizing CSS: the body grid keys on --dt-rail and the handle has a col-resize cursor (no-flash chrome)', () => {
  const css = readCss().replace(/\n/g, ' ');
  assert(/\.dt-body\s*\{[^}]*grid-template-columns:[^;]*var\(--dt-rail/.test(css), 'the body grid’s first column is the --dt-rail width');
  assert(/\.dt-rail-handle\s*\{[^}]*cursor:\s*col-resize/.test(css), 'the rail handle carries a col-resize cursor');
});

// ====================================================================
// The upper-left button reads "up": it navigates UP the selection
// hierarchy rather than acting as a browser-back control.
// ====================================================================

test('up button: the upper-left control reads "up" (not "back"), labels itself "navigate up", and still navigates to the parent route', async () => {
  freshState();
  const root = mountLiveShell(`#/e/${EPOCH_ID}/gen/v1`);
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  const upBtn = allByClass(root, 'dt-back')[0];
  assert(upBtn, 'the upper-left navigation control rendered');
  // it reads "up", NOT "back".
  const txt = allByClass(root, 'dt-back-text')[0];
  assert(txt && txt.textContent === 'up', 'the button text reads "up"');
  assert(!(root.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-back-text'))[0].textContent.toLowerCase().includes('back')),
    'the button no longer reads "back"');
  // the glyph is an up-arrow, and the aria-label/title name "up" / "navigate up".
  const glyph = allByClass(root, 'dt-back-glyph')[0];
  assert(glyph && glyph.textContent === '↑', 'the glyph is an up arrow (↑)');
  assert((upBtn.getAttribute('aria-label') || '').toLowerCase().includes('up'), 'the aria-label names "up" (navigate up)');
  assert(!(upBtn.getAttribute('aria-label') || '').toLowerCase().includes('back'), 'the aria-label no longer says "back"');
  assert((upBtn.getAttribute('title') || '').toLowerCase().includes('up'), 'the title names "up"');

  // BEHAVIOUR UNCHANGED: clicking it still navigates UP to the parent route.
  upBtn.dispatchEvent({ type: 'click' });
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  assertEqual(location.hash, `#/e/${EPOCH_ID}/gens`, 'clicking "up" navigates to the parent route (candidate → generations)');
});

await run();

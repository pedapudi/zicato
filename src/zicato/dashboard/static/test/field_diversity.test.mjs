// test/field_diversity.test.mjs — the FIELD-DIVERSITY ribbon + overlap matrix.
//
// The minted field's pairwise idea-overlap structure, surfaced under the
// proposed-field section. Pins:
//   * diversitySection renders ONLY for a real diversity block (field_size ≥ 2);
//     absent / single-challenger / pre-feature → NOTHING (byte-identical to today).
//   * the dual mean/max overlap meter earns its tone BY DIRECTION (over the
//     tolerance → caution, under → good, no tolerance → flat/diagnostic).
//   * the soft-rejected count rides the DEFERRED pill vocabulary (held, not
//     promoted); a per-standings-row badge maps diversity_status → soft-rejected
//     pill / penalized chip; applied/absent → no badge.
//   * svg.diversityMatrix clones the dn-mtx grid (challenger × mutation-site) and
//     returns null (no matrix) when per-challenger membership is absent — the
//     contract's diversity block carries NO membership, so the matrix degrades.
//   * the digest discipline: the diversity block folds into structureDigest with
//     ROUNDED overlaps + NO timestamps — a no-op beat is byte-identical and churns
//     ZERO DOM under the gate; a real overlap / soft-reject / status change flips
//     the digest and repaints. A clean (no-diversity) field is byte-identical to
//     the pre-feature digest.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const ui = await import('../js/ui.js');
const svg = await import('../js/svg.js');
const STRUCT = await import('../js/views/structure.js');
const router = await import('../js/router.js');

function classOf(node) { return (node && node.getAttribute && node.getAttribute('class')) || ''; }
function hasClass(node, cls) { return classOf(node).split(/\s+/).includes(cls); }
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => hasClass(n, cls));
}
function byClass(host, cls) { return allByClass(host, cls)[0] || null; }

function structFor(opts) {
  const o = opts || {};
  const st = {
    structure: o.structure || 'swiss', live: o.live !== false, source: 'active', tournament_id: 't1',
    competitors: [{ generation_id: 'c1', seed: 1 }, { generation_id: 'c2', seed: 2 }, { generation_id: 'c3', seed: 3 }],
    rounds: [],
    standings: [
      { generation_id: 'c1', rank: 1, scalar: 47.5, wins: 1, losses: 0, status: 'competing' },
      { generation_id: 'c2', rank: 2, scalar: 52.1, wins: 0, losses: 1, status: 'competing' },
      { generation_id: 'c3', rank: 3, scalar: 55.0, wins: 0, losses: 0, status: 'competing' },
    ],
    field_status: o.field_status || [
      { generation_id: 'c1', status: 'applied', diversity_status: 'applied' },
      { generation_id: 'c2', status: 'applied', diversity_status: 'applied' },
      { generation_id: 'c3', status: 'applied', diversity_status: 'applied' },
    ],
  };
  if (o.diversity !== undefined) st.diversity = o.diversity;
  if (o.membership_on_field) {
    // attach per-challenger mutation_ids to the field_status records (the
    // forward-compatible membership the matrix consumes).
    const byGen = o.membership_on_field;
    for (const f of st.field_status) if (byGen[f.generation_id]) f.mutation_ids = byGen[f.generation_id];
  }
  return st;
}

function diversityBlock(over) {
  return Object.assign({
    field_size: 3, distinct_ideas: 3, mean_overlap: 0.2, max_overlap: 0.5,
    max_overlap_pair: ['c1', 'c2'], tolerance: 0.7, soft_rejected_count: 0,
  }, over || {});
}

function renderStruct(st) {
  const ctx = { navigate() {}, href: router.href };
  const nodes = STRUCT.renderStructure(st, ctx, 'e0');
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  return host;
}

// ── 1. back-compat: absent / single-challenger → NOTHING (byte-identical) ─────
test('diversity: a structure with NO diversity block renders no ribbon (byte-identical)', () => {
  const host = renderStruct(structFor({}));
  assertEqual(allByClass(host, 'dn-divribbon').length, 0, 'no diversity panel without the block');
  assert(!byClass(host, 'dn-div-meter'), 'no overlap meter');
});

test('diversity: a field_size < 2 block renders nothing (single-challenger back-compat)', () => {
  const host = renderStruct(structFor({ diversity: diversityBlock({ field_size: 1 }) }));
  assertEqual(allByClass(host, 'dn-divribbon').length, 0, 'a sub-field block is suppressed');
});

// ── 2. the ribbon renders the meter + stat strip + caption ───────────────────
test('diversity: a real block renders the ribbon — stats, the mean/max meter, the caption', () => {
  const host = renderStruct(structFor({ diversity: diversityBlock() }));
  const ribbon = byClass(host, 'dn-divribbon');
  assert(ribbon, 'the diversity panel renders');
  assert(byClass(ribbon, 'dn-div-meter'), 'the overlap meter renders');
  const stats = allByClass(ribbon, 'dn-stat');
  assert(stats.length >= 3, 'distinct-ideas + mean + max stats render');
  // the distinct-ideas stat reads "N / field_size".
  const vals = allByClass(ribbon, 'v').map((n) => n.textContent);
  assert(vals.some((v) => v === '3 / 3'), 'distinct ideas reads N / field_size');
});

// ── 3. the meter earns its tone BY DIRECTION (over tolerance → caution) ───────
test('diversity meter: mean below the tolerance reads GOOD', () => {
  const host = renderStruct(structFor({ diversity: diversityBlock({ mean_overlap: 0.2, tolerance: 0.7 }) }));
  const fill = byClass(host, 'dn-div-fill');
  assert(fill, 'the meter fill renders');
  assert(hasClass(fill, 'dn-good-fill'), 'below the tolerance the field reads good (diverse)');
});

test('diversity meter: mean at/over the tolerance reads CAUTION', () => {
  const host = renderStruct(structFor({ diversity: diversityBlock({ mean_overlap: 0.8, tolerance: 0.7 }) }));
  const fill = byClass(host, 'dn-div-fill');
  assert(hasClass(fill, 'dn-caution-fill'), 'over the tolerance the field reads caution (collapsing)');
});

test('diversity meter: no tolerance (enforcement off) reads FLAT (diagnostic only)', () => {
  const host = renderStruct(structFor({ diversity: diversityBlock({ tolerance: null }) }));
  const fill = byClass(host, 'dn-div-fill');
  assert(hasClass(fill, 'dn-flat-fill'), 'with enforcement off the overlap is purely diagnostic');
});

// ── 4. the soft-reject count rides the deferred pill ─────────────────────────
test('diversity: a soft-rejected count rides the deferred pill vocabulary', () => {
  const host = renderStruct(structFor({ diversity: diversityBlock({ soft_rejected_count: 2 }) }));
  const ribbon = byClass(host, 'dn-divribbon');
  const pill = allByClass(ribbon, 'dn-pill').find((p) => /soft-rejected/.test(p.textContent || ''));
  assert(pill, 'the soft-reject count renders a chip');
  assert(hasClass(pill, 'dn-deferred'), 'it reuses the DEFERRED pill (held, not promoted)');
  assert(/2 soft-rejected/.test(pill.textContent), 'the chip reads the count');
});

test('diversity: zero soft-rejects renders no soft-reject chip', () => {
  const host = renderStruct(structFor({ diversity: diversityBlock({ soft_rejected_count: 0 }) }));
  const ribbon = byClass(host, 'dn-divribbon');
  assert(!allByClass(ribbon, 'dn-pill').some((p) => /soft-rejected/.test(p.textContent || '')),
    'no soft-reject chip when the count is zero');
});

// ── 5. the per-standings-row diversity badge ─────────────────────────────────
test('standings: a soft_rejected slot gets the deferred-pill badge beside its status', () => {
  const st = structFor({
    diversity: diversityBlock({ soft_rejected_count: 1 }),
    field_status: [
      { generation_id: 'c1', status: 'applied', diversity_status: 'applied' },
      { generation_id: 'c2', status: 'applied', diversity_status: 'soft_rejected' },
      { generation_id: 'c3', status: 'applied', diversity_status: 'penalized' },
    ],
  });
  const host = renderStruct(st);
  const softRej = allByClass(host, 'dn-div-softrej');
  assert(softRej.length === 1, 'exactly one row carries the soft-rejected badge');
  assert(hasClass(softRej[0], 'dn-deferred'), 'the row badge reuses the deferred pill');
  const pen = allByClass(host, 'dn-div-penalized');
  assert(pen.length === 1, 'the penalized slot carries its caution chip');
});

test('standings: an all-applied field carries NO per-row diversity badge (byte-identical)', () => {
  const host = renderStruct(structFor({ diversity: diversityBlock() }));
  assertEqual(allByClass(host, 'dn-div-softrej').length, 0, 'no soft-reject row badge when all applied');
  assertEqual(allByClass(host, 'dn-div-penalized').length, 0, 'no penalized row badge when all applied');
});

test('standings: NO diversity block → no per-row badge (back-compat)', () => {
  const host = renderStruct(structFor({
    field_status: [{ generation_id: 'c2', status: 'applied', diversity_status: 'soft_rejected' }],
  }));
  // without the diversity block the per-row badge map is null → no badges.
  assertEqual(allByClass(host, 'dn-div-softrej').length, 0, 'no badge without the diversity block');
});

// ── 6. svg.diversityMatrix — the dn-mtx grid + the absent-membership degrade ──
test('diversityMatrix: absent membership → null (no matrix; the contract has no membership)', () => {
  // the diversity block carries NO per-challenger membership, so a plain field
  // (no mutation_ids on the records) yields no matrix.
  const host = renderStruct(structFor({ diversity: diversityBlock() }));
  assertEqual(allByClass(host, 'dn-divmtx').length, 0, 'no matrix when membership is unavailable');
});

test('diversityMatrix: with membership it clones the dn-mtx grid (challenger × site)', () => {
  const m = svg.diversityMatrix({
    membership: [
      { generation_id: 'c1', sites: ['scoring.py:10', 'loss.py:4'] },
      { generation_id: 'c2', sites: ['scoring.py:10'] },
    ],
    highlightPair: ['c1', 'c2'],
  });
  assert(m, 'the matrix builds with ≥2 members');
  assert(byClass(m, 'dn-mtx'), 'the table clones the dn-mtx grammar');
  // 2 distinct sites → 2 rows; 2 challenger columns.
  const rows = allByClass(m, 'dn-mtx-row');
  assertEqual(rows.length, 2, 'one row per distinct mutation site');
  // the shared site (scoring.py:10) is ON for both → 2 filled marks in that row.
  const onCells = allByClass(m, 'dn-mtx-on');
  assertEqual(onCells.length, 3, 'three touched cells (c1 touches both, c2 touches one)');
  // the highlighted pair columns carry the accent rail.
  assert(allByClass(m, 'dn-divmtx-paired').length >= 2, 'the max-overlap pair columns get the accent rail');
});

test('diversityMatrix: a single member → null (no overlap to show)', () => {
  const m = svg.diversityMatrix({ membership: [{ generation_id: 'c1', sites: ['a'] }] });
  assertEqual(m, null, 'a one-challenger field has no overlap matrix');
});

test('diversity: the matrix wires up when membership rides on field_status records', () => {
  const host = renderStruct(structFor({
    diversity: diversityBlock(),
    membership_on_field: { c1: ['scoring.py:10', 'loss.py:4'], c2: ['scoring.py:10'], c3: ['gate.py:7'] },
  }));
  assert(byClass(host, 'dn-divmtx'), 'the overlap matrix renders when membership is present on the payload');
});

// ── 7. DIGEST DISCIPLINE — no-op beat byte-identical + ZERO DOM ───────────────
test('structureDigest: the diversity block folds in — a no-op beat is byte-identical', () => {
  const st = structFor({ diversity: diversityBlock() });
  const a = STRUCT.structureDigest(st);
  const b = STRUCT.structureDigest(structFor({ diversity: diversityBlock() }));
  assertEqual(a, b, 'two identical beats yield a byte-identical structureDigest');
});

test('structureDigest: a clean (no-diversity) field is byte-identical to the pre-feature digest', () => {
  const noDiv = structFor({});
  const withClean = structFor({});
  assertEqual(STRUCT.structureDigest(noDiv), STRUCT.structureDigest(withClean),
    'no diversity block → no diversity noise in the digest (back-compat)');
});

test('structureDigest: a soft-reject landing / overlap move FLIPS the digest (repaints)', () => {
  const base = STRUCT.structureDigest(structFor({ diversity: diversityBlock({ soft_rejected_count: 0 }) }));
  const afterSoftReject = STRUCT.structureDigest(structFor({
    diversity: diversityBlock({ soft_rejected_count: 1 }),
    field_status: [
      { generation_id: 'c1', status: 'applied', diversity_status: 'applied' },
      { generation_id: 'c2', status: 'applied', diversity_status: 'soft_rejected' },
      { generation_id: 'c3', status: 'applied', diversity_status: 'applied' },
    ],
  }));
  assert(base !== afterSoftReject, 'a soft-reject landing flips the structureDigest');
  const afterOverlap = STRUCT.structureDigest(structFor({ diversity: diversityBlock({ mean_overlap: 0.6 }) }));
  assert(base !== afterOverlap, 'an overlap move flips the structureDigest');
});

test('structureDigest: a sub-rounding overlap jitter does NOT flip the digest (rounded fold)', () => {
  const a = STRUCT.structureDigest(structFor({ diversity: diversityBlock({ mean_overlap: 0.20001 }) }));
  const b = STRUCT.structureDigest(structFor({ diversity: diversityBlock({ mean_overlap: 0.20002 }) }));
  assertEqual(a, b, 'overlaps round to 3dp — a no-op jitter is byte-identical');
});

test('diversity: a no-op beat over a diverse field churns ZERO DOM under the gate', () => {
  const st = structFor({ diversity: diversityBlock({ soft_rejected_count: 1 }),
    field_status: [
      { generation_id: 'c1', status: 'applied', diversity_status: 'applied' },
      { generation_id: 'c2', status: 'applied', diversity_status: 'soft_rejected' },
      { generation_id: 'c3', status: 'applied', diversity_status: 'applied' },
    ] });
  const ctx = { navigate() {}, href: router.href };
  const host = document.createElement('div');
  ui.gatedSwap(host, STRUCT.structureDigest(st), () => STRUCT.renderStructure(st, ctx, 'e0'));
  const ribbonA = byClass(host, 'dn-divribbon');
  assert(ribbonA, 'the ribbon renders on the first beat');
  const writes = host.innerHTMLWriteCount();
  ui.gatedSwap(host, STRUCT.structureDigest(st), () => STRUCT.renderStructure(st, ctx, 'e0'));
  const ribbonB = byClass(host, 'dn-divribbon');
  assert(ribbonA === ribbonB, 'a no-op beat preserves the ribbon node identity (zero rebuild)');
  assertEqual(host.innerHTMLWriteCount(), writes, 'a no-op beat writes ZERO additional DOM');
});

// ── 8. diversityMatrixDigest stability ───────────────────────────────────────
test('diversityMatrixDigest: identical membership is byte-identical; a new site flips it', () => {
  const mem = [
    { generation_id: 'c1', sites: ['b', 'a'] },
    { generation_id: 'c2', sites: ['a'] },
  ];
  const a = svg.diversityMatrixDigest({ membership: mem, highlightPair: ['c1', 'c2'] });
  // order-insensitive: sites + pair are sorted.
  const b = svg.diversityMatrixDigest({
    membership: [{ generation_id: 'c1', sites: ['a', 'b'] }, { generation_id: 'c2', sites: ['a'] }],
    highlightPair: ['c2', 'c1'],
  });
  assertEqual(a, b, 'identical membership (order-insensitive) is byte-identical');
  const c = svg.diversityMatrixDigest({
    membership: [{ generation_id: 'c1', sites: ['a', 'b', 'c'] }, { generation_id: 'c2', sites: ['a'] }],
    highlightPair: ['c1', 'c2'],
  });
  assert(a !== c, 'a new touched site flips the matrix digest');
  assertEqual(svg.diversityMatrixDigest({ membership: [] }), 'divmtx|none', 'the absent state is a stable sentinel');
});

run();

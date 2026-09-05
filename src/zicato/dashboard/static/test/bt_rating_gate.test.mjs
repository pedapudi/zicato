// test/bt_rating_gate.test.mjs — the BRADLEY–TERRY UNCERTAINTY GATE (marquee).
//
// Before the deterministic rule ladder, a confidence-thresholded run resolves
// the winner by a Bradley–Terry strength estimate: each side carries θ̂ + a
// credible interval, and the gate promotes only when P(θ_child > θ_champion)
// clears the configured threshold. `ratingBlock(gate.rating)` surfaces that
// pre-gate as the operator's first read: two θ̂ whiskers + the P-bar against the
// threshold marker, and (when deferred) the replicationStrip (replicates-spent
// dt-rungstep pips + the next closest-CI duel + a CI-convergence sparkline; a
// schedule-exhausted deferral reads "inconclusive", never a faked crown).
//
// Pins (all keys read VERBATIM from build_rating_view's contract):
//   * rating absent / present:false → null → gate panel byte-identical to today;
//   * n_duels below the credible-fit minimum (3) → a "rating forms after N duels"
//     placeholder rather than a faked estimate;
//   * present + credible → two whiskers (θ̂ + [ci_lo, ci_hi]) + the P-bar w/ the
//     threshold marker; the challenger earns good/bad by θ̂ direction (no hue);
//   * decision deferred → the replicationStrip (pips + next_duel + spark);
//   * schedule exhausted (no next_duel and not credible) → an "inconclusive" caption;
//   * the radar scalar vertex carries the CI band (buildRadarModel → chalBand);
//   * ratingDigest: a no-op beat is byte-identical (rounded, NO timestamps) while
//     a duel resolving / a CI tightening / P moving flips it (the bug class).

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const candidate = await import('../js/views/candidate.js');
const svg = await import('../js/svg.js');
const structure = { ...await import('../js/tournament_model.js'), ...await import('../js/views/structure.js') };

const CTX = { href: (v, p) => '#' + v + '/' + (p && p.gen || ''), navigate: () => {} };
function mountNodes(nodes) {
  const h = document.createElement('div');
  for (const n of (Array.isArray(nodes) ? nodes : [nodes])) if (n) h.appendChild(n);
  return h;
}

function classOf(node) { return (node && node.getAttribute && node.getAttribute('class')) || ''; }
function hasClass(node, cls) { return classOf(node).split(/\s+/).includes(cls); }
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => hasClass(n, cls));
}
function mountInto(node) { const h = document.createElement('div'); if (node) h.appendChild(node); return h; }

// a credible, deferred rating fixture (challenger ahead but CIs still overlap).
function credibleDeferred() {
  return {
    present: true, credible: true,
    champion: { theta: 0.0, se: 0.2, ci_lo: -0.4, ci_hi: 0.4 },
    challenger: { theta: 0.5, se: 0.25, ci_lo: 0.1, ci_hi: 0.9 },
    p_stronger: 0.82, threshold: 0.9, decision: 'deferred', ci_overlap: true,
    replicates_spent: 4, n_duels: 6,
    next_duel: { left: 'v3', right: 'v5' },
    ci_history: [
      { p_stronger: 0.61, ci_overlap: true, replicates_spent: 1 },
      { p_stronger: 0.70, ci_overlap: true, replicates_spent: 2 },
      { p_stronger: 0.82, ci_overlap: true, replicates_spent: 4 },
    ],
  };
}

// ── 1. back-compat: absent / present:false → NOTHING ─────────────────────────
test('ratingBlock: absent or present:false → null (gate panel byte-identical to today)', () => {
  assertEqual(candidate.ratingBlock(undefined), null, 'absent → null');
  assertEqual(candidate.ratingBlock(null), null, 'null → null');
  assertEqual(candidate.ratingBlock({ present: false }), null, 'present:false (feature OFF) → null');
});

test('gatePanel: a pre-BT gate (no rating) renders NO rating block (byte-identical to today)', () => {
  const host = mountInto(candidate.gatePanel({
    decision: 'promoted', delta_scalar: 1.2, rules: [],
  }));
  assertEqual(allByClass(host, 'dn-bt-rating').length, 0, 'no rating block on a pre-BT gate');
});

test('gatePanel: present:false rating renders NO rating block', () => {
  const host = mountInto(candidate.gatePanel({
    decision: 'deferred', rules: [], rating: { present: false },
  }));
  assertEqual(allByClass(host, 'dn-bt-rating').length, 0, 'present:false → no block');
});

// ── 2. forming: below the credible-fit minimum → a placeholder, no estimate ──
test('ratingBlock: n_duels below the credible minimum → a "rating forms after N duels" placeholder', () => {
  const host = mountInto(candidate.ratingBlock({
    present: true, credible: false, n_duels: 1, decision: 'deferred',
    champion: null, challenger: null, threshold: 0.9,
  }));
  assert(host.textContent.includes('rating forms after 3 duels'), 'reads the credible-fit minimum (3)');
  assert(host.textContent.includes('1 resolved'), 'reads the resolved-duel count');
  // NO whisker / prob-bar estimate is drawn below the minimum.
  assertEqual(allByClass(host, 'dn-bt-whiskers').length, 0, 'no θ̂ whiskers below the minimum');
  assertEqual(allByClass(host, 'dn-bt-probwrap').length, 0, 'no P-bar below the minimum (never a faked estimate)');
  assert(allByClass(host, 'dn-bt-forming').length === 1, 'the forming placeholder is shown');
});

// ── 3. credible: two whiskers + the P-bar against the threshold ──────────────
test('ratingBlock: a credible rating draws two θ̂ whiskers + the P(stronger) bar w/ the threshold marker', () => {
  const host = mountInto(candidate.ratingBlock(credibleDeferred()));
  assertEqual(allByClass(host, 'dn-bt-whiskers').length, 1, 'the whiskers row is drawn');
  assertEqual(allByClass(host, 'dn-bt-whisker').length, 2, 'two whiskers — champion + challenger');
  // the θ̂ point + CI readout reads off each whisker.
  assert(host.textContent.includes('θ̂ 0.50'), 'challenger θ̂ reads (2dp)');
  assert(host.textContent.includes('[0.10, 0.90]'), 'challenger CI reads [ci_lo, ci_hi]');
  // the P-bar carries the probability + the threshold marker + label.
  assertEqual(allByClass(host, 'dn-bt-prob').length, 1, 'the P-bar is drawn');
  assert(host.textContent.includes('0.82'), 'P(stronger) reads (2dp)');
  assert(allByClass(host, 'dn-bt-prob-thr').length === 1, 'the threshold marker line is drawn');
  assert(host.textContent.includes('thr 0.90'), 'the threshold value reads');
});

test('ratingBlock: the challenger whisker earns its tone by θ̂ DIRECTION (no new hue)', () => {
  // challenger θ̂ ahead of champion → dn-good.
  const ahead = mountInto(candidate.ratingBlock(credibleDeferred()));
  assert(allByClass(ahead, 'dn-good').length >= 1, 'a leading challenger earns dn-good');
  // a behind challenger → dn-bad.
  const r = credibleDeferred();
  r.challenger = { theta: -0.5, se: 0.25, ci_lo: -0.9, ci_hi: -0.1 };
  const behind = mountInto(candidate.ratingBlock(r));
  assert(allByClass(behind, 'dn-bad').length >= 1, 'a trailing challenger earns dn-bad');
});

test('ratingBlock: an unfit side (null) draws a faint "unfit" rail, not an estimate', () => {
  const r = credibleDeferred();
  r.challenger = null;       // not yet fit
  const host = mountInto(candidate.ratingBlock(r));
  assert(host.textContent.includes('unfit') || host.textContent.includes('not yet fit'), 'the unfit side reads "unfit" / "not yet fit"');
});

// ── 4. deferred → the replication strip ──────────────────────────────────────
test('ratingBlock: a deferred decision drives the replicationStrip (pips + next duel + spark)', () => {
  const host = mountInto(candidate.ratingBlock(credibleDeferred()));
  assertEqual(allByClass(host, 'dn-bt-replication').length, 1, 'the replication strip is shown when deferred');
  // (1) replicates-spent pips in the dt-rungstep treatment.
  assert(allByClass(host, 'dt-rungstep').length >= 1, 'the replicates-spent pips use the dt-rungstep token');
  assertEqual(allByClass(host, 'dt-rungstep-pip').length, 4, 'four pips for replicates_spent:4');
  // (2) the next closest-CI duel.
  assert(allByClass(host, 'dn-bt-nextduel').length === 1 && host.textContent.includes('v3 vs v5'), 'the next closest-CI duel reads');
  // (3) the CI-convergence sparkline.
  assert(allByClass(host, 'dn-bt-convspark').length === 1, 'the CI-convergence sparkline is drawn');
  assert(allByClass(host, 'dn-spark').length >= 1, 'the convergence track reuses the dn-spark component');
});

test('ratingBlock: a PROMOTED rating omits the replication strip (resolved — nothing left to sharpen)', () => {
  const r = credibleDeferred();
  r.decision = 'promoted'; r.p_stronger = 0.95; r.next_duel = null;
  const host = mountInto(candidate.ratingBlock(r));
  assertEqual(allByClass(host, 'dn-bt-replication').length, 0, 'no replication strip on a resolved (promoted) rating');
});

test('ratingBlock: schedule EXHAUSTED (no next_duel, not credible) → an "inconclusive" caption, NEVER a faked crown', () => {
  const r = credibleDeferred();
  r.credible = false; r.next_duel = null; r.n_duels = 8; r.decision = 'deferred';
  const host = mountInto(candidate.ratingBlock(r));
  assert(allByClass(host, 'dn-bt-inconclusive').length === 1, 'the inconclusive caption is shown');
  assert(host.textContent.toLowerCase().includes('inconclusive'), 'reads "inconclusive"');
  // the decision verdict is NEVER faked to a crown.
  assert(!host.textContent.toLowerCase().includes('promoted'), 'an exhausted-schedule deferral never reads "promoted"');
});

// ── 5. the radar CI band on the scalar vertex ────────────────────────────────
test('buildRadarModel: the scalar axis carries the BT credible-interval band (chalBand) when the rating fits', () => {
  const model = candidate.buildRadarModel({
    primaryGate: {
      delta_pass_rate: 0.0,
      scalar_components: { champion: { judgeA: 0.3, judgeB: 0.2 }, challenger: { judgeA: 0.25, judgeB: 0.18 } },
    },
    championScalar: 50, settledScalar: 45, projected: null, entries: [],
    // the rating rides as a TOP-LEVEL opts key (as the dossier threads
    // `rating: primaryGate && primaryGate.rating` from the live gate payload).
    rating: credibleDeferred(),
  });
  assert(model && Array.isArray(model.axes), 'a radar model is built');
  const scAxis = model.axes.find((a) => a.label === 'scalar');
  assert(scAxis, 'the scalar axis is present');
  assert(scAxis.chalBand && svg.isNum(scAxis.chalBand.lo) && svg.isNum(scAxis.chalBand.hi), 'the scalar vertex carries a chalBand {lo,hi}');
  assert(scAxis.chalBand.lo < scAxis.chalBand.hi, 'the band runs inner→outer');
});

test('radarSilhouette: a chalBand on the scalar axis draws the CI band line + ticks; absent → none', () => {
  const withBand = svg.radarSilhouette({
    axes: [
      { label: 'scalar', champ: 0.5, chal: 0.6, chalBand: { lo: 0.45, hi: 0.75 } },
      { label: 'pass-rate', champ: 0.5, chal: 0.7 },
      { label: 'judgeA', champ: 0.4, chal: 0.55 },
    ],
    raw: [], live: false,
  });
  const host = mountInto(withBand);
  assert(allByClass(host, 'dn-radar-ciband').length === 1, 'the CI band line is drawn on the banded axis');
  assert(allByClass(host, 'dn-radar-citick').length === 2, 'two credible-endpoint ticks are drawn');
  // no band axis → no band drawn (back-compat).
  const noBand = svg.radarSilhouette({
    axes: [
      { label: 'scalar', champ: 0.5, chal: 0.6 },
      { label: 'pass-rate', champ: 0.5, chal: 0.7 },
      { label: 'judgeA', champ: 0.4, chal: 0.55 },
    ],
    raw: [], live: false,
  });
  assertEqual(allByClass(mountInto(noBand), 'dn-radar-ciband').length, 0, 'no chalBand → no band (byte-identical to the pre-rating radar)');
});

// ── 6. digest discipline: no-op stability + duel-resolving repaint ───────────
test('ratingDigest: absent / present:false → null (contributes NOTHING to the gate digest)', () => {
  assertEqual(candidate.ratingDigest(undefined), null, 'absent → null (back-compat digest)');
  assertEqual(candidate.ratingDigest({ present: false }), null, 'present:false → null');
});

test('ratingDigest: a no-op beat is byte-identical (rounded, NO timestamps)', () => {
  const a = candidate.ratingDigest(credibleDeferred());
  // a second beat with sub-3dp jitter in θ̂ / P / CI — must NOT flip the digest.
  const r = credibleDeferred();
  r.champion.theta = 0.0004; r.challenger.theta = 0.5003;
  r.p_stronger = 0.8204; r.challenger.ci_hi = 0.9003;
  r.ci_history[2].p_stronger = 0.8201;
  const b = candidate.ratingDigest(r);
  assertEqual(JSON.stringify(a), JSON.stringify(b), 'sub-3dp jitter on a no-op beat → IDENTICAL digest (no flash)');
});

test('ratingDigest: a DUEL resolving (n_duels grows, P moves, CI tightens) flips the digest (repaints)', () => {
  const a = candidate.ratingDigest(credibleDeferred());
  const r = credibleDeferred();
  r.n_duels = 7;                       // a 7th duel resolved
  r.p_stronger = 0.91;                 // P moved past the threshold
  r.challenger.ci_lo = 0.2; r.challenger.ci_hi = 0.8;  // CI tightened
  r.replicates_spent = 5;
  r.ci_history.push({ p_stronger: 0.91, ci_overlap: false, replicates_spent: 5 });
  const b = candidate.ratingDigest(r);
  assert(JSON.stringify(a) !== JSON.stringify(b), 'a duel resolving flips the digest');
});

test('ratingDigest: a rating APPEARING (pre-BT → present) flips the digest', () => {
  const none = candidate.ratingDigest({ present: false });
  const some = candidate.ratingDigest(credibleDeferred());
  assert(JSON.stringify(none) !== JSON.stringify(some), 'a rating appearing (null → present) flips the digest');
});

test('radarSilhouetteDigest: a CI band TIGHTENING flips the digest; a no-op beat stays equal', () => {
  const wide = svg.radarSilhouetteDigest({ axes: [
    { label: 'scalar', champ: 0.5, chal: 0.6, chalBand: { lo: 0.40, hi: 0.80 } },
  ] });
  const wide2 = svg.radarSilhouetteDigest({ axes: [
    { label: 'scalar', champ: 0.5003, chal: 0.6002, chalBand: { lo: 0.4004, hi: 0.7997 } },
  ] });
  assertEqual(wide, wide2, 'a no-op beat (sub-3dp jitter) → identical radar digest');
  const tight = svg.radarSilhouetteDigest({ axes: [
    { label: 'scalar', champ: 0.5, chal: 0.6, chalBand: { lo: 0.52, hi: 0.68 } },
  ] });
  assert(wide !== tight, 'a CI band tightening flips the radar digest (repaints)');
});

// ── 7. end-to-end through gatePanel: the rating mounts ABOVE the rule ladder ──
test('gatePanel: the rating block renders ABOVE the deterministic rule ladder', () => {
  const host = mountInto(candidate.gatePanel({
    decision: 'deferred', delta_scalar: 1.2,
    rules: [{ id: 'scalar_margin', label: 'scalar margin', status: 'not_reached', detail: '', fired: false }],
    rating: credibleDeferred(),
  }));
  const card = host.firstChild;
  const kids = card.childNodes.filter((n) => n.getAttribute);
  const ratingIdx = kids.findIndex((n) => hasClass(n, 'dn-bt-rating'));
  const rulesIdx = kids.findIndex((n) => hasClass(n, 'dn-rules'));
  assert(ratingIdx >= 0, 'the rating block is in the gate panel');
  assert(rulesIdx >= 0, 'the rule ladder is in the gate panel');
  assert(ratingIdx < rulesIdx, 'the BT pre-gate renders ABOVE the rule ladder (resolves the winner first)');
});

test('gatePanel: two builds of the SAME rating gate read identically (digest-stable)', () => {
  const gate = { decision: 'deferred', rules: [], rating: credibleDeferred() };
  const h1 = mountInto(candidate.gatePanel(gate));
  const h2 = mountInto(candidate.gatePanel(gate));
  assertEqual(h1.textContent.replace(/\s+/g, ' '), h2.textContent.replace(/\s+/g, ' '), 'two builds of the same rating gate read identically (no-op beat is byte-stable)');
});

// ── 8. structure.js — the field-level DEFERRED caption + statusPill state ─────
// A LIVE structure whose standings are all still in contention (no committed
// champion / eliminated) must read its deferred state intentionally — the winner
// resolves once the duels separate the strengths — not blank. The caption is a
// pure function of `live` + the standings' status set (both already in
// structureDigest), so it is digest-stable on a no-op beat.
function deferredStandings(live) {
  return {
    structure: 'gauntlet', structure_params: {}, competitors: [], rounds: [],
    field_status: [], source: 'active', live: !!live,
    standings: [
      { generation_id: 'v3', rank: 1, scalar: 44.0, wins: 0, losses: 0, status: 'competing' },
      { generation_id: 'v4', rank: 2, scalar: 46.0, wins: 0, losses: 0, status: 'competing' },
    ],
  };
}

test('renderStructure: a LIVE all-in-contention field carries the field-level deferred caption', () => {
  const host = mountNodes(structure.renderStructure(deferredStandings(true), CTX, 'e1'));
  assert(allByClass(host, 'dt-standings-deferred').length === 1, 'the deferred caption is shown');
  assert(host.textContent.toLowerCase().includes('no winner committed'), 'reads "no winner committed yet"');
  assert(host.textContent.toLowerCase().includes('held, not rejected'), 'frames it as held, not rejected');
  // the standings statusPill state for an in-contention row maps to the deferred
  // verdict pill (not promoted / not rejected).
  const pills = allByClass(host, 'dn-deferred');
  assert(pills.length >= 1, 'in-contention rows wear the deferred status pill');
});

test('renderStructure: a SETTLED field (a committed champion) does NOT show the deferred caption', () => {
  const st = deferredStandings(false);          // not live
  st.standings[0].status = 'champion';          // crowned
  st.standings[1].status = 'eliminated';
  const host = mountNodes(structure.renderStructure(st, CTX, 'e1'));
  assertEqual(allByClass(host, 'dt-standings-deferred').length, 0, 'no deferred caption once a verdict committed (byte-identical to today)');
});

test('renderStructure: a live field with a committed terminal verdict suppresses the deferred caption', () => {
  const st = deferredStandings(true);
  st.standings[0].status = 'eliminated';        // a terminal verdict landed mid-run
  const host = mountNodes(structure.renderStructure(st, CTX, 'e1'));
  assertEqual(allByClass(host, 'dt-standings-deferred').length, 0, 'a landed terminal verdict suppresses the deferred caption');
});

test('structureDigest: the deferred-caption inputs (live + statuses) are digest-stable on a no-op beat', () => {
  const a = structure.structureDigest(deferredStandings(true));
  const b = structure.structureDigest(deferredStandings(true));
  assertEqual(a, b, 'two identical live deferred beats → byte-identical structure digest (no flash)');
  // a status committing (competing → champion) flips the digest → repaints.
  const settled = deferredStandings(true);
  settled.standings[0].status = 'champion';
  assert(structure.structureDigest(settled) !== a, 'a status committing flips the structure digest');
});

await run();

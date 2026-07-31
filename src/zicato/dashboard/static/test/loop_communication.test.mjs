// test/loop_communication.test.mjs — LOOP COMMUNICATION surfaces (WS4-A item 1).
//
// The reads: /api/epoch/{id}/trajectory (promotion rate + the UNCERTAINTY-
// HONEST verdict + the measured A/A noise floor) and /api/epoch/{id}/cost
// (cost_per_promotion_ms). The renders: the home fleet-card loop stats +
// verdict chip, the epoch view's trajectory + cost panels, and the sparkline's
// measured-noise band.
//
// Pins:
//   * loopVerdict: "no_signal" renders the EXACT honest phrase "no detectable
//     signal (below noise floor)" — never a confident "plateaued"; "improving"
//     renders NO chip (the calm default);
//   * promotionRateLabel / costPerPromotionLabel null-degrade (absent endpoint
//     on the Rust supervisor → the stats are simply omitted);
//   * sparkline({noiseBand}) draws ONE dn-spark-noise rect; absent floor → none
//     (byte-identical to today);
//   * buildTrajectoryPanel / buildCostPanel: null on an absent/empty read; the
//     no-signal caption carries the honest phrase; the cost table carries one
//     row per matchup;
//   * the panel digests: a no-op beat is byte-identical, a real move flips.

import { installDom, test, run, assert, assertEqual, assertDeep } from './harness.mjs';

installDom();

const home = await import('../js/views/home.js');
const epoch = await import('../js/views/epoch.js');
const svg = await import('../js/svg.js');

function classOf(node) { return (node && node.getAttribute && node.getAttribute('class')) || ''; }
function hasClass(node, cls) { return classOf(node).split(/\s+/).includes(cls); }
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => hasClass(n, cls));
}
function mountInto(node) { const h = document.createElement('div'); if (node) h.appendChild(node); return h; }

// ── realistic fixtures (build_optimization_trajectory / build_tournament_cost
// shapes, keys verbatim) ─────────────────────────────────────────────────────
function trajFixture(overrides) {
  return Object.assign({
    epoch_id: 'e0',
    points: [
      { generation_id: 'v0', scalar: 3.6, namespace_values: {} },
      { generation_id: 'v1', scalar: 2.4, namespace_values: {} },
      { generation_id: 'v3', scalar: 1.2, namespace_values: {} },
    ],
    promotion_rate: 2 / 3, promoted_count: 2, challenger_count: 3,
    plateaued: false, verdict: 'improving', recent_movement: 1.2,
    noise_floor: null,
  }, overrides || {});
}
function costFixture(overrides) {
  return Object.assign({
    epoch_id: 'e0',
    per_matchup: [
      { challenger_generation_id: 'v1', decision: 'promoted', runtime_ms: 61000, run_count: 5, aborted_count: 0 },
      { challenger_generation_id: 'v2', decision: 'rejected', runtime_ms: 58000, run_count: 5, aborted_count: 1 },
      { challenger_generation_id: 'v3', decision: 'promoted', runtime_ms: 66000, run_count: 5, aborted_count: 0 },
    ],
    total_runtime_ms: 185000, total_run_count: 15, total_aborted_count: 1,
    promoted_count: 2, cost_per_promotion_ms: 92500,
  }, overrides || {});
}

// ── 1. the verdict chip: uncertainty-honest wording ─────────────────────────
test('loopVerdict: no_signal renders the exact honest phrase, plateaued reads plateaued, improving reads NOTHING', () => {
  const noSig = home.loopVerdict(trajFixture({ verdict: 'no_signal', plateaued: true }));
  assert(noSig, 'a no_signal verdict earns a chip');
  assertEqual(noSig.word, 'no detectable signal (below noise floor)',
    'the below-floor phrase is verbatim — never a confident "plateaued"');
  assertEqual(noSig.cls, 'nosignal', 'no_signal chips wear the faint nosignal class');

  const plat = home.loopVerdict(trajFixture({ verdict: 'plateaued', plateaued: true }));
  assertEqual(plat.word, 'plateaued', 'an above-floor plateau reads plateaued');
  assertEqual(plat.cls, 'plateau', 'plateau chips wear the caution class');

  assertEqual(home.loopVerdict(trajFixture()), null, 'improving = the calm default, NO chip');
  assertEqual(home.loopVerdict(null), null, 'a null read (Rust supervisor) → no chip');
  assertEqual(home.loopVerdict({}), null, 'a degraded read (verdict null) → no chip');
});

// ── 1b. issue #129: the STALLED verdict — challengers fielded, none promoted,
// no A/A floor measured. This case used to fall through to "improving" on the
// epoch view and to NOTHING on the fleet card, so the two surfaces disagreed
// about the worst regime the loop has. Both must now say the same word.
test('stalled: both surfaces render it, and neither surface can call a 0-promotion run improving', () => {
  const stalled = trajFixture({
    points: [{ generation_id: 'v0', scalar: 3.6, namespace_values: {} }],
    promotion_rate: 0, promoted_count: 0, challenger_count: 6,
    plateaued: false, plateau_measurable: false, verdict: 'stalled',
    recent_movement: null, noise_floor: null,
  });

  const chip = home.loopVerdict(stalled);
  assert(chip, 'a stalled loop earns a chip on the fleet card — it used to earn none');
  assertEqual(chip.word, 'stalled (no promotions)', 'the phrase names the promotions that did not happen');
  assertEqual(chip.cls, 'stalled', 'stalled chips wear their own class');

  // The epoch panel's verdictLine layers "improving" on top of loopVerdict, so
  // the pin that matters is that it reaches for loopVerdict FIRST.
  const panel = epoch.buildTrajectoryPanel(stalled, {});
  const host = mountInto(panel);
  const verdictChips = allByClass(host, 'dn-looptraj-verdict');
  assertEqual(verdictChips.length, 1, 'the epoch panel prints exactly one verdict chip');
  assertEqual(verdictChips[0].textContent, 'stalled (no promotions)',
    'the epoch view says the same word as the fleet card — never a green "improving"');
  assert(hasClass(verdictChips[0], 'dn-chip-stalled'), 'and wears the stalled class, not dn-chip-open');
});

// ── 1c. warming_up: nothing has settled yet. Quiet on BOTH surfaces — there is
// no advance to praise and no stall to report, and inventing either would be
// the same defect in the opposite direction.
test('warming_up: no chip anywhere — an undecided loop is not an improving one', () => {
  const fresh = trajFixture({
    points: [{ generation_id: 'v0', scalar: 3.6, namespace_values: {} }],
    promotion_rate: null, promoted_count: 0, challenger_count: 0,
    plateaued: false, plateau_measurable: false, verdict: 'warming_up',
    recent_movement: null, noise_floor: null,
  });
  assertEqual(home.loopVerdict(fresh), null, 'the fleet card stays quiet');
  const host = mountInto(epoch.buildTrajectoryPanel(fresh, {}));
  assertEqual(allByClass(host, 'dn-looptraj-verdict').length, 0,
    'and so does the epoch panel — no verdict word is honest yet');
});

// ── 1d. issue #129: the fleet-card hero placeholder. "no trajectory yet" after
// twenty settled rounds reads as a loop that never started; the honest reading
// of a short spine with rejected challengers is a champion that held.
test('heroPlaceholderText: a retained champion is reported, not called "no trajectory yet"', () => {
  assertEqual(home.heroPlaceholderText(trajFixture({ promoted_count: 0, challenger_count: 20 })),
    'champion retained · 20 rounds', 'rejections are history, not absence');
  assertEqual(home.heroPlaceholderText(trajFixture({ promoted_count: 0, challenger_count: 1 })),
    'champion retained · 1 round', 'singular round reads singular');
  assertEqual(home.heroPlaceholderText(trajFixture({ promoted_count: 2, challenger_count: 3 })),
    '3 rounds · 2 promoted', 'a promoted-but-unplottable epoch reports its promotions');
  assertEqual(home.heroPlaceholderText(trajFixture({ promoted_count: 0, challenger_count: 0 })),
    'no trajectory yet', 'before any challenger settles, the honest word IS "yet"');
  assertEqual(home.heroPlaceholderText(null), 'no trajectory yet',
    'a null read (Rust supervisor) keeps the original placeholder');

  // Settled rounds, not fielded ones: an in-flight challenger has retained
  // nothing yet, so counting it would report a round the loop has not
  // finished — the same over-claim the verdict ladder avoids one field over.
  assertEqual(home.heroPlaceholderText(trajFixture({ promoted_count: 0, challenger_count: 1, settled_count: 0 })),
    'no trajectory yet', 'the first challenger, still racing, is not a retained round');
  assertEqual(home.heroPlaceholderText(trajFixture({ promoted_count: 0, challenger_count: 4, settled_count: 3 })),
    'champion retained · 3 rounds', 'the racer is excluded; the three decided rounds are reported');
});

// ── 2. the fleet-card stat labels null-degrade ──────────────────────────────
test('promotionRateLabel / costPerPromotionLabel: real values format, absent reads null', () => {
  assertEqual(home.promotionRateLabel(trajFixture()), '2/3 · 67%', 'promoted/challengers · percent');
  assertEqual(home.promotionRateLabel(null), null, 'null read → omitted');
  assertEqual(home.promotionRateLabel(trajFixture({ promotion_rate: null, challenger_count: 0 })),
    null, 'no challengers yet → omitted');

  assertEqual(home.costPerPromotionLabel(costFixture()), '1.5m', 'ms → compact human duration');
  assertEqual(home.costPerPromotionLabel(costFixture({ cost_per_promotion_ms: null })),
    null, 'nothing promoted → omitted (never a fabricated 0)');
  assertEqual(home.costPerPromotionLabel(null), null, 'null read → omitted');
});

test('fmtDurationMs: ms → s → m → h buckets', () => {
  assertEqual(home.fmtDurationMs(850), '850ms', 'sub-second stays ms');
  assertEqual(home.fmtDurationMs(12300), '12.3s', 'seconds to one decimal');
  assertEqual(home.fmtDurationMs(250000), '4.2m', 'minutes past 90s');
  assertEqual(home.fmtDurationMs(2 * 60 * 60 * 1000), '2h', 'hours past 90m');
});

// ── 3. the noise band: spec + the sparkline rect ─────────────────────────────
test('noiseBandFor: measured floor centres on the LAST scalar; absent floor → null', () => {
  const traj = trajFixture({ noise_floor: { max_abs_delta: 0.2, delta_std: 0.1, runs: 3 } });
  assertDeep(home.noiseBandFor(traj, [3.6, 2.4, 1.2]), { center: 1.2, half: 0.2 },
    'band = last scalar ± max_abs_delta');
  assertEqual(home.noiseBandFor(trajFixture(), [3.6, 1.2]), null, 'no floor measured → no band');
  assertEqual(home.noiseBandFor(traj, []), null, 'no plotted scalar → no band');
});

test('sparkline({noiseBand}) draws ONE dn-spark-noise rect; omitting it draws none', () => {
  const withBand = mountInto(svg.sparkline({
    width: 240, height: 46, values: [3.6, 2.4, 1.2], noiseBand: { center: 1.2, half: 0.2 },
  }));
  assertEqual(allByClass(withBand, 'dn-spark-noise').length, 1, 'the measured-noise band renders');
  const without = mountInto(svg.sparkline({ width: 240, height: 46, values: [3.6, 2.4, 1.2] }));
  assertEqual(allByClass(without, 'dn-spark-noise').length, 0,
    'no floor → byte-identical to today (no band)');
});

// ── 4. the epoch trajectory panel ────────────────────────────────────────────
test('buildTrajectoryPanel: absent / empty reads → null (the view is byte-identical to today)', () => {
  assertEqual(epoch.buildTrajectoryPanel(null), null, 'null read (Rust supervisor) → no panel');
  assertEqual(epoch.buildTrajectoryPanel(undefined), null, 'absent → no panel');
  assertEqual(epoch.buildTrajectoryPanel({ points: [], promotion_rate: null }), null,
    'a degraded (never-indexed) read → no panel');
});

test('buildTrajectoryPanel: renders the rate + verdict chip + noise band + the honest no-signal caption', () => {
  const traj = trajFixture({
    verdict: 'no_signal', plateaued: true, recent_movement: 0.1,
    noise_floor: { max_abs_delta: 0.2, delta_std: 0.1, runs: 3 },
  });
  const host = mountInto(epoch.buildTrajectoryPanel(traj));
  const chips = allByClass(host, 'dn-looptraj-verdict');
  assertEqual(chips.length, 1, 'one verdict chip');
  assertEqual(chips[0].textContent, 'no detectable signal (below noise floor)',
    'the chip carries the exact honest phrase');
  assert(hasClass(chips[0], 'dn-chip-nosignal'), 'no_signal wears the faint class');
  assertEqual(allByClass(host, 'dn-spark-noise').length, 1, 'the floor renders as a sparkline band');
  assert(host.textContent.includes('2/3 · 67%'), 'the promotion rate renders');
  assert(host.textContent.includes('no detectable signal (below noise floor)'),
    'the caption repeats the honest phrase');
});

test('trajectoryPanelDigest: a no-op beat is byte-identical; a verdict/floor move flips it', () => {
  const a = JSON.stringify(epoch.trajectoryPanelDigest(trajFixture()));
  const b = JSON.stringify(epoch.trajectoryPanelDigest(trajFixture()));
  assertEqual(a, b, 'identical reads fold to identical digests (no-op beat = zero DOM)');
  const moved = JSON.stringify(epoch.trajectoryPanelDigest(trajFixture({ verdict: 'no_signal' })));
  assert(moved !== a, 'a verdict change flips the digest');
  const floored = JSON.stringify(epoch.trajectoryPanelDigest(
    trajFixture({ noise_floor: { max_abs_delta: 0.2 } })));
  assert(floored !== a, 'a newly-measured floor flips the digest');
});

// ── 5. the epoch cost panel ──────────────────────────────────────────────────
test('buildCostPanel: absent / empty reads → null; a real read renders totals + one row per matchup', () => {
  assertEqual(epoch.buildCostPanel(null), null, 'null read → no panel');
  assertEqual(epoch.buildCostPanel({ per_matchup: [], total_run_count: 0 }), null,
    'a no-runs epoch → no panel');

  const host = mountInto(epoch.buildCostPanel(costFixture()));
  assertEqual(allByClass(host, 'dn-loopcost-row').length, 3, 'one table row per matchup');
  assert(host.textContent.includes('1.5m'), 'cost/promotion renders as a compact duration');
  assert(host.textContent.includes('promoted'), 'per-matchup decisions render');
});

test('buildCostPanel: cost/promotion reads an honest — when nothing was promoted', () => {
  const host = mountInto(epoch.buildCostPanel(costFixture({ cost_per_promotion_ms: null, promoted_count: 0 })));
  assert(host.textContent.includes('—'), 'null cost_per_promotion_ms renders an em-dash, never 0');
});

test('costPanelDigest: no-op beat byte-identical; a landed run flips it', () => {
  const a = JSON.stringify(epoch.costPanelDigest(costFixture()));
  const b = JSON.stringify(epoch.costPanelDigest(costFixture()));
  assertEqual(a, b, 'identical reads → identical digests');
  const moved = JSON.stringify(epoch.costPanelDigest(costFixture({ total_run_count: 16 })));
  assert(moved !== a, 'a new run flips the digest');
});

// ── 6. the fleet-card digest fold ────────────────────────────────────────────
test('loopStatsDigest: rounded + timestamp-free; null reads fold to nulls (stable)', () => {
  const a = JSON.stringify(home.loopStatsDigest(trajFixture(), costFixture()));
  const b = JSON.stringify(home.loopStatsDigest(trajFixture(), costFixture()));
  assertEqual(a, b, 'no-op fold is byte-identical');
  const nulls = JSON.stringify(home.loopStatsDigest(null, null));
  assertEqual(nulls, JSON.stringify([null, null, null, null, null, null]),
    'absent reads fold to a stable all-null tuple');
  const moved = JSON.stringify(home.loopStatsDigest(
    trajFixture({ verdict: 'plateaued' }), costFixture()));
  assert(moved !== a, 'a verdict move flips the fold');
});

run();

// test/prediction_calibration.test.mjs — PROPOSER PREDICTION-ACCURACY +
// CALIBRATION (the diagnostic surfaces).
//
// Two orthogonal-to-the-gate reads:
//   * candidate.buildPredictionScorecard(scorecard) — the dossier card consuming
//     /api/hypothesis-accuracy/{epoch}/{gen}: predicted-vs-realised movements per
//     claim with hit/miss/band/unpredicted glyphs + the calibration fraction.
//   * svg.calibrationTrend(opts) — the home meta-loop ledger figure consuming
//     /api/calibration-trend: the score fraction over the epoch's lineage,
//     reusing the sparkline/staircase grammar.
//
// Pins (all keys read VERBATIM from build_hypothesis_accuracy /
// build_calibration_trend):
//   * absent / no-claims scorecard → null → the dossier is byte-identical to
//     today (back-compat clean); a baseline (seed) never reads the endpoint;
//   * each claim's stamped hypothesis_match drives the hit/miss glyph; a
//     predicted-but-unpaired claim is "unresolved" (◌); an unpredicted realised
//     movement is "＋" and NEVER scored;
//   * brier is always null → no Brier value rendered;
//   * the card carries the EXPLICIT 'diagnostic — does not affect the gate'
//     caption;
//   * scorecardDigest: a no-op beat is byte-identical (rounded, NO timestamps)
//     while a movement landing / the fraction moving flips it (the bug class);
//   * calibrationTrend: a null score_fraction generation is a hollow tick (not a
//     drop to zero); the end dot earns good/bad by trend_sign;
//   * calibrationTrendDigest: a no-op beat is byte-identical while a new scored
//     generation flips it;
//   * the HOME view mounts the trend beside the ledger, captions it diagnostic,
//     and a no-op heartbeat churns NO DOM (digest-gated).

import { installDom, test, run, assert, assertEqual, assertDeep } from './harness.mjs';

installDom();

const candidate = await import('../js/views/candidate.js');
const svg = await import('../js/svg.js');
const home = await import('../js/views/home.js');
const data = await import('../js/data.js');
const router = await import('../js/router.js');
const hovercard = await import('../js/hovercard.js');

function classOf(node) { return (node && node.getAttribute && node.getAttribute('class')) || ''; }
function hasClass(node, cls) { return classOf(node).split(/\s+/).includes(cls); }
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => hasClass(n, cls));
}
function mountInto(node) { const h = document.createElement('div'); if (node) h.appendChild(node); return h; }
function hovercardTextOf(node) {
  hovercard.hide();
  node.dispatchEvent({ type: 'mouseenter', target: node });
  const text = hovercard.cardText();
  node.dispatchEvent({ type: 'mouseleave', target: node });
  return text;
}

// ── a realistic hypothesis-accuracy scorecard: two predicted movements (one hit,
// one miss), one predicted-but-unresolved, plus one unpredicted realised mover.
function scorecardFixture() {
  return {
    epoch_id: 'e1', generation_id: 'v5',
    claims: [
      { target: 'no_fabricated_numbers', kind: 'metric', predicted_direction: 'up',
        predicted_magnitude: 'small', from_rate: 0.60, to_rate: 0.78,
        observed_direction: 'up', signed_error: 0.18, hypothesis_match: true,
        unpredicted: false, note: 'landed' },
      { target: 'tone_consistency', kind: 'drift', predicted_direction: 'down',
        predicted_magnitude: 'moderate', from_rate: 0.40, to_rate: 0.55,
        observed_direction: 'up', signed_error: 0.15, hypothesis_match: false,
        unpredicted: false, note: 'went the other way' },
      { target: 'citation_density', kind: 'metric', predicted_direction: 'up',
        predicted_magnitude: 'small', from_rate: null, to_rate: null,
        observed_direction: 'flat', signed_error: null, hypothesis_match: null,
        unpredicted: false, note: '' },
      { target: 'verbosity', kind: 'drift', predicted_direction: null,
        predicted_magnitude: null, from_rate: 0.30, to_rate: 0.50,
        observed_direction: 'up', signed_error: 0.20, hypothesis_match: false,
        unpredicted: true, note: 'nobody claimed this' },
    ],
    score: { hits: 1, total: 3, fraction: 1 / 3, brier: null },
    pass_rate: { predicted: '+0.05 board-wide', observed: 0.04 },
  };
}

// ── 1. back-compat: absent / no-claims → NOTHING ────────────────────────────
test('buildPredictionScorecard: absent / empty / no-claims → null (byte-identical to today)', () => {
  assertEqual(candidate.buildPredictionScorecard(undefined), null, 'absent → null');
  assertEqual(candidate.buildPredictionScorecard(null), null, 'null → null');
  assertEqual(candidate.buildPredictionScorecard({}), null, 'no claims key → null');
  assertEqual(candidate.buildPredictionScorecard({ claims: [] }), null, 'empty claims → null');
});

// ── 2. the calibration fraction headline + the per-movement glyphs ──────────
test('buildPredictionScorecard: renders the calibration fraction + a hit/miss/unresolved/unpredicted glyph per claim', () => {
  const host = mountInto(candidate.buildPredictionScorecard(scorecardFixture()));
  // the fraction headline: 1/3 ≈ 33%.
  const frac = allByClass(host, 'dn-predcard-frac')[0];
  assert(frac, 'the calibration-fraction headline renders');
  assert(frac.textContent.includes('1/3'), 'shows hits/total (1/3)');
  assert(frac.textContent.includes('33%'), 'shows the rounded fraction (33%)');
  // 1 hit of 3 predicted → below half → the bad tone (direction-earned, no hue).
  assert(hasClass(frac, 'dn-bad'), 'a < 50% calibration reads in the bad tone');

  // one glyph per claim row (4 rows: hit, miss, unresolved, unpredicted).
  const glyphs = allByClass(host, 'dn-pred-glyph');
  assertEqual(glyphs.length, 4, 'a verdict glyph per claim');
  const tones = glyphs.map((g) => (hasClass(g, 'dn-good') ? 'good' : hasClass(g, 'dn-bad') ? 'bad' : 'flat'));
  assertDeep(tones, ['good', 'bad', 'flat', 'flat'], 'hit=good · miss=bad · unresolved=flat · unpredicted=flat');
  assertEqual(glyphs[0].textContent, '✓', 'hit glyph is ✓');
  assertEqual(glyphs[1].textContent, '✗', 'miss glyph is ✗');
  assertEqual(glyphs[2].textContent, '◌', 'unresolved glyph is ◌');
  assertEqual(glyphs[3].textContent, '＋', 'unpredicted glyph is ＋');
});

// ── 3. the EXPLICIT diagnostic caption (the non-negotiable disclaimer) ──────
test('buildPredictionScorecard: carries the explicit "diagnostic — does not affect the gate" caption', () => {
  const host = mountInto(candidate.buildPredictionScorecard(scorecardFixture()));
  const cap = allByClass(host, 'dn-predcard-cap')[0];
  assert(cap, 'the diagnostic caption renders');
  assert(cap.textContent.includes('diagnostic — does not affect the gate'),
    'the caption states it does not couple to the gate, verbatim');
});

// ── 4. hover detail lives in the hovercard (OUTSIDE the gated render) ────────
test('buildPredictionScorecard: a glyph carries its signed-error + note in the hovercard singleton', () => {
  const host = mountInto(candidate.buildPredictionScorecard(scorecardFixture()));
  const glyphs = allByClass(host, 'dn-pred-glyph');
  const t = hovercardTextOf(glyphs[0]);
  assert(t.includes('no_fabricated_numbers'), 'the hovercard names the movement target');
  assert(t.includes('hit'), 'the hovercard names the verdict');
  assert(t.includes('signed error'), 'the hovercard carries the signed error');
  // the unpredicted row reads as "did not claim".
  const tu = hovercardTextOf(glyphs[3]);
  assert(tu.includes('did not claim'), 'the unpredicted hovercard flags the proposer never claimed it');
});

// ── 5. scorecardDigest stability (the SSE-heartbeat flashing bug class) ──────
test('buildPredictionScorecard digest: a no-op beat is byte-identical; a movement landing / fraction move flips it', () => {
  // The candidate digest folds the scorecard via the internal scorecardDigest;
  // we drive it through the dossier digest by rendering the SAME card twice.
  // The card builder itself is deterministic, so identical input → identical DOM.
  const a = mountInto(candidate.buildPredictionScorecard(scorecardFixture()));
  const b = mountInto(candidate.buildPredictionScorecard(scorecardFixture()));
  assertEqual(a.textContent, b.textContent, 'identical scorecards render identical text (deterministic)');

  // a movement LANDING (the unresolved claim now resolves to a hit) MUST change
  // the rendered content (a real datum changed → repaint).
  const landed = scorecardFixture();
  landed.claims[2].from_rate = 0.20; landed.claims[2].to_rate = 0.40;
  landed.claims[2].observed_direction = 'up'; landed.claims[2].hypothesis_match = true;
  landed.score = { hits: 2, total: 3, fraction: 2 / 3, brier: null };
  const c = mountInto(candidate.buildPredictionScorecard(landed));
  assert(a.textContent !== c.textContent, 'a movement landing changes the rendered content');
  const fracC = allByClass(c, 'dn-predcard-frac')[0];
  assert(fracC.textContent.includes('67%'), 'the fraction repaints to 67% (2/3)');
  assert(hasClass(fracC, 'dn-good'), 'a ≥ 50% calibration now reads in the good tone');
});

// ── 6. NO Brier value is rendered (brier is always null) ────────────────────
test('buildPredictionScorecard: never renders a Brier value (the contract is always null)', () => {
  const host = mountInto(candidate.buildPredictionScorecard(scorecardFixture()));
  assert(!host.textContent.toLowerCase().includes('brier'), 'no Brier value is surfaced');
});

// ── 7. svg.calibrationTrend — the staircase grammar + degradation ───────────
function trendFixture() {
  return {
    epoch_id: 'e1',
    points: [
      { generation_id: 'v1', score_fraction: 0.50, total_claims: 4, decision: 'rejected' },
      { generation_id: 'v2', score_fraction: null, total_claims: 0, decision: 'rejected' },
      { generation_id: 'v3', score_fraction: 0.66, total_claims: 3, decision: 'promoted' },
      { generation_id: 'v4', score_fraction: 0.80, total_claims: 5, decision: 'promoted' },
    ],
    rolling_mean: 0.6533, n_scored: 3, latest_fraction: 0.80, trend_sign: 1,
  };
}

test('calibrationTrend: 0 points → an honest placeholder; a point set → a dot per scored gen + a hollow no-claim tick', () => {
  const empty = svg.calibrationTrend({ points: [] });
  assert(String(empty.textContent).includes('no scored predictions yet'), 'empty → honest placeholder');

  const fig = svg.calibrationTrend(trendFixture());
  const host = mountInto(fig);
  // 3 scored gens → 3 solid dots; 1 no-claim gen → 1 hollow tick.
  assertEqual(allByClass(host, 'dn-caltrend-dot').length, 3, 'a solid dot per scored generation');
  assertEqual(allByClass(host, 'dn-caltrend-noclaim').length, 1, 'a hollow tick for the no-claim generation');
  // the rolling-mean dashed reference + the 0.5 midline both render.
  assertEqual(allByClass(host, 'dn-caltrend-mean').length, 1, 'the rolling-mean reference renders');
  assertEqual(allByClass(host, 'dn-caltrend-mid').length, 1, 'the 0.5 midline renders');
});

test('calibrationTrend: the END dot earns good/bad by trend_sign (improving → good)', () => {
  const up = mountInto(svg.calibrationTrend(trendFixture()));
  const upDots = allByClass(up, 'dn-caltrend-dot');
  assert(hasClass(upDots[upDots.length - 1], 'dn-good'), 'an improving trend ends on a good dot');

  const down = trendFixture(); down.trend_sign = -1;
  const dn = mountInto(svg.calibrationTrend(down));
  const dnDots = allByClass(dn, 'dn-caltrend-dot');
  assert(hasClass(dnDots[dnDots.length - 1], 'dn-bad'), 'a regressing trend ends on a bad dot');

  const flat = trendFixture(); flat.trend_sign = 0;
  const fl = mountInto(svg.calibrationTrend(flat));
  const flDots = allByClass(fl, 'dn-caltrend-dot');
  const last = flDots[flDots.length - 1];
  assert(!hasClass(last, 'dn-good') && !hasClass(last, 'dn-bad'), 'a flat trend ends neutral (no tone)');
});

test('calibrationTrend: a no-claim generation reads its lack of claim in the hovercard (no faked drop)', () => {
  const host = mountInto(svg.calibrationTrend(trendFixture()));
  const tick = allByClass(host, 'dn-caltrend-noclaim')[0];
  const t = hovercardTextOf(tick);
  assert(t.includes('v2'), 'the hollow tick names its generation');
  assert(t.includes('no falsifiable claim'), 'the hollow tick reads as "no falsifiable claim" (not a 0% score)');
});

// ── 8. calibrationTrendDigest stability (the bug class) ─────────────────────
test('calibrationTrendDigest: a no-op beat is byte-identical; a new scored generation flips it', () => {
  const d1 = svg.calibrationTrendDigest(trendFixture());
  const d2 = svg.calibrationTrendDigest(trendFixture());
  assertEqual(d1, d2, 'identical trends digest byte-identically (rounded, no timestamps)');

  // a new scored generation lands → the digest MUST flip (repaint).
  const grew = trendFixture();
  grew.points.push({ generation_id: 'v5', score_fraction: 0.90, total_claims: 2, decision: 'promoted' });
  grew.rolling_mean = 0.715; grew.latest_fraction = 0.90;
  assert(svg.calibrationTrendDigest(grew) !== d1, 'a new scored generation flips the digest');

  // a NO-OP fraction jitter below the rounding precision must NOT flip it.
  const jitter = trendFixture();
  jitter.points[0].score_fraction = 0.5000004; // rounds to 0.500 → no change.
  assertEqual(svg.calibrationTrendDigest(jitter), d1, 'sub-precision jitter does not flip the digest');
});

// ── 9. the HOME view mounts the trend beside the ledger + digest-gates it ────

const WS = {
  current_epoch_id: 'e1',
  epochs: [
    { epoch_id: 'e0', generation_count: 5, promoted_count: 1, best_scalar: 42.1, closed: true, goal: 'baseline' },
    { epoch_id: 'e1', generation_count: 6, promoted_count: 1, best_scalar: 34.2, closed: false, goal: 'tighten' },
  ],
  ledger: [
    { epoch_id: 'e0', floor: 42.1, champion_gen: 'v4', champion_index: 4, generation_count: 5, structure: 'racing', closed: true, open: false,
      changed_components: { board: false, brief: false, scoring: false, entrypoint: false, mutable_trees: false, structure: false, proposer: false }, changed_list: [], soft: false },
    { epoch_id: 'e1', floor: 34.2, champion_gen: 'v7', champion_index: 5, generation_count: 6, structure: 'swiss', closed: false, open: true,
      changed_components: { board: true, brief: false, scoring: false, entrypoint: false, mutable_trees: false, structure: false, proposer: false }, changed_list: ['board'], soft: false },
  ],
};

function installFetch(map) {
  globalThis.fetch = async (path) => {
    const q = path.indexOf('?');
    const base = q >= 0 ? path.slice(0, q) : path;
    const v = Object.prototype.hasOwnProperty.call(map, path) ? map[path]
      : Object.prototype.hasOwnProperty.call(map, base) ? map[base] : undefined;
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}

test('home view: mounts the calibration trend beside the ledger + captions it diagnostic', async () => {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  installFetch({
    '/api/workspace': WS,
    '/api/health-report': { epoch_id: 'e1', healthy: true, findings: [] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 41 }, { generation_id: 'v1', scalar: 40 }] },
    '/api/calibration-trend': trendFixture(),
  });
  const host = document.createElement('div');
  await home.render(host, { navigate() {}, href: router.href }, {});
  assert(host.textContent.includes('Calibration trend'), 'the calibration-trend section is titled');
  assertEqual(allByClass(host, 'dn-caltrend').length, 1, 'the trend figure renders once');
  assert(host.textContent.includes('diagnostic — does not affect the gate'),
    'the trend caption states it does not affect the gate, verbatim');
  // it sits AFTER the meta-loop ledger (sibling overview, ledger leads).
  const txt = host.textContent;
  assert(txt.indexOf('Meta-loop ledger') < txt.indexOf('Calibration trend'),
    'the ledger precedes the calibration trend');
});

test('home view: a no-op re-render with the SAME calibration trend churns NO DOM (digest-gated)', async () => {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  installFetch({
    '/api/workspace': WS,
    '/api/health-report': { epoch_id: 'e1', healthy: true, findings: [] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 41 }, { generation_id: 'v1', scalar: 40 }] },
    '/api/calibration-trend': trendFixture(),
  });
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await home.render(host, ctx, {});
  const firstDigest = host.getAttribute('data-t-digest');
  assert(firstDigest, 'the host carries a content digest after first render');
  assert(firstDigest.includes('calib'), 'the calibration trend is folded into the home digest');
  const writesBefore = host.innerHTMLWriteCount();
  data.invalidate(); // bust the cache so the SAME payload is re-fetched
  await home.render(host, ctx, {});
  assertEqual(host.getAttribute('data-t-digest'), firstDigest, 'the digest is unchanged on identical data');
  assertEqual(host.innerHTMLWriteCount(), writesBefore, 'no DOM churn on a no-op re-render');
});

test('home view: a NEW scored generation in the trend flips the home digest (repaints)', async () => {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  installFetch({
    '/api/workspace': WS,
    '/api/health-report': { epoch_id: 'e1', healthy: true, findings: [] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 41 }, { generation_id: 'v1', scalar: 40 }] },
    '/api/calibration-trend': trendFixture(),
  });
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await home.render(host, ctx, {});
  const firstDigest = host.getAttribute('data-t-digest');

  const grew = trendFixture();
  grew.points.push({ generation_id: 'v5', score_fraction: 0.90, total_claims: 2, decision: 'promoted' });
  grew.rolling_mean = 0.715;
  data.invalidate();
  installFetch({
    '/api/workspace': WS,
    '/api/health-report': { epoch_id: 'e1', healthy: true, findings: [] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 41 }, { generation_id: 'v1', scalar: 40 }] },
    '/api/calibration-trend': grew,
  });
  await home.render(host, ctx, {});
  assert(host.getAttribute('data-t-digest') !== firstDigest, 'a new scored generation flips the home digest');
});

// ── 10. the FULL candidate dossier folds the scorecard into the digest ──────
// Proves the scorecard rides the dossier `candidateDigest` (candidate.js): a
// no-op heartbeat over a scorecard-bearing dossier churns NO DOM, while a
// movement landing flips the dossier digest → repaint (the bug class, through
// the real render path).

const DEPOCH = 'e1';
const DGEN = 'v1';
function candidateBackend(scorecard) {
  const F = {
    '/api/epoch': {
      epoch_id: DEPOCH, closed: false, goal: 'tighten',
      experiments: [
        { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } },
        { generation_id: 'v1', parent_generation_id: 'v0', outcome: { decision: 'rejected' } },
      ],
      board: [{ id: 'b1', kind: 'single_turn', expectation_kind: 'predicate', weight: 1 }],
    },
    '/api/lineage': { generations: [
      { generation_id: 'v0', epoch_id: DEPOCH, parent_generation_id: '', promoted: true },
      { generation_id: 'v1', epoch_id: DEPOCH, parent_generation_id: 'v0', promoted: false },
    ] },
    '/api/tournaments': { epoch_id: DEPOCH, champion_lineage: ['v0'], matchups: [
      { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 5.2 },
    ] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 41 }, { generation_id: 'v1', scalar: 46 }] },
  };
  F[`/api/generation/${DEPOCH}/v0/per-entry`] = { entries: [{ entry_id: 'b1', drift_loss: 41, pass_fail: 1 }] };
  F[`/api/generation/${DEPOCH}/v1/per-entry`] = { entries: [{ entry_id: 'b1', drift_loss: 46, pass_fail: 0 }] };
  F[`/api/round/${DEPOCH}/v0/v1/gate`] = { decision: 'rejected', delta_scalar: 5.2, rules: [
    { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', fired: true, detail: '41 → 46' },
  ] };
  F[`/api/hypothesis-accuracy/${DEPOCH}/v1`] = scorecard;
  return F;
}

test('candidate dossier: the prediction scorecard renders + folds into the dossier digest (no-op beat churns NO DOM)', async () => {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  installFetch(candidateBackend(scorecardFixture()));
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: DEPOCH, gen: DGEN });
  // the scorecard section + card rendered on the dossier.
  assert(host.textContent.includes('Prediction accuracy'), 'the prediction-accuracy section is titled on the dossier');
  assertEqual(allByClass(host, 'dn-predcard').length, 1, 'the scorecard card renders once');
  assert(host.textContent.includes('diagnostic — does not affect the gate'), 'the dossier scorecard carries the diagnostic caption');

  const digest1 = host.getAttribute('data-t-digest');
  assert(digest1, 'the dossier carries a content digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  data.invalidate();
  await candidate.render(host, ctx, { epochId: DEPOCH, gen: DGEN });
  assertEqual(host.getAttribute('data-t-digest'), digest1, 'the dossier digest is unchanged on a no-op beat over the scorecard');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op beat');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no DOM churn on the no-op beat (scorecard folded, not churned)');
});

test('candidate dossier: a movement landing in the scorecard flips the dossier digest (repaints)', async () => {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  installFetch(candidateBackend(scorecardFixture()));
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: DEPOCH, gen: DGEN });
  const digest1 = host.getAttribute('data-t-digest');

  const landed = scorecardFixture();
  landed.claims[2].from_rate = 0.20; landed.claims[2].to_rate = 0.40; landed.claims[2].hypothesis_match = true;
  landed.score = { hits: 2, total: 3, fraction: 2 / 3, brier: null };
  data.invalidate();
  installFetch(candidateBackend(landed));
  await candidate.render(host, ctx, { epochId: DEPOCH, gen: DGEN });
  assert(host.getAttribute('data-t-digest') !== digest1, 'a movement landing flips the dossier digest');
});

test('candidate dossier: a candidate with NO hypothesis data drops the scorecard (byte-identical to today)', async () => {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  // the 404 fallback → null scorecard → no panel.
  const F = candidateBackend(scorecardFixture());
  delete F[`/api/hypothesis-accuracy/${DEPOCH}/v1`];
  installFetch(F);
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: DEPOCH, gen: DGEN });
  assert(!host.textContent.includes('Prediction accuracy'), 'no scorecard section when the endpoint is absent');
  assertEqual(allByClass(host, 'dn-predcard').length, 0, 'no scorecard card when there is no hypothesis data');
});

test('home view: a workspace with NO calibration data drops the trend (byte-identical to today)', async () => {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  installFetch({
    '/api/workspace': WS,
    '/api/health-report': { epoch_id: 'e1', healthy: true, findings: [] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 41 }, { generation_id: 'v1', scalar: 40 }] },
    '/api/calibration-trend': { epoch_id: 'e1', points: [], rolling_mean: null, n_scored: 0, latest_fraction: null, trend_sign: 0 },
  });
  const host = document.createElement('div');
  await home.render(host, { navigate() {}, href: router.href }, {});
  assert(!host.textContent.includes('Calibration trend'),
    'an epoch with no scored predictions drops the calibration-trend section');
  assertEqual(allByClass(host, 'dn-caltrend').length, 0, 'no trend figure when nothing is scored');
});

run();

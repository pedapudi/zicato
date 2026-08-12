// test/instrument.test.mjs — the Instrument lens (board reflection · R5).
//
// Covers the three modes (landing / bill of health / x-ray), the null-p_flip
// honesty, the untested-judge greying, the span highlight + fidelity label +
// honest-unavailable transcript, the digest no-op identity guardrail (two
// identical fetches ⇒ zero DOM rebuild), and the router round-trip for the new
// routes. Fixtures are the reflection_view.py reader shapes (see fixtures.mjs).

import { installDom, test, run, assert, assertEqual, assertDeep } from './harness.mjs';

installDom();

const {
  router, tree, shell, data, EPOCH_ID,
  installFixtureMap, freshState, allByClass,
  REFLECTION_ID, REFL_JUDGE, REFL_RUN_REF, REFLECTION_SUMMARY,
  REFLECTION_XRAY, REFLECTION_XRAY_UNAVAILABLE,
  REFLECTION_PRACTICES, reflectionFixtureMap,
} = await import('./fixtures.mjs');

const instrument = await import('../js/views/instrument.js');

const CTX = { navigate() {}, href: router.href };
function fresh() { freshState(); }
function textOf(host) { return host.textContent || ''; }
function hasClass(host, cls) { return allByClass(host, cls).length > 0; }

// ====================================================================
// ROUTER round-trip — the new #/e/<epoch>/instrument[/…] routes.
// ====================================================================
test('router: parseRoute + href round-trip for every instrument depth', () => {
  const cases = [
    { params: { epochId: EPOCH_ID } },
    { params: { epochId: EPOCH_ID, reflectionId: REFLECTION_ID } },
    { params: { epochId: EPOCH_ID, reflectionId: REFLECTION_ID, judge: REFL_JUDGE, runRef: REFL_RUN_REF } },
  ];
  for (const c of cases) {
    const url = router.href('instrument', c.params);
    const parsed = router.parseRoute(url);
    assertEqual(parsed.view, 'instrument', 'view for ' + url);
    assertEqual(parsed.params.epochId, c.params.epochId, 'epochId for ' + url);
    assertEqual(parsed.params.reflectionId || null, c.params.reflectionId || null, 'reflectionId for ' + url);
    assertEqual(parsed.params.judge || null, c.params.judge || null, 'judge for ' + url);
    // the run_ref carries `:` — it must survive the enc/dec round-trip verbatim.
    assertEqual(parsed.params.runRef || null, c.params.runRef || null, 'runRef for ' + url);
  }
});

test('router F9: an id containing ~ round-trips the x-ray route without truncating', () => {
  const weird = 'gen~cmp:task.itinerary:r0'; // a run_ref carrying a literal ~
  const url = router.href('instrument', { epochId: EPOCH_ID, reflectionId: REFLECTION_ID, judge: REFL_JUDGE, runRef: weird });
  const parsed = router.parseRoute(url);
  assertEqual(parsed.view, 'instrument', 'the view still resolves');
  assertEqual(parsed.params.runRef, weird, 'the ~-bearing run_ref survives enc/dec verbatim (no split at ~)');
});

test('router: instrument is a registered VIEW; up() climbs x-ray → bill → landing → epoch', () => {
  assert(router.VIEWS.includes('instrument'), 'instrument in VIEWS');
  const xray = { view: 'instrument', params: { epochId: EPOCH_ID, reflectionId: REFLECTION_ID, judge: REFL_JUDGE, runRef: REFL_RUN_REF } };
  const up1 = router.up(xray);
  assertDeep([up1.view, up1.params.reflectionId, up1.params.runRef || null], ['instrument', REFLECTION_ID, null], 'x-ray up → bill');
  const up2 = router.up(up1);
  assertDeep([up2.view, up2.params.reflectionId || null], ['instrument', null], 'bill up → landing');
  const up3 = router.up(up2);
  assertDeep([up3.view, up3.params.epochId], ['epoch', EPOCH_ID], 'landing up → epoch');
});

test('router: crumbTrail names the instrument path at each depth', () => {
  const trail = router.crumbTrail({ view: 'instrument', params: { epochId: EPOCH_ID, reflectionId: REFLECTION_ID, judge: REFL_JUDGE, runRef: REFL_RUN_REF } });
  const labels = trail.map((c) => c.label);
  assert(labels.includes('instrument'), 'has instrument crumb');
  assert(labels.includes(REFLECTION_ID), 'has reflection crumb');
  assert(labels[labels.length - 1].includes(REFL_JUDGE), 'leaf names the judge · run_ref');
  assert(trail[trail.length - 1].current === true, 'leaf is current');
});

// ====================================================================
// LANDING — the reflection list.
// ====================================================================
test('landing: renders the reflection list from the fixture', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID });
  const t = textOf(host);
  assert(hasClass(host, 'dn-instr-list'), 'the list table rendered');
  assert(t.includes(REFLECTION_ID), 'the newest reflection id is listed');
  assert(t.includes('refl-2026-05-29'), 'the older reflection id is listed');
  // the older reflection has decision_flip_p:null ⇒ the flip cell reads n/a.
  assert(t.includes('n/a'), 'a null-flip reflection renders n/a in the flip column');
});

test('landing: an empty reflection set points at `zicato reflect run`', async () => {
  fresh();
  const F = { ...reflectionFixtureMap(), '/api/reflections': { reflections: [] } };
  installFixtureMap(F);
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID });
  const t = textOf(host);
  assert(t.includes('No reflections'), 'honest empty state');
  assert(t.includes('zicato reflect run'), 'points at the CLI entry point');
});

// ====================================================================
// BILL OF HEALTH — the four-pillar quadrant + findings + judge audit.
// ====================================================================
test('bill of health: renders the four pillar cards with reader numbers', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const pillars = allByClass(host, 'dn-instr-pillar');
  assertEqual(pillars.length, 4, 'four pillar cards');
  const t = textOf(host);
  assert(t.includes('Reliability') && t.includes('Discrimination') && t.includes('Validity') && t.includes('Calibration'), 'all four pillar names');
  // reader-owned numbers surface verbatim.
  assert(t.includes('31%'), 'decision-flip P (0.31) rendered as a percent');
  assert(t.includes('0.81'), 'aggregate F1 rendered');
  assert(t.includes('2 / 3'), 'differentiating-entries tally (2 of 3 judgeable)');
  // the calibration margin fails to clear the floor.
  assert(t.includes('margin clears floor'), 'calibration margin-to-noise row');
});

test('bill of health: null p_flip renders the honest "insufficient replication" reason', async () => {
  fresh();
  const nullFlip = JSON.parse(JSON.stringify(REFLECTION_SUMMARY));
  nullFlip.decision_flip_p = null;
  nullFlip.pillars.reliability.decision_flip = { p_flip: null, reason: 'a contributing (candidate, entry) unit has fewer than two replicates', base_decision: null };
  installFixtureMap(reflectionFixtureMap({ summary: nullFlip }));
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const t = textOf(host);
  assert(t.includes('n/a — insufficient replication'), 'honest n/a label for a null p_flip');
  assert(t.includes('fewer than two replicates'), 'the reader reason is surfaced');
  assert(!/\bnull%\b/.test(t) && !t.includes('NaN'), 'never fabricates a percent from null');
});

test('bill of health: findings are DE-TAGGED quiet rows — a tone glyph + word, no severity chip', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  // findings render as the loop-health row grammar (dn-instr-frow), NOT a chip
  // per row. Three findings + the four practice-review rows = seven frows.
  const frows = allByClass(host, 'dn-instr-frow');
  assert(frows.length === 3 + 4, 'three finding rows + four practice rows, one grammar');
  // the de-tagging: NO bespoke severity chip survives.
  assert(!hasClass(host, 'dn-chip-instr-sev-crit'), 'no severity chip (de-tagged to a glyph/tone)');
  assert(allByClass(host, 'dn-instr-mark').length >= 3, 'each row leads with a tone glyph');
  assert(hasClass(host, 'dn-instr-fs-bad'), 'the critical finding carries the bad tone accent');
  const t = textOf(host);
  assert(t.includes('critical'), 'the severity word is present as tone-coloured text (not a chip)');
  // the copyable apply invocation stays — but only for an ACTIONABLE finding
  // (one with a proposed_op; reflect apply refuses a null-op finding).
  const applies = allByClass(host, 'dn-instr-apply');
  assert(applies.some((n) => (n.textContent || '').includes(`zicato reflect apply ${REFLECTION_ID} find-0a1b2c3d`)), 'the exact CLI invocation with reflection_id + finding_id');
});

test('bill of health: evidence renders as inline x-ray links in the row prose, not chip strips', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const evLine = allByClass(host, 'dn-instr-frow-ev');
  assert(evLine.length >= 1, 'a finding with evidence shows an inline evidence line');
  const target = router.href('instrument', { epochId: EPOCH_ID, reflectionId: REFLECTION_ID, judge: REFL_JUDGE, runRef: REFL_RUN_REF });
  const links = allByClass(host, 'dn-instr-link');
  assert(links.some((a) => a.getAttribute('href') === target), 'an inline evidence link points at the x-ray route');
});

// Issue #129's render-conformance rule, applied to the lens: the report
// already prints these, and a surface that states a verdict while dropping
// the numbers behind it (or the remedy) reproduces the bug it fixed.
test('render conformance: a practice check shows the measured numbers behind its headline', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const t = textOf(host);
  // promotion_hygiene's "remedy safety" pair — the margin against the floor
  // it has to clear. Stating "promotions are earned" without them is a claim
  // the operator cannot check.
  assert(t.includes('promote_margin=0.045'), 'the practice evidence names the margin');
  assert(t.includes('noise_floor=0.0145'), 'and the noise floor it is measured against');
  assert(t.includes('min_detectable_delta=0.052'), 'the unsound check shows its own evidence too');
  // an empty evidence dict adds no line (the unmeasured check carries {}).
  const rows = allByClass(host, 'dn-instr-frow');
  const unmeasured = rows.find((r) => ((allByClass(r, 'dn-instr-frow-verdict')[0] || {}).textContent || '').trim() === 'unmeasured');
  assert(unmeasured, 'the unmeasured row exists');
  assert(!allByClass(unmeasured, 'dn-instr-frow-ev').length, 'an empty evidence dict renders no line');
});

test('render conformance: findings and judge scorecards show their recommendation', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const recs = allByClass(host, 'dn-instr-frow-rec');
  assert(recs.length >= 2, 'recommendations render on findings and on judge cards');
  const t = textOf(host);
  // the finding whose ONLY remedy text is the recommendation (no proposed_op).
  assert(t.includes("broaden 'safety.scope' to catch the named missed-fire spans"),
    'a proposed_op-less finding still shows how to fix it');
  // …and the per-judge remedy, which NEITHER surface rendered before.
  assert(t.includes("tighten 'format.json' — it fires on 3 well-formed payloads"),
    'the judge scorecard names its remedy');
});

// Issue #112: the promote-margin recommendation scales `delta_std` (the
// draw-count-stable dispersion), NOT the max|Δ| RANGE, which inflates with the
// calibration draw count K. A calibration card showing only the range shows the
// one number the code says not to act on.
test('calibration: the draw-count-stable delta_std reaches the pillar card', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const t = textOf(host);
  assert(t.includes('noise floor Δ std'), 'the delta_std row is labelled distinctly from the range');
  assert(t.includes('0.0062'), 'the MEASURED delta_std number reaches the output');
  assert(t.includes('0.0180'), 'the max|Δ| range is still shown beside it');
});

test('calibration: delta_std is FOLDED INTO THE BILL DIGEST — a moved value repaints', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  assert(textOf(host).includes('0.0062'), 'the first read paints the first delta_std');
  // a re-calibration moves ONLY delta_std. If it is not in the digest the
  // gated swap writes nothing and the stale number stays on screen.
  const moved = JSON.parse(JSON.stringify(REFLECTION_SUMMARY));
  moved.pillars.calibration.noise_floor_delta_std = 0.0099;
  fresh(); // bust the data cache only — the host keeps its digest attribute
  installFixtureMap(reflectionFixtureMap({ summary: moved }));
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const t = textOf(host);
  assert(t.includes('0.0099'), 'the moved delta_std repainted (the digest folded it)');
  assert(!t.includes('0.0062'), 'the stale value is gone');
});

test('calibration: an absent delta_std renders NO row (never an "undefined")', async () => {
  fresh();
  const old = JSON.parse(JSON.stringify(REFLECTION_SUMMARY));
  delete old.pillars.calibration.noise_floor_delta_std;
  installFixtureMap(reflectionFixtureMap({ summary: old }));
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const t = textOf(host);
  assert(!t.includes('noise floor Δ std'), 'a record predating the statistic grows no row');
  assert(!t.includes('undefined') && !t.includes('NaN'), 'and fabricates nothing in its place');
});

test('bill of health: metadata (fidelity) is a dn-faint caption, not per-row tags', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const caps = allByClass(host, 'dn-instr-cap');
  assert(caps.some((c) => (c.textContent || '').includes('verbatim')), 'the fidelity tier rides an identity caption line');
  assert(!hasClass(host, 'dn-instr-kv'), 'the old tag-like identity strip is gone');
});

// ====================================================================
// PRACTICE REVIEW — the narrative layer (affirmation-first + fallbacks).
// ====================================================================
test('practice review: renders as loop-health rows, affirmation-FIRST ordering', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  // the four practice rows are the SAME grammar as findings (dn-instr-frow).
  const panel = allByClass(host, 'dn-instr-list-panel')[0];
  assert(panel, 'the practice-review panel rendered (shared list-panel grammar)');
  const rows = allByClass(panel, 'dn-instr-frow');
  assertEqual(rows.length, 4, 'four practice rows');
  // affirmations (sound) FIRST, then unsound > attend > unmeasured.
  const verdicts = rows.map((r) => {
    const v = allByClass(r, 'dn-instr-frow-verdict')[0];
    return (v && v.textContent || '').trim();
  });
  assertDeep(verdicts, ['sound', 'unsound', 'attend', 'unmeasured'], 'sound leads; unsound above attend; unmeasured last');
  const t = textOf(host);
  assert(t.includes('1 sound · 1 attend · 1 unsound · 1 unmeasured'), 'the verdict tally caption');
});

test('practice review: an unmeasured check names its missing input faint', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const missing = allByClass(host, 'dn-instr-frow-missing');
  assert(missing.length === 1, 'exactly the unmeasured row shows a missing-input line');
  assert((missing[0].textContent || '').includes('no corpus term-contributions'), 'names the missing input');
});

test('practice review: a proposed_op renders as copyable JSON + an "apply via the builder" note', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const t = textOf(host);
  // practice checks are NOT a `reflect apply` target — the op is copyable JSON.
  assert(t.includes('"op":"set_param"') || t.includes('"op": "set_param"'), 'the proposed op is copyable JSON');
  assert(t.includes('apply via the builder'), 'the faint "apply via the builder" note (no CLI apply for practice checks)');
  // a NO-proposed-op practice row (the sound affirmation) shows neither.
  const rows = allByClass(host, 'dn-instr-frow');
  const soundRow = rows.find((r) => ((allByClass(r, 'dn-instr-frow-verdict')[0] || {}).textContent || '').trim() === 'sound');
  assert(soundRow, 'the sound row exists');
  assert(!allByClass(soundRow, 'dn-instr-apply').length, 'a proposed_op-less row shows no copyable op');
});

test('practice review: an empty review degrades to the honest CLI prompt', async () => {
  fresh();
  const empty = { reflection_id: REFLECTION_ID, epoch_id: EPOCH_ID, found: false, checks: [], verdict_counts: { sound: 0, attend: 0, unsound: 0, unmeasured: 0 } };
  installFixtureMap(reflectionFixtureMap({ practices: empty }));
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const t = textOf(host);
  assert(t.includes('No practice review'), 'honest empty state');
  assert(t.includes('zicato reflect run') || t.includes('zicato reflect practices'), 'points at a CLI entry point');
});

// ====================================================================
// JUDGE AUDIT — the confusion matrix + untested greying.
// ====================================================================
test('judge audit: renders the 2×2 confusion matrix + ambiguous pile', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const cards = allByClass(host, 'dn-instr-card');
  assertEqual(cards.length, 3, 'one card per judge');
  const cells = allByClass(host, 'dn-instr-cmcell');
  // format.json card: 4 cells; safety.scope: 4 cells; recall.multi is untested
  // (no matrix). So 8 cells total.
  assertEqual(cells.length, 8, 'two exercised judges × 4 confusion cells');
  const t = textOf(host);
  assert(t.includes('ambiguous'), 'the ambiguous pile is surfaced');
  assert(t.includes('FPR'), 'the FPR rate is labelled');
  // κ and the disagreement rate are HONESTLY, separately labelled.
  assert(t.includes('self-consistency κ') && t.includes('disagreement rate'), 'both consistency statistics named distinctly');
});

test('judge audit: an unexercised judge is greyed "never fired" with no matrix', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  assert(hasClass(host, 'dn-instr-card-untested'), 'the untested card is greyed');
  const t = textOf(host);
  assert(t.includes('never fired'), 'labelled never fired');
  assert(t.includes('recall.multi'), 'names the untested judge');
});

test('judge audit: the FP/FN pile is inline x-ray links (not chip strips)', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  assert(!hasClass(host, 'dn-instr-echip'), 'the boxed evidence chips are gone (de-tagged)');
  const evLine = allByClass(host, 'dn-instr-card-ev');
  assert(evLine.length >= 1, 'a card with adjudicated evidence shows an inline pile line');
  const target = router.href('instrument', { epochId: EPOCH_ID, reflectionId: REFLECTION_ID, judge: REFL_JUDGE, runRef: REFL_RUN_REF });
  const links = allByClass(host, 'dn-instr-link');
  assert(links.some((c) => c.getAttribute('href') === target), 'an inline link goes to the x-ray route (enc run_ref)');
});

test('judge audit: redundancy/conflict collapse to ONE faint inline sentence (no chips)', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  assert(!hasClass(host, 'dn-instr-xchips'), 'no redundancy/conflict chip strip');
  const t = textOf(host);
  assert(t.includes('fires with tool.args'), 'redundancy reads as an inline "fires with" sentence');
  assert(t.includes('conflicts with safety.scope'), 'conflict reads as an inline "conflicts with" sentence');
});

// ====================================================================
// X-RAY — the span highlight + fidelity label + honest-unavailable.
// ====================================================================
test('x-ray: renders the span highlight + verbatim fidelity + verdict chip', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID, judge: REFL_JUDGE, runRef: REFL_RUN_REF });
  const t = textOf(host);
  assert(hasClass(host, 'dn-instr-span'), 'the evidence span is highlighted (it matched the transcript text)');
  assert(t.includes('verbatim'), 'the fidelity tier is labelled');
  assert(hasClass(host, 'dn-chip-instr-verdict-fp'), 'the FP verdict chip');
  assert(t.includes('independent-adjudicator'), 'the meta-judge model is shown');
  assert(t.includes('self-agreement'), 'adjudicator self-agreement is surfaced when present');
});

// Severity correctness is a SEPARATE axis from fire/silence: a judge that fires
// on the right span at the wrong severity passes the 2×2 and still mis-weights
// the loss. Only the AGGREGATE severity_accuracy had a surface before this.
function tpXray(severityMatch) {
  const x = JSON.parse(JSON.stringify(REFLECTION_XRAY));
  x.adjudication.verdict = 'TP';
  x.adjudication.adjudicated = 'should_fire';
  x.adjudication.severity_match = severityMatch;
  return x;
}

test('x-ray: a severity MISMATCH on a correct fire is named beside the verdict', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap({ xray: tpXray(false) }));
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID, judge: REFL_JUDGE, runRef: REFL_RUN_REF });
  const line = allByClass(host, 'dn-instr-xsevmatch');
  assertEqual(line.length, 1, 'one severity-agreement line');
  const t = (line[0].textContent || '');
  assert(t.includes('severity mismatch'), 'the disagreement is stated');
  assert(t.includes('warning'), 'and names the severity the judge CLAIMED');
  assert(t.includes('mis-weights the loss'), 'and why it matters despite the TP');
  assert((line[0].getAttribute('class') || '').includes('dn-instr-t-bad'), 'a mismatch carries the bad tone');
});

test('x-ray: severity_match is FOLDED INTO THE X-RAY DIGEST — a flip repaints', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap({ xray: tpXray(true) }));
  const host = document.createElement('div');
  const p = { epochId: EPOCH_ID, reflectionId: REFLECTION_ID, judge: REFL_JUDGE, runRef: REFL_RUN_REF };
  await instrument.render(host, CTX, p);
  assert(textOf(host).includes('severity agrees'), 'agreement paints first');
  // a re-adjudication flips ONLY the severity verdict — everything else holds.
  fresh(); // bust the data cache only; the host keeps its digest attribute
  installFixtureMap(reflectionFixtureMap({ xray: tpXray(false) }));
  await instrument.render(host, CTX, p);
  const t = textOf(host);
  assert(t.includes('severity mismatch'), 'the flip repainted (the adj digest tuple folded it)');
  assert(!t.includes('severity agrees'), 'the stale agreement is gone');
});

test('x-ray: a null severity_match renders NOTHING (it is scored on a TP only)', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap()); // the FP fixture: severity_match null
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID, judge: REFL_JUDGE, runRef: REFL_RUN_REF });
  assert(!hasClass(host, 'dn-instr-xsevmatch'), 'no severity line on a non-TP adjudication');
  const t = textOf(host);
  assert(!t.includes('severity') || !t.includes('undefined'), 'and nothing is fabricated in its place');
});

test('x-ray: an unavailable transcript degrades honestly (no fabricated turns)', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap({ xray: REFLECTION_XRAY_UNAVAILABLE }));
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID, judge: REFL_JUDGE, runRef: REFL_RUN_REF });
  const t = textOf(host);
  assert(t.includes('Transcript unavailable'), 'honest unavailable message');
  assert(!hasClass(host, 'dn-instr-turn'), 'no transcript turns fabricated');
});

// ====================================================================
// DIGEST NO-OP IDENTITY — two identical fetches ⇒ zero DOM rebuild.
// ====================================================================
test('digest no-op: a second identical render rebuilds ZERO DOM', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  assert(host.firstChild === first, 'no clear-and-rebuild on the identical repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

test('digest no-op: the x-ray is fetch-once (immutable) — identical repaint is a no-op', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  const p = { epochId: EPOCH_ID, reflectionId: REFLECTION_ID, judge: REFL_JUDGE, runRef: REFL_RUN_REF };
  await instrument.render(host, CTX, p);
  const first = host.firstChild;
  await instrument.render(host, CTX, p);
  assert(host.firstChild === first, 'x-ray no-op repaint keeps the same first child');
});

// ====================================================================
// TREE integration — the Instrument node shows only when the epoch has one.
// ====================================================================
test('tree: the Instrument leaf appears only when the epoch has reflections', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const model = await shell.buildTreeModel({ view: 'epoch', params: { epochId: EPOCH_ID } });
  assert(model.byEpoch[EPOCH_ID].hasReflections === true, 'epoch flagged with reflections');
  const host = document.createElement('div');
  tree.buildTree(host, model, { view: 'epoch', params: { epochId: EPOCH_ID } }, new Set(['e:' + EPOCH_ID]), CTX, () => {}, new Set());
  const leaves = host.querySelectorAll('[data-kind]').filter((n) => n.getAttribute('data-kind') === 'instrument');
  assertEqual(leaves.length, 1, 'exactly one Instrument node under the epoch');
});

test('tree: no Instrument leaf when the epoch has no reflections', async () => {
  fresh();
  const F = { ...reflectionFixtureMap(), '/api/reflections': { reflections: [] } };
  installFixtureMap(F);
  const model = await shell.buildTreeModel({ view: 'epoch', params: { epochId: EPOCH_ID } });
  assert(!model.byEpoch[EPOCH_ID].hasReflections, 'epoch not flagged');
  const host = document.createElement('div');
  tree.buildTree(host, model, { view: 'epoch', params: { epochId: EPOCH_ID } }, new Set(['e:' + EPOCH_ID]), CTX, () => {}, new Set());
  const leaves = host.querySelectorAll('[data-kind]').filter((n) => n.getAttribute('data-kind') === 'instrument');
  assertEqual(leaves.length, 0, 'no Instrument node without reflections');
});

// ====================================================================
// F4 — the evidence chip verdict comes from the payload, not a title regex.
// ====================================================================
test('judge audit F4: an evidence chip reads its verdict from the payload, not the title', async () => {
  fresh();
  const summary = JSON.parse(JSON.stringify(REFLECTION_SUMMARY));
  // a finding whose TITLE says "fires falsely" (regex → FP) but whose evidence
  // is adjudicated FN. The chip must follow the DATA, not the wording.
  summary.findings = [{
    finding_id: 'find-mislabel', pillar: 'validity', severity: 'warning',
    title: "Judge 'format.json' fires falsely", detail: 'x',
    evidence: [{ run_ref: REFL_RUN_REF, judge_name: REFL_JUDGE, verdict: 'FN', span: 'a span', adjudication_path: 'p' }],
    recommendation: 'y', proposed_op: null,
  }];
  installFixtureMap(reflectionFixtureMap({ summary }));
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const links = allByClass(host, 'dn-instr-card-ev');
  assert(links.length >= 1, 'an inline evidence line rendered on the format.json card');
  const t = textOf(host);
  assert(t.includes('FN'), 'the inline link reads FN (from evidence.verdict)');
  assert(hasClass(host, 'dn-instr-t-fn'), 'the FN tone is applied (not the FP the title regex would pick)');
});

// ====================================================================
// F5 — invalidateLive busts the reflection LIST but keeps the immutable
// singular reads (the prefix must not over-match /api/reflection/<id>).
// ====================================================================
test('data F5: invalidateLive busts the cached reflection list but not the singular reads', async () => {
  fresh(); // clears the data cache
  installFixtureMap(reflectionFixtureMap());
  const first = await data.reflections();
  assertEqual((first.reflections || []).length, 2, 'the list fetched + cached (two fixtures)');
  const sumFirst = await data.reflectionSummary(REFLECTION_ID);
  assert(sumFirst.found === true, 'the singular summary fetched + cached');
  // a NEW reflection completes: the list changes, and a would-be-different
  // summary is installed. Only the LIST must re-fetch on invalidateLive.
  const changedSummary = JSON.parse(JSON.stringify(REFLECTION_SUMMARY));
  changedSummary.found = false;
  installFixtureMap({ ...reflectionFixtureMap({ summary: changedSummary }), '/api/reflections': { reflections: [] } });
  data.invalidateLive();
  const refetched = await data.reflections();
  assertEqual((refetched.reflections || []).length, 0, 'invalidateLive busted the list → it re-fetched the new (empty) payload');
  const sumAfter = await data.reflectionSummary(REFLECTION_ID);
  assert(sumAfter.found === true, 'the singular /api/reflection/<id>/summary stayed cached (prefix did not over-match)');
});

await run();

// ====================================================================
// PROPOSER PANEL — the scorecard trend + the pending recommendations.
//
// The lens's second instrument. The checks that matter are the honesty ones:
// a null rate must not read as zero, a thin sample must be marked, and a
// no-op heartbeat must not repaint the panel.
// ====================================================================
test('proposer panel: the trend renders one row per epoch with reader numbers', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID });
  const t = textOf(host);
  assert(t.includes('Proposer scorecard'), 'the panel rendered');
  assert(t.includes('builtin:default') && t.includes('dir:fancy'), 'both proposers named');
  // e0's promote rate is 2/8 — the value AND the sample count both surface.
  assert(t.includes('25% (2/8)'), 'a rate renders with its sample count beside it');
});

test('proposer panel: a null rate renders as — with its n, never as 0%', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID });
  const t = textOf(host);
  // e0 screened nothing ⇒ value null, n 0. "0%" would claim it screened
  // plenty and vetoed none, which is the opposite of what happened.
  assert(t.includes('— (n=0)'), 'an unobserved rate reads as an em dash with n=0');
  assert(!t.includes('0% (0/0)'), 'a null rate is never rendered as a measured zero');
  // ...and a null median margin degrades the same way rather than to 0.000.
  assert(t.includes('—'), 'a null median margin renders as an em dash');
});

test('proposer panel: a thin sample is marked provisional', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID });
  // the current epoch's rates are over 3 samples (< min_sample_n 5).
  assert(textOf(host).includes('33%? (1/3)'), 'a provisional rate carries the ? marker');
});

test('proposer panel: a pending recommendation names its remedy and its apply command', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID });
  const t = textOf(host);
  assert(t.includes('Post-apply check A4 fails'), 'the recommendation title');
  assert(t.includes('skills/preserve-imports.md'), 'the remedy path');
  assert(t.includes('zicato proposer apply-recommendation prec-9f3a12bc'), 'the apply command');
  // The five evidence slots reach the panel — a recommendation is evidence-led.
  assert(t.includes('population:') && t.includes('compared against:'), 'the evidence slots');
  // It renders in the lens's EXISTING findings-row grammar, not new chrome.
  assert(hasClass(host, 'dn-instr-frow'), 'reuses the findings-row grammar');
});

test('proposer panel: null-degrades when the reads are unavailable', async () => {
  fresh();
  const F = { ...reflectionFixtureMap(),
    '/api/proposer/scorecard': { found: false, epochs: [], card: null },
    '/api/proposer/recommendations': { found: true, count: 0, pending: [] } };
  installFixtureMap(F);
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID });
  const t = textOf(host);
  assert(t.includes('No proposer scorecard yet'), 'honest empty scorecard state');
  assert(t.includes('zicato proposer scorecard'), 'points at the CLI entry point');
  assert(t.includes('No pending recommendations'), 'honest empty queue state');
  assert(t.includes('zicato proposer reflect'), 'points at the drafting command');
});

test('proposer panel: an identical repaint rebuilds ZERO DOM', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID });
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  await instrument.render(host, CTX, { epochId: EPOCH_ID });
  assert(host.firstChild === first, 'no clear-and-rebuild when the panel data is unchanged');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

test('proposer panel: a changed recommendation queue DOES repaint', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID });
  const first = host.firstChild;
  installFixtureMap({ ...reflectionFixtureMap(),
    '/api/proposer/recommendations': { found: true, count: 0, pending: [] } });
  await instrument.render(host, CTX, { epochId: EPOCH_ID });
  assert(host.firstChild !== first, 'the digest folds the queue, so a drained queue repaints');
});

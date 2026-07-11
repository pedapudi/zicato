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
  REFLECTION_ID, REFL_JUDGE, REFL_RUN_REF, REFLECTION_SUMMARY, REFLECTION_XRAY_UNAVAILABLE,
  reflectionFixtureMap,
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

test('bill of health: findings carry a severity chip + a copyable apply invocation', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  assert(allByClass(host, 'dn-instr-finding').length === 3, 'three findings rendered');
  assert(hasClass(host, 'dn-chip-instr-sev-crit'), 'a critical severity chip');
  const applies = allByClass(host, 'dn-instr-apply');
  assert(applies.length >= 3, 'each finding shows its apply invocation');
  assert(applies.some((n) => (n.textContent || '').includes(`zicato reflect apply ${REFLECTION_ID} find-0a1b2c3d`)), 'the exact CLI invocation with reflection_id + finding_id');
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

test('judge audit: FP/FN evidence chips link into the x-ray route', async () => {
  fresh();
  installFixtureMap(reflectionFixtureMap());
  const host = document.createElement('div');
  await instrument.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFLECTION_ID });
  const chips = allByClass(host, 'dn-instr-echip');
  assert(chips.length >= 1, 'at least one evidence chip');
  const target = router.href('instrument', { epochId: EPOCH_ID, reflectionId: REFLECTION_ID, judge: REFL_JUDGE, runRef: REFL_RUN_REF });
  assert(chips.some((c) => c.getAttribute('href') === target), 'a chip links to the x-ray route (enc run_ref)');
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
  const chips = allByClass(host, 'dn-instr-echip');
  assert(chips.length >= 1, 'an evidence chip rendered on the format.json card');
  assert(chips.some((c) => (c.textContent || '').includes('FN')), 'the chip reads FN (from evidence.verdict)');
  assert(hasClass(host, 'dn-instr-t-fn'), 'the FN tone is applied (not the FP the title regex would pick)');
  assert(!chips.some((c) => (c.textContent || '').includes('FP')), 'no FP chip fabricated from the "falsely" title');
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

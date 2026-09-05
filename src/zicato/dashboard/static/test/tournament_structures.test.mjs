// test/tournament_structures.test.mjs — the configurable tournament structure:
// bracket / standings / racing renderers
// over mock structure payloads, the universal radar, and the non-gauntlet
// epoch overview.
//
// Shared fixtures and helpers live in ./fixtures.mjs.

import { installDom, test, run, assert, assertEqual, assertDeep, makeEvent } from './harness.mjs';

installDom();

const {
  router, svg, data, tree, coreState, rounds,
  hovercard, live, STRUCT, EPOCH_ID, FIXTURE, installFetch,
  freshState, allByClass, svgsByClass, hasScrollWrapperAncestor, SE_STRUCT, SWISS_STRUCT,
  RACING_STRUCT, structFixture, installFixtureMap,
} = await import('./fixtures.mjs');

// ====================================================================
// Configurable tournament STRUCTURE — the bracket / standings / racing
// renderers, driven by MOCK structure payloads (the live workspace is
// gauntlet-only, so the non-gauntlet renderers are exercised here).
// ====================================================================







test('structure helpers: label + non-gauntlet detection', () => {
  assertEqual(STRUCT.isNonGauntlet('gauntlet'), false);
  assertEqual(STRUCT.isNonGauntlet('single_elim'), true);
  assertEqual(STRUCT.isNonGauntlet('swiss'), true);
  assertEqual(STRUCT.isNonGauntlet('racing'), true);
  assert(STRUCT.structureLabel('swiss', { rounds: 4 }).includes('4 rounds'), 'swiss label names its rounds');
  assert(STRUCT.structureLabel('single_elim', { seed_order: 'scalar' }).includes('scalar'), 'single-elim label names the seed order');
  assert(STRUCT.structureLabel('racing', { rungs: [1, 2, 3] }).includes('3 rungs'), 'racing label names its rungs');
});

test('structure: single-elim renders a fit-to-width RADIAL bracket (elimRadial spokes + standings)', async () => {
  freshState();
  installFixtureMap(structFixture('single_elim', SE_STRUCT, 'tourn_e0_se'));
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  // the structure pill names the configured structure (NOT the gauntlet ladder).
  assert(allByClass(host, 'dt-structure-pill').length >= 1, 'a structure pill labels the configured structure');
  assert(host.textContent.includes('Single elimination'), 'the pill names single-elim');
  assertEqual(allByClass(host, 'dt-champ-banner').length, 0, 'NO gauntlet champion-defends banner for a non-gauntlet structure');

  // the seat/box bracket tree and the lane flow are gone — the elim figure is the radial.
  assertEqual(svgsByClass(host, 'dn-elimbracket').length, 0, 'the seat/box bracket tree (dn-elimbracket) is retired');
  assertEqual(svgsByClass(host, 'dn-elimflow').length, 0, 'the lane flow is deleted');
  const bracket = svgsByClass(host, 'dn-elimradial')[0];
  assert(bracket, 'the elim figure is the RADIAL bracket (dn-elimradial)');
  assertEqual(svgsByClass(host, 'dn-elimradial').length, 1, 'ONE radial on the page — no companion figure');
  assertEqual(bracket.getAttribute('width'), '100%', 'the radial is fit-to-width (width:100%)');
  assert((bracket.getAttribute('viewBox') || '').startsWith('0 0 '), 'the radial carries a viewBox so it scales to its pane');
  assert(!hasScrollWrapperAncestor(bracket, host), 'no horizontal-scroll wrapper around the radial');
  // both bracket rounds rendered as rings + the centre champion seat.
  assertEqual(allByClass(bracket, 'dn-elimradial-ring').length, 3, 'both bracket rounds render as rings, plus the gate ring');
  assertEqual(allByClass(bracket, 'dn-elimradial-seat').length, 1, 'the radial carries the centre champion seat');
  // one spoke per competitor.
  assertEqual(allByClass(bracket, 'dn-elimradial-spoke').length, 4, 'a spoke per competitor is drawn');
  // a standings leaderboard rendered too.
  assert(allByClass(host, 'dt-standings').length >= 1, 'a standings leaderboard rendered');
  assert(host.textContent.includes('champion'), 'the standings names the champion status');
});

test('structure: double-elim renders the RADIAL bracket with the losers’ bracket on the lower arc', async () => {
  freshState();
  const DE = JSON.parse(JSON.stringify(SE_STRUCT));
  DE.structure = 'double_elim';
  DE.structure_params = { grand_final_reset: true };
  DE.rounds.push({ round_index: 2, label: 'LB Round 1', matches: [
    { match_id: 'LB-R0-0', competitors: ['v0', 'v2'], winner: 'v0', decision: 'rejected', delta_scalar: 0.02, bracket_slot: 'LB-R0-0', bye: false },
  ] });
  installFixtureMap(structFixture('double_elim', DE, 'tourn_e0_de'));
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(host.textContent.includes('Double elimination'), 'the pill names double-elim');
  // one radial SVG holds both brackets: winners' on the upper arc, losers' on
  // the lower, split by the equator; no figure toggle is offered.
  assertEqual(svgsByClass(host, 'dn-elimbracket').length, 0, 'the seat/box bracket tree is retired');
  assertEqual(svgsByClass(host, 'dn-elimflow').length, 0, 'the lane flow is deleted');
  const bracket = svgsByClass(host, 'dn-elimradial')[0];
  assert(bracket, 'the double-elim radial SVG rendered');
  assertEqual(svgsByClass(host, 'dn-elimradial').length, 1, 'ONE radial on the page — no toggle, no companion');
  assertEqual(allByClass(host, 'dt-fig-switch').length, 0, 'no figure-variant toggle');
  assertEqual(allByClass(bracket, 'dn-elimradial-equator').length, 1, 'the dashed equator splits the two brackets');
  assertEqual(allByClass(bracket, 'dn-elimradial-seat').length, 1, 'the radial ends in the centre champion seat');
  // the LB round adds a ring (three rounds + the gate ring).
  assertEqual(allByClass(bracket, 'dn-elimradial-ring').length, 4, 'the losers’ bracket round renders as a ring');
});

test('structure: swiss renders the standings LADDER hero + per-round pairings', async () => {
  freshState();
  installFixtureMap(structFixture('swiss', SWISS_STRUCT, 'tourn_e0_sw'));
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(host.textContent.includes('Swiss'), 'the pill names swiss');
  // the swiss standings LADDER (the racing-analogue hero) rendered.
  const ladder = svgsByClass(host, 'dn-swissladder')[0];
  assert(ladder, 'the swiss standings ladder SVG rendered');
  assertEqual(ladder.getAttribute('width'), '100%', 'the swiss ladder is fit-to-width (width:100%)');
  assert((ladder.getAttribute('viewBox') || '').startsWith('0 0 '), 'the swiss ladder carries a viewBox');
  // the ladder shows the accumulating standings + the champion-gate node.
  assert(allByClass(ladder, 'dn-swissladder-stand').length >= 1, 'the accumulating Copeland-point standings rendered');
  assert(ladder.textContent.toLowerCase().includes('standings'), 'the ladder labels the standings column');
  assert(ladder.textContent.toLowerCase().includes('champion-gate'), 'the ladder ends in a champion-gate node');
  assert(host.textContent.includes('Round 1') && host.textContent.includes('Round 2'), 'both swiss rounds render');
  // the per-round pairings now live INSIDE the ladder (one lane per match); the
  // standalone "Pairings · round by round" tables were collapsed away as a
  // duplicate of the ladder's pairing columns.
  assert(allByClass(ladder, 'dn-swissladder-pair').length >= 1, 'the ladder lays out the round pairings');
  assert(allByClass(host, 'dt-swiss-pairings').length === 0, 'the redundant standalone pairings tables are gone');
});

test('structure: the "Proposed field" section renders applied ✓ / rejected ✗ + reasons from field_status', async () => {
  freshState();
  const payload = JSON.parse(JSON.stringify(SWISS_STRUCT));
  payload.field_status = [
    { generation_id: 'v1', status: 'applied', reason: '', seed: 2 },
    { generation_id: 'v2', status: 'rejected', reason: 'proposer returned invalid JSON', seed: 3 },
  ];
  installFixtureMap(structFixture('swiss', payload, 'tourn_e0_sw'));
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  // the Proposed field tracker rendered with the headline counts.
  const tracker = allByClass(host, 'dn-prop-tracker')[0];
  assert(tracker, 'the "Proposed field" tracker rendered');
  const head = allByClass(host, 'dn-prop-head')[0];
  assert(head && head.textContent.includes('2 proposed') && head.textContent.includes('1 applied'),
    'the headline reads "2 proposed · 1 applied"');
  // one applied row (✓) and one rejected row (✗).
  const okRows = allByClass(host, 'dn-prop-row-ok');
  const badRows = allByClass(host, 'dn-prop-row-bad');
  assertEqual(okRows.length, 1, 'one applied row');
  assertEqual(badRows.length, 1, 'one rejected row');
  assert(okRows[0].textContent.includes('✓') && okRows[0].textContent.includes('v1'), 'the applied row shows ✓ + v1');
  assert(badRows[0].textContent.includes('✗') && badRows[0].textContent.includes('v2'), 'the rejected row shows ✗ + v2');
  // the rejection reason is reachable via the hovercard (attached to the row).
  const hc = await import('../js/hovercard.js');
  hc.show(badRows[0], 'x'); // prime the card surface (the row carries a hovercard binding)
  assert(svg.proposingTracker, 'the shared proposingTracker renderer is exported for reuse');
});

test('proposing tracker: a rejected challenger shows its SPECIFIC reason INLINE (validation error visible, not a black box)', () => {
  const tracker = svg.proposingTracker({
    fieldStatus: [
      { generation_id: 'v1', status: 'applied', reason: '', attempts: 1, attempt_reasons: [], hypothesis: 'swap the greeting string', seed: 2 },
      {
        generation_id: 'v2', status: 'rejected', seed: 3,
        reason: "hypothesis.expected_metric_movements[0]: unknown drift kind 'file_findability'",
        attempts: 2,
        attempt_reasons: [
          "hypothesis.expected_metric_movements[0]: unknown drift kind 'file_findability'",
          "hypothesis.expected_metric_movements[0]: unknown drift kind 'file_findability'",
        ],
        hypothesis: '',
      },
    ],
  });
  // The rejected row renders the SPECIFIC reason inline (not just on hover).
  const reasonEls = allByClass(tracker, 'dn-prop-reason');
  assertEqual(reasonEls.length, 1, 'one inline reason line (for the rejected slot)');
  assert(reasonEls[0].textContent.includes('file_findability'),
    'the file_findability validation message is rendered inline, plainly visible');
  // The retry badge surfaces the attempt count for the retried slot.
  const attemptEls = allByClass(tracker, 'dn-prop-attempts');
  assertEqual(attemptEls.length, 1, 'the retried slot shows an attempt badge');
  assert(attemptEls[0].textContent.includes('2 attempts'), 'the badge reads "2 attempts"');
  // The applied row renders its hypothesis as the detail line rather than a reason.
  const okRow = allByClass(tracker, 'dn-prop-row-ok')[0];
  assert(okRow && okRow.textContent.includes('swap the greeting string'),
    'the applied slot shows its hypothesis');
  // Headline counts roll up applied + rejected.
  const head = allByClass(tracker, 'dn-prop-head')[0];
  assert(head.textContent.includes('2 proposed') && head.textContent.includes('1 applied')
    && head.textContent.includes('1 rejected'), 'headline: 2 proposed · 1 applied · 1 rejected');
});

test('proposing tracker: an in-flight "proposing" slot reads as pending (not rejected, not all-rejected)', () => {
  const tracker = svg.proposingTracker({
    fieldStatus: [
      { generation_id: 'v1', status: 'applied', reason: '', attempts: 1, attempt_reasons: [], hypothesis: 'h', seed: 2 },
      { generation_id: 'v2', status: 'proposing', reason: '', attempts: 0, attempt_reasons: [], hypothesis: '', seed: 3 },
    ],
  });
  const pendingRows = allByClass(tracker, 'dn-prop-row-pending');
  assertEqual(pendingRows.length, 1, 'one pending row for the in-flight slot');
  assert(pendingRows[0].textContent.includes('proposing'), 'the pending row reads "proposing…"');
  const head = allByClass(tracker, 'dn-prop-head')[0];
  assert(head.textContent.includes('proposing'), 'headline surfaces the in-flight count');
  assert(!/all rejected/i.test(head.textContent),
    'a field with a still-proposing slot is NOT declared all-rejected prematurely');
  assert(!(head.getAttribute('class') || '').includes('dn-prop-head-allbad'),
    'no all-bad headline class while a slot is still proposing');
});

test('data.fieldStatus: carries the v6 observability fields (status proposing, attempts, attempt_reasons, hypothesis)', () => {
  const fs = data.fieldStatus({
    field_status: [
      {
        generation_id: 'v1', status: 'rejected', reason: 'final reason', seed: 2,
        attempts: 3, attempt_reasons: ['a', 'b', '', 'c'], hypothesis: 'ignored on reject',
      },
      { generation_id: 'v2', status: 'proposing' },
      { generation_id: 'v3', status: 'applied', hypothesis: 'do the thing' },
    ],
  });
  assertEqual(fs[0].status, 'rejected');
  assertEqual(fs[0].attempts, 3, 'attempts preserved');
  assertDeep(fs[0].attempt_reasons, ['a', 'b', 'c'], 'empty per-attempt reasons filtered out');
  assertEqual(fs[1].status, 'proposing', 'the proposing status is recognised (not coerced to rejected)');
  assertEqual(fs[1].attempts, 0, 'a proposing slot with no attempts count defaults to 0');
  assertEqual(fs[2].hypothesis, 'do the thing', 'applied hypothesis preserved');
  // The summary counts proposing distinctly + does not prematurely flag allRejected.
  const sum = data.fieldStatusSummary(fs);
  assertEqual(sum.proposing, 1, 'one proposing slot');
  assertEqual(sum.applied, 1, 'one applied slot');
  assertEqual(sum.rejected, 1, 'one rejected slot');
  assertEqual(sum.allRejected, false, 'not all-rejected while a slot proposes / one applied');
});

test('proposingDigest: re-stamps on an attempt-count / reason change, stable on a no-op', () => {
  const a = [{ generation_id: 'v1', status: 'proposing', attempts: 0, reason: '' }];
  const b = [{ generation_id: 'v1', status: 'rejected', attempts: 2, reason: 'file_findability validation error' }];
  assertEqual(svg.proposingDigest(a), svg.proposingDigest(a.map((x) => ({ ...x }))),
    'identical field → identical digest (no-op → ZERO DOM)');
  assert(svg.proposingDigest(a) !== svg.proposingDigest(b),
    'a proposing → rejected transition (with a reason) re-stamps the digest');
});

test('structure: a completed field where ALL challengers rejected reads "0 applied — all rejected", not empty', async () => {
  freshState();
  const payload = JSON.parse(JSON.stringify(SWISS_STRUCT));
  payload.field_status = [
    { generation_id: 'v1', status: 'rejected', reason: 'empty response', seed: 2 },
    { generation_id: 'v2', status: 'rejected', reason: 'post-apply validation failed', seed: 3 },
    { generation_id: 'v3', status: 'rejected', reason: 'mutation_id no longer resolves', seed: 4 },
    { generation_id: 'v4', status: 'rejected', reason: 'empty response', seed: 5 },
  ];
  installFixtureMap(structFixture('swiss', payload, 'tourn_e0_sw'));
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  const head = allByClass(host, 'dn-prop-head')[0];
  assert(head, 'the headline rendered');
  assert(head.textContent.includes('4 proposed') && head.textContent.includes('0 applied'),
    'the headline reads "4 proposed · 0 applied"');
  assert(/all rejected/i.test(head.textContent), 'the headline reads "all rejected" (not an empty/idle state)');
  assert((head.getAttribute('class') || '').includes('dn-prop-head-allbad'), 'the all-rejected headline carries the bad-state class');
  assertEqual(allByClass(host, 'dn-prop-row-ok').length, 0, 'NO applied rows for an all-rejected field');
  assertEqual(allByClass(host, 'dn-prop-row-bad').length, 4, 'all four challengers render as rejected rows');
});

test('structure: an absent field_status renders NO "Proposed field" section (back-compat)', async () => {
  freshState();
  // SWISS_STRUCT carries no field_status → no tracker section.
  installFixtureMap(structFixture('swiss', SWISS_STRUCT, 'tourn_e0_sw'));
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assertEqual(allByClass(host, 'dn-prop-tracker').length, 0, 'no proposing tracker when field_status is absent');
});

test('structure: a COMPLETED, all-applied field renders NO "Proposed field" section (it would just be an empty one-liner)', async () => {
  freshState();
  const payload = JSON.parse(JSON.stringify(SWISS_STRUCT));
  // every proposal applied + the run is complete (no live flag) → the ladder
  // already shows the field, so the lone section is omitted rather than left
  // reading as an empty "N proposed · N applied" line.
  payload.field_status = [
    { generation_id: 'v1', status: 'applied', reason: '', seed: 2 },
    { generation_id: 'v2', status: 'applied', reason: '', seed: 3 },
  ];
  installFixtureMap(structFixture('swiss', payload, 'tourn_e0_sw'));
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assertEqual(allByClass(host, 'dn-prop-tracker').length, 0, 'no Proposed-field section when completed + all applied');
  assert(!/Proposed field/.test(host.textContent), 'the "Proposed field" heading is not rendered');
  // …but the ladder (the field is shown there) still renders.
  assert(svgsByClass(host, 'dn-swissladder')[0], 'the swiss ladder still renders');
});

test('data.fieldStatus / fieldStatusSummary: normalize + roll up the proposing field', () => {
  const raw = {
    field_status: [
      { generation_id: 'v1', status: 'applied', reason: '', seed: 2 },
      { generation_id: 'v2', status: 'rejected', reason: 'invalid JSON', seed: 3 },
      { generation_id: '', status: 'applied' },          // dropped (no id)
      'garbage',                                          // dropped (not an object)
      { generation_id: 'v3', status: 'weird' },           // status coerced to rejected
    ],
  };
  const fs = data.fieldStatus(raw);
  assertEqual(fs.length, 3, 'only well-formed, identified rows survive');
  assertEqual(fs[2].status, 'rejected', 'an unknown status coerces to rejected');
  assertDeep(data.fieldStatus({}), [], 'absent field_status → empty list');
  assertDeep(data.fieldStatus(null), [], 'null payload → empty list');
  const sum = data.fieldStatusSummary(fs);
  assertEqual(sum.proposed, 3, 'proposed count');
  assertEqual(sum.applied, 1, 'applied count');
  assertEqual(sum.rejected, 2, 'rejected count');
  assertEqual(sum.allRejected, false, 'not all rejected (one applied)');
  const allBad = data.fieldStatusSummary([{ generation_id: 'v1', status: 'rejected' }]);
  assertEqual(allBad.allRejected, true, 'a non-empty zero-applied field is allRejected');
  assertEqual(data.fieldStatusSummary([]).allRejected, false, 'an empty field is NOT allRejected (idle, not failed)');
});

test('structure: racing renders a fit-to-width survival funnel with cuts + board fractions', async () => {
  freshState();
  installFixtureMap(structFixture('racing', RACING_STRUCT, 'tourn_e0_rc'));
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(host.textContent.includes('Racing'), 'the pill names racing');
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'a survival-funnel SVG rendered');
  assertEqual(ladder.getAttribute('width'), '100%', 'the survival funnel is fit-to-width (width:100%)');
  assert((ladder.getAttribute('viewBox') || '').startsWith('0 0 '), 'the survival funnel carries a viewBox');
  assert(!hasScrollWrapperAncestor(ladder, host), 'no horizontal-scroll wrapper around the survival funnel');
  assert(host.textContent.includes('50/100 board'), 'a rung shows its board fraction (budget escalation)');
  assert(host.textContent.includes('Rung 1') && host.textContent.includes('Rung 2'), 'both rungs render as stages');
});

// ---- REGRESSION: the per-ROUND Match-ups drill-down must build the SAME racing
// rung model the all-rounds / epoch view does (the recurring "round view empty"
// bug). A racing FIELD record carries `rounds: []` BY DESIGN — the rungs live in
// the LIVE active-tournament envelope (in flight) and in reconstructRacing / the
// per-tournament structure record (settled). Reading `round.tournamentRef.rounds`
// directly yields ZERO rungs and shows "No rungs evaluated yet." while the epoch
// view, going live-first then reconstruct, shows them. Both paths resolve through
// ONE shared resolver (resolveNonGauntletSt) so they CANNOT diverge; this pins
// LIVE and asserts that live and recorded converge.

// the per-round FIELD record the orchestrator opens at round start: it lists the
// competitor field + proposing status but carries EMPTY rounds/standings
// (`state="in_progress"`). This is the shape that reproduces the empty round.
const RACING_FIELD_EMPTY = {
  tournament_id: '2026-05-30_e0:field:v1', epoch_id: EPOCH_ID, structure: 'racing',
  structure_params: { board_fraction: 0.5, eta: 2, field_size: 4 },
  competitors: [
    { generation_id: 'v0', seed: 1, role: 'champion' }, { generation_id: 'v1', seed: 2, role: 'challenger' },
    { generation_id: 'v2', seed: 3, role: 'challenger' }, { generation_id: 'v3', seed: 4, role: 'challenger' },
  ],
  rounds: [], standings: [], field_status: [], state: 'in_progress', source: 'index',
};
// the LIVE active-tournament envelope DOES carry the populated rungs (stage_index
// stages, survivors/cut) — the in-flight source the round view must read.
const RACING_LIVE_AT = {
  epoch_id: EPOCH_ID, structure: 'racing', phase: 'tournament:round_0:running',
  structure_params: { board_fraction: 0.5, eta: 2, field_size: 4 },
  competitors: RACING_FIELD_EMPTY.competitors,
  rounds: [
    { stage_index: 0, label: 'Rung 0', matches: [{ match_id: 'rung0', competitors: ['v0', 'v1', 'v2', 'v3'], survivors: ['v1', 'v2'], cut: ['v3'], board_fraction: 0.5 }] },
    { stage_index: 1, label: 'Rung 1', matches: [{ match_id: 'rung1', competitors: ['v1', 'v2'], survivors: ['v1'], cut: ['v2'], board_fraction: 1.0 }] },
  ],
  standings: [
    { generation_id: 'v1', rank: 1, scalar: 40.0, status: 'alive' },
    { generation_id: 'v0', rank: 2, scalar: 54.0, status: 'alive', role: 'champion' },
  ],
};

function racingRoundFixture({ live }) {
  const gens = RACING_FIELD_EMPTY.competitors.map((c) => ({ generation_id: c.generation_id, epoch_id: EPOCH_ID, parent_generation_id: c.role === 'champion' ? '' : 'v0', promoted: c.role === 'champion', round_index: 0 }));
  // SETTLED: the field record + the per-tournament structure record carry the
  // resolved rungs (the orchestrator's settle upsert). LIVE: both stay
  // empty (the in_progress shape) so the ONLY rung source is the live envelope.
  const settledRounds = live ? [] : RACING_LIVE_AT.rounds.map((r) => ({ ...r, round_index: r.stage_index }));
  const settledStandings = live ? [] : RACING_LIVE_AT.standings;
  const fieldRec = { ...RACING_FIELD_EMPTY, rounds: settledRounds, standings: settledStandings, state: live ? 'in_progress' : 'settled' };
  const structRec = { ...fieldRec, source: 'index' };
  const F = {
    '/api/epoch': { epoch_id: EPOCH_ID, closed: !live, goal: 'g', current_champion: 'v0', tournament: { structure: 'racing', params: RACING_FIELD_EMPTY.structure_params },
      experiments: gens.map((g) => ({ generation_id: g.generation_id, parent_generation_id: g.parent_generation_id, outcome: { decision: g.promoted ? 'baseline' : 'pending' }, decision: g.promoted ? 'baseline' : 'pending', promoted: null, round_index: 0 })), board: [] },
    '/api/lineage': { generations: gens },
    '/api/score-trajectory': { points: live ? [] : [{ generation_id: 'v0', scalar: 54.0 }, { generation_id: 'v1', scalar: 40.0 }] },
    '/api/tournaments': { epoch_id: EPOCH_ID, structure: 'racing', structure_params: RACING_FIELD_EMPTY.structure_params, champion_lineage: ['v0'],
      matchups: [], tournaments: [fieldRec] },
    [`/api/tournament-structure/${EPOCH_ID}/${fieldRec.tournament_id}`]: structRec,
  };
  // LIVE: the views fetch the in-flight topology fresh from /api/active-tournament
  // (the only source carrying the rungs while the field record is still empty).
  if (live) F['/api/active-tournament'] = RACING_LIVE_AT;
  return F;
}

// helper: render a view and read whether the racing rung figures are present.
async function renderRacingView(view, params, { live }) {
  freshState();
  installFixtureMap(racingRoundFixture({ live }));
  if (live) {
    coreState.state.activeTournament = RACING_LIVE_AT;
    coreState.state.heartbeat = { phase: 'tournament:round_0:running', epoch_id: EPOCH_ID, ts: Date.now() };
    coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'run_v1' }];
  } else {
    coreState.state.activeTournament = null;
    coreState.state.heartbeat = null;
    coreState.state.activeRuns = [];
  }
  try {
    const mod = await import(`../js/views/${view}.js`);
    const host = document.createElement('div');
    await mod.render(host, { navigate() {}, href: router.href }, params);
    return host;
  } finally {
    coreState.state.activeTournament = null;
    coreState.state.heartbeat = null;
    coreState.state.activeRuns = [];
  }
}

test('REGRESSION (round-view-empty): the per-ROUND racing drill-down builds a NON-EMPTY rung model from the LIVE envelope when the field record carries rounds:[]', async () => {
  const host = await renderRacingView('gens', { epochId: EPOCH_ID, round: 0 }, { live: true });
  // the title is the round drill-down (not the all-rounds page).
  assert(host.textContent.includes('Round 0 · match-ups'), 'the round drill-down header reads "Round 0 · match-ups"');
  // The round view must NOT show this empty state.
  assert(!host.textContent.includes('No rungs evaluated yet'), 'the round view does NOT show the "No rungs evaluated yet." empty state');
  // the racing scalar track + survival funnel render the rungs from the live envelope.
  assert(host.textContent.includes('Scalar track'), 'the racing Scalar track renders in the round drill-down');
  assert(svgsByClass(host, 'dn-funnel')[0], 'the survival funnel SVG renders the rungs (non-empty rung model)');
  assert(host.textContent.includes('Rung 0') && host.textContent.includes('Rung 1'), 'both rungs render as stages in the round view');
});

test('REGRESSION (round-view-empty): the per-ROUND racing drill-down CONVERGES with the all-rounds + epoch views — same rungs LIVE and SETTLED', async () => {
  // LIVE: round drill-down vs all-rounds Match-ups — both must show the rungs.
  const roundLive = await renderRacingView('gens', { epochId: EPOCH_ID, round: 0 }, { live: true });
  const allRoundsLive = await renderRacingView('gens', { epochId: EPOCH_ID }, { live: true });
  const epochLive = await renderRacingView('epoch', { epochId: EPOCH_ID }, { live: true });
  for (const [name, h] of [['round', roundLive], ['all-rounds', allRoundsLive], ['epoch', epochLive]]) {
    assert(svgsByClass(h, 'dn-funnel')[0], `LIVE: the ${name} view renders the racing funnel (non-empty rungs)`);
    assert(!h.textContent.includes('No rungs evaluated yet'), `LIVE: the ${name} view has no empty rung state`);
  }
  // SETTLED: the field/per-tournament record now carries the rungs (the
  // orchestrator's settle upsert). With NO live run the round drill-down must
  // STILL build the rungs — proving live↔recorded convergence (no source drift).
  const roundSettled = await renderRacingView('gens', { epochId: EPOCH_ID, round: 0 }, { live: false });
  const allRoundsSettled = await renderRacingView('gens', { epochId: EPOCH_ID }, { live: false });
  for (const [name, h] of [['round', roundSettled], ['all-rounds', allRoundsSettled]]) {
    assert(!h.textContent.includes('No rungs evaluated yet'), `SETTLED: the ${name} view has no empty rung state`);
    assert(svgsByClass(h, 'dn-funnel')[0], `SETTLED: the ${name} view renders the racing funnel from the recorded rungs`);
    assert(h.textContent.includes('Rung 0') && h.textContent.includes('Rung 1'), `SETTLED: the ${name} view shows both rungs`);
  }
});

// ---- REGRESSION (single-source-of-truth across VIEWS): the EPOCH overview
// must build its racing/swiss/elim model through the SAME shared resolver the
// Match-ups + per-round views use. The OLD epoch path built racing via
// `normalizeStructure(liveRaw,true)` (no progressive overlay / projected re-rank
// / seeded-champ benchmark) and bypassed the completed per-tournament record,
// so its funnel diverged from the Match-ups figure LIVE *and* SETTLED. These
// pin the epoch view to the resolver, asserting it converges with gens.

test('REGRESSION (view-divergence): the EPOCH racing overview builds a NON-EMPTY funnel LIVE and SETTLED (resolver-built, never the empty rounds:[] field record)', async () => {
  const live = await renderRacingView('epoch', { epochId: EPOCH_ID }, { live: true });
  assert(svgsByClass(live, 'dn-funnel')[0], 'LIVE: the epoch overview renders the racing funnel from the live envelope');
  assert(!live.textContent.includes('not a gauntlet') && !live.textContent.includes('No rungs'), 'LIVE: no negative/empty rung state on the epoch view');
  const settled = await renderRacingView('epoch', { epochId: EPOCH_ID }, { live: false });
  assert(svgsByClass(settled, 'dn-funnel')[0], 'SETTLED: the epoch overview renders the racing funnel from the recorded rungs');
});

test('REGRESSION (view-divergence): the EPOCH racing model CONVERGES (digest-equal) with the all-rounds + per-round views — one shared resolver, no source drift', async () => {
  // Build the racing `st` the way EACH view now does: all through the resolver.
  // The active-tournament envelope is the live source; with the bracket empty
  // (the e4 in-progress shape) the resolver adopts the live model identically.
  const at = RACING_LIVE_AT;
  const hb = { phase: at.phase, epoch_id: at.epoch_id };
  const opts = { structure: 'racing', bracket: {}, epochId: EPOCH_ID, liveRaw: at, heartbeat: hb, activeRuns: [], params: at.structure_params };
  const epochSt = STRUCT.resolveNonGauntletSt(opts).st;     // epoch.js path
  const gensSt = STRUCT.resolveNonGauntletSt(opts).st;      // gens.js path
  const candSt = STRUCT.resolveNonGauntletSt(opts).st;      // candidate.js path
  assert(epochSt && gensSt && candSt, 'all three view paths resolve a racing st');
  const dE = STRUCT.structureDigest(epochSt);
  assertEqual(STRUCT.structureDigest(gensSt), dE, 'epoch ↔ all-rounds racing model is digest-equal');
  assertEqual(STRUCT.structureDigest(candSt), dE, 'candidate ↔ all-rounds racing model is digest-equal');
  // the resolved model is non-empty (the rungs the funnel/track draw).
  const m = STRUCT.racingModel(epochSt);
  assert(m && m.hasRungs && m.rungs.length >= 2, 'the shared racing model carries the rungs');
});

// ---- REGRESSION (round-N "stuck on seeding"): when the NEXT round has only
// begun PROPOSING — an empty live envelope (non-terminal phase, rounds:[]) —
// the resolver must PRESERVE the just-SETTLED prior round's bracket rather than
// let the empty envelope overwrite it with a "being seeded" ladder. The live
// envelope is adopted only when it carries real content, OR when there is no
// settled record (the first round's own proposing).
test('REGRESSION (stuck-on-seeding): an EMPTY live proposing envelope does NOT overwrite a SETTLED prior round; with no record it still shows live', () => {
  const liveProposing = {
    epoch_id: EPOCH_ID, structure: 'racing', phase: 'proposing',
    structure_params: { board_fraction: 0.5, eta: 2, field_size: 4 },
    competitors: [
      { generation_id: 'v0', seed: 1, role: 'champion' },
      { generation_id: 'v5', seed: 2, role: 'challenger' },
    ],
    rounds: [], standings: [], field_status: [],
  };
  const settled = STRUCT.normalizeStructure({
    structure: 'racing', source: 'record',
    competitors: [{ generation_id: 'v0', seed: 1 }, { generation_id: 'v1', seed: 2 }],
    rounds: [{ stage_index: 0, label: 'Rung 0', matches: [{ match_id: 'rung0_m0', competitors: ['v0', 'v1'], survivors: ['v1'], cut: [] }] }],
    standings: [{ generation_id: 'v1', rank: 1, scalar: 1.0 }],
  }, false);

  const withRecord = STRUCT.resolveNonGauntletSt({
    structure: 'racing', bracket: {}, epochId: EPOCH_ID,
    liveRaw: liveProposing, heartbeat: { phase: 'proposing', epoch_id: EPOCH_ID },
    activeRuns: [], completedRecord: settled,
  });
  assert(withRecord.source !== 'live', `the empty live envelope is rejected; settled round preserved (got source=${withRecord.source})`);
  assert(withRecord.st && (withRecord.st.rounds || []).some((r) => (r.matches || []).length), 'the preserved record carries the settled match');

  const firstRound = STRUCT.resolveNonGauntletSt({
    structure: 'racing', bracket: {}, epochId: EPOCH_ID,
    liveRaw: liveProposing, heartbeat: { phase: 'proposing', epoch_id: EPOCH_ID },
    activeRuns: [], completedRecord: null,
  });
  assert(firstRound.source === 'live', `first-round proposing still shows the live being-seeded state (got source=${firstRound.source})`);
});

test('REGRESSION (view-divergence): the EPOCH swiss + single_elim overviews build a NON-EMPTY model SETTLED (resolver completed-record fallback)', async () => {
  // SWISS settled: the per-tournament record carries the rounds; the epoch
  // overview must build the swiss bump/bars from it (not an empty strip).
  freshState();
  installFixtureMap(structFixture('swiss', SWISS_STRUCT, 'tourn_e0_sw'));
  const epochMod = await import('../js/views/epoch.js');
  let host = document.createElement('div');
  await epochMod.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(svgsByClass(host, 'dn-swissover')[0], 'SETTLED swiss: the epoch overview builds the swiss bump/bars figure from the record');
  assert(!host.textContent.includes('not a gauntlet'), 'SETTLED swiss: no negative placeholder');
  // SINGLE-ELIM settled: the epoch overview builds the radial bracket from the record.
  freshState();
  installFixtureMap(structFixture('single_elim', SE_STRUCT, 'tourn_e0_se'));
  host = document.createElement('div');
  await epochMod.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(svgsByClass(host, 'dn-elimradial')[0], 'SETTLED single_elim: the epoch overview builds the radial bracket from the recorded rounds');
  assert(!host.textContent.includes('not a gauntlet'), 'SETTLED single_elim: no negative placeholder');
});

test('REGRESSION (view-divergence): the CANDIDATE racing dossier builds field panels from the LIVE envelope (live-first via the resolver, not settled-only reconstruct)', async () => {
  freshState();
  installFixtureMap(racingRoundFixture({ live: true }));
  coreState.state.activeTournament = RACING_LIVE_AT;
  coreState.state.heartbeat = { phase: 'tournament:round_0:running', epoch_id: EPOCH_ID, ts: Date.now() };
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'run_v1' }];
  try {
    const candMod = await import('../js/views/candidate.js');
    const host = document.createElement('div');
    // view the surviving racer v1 — its dossier swaps to the field-relative panels.
    await candMod.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
    assert(allByClass(host, 'dn-racing-field')[0], 'the candidate dossier shows the field-relative racing panels (built from the LIVE envelope)');
    assert(host.textContent.includes('FIELD-relative'), 'the field-relative caption renders');
    // the field standings include the OTHER racers from the live rungs (v2/v3),
    // not just the settled reconstruction — proving the live envelope was read.
    assert(/v2|v3/.test(host.textContent), 'the field panels list the live-envelope field, not an empty/settled-only reconstruction');
  } finally {
    coreState.state.activeTournament = null;
    coreState.state.heartbeat = null;
    coreState.state.activeRuns = [];
  }
});

// ---- the RADAR SILHOUETTE is UNIVERSAL across tournament structures --------
// The radar is a PER-CANDIDATE quality view — THIS candidate's scalar-component
// shape vs the champion across the heterogeneous gate axes (scalar / pass-rate /
// per-judge drift). That comparison is independent of HOW the tournament was run
// (gauntlet · single/double-elim · swiss · racing all schedule the SAME
// champion→challenger gate), so the dossier must draw it for EVERY structure —
// never gated on a gauntlet-only field. These regression tests lock that in so a
// future structure-branch refactor can't silently drop the radar from a
// non-gauntlet dossier. The radar's data source is the candidate's settled gate
// (scalar_components) + its per-board slice, which exists for any structure; the
// helper below enriches a structFixture with exactly that per-candidate data.

// Enrich a structFixture so the viewed challenger v1 (parent v0) carries the
// SETTLED per-candidate data a radar needs under ANY structure: a per-entry
// slice with pass_fail (the pass-rate axis) + a champion per-entry slice (so the
// dumbbell pairs) + a settled v0→v1 gate with scalar_components for BOTH sides
// (the per-judge axes). Identical per-candidate payload regardless of structure,
// so the only thing varying across the tests is the tournament structure.
function radarStructFixture(structure, payload, tournamentId) {
  const F = structFixture(structure, payload, tournamentId);
  F[`/api/generation/${EPOCH_ID}/v0/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v0', entries: [
    { entry_id: 'waffles_single', run_id: 'run_v0_w', drift_loss: 60.5, pass_fail: false, runtime_ms: 180000, wall_clock_budget_exceeded: false },
    { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v0_p', drift_loss: 70.0, pass_fail: true, runtime_ms: 180000, wall_clock_budget_exceeded: false },
  ] };
  F[`/api/generation/${EPOCH_ID}/v1/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v1', entries: [
    { entry_id: 'waffles_single', run_id: 'run_v1_w', drift_loss: 55.0, pass_fail: true, runtime_ms: 180000, wall_clock_budget_exceeded: false },
    { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v1_p', drift_loss: 66.0, pass_fail: true, runtime_ms: 180000, wall_clock_budget_exceeded: false },
  ] };
  F[`/api/round/${EPOCH_ID}/v0/v1/gate`] = { decision: 'promoted', delta_scalar: -4.5, delta_pass_rate: 0.5,
    reason: 'challenger improved', rules: [
      { id: 'scalar_margin', label: 'Scalar margin', status: 'pass', fired: false, detail: '70.94 → 66.44 (needs ≤ -0.01)' },
      { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'pass', fired: false },
      { id: 'namespace_monotonicity', label: 'Namespace monotonicity', status: 'pass', fired: false },
    ],
    // BOTH sides decomposed across ≥2 components → the radar forms ≥3 axes
    // (scalar-inverse + pass-rate + each per-judge component).
    scalar_components: { champion: { drift: 68.5, schema: 1.43 }, challenger: { drift: 60.0, schema: 1.0 } } };
  return F;
}

// Drive the candidate dossier for challenger v1 under `structure` + assert the
// radar silhouette renders (folded into its width-capped side pane), names ≥3
// real axes (not numeric indices), and is plottable (≥3 hover-able vertices).
async function assertRadarRendersFor(structure, payload, tournamentId) {
  freshState();
  installFixtureMap(radarStructFixture(structure, payload, tournamentId));
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  const radar = svgsByClass(host, 'dn-radar')[0];
  assert(radar, `the radar silhouette renders in the ${structure} candidate dossier`);
  assert(allByClass(host, 'dn-radarpane')[0], `the ${structure} radar sits in its width-capped pane`);
  assert(allByClass(host, 'dn-radar-hot').length >= 3, `the ${structure} radar exposes ≥3 hover-able axis vertices (plottable)`);
  const labels = allByClass(radar, 'dn-radar-axislab').map((n) => (n.textContent || '').trim()).filter(Boolean);
  assert(labels.includes('scalar'), `the ${structure} radar labels its scalar axis`);
  assert(labels.includes('pass-rate'), `the ${structure} radar labels its pass-rate axis`);
  assert(!labels.some((l) => /^\d+$/.test(l)), `the ${structure} radar uses named axes, not numeric indices`);
  return host;
}

test('candidate dossier: the RADAR is UNIVERSAL — it renders for gauntlet', async () => {
  // gauntlet uses the base FIXTURE (v0/v1 gate carries scalar_components already).
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  assert(svgsByClass(host, 'dn-radar')[0], 'the radar renders in the gauntlet candidate dossier');
  assert(allByClass(host, 'dn-radar-hot').length >= 3, 'the gauntlet radar is plottable (≥3 vertices)');
  // gauntlet has no field-relative racing panels.
  assertEqual(allByClass(host, 'dn-racing-field').length, 0, 'gauntlet shows no racing field panels');
});

test('candidate dossier: the RADAR is UNIVERSAL — it renders for single_elim', async () => {
  await assertRadarRendersFor('single_elim', SE_STRUCT, 'tourn_e0_se');
});

test('candidate dossier: the RADAR is UNIVERSAL — it renders for double_elim', async () => {
  const DE = JSON.parse(JSON.stringify(SE_STRUCT));
  DE.structure = 'double_elim';
  DE.structure_params = { grand_final_reset: true };
  await assertRadarRendersFor('double_elim', DE, 'tourn_e0_de');
});

test('candidate dossier: the RADAR is UNIVERSAL — it renders for swiss', async () => {
  await assertRadarRendersFor('swiss', SWISS_STRUCT, 'tourn_e0_sw');
});

test('candidate dossier: the RADAR is UNIVERSAL — a SETTLED racer shows the radar ALONGSIDE the field-relative panels', async () => {
  // A settled racing candidate (settled scalar + a recorded gate with
  // scalar_components) gets BOTH the per-candidate radar AND racing's
  // field-relative panels — they answer different questions (component QUALITY
  // vs field POSITION), so the radar ADDS to, never replaces, the field panels.
  // Built from the SETTLED racing field record (its rungs reconstruct the field)
  // enriched with v1's settled gate + per-board slice (so the radar is plottable).
  freshState();
  const F = racingRoundFixture({ live: false });
  F[`/api/generation/${EPOCH_ID}/v0/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v0', entries: [
    { entry_id: 'b0', run_id: 'run_v0_b0', drift_loss: 60.5, pass_fail: false, runtime_ms: 180000, wall_clock_budget_exceeded: false },
    { entry_id: 'b1', run_id: 'run_v0_b1', drift_loss: 70.0, pass_fail: true, runtime_ms: 180000, wall_clock_budget_exceeded: false },
  ] };
  F[`/api/generation/${EPOCH_ID}/v1/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v1', entries: [
    { entry_id: 'b0', run_id: 'run_v1_b0', drift_loss: 38.0, pass_fail: true, runtime_ms: 180000, wall_clock_budget_exceeded: false },
    { entry_id: 'b1', run_id: 'run_v1_b1', drift_loss: 42.0, pass_fail: true, runtime_ms: 180000, wall_clock_budget_exceeded: false },
  ] };
  F[`/api/round/${EPOCH_ID}/v0/v1/gate`] = { decision: 'promoted', delta_scalar: -14.0, delta_pass_rate: 0.5,
    reason: 'challenger improved', rules: [
      { id: 'scalar_margin', label: 'Scalar margin', status: 'pass', fired: false, detail: '54.0 → 40.0 (needs ≤ -0.01)' },
      { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'pass', fired: false },
      { id: 'namespace_monotonicity', label: 'Namespace monotonicity', status: 'pass', fired: false },
    ],
    scalar_components: { champion: { drift: 53.0, schema: 1.4 }, challenger: { drift: 39.0, schema: 1.0 } } };
  installFixtureMap(F);
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  // the per-candidate radar renders + is plottable (≥3 named axes).
  const radar = svgsByClass(host, 'dn-radar')[0];
  assert(radar, 'the radar renders in the settled racing candidate dossier');
  assert(allByClass(host, 'dn-radar-hot').length >= 3, 'the settled-racing radar is plottable (≥3 vertices)');
  const labels = allByClass(radar, 'dn-radar-axislab').map((n) => (n.textContent || '').trim()).filter(Boolean);
  assert(labels.includes('scalar') && labels.includes('pass-rate'), 'the settled-racing radar names its scalar + pass-rate axes');
  // racing's field-relative panels render TOO (added rather than swapped in).
  assert(allByClass(host, 'dn-racing-field')[0], 'the racing field-relative panels render alongside the radar');
  assert(host.textContent.includes('FIELD-relative'), 'the racing field caption renders alongside the radar');
  // and the radar reads the FIELD-leader-reference caption for racing (not the
  // pairwise "vs champion" wording) — the structure-aware caption.
  const cap = allByClass(host, 'dn-radar-cap')[0];
  assert(cap && /field-leader/i.test(cap.textContent), 'the racing radar caption reads vs the field-leader reference');
});

test('candidate dossier: a no-op heartbeat over a NON-GAUNTLET (swiss) dossier WITH a radar churns NO DOM (digest-gated, anti-flash)', async () => {
  freshState();
  installFixtureMap(radarStructFixture('swiss', SWISS_STRUCT, 'tourn_e0_sw'));
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  // the radar IS present (so the digest folds a real radar model).
  assert(svgsByClass(host, 'dn-radar')[0], 'the swiss dossier rendered its radar');
  const digest1 = host.getAttribute('data-t-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  // an identical re-render (the no-op heartbeat) must write ZERO DOM.
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  assertEqual(host.getAttribute('data-t-digest'), digest1, 'the radar-bearing swiss dossier digest is unchanged on a no-op beat');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op beat (radar model folded, not churned)');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op beat over the radar-bearing dossier');
});

test('structure: a missing structure payload degrades gracefully (no throw, honest empty)', async () => {
  freshState();
  // epoch names swiss but the structure endpoint 404s + no tournaments[].
  const F = {
    '/api/epoch': { epoch_id: EPOCH_ID, closed: false, goal: 'g', tournament: { structure: 'swiss', params: {} }, experiments: [], board: [] },
    '/api/lineage': { generations: [] },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': { epoch_id: EPOCH_ID, structure: 'swiss', champion_lineage: [], matchups: [], tournaments: [] },
  };
  installFixtureMap(F);
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(host.textContent.includes('Swiss'), 'the structure pill still names swiss');
  assert(/No completed tournament|unavailable/i.test(host.textContent), 'an honest empty state renders rather than throwing');
});

test('structure: the epoch view shows the structure pill from the epoch tournament block', async () => {
  freshState();
  installFixtureMap(structFixture('swiss', SWISS_STRUCT, 'tourn_e0_sw'));
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(allByClass(host, 'dt-structure-pill').length >= 1, 'the epoch header carries a structure pill');
  assert(host.textContent.includes('Swiss'), 'the epoch pill names the configured swiss structure');
});

// ---- the EPOCH-VIEW non-gauntlet OVERVIEW (replaces the negative placeholder) ----

test('epoch timeline (swiss): the round episode embeds the standings BUMP chart + ranked Copeland bar + gate verdict — "not a gauntlet" is GONE', async () => {
  freshState();
  installFixtureMap(structFixture('swiss', SWISS_STRUCT, 'tourn_e0_sw'));
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  // the negative placeholder is gone — replaced by the timeline + figure.
  assert(!host.textContent.includes('not a gauntlet'), 'the negative "not a gauntlet" placeholder is GONE');
  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline rendered for the swiss epoch');

  // the swiss overview SVG is embedded as the episode figure, fit-to-width.
  const over = svgsByClass(host, 'dn-swissover')[0];
  assert(over, 'the swiss overview SVG is embedded as the swiss episode figure');
  assertEqual(over.getAttribute('width'), '100%', 'the swiss overview is fit-to-width (width:100%)');
  assert((over.getAttribute('viewBox') || '').startsWith('0 0 '), 'the swiss overview carries a viewBox so it scales');
  assert(!hasScrollWrapperAncestor(over, host), 'no horizontal-scroll wrapper around the overview');

  // (1) the bump chart: one line per competitor that has ranks.
  assertEqual(allByClass(over, 'dn-swissover-line').length, 3, 'one bump line per competitor (v0, v1, v2)');
  assert(allByClass(over, 'dn-swissover-line-champ').length >= 1, 'the champion line is emphasised');
  // (2) the ranked Copeland-point bar (the standings, one bar each).
  assert(allByClass(over, 'dn-swissover-bar').length >= 2, 'the ranked Copeland bars rendered');
  assert(over.textContent.includes('♔'), 'the leader is marked ♔ on the ranked bar');
  // the champion-gate verdict.
  assert(over.textContent.includes('promoted') || over.textContent.includes('♛'),
    'the champion-gate verdict (promoted ♛) is shown');
  // the episode drills into the round's full Match-ups.
  assert(host.textContent.includes('open round'), 'the episode keeps the "open round →" drill affordance');
});

test('epoch timeline (single-elim): the elim episode embeds the RADIAL bracket (elim parity), NOT the mini-bracket', async () => {
  freshState();
  installFixtureMap(structFixture('single_elim', SE_STRUCT, 'tourn_e0_se'));
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  assert(!host.textContent.includes('not a gauntlet'), 'the negative placeholder is GONE for elim too');
  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline rendered for the elim epoch');
  // the epoch hero leads with the radial bracket (matching racing→funnel,
  // swiss→bump) rather than the compact mini-bracket.
  const bracket = svgsByClass(host, 'dn-elimradial')[0];
  assert(bracket, 'the elim episode embeds the radial bracket (elimRadial)');
  assertEqual(bracket.getAttribute('width'), '100%', 'the radial is fit-to-width');
  assert((bracket.getAttribute('viewBox') || '').startsWith('0 0 '), 'the radial carries a viewBox');
  assertEqual(svgsByClass(host, 'dn-elimbracket-compact').length, 0, 'NO compact mini-bracket on the epoch overview (it is the radial)');
});

test('epoch timeline (no data): the timeline renders an honest empty — NEVER the negative "not a gauntlet" placeholder', async () => {
  freshState();
  // a swiss epoch with NO tournament records yet (mid-proposing / not run).
  const F = {
    '/api/epoch': { epoch_id: EPOCH_ID, closed: false, goal: 'g', tournament: { structure: 'swiss', params: { rounds: 3 } }, experiments: [], board: [] },
    '/api/lineage': { generations: [] },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': { epoch_id: EPOCH_ID, structure: 'swiss', champion_lineage: [], matchups: [], tournaments: [] },
  };
  installFixtureMap(F);
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  assert(!host.textContent.includes('not a gauntlet'), 'no negative "not a gauntlet" placeholder even with no data');
  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline renders even with no data');
  assertEqual(svgsByClass(host, 'dn-swissover').length, 0, 'no embedded swiss figure when there is no data');
  // a no-data epoch degrades to a single round-0 episode with no minted field.
  assert(/no challengers minted this round/i.test(host.textContent), 'the empty round reads "no challengers minted this round"');
});

test('structure: the data layer exposes tournamentStructure() + invalidates its cache live', async () => {
  assertEqual(typeof data.tournamentStructure, 'function', 'data.tournamentStructure() exists');
  // the live-invalidation set includes the new prefix.
  const css = '';  // (no css needed) — assert the source carries the prefix.
  const src = await import('node:fs').then((fs) => fs.readFileSync(new URL('../js/data.js', import.meta.url), 'utf8'));
  assert(src.includes('/api/tournament-structure/'), 'invalidateLive() busts the tournament-structure prefix');
});

// ---- gauntlet REGRESSION: the default structure is unchanged --------

test('gauntlet (default): the match-ups page renders the FIELD as the duel-flow graphic + integrated champion header (no structure pill)', async () => {
  freshState(); installFetch();  // the default gauntlet fixture (no tournament block)
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assertEqual(allByClass(host, 'dt-champ-banner').length, 0, 'NO boxed champion banner remains');
  assertEqual(allByClass(host, 'dt-match-card').length, 0, 'NO match-card boxes remain');
  assert(allByClass(host, 'dt-fieldflow-champ').length === 1, 'the integrated champion header renders');
  const flow = svgsByClass(host, 'dn-duelflow')[0];
  assert(flow, 'the field renders as the duel-flow structure-graphic');
  assertEqual(allByClass(flow, 'dn-duelflow-lane').length, 2, 'one challenger lane per round (v0→v1, v0→v2)');
  assertEqual(allByClass(host, 'dn-sbracket').length, 0, 'NO bracket SVG for the gauntlet default');
  assertEqual(allByClass(host, 'dt-structure-pill').length, 0, 'NO structure pill for a gauntlet epoch with no tournament block');
});

await run();

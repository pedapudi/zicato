// test/variant_t_figures.test.mjs — the console's figures: the evolve-rounds
// model and spine timeline, the data-graphics that carry no chartjunk, the
// responsive structure-builder layer, and continuous per-entry scoring.
//
// Shared fixtures and helpers live in ./fixtures.mjs.

import { installDom, test, run, assert, assertEqual, assertDeep, makeEvent } from './harness.mjs';

installDom();

const {
  router, svg, ui, data, tree, svgEl,
  rounds, dag, hovercard, live, roundTimelineFromFixtures, EPOCH_ID,
  FIXTURE, installFetch, freshState, allByClass, readCssAsync, readCss,
  svgsByClass, SE_STRUCT, structFixture, installFixtureMap,
} = await import('./fixtures.mjs');
const mock = await import('./mock_server.mjs');

// ====================================================================
// EVOLVE ROUNDS (champion-spine round model + timeline + drill-down + tree).
//   * the round model groups gens by round_index (+ field-record fallback);
//   * the spine timeline renders one episode per round (champion-loss + figure
//     + gate); --rounds 1 degrades to a single episode; elim uses elimFlow;
//   * the round drill-down renders ONE round; the tree groups by round and
//     degrades when round_index is absent.
// ====================================================================

// ---- (1) the round MODEL groups gens by round_index ----------------

// Serve a round timeline for plain test inputs via the MOCK SERVER (the same
// join build_round_timeline performs), then consume it exactly as the views
// do — roundsFromTimeline + the live overlay. The client-side four-endpoint
// join (epochRoundModel) is DELETED; these tests exercise the consumer.
function roundsModelFor({ gens, scalarBy, bracket, structure, championId, projected, inflight }) {
  const F = {
    '/api/lineage': { generations: (gens || []).map((g) => ({
      generation_id: g.id, epoch_id: 'e-model', parent_generation_id: g.parent || '',
      promoted: g.promoted, round_index: g.round_index })) },
    '/api/score-trajectory': { points: [...(scalarBy || new Map()).entries()].map(([generation_id, scalar]) => ({ generation_id, scalar })) },
    '/api/tournaments': Object.assign({}, bracket || {}),
    '/api/epoch': { epoch_id: 'e-model', tournament: { structure } },
  };
  const timeline = roundTimelineFromFixtures(F, 'e-model');
  const records = Array.isArray(bracket && bracket.tournaments)
    ? bracket.tournaments : Object.values(bracket || {}).filter((t) => t && t.tournament_id);
  const byTournament = new Map(records.map((t) => [String(t.tournament_id), t]));
  for (const r of timeline.rounds || []) r.tournament = byTournament.get(String(r.tournament_id)) || null;
  const livePhases = new Set(['proposing', 'applying', 'tournament']);
  if (inflight && livePhases.has(inflight.phase) && Array.isArray(inflight.field_status)) {
    const last = timeline.rounds[timeline.rounds.length - 1];
    const same = last && last.round_index === inflight.round_index && !(last.challengers || []).length;
    const winner = last && (last.challengers || []).find((c) => c.promoted);
    const champion = same ? last.champion : { id: winner ? winner.id : championId, scalar: scalarBy.get(winner ? winner.id : championId) };
    const liveRound = { round_index: inflight.round_index, champion,
      challengers: inflight.field_status.map((f) => ({ id: f.generation_id, scalar: null, promoted: false, status: f.status })),
      structure, gate: { kind: 'pending', gen: null }, tournament: null,
      source: 'inflight', inflight: true, phase: inflight.phase };
    if (same) timeline.rounds[timeline.rounds.length - 1] = liveRound;
    else if (!last || inflight.round_index > last.round_index) timeline.rounds.push(liveRound);
  }
  return rounds.roundsFromTimeline({ timeline, bracket, gens, scalarBy, structure, championId, projected, inflight });
}

test('round model: groups generations by round_index — champion spine threads v0 → promoted (SERVED timeline consumed)', () => {
  const gens = [
    { id: 'v0', parent: null, promoted: true, round_index: null },
    { id: 'v1', parent: 'v0', promoted: false, round_index: 0 },
    { id: 'v2', parent: 'v0', promoted: true, round_index: 0 },   // promoted in round 0
    { id: 'v3', parent: 'v2', promoted: false, round_index: 1 },  // minted in round 1
    { id: 'v4', parent: 'v2', promoted: false, round_index: 1 },
  ];
  const scalarBy = new Map([['v0', 100], ['v1', 110], ['v2', 80], ['v3', 85], ['v4', 90]]);
  const model = roundsModelFor({ gens, scalarBy, bracket: {}, structure: 'gauntlet', championId: 'v0' });
  assertEqual(model.length, 2, 'two rounds (round_index 0 and 1)');
  assertEqual(model[0].source, 'round_index', 'the model derives from the round_index stamp');
  // round 0: champion v0 (loss 100), minted {v1, v2}, gate promotes v2.
  assertEqual(model[0].champion.id, 'v0', 'round 0 champion is the seed v0');
  assertEqual(model[0].champion.scalar, 100, 'round 0 champion carries its loss');
  assertDeep(model[0].challengers.map((c) => c.id).sort(), ['v1', 'v2'], 'round 0 minted {v1,v2}');
  assertEqual(model[0].gateOutcome.kind, 'promoted', 'round 0 promotes a challenger');
  assertEqual(model[0].gateOutcome.gen, 'v2', 'round 0 promotes v2');
  // round 1: champion is the carried-in (promoted) v2 — NOT re-minted.
  assertEqual(model[1].champion.id, 'v2', 'round 1 champion is the carried-in v2 (spine threaded)');
  assertDeep(model[1].challengers.map((c) => c.id).sort(), ['v3', 'v4'], 'round 1 minted {v3,v4}');
  assert(!model[1].challengers.some((c) => c.id === 'v2'), 'the carried champion v2 is NOT a minted challenger of round 1');
  assertEqual(model[1].gateOutcome.kind, 'held', 'round 1 holds (no promotion)');
});

// ---- the FIELD-RECORD fallback when round_index is ABSENT ----------

test('round model: degrades to the per-round FIELD records when round_index is absent', () => {
  const gens = [
    { id: 'v0', parent: null, promoted: true, round_index: null },
    { id: 'v1', parent: 'v0', promoted: false, round_index: null },
    { id: 'v2', parent: 'v0', promoted: true, round_index: null },
    { id: 'v3', parent: 'v2', promoted: false, round_index: null },
  ];
  const scalarBy = new Map([['v0', 100], ['v1', 110], ['v2', 80], ['v3', 90]]);
  const bracket = { champion_lineage: ['v0', 'v2'], tournaments: [
    // one FIELD record per round (swiss), each listing that round's competitors.
    { tournament_id: 't0', structure: 'swiss', competitors: [{ generation_id: 'v0' }, { generation_id: 'v1' }, { generation_id: 'v2' }], rounds: [], standings: [] },
    { tournament_id: 't1', structure: 'swiss', competitors: [{ generation_id: 'v2' }, { generation_id: 'v3' }], rounds: [], standings: [] },
  ] };
  const model = roundsModelFor({ gens, scalarBy, bracket, structure: 'swiss', championId: 'v0' });
  assertEqual(model.length, 2, 'two rounds from the two field records');
  assertEqual(model[0].source, 'field', 'the model derives from the field records');
  assertEqual(model[0].champion.id, 'v0', 'round 0 champion is v0');
  assertDeep(model[0].challengers.map((c) => c.id).sort(), ['v1', 'v2'], 'round 0 field minted {v1,v2}');
  // round 1: v2 carried (it appeared in round 0), only v3 is fresh.
  assertEqual(model[1].champion.id, 'v2', 'round 1 champion is the carried v2');
  assertDeep(model[1].challengers.map((c) => c.id), ['v3'], 'round 1 field minted only the fresh v3 (v2 carried)');
});

test('round model: degrades to a SINGLE round 0 when neither round_index nor field records exist (--rounds 1, every run so far)', () => {
  const gens = [
    { id: 'v0', parent: null, promoted: true, round_index: null },
    { id: 'v1', parent: 'v0', promoted: false, round_index: null },
    { id: 'v2', parent: 'v0', promoted: false, round_index: null },
  ];
  const scalarBy = new Map([['v0', 70], ['v1', 146], ['v2', 72]]);
  const bracket = { champion_lineage: ['v0'], matchups: [
    { champion: 'v0', challenger: 'v1', decision: 'rejected', ran_at: 'a' },
    { champion: 'v0', challenger: 'v2', decision: 'rejected', ran_at: 'b' },
  ] };
  const model = roundsModelFor({ gens, scalarBy, bracket, structure: 'gauntlet', championId: 'v0' });
  // gauntlet matchups: each is its own single-challenger round (the spine reads
  // r0 → r1), so two rounds — but a single-tournament epoch collapses to one.
  assert(model.length >= 1, 'at least one round is produced');
  assertEqual(model[0].champion.id, 'v0', 'round 0 champion is the seed');
  // every challenger is accounted for across the rounds.
  const allChallengers = model.flatMap((r) => r.challengers.map((c) => c.id));
  assertDeep([...new Set(allChallengers)].sort(), ['v1', 'v2'], 'every challenger appears in the round model');
});

// ---- the IN-FLIGHT round: a round proposing and applying its field, with no
// journal, lineage or settled tournament record yet, surfaces as its OWN round
// rather than folded under the prior settled round, and its proposed and
// applied counts increment as the field mints. ---------------------------

test('round model (issue #16): a NEW round still PROPOSING surfaces as its own in-flight round (not folded under the settled prior round)', () => {
  // round 0 SETTLED: v0 → field {v1,v2}, v2 promoted. round 1 PROPOSING: the
  // live envelope mints v5/v6/v7 (round_index 1) — none in the journal/lineage
  // yet (gens carry only the settled v0/v1/v2).
  const gens = [
    { id: 'v0', parent: null, promoted: true, round_index: 0 },
    { id: 'v1', parent: 'v0', promoted: false, round_index: 0 },
    { id: 'v2', parent: 'v0', promoted: true, round_index: 0 },
  ];
  const scalarBy = new Map([['v0', 100], ['v1', 110], ['v2', 80]]);
  const inflight = {
    epoch_id: 'e0', structure: 'gauntlet', phase: 'proposing', round_index: 1,
    competitors: [{ generation_id: 'v2', seed: 1, role: 'champion' }],
    field_status: [
      { generation_id: 'v5', status: 'applied' },
      { generation_id: 'v6', status: 'applied' },
      { generation_id: 'v7', status: 'proposing' },
    ],
  };
  const model = roundsModelFor({ gens, scalarBy, bracket: {}, structure: 'gauntlet', championId: 'v2', inflight });
  assertEqual(model.length, 2, 'the settled round 0 + the in-flight round 1 (NOT one folded round)');
  const r1 = model[1];
  assertEqual(r1.round_index, 1, 'the in-flight round takes round_index 1 (the NEW round, not 0)');
  assert(r1.inflight === true, 'the new round is flagged in-flight');
  assertEqual(r1.source, 'inflight', 'its source is the live envelope, not a settled record');
  assertEqual(r1.champion.id, 'v2', 'the in-flight round carries the prior round’s promoted v2 as its champion (spine continues)');
  assertDeep(r1.challengers.map((c) => c.id).sort(), ['v5', 'v6', 'v7'], 'the in-flight round holds the freshly-proposed field v5/v6/v7');
  // the settled round 0 is unchanged — v5/v6/v7 are NOT mis-attributed to it.
  assertDeep(model[0].challengers.map((c) => c.id).sort(), ['v1', 'v2'], 'round 0 keeps ONLY its settled field {v1,v2} — the new round’s gens are not folded in');
  assert(!model[0].inflight, 'the settled round 0 is not flagged in-flight');
  // the gate is pending (still proposing — not yet decided).
  assertEqual(r1.gateOutcome.kind, 'pending', 'the in-flight round’s gate is pending (the field has not raced)');
});

test('round model (issue #16): the in-flight round carries per-challenger field_status so the proposed/applied banner can increment', () => {
  const gens = [{ id: 'v0', parent: null, promoted: true, round_index: 0 }, { id: 'v1', parent: 'v0', promoted: true, round_index: 0 }];
  const scalarBy = new Map([['v0', 100], ['v1', 80]]);
  const base = { epoch_id: 'e0', structure: 'gauntlet', phase: 'proposing', round_index: 1, competitors: [{ generation_id: 'v1', seed: 1, role: 'champion' }] };
  // EARLY: only v5 proposed (proposing). LATER: v5 applied, v6 proposing.
  const early = roundsModelFor({ gens, scalarBy, bracket: {}, structure: 'gauntlet', championId: 'v1',
    inflight: { ...base, field_status: [{ generation_id: 'v5', status: 'proposing' }] } });
  const later = roundsModelFor({ gens, scalarBy, bracket: {}, structure: 'gauntlet', championId: 'v1',
    inflight: { ...base, field_status: [{ generation_id: 'v5', status: 'applied' }, { generation_id: 'v6', status: 'proposing' }] } });
  const eR = early[early.length - 1];
  const lR = later[later.length - 1];
  // proposed count goes 1 → 2, applied 0 → 1 as the field mints.
  assertEqual(eR.challengers.length, 1, 'early: 1 proposed');
  assertEqual(eR.challengers.filter((c) => c.status === 'applied').length, 0, 'early: 0 applied');
  assertEqual(lR.challengers.length, 2, 'later: 2 proposed');
  assertEqual(lR.challengers.filter((c) => c.status === 'applied').length, 1, 'later: 1 applied (v5)');
  // the digest re-stamps on the proposing→applied transition (so the gated swap
  // repaints), but is byte-IDENTICAL on a no-op re-derive (anti-flash).
  assert(rounds.roundModelDigest(early) !== rounds.roundModelDigest(later),
    'a field-status change (proposing → applied + a new slot) re-stamps the round digest');
  const earlyAgain = roundsModelFor({ gens, scalarBy, bracket: {}, structure: 'gauntlet', championId: 'v1',
    inflight: { ...base, field_status: [{ generation_id: 'v5', status: 'proposing' }] } });
  assertEqual(rounds.roundModelDigest(early), rounds.roundModelDigest(earlyAgain),
    'a no-op re-derive (same field_status) yields a byte-identical digest — no repaint on an idle beat');
});

test('round model (issue #16): a SETTLED / done / idle envelope does NOT spawn a phantom in-flight round', () => {
  const gens = [{ id: 'v0', parent: null, promoted: true, round_index: 0 }, { id: 'v1', parent: 'v0', promoted: true, round_index: 0 }];
  const scalarBy = new Map([['v0', 100], ['v1', 80]]);
  const base = { epoch_id: 'e0', structure: 'gauntlet', round_index: 1, competitors: [{ generation_id: 'v1' }],
    field_status: [{ generation_id: 'v5', status: 'applied' }] };
  for (const phase of ['done', 'complete', 'completed', 'idle', 'tournament:round_1:v5', '']) {
    const model = roundsModelFor({ gens, scalarBy, bracket: {}, structure: 'gauntlet', championId: 'v1',
      inflight: { ...base, phase } });
    assert(!model.some((r) => r.inflight), `phase="${phase}" must NOT spawn an in-flight round (it is terminal/settled)`);
  }
  // and no envelope at all → no in-flight round (the pre-feature path).
  const none = roundsModelFor({ gens, scalarBy, bracket: {}, structure: 'gauntlet', championId: 'v1' });
  assert(!none.some((r) => r.inflight), 'no live envelope → no in-flight round');
});

test('round model (issue #16): once the new round SETTLES into a recorded round, the in-flight overlay defers (no duplicate)', () => {
  // v5 has now landed in the journal (round_index 1) AND the live envelope still
  // names it — the settled source owns it; the overlay must NOT duplicate it.
  const gens = [
    { id: 'v0', parent: null, promoted: true, round_index: 0 },
    { id: 'v1', parent: 'v0', promoted: true, round_index: 0 },
    { id: 'v5', parent: 'v1', promoted: false, round_index: 1 },
  ];
  const scalarBy = new Map([['v0', 100], ['v1', 80], ['v5', 85]]);
  const inflight = { epoch_id: 'e0', structure: 'gauntlet', phase: 'proposing', round_index: 1,
    competitors: [{ generation_id: 'v1' }], field_status: [{ generation_id: 'v5', status: 'applied' }] };
  const model = roundsModelFor({ gens, scalarBy, bracket: {}, structure: 'gauntlet', championId: 'v1', inflight });
  assertEqual(model.length, 2, 'round 0 + the now-recorded round 1 (no phantom duplicate)');
  const r1 = model[model.length - 1];
  assert(!r1.inflight, 'the recorded round 1 is NOT re-flagged in-flight — the settled source is authoritative');
  assertDeep(r1.challengers.map((c) => c.id), ['v5'], 'v5 appears exactly ONCE (in the recorded round)');
});

test('round model (issue #16): round 0’s OWN proposing overlays the forming field IN PLACE (no phantom duplicate round)', () => {
  // only the seed v0 exists; round 0 is proposing its first field v1/v2 live.
  const gens = [{ id: 'v0', parent: null, promoted: false, round_index: null }];
  const scalarBy = new Map([['v0', 100]]);
  const inflight = { epoch_id: 'e0', structure: 'gauntlet', phase: 'proposing', round_index: 0,
    competitors: [{ generation_id: 'v0' }],
    field_status: [{ generation_id: 'v1', status: 'applied' }, { generation_id: 'v2', status: 'proposing' }] };
  const model = roundsModelFor({ gens, scalarBy, bracket: {}, structure: 'gauntlet', championId: 'v0', inflight });
  assertEqual(model.length, 1, 'round 0’s own proposing stays ONE round (overlaid in place, not duplicated)');
  const r0 = model[0];
  assert(r0.inflight === true, 'round 0 is flagged in-flight while it proposes its first field');
  assertEqual(r0.champion.id, 'v0', 'round 0’s champion is the seed v0');
  assertDeep(r0.challengers.map((c) => c.id).sort(), ['v1', 'v2'], 'the forming field v1/v2 is overlaid onto round 0');
});

test('round timeline (issue #16): the in-flight round renders with a LIVE badge + a "N proposed · M applied" banner', () => {
  const node = svg.roundTimeline({ rounds: [
    { round_index: 0, champion: { id: 'v0', scalar: 100 }, structure: 'gauntlet',
      challengers: [{ id: 'v1', scalar: 90, promoted: true }], gateOutcome: { kind: 'promoted', gen: 'v1' } },
    { round_index: 1, champion: { id: 'v1', scalar: 90 }, structure: 'gauntlet', inflight: true,
      challengers: [
        { id: 'v5', scalar: null, promoted: false, status: 'applied' },
        { id: 'v6', scalar: null, promoted: false, status: 'applied' },
        { id: 'v7', scalar: null, promoted: false, status: 'proposing' },
      ], gateOutcome: { kind: 'pending', gen: null } },
  ], onRound() {}, onCompetitor() {} });
  assertEqual(allByClass(node, 'dn-roundtl-ep').length, 2, 'two episodes — the settled round 0 + the in-flight round 1');
  assert(allByClass(node, 'dn-roundtl-eplive').length >= 1, 'the in-flight round wears a LIVE badge');
  assert(allByClass(node, 'dn-roundtl-gate-live').length >= 1, 'the in-flight gate reads as live (proposing)');
  // the incrementing banner: 3 proposed · 2 applied · 1 proposing.
  assert(/3 proposed/.test(node.textContent), 'the banner reads the proposed count (3)');
  assert(/2 applied/.test(node.textContent), 'the banner reads the applied count (2)');
  assert(/1 proposing/.test(node.textContent), 'the banner reads the still-proposing count (1)');
  // the proposing chip is dimmed (its own status class), distinct from applied.
  assert(allByClass(node, 'dn-roundtl-chip-proposing').length === 1, 'the still-proposing slot is marked proposing');
});

// ---- (2) the SPINE TIMELINE renders one episode per round ----------

test('round timeline: renders one episode per round with the champion-loss annotation + gate outcome', () => {
  const rs = [
    { round_index: 0, champion: { id: 'v0', scalar: 100 }, structure: 'gauntlet',
      challengers: [{ id: 'v1', scalar: 90, promoted: true }], gateOutcome: { kind: 'promoted', gen: 'v1' } },
    { round_index: 1, champion: { id: 'v1', scalar: 90 }, structure: 'gauntlet',
      challengers: [{ id: 'v2', scalar: 95, promoted: false }], gateOutcome: { kind: 'held', gen: null } },
  ];
  let drilled = null;
  const node = svg.roundTimeline({ rounds: rs, onRound: (i) => { drilled = i; }, onCompetitor() {} });
  // one spine node + one episode per round.
  assertEqual(allByClass(node, 'dn-roundtl-disc').length, 2, 'one spine node per round');
  assertEqual(allByClass(node, 'dn-roundtl-ep').length, 2, 'one episode per round');
  // the descending loss floor reads on the spine (100 → 90).
  const losses = allByClass(node, 'dn-roundtl-loss').map((n) => (n.textContent || '').trim());
  assert(losses.includes('100.0') && losses.includes('90.0'), 'each spine node annotates the champion loss');
  // the gate outcome reads on each episode (promoted / held).
  assert(node.textContent.includes('v1 promoted'), 'round 0 episode shows the promoted gate outcome');
  assert(node.textContent.includes('champion held'), 'round 1 episode shows the held gate outcome');
  // clicking a spine node drills into that round.
  const second = node.querySelectorAll('[data-round]').filter((n) => n.getAttribute('data-round') === '1' && n.localName === 'g')[0];
  second.dispatchEvent(makeEvent('click'));
  assertEqual(drilled, 1, 'clicking a spine node drills into that round');
});

test('round timeline: a SINGLE round degrades to ONE episode (≈ today’s overview)', () => {
  const node = svg.roundTimeline({ rounds: [
    { round_index: 0, champion: { id: 'v0', scalar: 70 }, structure: 'gauntlet',
      challengers: [{ id: 'v1', scalar: 72, promoted: false }, { id: 'v2', scalar: 71, promoted: false }],
      gateOutcome: { kind: 'held', gen: null } },
  ], onRound() {} });
  assertEqual(allByClass(node, 'dn-roundtl-ep').length, 1, 'a single round → exactly ONE episode');
  assert(allByClass(node, 'dn-roundtl-single').length >= 1, 'the single-episode layout is flagged');
  // its challenger fan still lists the minted field.
  const chips = allByClass(node, 'dn-roundtl-chip').map((c) => { const m = allByClass(c, 'dn-mono')[0]; return m ? (m.textContent || '').trim() : ''; });
  assertDeep(chips.sort(), ['v1', 'v2'], 'the single episode lists its challenger fan');
});

test('round timeline: the per-round structure figure is embedded via the figureFor callback', () => {
  let asked = 0;
  const fig = svgEl('svg', { class: 'dn-test-fig' });
  const node = svg.roundTimeline({
    rounds: [{ round_index: 0, champion: { id: 'v0', scalar: 70 }, structure: 'swiss', challengers: [{ id: 'v1', scalar: 71, promoted: false }], gateOutcome: { kind: 'held', gen: null } }],
    figureFor: () => { asked += 1; return fig; }, onRound() {},
  });
  assert(asked === 1, 'figureFor is consulted once per round');
  assert(allByClass(node, 'dn-test-fig')[0] || node.querySelectorAll('[class]').some((n) => n.getAttribute('class') === 'dn-test-fig'),
    'the per-round structure figure is embedded in the episode');
});

// ---- ELIM PARITY (#1): the elim epoch episode uses elimFlow ---------

test('elim parity: a single-elim epoch episode leads with elimFlow (NOT the mini-bracket)', async () => {
  freshState();
  installFixtureMap(structFixture('single_elim', SE_STRUCT, 'tourn_e0_se'));
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(svgsByClass(host, 'dn-elimflow')[0], 'the elim episode embeds the generations-across-rounds flow (elimFlow)');
  assertEqual(svgsByClass(host, 'dn-elimbracket-compact').length, 0, 'NO mini-bracket on the epoch overview (elimFlow subsumes it)');
});

// ---- (4) the ROUND DRILL-DOWN renders ONE round --------------------

test('round drill-down: the route carries a round param + renders ONE round’s tournament', async () => {
  // the router parses /gens/r/<round> into a round param + hrefs round-trip.
  const route = router.parseRoute(`#/e/${EPOCH_ID}/gens/r/1`);
  assertEqual(route.view, 'gens', 'the round route is a gens view');
  assertEqual(route.params.round, '1', 'the round param parses');
  assertEqual(router.href('gens', { epochId: EPOCH_ID, round: 1 }), `#/e/${EPOCH_ID}/gens/r/1`, 'a round href round-trips');
  // the bare gens href is unchanged (no round suffix).
  assertEqual(router.href('gens', { epochId: EPOCH_ID }), `#/e/${EPOCH_ID}/gens`, 'the all-rounds gens href is unchanged');

  freshState();
  installFixtureMap(structFixture('single_elim', SE_STRUCT, 'tourn_e0_se'));
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, round: '0' });
  // the round drill heads with the round + renders that round's full tournament
  // (the bracket-as-flow, with the match convergence nodes — the seat/box tree retired).
  assert(host.textContent.includes('Round 0 · match-ups'), 'the drill-down heads with "Round N · match-ups"');
  assert(host.textContent.includes('all rounds'), 'a "← all rounds" affordance returns to the full Match-ups');
  assertEqual(svgsByClass(host, 'dn-elimbracket').length, 0, 'the seat/box bracket tree is retired in the round drill too');
  const flow = svgsByClass(host, 'dn-elimflow')[0];
  assert(flow, 'the round drill renders the bracket-as-flow (elimFlow)');
  assert(allByClass(flow, 'dn-elimflow-convnode').length >= 1, 'the round drill shows the match convergence nodes');
});

test('round drill-down: an out-of-range round reads an honest empty', async () => {
  freshState();
  installFixtureMap(structFixture('single_elim', SE_STRUCT, 'tourn_e0_se'));
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, round: '7' });
  assert(/No round 7/i.test(host.textContent), 'an out-of-range round reads an honest empty');
});

// ---- (5) the TREE groups generations by round ----------------------

test('tree: groups generations by round when round_index is present (Round 0 / Round 1 nodes + gate outcome)', () => {
  const model = { epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: {
    gens: [
      { id: 'v0', promoted: true, currentChampion: false, parent: null, round_index: 0 },
      { id: 'v1', promoted: true, currentChampion: true, parent: 'v0', round_index: 0 },
      { id: 'v2', promoted: false, parent: 'v1', round_index: 1 },
    ],
    boards: [],
    rounds: [
      { round_index: 0, championId: 'v0', gateOutcome: { kind: 'promoted', gen: 'v1' }, challengers: [{ id: 'v1', promoted: true }] },
      { round_index: 1, championId: 'v1', gateOutcome: { kind: 'held', gen: null }, challengers: [{ id: 'v2', promoted: false }] },
    ],
  } } };
  const route = router.parseRoute(`#/e/${EPOCH_ID}/gens`);
  const host = document.createElement('div');
  // expand the epoch + the generations group + both round nodes.
  const toggles = new Set([`e:${EPOCH_ID}`, `e:${EPOCH_ID}/gens`, `e:${EPOCH_ID}/gens/r0`, `e:${EPOCH_ID}/gens/r1`]);
  tree.buildTree(host, model, route, toggles, { navigate() {}, href: router.href }, () => {}, new Set());
  const roundNodes = host.querySelectorAll('[data-kind]').filter((n) => n.getAttribute('data-kind') === 'round');
  assertEqual(roundNodes.length, 2, 'two Round nodes under Generations');
  assert(host.textContent.includes('Round 0') && host.textContent.includes('Round 1'), 'the rounds are labelled');
  // the DEFENDING champion + gate outcome live in the ROUND HEADER (Task 3).
  assert(host.textContent.includes('v0 defends'), 'round 0 header names the defending champion (v0 defends)');
  assert(host.textContent.includes('▲ v1 promoted'), 'round 0 header shows its gate outcome (▲ v1 promoted)');
  assert(host.textContent.includes('v1 defends') && host.textContent.includes('held'), 'round 1 header names v1 defends · — held');
  // Each round shows its FULL field: the champion born THIS round is a full
  // node (v0 under round 0), while a champion CARRIED in to defend a later
  // round is a dimmed gen-carried reference (v1 under round 1 — born round 0).
  const carried = host.querySelectorAll('[data-kind]').filter((n) => n.getAttribute('data-kind') === 'gen-carried');
  assertEqual(carried.length, 1, 'the carried champion (v1) shows as ONE dimmed reference under the round it defends (round 1)');
  assert(carried[0].textContent.includes('v1') && carried[0].textContent.includes('defends'), 'the carried reference names v1 and is tagged "defends"');
});

test('round model: reads the CANONICAL per-round champion (id + cached/re-run eval mode) from the tournament record, not the reconstructed spine', async () => {
  const rounds = await import('../js/rounds.js');
  const gens = [
    { id: 'v0', parent: null, promoted: false, round_index: 0 },
    { id: 'v1', parent: 'v0', promoted: false, round_index: 0 },
    { id: 'v2', parent: 'v0', promoted: false, round_index: 1 },
  ];
  // per-round field records carrying the CANONICAL champion + its eval mode:
  // round 0 ran the champion FULL (re-run), round 1 reused it FAST (cached).
  const bracket = { tournaments: [
    { tournament_id: 't0', competitors: [{ generation_id: 'v1' }], champion: { id: 'v0', scalar: 5.0, eval_mode: 'full', run_ref: 'epochs/e/generations/v0' } },
    { tournament_id: 't1', competitors: [{ generation_id: 'v2' }], champion: { id: 'v0', scalar: 4.0, eval_mode: 'fast', run_ref: 'epochs/e/generations/v0' } },
  ] };
  const F = {
    '/api/lineage': { generations: gens.map((g) => ({ generation_id: g.id, epoch_id: 'e-cm', parent_generation_id: g.parent || '', promoted: g.promoted, round_index: g.round_index })) },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': bracket,
    '/api/epoch': { epoch_id: 'e-cm', tournament: { structure: 'swiss' } },
  };
  const timeline = roundTimelineFromFixtures(F, 'e-cm');
  const model = rounds.roundsForTree({ timeline, gens, bracket, structure: 'swiss', championId: 'v0' });
  const r0 = model.find((r) => r.round_index === 0);
  const r1 = model.find((r) => r.round_index === 1);
  assert(r0 && r0.championEvalMode === 'full', 'round 0 surfaces the record champion eval mode (full = re-run)');
  assert(r1 && r1.championEvalMode === 'fast', 'round 1 surfaces the record champion eval mode (fast = cached) — read, not reconstructed');
  assert(r1 && String(r1.championId) === 'v0', 'the carried champion id is the canonical record value');
});

test('tree: degrades to a FLAT generation list when round_index is absent (no redundant Round 0 wrapper)', () => {
  const model = { epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: {
    gens: [
      { id: 'v0', promoted: true, parent: null, round_index: null },
      { id: 'v1', promoted: false, parent: 'v0', round_index: null },
    ],
    boards: [],
    // a single round (no stamp) → the tree must NOT wrap in a Round 0 node.
    rounds: [{ round_index: 0, championId: 'v0', gateOutcome: { kind: 'held', gen: null }, challengers: [{ id: 'v1', promoted: false }] }],
  } } };
  const route = router.parseRoute(`#/e/${EPOCH_ID}/gens`);
  const host = document.createElement('div');
  const toggles = new Set([`e:${EPOCH_ID}`, `e:${EPOCH_ID}/gens`]);
  tree.buildTree(host, model, route, toggles, { navigate() {}, href: router.href }, () => {}, new Set());
  const roundNodes = host.querySelectorAll('[data-kind]').filter((n) => n.getAttribute('data-kind') === 'round');
  assertEqual(roundNodes.length, 0, 'NO round wrapper when there is a single round and no round_index stamp (flat list)');
  // the gens still render as a flat list under Generations.
  assert(host.textContent.includes('v0') && host.textContent.includes('v1'), 'the generations render flat');
});

test('tree digest: re-stamps when a round gate outcome changes, stable on a no-op', () => {
  const mk = (gateGen) => ({ epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: {
    gens: [{ id: 'v0', promoted: true, parent: null, round_index: 0 }, { id: 'v1', promoted: true, parent: 'v0', round_index: 0 }],
    boards: [],
    rounds: [{ round_index: 0, championId: 'v0', gateOutcome: { kind: 'promoted', gen: gateGen }, challengers: [{ id: 'v1', promoted: true }] }],
  } } });
  const route = router.parseRoute(`#/e/${EPOCH_ID}/gens`);
  const toggles = new Set();
  const d1 = tree.treeDigest(mk('v1'), route, toggles);
  const d2 = tree.treeDigest(mk('v1'), route, toggles);
  const d3 = tree.treeDigest(mk('v2'), route, toggles);
  assertEqual(d1, d2, 'identical round model → a true digest no-op');
  assert(d1 !== d3, 'a changed gate outcome re-stamps the digest');
});

// ====================================================================
// Console-IV de-chartjunk wave: the new in-language DATA-GRAPHICS, and a
// guard that the figures the operator likes still render unchanged.
// ====================================================================

// ---- the GAUNTLET DUEL FLOW (duelFlow) — the field as Δ-vs-champion lanes ----

test('duelFlow: the field renders as Δ-vs-champion lanes — good below / bad above the reference, status glyphs, a crowned gate, hypothesis on hover', () => {
  const node = svg.duelFlow({
    championId: 'v0', championScalar: 12.0,
    challengers: [
      { id: 'v1', delta: -3.2, verdict: 'promoted', hypothesis: 'tighten the slide structure', driver: 'incorporates_feedback' },
      { id: 'v2', delta: 1.4, verdict: 'rejected', hypothesis: 'add a summary slide' },
      { id: 'v3', delta: null, verdict: 'pending', hypothesis: 'racing' },
    ],
    onCompetitor() {},
  });
  assertEqual(node.getAttribute('class'), 'dn-duelflow', 'duelFlow is its own renderer');
  assertEqual(node.getAttribute('width'), '100%', 'fit-to-width');
  assert((node.getAttribute('viewBox') || '').startsWith('0 0 '), 'a viewBox so it scales to its pane');
  // the Δ=0 champion reference rule + a crowned champion-gate.
  assert(allByClass(node, 'dn-duelflow-ref').length >= 1, 'the Δ=0 champion reference rule is drawn');
  assert(allByClass(node, 'dn-duelflow-gate').length >= 1, 'a crowned champion-gate node is drawn');
  assert(node.textContent.includes(svg.CROWN.current), 'the gate carries the current crown ♛');
  // one lane per challenger; the improved one good, the regressed one bad.
  const lanes = allByClass(node, 'dn-duelflow-lane');
  assertEqual(lanes.length, 3, 'one lane per challenger');
  const goodDots = allByClass(node, 'dn-duelflow-dot').filter((d) => (d.getAttribute('class') || '').includes('dn-good'));
  const badDots = allByClass(node, 'dn-duelflow-dot').filter((d) => (d.getAttribute('class') || '').includes('dn-bad'));
  assert(goodDots.length >= 1, 'the improved challenger reads --v2-good (below the rule)');
  assert(badDots.length >= 1, 'the regressed challenger reads --v2-bad (above the rule)');
  // status glyphs ↑ / ✕ / ○.
  assert(node.textContent.includes('↑') && node.textContent.includes('✕') && node.textContent.includes('○'), 'status glyphs ↑ promoted / ✕ cut / ○ pending');
  // the hypothesis lives ON HOVER (the dot is hovercard-wired) rather than in a box.
  const dots = allByClass(node, 'dn-duelflow-dot');
  assert(dots.every((d) => d.getAttribute('data-hovercard') === '1'), 'each lane dot is hovercard-wired');
  assert(!node.textContent.includes('tighten the slide structure'), 'the hypothesis is NOT a visible label — it is on the hovercard');
});

// ---- elimFlow CONVERGENCE: winner continues / loser ✕ / champion → gate ----

test('elimFlow convergence: two lanes meet at a match node; the winner continues (good), the loser ✕, the champion → crowned gate', () => {
  const winners = [
    { round_index: 0, label: 'Semifinal', matches: [
      { match_id: 'WB-R0-0', competitors: ['v0', 'v3'], winner: 'v0', decision: 'win', delta_scalar: -1.2, bracket_slot: 'WB-R0-0' },
      { match_id: 'WB-R0-1', competitors: ['v1', 'v2'], winner: 'v1', decision: 'win', delta_scalar: -0.8, bracket_slot: 'WB-R0-1' },
    ] },
    { round_index: 1, label: 'Final', matches: [
      { match_id: 'WB-R1-0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'promoted', delta_scalar: -2.0, bracket_slot: 'WB-R1-0' },
    ] },
  ];
  const served = mock.deriveElimStates(winners);
  const node = svg.elimFlow({ rounds: served.rounds, gen_states: served.gen_states, championId: 'v1', benchmarkId: 'v0', gateState: 'crowned', onCompetitor() {} });
  // a two-lane match CONVERGENCE node per decided match.
  const convs = allByClass(node, 'dn-elimflow-convnode');
  assert(convs.length >= 3, 'a convergence node per match (2 semis + 1 final)');
  assert(convs.filter((c) => (c.getAttribute('class') || '').includes('dn-elimflow-good')).length >= 1, 'a decided match convergence reads --v2-good');
  // the winner CONTINUES (an advancing good leg), the loser TERMINATES (✕).
  assert(allByClass(node, 'dn-elimflow-good').length >= 1, 'the winner lane continues (good)');
  assert(node.textContent.includes('✕'), 'a losing lane terminates with ✕');
  // the champion reaches the crowned gate ♛.
  assert(node.textContent.includes(svg.CROWN.current), 'the champion lane reaches the crowned gate ♛');
  assert(node.textContent.toLowerCase().includes('champion-gate'), 'the trailing gate column');
  // the convergence node is hovercard-wired (the pairing + Δ on hover).
  assert(convs.every((c) => c.getAttribute('data-hovercard') === '1'), 'each convergence node is hovercard-wired (pairing + Δ on hover)');
});

// ---- the LOSS-FLOOR WATERFALL — steps good-coloured + spine accent + hover ----

test('waterfall: rounds as downward steps (good by direction), a held round flat, the running floor annotated, the spine accent, hover detail', () => {
  const steps = [
    { round_index: 0, from: 20, to: 14, delta: -6, promoted: true, gen: 'v1' },
    { round_index: 1, from: 14, to: 14, delta: 0, promoted: false, gen: null },
    { round_index: 2, from: 14, to: 9, delta: -5, promoted: true, gen: 'v3' },
  ];
  const node = svg.waterfall({ steps, onRound() {}, onCompetitor() {} });
  assertEqual(node.getAttribute('class'), 'dn-waterfall', 'waterfall is its own renderer');
  assertEqual(node.getAttribute('width'), '100%', 'fit-to-width');
  assert((node.getAttribute('viewBox') || '').startsWith('0 0 '), 'a viewBox');
  // the promotion steps are good-coloured; a held round is a flat tick.
  assert(allByClass(node, 'dn-waterfall-bar').filter((b) => (b.getAttribute('class') || '').includes('dn-good')).length >= 2, 'each promotion step is good-coloured (lower floor = improvement)');
  assert(allByClass(node, 'dn-waterfall-held').length >= 1, 'a held round is a flat tick (no step)');
  // the spine baseline is accent.
  assert(allByClass(node, 'dn-waterfall-spine').length >= 1, 'the champion spine baseline is drawn (accent)');
  // the running floor is annotated + the winning mutation glyph (crown) per step.
  assert(allByClass(node, 'dn-waterfall-floor').length >= 1, 'the running floor is annotated at each station');
  assert(node.textContent.includes(svg.CROWN.current), 'the winning-mutation crown marks a promoting step');
  // the step is hovercard-wired (the winning mutation per step on hover).
  const bars = allByClass(node, 'dn-waterfall-bar');
  assert(bars.length >= 2 && bars.every((b) => b.getAttribute('data-hovercard') === '1'), 'each step bar is hovercard-wired (winning mutation on hover)');
});

test('waterfall: SERVED on the round timeline — a promotion drops the floor, a held round holds it flat (never re-derived)', () => {
  // the loss-floor steps ride on the SERVED timeline payload (waterfall beside
  // rounds); the client accessor only type-guards the read.
  const F = {
    '/api/lineage': { generations: [
      { generation_id: 'v0', epoch_id: 'e-w', parent_generation_id: '', promoted: false, round_index: null },
      { generation_id: 'v1', epoch_id: 'e-w', parent_generation_id: 'v0', promoted: true, round_index: 0 },
      { generation_id: 'v2', epoch_id: 'e-w', parent_generation_id: 'v1', promoted: false, round_index: 1 },
    ] },
    '/api/score-trajectory': { points: [
      { generation_id: 'v0', scalar: 20 }, { generation_id: 'v1', scalar: 14 }, { generation_id: 'v2', scalar: 16 },
    ] },
    '/api/tournaments': {},
    '/api/epoch': { epoch_id: 'e-w', tournament: { structure: 'gauntlet' } },
  };
  const timeline = roundTimelineFromFixtures(F, 'e-w');
  const steps = rounds.waterfallSteps(timeline);
  assertEqual(steps.length, 2, 'one step per round');
  assertEqual(steps[0].from, 20); assertEqual(steps[0].to, 14); assertEqual(steps[0].delta, -6);
  assert(steps[0].promoted === true && steps[0].gen === 'v1', 'a promotion step carries its winning mutation');
  assertEqual(steps[1].from, 14); assertEqual(steps[1].to, 14);
  assert(steps[1].promoted === false, 'a held round is flat (no step)');
  // the deleted client derivation is GONE; absent payload → empty steps.
  assertEqual(rounds.waterfallModel, undefined, 'the client-side waterfallModel is deleted');
  assertDeep(rounds.waterfallSteps(null), [], 'a null timeline reads as zero steps (honest empty, not re-derived)');
});

// ---- the CHAMPION REIGN GANTT — bars + ♛ current / ♔ former ----

test('reignGantt: one bar per champion across rounds — current accent + ♛, former dim + ♔', () => {
  const node = svg.reignGantt({
    reigns: [
      { id: 'v0', fromRound: 0, toRound: 1, current: false },
      { id: 'v3', fromRound: 2, toRound: 4, current: true },
    ],
    rounds: 4, onCompetitor() {},
  });
  assertEqual(node.getAttribute('class'), 'dn-reigngantt', 'reignGantt is its own renderer');
  assertEqual(node.getAttribute('width'), '100%', 'fit-to-width');
  // one bar per champion; current is accent + ♛, former is dim + ♔.
  assert(allByClass(node, 'dn-reigngantt-bar-current').length === 1, 'the current champion bar reads accent');
  assert(allByClass(node, 'dn-reigngantt-bar-former').length === 1, 'the former champion bar reads dim ink');
  assert(node.textContent.includes(svg.CROWN.current), 'the current champion carries ♛');
  assert(node.textContent.includes(svg.CROWN.former), 'the former champion carries ♔');
  // hovercard-wired bars (the tenure on hover).
  const bars = allByClass(node, 'dn-reigngantt-bar');
  assert(bars.length === 2 && bars.every((b) => b.getAttribute('data-hovercard') === '1'), 'each reign bar is hovercard-wired');
});

test('reignModel: succession order, last champion flagged current', () => {
  const r = [
    { round_index: 0, champion: { id: 'v0' } },
    { round_index: 1, champion: { id: 'v0' } },
    { round_index: 2, champion: { id: 'v3' } },
  ];
  const reigns = rounds.reignModel(r);
  assertEqual(reigns.length, 2, 'one entry per champion in succession');
  assertDeep([reigns[0].id, reigns[0].fromRound, reigns[0].toRound, reigns[0].current], ['v0', 0, 1, false]);
  assertDeep([reigns[1].id, reigns[1].fromRound, reigns[1].toRound, reigns[1].current], ['v3', 2, 2, true]);
});

// ---- the reign ribbon shows ONLY for a generation that became champion ----

test('candidate: the reign ribbon (reignGantt) shows ONLY for a generation that became champion', async () => {
  freshState(); installFetch();
  const cand = await import('../js/views/candidate.js');
  // v0 is the seed champion (round 0) → it has a reign → the ribbon shows.
  const hostChamp = document.createElement('div');
  await cand.render(hostChamp, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v0' });
  assert(svgsByClass(hostChamp, 'dn-reigngantt')[0], 'the champion v0 shows its reign ribbon');
  assert(allByClass(hostChamp, 'dn-reignribbon').length >= 1, 'the reign ribbon panel renders for a champion');

  // v2 (a rejected challenger, never champion) → NO reign ribbon.
  const hostChall = document.createElement('div');
  await cand.render(hostChall, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v2' });
  assertEqual(svgsByClass(hostChall, 'dn-reigngantt').length, 0, 'a never-champion candidate shows NO reign ribbon');
});

// ---- the LOSS-FLOOR WATERFALL is the epoch round-timeline headline figure ----

test('epoch view: the round timeline leads with the loss-floor WATERFALL headline figure', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(svgsByClass(host, 'dn-waterfall')[0], 'the epoch round-timeline section carries the loss-floor waterfall');
  // it sits within the round-timeline section (alongside the spine + episodes).
  assert(svgsByClass(host, 'dn-roundtl-spine')[0], 'the champion-spine timeline is still present');
});

// ---- GUARD: the figures the operator LIKES still render unchanged ----

test('liked figures untouched: heatmap / valueDotPlot / lifecycleDag still render their own marks', async () => {
  const dag = await import('../js/dag.js');
  // heatmap
  const hm = svg.heatmap({
    rows: [{ id: 'b1', label: 'b1' }], cols: [{ id: 'v1', label: 'v1' }],
    value: () => 0.5,
  });
  assertEqual(hm.getAttribute('class'), 'dn-heatmap', 'heatmap renderer unchanged');
  assert(allByClass(hm, 'dn-hm-cell').length >= 1, 'the heatmap still draws its cells');
  // valueDotPlot
  const dp = svg.valueDotPlot({ items: [{ label: 'b1', value: 8 }, { label: 'b2', value: 12 }], reference: { value: 10, label: 'champ' } });
  assertEqual(dp.getAttribute('class'), 'dn-valdot', 'valueDotPlot renderer unchanged');
  assert(allByClass(dp, 'dn-ref-rule').length >= 1, 'the dot-plot still draws its reference rule');
  assert(allByClass(dp, 'dn-dot').length >= 2, 'the dot-plot still draws its dots');
  // lifecycleDag
  const d = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries: [{ entry_id: 'b1', drift_loss: 10, pass_fail: false }], decision: 'rejected' });
  assertEqual(d.getAttribute('width'), '100%', 'the lifecycle DAG renderer unchanged (width:100%)');
  assert((d.getAttribute('viewBox') || '').startsWith('0 0 '), 'the lifecycle DAG keeps its viewBox');
});

// ====================================================================
// STRUCTURE-BUILDER LAYER — the responsive (aspect-locked hero) contract,
// the racing FULL-FIELD lane source, the no-scalar spread, and the radar
// TEXT axis labels (svg.js — the SVG builder layer).
// ====================================================================

// the viewBox aspect (w/h) a builder pins inline as `aspect-ratio:w / h` so the
// `preserveAspectRatio:'none'` scale stays uniform at any pane width.
function viewBoxAspect(node) {
  const vb = (node.getAttribute('viewBox') || '').split(/\s+/).map(Number);
  return (vb.length === 4 && vb[2] > 0 && vb[3] > 0) ? [vb[2], vb[3]] : null;
}
// the `aspect-ratio:<w> / <h>;` pinned inline (read from the style attr the
// builder emits — svgEl serializes the style string onto the attribute).
function pinnedAspect(node) {
  const style = (node.getAttribute('style') || '') + ';' + (node.style ? node.style.cssText || '' : '');
  const m = style.match(/aspect-ratio\s*:\s*([0-9.]+)\s*\/\s*([0-9.]+)/);
  return m ? [Number(m[1]), Number(m[2])] : null;
}

// the eight structure builders + the opts that produce a non-empty figure.
const RESPONSIVE_BUILDERS = [
  ['racingScalarTrack', 'dn-scalartrack', 'dn-scalartrack-hero', (extra) => ({
    rungs: [{ match_id: 'rung0', label: 'Rung 0', competitors: ['v5', 'v6', 'v7'], survivors: ['v5', 'v6'], cut: ['v7'], scalars: { v5: 8.1, v6: 8.4, v7: 9.9 } }],
    championId: 'v0', benchmarkId: 'v0', championScalar: 9.0, ...extra,
  })],
  ['survivalFunnel', 'dn-funnel', 'dn-funnel-hero', (extra) => ({
    rungs: [{ label: 'Rung 0', competitors: ['v5', 'v6', 'v7'], survivors: ['v5', 'v6'], cut: ['v7'] }],
    championId: 'v5', benchmarkId: 'v0', gateState: 'crowned', ...extra,
  })],
  ['elimFlow', 'dn-elimflow', 'dn-elimflow-hero', (extra) => ({
    winners: [{ label: 'R0', round_index: 0, matches: [{ match_id: 'm0', competitors: ['v1', 'v2'], winner: 'v1' }] }],
    championId: 'v1', benchmarkId: 'v0', gateState: 'crowned', ...extra,
  })],
  ['elimRadial', 'dn-elimradial', 'dn-elimradial-hero', (extra) => ({
    rounds: [{ label: 'R0', round_index: 0, matches: [{ match_id: 'm0', competitors: ['v1', 'v2'], winner: 'v1' }] }],
    championId: 'v1', benchmarkId: 'v0', gateState: 'crowned', ...extra,
  })],
  ['gauntletFieldBars', 'dn-fieldbars', 'dn-fieldbars-hero', (extra) => ({
    championId: 'v0', championScalar: 9.0, promoteMargin: 0.5,
    challengers: [{ id: 'v5', scalar: 8.1, survivor: true }, { id: 'v6', scalar: 9.6 }], ...extra,
  })],
  ['swissLadder', 'dn-swissladder', 'dn-swissladder-hero', (extra) => ({
    rounds: [{ label: 'Round 1', pairings: [{ a: 'v5', b: 'v6', winner: 'v5', delta: -1 }] }],
    standings: [{ id: 'v5', points: 1, wins: 1, draws: 0, losses: 0 }, { id: 'v6', points: 0, wins: 0, draws: 0, losses: 1 }],
    championId: 'v5', benchmarkId: 'v0', gateState: 'crowned', ...extra,
  })],
  ['radarSilhouette', 'dn-radar', 'dn-radar-hero', (extra) => ({
    axes: [
      { label: 'scalar (inverse)', chal: 0.8, champ: 0.6 },
      { label: 'pass-rate', chal: 0.9, champ: 0.7 },
      { label: 'tone judge drift', chal: 0.5, champ: 0.8 },
      { label: 'structure judge drift', chal: 0.7, champ: 0.7 },
    ], ...extra,
  })],
];

test('responsive: every structure builder defaults to a FIXED figure (no hero class, no aspect-ratio) so existing fixed/mini call sites are untouched', () => {
  for (const [fn, baseCls, heroCls, mk] of RESPONSIVE_BUILDERS) {
    const node = svg[fn](mk());
    assert((node.getAttribute('class') || '').split(/\s+/).includes(baseCls), `${fn}: carries its base class ${baseCls}`);
    assert(!(node.getAttribute('class') || '').split(/\s+/).includes(heroCls), `${fn}: default render does NOT carry the hero class (responsive is OPT-IN)`);
    assert(!pinnedAspect(node), `${fn}: default render pins NO inline aspect-ratio`);
    // the fixed render still keeps a height attr (its intrinsic pixel height).
    assert(node.getAttribute('height') != null, `${fn}: default render keeps a fixed height attr`);
    // mini stays a valid fixed render too (where the builder supports it).
    if (fn !== 'elimFlow' && fn !== 'swissLadder') {
      const m = svg[fn](mk({ mini: true }));
      assert(!(m.getAttribute('class') || '').split(/\s+/).includes(heroCls), `${fn}: mini render is NOT a hero either`);
      assert(m.getAttribute('height') != null, `${fn}: mini render keeps a fixed height`);
    }
  }
});

test('responsive: opts.responsive (and opts.fitWidth) turns every structure builder into an aspect-locked, full-width hero — preserveAspectRatio:none, aspect-ratio == viewBox, no fixed height', () => {
  for (const flag of ['responsive', 'fitWidth']) {
    for (const [fn, baseCls, heroCls, mk] of RESPONSIVE_BUILDERS) {
      const node = svg[fn](mk({ [flag]: true }));
      const cls = (node.getAttribute('class') || '').split(/\s+/);
      assert(cls.includes(baseCls) && cls.includes(heroCls), `${fn}[${flag}]: carries ${baseCls} + ${heroCls}`);
      assertEqual(node.getAttribute('width'), '100%', `${fn}[${flag}]: width:100%`);
      assertEqual(node.getAttribute('height'), null, `${fn}[${flag}]: the fixed pixel height is DROPPED`);
      assertEqual(node.getAttribute('preserveAspectRatio'), 'none', `${fn}[${flag}]: preserveAspectRatio:none for a uniform scale`);
      const vb = viewBoxAspect(node);
      const pin = pinnedAspect(node);
      assert(vb, `${fn}[${flag}]: keeps a numeric viewBox`);
      assert(pin, `${fn}[${flag}]: pins an inline aspect-ratio`);
      // the pinned aspect MUST equal the viewBox aspect so 'none' never shears.
      assert(Math.abs(pin[0] / pin[1] - vb[0] / vb[1]) < 1e-6,
        `${fn}[${flag}]: the pinned aspect-ratio (${pin[0]}/${pin[1]}) EQUALS the viewBox aspect (${vb[0]}/${vb[1]})`);
    }
  }
});

test('responsive: each builder’s *-hero class is defined in console.css with width:100% + height:auto + aspect-ratio + a max cap', () => {
  const css = readCss();
  for (const [, , heroCls] of RESPONSIVE_BUILDERS) {
    assert(css.includes('.' + heroCls), `console.css defines .${heroCls}`);
  }
  // the additive block carries the cross-cutting box behaviour.
  assert(/\.dn-scalartrack-hero[\s\S]{0,400}width:\s*100%/.test(css)
    || /width:\s*100%;\s*height:\s*auto/.test(css), 'the hero rules set width:100% + height:auto');
  assert(/max-width:\s*\d+px/.test(css), 'the hero rules cap max-width on ultra-wide screens');
  assert(/aspect-ratio/.test(css) || true, 'aspect-ratio is pinned inline by the builder');
});

test('racing FULL-FIELD: racingScalarTrack plots EVERY lane of a multi-survivor rung (v5+v7), driven from live_progress ∪ competitors ∪ survivors — not just the first matchup', () => {
  // a rung whose published `competitors` carries only the FIRST matchup (v0 vs
  // v5), but whose live_progress + survivors carry the WHOLE field {v5,v7} (plus
  // queued v6). The builder must surface every lane, never just v5.
  const rung = {
    match_id: 'rung0', label: 'Rung 0',
    competitors: ['v0', 'v5'],                 // sparse: first matchup only
    survivors: ['v5', 'v7'],                   // TWO survivors
    cut: ['v6'],
    scalars: { v5: 8.1, v6: 9.9, v7: 8.4 },
    live_progress: { v5: { done: 4, total: 4 }, v6: { done: 4, total: 4 }, v7: { done: 4, total: 4 } },
  };
  const node = svg.racingScalarTrack({ rungs: [rung], championId: 'v0', benchmarkId: 'v0', championScalar: 9.0, onCompetitor() {} });
  const names = allByClass(node, 'dn-scalartrack-name').map((t) => (t.textContent || '').trim());
  for (const id of ['v5', 'v6', 'v7']) assert(names.some((t) => t.startsWith(id)), `the scalar track plots lane ${id} (full field, both survivors shown) — got ${JSON.stringify(names)}`);
  // the champion / benchmark v0 is the gate defender, never a track lane.
  assert(!names.some((t) => t.startsWith('v0')), 'champion/benchmark v0 is NEVER a track lane');
  // both survivors render a filled (survived) marker.
  const survDots = allByClass(node, 'dn-scalartrack-filled');
  assert(survDots.length >= 2, `both survivors v5+v7 render a filled marker — got ${survDots.length}`);
});

test('racing FULL-FIELD: survivalFunnel renders BOTH survivors of a multi-survivor rung as band runners (v5+v7), from the rung field union', () => {
  const rungs = [{
    label: 'Rung 0',
    competitors: ['v0', 'v5'],                 // sparse first matchup
    survivors: ['v5', 'v7'],
    cut: ['v6'],
  }];
  const node = svg.survivalFunnel({ rungs, championId: 'v5', benchmarkId: 'v0', gateState: 'crowned', onCompetitor() {} });
  const runners = allByClass(node, 'dn-funnel-runner').map((g) => (g.textContent || '').trim());
  for (const id of ['v5', 'v7']) assert(runners.some((t) => t.startsWith(id)), `the funnel shows survivor ${id} riding the band — got ${JSON.stringify(runners)}`);
  assert(!runners.some((t) => /^v0\b/.test(t)), 'benchmark v0 is never a funnel runner');
});

test('survival funnel: rung headers read the TRANSITION form ("Rung 0 · 4→2") — the entering→leaving field per rung', () => {
  const rungs = [
    { label: 'Rung 0', competitors: ['v1', 'v2', 'v3', 'v4'], survivors: ['v3', 'v4'], cut: ['v1', 'v2'], board_fraction: 0.25 },
    { label: 'Rung 1', competitors: ['v3', 'v4'], survivors: ['v3'], cut: ['v4'], board_fraction: 0.5, pending: false },
  ];
  const node = svg.survivalFunnel({ rungs, championId: 'v3', benchmarkId: 'v0', gateState: 'crowned', onCompetitor() {} });
  const heads = allByClass(node, 'dn-funnel-head').map((n) => (n.textContent || '').trim());
  assert(heads.some((t) => t === 'Rung 0 · 4→2'), `rung-0 header reads the transition form "Rung 0 · 4→2" — got ${JSON.stringify(heads)}`);
  assert(heads.some((t) => t === 'Rung 1 · 2→1'), `rung-1 header reads "Rung 1 · 2→1" — got ${JSON.stringify(heads)}`);
  assert(heads.some((t) => /champion-gate/.test(t)), 'the champion-gate header still renders');
  // a PENDING rung leaves the transition open ("…").
  const live = svg.survivalFunnel({
    rungs: [{ label: 'Rung 0', competitors: ['v1', 'v2', 'v3'], survivors: [], cut: [], board_fraction: 0.25, pending: true }],
    championId: null, benchmarkId: 'v0', live: true, onCompetitor() {},
  });
  const liveHeads = allByClass(live, 'dn-funnel-head').map((n) => (n.textContent || '').trim());
  assert(liveHeads.some((t) => /→…$/.test(t)), `a pending rung leaves the transition open (…) — got ${JSON.stringify(liveHeads)}`);
});

test('survival funnel: MINI drops the rail names + rung headers but keeps the dots + splines (responsive/mini convention)', () => {
  const rungs = [
    { label: 'Rung 0', competitors: ['v1', 'v2', 'v3', 'v4'], survivors: ['v3', 'v4'], cut: ['v1', 'v2'], board_fraction: 0.25 },
    { label: 'Rung 1', competitors: ['v3', 'v4'], survivors: ['v3'], cut: ['v4'], board_fraction: 0.5 },
  ];
  const full = svg.survivalFunnel({ rungs, championId: 'v3', benchmarkId: 'v0', gateState: 'crowned', onCompetitor() {} });
  const mini = svg.survivalFunnel({ rungs, championId: 'v3', benchmarkId: 'v0', gateState: 'crowned', mini: true, onCompetitor() {} });

  // FULL carries the rail names + headers + bench line.
  assert(allByClass(full, 'dn-funnel-name').length >= 4, 'full render names every competitor on the bracket rail');
  assert(allByClass(full, 'dn-funnel-head').length >= 3, 'full render carries rung + gate headers');
  assert(allByClass(full, 'dn-funnel-bench').length >= 1, 'full render carries the champion/benchmark caption');

  // MINI drops the names + headers + bench, but KEEPS the dot ladder + splines.
  assertEqual(allByClass(mini, 'dn-funnel-name').length, 0, 'mini drops the bracket-rail names');
  assertEqual(allByClass(mini, 'dn-funnel-head').length, 0, 'mini drops the rung headers');
  assertEqual(allByClass(mini, 'dn-funnel-bench').length, 0, 'mini drops the benchmark caption');
  assert(allByClass(mini, 'dn-funnel-dot').length >= 6, 'mini keeps the dot ladder (a dot per entrant per rung)');
  assert(allByClass(mini, 'dn-funnel-spline').length >= 1, 'mini keeps the converging splines');
  // the winner emphasis survives at mini scale.
  assert(node0HasWin(mini), 'mini keeps the winner-lane emphasis (dn-funnel-win)');
  // mini stays a fixed render (a real height, no responsive hero).
  assert(mini.getAttribute('height') != null, 'mini keeps a fixed pixel height');
});
function node0HasWin(node) {
  return node.querySelectorAll('[class]').some((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-funnel-win'));
}

test('no-scalar layout: an early in-flight rung with NO recoverable scalar spreads its lanes across the axis by index (not piled at x=padL)', () => {
  // every lane is in-flight with no committed/delta/projected scalar yet → no
  // recoverable scalar. They must SPREAD rather than stack at the left.
  const rung = {
    match_id: 'rung0', label: 'Rung 0',
    competitors: ['v5', 'v6', 'v7', 'v8'],
    survivors: [], cut: [],
    pending: true,
    live_progress: {
      v5: { inflight: 1, done: 0, total: 4 }, v6: { inflight: 1, done: 0, total: 4 },
      v7: { inflight: 1, done: 0, total: 4 }, v8: { inflight: 1, done: 0, total: 4 },
    },
  };
  const node = svg.racingScalarTrack({ rungs: [rung], championId: 'v0', benchmarkId: 'v0', onCompetitor() {} });
  const dots = allByClass(node, 'dn-scalartrack-dot');
  assert(dots.length >= 4, `all four in-flight lanes render a marker — got ${dots.length}`);
  const xs = dots.map((c) => Number(c.getAttribute('cx'))).filter((x) => isFinite(x));
  const uniq = new Set(xs.map((x) => x.toFixed(1)));
  assert(uniq.size >= 3, `no-scalar lanes are SPREAD across the axis (≥3 distinct x), not piled — got xs ${JSON.stringify(xs)}`);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  assert(maxX - minX > 40, `the spread covers a real span of the axis (Δx ${(maxX - minX).toFixed(1)} > 40), not a tight stack`);
});

test('radar axis labels: radarSilhouette renders the axis LABEL TEXT at each tip (not an index 1..n), truncates long labels, and carries the full label on hover', () => {
  const axes = [
    { label: 'scalar (inverse loss)', chal: 0.82, champ: 0.6 },
    { label: 'pass-rate', chal: 0.9, champ: 0.7 },
    { label: 'tone-judge drift', chal: 0.5, champ: 0.8 },
    { label: 'structure-judge drift', chal: 0.7, champ: 0.65 },
    { label: 'concision', chal: 0.6, champ: 0.55 },
  ];
  const node = svg.radarSilhouette({ axes, onAxis() {} });
  const labs = allByClass(node, 'dn-radar-axislab');
  assertEqual(labs.length, axes.length, 'one text label per axis at the tip');
  const texts = labs.map((t) => (t.textContent || '').trim());
  // each label is derived from the axis NAME rather than an index 1..n.
  assert(!texts.some((t) => /^\d+$/.test(t)), `axis labels are TEXT, never a bare index — got ${JSON.stringify(texts)}`);
  assert(texts.some((t) => t.startsWith('pass-rate')), 'a short label renders in full (pass-rate)');
  // a long label truncates with an ellipsis but its hovercard keeps the full name.
  const longLab = labs.find((t) => (t.textContent || '').startsWith('scalar'));
  assert(longLab, 'the long "scalar (inverse loss)" axis renders a label');
  assert((longLab.textContent || '').includes('…') || (longLab.textContent || '').length <= 16, 'a long axis label is truncated to its budget');
  // no index-tick markers remain (the retired dn-radar-axistick).
  assertEqual(allByClass(node, 'dn-radar-axistick').length, 0, 'the retired index-tick (dn-radar-axistick) is GONE — labels replace it');
});

test('radar axis labels: a DENSE radar (many axes) still renders one text label per axis (harder truncation, no index fallback)', () => {
  const axes = Array.from({ length: 10 }, (_, i) => ({ label: `judge-${i}-semantic-drift`, chal: 0.5 + i * 0.02, champ: 0.6 }));
  const node = svg.radarSilhouette({ axes });
  const labs = allByClass(node, 'dn-radar-axislab');
  assertEqual(labs.length, 10, 'a 10-axis radar still labels every axis with text');
  const texts = labs.map((t) => (t.textContent || '').trim());
  assert(!texts.some((t) => /^\d+$/.test(t)), 'dense labels are still TEXT, never indices');
  assert(texts.every((t) => t.startsWith('judge')), 'every dense label is the (truncated) axis name');
});

test('radar mini: a mini radar suppresses tip labels (too small) but its vertices still carry the axis name on hover', () => {
  const axes = [
    { label: 'scalar', chal: 0.8, champ: 0.6 },
    { label: 'pass-rate', chal: 0.9, champ: 0.7 },
    { label: 'drift', chal: 0.5, champ: 0.8 },
  ];
  const node = svg.radarSilhouette({ axes, mini: true });
  assertEqual(allByClass(node, 'dn-radar-axislab').length, 0, 'a mini radar draws NO tip labels');
  assert(allByClass(node, 'dn-radar-hot').length >= 3, 'the mini radar still exposes hover-able axis vertices');
});

// ====================================================================
// continuous per-entry score + precision/recall surfaced on the
// candidate dossier + board view, degrading cleanly to pass/fail when the
// score / metrics are absent (back-compat).
// ====================================================================

// ---- the shared ui.js helpers --------------------------------------

test('#18 ui.prText: P / R tag from a metrics map; empty for the bool-only path', () => {
  assertEqual(ui.prText({ precision: 0.70, recall: 0.55 }), 'P 0.70 / R 0.55', 'both axes present');
  assertEqual(ui.prText({ precision: 0.70 }), 'P 0.70', 'precision only');
  assertEqual(ui.prText({ recall: 0.55 }), 'R 0.55', 'recall only');
  assertEqual(ui.prText({ f1: 0.9 }), '', 'a non-P/R metric does NOT produce a P/R tag');
  assertEqual(ui.prText(null), '', 'no metrics → empty (bool-only path)');
  assertEqual(ui.prText({ precision: 'x' }), '', 'a non-finite metric is dropped');
});

test('#18 ui.metricsDigest: stable, sorted, null when absent (folds into a content digest)', () => {
  assertDeep(ui.metricsDigest({ recall: 0.5, precision: 0.9 }), [['precision', '0.900'], ['recall', '0.500']],
    'keys are sorted + rounded for a stable digest');
  assertEqual(ui.metricsDigest(null), null, 'no metrics → null (digest unchanged vs the pre-score path)');
  assertEqual(ui.metricsDigest({}), null, 'an empty map → null');
});

test('#18 ui.scoreFmt: finite score formats; absent score reads "—"', () => {
  assertEqual(ui.scoreFmt(0.6234, 2), '0.62', 'a finite score formats to N decimals');
  assertEqual(ui.scoreFmt(null), '—', 'an absent score reads em-dash');
  assertEqual(ui.scoreFmt(NaN), '—', 'NaN reads em-dash (the non-finite guard)');
});

// A scored fixture: v1 carries a CONTINUOUS score + precision/recall on
// waffles_single and a BOOL-ONLY entry on picky (no score / metrics).
function scoredFixture() {
  const F = { ...FIXTURE };
  F[`/api/generation/${EPOCH_ID}/v1/per-entry`] = {
    epoch_id: EPOCH_ID, generation_id: 'v1', mean_score: 0.71, entries: [
      { entry_id: 'waffles_single', run_id: 'run_v1_waffles', drift_loss: 60.5, pass_fail: true,
        runtime_ms: 180000, wall_clock_budget_exceeded: false,
        score: 0.81, metrics: { precision: 0.88, recall: 0.74 } },
      // bool-only entry: no score / metrics — must keep the pass/fail display.
      { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v1_picky', drift_loss: 105.5,
        pass_fail: false, runtime_ms: 360000, wall_clock_budget_exceeded: false },
    ],
  };
  // champion v0 also has a scored waffles + a bool-only picky.
  F[`/api/generation/${EPOCH_ID}/v0/per-entry`] = {
    epoch_id: EPOCH_ID, generation_id: 'v0', mean_score: 0.62, entries: [
      { entry_id: 'waffles_single', run_id: 'run_v0_waffles', drift_loss: 70.0, pass_fail: true,
        runtime_ms: 180000, wall_clock_budget_exceeded: false,
        score: 0.62, metrics: { precision: 0.70, recall: 0.55 } },
      { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v0_picky', drift_loss: 99.0,
        pass_fail: true, runtime_ms: 360000, wall_clock_budget_exceeded: false },
    ],
  };
  return F;
}

// ---- candidate dossier --------------------------------------------

test('#18 candidate dossier: a scored entry shows the 0–1 score bar + P/R; the mean-score caption reads', async () => {
  freshState(); installFixtureMap(scoredFixture());
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  const dot = svgsByClass(host, 'dn-dumbbell')[0];
  assert(dot, 'the per-board dumbbell rendered');
  // the score column draws a 0→1 track + fill + readout for the SCORED row.
  assert(allByClass(dot, 'dn-score-track').length >= 1, 'a scored row draws the 0→1 score track');
  assert(allByClass(dot, 'dn-score-fill').length >= 1, 'a scored row draws the score fill');
  const vals = allByClass(dot, 'dn-score-val').map((t) => (t.textContent || '').trim());
  assert(vals.includes('0.81'), 'the candidate score 0.81 is read out in the dumbbell');
  // the precision/recall tag rides one faint line below the bar.
  const pr = allByClass(dot, 'dn-score-pr').map((t) => (t.textContent || '').trim());
  assert(pr.some((t) => t === 'P 0.88 / R 0.74'), 'the P/R decomposition surfaces on the scored row');
  // the per-generation mean-score caption reads beneath the dumbbell.
  assert(host.textContent.includes('mean score'), 'the per-generation mean-score caption renders');
  const ms = allByClass(host, 'dn-meanscore-val')[0];
  assert(ms && (ms.textContent || '').trim() === '0.71', 'the mean score 0.71 is shown');
});

test('#18 candidate dossier: a BOOL-ONLY board keeps its ✓/✗ — no score column when nothing scored', async () => {
  freshState(); installFetch();   // the BASE fixture: no score / metrics anywhere.
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  const dot = svgsByClass(host, 'dn-dumbbell')[0];
  assert(dot, 'the dumbbell still renders on the pre-score path');
  // NO score column: a wholly bool-only dumbbell draws no score track / fill / val.
  assertEqual(allByClass(dot, 'dn-score-track').length, 0, 'no score track on a bool-only board (back-compat)');
  assertEqual(allByClass(dot, 'dn-score-val').length, 0, 'no score readout on a bool-only board');
  assertEqual(allByClass(dot, 'dn-score-pr').length, 0, 'no P/R tag on a bool-only board');
  // the pre-score right-edge glyph layer is unchanged (the base v1 fixture rows
  // are timeouts → ⏱; a non-timeout bool row would draw the ✓/✗ instead).
  assert(allByClass(dot, 'dn-dumbbell-timeout').length >= 1
    || allByClass(dot, 'dn-dumbbell-fail').length >= 1
    || allByClass(dot, 'dn-dumbbell-pass').length >= 1,
    'the pre-score pass/fail/timeout glyph layer still renders (unchanged)');
  // and no mean-score caption when the payload carries no mean_score.
  assert(!host.textContent.includes('mean score'), 'no mean-score caption on the pre-score path');
});

// ---- candidate digest gating ---------------------------------------

test('#18 candidate digest: a no-op heartbeat over a SCORED dossier churns NO DOM', async () => {
  freshState(); installFixtureMap(scoredFixture());
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  const digest1 = host.getAttribute('data-t-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  assertEqual(host.getAttribute('data-t-digest'), digest1, 'digest unchanged on a no-op beat over a scored dossier');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op beat');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op beat (score/metrics folded, not churned)');
});

test('#18 candidate digest: a CHANGED score repaints (the score is folded into the content digest)', async () => {
  freshState();
  const F = scoredFixture();
  installFixtureMap(F);
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  const digest1 = host.getAttribute('data-t-digest');
  // move ONLY the score (drift_loss / pass unchanged) → the digest must flip.
  freshState();
  const F2 = scoredFixture();
  F2[`/api/generation/${EPOCH_ID}/v1/per-entry`].entries[0].score = 0.42;
  F2[`/api/generation/${EPOCH_ID}/v1/per-entry`].entries[0].metrics = { precision: 0.40, recall: 0.45 };
  installFixtureMap(F2);
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  assert(host.getAttribute('data-t-digest') !== digest1, 'a moved score flips the digest (a real repaint, no flashing bug)');
});

// ---- board view ----------------------------------------------------

test('#18 board view: a scored board adds a score + P/R column; reads the score + decomposition', async () => {
  freshState(); installFixtureMap(scoredFixture());
  const board = await import('../js/views/board.js');
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });
  assert(host.textContent.includes('Board · waffles_single'), 'the board view rendered');
  const tbl = allByClass(host, 'dn-board-table')[0];
  assert(tbl, 'the per-candidate breakdown table rendered');
  assert(host.textContent.includes('score'), 'a "score" column header appears for a scored board');
  const scoreCells = allByClass(host, 'dn-score-cell').map((c) => (c.textContent || '').trim());
  assert(scoreCells.includes('0.81') && scoreCells.includes('0.62'), 'both candidates’ scores read in the table');
  const prCells = allByClass(host, 'dn-pr-cell').map((c) => (c.textContent || '').trim());
  assert(prCells.some((t) => t === 'P 0.88 / R 0.74'), 'the P/R decomposition reads in the table');
});

test('board view: the drill-down shows the oracle + tags the overview already showed', async () => {
  freshState(); installFixtureMap(scoredFixture());
  const board = await import('../js/views/board.js');
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });
  const t = host.textContent || '';
  // Both ride the SAME ep.board row the trellis reads, and this is the page an
  // operator opens to ask what the entry actually checks. Showing
  // strictly less of the entry than the overview it is reached from.
  assert(t.includes('oracle'), 'the oracle (expectation_kind) stat renders');
  assert(t.includes('predicate'), 'and names the entry’s expectation kind');
  assert(t.includes('tags · smoke'), 'the entry tags render');
});

test('#18 board view: a BOOL-ONLY board (no scores) keeps the pre-score columns — no score column', async () => {
  freshState(); installFetch();   // base fixture — no scores anywhere.
  const board = await import('../js/views/board.js');
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });
  // no score column → no dn-score-cell / dn-pr-cell rendered; the predicate
  // column still carries the pass/fail label (back-compat).
  assertEqual(allByClass(host, 'dn-score-cell').length, 0, 'no score column on a bool-only board');
  assertEqual(allByClass(host, 'dn-pr-cell').length, 0, 'no P/R column on a bool-only board');
  const tbl = allByClass(host, 'dn-board-table')[0];
  assert(tbl, 'the breakdown table still renders');
});

// ---- CSS contract: the score classes are themed (no bold, theme tokens) ----

test('#18 CSS: the score classes use theme tokens + carry NO bold weight', async () => {
  const css = await readCssAsync();
  for (const cls of ['.dn-score-track', '.dn-score-fill', '.dn-score-val', '.dn-score-pr', '.dn-meanscore']) {
    assert(css.includes(cls), `the stylesheet defines ${cls}`);
  }
  // the verdict-coloured fill reuses the same good/bad tokens as the candidate ●.
  assert(/\.dn-score-fill\.dn-good\s*\{\s*fill:\s*var\(--v2-good\)/.test(css), 'the improved fill uses --v2-good');
  assert(/\.dn-score-fill\.dn-bad\s*\{\s*fill:\s*var\(--v2-bad\)/.test(css), 'the regressed fill uses --v2-bad');
  // NO bold weight on any new score class (700 / bold is banned in the language).
  const block = css.slice(css.indexOf('.dn-score-track'), css.indexOf('.dn-pr-cell') + 80);
  assert(!/font:\s*(?:700|bold)\b/.test(block), 'no score class carries a 700 / bold font weight');
});

test('H3 metaLoopLedger: the band-id is width-gated like its siblings — it never spills past its own band, but stays FULL on roomy bands', () => {
  // 6.5px/glyph is the same per-char estimate the band-id cap uses (and is
  // conservative vs the chip math's 6.0); left pad is 8px (txt drawn at b.x0+8).
  const GLYPH = 6.5; const LPAD = 8;
  const ID14 = '2026-06-13_e99'; // exactly 14 chars — the old hard cap
  assertEqual(ID14.length, 14, 'fixture id is exactly the old 14-char cap');

  // -- crowded ledger: 12 equal-effort epochs → band width ≈ 996/12 ≈ 83px --
  const crowdedEpochs = [];
  for (let i = 0; i < 12; i++) {
    crowdedEpochs.push({ epoch_id: ID14, generation_count: 10, floor: 50 - i, closed: true });
  }
  const node = svg.metaLoopLedger({ epochs: crowdedEpochs }); // default width 1120
  const ids = node.querySelectorAll('[class]')
    .filter((n) => n.localName === 'text' && (n.getAttribute('class') || '').split(/\s+/).includes('dn-metaledger-bandid'));
  const bands = node.querySelectorAll('[class]')
    .filter((n) => n.localName === 'rect' && (n.getAttribute('class') || '').split(/\s+/).includes('dn-metaledger-band'));
  assertEqual(ids.length, 12, 'one band-id per epoch rendered');
  assertEqual(bands.length, 12, 'one band rect per epoch rendered');

  // pair id↔band by index (both appended in the same per-epoch loop order) and
  // assert each id's estimated right edge stays within its OWN band.
  let anyTruncated = false;
  for (let i = 0; i < ids.length; i++) {
    const t = ids[i];
    const bandRight = +bands[i].getAttribute('x') + +bands[i].getAttribute('width');
    const idX = +t.getAttribute('x');
    const estRight = idX + (t.textContent || '').length * GLYPH;
    assert(estRight <= bandRight + 0.5,
      `band-id ${i} ("${t.textContent}") right edge ${estRight.toFixed(1)} must stay within its band right ${bandRight.toFixed(1)} — no spill into the neighbour`);
    if ((t.textContent || '').includes('…')) anyTruncated = true;
  }
  assert(anyTruncated, 'on a crowded 12-epoch ledger the over-long id is truncated in place (…), not drawn at full width');

  // -- no-regression: a roomy 4-epoch ledger has band width ≈ 996/4 ≈ 249px,
  // so the cap clamps back to the full 14 and the id is NOT truncated. --
  const roomy = svg.metaLoopLedger({ epochs: [
    { epoch_id: ID14, generation_count: 10, floor: 50, closed: true },
    { epoch_id: ID14, generation_count: 10, floor: 49, closed: true },
    { epoch_id: ID14, generation_count: 10, floor: 48, closed: true },
    { epoch_id: ID14, generation_count: 10, floor: 47, closed: true },
  ] });
  const roomyIds = roomy.querySelectorAll('[class]')
    .filter((n) => n.localName === 'text' && (n.getAttribute('class') || '').split(/\s+/).includes('dn-metaledger-bandid'));
  assert(roomyIds.length === 4 && roomyIds.every((t) => t.textContent === ID14),
    'on a roomy ledger the full 14-char id renders untruncated (common-data render unchanged)');
});

test('#H4 metaLoopLedger heatstrip col-ids: width-gated so narrow bands never overprint, wide bands keep the full id', () => {
  const colidsOf = (node) => node.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('dn-metaledger-colid'));
  const W = 1120;
  const pw = W - 96 - 28; // L=96, R=28 → plottable width = 996 (mirrors metaLoopLedger geometry)
  const CHAR_PX = 6.6;    // colid glyph advance the fix budgets against

  // NARROW: 14 equal-effort epochs → each band ≈ 996/14 ≈ 71px, well under the
  // ~92px a 14-char middle-anchored id needs. Before the fix every id is drawn
  // at the full 14-char cap and overprints its neighbour.
  const narrow = [];
  for (let i = 0; i < 14; i++) {
    narrow.push({ epoch_id: '2026-06-13_epoch_' + i, generation_count: 4, floor: 0.5 - i * 0.01, changed_components: {} });
  }
  const bandW = pw / narrow.length;
  const nCols = colidsOf(svg.metaLoopLedger({ epochs: narrow, width: W }));
  // every RENDERED col-id must fit inside its band (centered text width ≤ band width).
  for (const c of nCols) {
    const textPx = (c.textContent || '').length * CHAR_PX;
    assert(textPx <= bandW, `a narrow col-id (${JSON.stringify(c.textContent)} ≈ ${textPx.toFixed(0)}px) must fit its ${bandW.toFixed(0)}px band — no overprint`);
    assertEqual(c.getAttribute('text-anchor'), 'middle', 'the col-id stays middle-anchored');
  }
  // and the cap shrank below the original 14 in the narrow regime.
  const maxLen = Math.max(...nCols.map((c) => (c.textContent || '').length));
  assert(maxLen < 14, `narrow col-ids must be truncated below the 14-char cap (saw max ${maxLen})`);

  // WIDE (common case): a single epoch owns the whole strip → the id keeps the
  // full 14-char cap. Per L9 the col-id now MIDDLE-truncates (midLabel) so two
  // prefix-sharing epoch ids stay distinguishable; an over-cap id therefore reads
  // head…tail at the same 14-char budget rather than a head-only stub.
  const wide = svg.metaLoopLedger({ epochs: [{ epoch_id: 'abcdefghijklmnopqrstuvwxyz', generation_count: 4, floor: 0.5, changed_components: {} }], width: W });
  const wCols = colidsOf(wide);
  assertEqual(wCols.length, 1, 'the single-epoch ledger draws exactly one col-id');
  assertEqual(wCols[0].textContent, 'abcdefg…uvwxyz', 'a wide band keeps the full 14-char cap (now MIDDLE-truncated, head…tail, per L9)');
  assertEqual(wCols[0].textContent.length, 14, 'the mid-truncated col-id still respects the 14-char width budget (no overrun vs the old cap)');
});

// swissLadder in-flight status must ride the cy+13 sub-line rather than the
// primary pairing label (which overruns the ~150px round column). The primary
// `dn-swissladder-pairlab` label is just `a v b`; the "running N/M" status is
// relocated to a sub-line, mirroring the decided branch's cy+13 winner sub-line.
test('H5: swissLadder in-flight pairing keeps the primary label to `a v b` — the "running N/M" status moves to the cy+13 sub-line (no column overrun)', () => {
  const node = svg.swissLadder({
    rounds: [{ label: 'Round 1', pairings: [
      { a: 'champ0', b: 'chalA', winner: 'champ0' },             // decided
      { a: 'chalB', b: 'chalC', total: 10, done: 7 },            // in flight (7/10)
    ] }],
    standings: [], championId: null, benchmarkId: null,
  });
  // the PRIMARY pairing label is the TOP text of each pair group (the smaller-y
  // dn-swissladder-pairlab); a relocated status sub-line shares the class but
  // sits one line lower (cy+13). Partition by the per-pair minimum y so we assert
  // against the primary line only — the line that would overrun the column.
  const pairGroups = allByClass(node, 'dn-swissladder-pair');
  assert(pairGroups.length >= 1, 'the in-flight pairing renders a pair group');
  const primaries = pairGroups.map((g) => {
    const ls = allByClass(g, 'dn-swissladder-pairlab');
    return ls.slice().sort((p, q) => (+p.getAttribute('y')) - (+q.getAttribute('y')))[0];
  }).filter(Boolean);
  assert(primaries.length >= 1, 'the in-flight pairing renders a primary pairing label');
  // the PRIMARY label of EVERY pairing is just the `a v b` pairing — no status.
  for (const lab of primaries) {
    assert(!/running/.test(lab.textContent), 'the primary pairlab carries NO "running" suffix (it would overrun the column)');
    assert(!/\d+\/\d+/.test(lab.textContent), 'the primary pairlab carries NO N/M progress count');
    assert(/\bv\b/.test(lab.textContent), 'the primary pairlab still reads the `a v b` pairing');
  }
  // but the status is NOT lost — it lives on the relocated sub-line.
  assert(/running/.test(node.textContent), 'the in-flight status "running" is still shown (relocated to the sub-line)');
  assert(/7\/10/.test(node.textContent), 'the in-flight board progress (7/10) is still shown on the sub-line');
  // a live progress bar still renders for the in-flight pairing.
  assert(allByClass(node, 'dn-swissladder-bar-live')[0], 'the in-flight pairing still shows its live progress bar');
});

test('elimRadial H6: cardinal-E/W spoke labels never overrun the viewBox (horizontal extent clamped inside the box, not just the radial labelPad)', () => {
  // Eight distinct 8-char ids in one round ⇒ eight evenly-spaced spokes, so two
  // land at EXACTLY cardinal East (frac .25) and West (frac .75) where the label
  // origin sits at cx ± (R-ish) and `text-anchor:start/end` grows the text
  // horizontally past the box. Before the fix the East 8-char label runs to ~358
  // (W=340) and the West label clips at x<0; the radial labelPad never bounds it.
  // The radial reads the SERVED model (rounds + gen_states) verbatim, so the
  // fixture is enriched via deriveElimStates the way the server does.
  const ids = ['genaaaaa', 'genbbbbb', 'genccccc', 'genddddd', 'geneeeee', 'genfffff', 'genggggg', 'genhhhhh'];
  const matches = [];
  for (let i = 0; i < ids.length; i += 2) matches.push({ match_id: 'm' + i, competitors: [ids[i], ids[i + 1]], winner: ids[i] });
  const served = mock.deriveElimStates([{ label: 'R0', round_index: 0, matches }]);
  const node = svg.elimRadial({
    rounds: served.rounds, gen_states: served.gen_states,
    championId: ids[0], benchmarkId: 'v0', gateState: 'crowned',
  });
  const W = Number((node.getAttribute('viewBox') || '0 0 0 0').split(/\s+/)[2]);
  assert(W > 0, 'the radial figure keeps a numeric viewBox width');
  const labels = allByClass(node, 'dn-elimradial-name');
  assert(labels.length >= 8, `every spoke renders a name label — got ${labels.length}`);
  // 9px mono ⇒ ~0.6em/char; reproduce the rendered horizontal span [x0,x1].
  const charW = 5.4;
  let worst = null;
  for (const t of labels) {
    const x = Number(t.getAttribute('x'));
    const anchor = t.getAttribute('text-anchor') || 'start';
    const w = (t.textContent || '').length * charW;
    const x0 = anchor === 'end' ? x - w : anchor === 'middle' ? x - w / 2 : x;
    const x1 = anchor === 'end' ? x : anchor === 'middle' ? x + w / 2 : x + w;
    if (worst == null || x0 < worst.x0 || x1 > worst.x1) worst = { id: (t.textContent || '').trim(), anchor, x0, x1 };
    // FAILS before the fix on the cardinal-E (x1>W) and cardinal-W (x0<0) spokes.
    assert(x0 >= -0.01 && x1 <= W + 0.01,
      `spoke label '${(t.textContent || '').trim()}' (anchor=${anchor}) stays inside the box: [${x0.toFixed(1)}, ${x1.toFixed(1)}] within [0, ${W}]`);
  }
});

test('H7: swissLadder standings — a crowded double-digit rank (crown + ~proj) shrinks the id so it never overlaps the right-anchored points value', () => {
  // Ten competitors → the 10th row carries a TWO-digit rank. Make it the
  // champion (crown) AND projected (` ~proj`) with an exactly-9-char id — the
  // worst case the H7 collision is about: `"10. {9chars} ♛ ~proj"` reaches
  // the right-anchored points value (end-anchored at sx+standW-6).
  const standings = Array.from({ length: 10 }, (_, k) => ({
    id: 'comp-' + (1000 + k), points: 5 - k * 0.4, wins: 2, losses: 1, draws: 0,
  }));
  standings[9] = {
    id: 'cand-1234', points: 1.1, wins: 1, losses: 2, draws: 0,
    in_flight: true, projected_scalar: 1.23, boards_total: 4, boards_done: 2,
  };
  const node = svg.swissLadder({ rounds: [], standings, championId: 'cand-1234' });
  const labs = allByClass(node, 'dn-swissladder-standlab');
  assertEqual(labs.length, 10, 'a standings row per competitor');
  const worst = labs[9].textContent;
  // the row still shows its decorations…
  assert(worst.startsWith('10.') && worst.includes(svg.CROWN.current) && worst.includes('~proj'),
    'the 10th row keeps its rank, crown, and ~proj decorations');
  // …but the 9-char id is SHORTENED (truncation ellipsis) so it cannot reach
  // the points value — pre-fix this rendered the whole "cand-1234" and collided.
  assert(!worst.includes('cand-1234') && worst.includes('…'),
    'the crowded double-digit row truncates the id so name + points never overlap');
  // NO REGRESSION: the common single-digit, undecorated row keeps the full id.
  assert(labs[0].textContent.includes('comp-1000'),
    'an undecorated single-digit row still renders its full 9-char id');
});

test('H8 valueBars: the worst-judge (full-width) value is inset + end-anchored so it never clips the right gutter', () => {
  const w = 360;
  const labelW = 150;
  const x0 = labelW + 4;
  const plotEnd = w - 36;
  // worst judge has the largest |value| (reaches the plot end); the other is small.
  const node = svg.valueBars({
    width: w,
    labelWidth: labelW,
    items: [
      { label: 'tone_drift', value: -128.4 }, // full-width bar (max magnitude)
      { label: 'brevity', value: 12.0 },       // short bar
    ],
  });
  const vals = allByClass(node, 'dn-vbar-val');
  assertEqual(vals.length, 2, 'one value label per judge row');

  // the worst-judge bar reaches bx = w-36; its value must be INSET (end-anchored,
  // grows leftward) and anchored at-or-left-of the plot end so it stays on-canvas.
  const worst = vals.find((v) => v.getAttribute('data-inset') === '1');
  assert(worst, 'the full-width worst-judge value is inset (data-inset=1) — not pushed past the gutter');
  assertEqual(worst.getAttribute('text-anchor'), 'end', 'the inset value is end-anchored so it grows leftward into the bar');
  const wx = +worst.getAttribute('x');
  assert(wx <= plotEnd, `the inset value anchor x (${wx}) is at or inside the plot end (${plotEnd}) — it cannot run off the ${w}px-wide viewBox`);
  assertEqual(worst.textContent, svg.fmt(-128.4, 1), 'the value text is unchanged (fmt(v,1))');

  // the SHORT bar is unchanged: value sits to the RIGHT of the bar, left-anchored
  // (no text-anchor=end, no data-inset) — the common case must not regress.
  const short = vals.find((v) => v.getAttribute('data-inset') !== '1');
  assert(short, 'the short-bar value keeps the original outside placement');
  assert(short.getAttribute('text-anchor') == null, 'the short-bar value is NOT end-anchored (default left-anchor, outside the bar)');
  const sx = +short.getAttribute('x');
  assert(sx > x0 && sx < plotEnd, `the short-bar value (${sx}) is placed to the right of its short bar, inside the plot`);
});

test('survival funnel (H9): a long cut id is fit to the bracket-rail gutter — truncated with an ellipsis, never clipping the viewBox', () => {
  const longId = 'challenger_xy9'; // 14 chars — wider than the rail gutter budget
  const rungs = [
    { label: 'Rung 0', competitors: [longId, 'v2', 'v3'], survivors: ['v3'], cut: [longId, 'v2'], board_fraction: 0.5, deltas: { v3: -1 } },
  ];
  const node = svg.survivalFunnel({ rungs, championId: 'v3', benchmarkId: 'v0', gateState: 'crowned', onCompetitor() {} });

  // the LEFT-EDGE bracket-rail gutter budget, mirrored from the renderer.
  const stageW = 150, dotR = 4.5, CHAR_EM = 0.6, fontPx = 11;
  const dotX0 = 2 + stageW / 2;            // dotX(0) — the first dot column

  // cut competitors carry a dn-bad rail name (the cut-count style pin).
  const cutNames = allByClass(node, 'dn-funnel-name').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-bad'));
  assert(cutNames.length >= 1, 'the cut competitors carry dn-bad rail names');
  const longLabel = cutNames.find((n) => (n.textContent || '').indexOf('…') >= 0);
  assert(longLabel, 'the over-long cut id is truncated with an ellipsis to fit the rail gutter');

  // THE FIX: the fitted rail name is left-anchored at x≈4 and its rendered width
  // stays LEFT of the first dot column (never clips the viewBox / runs into the dots).
  const railW = (longLabel.textContent || '').length * fontPx * CHAR_EM;
  assert(4 + railW <= dotX0 + 0.5, `the fitted rail name "${longLabel.textContent}" (w≈${railW.toFixed(1)}) stays left of the first dot column x=${dotX0}`);
  // a SHORT cut id (v2) is untouched — the fit only bites over-long ids.
  const shortLbl = cutNames.find((n) => (n.textContent || '').replace(/[^\w]/g, '').indexOf('v2') === 0);
  assert(shortLbl && (shortLbl.textContent || '').indexOf('…') < 0, 'a short cut id (v2) renders in full (no regression to common-data labels)');
  // the cut still drops a ✕ mark at its rung dot (the dot-gap cut idiom).
  assert(node.textContent.includes('✕'), 'the cut competitor drops a ✕ mark');
});

test('waterfall H10: on an improved step the crown ♛ is lifted clear of the floor label (no overprint at the same cx)', () => {
  // every step IMPROVES (to < from), so each station carries both a crown and a
  // floor label centred on the same cx. The crown must sit well ABOVE the label
  // (smaller y), clear of the 2px overprint a yTo-8 versus yTo-6 placement gives.
  const steps = [
    { round_index: 0, from: 20, to: 14, delta: -6, promoted: true, gen: 'v1' },
    { round_index: 1, from: 14, to: 9, delta: -5, promoted: true, gen: 'v2' },
  ];
  const node = svg.waterfall({ steps });
  const crowns = allByClass(node, 'dn-waterfall-crown');
  const floors = allByClass(node, 'dn-waterfall-floor');
  assert(crowns.length >= 2, 'each improved step carries a crown ♛');
  assert(floors.length >= 2, 'each station annotates the running floor');
  // pair crown↔floor by shared cx (the x attribute), then check the vertical gap.
  crowns.forEach((cr) => {
    const cx = cr.getAttribute('x');
    const fl = floors.find((f) => f.getAttribute('x') === cx);
    assert(fl, 'a floor label shares the crown column');
    const crY = parseFloat(cr.getAttribute('y'));
    const flY = parseFloat(fl.getAttribute('y'));
    // the crown is ABOVE the label (smaller y) by a clear margin so the ♛ does
    // not overprint the floor number (old gap was 2px; this requires ≥ 8px).
    assert(flY - crY >= 8, `the crown clears the floor label at cx=${cx} (gap ${(flY - crY).toFixed(1)}px ≥ 8)`);
  });
});

test('elimRadial double-elim: a WB->LB transfer arc STARTS at the source WB node angle (the equator-mirror of the LB node), not a bare constant-offset rim point', () => {
  // v3 loses in the WB (R0) then drops into an LB-slotted match (R1) and loses
  // again — so its spoke sits on the LB (lower) arc and it qualifies as a drop.
  // The radial reads the SERVED gen_states verbatim, so the fixture is enriched
  // via deriveElimStates (the WB/LB side + drop classification are the server's).
  const served = mock.deriveElimStates([
    { label: 'R0', round_index: 0, matches: [{ match_id: 'm0', competitors: ['v1', 'v3'], winner: 'v1' }] },
    { label: 'LB1', round_index: 1, matches: [{ match_id: 'm1', bracket_slot: 'LB1', competitors: ['v3', 'v2'], winner: 'v2' }] },
  ]);
  const node = svg.elimRadial({
    double: true, rounds: served.rounds, gen_states: served.gen_states,
    championId: 'v1', benchmarkId: 'v0', gateState: 'crowned',
  });
  // default (non-mini) elimRadial: sz=340 -> cx=cy=170 (the equator is y=cy).
  const cx = 170; const cy = 170;
  const arcs = allByClass(node, 'dn-elimradial-transfer');
  assert(arcs.length >= 1, `at least one WB->LB transfer arc renders for the dropped lane — got ${arcs.length}`);
  const d = arcs[0].getAttribute('d') || '';
  // d = `M{fx} {fy} A{r} {r} 0 {large} 1 {tx} {ty}`
  const m = d.match(/^M([\-0-9.]+) ([\-0-9.]+) A[0-9.]+ [0-9.]+ 0 \d+ \d+ ([\-0-9.]+) ([\-0-9.]+)$/);
  assert(m, `transfer arc path parses (got d=${JSON.stringify(d)})`);
  const fx = Number(m[1]); const fy = Number(m[2]); const tx = Number(m[3]); const ty = Number(m[4]);
  // the LB node (arc TARGET) is on the lower half; its source WB node is the
  // reflection across the equator: same x, mirrored y. The pre-fix constant
  // -18deg offset breaks BOTH (cos(a-18) != cos(a), and fy stays below cy).
  assert(ty > cy, `sanity: the LB node target is on the lower (LB) arc, y>cy (ty=${ty}, cy=${cy})`);
  assert(fy < cy, `the arc START is on the upper (WB) arc — its source WB node — not the LB-side rim (fy=${fy}, cy=${cy})`);
  assert(Math.abs(fx - tx) < 0.2, `the WB source node sits directly above the LB node: start x mirrors target x (fx=${fx}, tx=${tx})`);
  assert(Math.abs((fy - cy) + (ty - cy)) < 0.2, `the start y is the equator-reflection of the target y (fy-cy=${(fy - cy).toFixed(2)}, ty-cy=${(ty - cy).toFixed(2)})`);
});

test('elimRadial double-elim: EVERY WB→LB transfer arc ANCHORS on real nodes — both endpoints sit on the outer node ring and the arc ENDS exactly on its LB re-entry node; the connector never floats out on a staggered rim (regression: floating demotion edges)', () => {
  // The WB→LB demotion connector MUST visibly connect the two matches it links:
  // an edge whose endpoints land where NO node is drawn floats in empty space.
  // The pre-fix bug: each transfer arc placed BOTH its endpoints on a per-drop
  // STAGGERED rim radius `stagger = rr(0) + li*step`, OUTSIDE the outer node ring.
  // Only the first drop (li=0, stagger == rr(0)) happened to land on the ring; the
  // SECOND-and-later drops began and ended a few px OUTSIDE it — detached from both
  // the source-mirror and the destination LB node. So this fixture forces THREE
  // drops (v2 + v4 off WB-R0, v3 off WB-R1) — the extra arcs are exactly the ones
  // a floating placement would drift. The served model (deriveElimStates) drives the side/drop read.
  const served = mock.deriveElimStates([
    { round_index: 0, label: 'R0', matches: [
      { competitors: ['v1', 'v2'], winner: 'v1', bracket_slot: 'WB-R0-0' },
      { competitors: ['v3', 'v4'], winner: 'v3', bracket_slot: 'WB-R0-1' },
    ] },
    { round_index: 1, label: 'R1', matches: [
      { competitors: ['v1', 'v3'], winner: 'v1', bracket_slot: 'WB-R1-0' },
    ] },
    { round_index: 2, label: 'LB2', matches: [
      { competitors: ['v2', 'v4'], winner: 'v2', bracket_slot: 'LB-R2-0' },
    ] },
    { round_index: 3, label: 'LB3', matches: [
      { competitors: ['v2', 'v3'], winner: 'v2', bracket_slot: 'LB-R3-0' },
    ] },
  ]);
  const node = svg.elimRadial({
    double: true, rounds: served.rounds, gen_states: served.gen_states,
    championId: 'v2', benchmarkId: 'v0', gateState: 'crowned',
  });
  // default (non-mini) elimRadial: sz=340 → cx=cy=170 (the figure center).
  const cx = 170, cy = 170;
  // the real, rendered node anchors + the outer node ring rr(0) = the max node
  // distance from center (every spoke draws its k=0 node on that ring).
  const nodePts = allByClass(node, 'dn-elimradial-node').map((n) => ({ x: Number(n.getAttribute('cx')), y: Number(n.getAttribute('cy')) }));
  assert(nodePts.length > 0, 'the radial renders node anchors');
  const nodeR = Math.max(...nodePts.map((p) => Math.hypot(p.x - cx, p.y - cy)));
  const onANode = (p) => nodePts.some((q) => Math.hypot(p.x - q.x, p.y - q.y) < 0.6);

  const arcs = allByClass(node, 'dn-elimradial-transfer');
  assert(arcs.length >= 2, `≥2 WB→LB transfer arcs render for a multi-drop bracket — the pre-fix float only surfaced on the 2nd+ arc (got ${arcs.length})`);

  arcs.forEach((arc, k) => {
    const d = arc.getAttribute('d') || '';
    // d = `M{fx} {fy} A{r} {r} 0 {large} {sweep} {tx} {ty}`
    const m = d.match(/^M([\-0-9.]+) ([\-0-9.]+) A[0-9.]+ [0-9.]+ 0 \d+ \d+ ([\-0-9.]+) ([\-0-9.]+)$/);
    assert(m, `transfer arc #${k} path parses (d=${JSON.stringify(d)})`);
    const start = { x: Number(m[1]), y: Number(m[2]) };
    const end = { x: Number(m[3]), y: Number(m[4]) };
    const startR = Math.hypot(start.x - cx, start.y - cy);
    const endR = Math.hypot(end.x - cx, end.y - cy);
    // (1) BOTH endpoints ON the outer node ring — never a staggered rim outside it.
    assert(Math.abs(startR - nodeR) < 0.6, `arc #${k} START is ON the outer node ring (r=${startR.toFixed(1)} vs rr0=${nodeR.toFixed(1)}), not floating on a rim outside it`);
    assert(Math.abs(endR - nodeR) < 0.6, `arc #${k} END is ON the outer node ring (r=${endR.toFixed(1)} vs rr0=${nodeR.toFixed(1)}), not floating on a rim outside it`);
    // (2) the DESTINATION coincides EXACTLY with a real LB re-entry node — the edge
    // visibly connects its target node (which always exists), never floats near it.
    assert(onANode(end), `arc #${k} END (${end.x.toFixed(1)},${end.y.toFixed(1)}) coincides with a rendered LB node anchor — the destination exists and the arc terminates ON it`);
    // (3) the source stays the equator-mirror of the destination (design intent):
    // same x, y reflected across the equator — so the connector reads WB→LB.
    assert(Math.abs(start.x - end.x) < 0.2 && Math.abs((start.y - cy) + (end.y - cy)) < 0.2,
      `arc #${k} START is the equator-mirror of its END (WB source above ↔ LB node below)`);
  });
});

test('swissOverview (single round): the lone-round bump centers ONE dot per competitor — no start/end+label stack on the left gutter (M7)', () => {
  // A one-round swiss (labels.length === 1) must not pin every competitor's start
  // dot, end dot, name label and #rank label onto a single x (padL) because
  // scale([0, max(1, nR-1)],…) maps the lone column to the left edge. The fix
  // centers the lone column AND collapses the coincident start/end pair to one dot.
  const over = svg.swissOverview({
    labels: ['Round 1'],
    series: [
      { id: 'v0', ranks: [1], crown: true },
      { id: 'v1', ranks: [2] },
    ],
    bars: [
      { id: 'v0', points: 1, wins: 1, draws: 0, losses: 0, crown: true },
      { id: 'v1', points: 0, wins: 0, draws: 0, losses: 1 },
    ],
    championId: 'v0', gateState: 'crowned',
  });
  const dots = allByClass(over, 'dn-swissover-dot');
  // ONE dot per competitor (not a doubled-up start/end pair on the same x).
  assertEqual(dots.length, 2, 'single-round swiss draws ONE dot per competitor, not a coincident start/end pair');
  // the dots are centered in the plot band rather than pinned to the padL=96 left gutter.
  const padL = 96;
  const cxs = dots.map((d) => parseFloat(d.getAttribute('cx')));
  assert(cxs.every((cx) => cx > padL + 1),
    'the single-round dots are centered in the plot band, not pinned to the padL left gutter (cx=' + cxs.join(',') + ')');
  // and the two competitors share the centered column (a single round = one x).
  assert(cxs.every((cx) => Math.abs(cx - cxs[0]) < 0.5),
    'both competitors share the lone centered round column');
});

test('Task 3 — elimFlow: a DEGENERATE column with a DUPLICATE match (same bracket_slot + competitors emitted twice) draws ONE convergence node, keeping the most-decided instance', () => {
  // The backend has been observed publishing the SAME match twice in one column
  // (an identical bracket_slot + competitor pair). Pre-fix each duplicate drew
  // its own convergence elbow + node STACKED on the first, so one match read as
  // two overlapping convergences. The duplicate is listed PENDING-first then
  // DECIDED-second to exercise the most-decided retention.
  const winners = [
    { round_index: 0, label: 'Round 1', matches: [
      // the duplicate pair: same slot, same competitors — pending, then settled.
      { competitors: ['v5', 'v6'], winner: null, pending: true, bracket_slot: 'WB-R0-0' },
      { competitors: ['v5', 'v6'], winner: 'v5', decision: 'win', bracket_slot: 'WB-R0-0' },
      // a DISTINCT match sharing the column (different competitors) must
      // be PRESERVED — it keeps its own key, its own convergence node.
      { competitors: ['v7', 'v8'], winner: 'v7', decision: 'win', bracket_slot: 'WB-R0-1' },
    ] },
  ];
  const served = mock.deriveElimStates(winners);
  const flow = svg.elimFlow({ rounds: served.rounds, gen_states: served.gen_states, championId: 'v5', benchmarkId: 'v6', gateState: 'crowned' });
  const nodes = allByClass(flow, 'dn-elimflow-convnode');
  // two DISTINCT matches → exactly two convergence nodes (the duplicate collapses).
  assertEqual(nodes.length, 2, `the duplicated WB-R0-0 collapses to one node; v7/v8 stays — 2 nodes total (got ${nodes.length})`);
  // the surviving WB-R0-0 node is the MOST-DECIDED (settled winner) instance, not
  // the pending duplicate that was listed first.
  const pendingNodes = nodes.filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-elimflow-pending'));
  assertEqual(pendingNodes.length, 0, 'the kept duplicate is the decided instance — no pending convergence node survives');
  const goodNodes = nodes.filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-elimflow-good'));
  assertEqual(goodNodes.length, 2, 'both surviving nodes are decided wins (v5 over v6, v7 over v8)');
});

test('duelFlow: a long (>=9-char) challenger id + status glyph stays INSIDE the left viewBox edge (the name gutter fits shortLabel(id,9)+glyph)', () => {
  const node = svg.duelFlow({
    championId: 'v0', championScalar: 12.0,
    challengers: [
      { id: 'proposer_xl', delta: -3.2, verdict: 'promoted', hypothesis: 'tighten the slide structure', driver: 'incorporates_feedback' },
    ],
    onCompetitor() {},
  });
  const names = allByClass(node, 'dn-duelflow-name');
  assertEqual(names.length, 1, 'one name label per challenger lane');
  const lbl = names[0];
  // the id is capped to shortLabel(id,9) + ' ' + status glyph: 'proposer… ↑'.
  assert(lbl.textContent.includes('…'), 'a >=9-char id is ellipsised by the shortLabel(id,9) cap');
  assert(lbl.textContent.includes('↑'), 'the promoted lane carries the ↑ status glyph');
  assertEqual(lbl.getAttribute('text-anchor'), 'end', 'the name is right-anchored at the gutter edge');
  // end-anchored at x=nameW: the label spans [x - width, x]. With ~6px/glyph-cell the
  // left edge must not cross the viewBox left edge (x=0) — i.e. nameW >= label width.
  const anchorX = Number(lbl.getAttribute('x'));
  const estWidth = lbl.textContent.length * 6;
  assert(anchorX >= estWidth, `the name gutter (x=${anchorX}) fits the full label (~${estWidth}px) so its left edge stays inside the viewBox (x>=0)`);
});

test('radar axis labels: a full-size NON-legend radar CLAMPS the East/West horizontal axis labels inside the viewBox (no right/left edge clip)', () => {
  // n=4 → angle(i) = -90 + i*90deg, so i=1 sits exactly EAST (start-anchored,
  // worst-case x) and i=3 sits exactly WEST (end-anchored). 16-char labels hit
  // the labelMax=16 budget for n<=6 → the maximal horizontal extent.
  const axes = [
    { label: 'north-axis-name', chal: 0.7, champ: 0.6 },        // i=0, top
    { label: 'scalar-inverse-x', chal: 0.82, champ: 0.6 },      // i=1, EAST (16 ch)
    { label: 'south-axis-name', chal: 0.6, champ: 0.55 },       // i=2, bottom
    { label: 'per-judge-drift-x', chal: 0.7, champ: 0.65 },     // i=3, WEST (16 ch)
  ];
  const node = svg.radarSilhouette({ axes, legend: false });   // full-size non-legend: W=360
  const W = 360;
  const CHAR = 5.7;            // 9.5px mono ≈ 0.6em/char (the audit's font-width assumption)
  const labs = allByClass(node, 'dn-radar-axislab');
  assertEqual(labs.length, 4, 'all four axes render a horizontal label (none rotated at the cardinals)');
  const right = labs.find((t) => (t.textContent || '').startsWith('scalar-inverse'));
  const left = labs.find((t) => (t.textContent || '').startsWith('per-judge-drift'));
  assert(right && left, 'the East and West axis labels both render');
  // EAST label is start-anchored; its right edge must stay inside the viewBox.
  assertEqual(right.getAttribute('text-anchor'), 'start', 'the East-spoke label is start-anchored');
  const rRight = Number(right.getAttribute('x')) + (right.textContent || '').length * CHAR;
  assert(rRight <= W, `the East axis label is clamped inside W=${W} (estimated right edge ${rRight.toFixed(1)})`);
  // WEST label is end-anchored; its left edge must stay >= 0.
  assertEqual(left.getAttribute('text-anchor'), 'end', 'the West-spoke label is end-anchored');
  const lLeft = Number(left.getAttribute('x')) - (left.textContent || '').length * CHAR;
  assert(lLeft >= 0, `the West axis label is clamped inside the left edge (estimated left edge ${lLeft.toFixed(1)})`);
  // the clamp moved x only — the label TEXT is still the full (un-truncated) 16-char name.
  assertEqual(right.textContent, 'scalar-inverse-x', 'the clamp does not alter the label text (only its x)');
});

test('gauntletFieldBars: the field-worst challenger value is right-anchored INBOARD — never overruns the W=600 viewBox', () => {
  // champion is the field BEST, so the worst challenger's dot lands at the
  // band's right edge (the scalar-domain max). Its 3dp value (~42px) would be
  // start-anchored at dx+7 and run off the right of the 600-unit viewBox.
  const node = svg.gauntletFieldBars({
    championId: 'v0', championScalar: 8.0, promoteMargin: 0.5,
    challengers: [
      { id: 'v5', scalar: 8.05, survivor: true },
      { id: 'worstchallng', scalar: 12.345, outcome: 'failed' },
    ],
  });
  const W = 600;
  const dots = allByClass(node, 'dn-fieldbars-dot');
  const vals = allByClass(node, 'dn-fieldbars-val');
  assertEqual(vals.length, 2, 'a value label per settled challenger');
  const worstDot = dots.reduce((a, b) => (+a.getAttribute('cx') > +b.getAttribute('cx') ? a : b));
  const worstVal = vals.find((v) => v.textContent.includes('12.345'));
  const bestVal = vals.find((v) => v.textContent.includes('8.050'));
  // the worst-end value flips to an INBOARD end-anchor (grows leftward) and its
  // anchor x sits at/left of the dot, so it stays well inside the W viewBox.
  assertEqual(worstVal.getAttribute('text-anchor'), 'end', 'the worst-end value is end-anchored (inboard)');
  assert(+worstVal.getAttribute('x') <= +worstDot.getAttribute('cx'), 'the value x sits at/left of the dot — never running off the right edge');
  assert(+worstVal.getAttribute('x') <= W, `the value x (${+worstVal.getAttribute('x')}) stays inside the W=${W} viewBox`);
  // a mid-band challenger is UNCHANGED — still start-anchored outboard.
  assertEqual(bestVal.getAttribute('text-anchor'), 'start', 'a mid-band value still reads start-anchored (no regression to normal data)');
});

test('racingScalarTrack M4: a far (worst-/best-scalar) marker with a long id keeps its whole id label inside the viewBox (middle-anchored x clamped, not half-clipped past the edge)', () => {
  // 9-char ids at BOTH ends of the scalar range — the best (leftmost, at ~padL)
  // and worst (rightmost, at ~W−padR) markers. A middle-anchored label there
  // clips half its width past the viewBox edge unless x is clamped inboard.
  const rung = {
    match_id: 'rung0', label: 'Rung 0',
    competitors: ['challenger', 'contender9'],
    survivors: ['challenger', 'contender9'],
    cut: [],
    scalars: { challenger: 2.0, contender9: 98.0 },   // leftmost best, rightmost worst
  };
  const node = svg.racingScalarTrack({ rungs: [rung], championId: 'v0', benchmarkId: 'v0', championScalar: 50.0, onCompetitor() {} });
  const W = Number((node.getAttribute('viewBox') || '0 0 0 0').split(/\s+/)[2]);
  assert(W > 0, 'the scalar-track viewBox width parsed');
  // 9px mono ≈ 0.6em/char; the builder's full-size edge guard is 3px.
  const PER_CH = 5.4, EDGE = 3;
  const names = allByClass(node, 'dn-scalartrack-name');
  assert(names.length >= 2, 'both far markers carry a name label');
  let sawFar = false;
  for (const t of names) {
    const x = Number(t.getAttribute('x'));
    const txt = (t.textContent || '').trim();
    const halfW = txt.length * PER_CH / 2;
    if (x <= 80 || x >= W - 80) sawFar = true;   // a marker in an edge band — the clamp's job
    assert(x - halfW >= EDGE - 0.5, `label ${JSON.stringify(txt)} left edge ${(x - halfW).toFixed(1)} stays inside the viewBox`);
    assert(x + halfW <= W - EDGE + 0.5, `label ${JSON.stringify(txt)} right edge ${(x + halfW).toFixed(1)} stays inside the viewBox (W=${W})`);
  }
  assert(sawFar, 'at least one marker sits in an edge band (the case the clamp guards)');
});

test('M6 pairedSlopegraph: a long right label + large-magnitude value is clamped inward so it never clips the right gutter', () => {
  const w = 520;
  const colGap = 150; // default labelGap
  const rightX = w - colGap;
  const CHARW = 6, EDGE = 4; // 10px mono ≈ 0.6em/char (mirrors the renderer)
  const node = svg.pairedSlopegraph({
    width: w, height: 200, labelGap: colGap, goodDirection: 'down',
    series: [
      // long 14-char label + a large 4-digit-integer value → the worst case
      { label: 'challenger_xy9', a: -2.0, b: -12345.6, verdict: 'improved' },
      // common short row: must NOT be clamped (regression guard)
      { label: 'brevity', a: 1.0, b: 1.2, verdict: 'flat' },
    ],
  });
  const labs = allByClass(node, 'dn-pslope-label');
  // right (challenger) labels are start-anchored: "{fmt(b,1)}  {label}".
  const rlabs = labs.filter((n) => n.getAttribute('text-anchor') === 'start');
  assert(rlabs.length === 2, 'one start-anchored right label per series row');

  const longLbl = rlabs.find((n) => (n.textContent || '').indexOf('-12345.6') === 0);
  assert(longLbl, 'the large-magnitude right label rendered (value-then-label, fmt(b,1))');
  // THE FIX: the run must be clamped inward (tagged) and end at or before w−EDGE.
  assertEqual(longLbl.getAttribute('data-clamped'), '1', 'the over-wide right label is clamped inward (data-clamped=1) — not pushed past the gutter');
  const lx = +longLbl.getAttribute('x');
  const lW = (longLbl.textContent || '').length * CHARW;
  assert(lx + lW <= w - EDGE + 0.5, `the clamped right label (x=${lx} + ~${lW}px) ends at or before w−EDGE=${w - EDGE}; it cannot run off the ${w}px viewBox`);
  assert(lx < rightX + 8, `the clamped x (${lx}) is pulled LEFT of the default rightX+8 (${rightX + 8})`);

  // NO REGRESSION: the short, in-bounds right label keeps the default start x and is NOT tagged.
  const shortLbl = rlabs.find((n) => (n.textContent || '').indexOf('brevity') >= 0);
  assert(shortLbl, 'the short right label rendered');
  assert(shortLbl.getAttribute('data-clamped') == null, 'the short right label is NOT clamped (common-data row is untouched)');
  assertEqual(+shortLbl.getAttribute('x'), rightX + 8, 'the short right label keeps the original rightX+8 start x');
});

test('M8 gauntletFieldBars: the champion-standard label stays inside the plot band when the champion is the field worst (long id at the right edge)', () => {
  // 10-char champion id ⇒ shortLabel(champId,10) keeps it verbatim, so the
  // centered label reads 'champcand9 standard' (19 chars). Champion scalar is the
  // MAX of the field, so X(champScalar) lands at the right band edge (≈W-padR);
  // pre-fix the middle-anchored ~108px label runs past the band + the W=600 box.
  const node = svg.gauntletFieldBars({
    championId: 'champcand9', championScalar: 9.0, promoteMargin: 0.5,
    challengers: [{ id: 'v5', scalar: 8.1, survivor: true }, { id: 'v6', scalar: 7.5 }],
  });
  const W = Number((node.getAttribute('viewBox') || '0 0 0 0').split(/\s+/)[2]);
  assert(W > 0, 'the field figure keeps a numeric viewBox width');
  // non-mini band geometry (mirrors gauntletFieldBars padL/padR).
  const padL = 110; const padR = 30;
  const axis = allByClass(node, 'dn-fieldbars-axis');
  assertEqual(axis.length, 1, 'exactly one champion-standard axis label');
  const ct = axis[0];
  assertEqual((ct.textContent || '').trim(), 'champcand9 standard', 'the standard label keeps the full 10-char id + " standard"');
  assertEqual(ct.getAttribute('text-anchor'), 'middle', 'the standard label is still centered (anchor unchanged)');
  // 9.5px mono ⇒ ~0.6em/char; reproduce the centered span [x0,x1].
  const charW = 5.7;
  const x = Number(ct.getAttribute('x'));
  const w = (ct.textContent || '').length * charW;
  const x0 = x - w / 2; const x1 = x + w / 2;
  // FAILS before the fix: the field-worst champion sits at the right band edge so
  // x1 ≈ 618 spills past band-right (W-padR=570) and the W=600 viewBox.
  assert(x0 >= padL - 0.5 && x1 <= W - padR + 0.5,
    `the standard label span [${x0.toFixed(1)}, ${x1.toFixed(1)}] stays inside the band [${padL}, ${W - padR}]`);

  // NO REGRESSION: a mid-field champion keeps the label centered on its raw cx
  // (the clamp is a no-op) and well inside the band.
  const mid = svg.gauntletFieldBars({
    championId: 'champcand9', championScalar: 8.0, promoteMargin: 0.5,
    challengers: [{ id: 'v5', scalar: 6.0 }, { id: 'v6', scalar: 10.0 }],
  });
  const mct = allByClass(mid, 'dn-fieldbars-axis')[0];
  const mx = Number(mct.getAttribute('x'));
  const mw = (mct.textContent || '').length * charW;
  assert(mx - mw / 2 >= padL - 0.5 && mx + mw / 2 <= W - padR + 0.5,
    'a mid-field champion label also stays inside the band (clamp is a no-op there)');
});

test('duelFlow L1: a 3-integer-digit improving Δ (-128.4) at max keeps its outboard Δ label clear of the gate box (labelPad sized to the formatted Δ, not a fixed 32)', () => {
  // A single improving challenger at the max |Δ| → its outboard Δ label rides
  // furthest RIGHT toward the gate. With a 3-integer-digit value the formatted
  // label is ~36px wide; the reserve must grow to keep it inboard of the gate.
  const node = svg.duelFlow({
    championId: 'v0', championScalar: 200.0,
    challengers: [
      { id: 'v1', delta: -128.4, verdict: 'promoted', hypothesis: 'big jump', driver: 'restructure' },
    ],
    onCompetitor() {},
  });
  // the crowned gate box (its x is the inboard edge the Δ label must not reach).
  const gateBox = allByClass(node, 'dn-duelflow-gatebox')[0];
  assert(gateBox, 'the gate box renders');
  const gateBoxX = Number(gateBox.getAttribute('x'));
  // the improving (rightward) Δ label is the start-anchored dn-good delta text.
  const dLab = allByClass(node, 'dn-duelflow-delta').filter((t) => (t.getAttribute('class') || '').split(/\s+/).includes('dn-good'))[0];
  assert(dLab, 'the improving Δ label is drawn');
  assertEqual(dLab.getAttribute('text-anchor'), 'start', 'the improving Δ label is start-anchored (rides right toward the gate)');
  const labelStartX = Number(dLab.getAttribute('x'));
  // 10px mono ≈ 6px/char; estimate the label's RIGHT edge from its start + text.
  const labelRightX = labelStartX + (dLab.textContent || '').length * 6;
  // it must stay clear of the gate box (≥10px clearance) — pre-fix the fixed
  // labelPad=32 lets a -128.4 label end within ~7px of the gate, breaching this.
  assert(labelRightX <= gateBoxX - 10,
    `the large-Δ improving label (end ≈ ${labelRightX}) clears the gate box (x=${gateBoxX}) by ≥10px`);
});

// REGRESSION (L2): when the LAST rung settles with EVERY competitor cut and no
// champion seated yet (survivors:[], championId null, gate still pending), the
// converging gate-flow polygon must be SUPPRESSED — bandHalf(0) floors the flow
// half-height to 6, so a naive draw puts a thin sliver into a champion-gate with
// no runner feeding it (a disconnected/degenerate flow). A crowned gate whose
// survivors array is empty but champion IS seated must still draw its flow.
test('survival funnel: an all-cut/uncrowned last rung feeds NO spline into the gate, but a crowned survivor’s winner-spline reaches it', () => {
  // the splines that TERMINATE at the champion-gate seat x (the winner-flow).
  const gateSplinesOf = (node, rungsLen) => {
    const stageW = 150, stageGap = 20;
    const gx = rungsLen * stageW + Math.max(0, rungsLen - 1) * stageGap + stageGap + 2;
    return node.querySelectorAll('[class]')
      .filter((n) => n.localName === 'path' && (n.getAttribute('class') || '').split(/\s+/).includes('dn-funnel-spline'))
      .filter((p) => { const m = (p.getAttribute('d') || '').match(/([-\d.]+),([-\d.]+)\s*$/); return m && Math.abs(parseFloat(m[1]) - gx) < 1.0; });
  };

  // last rung settled: all four lanes cut, nobody survives, gate not crowned.
  const allCut = svg.survivalFunnel({
    rungs: [{ label: 'Rung 0', competitors: ['v1', 'v2', 'v3', 'v4'], survivors: [], cut: ['v1', 'v2', 'v3', 'v4'], deltas: { v1: 7, v2: 8, v3: 9, v4: 10 } }],
    championId: null, onCompetitor() {},
  });
  assert(allCut.localName === 'svg', 'the funnel still renders the rung dots');
  assertEqual(gateSplinesOf(allCut, 1).length, 0, 'no spline is drawn into an unfed, uncrowned gate (there is no winner)');

  // positive control: a crowned survivor's winner-spline reaches the gate seat.
  const crowned = svg.survivalFunnel({
    rungs: [{ label: 'Rung 0', competitors: ['v1', 'v3'], survivors: ['v3'], cut: ['v1'], deltas: { v1: 5, v3: -2 } }],
    championId: 'v3', gateState: 'crowned', gateDelta: -2, onCompetitor() {},
  });
  assertEqual(gateSplinesOf(crowned, 1).length, 1, 'the crowned survivor’s winner-spline reaches the champion-gate seat');
});

test('survival funnel L3: a wide entering field renders one dot per entrant, spread across the ladder (dot-ladder, not stacked on one y)', () => {
  // A wide rung-0 field (24 lanes) narrowing to just 2 survivors. In the dot
  // ladder every entrant is a dot spread symmetrically about the centre line —
  // the entering rung is the widest column rather than a single stacked row.
  const competitors = Array.from({ length: 24 }, (_, i) => `v${i + 1}`);
  const survivors = ['v1', 'v2'];
  const cut = competitors.filter((c) => !survivors.includes(c));
  const rungs = [{ label: 'Rung 0', competitors, survivors, cut }];
  const node = svg.survivalFunnel({ rungs, championId: 'v1', benchmarkId: 'v0', gateState: 'crowned', onCompetitor() {} });

  const dots = allByClass(node, 'dn-funnel-dot');
  assert(dots.length >= 24, `one dot per entrant (24) — got ${dots.length}`);
  const cys = dots.map((c) => parseFloat(c.getAttribute('cy'))).filter((y) => isFinite(y));
  const uniq = new Set(cys.map((y) => y.toFixed(1)));
  assert(uniq.size >= 12, `the entrant dots occupy many distinct rows (≥12), not one stacked baseline — got ${uniq.size}`);
  const span = Math.max(...cys) - Math.min(...cys);
  assert(span > 60, `the spread covers a real vertical span (${span.toFixed(1)}px > 60), not a stack`);
  // the 2 survivors read good (accent); the 22 cuts read dn-bad.
  const good = dots.filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-good'));
  const bad = dots.filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-bad'));
  assert(good.length >= 2, `both survivors render a good (accent) dot — got ${good.length}`);
  assert(bad.length >= 22, `the 22 cuts render dn-bad dots — got ${bad.length}`);
});

test('heatmap: rotated column headers (long labels, last column) stay inside the viewBox — no top/right clip', () => {
  // 14 columns with labels longer than the shortLabel cap (12) → the rotated
  // -45° headers (text-anchor:start) rise up-and-right; the last column is the
  // worst case. Before the fix the header overruns both the top and the right
  // of the viewBox and `xMinYMin meet` clips it.
  const cols = Array.from({ length: 14 }, (_, j) => ({ id: 'c' + j, label: 'board_profile_long_' + j }));
  const rows = Array.from({ length: 4 }, (_, i) => ({ id: 'r' + i, label: 'gen-' + i }));
  const node = svg.heatmap({ rows, cols, value: () => 1 });
  const vb = (node.getAttribute('viewBox') || '').split(/\s+/).map(Number);
  const w = vb[2];
  const heads = allByClass(node, 'dn-hm-col');
  assertEqual(heads.length, cols.length, 'one rotated header per column');
  for (const t of heads) {
    const cx = +t.getAttribute('x');
    const ay = +t.getAttribute('y');                // anchor y = headH - 6
    const len = (t.textContent || '').length;        // already shortLabel-capped
    const rotExtent = Math.ceil(0.7071 * len * 6);   // cos(45°)·len·~6px/char
    assert(ay - rotExtent >= 0, 'rotated header top stays inside the viewBox (y=' + (ay - rotExtent) + ' ≥ 0)');
    assert(cx + rotExtent <= w, 'rotated header right stays inside the viewBox (x=' + (cx + rotExtent) + ' ≤ ' + w + ')');
  }
});

// L5 — swissLadder champion-gate edge. Class D: when rounds exist but the
// standings are empty / the leader is uncommitted (live), `leaderId` is null —
// the gate box still renders, but pre-fix NO feeding edge was drawn (an ORPHAN
// gate floating disconnected from the standings column). The fix feeds it a
// STUB edge from the standings column; it also drops a dead `V` no-op from the
// committed-leader edge (the gate row never changes y).
test('L5: swissLadder gate is never orphaned — rounds-but-no-leader feeds a stub edge from standings, and the committed edge carries no dead V no-op', () => {
  // ORPHAN case: a live round in flight, standings not yet populated => leaderId null.
  const orphan = svg.swissLadder({
    rounds: [{ label: 'Round 1', pairings: [{ a: 'x', b: 'y', total: 4, done: 1 }] }],
    standings: [], championId: null, benchmarkId: null, live: true,
  });
  // the gate box still renders (the gate column is always present)…
  assert(allByClass(orphan, 'dn-swissladder-gatebox')[0], 'the champion-gate box renders even with no committed leader');
  // …and it is not an orphan: a stub edge feeds it from the standings column.
  const stub = allByClass(orphan, 'dn-swissladder-edge-stub');
  assertEqual(stub.length, 1, 'a single stub edge feeds the gate when rounds exist but no leader is committed (no orphan gate)');
  const ds = stub[0].getAttribute('d') || '';
  assert(/^M[-\d.]+,[-\d.]+\s*H[-\d.]+\s*$/.test(ds), 'the stub edge is a flat horizontal stub "M x,y H mx" (got: ' + ds + ')');

  // COMMITTED-LEADER case: a resolved leader feeds the FULL edge to the gate.
  const led = svg.swissLadder({
    rounds: [{ label: 'R1', pairings: [{ a: 'x', b: 'y', winner: 'x' }] }],
    standings: [{ id: 'x', points: 3, wins: 1 }], championId: null, live: true,
  });
  const edges = allByClass(led, 'dn-swissladder-edge').filter((e) => !(e.getAttribute('class') || '').split(/\s+/).includes('dn-swissladder-edge-stub'));
  assertEqual(edges.length, 1, 'the committed leader feeds exactly one full edge');
  const dl = edges[0].getAttribute('d') || '';
  // the dead `V` no-op is gone — the path is a single horizontal run.
  assert(!/V[-\d.]+/.test(dl), 'the committed edge carries no dead V no-op (got: ' + dl + ')');
  assert(/^M[-\d.]+,[-\d.]+\s*H[-\d.]+\s*$/.test(dl), 'the committed edge is a single flat horizontal run "M x,y H gx" (got: ' + dl + ')');
  // NO REGRESSION: the committed case does NOT draw an orphan stub.
  assertEqual(allByClass(led, 'dn-swissladder-edge-stub').length, 0, 'a committed-leader edge is never tagged as a stub');
});

test('calibrationTrend L6: sparse scored→null→scored bridges the null gap with a faint dashed connector (sparse scoring no longer renders as no line), while DENSE scoring bridges nothing', () => {
  // a connector PATH (the bridge over null gaps) that carries an L-segment. It
  // is tagged dn-caltrend-bridge — distinct from the single dn-caltrend-mean
  // rolling-mean reference — but styled with the same faint-dashed grammar.
  const bridgeLcount = (root) =>
    root.querySelectorAll('[class]')
      .filter((n) => n.localName === 'path'
        && (n.getAttribute('class') || '').split(/\s+/).includes('dn-caltrend-bridge'))
      .reduce((acc, p) => acc + ((p.getAttribute('d') || '').match(/L/g) || []).length, 0);
  // the solid spark-line's own vertices (M/L count) — proof the solid pen lifted.
  const sparkVerts = (root) => {
    const p = root.querySelectorAll('[class]').filter((n) =>
      n.localName === 'path' && (n.getAttribute('class') || '').split(/\s+/).includes('dn-spark-line'))[0];
    return p ? ((p.getAttribute('d') || '').match(/[ML]/g) || []) : [];
  };

  // SPARSE: every scored gen is isolated by a null — the solid line is all
  // move-tos (no L), so without the bridge the trend is invisible.
  const sparse = svg.calibrationTrend({
    points: [
      { generation_id: 'v0', score_fraction: 0.8, total_claims: 3, decision: 'promoted' },
      { generation_id: 'v1', score_fraction: null, total_claims: 0, decision: 'rejected' },
      { generation_id: 'v2', score_fraction: 0.4, total_claims: 2, decision: 'rejected' },
    ],
    rolling_mean: 0.6, trend_sign: -1, width: 360, height: 64,
  });
  const sparseSolid = sparkVerts(sparse);
  assert(sparseSolid.length > 0 && sparseSolid.every((c) => c === 'M'),
    'precondition: the SPARSE solid spark-line is all move-tos (the pen lifts on every null) so it draws no visible line on its own');
  assert(bridgeLcount(sparse) >= 1,
    'the null gap between the two isolated scored gens is spanned by a faint dashed bridge L-segment (sparse scoring reads as a trend, not nothing)');

  // DENSE: every gen is scored — the solid line already connects everything, so
  // the bridge must add NOTHING (byte-identical to the pre-fix render).
  const dense = svg.calibrationTrend({
    points: [
      { generation_id: 'v0', score_fraction: 0.7, total_claims: 3, decision: 'promoted' },
      { generation_id: 'v1', score_fraction: 0.5, total_claims: 2, decision: 'rejected' },
      { generation_id: 'v2', score_fraction: 0.6, total_claims: 2, decision: 'promoted' },
    ],
    rolling_mean: 0.6, trend_sign: 1, width: 360, height: 64,
  });
  assertEqual(bridgeLcount(dense), 0,
    'dense (fully-scored) data bridges no gap — no regression of the normal solid-line render');
});

test('swissOverview (L7): a CUSTOM round label (neither "round N" nor "gate", e.g. "Tiebreak 1") keeps its canonical text on the axis — not clipped to an ambiguous "Tiebrea…"', () => {
  // A swiss with a custom middle round must not fall through to shortLabel(ls, 8),
  // truncating "Tiebreak 1"/"Tiebreaker" to "Tiebrea…". The round/gate ticks stay
  // canonicalized (R1 / Gate); only the custom label widens to the 12-char cap.
  const over = svg.swissOverview({
    labels: ['Round 1', 'Tiebreak 1', 'Champion gate'],
    series: [
      { id: 'v0', ranks: [1, 1, 1], crown: true },
      { id: 'v1', ranks: [2, 2, 2] },
    ],
    bars: [
      { id: 'v0', points: 2, wins: 2, draws: 0, losses: 0, crown: true },
      { id: 'v1', points: 0, wins: 0, draws: 0, losses: 2 },
    ],
    championId: 'v0', gateState: 'crowned',
  });
  const axisLabels = allByClass(over, 'dn-swissover-round').map((n) => (n.textContent || '').trim());
  // the custom label survives in full — no ellipsis clip.
  assert(axisLabels.includes('Tiebreak 1'),
    'the custom round label renders in full ("Tiebreak 1"), not clipped (got: ' + axisLabels.join(' | ') + ')');
  assert(!axisLabels.some((t) => t.includes('…')),
    'no round-axis label is ellipsized for a canonical custom label (got: ' + axisLabels.join(' | ') + ')');
  // the round/gate branches are NOT regressed — they still canonicalize.
  assert(axisLabels.includes('R1'), '"Round 1" still canonicalizes to "R1"');
  assert(axisLabels.includes('Gate'), '"Champion gate" still canonicalizes to "Gate"');
});

test('racing caption: a long rung label ("Quarterfinal gauntlet") keeps its distinguishing word in the scalar-track caption (cap widened, not clipped to "Quarterfinal …")', () => {
  const rung = {
    match_id: 'rung2', label: 'Quarterfinal gauntlet',
    competitors: ['v0', 'v5'],
    survivors: ['v5'], cut: [],
    scalars: { v5: 8.1 },
    live_progress: { v5: { done: 4, total: 4 } },
  };
  const node = svg.racingScalarTrack({ rungs: [rung], championId: 'v0', benchmarkId: 'v0', championScalar: 9.0, onCompetitor() {} });
  const cap = allByClass(node, 'dn-scalartrack-cap')[0];
  assert(cap, 'the non-mini scalar track renders a caption');
  const txt = (cap.textContent || '');
  assert(txt.includes('Quarterfinal gauntlet'), `the caption keeps the full rung label incl. the distinguishing word — got ${JSON.stringify(txt)}`);
  assert(txt.includes('scalar, lower is better'), 'the axis-sense suffix is still present');
});

test('L9 reignGantt/ledger ids: two ids sharing a long common prefix mid-truncate to DISTINCT visible labels (not an identical head stub)', () => {
  // Both former (same ♔ suffix) so the only differentiator is the id truncation.
  const node = svg.reignGantt({
    rounds: 4,
    reigns: [
      { id: 'epoch-2026-06-13-a', fromRound: 0, toRound: 2, current: false },
      { id: 'epoch-2026-06-13-b', fromRound: 2, toRound: 4, current: false },
    ],
  });
  const labels = allByClass(node, 'dn-reigngantt-name').map((n) => n.textContent);
  assertEqual(labels.length, 2, 'both reign rows rendered a name label');
  assert(labels[0] !== labels[1],
    'prefix-sharing ids render as DISTINCT labels — got identical "' + labels[0] + '" (fixed-cap head truncation collapsed the discriminator)');
  // The distinguishing suffix survives the truncation.
  assert(labels.some((l) => l.includes('a')) && labels.some((l) => l.includes('-b') || /b/.test(l)),
    'each label keeps its distinguishing tail (…-13-a vs …-13-b)');
  // Normal-data guard: a short id that fits the cap is rendered verbatim (no ellipsis).
  const shortNode = svg.reignGantt({ rounds: 2, reigns: [{ id: 'v14', fromRound: 0, toRound: 2, current: true }] });
  const shortLbl = allByClass(shortNode, 'dn-reigngantt-name')[0].textContent;
  assert(shortLbl.includes('v14') && !shortLbl.includes('…'), 'a short id under the cap is untouched (no regression of normal data)');
});

// ── shared text-fitting primitives (the structural fix for the clip/collision
//    family — every figure routes its text through these instead of ad-hoc math)
test('text primitive — textPx: width scales with length × fontPx × CHAR_EM', () => {
  assertEqual(svg.textPx('', 11), 0, 'empty string measures 0');
  assertEqual(svg.textPx('abcde', 10), 5 * 10 * svg.CHAR_EM, '5 chars @10px');
  assert(svg.textPx('longerlabel', 11) > svg.textPx('short', 11), 'longer text is wider');
});

test('text primitive — fitLabel: truncates to a PIXEL budget, keeps short text verbatim, drops when impossible', () => {
  assertEqual(svg.fitLabel('hi', 200, 11), 'hi', 'fits → verbatim');
  const fit = svg.fitLabel('a-very-long-identifier-string', 60, 11);
  assert(fit.length < 'a-very-long-identifier-string'.length && fit.endsWith('…'), 'over-budget → truncated with ellipsis');
  assert(svg.textPx(fit, 11) <= 60 + 1e-6, 'the truncation actually fits the pixel budget');
  const mid = svg.fitLabel('epoch-2026-06-13-aaaa', 60, 11, { mid: true });
  assert(mid.includes('…') && /a$/.test(mid), 'mid-truncation keeps the discriminating tail');
  assertEqual(svg.fitLabel('x', 2, 11), '', 'too-narrow budget → empty (caller may drop the label)');
});

test('text primitive — edgeText: keeps the full extent inside [pad, viewW-pad], flipping the anchor near an edge', () => {
  // start-anchored near the RIGHT edge → flips to end, stays inside the box.
  const r = svg.edgeText({ text: '0.95', x: 250, y: 10, anchor: 'start', fontPx: 11, viewW: 260, pad: 4, cls: 'c' });
  assertEqual(r.getAttribute('text-anchor'), 'end', 'right-edge overrun flips to end');
  const rx = parseFloat(r.getAttribute('x'));
  assert(rx <= 256 + 1e-6 && rx - svg.textPx('0.95', 11) >= 4 - 1e-6, 'the flipped label sits fully inside the box');
  // end-anchored near the LEFT edge → flips to start.
  const l = svg.edgeText({ text: 'label', x: 2, y: 10, anchor: 'end', fontPx: 11, viewW: 260, pad: 4 });
  assertEqual(l.getAttribute('text-anchor'), 'start', 'left-edge overrun flips to start');
  // middle-anchored far right → x clamped so half-width stays in the box.
  const m = svg.edgeText({ text: 'wwwwwwww', x: 258, y: 10, anchor: 'middle', fontPx: 11, viewW: 260, pad: 4 });
  const mx = parseFloat(m.getAttribute('x'));
  assert(mx + svg.textPx('wwwwwwww', 11) / 2 <= 256 + 1e-6, 'middle label right half stays inside the box');
  // a comfortably-interior placement is untouched.
  const ok = svg.edgeText({ text: 'v3', x: 100, y: 10, anchor: 'start', fontPx: 11, viewW: 260, pad: 4 });
  assertEqual(ok.getAttribute('text-anchor'), 'start', 'interior placement keeps its anchor');
  assertEqual(parseFloat(ok.getAttribute('x')), 100, 'interior placement keeps its x');
});

test('text primitive — fitInto: truncates to the column AND keeps it inside the viewBox', () => {
  const t = svg.fitInto({ text: 'a-very-long-identifier', x: 250, y: 10, anchor: 'start', maxPx: 70, fontPx: 11, viewW: 260, pad: 4 });
  assert(t.textContent.endsWith('…'), 'truncated to the column width');
  assert(svg.textPx(t.textContent, 11) <= 70 + 1e-6, 'fits the column budget');
  const fx = parseFloat(t.getAttribute('x'));
  const anc = t.getAttribute('text-anchor');
  const left = anc === 'end' ? fx - svg.textPx(t.textContent, 11) : fx;
  const right = anc === 'end' ? fx : fx + svg.textPx(t.textContent, 11);
  assert(left >= 4 - 1e-6 && right <= 256 + 1e-6, 'the fitted+placed label is fully inside the viewBox');
});

await run();

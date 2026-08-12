// test/candidate_identity.test.mjs — THE CANDIDATE'S IDENTITY (issue #194 §4).
//
// The dossier used to open on "born from v0 by a patch" — true of every
// candidate ever minted, and therefore about none of them. It now opens on the
// PROPOSAL (core idea · why · what it expected to move · the sites it touched ·
// the diff), with a ONE-LINE verdict sentence under it, and ranks its gates:
// the round that decided THIS candidate leads at full detail, the rounds it
// merely defended collapse.
//
// Pins:
//   * buildProposalModel: lifts the recorded hypothesis + patches; the metric
//     movement WINS over a drift movement naming the same target (the grader's
//     own precedence); declared `modulating` ids stand in when no patch record
//     was read back; a record with nothing proposed → null (the seed);
//   * proposalHeader: renders the idea, the claims in the scorecard's arrow
//     grammar, the sites WITH their op kinds, the diff-size line + diff link;
//   * verdictSentence: assembled from STRUCTURED fields ONLY — every clause
//     drops when its field is absent (a SHORTER true sentence, never a
//     fabricated one), and the margin clause is signed against the promote bar;
//   * buildGateStack: the DECIDING round leads at full detail; the defences
//     collapse to expandable summary rows and their expansion survives a
//     re-render;
//   * the whole thing degrades: no proposal / no gate → the dossier is
//     byte-identical to before the feature.

import { installDom, test, run, assert, assertEqual, assertDeep } from './harness.mjs';

installDom();

const cand = await import('../js/views/candidate.js');

function classOf(node) { return (node && node.getAttribute && node.getAttribute('class')) || ''; }
function hasClass(node, cls) { return classOf(node).split(/\s+/).includes(cls); }
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => hasClass(n, cls));
}
function mountInto(node) { const h = document.createElement('div'); if (node) h.appendChild(node); return h; }

// The recorded experiment shape, keys verbatim (core/experiment.py
// HypothesisSpec + core/mutation.py Patch, as build_epoch_view serves them).
function expFixture(overrides) {
  return Object.assign({
    generation_id: 'v2',
    parent_generation_id: 'v0',
    hypothesis: {
      core_idea: 'Name the audience up front and demand a slide outline before prose',
      why: 'The judge flags narrative drift whenever the agent starts writing paragraphs first.',
      modulating: ['prompt.system'],
      expected_drift_movements: [{ kind: 'off_topic', direction: 'decrease', magnitude: 'medium' }],
      expected_metric_movements: [
        { metric_name: 'drift:off_topic', direction: 'decrease', magnitude: 'large' },
        { metric_name: 'cost:tokens_spent', direction: 'increase', magnitude: 'small' },
      ],
      expected_pass_rate_delta: '+0.10 to +0.20',
      risks: 'A longer prompt may crowd out the task description.',
    },
    patches: {
      'prompt.system': { mutation_id: 'prompt.system', op: 'replace', new_content: 'a\nb\nc' },
      'agent.temperature': { mutation_id: 'agent.temperature', op: 'set_numeric', new_numeric: 0.3 },
    },
  }, overrides || {});
}

// ── 1. the proposal MODEL ───────────────────────────────────────────────────
test('buildProposalModel: lifts the proposer’s own words, claims, sites and diff size', () => {
  const m = cand.buildProposalModel(expFixture());
  assert(m, 'a recorded proposal yields a model');
  assert(m.coreIdea.startsWith('Name the audience'), 'the core idea is the proposer’s sentence');
  assert(m.why.includes('narrative drift'), 'the reasoning is carried verbatim');
  assertEqual(m.passRateClaim, '+0.10 to +0.20', 'the free-text pass-rate claim rides along');
  assertDeep(m.sites, [
    { mutation_id: 'agent.temperature', op: 'set_numeric' },
    { mutation_id: 'prompt.system', op: 'replace' },
  ], 'every patched site is named WITH the op kind that edited it');
  assertEqual(m.patchCount, 2, 'the patch count is the record’s, not a guess');
  assertEqual(m.newLines, 3, 'the diff size counts the lines of NEW content the patches carry');
});

test('buildProposalModel: claims are keyed EXACTLY as the prediction grader keys them', () => {
  // The grader (query/hypothesis_view.py `_expected_index`) keys a metric claim
  // by its `metric_name` and a drift claim by its bare `kind`, into ONE target
  // map — so "drift:off_topic" and "off_topic" are two targets, not one, and
  // this header must show the same three claims the scorecard will score.
  const m = cand.buildProposalModel(expFixture());
  assertDeep(m.movements.map((mv) => mv.target), ['drift:off_topic', 'cost:tokens_spent', 'off_topic'],
    'the namespaced claims lead, the bare drift kind follows — the grader’s own keying');

  // when the two DO collide on one name, the metric claim wins (same precedence).
  const collide = expFixture();
  collide.hypothesis.expected_metric_movements = [{ metric_name: 'off_topic', direction: 'decrease', magnitude: 'large' }];
  const c = cand.buildProposalModel(collide);
  assertDeep(c.movements.map((mv) => mv.target), ['off_topic'], 'one target, named once');
  assertEqual(c.movements[0].magnitude, 'large', 'the metric claim wins the collision, as it does at grading time');

  // a proposal that made only the OLD drift-shaped claim still surfaces it.
  const legacy = expFixture();
  legacy.hypothesis.expected_metric_movements = [];
  assertDeep(cand.buildProposalModel(legacy).movements.map((mv) => mv.target), ['off_topic'],
    'a drift-only hypothesis still reads');
});

test('buildProposalModel: DECLARED modulating ids stand in when no patch record was read back', () => {
  const m = cand.buildProposalModel(expFixture({ patches: {} }));
  assertDeep(m.sites, [{ mutation_id: 'prompt.system', op: null }],
    'the declared site is named, with NO op — we do not know how it was edited');
  assertEqual(m.newLines, 0, 'and no diff size is invented');
});

test('buildProposalModel: nothing proposed (the seed) → null, so the header is simply absent', () => {
  assertEqual(cand.buildProposalModel(null), null, 'no experiment record → no model');
  assertEqual(cand.buildProposalModel({ generation_id: 'v0' }), null, 'a bare record → no model');
  assertEqual(cand.buildProposalModel({ hypothesis: {}, patches: {} }), null, 'an empty hypothesis → no model');
});

// ── 2. the proposal HEADER ──────────────────────────────────────────────────
test('proposalHeader: the IDEA leads, with the reasoning, claims, sites and the diff a click away', () => {
  const host = mountInto(cand.proposalHeader(cand.buildProposalModel(expFixture()), {
    diffHref: '#/diff/e0/v2',
  }));
  assert(allByClass(host, 'dn-proposal-idea')[0].textContent.startsWith('Name the audience'),
    'the core idea is the first thing the dossier says');
  assert(allByClass(host, 'dn-proposal-why')[0].textContent.includes('narrative drift'), 'the why follows it');

  // the claims, in the prediction scorecard's OWN arrow grammar.
  const movements = allByClass(host, 'dn-proposal-mv');
  assert(movements.length >= 3, 'each falsifiable claim + the pass-rate claim gets a slot');
  assert(host.textContent.includes('↓'), 'a predicted decrease reads as the scorecard’s down arrow');
  assert(host.textContent.includes('↑'), 'a predicted increase reads as its up arrow');
  assert(host.textContent.includes('+0.10 to +0.20'), 'the free-text pass-rate claim is shown as written');

  // the sites, WITH op kinds, and the honest diff-size line.
  const sites = allByClass(host, 'dn-proposal-site').map((n) => n.textContent);
  assert(sites.some((t) => t.includes('prompt.system') && t.includes('replace')), 'each site names its op kind');
  const size = allByClass(host, 'dn-proposal-size')[0].textContent;
  assert(size.includes('2 sites') && size.includes('2 patches') && size.includes('3 lines of new content'),
    'the diff-size line states exactly what it measures');
  assertEqual(allByClass(host, 'dn-proposal-difflink')[0].getAttribute('href'), '#/diff/e0/v2',
    'the header links out to the side-by-side diff');
});

test('proposalHeader: no diff route → no dangling link; no model → no header at all', () => {
  const host = mountInto(cand.proposalHeader(cand.buildProposalModel(expFixture()), {}));
  assertEqual(allByClass(host, 'dn-proposal-difflink').length, 0, 'no route → no link (never a dead one)');
  assertEqual(cand.proposalHeader(null, {}), null, 'no proposal → the header is omitted entirely');
});

// ── 3. the VERDICT SENTENCE ─────────────────────────────────────────────────
// deriveGateExplain's shape (candidate.js): {decision, decidingRule,
// decidingLabel, detail, deltaScalar, margin, regressed, reason}.
function explain(overrides) {
  return Object.assign({
    decision: 'rejected',
    decidingRule: 'pass_rate_monotonicity',
    decidingLabel: 'Pass-rate monotonicity',
    detail: '0.73 → 0.72 (+0.01; needs ≤ -0.02)',
    deltaScalar: 0.014,
    margin: 0.02,
    regressed: 'q3_metrics_outline',
    regressedFrom: 'rule',
  }, overrides || {});
}

test('verdictSentence: decision · deciding rule · what regressed · how far from the margin', () => {
  assertEqual(cand.verdictSentence(explain()),
    'rejected · pass-rate monotonicity · regressed q3_metrics_outline · 0.034 short of the 0.020 margin',
    'the whole sentence assembles from STRUCTURED fields only');
});

test('verdictSentence: the margin clause is signed against the promote bar (short vs clear)', () => {
  const cleared = cand.verdictSentence(explain({
    decision: 'promoted', decidingRule: null, decidingLabel: null, regressed: null, regressedFrom: null,
    deltaScalar: -0.08, margin: 0.02,
  }));
  assertEqual(cleared, 'promoted · 0.060 clear of the 0.020 margin',
    'a Δ past the bar reads as CLEARED by the distance, not "short" of it');
});

test('verdictSentence: a PRIMARY DRIVER is called a driver, never "regressed"', () => {
  // The bug this pins, caught on a real promoted candidate: when no
  // monotonicity rule fires, deriveGateExplain falls back to the gate's
  // primary_driver judge — the judge that moved the round MOST, in either
  // direction. Printing "regressed <judge>" under a PROMOTED verdict asserts a
  // regression that did not happen.
  const promoted = cand.verdictSentence(explain({
    decision: 'promoted', decidingRule: null, decidingLabel: null,
    regressed: 'no_fabricated_numbers', regressedFrom: 'driver',
    deltaScalar: -18.001, margin: 0.01,
  }));
  assertEqual(promoted, 'promoted · driver no_fabricated_numbers · 17.991 clear of the 0.010 margin',
    'the primary driver is NAMED as the driver — the useful fact, told truthfully');
  assert(cand.verdictSentence(explain()).includes('regressed q3_metrics_outline'),
    '...while a rule-named entry still reads as the regression it was');
});

test('deriveGateExplain (via the dossier): tags WHERE the named entry came from', () => {
  // pinned through the public sentence, since deriveGateExplain is internal:
  // a gate that names a regressed predicate keeps "regressed"; a gate that
  // only carries a primary_driver switches the word.
  assert(cand.verdictSentence(explain({ regressedFrom: null })).includes('regressed '),
    'an untagged (pre-feature) explain keeps the historical wording');
});

test('verdictSentence: every clause DROPS with its field — a shorter true sentence, never a fabricated one', () => {
  assertEqual(cand.verdictSentence(explain({ regressed: null })),
    'rejected · pass-rate monotonicity · 0.034 short of the 0.020 margin',
    'no regressed entry recorded → that clause is simply absent');
  assertEqual(cand.verdictSentence(explain({ decidingLabel: null, decidingRule: null })),
    'rejected · regressed q3_metrics_outline · 0.034 short of the 0.020 margin',
    'no deciding rule recorded → that clause is absent');
  assertEqual(cand.verdictSentence(explain({ margin: null })),
    'rejected · pass-rate monotonicity · regressed q3_metrics_outline',
    'no margin recorded → no margin distance is invented');
  assertEqual(cand.verdictSentence(explain({ deltaScalar: null })),
    'rejected · pass-rate monotonicity · regressed q3_metrics_outline',
    'no Δ recorded → likewise');
  assertEqual(cand.verdictSentence(explain({
    decidingLabel: null, decidingRule: null, regressed: null, margin: null, deltaScalar: null,
  })), 'rejected', 'a bare decision is still a true sentence');
});

test('verdictSentence: nothing to say → null (an unsettled candidate makes no claim)', () => {
  assertEqual(cand.verdictSentence(null), null, 'no gate → no sentence');
  assertEqual(cand.verdictSentence(explain({ decision: 'pending' })), null, 'a pending gate → no sentence');
  assertEqual(cand.verdictSentence(explain({ decision: null })), null, 'no decision → no sentence');
});

test('verdictSentenceEl: the DECISION earns the tone; the line adds no colour of its own', () => {
  assert(hasClass(cand.verdictSentenceEl(explain()), 'dn-bad-t'), 'a rejection reads in the bad tone');
  assert(hasClass(cand.verdictSentenceEl(explain({ decision: 'promoted' })), 'dn-good-t'),
    'a promotion reads in the good tone');
  assertEqual(cand.verdictSentenceEl(explain({ decision: 'deferred' })).getAttribute('class'),
    'dn-verdictline', 'a deferral is untoned — the gate has not answered');
  assertEqual(cand.verdictSentenceEl(null), null, 'no sentence → no node');
});

// ── 4. the GATE STACK: deciding leads, defences collapse ────────────────────
function gateFixture(decision, delta) {
  return {
    decision, delta_scalar: delta, deciding_rule: 'scalar_margin', margin: 0.02,
    rules: [
      { id: 'scalar_margin', label: 'Scalar margin', status: decision === 'promoted' ? 'pass' : 'fail', detail: 'd', fired: decision !== 'promoted' },
    ],
  };
}

function stateFixture() {
  return {
    baseline: false,
    gateSpecs: [
      { champ: 'v0', chall: 'v2', role: 'as challenger' },
      { champ: 'v2', chall: 'v5', role: 'defended' },
      { champ: 'v2', chall: 'v6', role: 'defended' },
    ],
    gates: [gateFixture('promoted', -0.08), gateFixture('rejected', 0.03), gateFixture('rejected', 0.01)],
    judgeComparisons: [null, null, null],
  };
}

test('buildGateStack: the round that DECIDED this candidate leads at full detail', () => {
  cand._resetDefenceExpansion();
  const sections = cand.buildGateStack(stateFixture());
  const host = document.createElement('div');
  for (const s of sections) host.appendChild(s);

  const heads = host.querySelectorAll('[class]').filter((n) => n.tagName === 'H2').map((n) => n.textContent);
  assert(heads[0].includes('v0 → v2') && heads[0].includes('deciding'),
    'the challenger round leads, and is NAMED as the deciding one');
  // its full rule ladder is present, unexpanded.
  assertEqual(allByClass(host, 'dn-rules').length >= 1, true, 'the deciding gate shows its rule ladder outright');
});

test('buildGateStack: the DEFENDED rounds collapse to expandable summary rows — nothing is dropped', () => {
  cand._resetDefenceExpansion();
  const host = document.createElement('div');
  for (const s of cand.buildGateStack(stateFixture())) host.appendChild(s);

  const defences = allByClass(host, 'dn-gate-defence');
  assertEqual(defences.length, 2, 'one collapsed row per defended round');
  for (const d of defences) assert(!d.hasAttribute('open'), 'a defence starts collapsed');
  const summary = defences[0].children.find((c) => c.tagName === 'SUMMARY');
  assert(summary.textContent.includes('vs v5'), 'the summary names the challenger it faced');
  assert(summary.textContent.includes('rejected'), 'and how the gate answered');
  assert(summary.textContent.includes('Δ +0.030'), 'and the Δ, at gate precision');
  // the FULL panel is inside — collapsed, not discarded.
  assert(allByClass(defences[0], 'dn-gate').length === 1, 'the full gate panel rides inside the collapsed row');
});

test('buildGateStack: an expanded defence SURVIVES a re-render (the digest-gated rebuild must not collapse it)', () => {
  cand._resetDefenceExpansion();
  const first = document.createElement('div');
  for (const s of cand.buildGateStack(stateFixture())) first.appendChild(s);
  const row = allByClass(first, 'dn-gate-defence')[0];
  row.open = true;
  row.dispatchEvent({ type: 'toggle' });

  const rebuilt = document.createElement('div');
  for (const s of cand.buildGateStack(stateFixture())) rebuilt.appendChild(s);
  assert(allByClass(rebuilt, 'dn-gate-defence')[0].hasAttribute('open'),
    'the operator’s expand is remembered across the rebuild');
  assert(!allByClass(rebuilt, 'dn-gate-defence')[1].hasAttribute('open'),
    '...and only for the row they opened');
});

test('buildGateStack: a champion that never challenged shows only its defences (no empty "deciding" section)', () => {
  cand._resetDefenceExpansion();
  const s = stateFixture();
  s.gateSpecs = s.gateSpecs.slice(1);
  s.gates = s.gates.slice(1);
  s.judgeComparisons = s.judgeComparisons.slice(1);
  const host = document.createElement('div');
  for (const n of cand.buildGateStack(s)) host.appendChild(n);
  const heads = host.querySelectorAll('[class]').filter((n) => n.tagName === 'H2').map((n) => n.textContent);
  assertEqual(heads.length, 1, 'no phantom empty gate section');
  assert(heads[0].startsWith('Defended rounds'), 'the defences stand alone');
});

test('buildGateStack: no recorded gate → the honest empty, worded for the seed vs a challenger', () => {
  cand._resetDefenceExpansion();
  const none = { baseline: false, gateSpecs: [], gates: [], judgeComparisons: [] };
  const host = mountInto(cand.buildGateStack(none)[0]);
  assert(host.textContent.includes('No gate decomposition recorded'), 'a challenger with no gate says so');

  const seed = mountInto(cand.buildGateStack({ baseline: true, gateSpecs: [], gates: [], judgeComparisons: [] })[0]);
  assert(seed.textContent.includes('defines the loss floor'), 'the seed is explained, not reported as missing');
});

// ── 5. the digest ───────────────────────────────────────────────────────────
test('proposalDigest: byte-identical on a no-op beat; flips when the proposal or its patches change', () => {
  const a = JSON.stringify(cand.proposalDigest(cand.buildProposalModel(expFixture())));
  assertEqual(JSON.stringify(cand.proposalDigest(cand.buildProposalModel(expFixture()))), a,
    'a no-op heartbeat re-emits an identical digest');

  const repatched = expFixture();
  repatched.patches['prompt.system'].new_content = 'a\nb\nc\nd';
  assert(JSON.stringify(cand.proposalDigest(cand.buildProposalModel(repatched))) !== a,
    'a re-read patch (a different diff size) repaints the header');

  assertEqual(cand.proposalDigest(null), null, 'the seed contributes NOTHING to the dossier digest');
});

// ── 6. the dossier, end to end: the idea leads, the numbers follow ─────────

const { router, lookupFixture, freshState } = await import('./fixtures.mjs');
const ID_EPOCH = '2026-08-01_identity';

function installIdentityFetch() {
  const gens = [
    { generation_id: 'v0', epoch_id: ID_EPOCH, parent_generation_id: '', promoted: true },
    { generation_id: 'v2', epoch_id: ID_EPOCH, parent_generation_id: 'v0', promoted: false },
  ];
  const F = {
    '/api/epoch': {
      epoch_id: ID_EPOCH, closed: true, goal: 'Identity.', current_champion: 'v0',
      experiments: [
        { generation_id: 'v0', parent_generation_id: '', decision: 'baseline', promoted: true },
        Object.assign(expFixture(), { decision: 'rejected', promoted: false, outcome: { decision: 'rejected' } }),
      ],
      board: [{ entry_id: 'b1', kind: 'single_turn' }],
    },
    '/api/lineage': { generations: gens },
    '/api/tournaments': { epoch_id: ID_EPOCH, champion_lineage: ['v0'], matchups: [
      { champion: 'v0', challenger: 'v2', decision: 'rejected', delta_scalar: 0.014, ran_at: 'a' },
    ], tournaments: [] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 0.72 }, { generation_id: 'v2', scalar: 0.73 }] },
    [`/api/round/${ID_EPOCH}/v0/v2/gate`]: {
      decision: 'rejected', delta_scalar: 0.014, delta_pass_rate: -0.25, margin: 0.02,
      deciding_rule: 'pass_rate_monotonicity', regressed_predicate: 'q3_metrics_outline',
      rules: [
        { id: 'scalar_margin', label: 'Scalar margin', status: 'pass', detail: 'd', fired: false },
        { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'fail', detail: 'd', fired: true },
      ],
    },
  };
  for (const g of gens) F[`/api/generation/${ID_EPOCH}/${g.generation_id}/per-entry`] = { entries: [{ entry_id: 'b1', drift_loss: 0.5 }] };
  globalThis.fetch = async (path) => {
    const v = lookupFixture(F, path);
    return v !== undefined ? { ok: true, json: async () => v } : { ok: false, status: 404, json: async () => ({}) };
  };
}

test('candidate dossier: opens on the IDEA, with the verdict sentence under it and the lifecycle below', async () => {
  freshState(); installIdentityFetch(); cand._resetDefenceExpansion();
  const host = document.createElement('div');
  await cand.render(host, { navigate() {}, href: router.href }, { epochId: ID_EPOCH, gen: 'v2' });

  const proposal = allByClass(host, 'dn-proposal')[0];
  assert(proposal, 'the proposal header rendered on the dossier');
  assert(proposal.textContent.includes('Name the audience'), 'it leads with the candidate’s core idea');

  const verdict = allByClass(host, 'dn-verdictline')[0];
  assert(verdict, 'the one-line verdict sentence rendered');
  assertEqual(verdict.textContent,
    'rejected · pass-rate monotonicity · regressed q3_metrics_outline · 0.034 short of the 0.020 margin',
    'assembled from the SERVED structured gate fields — deciding_rule + regressed_predicate + margin');

  // ORDER: the identity precedes the lifecycle figure it explains.
  const flat = host.querySelectorAll('[class]');
  const proposalAt = flat.indexOf(proposal);
  const dagAt = flat.findIndex((n) => hasClass(n, 'dn-dagpane') || (n.tagName === 'H2' && n.textContent.startsWith('Lifecycle')));
  assert(proposalAt >= 0 && dagAt > proposalAt, 'the idea comes before the lifecycle DAG, not after it');

  // the PRESERVED figures are untouched by the reorder.
  const heads = flat.filter((n) => n.tagName === 'H2').map((n) => n.textContent);
  assert(heads.some((h) => h.startsWith('Lifecycle')), 'the lifecycle DAG is preserved');
  assert(heads.some((h) => h.startsWith('Per-board scoring')), 'the per-board dumbbell is preserved');
  assert(heads.some((h) => h.startsWith('Match-ups')), 'the match-ups panel is preserved');
});

test('candidate dossier: the SEED shows no proposal header and no verdict sentence (nothing invented)', async () => {
  freshState(); installIdentityFetch(); cand._resetDefenceExpansion();
  const host = document.createElement('div');
  await cand.render(host, { navigate() {}, href: router.href }, { epochId: ID_EPOCH, gen: 'v0' });
  assertEqual(allByClass(host, 'dn-proposal').length, 0, 'the seed proposed nothing, so it says nothing');
  assertEqual(allByClass(host, 'dn-verdictline').length, 0, 'and it faced no gate, so it claims no verdict');
  assert(host.textContent.includes('defines the loss floor'), 'the seed’s role is explained instead');
});

await run();

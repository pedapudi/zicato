// test/v2_slopegraph.test.mjs — the tournament slopegraph (DASHBOARD-V2
// §3, the corrected visual language; §8 done-criteria).
//
// The headline visual: the tournament & promotion as a Tufte slopegraph /
// bumps chart. This pins the load-bearing invariants:
//   * PROMOTE → green slope, the challenger node JOINS the champion
//     through-line (becomes the next champion node).
//   * REJECT → red slope, a detached FADED node that does NOT join the
//     line (falls away).
//   * RUNNING → amber, dashed, pulsing (the live in-flight matchup).
//   * Tufte endpoint labels (id + scalar); a verdict glyph per matchup.
//   * Interactive: hover a slope/node → tooltip (verdict · Δscalar ·
//     fired rule); click a matchup → onMatchup(challengerId); click a
//     node → onGeneration(id).
//   * Honest at 0 / 1 / few rounds (no empty/NaN SVG).
//   * The Tournament VIEW: rounds built from the epoch contract + lineage
//     scalars + the live active-tournament; deep-link ensure-load of the
//     contract; digest-gated (no flash under SSE).

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

const slope = await import('../js/v2/components/slopegraph.js');
const { slopegraph, computeSlopegraphLayout } = slope;
const tourn = await import('../js/v2/views/tournament.js');
const { buildRounds, renderTournament, resetTournamentView } = tourn;
const { state } = await import('../js/core/state.js');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function descendantsWithClass(node, cls) {
  const out = [];
  const walk = (n) => {
    if (!n || n.nodeType !== 1) return;
    if (n.classList && n.classList.contains(cls)) out.push(n);
    for (const c of n.children) walk(c);
  };
  walk(node);
  return out;
}
function firstWithClass(node, cls) { return descendantsWithClass(node, cls)[0] || null; }
function allText(node) { return node && node.textContent ? node.textContent : ''; }

const REJECT = Symbol('reject');
function installFetch(routes) {
  globalThis.fetch = (path) => {
    const p = String(path);
    for (const [key, value] of Object.entries(routes)) {
      if (p === key || p.startsWith(key)) {
        if (value === REJECT) return Promise.reject(new Error(`${p} -> HTTP 500`));
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(value) });
      }
    }
    return Promise.reject(new Error(`${p} -> HTTP 404`));
  };
}
async function settle() {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
  await new Promise((r) => setTimeout(r, 0));
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
}
function freshHost() {
  const body = globalThis.document.body;
  for (const child of [...body.children]) body.removeChild(child);
  const host = globalThis.document.createElement('div');
  host.setAttribute('id', 'v2-view');
  body.appendChild(host);
  return host;
}

// A representative three-round slopegraph: promote, reject, running.
function roundsFixture() {
  return [
    { round: 0, champion: { id: 'v0', scalar: 0.80 }, challenger: { id: 'v1', scalar: 0.55 }, decision: 'promoted', deltaScalar: -0.25, firedRule: null },
    { round: 1, champion: { id: 'v1', scalar: 0.55 }, challenger: { id: 'v2', scalar: 0.70 }, decision: 'rejected', deltaScalar: 0.15, firedRule: 'scalar_margin' },
    { round: 2, champion: { id: 'v1', scalar: 0.55 }, challenger: { id: 'v3', scalar: null }, decision: 'running', deltaScalar: null, firedRule: null },
  ];
}

// The epoch contract shape (state.epochDef.experiments) the view builds from.
function epochDefFixture() {
  return {
    epoch_id: '2026-05-30_e0',
    experiments: [
      // v0 — the baseline seed (no parent, no outcome) → the first champion.
      { generation_id: 'v0', parent_generation_id: '', hypothesis: { core_idea: 'baseline' } },
      // v1 — promoted challenger of v0.
      {
        generation_id: 'v1', parent_generation_id: 'v0',
        hypothesis: { core_idea: 'tighten the prompt' },
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.25, rejection_reason: '' },
      },
      // v2 — rejected challenger of v1.
      {
        generation_id: 'v2', parent_generation_id: 'v1',
        hypothesis: { core_idea: 'noisy variation' },
        outcome: { tournament_decision: 'rejected', scalar_score_delta: 0.15, rejection_reason: 'regressed past margin' },
      },
    ],
  };
}
function lineageFixture() {
  return {
    generations: [
      { id: 'v0', scalar: 0.80, parent_id: null, verdict: 'promoted' },
      { id: 'v1', scalar: 0.55, parent_id: 'v0', verdict: 'promoted' },
      { id: 'v2', scalar: 0.70, parent_id: 'v1', verdict: 'rejected' },
    ],
  };
}

// ===========================================================================
// Layout — the promote-joins / reject-falls-away invariants (no DOM).
// ===========================================================================

test('layout: promote → the challenger node JOINS the line; reject → it does NOT', () => {
  const L = computeSlopegraphLayout(roundsFixture(), { live: true });
  assertEqual(L.mode, 'plot');
  assertEqual(L.matchups.length, 3);
  // Promote joins; reject + running fall away / are in flight.
  assertEqual(L.matchups[0].challenger.joins, true, 'promoted challenger joins the line');
  assertEqual(L.matchups[1].challenger.joins, false, 'rejected challenger falls away');
  assertEqual(L.matchups[2].challenger.joins, false, 'running challenger is not yet on the line');
});

test('layout: the champion through-line carries the PROMOTED challenger value forward', () => {
  const L = computeSlopegraphLayout(roundsFixture(), {});
  // After v1 was promoted (lower loss → higher on screen → smaller y), the
  // round-1 champion node sits at v1's y, ABOVE round-0's champion (v0).
  const c0 = L.matchups[0].champion.y;       // v0 @ 0.80
  const c1 = L.matchups[1].champion.y;       // v1 @ 0.55
  assert(c1 < c0, `the line descended after a promotion (y up the screen): ${c1} < ${c0}`);
  assert(L.championPath.length > 0, 'a champion through-line is drawn');
});

test('layout: a running last round splits into the live champion segment', () => {
  const L = computeSlopegraphLayout(roundsFixture(), { live: true });
  assert(L.championPathLive.length > 0, 'the hop into the running matchup is the live segment');
});

test('layout: 0 rounds → an honest empty mode (no NaN SVG)', () => {
  assertEqual(computeSlopegraphLayout([]).mode, 'empty');
  assertEqual(computeSlopegraphLayout(null).mode, 'empty');
});

test('layout: 1 round still plots (no degenerate geometry)', () => {
  const L = computeSlopegraphLayout([roundsFixture()[0]], {});
  assertEqual(L.mode, 'plot');
  assertEqual(L.matchups.length, 1);
  // Every coordinate is finite — never a NaN.
  for (const m of L.matchups) {
    assert(isFinite(m.champion.y) && isFinite(m.challenger.y), 'finite node y');
    assert(isFinite(m.x), 'finite column x');
  }
});

test('layout: a null challenger scalar (mid-run) lands on a finite mid-line, never NaN', () => {
  const L = computeSlopegraphLayout([
    { round: 0, champion: { id: 'v0', scalar: 0.5 }, challenger: { id: 'v1', scalar: null }, decision: 'running' },
  ], { live: true });
  assert(isFinite(L.matchups[0].challenger.y), 'a null scalar maps to a finite y');
});

// ===========================================================================
// Render — color/glyph encoding, labels, interactivity.
// ===========================================================================

test('render: promote slope is green + joins; reject slope is red + falls away; running is amber', () => {
  const node = slopegraph({ rounds: roundsFixture(), live: true });
  // Slope edges carry decision-keyed classes.
  assert(firstWithClass(node, 'v2-slope-edge-promoted'), 'a promoted (green) slope');
  assert(firstWithClass(node, 'v2-slope-edge-rejected'), 'a rejected (red) slope');
  assert(firstWithClass(node, 'v2-slope-edge-running'), 'a running (amber) slope');
  // Challenger nodes carry joins / falls semantics (redundant to color).
  assert(firstWithClass(node, 'v2-slope-node-joins'), 'the promoted challenger node JOINS the line');
  assert(firstWithClass(node, 'v2-slope-node-falls'), 'the rejected challenger node FALLS away');
});

test('render: the running challenger node is marked live (the pulse hook)', () => {
  const node = slopegraph({ rounds: roundsFixture(), live: true });
  const live = descendantsWithClass(node, 'v2-slope-node-challenger')
    .find((n) => n.getAttribute('data-live') === 'true');
  assert(live != null, 'the in-flight challenger node carries data-live for the CSS pulse');
});

test('render: Tufte endpoint labels carry the gen id + scalar', () => {
  const node = slopegraph({ rounds: roundsFixture(), live: true });
  const txt = allText(node);
  assert(txt.includes('v0') && txt.includes('v1') && txt.includes('v2'), 'gen ids labeled');
  assert(txt.includes('0.800') && txt.includes('0.550'), 'scalars labeled (Tufte endpoints)');
});

test('render: hover a slope reveals the tooltip with verdict · Δscalar · fired rule', () => {
  const node = slopegraph({ rounds: roundsFixture(), live: true });
  const tip = firstWithClass(node, 'v2-slope-tip');
  assert(tip != null, 'the tooltip element exists');
  assertEqual(tip.getAttribute('data-show'), 'false', 'hidden until hover');
  // Hover the rejected matchup hit target (it carries the fired rule).
  const hit = descendantsWithClass(node, 'v2-slope-hit')
    .find((h) => h.getAttribute('data-decision') === 'rejected');
  assert(hit != null, 'a rejected slope hit target exists');
  hit.dispatchEvent(makeEvent('mouseenter'));
  assertEqual(tip.getAttribute('data-show'), 'true', 'hover shows the tooltip');
  const tipText = allText(tip);
  assert(tipText.includes('rejected'), 'tooltip shows the verdict');
  assert(tipText.includes('+0.150') || tipText.includes('0.150'), 'tooltip shows Δscalar');
  assert(tipText.includes('scalar_margin'), 'tooltip shows the fired gate rule');
  hit.dispatchEvent(makeEvent('mouseleave'));
  assertEqual(tip.getAttribute('data-show'), 'false', 'leave hides the tooltip');
});

test('render: clicking a matchup slope fires onMatchup(challengerId)', () => {
  let got = null;
  const node = slopegraph({
    rounds: roundsFixture(), live: true,
    onMatchup: (id) => { got = id; },
  });
  const hit = descendantsWithClass(node, 'v2-slope-hit')
    .find((h) => h.getAttribute('data-challenger') === 'v2');
  assert(hit != null, 'the v2 matchup hit target exists');
  hit.dispatchEvent(makeEvent('click', { preventDefault() {} }));
  assertEqual(got, 'v2', 'onMatchup received the challenger id');
});

test('render: clicking a node fires onGeneration(id)', () => {
  let got = null;
  const node = slopegraph({
    rounds: roundsFixture(), live: true,
    onGeneration: (id) => { got = id; },
  });
  const champNode = descendantsWithClass(node, 'v2-slope-node-champion')
    .find((n) => n.getAttribute('data-gen') === 'v0');
  assert(champNode != null, 'the v0 champion node exists');
  champNode.dispatchEvent(makeEvent('click', { preventDefault() {} }));
  assertEqual(got, 'v0', 'onGeneration received the node id');
});

test('render: 0 rounds → a labeled empty state, never a blank SVG', () => {
  const node = slopegraph({ rounds: [] });
  assertEqual(node.getAttribute('data-mode'), 'empty');
  assert(allText(node).toLowerCase().includes('no tournament'), 'reads honestly');
  assertEqual(descendantsWithClass(node, 'v2-slope-svg').length, 0, 'no SVG when empty');
});

// ===========================================================================
// The Tournament VIEW — rounds build, deep-link ensure-load, no-flash.
// ===========================================================================

test('buildRounds: a non-seed generation is a matchup vs its lineage parent', () => {
  const scalars = new Map([['v0', 0.80], ['v1', 0.55], ['v2', 0.70]]);
  const rounds = buildRounds(epochDefFixture(), scalars, null);
  // The seed (v0) is the first champion, not a matchup → 2 settled rounds.
  assertEqual(rounds.length, 2);
  assertEqual(rounds[0].champion.id, 'v0');
  assertEqual(rounds[0].challenger.id, 'v1');
  assertEqual(rounds[0].champion.scalar, 0.80, 'champion scalar from the parent lineage');
  assertEqual(rounds[0].challenger.scalar, 0.55, 'challenger scalar from the child lineage');
  assertEqual(rounds[0].decision, 'promoted');
  assertEqual(rounds[1].decision, 'rejected');
  assertEqual(rounds[1].firedRule, 'scalar_margin', 'fired rule inferred from the rejection reason');
});

test('buildRounds: a rejected child with no lineage scalar derives challenger = champion + Δ (never NaN)', () => {
  const scalars = new Map([['v0', 0.80], ['v1', 0.55]]); // v2 absent
  const rounds = buildRounds(epochDefFixture(), scalars, null);
  const r1 = rounds[1];
  assert(r1.challenger.scalar != null && isFinite(r1.challenger.scalar),
    'derived a finite challenger scalar');
  assertEqual(Math.round(r1.challenger.scalar * 100) / 100, 0.70, 'champion 0.55 + Δ 0.15 = 0.70');
});

test('buildRounds: a live active-tournament appends a running matchup', () => {
  const scalars = new Map([['v0', 0.80], ['v1', 0.55], ['v2', 0.70]]);
  const at = {
    phase: 'running', parent_generation_id: 'v1', child_generation_id: 'v3',
    partial_champion_agg: { scalar: 0.55 }, partial_challenger_agg: { scalar: 0.48 },
  };
  const rounds = buildRounds(epochDefFixture(), scalars, at);
  assertEqual(rounds.length, 3, '2 settled + 1 live');
  const live = rounds[2];
  assertEqual(live.decision, 'running');
  assertEqual(live.challenger.id, 'v3');
  assertEqual(live.champion.scalar, 0.55);
  assertEqual(live.challenger.scalar, 0.48, 'live challenger scalar from the partial aggregate');
});

test('view: renders the slopegraph from state (contract + lineage + live)', async () => {
  resetTournamentView();
  state.epochDef = epochDefFixture();
  state.lineage = lineageFixture();
  state.activeTournament = null;
  installFetch({ '/api/epoch': epochDefFixture(), '/api/active-tournament': REJECT });
  const host = freshHost();
  renderTournament(host, { view: 'tournament', params: {} });
  await settle();
  const sg = firstWithClass(host, 'v2-slope');
  assert(sg != null, 'the tournament view renders the slopegraph');
  assertEqual(sg.getAttribute('data-mode'), 'plot');
  // Promote-joins + reject-falls invariants are visible in the view.
  assert(firstWithClass(host, 'v2-slope-node-joins'), 'a promoted challenger joins');
  assert(firstWithClass(host, 'v2-slope-node-falls'), 'a rejected challenger falls away');
});

test('view: deep-link ensure-load — an empty contract fetches /api/epoch and re-renders', async () => {
  resetTournamentView();
  state.epochDef = null;          // cold deep-link: no contract folded yet
  state.lineage = lineageFixture();
  state.activeTournament = null;
  let epochFetches = 0;
  globalThis.fetch = (path) => {
    const p = String(path);
    if (p.startsWith('/api/epoch')) {
      epochFetches += 1;
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(epochDefFixture()) });
    }
    return Promise.reject(new Error(`${p} -> HTTP 404`));
  };
  const host = freshHost();
  renderTournament(host, { view: 'tournament', params: {} });
  await settle();
  assert(epochFetches >= 1, 'a cold deep-link ensure-loads the epoch contract');
  // After the fetch lands, the slopegraph renders from the loaded contract.
  const sg = firstWithClass(host, 'v2-slope');
  assert(sg != null && sg.getAttribute('data-mode') === 'plot',
    'the view re-rendered the slopegraph once the contract arrived');
});

test('view: digest-gated — a re-render with unchanged data does not rebuild (no flash)', async () => {
  resetTournamentView();
  state.epochDef = epochDefFixture();
  state.lineage = lineageFixture();
  state.activeTournament = null;
  installFetch({ '/api/epoch': epochDefFixture(), '/api/active-tournament': REJECT });
  const host = freshHost();
  renderTournament(host, { view: 'tournament', params: {} });
  await settle();
  const firstWrap = firstWithClass(host, 'v2-tournament');
  assert(firstWrap != null, 'first render built the wrapper');
  // A second render with identical state must keep the SAME wrapper node
  // (swapIfChanged short-circuits) — node identity proves no rebuild/flash.
  renderTournament(host, { view: 'tournament', params: {} });
  await settle();
  const secondWrap = firstWithClass(host, 'v2-tournament');
  assert(secondWrap === firstWrap, 'an unchanged re-render reuses the node (no flash)');
  // And the render spine never reached for innerHTML.
  assertEqual(host.innerHTMLWriteCount(), 0, 'no innerHTML writes (no-flash discipline)');
});

await run();

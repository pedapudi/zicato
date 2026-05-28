// test/l1_redesign.test.mjs — Task #191 L1 redesign coverage.
//
// Pins four related changes on the L1 epoch view:
//   (A) Spine renders rejected challengers as branches off their parent
//       — and treats v0 (baseline seed, no outcome, no parent) as the
//       promoted root of the spine, not as a rejected pile entry.
//   (B) Recent experiments render as full-width cards stacked
//       vertically — verdict pill + key deltas in the header row, the
//       hypothesis core idea as the prominent body line, "why" and
//       "predicted" as labelled inline rows.
//   (C) The Journal Preview section is gone from L1 — its slot is no
//       longer in index.html and the renderer no longer paints it.
//   (D) The Analysis Report section is wired in at the bottom of L1 —
//       it fetches /api/epoch/{id}/analysis lazily, embeds the
//       paper-styled HTML fragment, and offers an "open full report"
//       link when the standalone HTML file is available.

import { installDom, test, run, assert } from './harness.mjs';

installDom();

const router = await import('../js/views/phase0_router.js');
void router;

const { state } = await import('../js/core/state.js');
const epoch = await import('../js/views/phase0_epoch.js');
const spine = await import('../js/components/spine.js');

// One-time DOM helper — strip a stale node with the same id before
// installing a fresh one. Mirrors loading_states.test.mjs.
function installNode(id, tag = 'div') {
  let stale = document.getElementById(id);
  while (stale) {
    if (stale.parentNode) stale.parentNode.removeChild(stale);
    stale = document.getElementById(id);
  }
  const node = document.createElement(tag);
  node.id = id;
  document.body.appendChild(node);
  return node;
}

function installEpochSlots() {
  installNode('phase0-epoch-goal');
  installNode('phase0-epoch-contract-diff');
  installNode('phase0-epoch-spine');
  installNode('phase0-epoch-heatmap-entries');
  installNode('phase0-epoch-heatmap-judges');
  installNode('phase0-epoch-experiments');
  installNode('phase0-epoch-analysis');
}

function resetEpochCaches() {
  epoch.resetContractDiffCache();
  epoch.resetPerJudgeTrendCache();
  epoch.resetPerEntryTrendCache();
  epoch.resetAnalysisCache();
}

// Walk the descendants of a node and return every element that has
// the given class in its classList. The harness's querySelectorAll
// only supports an exact attribute match, so [class*=foo] is not a
// usable selector inside the harness — we do the walk by hand.
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

function mockFetch(handler) {
  const original = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const body = handler(url);
    return {
      ok: true,
      status: 200,
      headers: new Map(),
      json: async () => body,
      text: async () => JSON.stringify(body),
    };
  };
  return () => { globalThis.fetch = original; };
}

// The lineage from the dogfood workspace: v0 baseline → v1 (promoted)
// → v3 (promoted), with v2 rejected against v1 and v4..v7 rejected
// against v3. v8 is the live (unscored) challenger to v3. This is the
// fixture every spine test below pins.
const TOUR_EXPERIMENTS = [
  {
    generation_id: 'v0',
    parent_generation_id: '',
    hypothesis: { core_idea: 'baseline seed' },
    outcome: null,
  },
  {
    generation_id: 'v1',
    parent_generation_id: 'v0',
    hypothesis: { core_idea: 'first promotion' },
    outcome: { tournament_decision: 'promoted', scalar_score_delta: -1.0 },
  },
  {
    generation_id: 'v2',
    parent_generation_id: 'v1',
    hypothesis: { core_idea: 'rejected challenger of v1' },
    outcome: { tournament_decision: 'rejected', scalar_score_delta: 2.0 },
  },
  {
    generation_id: 'v3',
    parent_generation_id: 'v1',
    hypothesis: { core_idea: 'second promotion' },
    outcome: { tournament_decision: 'promoted', scalar_score_delta: -3.0 },
  },
  {
    generation_id: 'v4',
    parent_generation_id: 'v3',
    hypothesis: { core_idea: 'rejected challenger v4' },
    outcome: { tournament_decision: 'rejected', scalar_score_delta: 4.0 },
  },
  {
    generation_id: 'v5',
    parent_generation_id: 'v3',
    hypothesis: { core_idea: 'rejected challenger v5' },
    outcome: { tournament_decision: 'rejected', scalar_score_delta: 5.0 },
  },
  {
    generation_id: 'v6',
    parent_generation_id: 'v3',
    hypothesis: { core_idea: 'rejected challenger v6' },
    outcome: { tournament_decision: 'rejected', scalar_score_delta: 6.0 },
  },
  {
    generation_id: 'v7',
    parent_generation_id: 'v3',
    hypothesis: { core_idea: 'rejected challenger v7' },
    outcome: { tournament_decision: 'rejected', scalar_score_delta: 7.0 },
  },
];

// ===================================================================
// (A) Spine — rejected challengers branch off their parent
// ===================================================================

test('renderSpine groups rejected branches under their parent column', () => {
  const node = spine.renderSpine({
    nodes: [
      { id: 'v0', scalar: 0.6, promoted: true, parent_id: null },
      { id: 'v1', scalar: 0.5, promoted: true, parent_id: 'v0' },
      { id: 'v2', scalar: 0.7, promoted: false, parent_id: 'v1' },
      { id: 'v3', scalar: 0.4, promoted: true, parent_id: 'v1' },
      { id: 'v4', scalar: 0.8, promoted: false, parent_id: 'v3' },
      { id: 'v5', scalar: 0.9, promoted: false, parent_id: 'v3' },
    ],
  });
  // The spine row carries one column per promoted node; each column
  // includes its rejected-branch chips ABOVE the spine node, attached
  // by a vertical tee.
  const cols = descendantsWithClass(node, 'spine-col');
  assert(cols.length === 3,
    `must paint one spine-col per promoted node (v0, v1, v3); got ${cols.length}`);

  // Identify each spine column by its OWN spine-node-label (the id
  // painted directly on the promoted/live node), then assert each
  // column's branch row carries the expected rejected challengers.
  function columnSpineId(col) {
    const labels = descendantsWithClass(col, 'spine-node-label');
    return labels.length ? labels[0].textContent : '';
  }
  function columnBranchIds(col) {
    return descendantsWithClass(col, 'spine-branch-id').map((n) => n.textContent);
  }
  const byId = {};
  for (const col of cols) byId[columnSpineId(col)] = col;
  assert('v0' in byId && 'v1' in byId && 'v3' in byId,
    `every promoted column must be keyed by its own id; got: ${Object.keys(byId).join(',')}`);

  const v0Branches = columnBranchIds(byId.v0);
  const v1Branches = columnBranchIds(byId.v1);
  const v3Branches = columnBranchIds(byId.v3);
  assert(v0Branches.length === 0,
    `v0 column must carry no rejected branches; got: ${v0Branches.join(',')}`);
  assert(v1Branches.length === 1 && v1Branches[0] === 'v2',
    `v1 column must carry exactly v2 as a branch; got: ${v1Branches.join(',')}`);
  // v4 + v5 both challenged v3; the order is id-natural.
  assert(v3Branches.length === 2 && v3Branches.includes('v4') && v3Branches.includes('v5'),
    `v3 column must carry v4+v5 as branches; got: ${v3Branches.join(',')}`);
});

test('renderSpine paints a vertical tee for each parent column with branches', () => {
  const node = spine.renderSpine({
    nodes: [
      { id: 'v0', scalar: null, promoted: true, parent_id: null },
      { id: 'v1', scalar: null, promoted: true, parent_id: 'v0' },
      { id: 'v2', scalar: null, promoted: false, parent_id: 'v1' },
    ],
  });
  // The connector tee is a thin visual line that grounds the branch
  // chip onto the spine node. Exactly one column should carry it (the
  // v1 column, since v2 challenged v1).
  const tees = descendantsWithClass(node, 'spine-branch-tee');
  assert(tees.length === 1,
    `exactly one column must paint the branch tee; got ${tees.length}`);
});

test('renderSpine falls back to footer footnote when a rejected node has no parent', () => {
  // No parent_id on the rejected node — the renderer has nothing to
  // anchor it to, so it falls back to the legacy "rejected (no parent)"
  // footnote at the bottom of the spine.
  const node = spine.renderSpine({
    nodes: [
      { id: 'v0', scalar: null, promoted: true, parent_id: null },
      { id: 'v1', scalar: null, promoted: true, parent_id: 'v0' },
      { id: 'orphan', scalar: null, promoted: false, parent_id: null },
    ],
  });
  const text = node.textContent;
  assert(text.includes('rejected (no parent)'),
    `orphan rejected must surface in the footer; got: ${text}`);
  assert(text.includes('orphan'),
    'the orphan rejected id must still render');
});

test('renderPhase0Epoch spine treats v0 (no parent, no outcome) as promoted root', () => {
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = null;
  state.epochDef = {
    epoch_id: 'e0', goal: 'g', experiments: TOUR_EXPERIMENTS,
  };
  const restoreFetch = mockFetch(() => ({}));
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const spineNode = document.getElementById('phase0-epoch-spine');
    const text = spineNode.textContent;
    // v0 is the LEFTMOST node on the spine row. It must render in a
    // spine-col (not a rejected chip / branch), and the spine column
    // count must equal 3 promoted nodes (v0 + v1 + v3).
    const cols = descendantsWithClass(spineNode, 'spine-col');
    assert(cols.length === 3,
      `v0+v1+v3 must each get a spine column; got ${cols.length}`);
    assert(text.includes('v0'), `v0 must render in the spine; got: ${text.slice(0, 200)}`);
    // v0 must NOT appear in the rejected footer — there is no
    // "rejected (no parent)" footer line at all for this fixture
    // because every rejected node has a parent in the spine.
    assert(!text.includes('rejected (no parent)'),
      `no orphan-rejected footer expected for the tour fixture; got: ${text.slice(0, 400)}`);
    // v4-v7 should all surface as branches attached to v3 (text only
    // pin — the column-grouping assertion is in the unit test above).
    assert(text.includes('v4') && text.includes('v5')
      && text.includes('v6') && text.includes('v7'),
      `all rejected challengers of v3 must render; got: ${text.slice(0, 400)}`);
  } finally {
    restoreFetch();
  }
});

// ===================================================================
// (B) Recent experiments — full-width cards
// ===================================================================

test('renderPhase0Epoch paints one full-width card per recent experiment', () => {
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = null;
  state.epochDef = {
    epoch_id: 'e0', goal: 'g',
    experiments: [
      {
        generation_id: 'v7',
        parent_generation_id: 'v3',
        hypothesis: {
          core_idea: "Tighten the researcher's topicality constraints.",
          why: 'Pattern indicates off_topic is a primary risk.',
          expected_drift_movements: [
            { kind: 'off_topic', direction: 'decrease', magnitude: 'medium' },
          ],
          expected_pass_rate_delta: '+0.10 to +0.20',
        },
        outcome: {
          tournament_decision: 'rejected',
          scalar_score_delta: -15.643,
          pass_rate_delta: 0.0,
          drift_loss_delta: -16.357,
          rejection_reason: 'challenger regressed on pass-rate',
        },
      },
    ],
  };
  const restoreFetch = mockFetch(() => ({}));
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const expNode = document.getElementById('phase0-epoch-experiments');
    const cards = descendantsWithClass(expNode, 'phase0-exp-card');
    assert(cards.length === 1,
      `exactly one experiment card must render; got ${cards.length}`);
    const text = expNode.textContent;
    // Header carries gen id, verdict pill, and the three metric tiles
    // with labels Δscalar / Δpass / Δdrift.
    assert(text.includes('v7'), 'card header must include gen id');
    assert(text.includes('REJECTED'),
      'verdict pill label must render as uppercase REJECTED');
    assert(text.includes('Δscalar') && text.includes('Δpass')
      && text.includes('Δdrift'),
      'metric tiles must render in the header');
    assert(text.includes('-15.643'),
      `Δscalar value must render with sign; got: ${text.slice(0, 400)}`);
    // Body carries the core idea as the prominent first line, plus
    // labelled "why" + "predicted" rows.
    assert(text.includes('topicality constraints'),
      'core_idea must render in the body');
    assert(text.includes('why'),
      'why lead label must render');
    assert(text.includes('predicted'),
      'predicted lead label must render');
    assert(text.includes('off_topic decrease'),
      'predicted drift movement must render in the predicted line');
    // Rejected cards carry an italic "rejected because" line.
    assert(text.includes('rejected because'),
      `rejected cards must show the rejection reason lead; got: ${text.slice(0, 400)}`);
    assert(text.includes('challenger regressed'),
      'rejection reason body must render');
  } finally {
    restoreFetch();
  }
});

test('renderPhase0Epoch experiment cards use distinct decision classes', () => {
  // The card border-left rail colour-codes the verdict, so each
  // experiment must carry a decision-specific class on the card root.
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = null;
  state.epochDef = {
    epoch_id: 'e0', goal: 'g',
    experiments: [
      {
        generation_id: 'v1',
        parent_generation_id: 'v0',
        hypothesis: { core_idea: 'first promoted' },
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -1 },
      },
      {
        generation_id: 'v2',
        parent_generation_id: 'v1',
        hypothesis: { core_idea: 'rejected' },
        outcome: { tournament_decision: 'rejected', scalar_score_delta: 1 },
      },
    ],
  };
  const restoreFetch = mockFetch(() => ({}));
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const expNode = document.getElementById('phase0-epoch-experiments');
    // Find the two cards by their decision class.
    const promotedCards = descendantsWithClass(expNode, 'phase0-exp-card-promoted');
    const rejectedCards = descendantsWithClass(expNode, 'phase0-exp-card-rejected');
    assert(promotedCards.length === 1,
      `one promoted card expected; got ${promotedCards.length}`);
    assert(rejectedCards.length === 1,
      `one rejected card expected; got ${rejectedCards.length}`);
  } finally {
    restoreFetch();
  }
});

// ===================================================================
// (C) Journal Preview removed
// ===================================================================

test('renderPhase0Epoch no longer paints any Journal preview slot', () => {
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = null;
  state.epochDef = {
    epoch_id: 'e0', goal: 'g', journal: 'old journal contents...',
    experiments: TOUR_EXPERIMENTS,
  };
  const restoreFetch = mockFetch(() => ({}));
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    // The slot itself must NOT have a paint — we never installed
    // #phase0-epoch-journal, and the renderer must not crash trying
    // to find it. Also: nowhere in the L1 painted DOM should the text
    // "Journal preview" appear.
    const body = document.body.textContent;
    assert(!body.includes('Journal preview'),
      `Journal preview heading must NOT render anywhere on L1; got body slice: ${body.slice(0, 200)}`);
  } finally {
    restoreFetch();
  }
});

test('index.html no longer carries the #phase0-epoch-journal slot', () => {
  // The slot was removed from the static page so a stale renderer that
  // tries to paint it falls through gracefully (the renderer no longer
  // calls $('phase0-epoch-journal') either).
  const stale = document.getElementById('phase0-epoch-journal');
  assert(stale == null,
    'phase0-epoch-journal slot must NOT exist in this DOM');
});

// ===================================================================
// (D) Analysis Report section
// ===================================================================

test('renderPhase0Epoch fetches /api/epoch/{id}/analysis and embeds the inline HTML', async () => {
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = null;
  state.epochDef = {
    epoch_id: 'e0', goal: 'g', experiments: TOUR_EXPERIMENTS,
  };
  const inlineFragment = (
    '<article class="paper paper-card" data-epoch="e0">'
    + '<div class="paper-article"><h1>Epoch analysis</h1>'
    + '<p>v3 reduced the off_topic drift by 40%.</p>'
    + '<svg width="100" height="50" data-figure="hypothesis"></svg>'
    + '</div></article>'
  );
  let analysisCalls = 0;
  const restoreFetch = mockFetch((url) => {
    if (url.includes('/analysis')) {
      analysisCalls += 1;
      return {
        epoch_id: 'e0',
        analysis_md: '# Epoch analysis\n\nv3 reduced the off_topic drift.',
        analysis_html_inline: inlineFragment,
        analysis_html_available: true,
      };
    }
    return {};
  });
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const analysisNode = document.getElementById('phase0-epoch-analysis');
    const text = analysisNode.textContent;
    assert(text.includes('Analysis report'),
      `card title must render; got: ${text.slice(0, 200)}`);
    // The harness's Element shim does NOT parse innerHTML — it tracks
    // the write count instead — so we verify the embed happened via
    // innerHTMLWriteCount() rather than textContent. (In the real
    // browser the fragment's headings and figures render directly.)
    const hostNodes = descendantsWithClass(analysisNode, 'phase0-analysis-host');
    assert(hostNodes.length === 1,
      `analysis host wrapper must render exactly once; got ${hostNodes.length}`);
    assert(hostNodes[0].innerHTMLWriteCount() >= 1,
      'the inline HTML fragment must be injected via innerHTML');
    // "Open full report" link must surface when the standalone HTML
    // file is available on disk.
    assert(text.includes('Open full report'),
      `full-report link must render; got: ${text.slice(0, 400)}`);
    assert(analysisCalls >= 1,
      'analysis endpoint must be called at least once');
  } finally {
    restoreFetch();
  }
});

test('renderPhase0Epoch shows "not yet generated" when analysis is absent', async () => {
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = null;
  state.epochDef = {
    epoch_id: 'e0', goal: 'g', experiments: TOUR_EXPERIMENTS,
  };
  const restoreFetch = mockFetch((url) => {
    if (url.includes('/analysis')) {
      return {
        epoch_id: 'e0',
        analysis_md: '',
        analysis_html_inline: '',
        analysis_html_available: false,
      };
    }
    return {};
  });
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const analysisNode = document.getElementById('phase0-epoch-analysis');
    const text = analysisNode.textContent;
    assert(text.includes('Analysis report'),
      'card title must still render in the empty state');
    assert(text.includes('not yet generated'),
      `empty-state copy must render; got: ${text.slice(0, 400)}`);
    // No full-report link when the standalone file is absent.
    assert(!text.includes('Open full report'),
      `Open-full-report link must NOT render when html_available is false; got: ${text.slice(0, 400)}`);
  } finally {
    restoreFetch();
  }
});

await run();

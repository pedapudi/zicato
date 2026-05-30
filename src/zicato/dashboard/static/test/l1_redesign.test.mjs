// test/l1_redesign.test.mjs — L1 epoch-view redesign coverage.
//
// The L1 view is now decision-/health-centric. This suite pins:
//   (A) LINEAGE RIBBON — the flat generation spine is replaced by the
//       shared lineageRibbon component (zoom 'generations'). v0 (the
//       parentless, outcome-less baseline seed) anchors the promoted
//       spine; rejected challengers branch off their parent; the live
//       node surfaces; clicking a node drills to L2.
//   (B) LOOP-HEALTH BANNER — fetched from /api/health-report and rendered
//       at the top of L1; degrades to "not yet evaluated" on a null
//       report.
//   (C) EPOCH STORY HEADER — the compact goal + rollup line.
//   (D) TABBED HEATMAPS — the per-entry and per-judge heatmaps fold into
//       ONE card with an entries/judges tab toggle.
//   (E) RECENT EXPERIMENTS — full-width cards; verdict iconography speaks
//       through the shared verdictGlyph.
//   (F) ANALYSIS REPORT — fetched lazily, embeds the paper-styled HTML
//       fragment, offers an "open full report" link.

import { installDom, test, run, assert, makeEvent } from './harness.mjs';

installDom();

const router = await import('../js/views/phase0_router.js');
void router;

const { state } = await import('../js/core/state.js');
const epoch = await import('../js/views/phase0_epoch.js');

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
  epoch.resetHealthReportCache();
  epoch.resetHeatmapTab();
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

// Walk descendants returning every element carrying the given attribute
// (the harness exposes hasAttribute / getAttribute per element).
function descendantsWithAttr(node, attr, value) {
  const out = [];
  const walk = (n) => {
    if (!n || n.nodeType !== 1) return;
    if (typeof n.hasAttribute === 'function' && n.hasAttribute(attr)) {
      if (value === undefined || n.getAttribute(attr) === value) out.push(n);
    }
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
// against v3. This is the fixture every lineage test below pins.
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
    outcome: { tournament_decision: 'promoted', scalar_score: 0.5, scalar_score_delta: -1.0 },
  },
  {
    generation_id: 'v2',
    parent_generation_id: 'v1',
    hypothesis: { core_idea: 'rejected challenger of v1' },
    outcome: { tournament_decision: 'rejected', scalar_score: 0.7, scalar_score_delta: 2.0 },
  },
  {
    generation_id: 'v3',
    parent_generation_id: 'v1',
    hypothesis: { core_idea: 'second promotion' },
    outcome: { tournament_decision: 'promoted', scalar_score: 0.3, scalar_score_delta: -3.0 },
  },
  {
    generation_id: 'v4',
    parent_generation_id: 'v3',
    hypothesis: { core_idea: 'rejected challenger v4' },
    outcome: { tournament_decision: 'rejected', scalar_score: 0.8, scalar_score_delta: 4.0 },
  },
  {
    generation_id: 'v5',
    parent_generation_id: 'v3',
    hypothesis: { core_idea: 'rejected challenger v5' },
    outcome: { tournament_decision: 'rejected', scalar_score: 0.9, scalar_score_delta: 5.0 },
  },
  {
    generation_id: 'v6',
    parent_generation_id: 'v3',
    hypothesis: { core_idea: 'rejected challenger v6' },
    outcome: { tournament_decision: 'rejected', scalar_score: 1.0, scalar_score_delta: 6.0 },
  },
  {
    generation_id: 'v7',
    parent_generation_id: 'v3',
    hypothesis: { core_idea: 'rejected challenger v7' },
    outcome: { tournament_decision: 'rejected', scalar_score: 1.1, scalar_score_delta: 7.0 },
  },
];

// ===================================================================
// (A) Lineage ribbon replaces the spine
// ===================================================================

test('renderPhase0Epoch paints a lineage ribbon (not a spine) in the spine slot', () => {
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = null;
  state.epochDef = { epoch_id: 'e0', goal: 'g', experiments: TOUR_EXPERIMENTS };
  const restoreFetch = mockFetch(() => ({}));
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const slot = document.getElementById('phase0-epoch-spine');
    // The ribbon component renders a .ribbon root; the legacy spine
    // (.spine-col) must NOT appear in the view any more.
    const ribbons = descendantsWithClass(slot, 'ribbon');
    assert(ribbons.length >= 1,
      `lineage ribbon must render in the spine slot; found ${ribbons.length}`);
    const spineCols = descendantsWithClass(slot, 'spine-col');
    assert(spineCols.length === 0,
      `the legacy spine must be gone from L1; found ${spineCols.length} spine-col`);
    // Every generation must surface as a ribbon node.
    for (const gid of ['v0', 'v1', 'v2', 'v3', 'v4', 'v5', 'v6', 'v7']) {
      const matches = descendantsWithAttr(slot, 'data-node-id', gid);
      assert(matches.length >= 1, `ribbon must carry a node for ${gid}`);
    }
  } finally {
    restoreFetch();
  }
});

test('lineage ribbon marks v0/v1/v3 promoted and v2/v4 rejected', () => {
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = null;
  state.epochDef = { epoch_id: 'e0', goal: 'g', experiments: TOUR_EXPERIMENTS };
  const restoreFetch = mockFetch(() => ({}));
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const slot = document.getElementById('phase0-epoch-spine');
    const verdictOf = (gid) => {
      const node = descendantsWithAttr(slot, 'data-node-id', gid)[0];
      return node ? node.getAttribute('data-verdict') : null;
    };
    // v0 (baseline seed) anchors the promoted spine.
    assert(verdictOf('v0') === 'promoted',
      `v0 (baseline) must read as promoted; got ${verdictOf('v0')}`);
    assert(verdictOf('v1') === 'promoted', 'v1 must read promoted');
    assert(verdictOf('v3') === 'promoted', 'v3 must read promoted');
    assert(verdictOf('v2') === 'rejected', 'v2 must read rejected');
    assert(verdictOf('v4') === 'rejected', 'v4 must read rejected');
  } finally {
    restoreFetch();
  }
});

test('lineage ribbon surfaces the live generation as a live node', () => {
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = { epoch_id: 'e0', generation_id: 'v8' };
  state.epochDef = { epoch_id: 'e0', goal: 'g', experiments: TOUR_EXPERIMENTS };
  const restoreFetch = mockFetch(() => ({}));
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const slot = document.getElementById('phase0-epoch-spine');
    const liveNodes = descendantsWithClass(slot, 'ribbon-node-live');
    assert(liveNodes.length === 1,
      `exactly one live ribbon node expected; got ${liveNodes.length}`);
    const v8 = descendantsWithAttr(slot, 'data-node-id', 'v8')[0];
    assert(v8 != null, 'the live generation v8 must render a node');
  } finally {
    state.heartbeat = null;
    restoreFetch();
  }
});

test('clicking a ribbon node navigates to the L2 generation route', () => {
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = null;
  state.epochDef = { epoch_id: 'e0', goal: 'g', experiments: TOUR_EXPERIMENTS };
  window.location.hash = '#/epoch/e0';
  const restoreFetch = mockFetch(() => ({}));
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const slot = document.getElementById('phase0-epoch-spine');
    const v3 = descendantsWithAttr(slot, 'data-node-id', 'v3')[0];
    assert(v3 != null, 'v3 node must exist to click');
    v3.dispatchEvent(makeEvent('click'));
    assert(window.location.hash === '#/gen/e0/v3',
      `clicking v3 must route to L2; got hash ${window.location.hash}`);
  } finally {
    restoreFetch();
  }
});

// ===================================================================
// (B) Loop-health banner
// ===================================================================

test('renderPhase0Epoch renders the loop-health banner from /api/health-report', async () => {
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = null;
  state.epochDef = { epoch_id: 'e0', goal: 'g', experiments: TOUR_EXPERIMENTS };
  let healthCalls = 0;
  const restoreFetch = mockFetch((url) => {
    if (url.includes('/api/health-report')) {
      healthCalls += 1;
      return {
        epoch_id: 'e0',
        healthy: false,
        findings: [
          {
            code: 'flat_drift_signal',
            severity: 'warning',
            summary: 'drift loss is flat across the last generations',
            detail: { generations: 4, range: 0.0 },
          },
        ],
      };
    }
    return {};
  });
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const goalSlot = document.getElementById('phase0-epoch-goal');
    const banners = descendantsWithClass(goalSlot, 'health-banner');
    assert(banners.length >= 1,
      `the health banner must render in the L1 head; got ${banners.length}`);
    const text = goalSlot.textContent;
    assert(text.includes('drift loss is flat'),
      `the top finding summary must render; got: ${text.slice(0, 300)}`);
    // The banner tone tracks the warning severity.
    const warn = descendantsWithClass(goalSlot, 'health-banner-warn');
    assert(warn.length >= 1, 'a warning report must paint the warn-tone banner');
    assert(healthCalls >= 1, 'the health-report endpoint must be called');
  } finally {
    restoreFetch();
  }
});

test('loop-health banner degrades to "not yet evaluated" before the report lands', () => {
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = null;
  state.epochDef = { epoch_id: 'e0', goal: 'g', experiments: TOUR_EXPERIMENTS };
  // Never resolve the health fetch — the synchronous first paint must
  // still render a muted banner rather than crashing or blanking.
  const restoreFetch = mockFetch(() => ({}));
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const goalSlot = document.getElementById('phase0-epoch-goal');
    const muted = descendantsWithClass(goalSlot, 'health-banner-muted');
    assert(muted.length >= 1,
      'a not-yet-loaded health report must paint the muted banner');
    assert(goalSlot.textContent.includes('not yet evaluated'),
      'muted banner copy must render');
  } finally {
    restoreFetch();
  }
});

// ===================================================================
// (C) Epoch story header
// ===================================================================

test('epoch story header surfaces the goal, generation count, and champion', () => {
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = null;
  state.epochDef = {
    epoch_id: 'e0',
    goal: 'Reduce off-topic drift on the research board.',
    experiments: TOUR_EXPERIMENTS,
  };
  const restoreFetch = mockFetch(() => ({}));
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const goalSlot = document.getElementById('phase0-epoch-goal');
    const stories = descendantsWithClass(goalSlot, 'epoch-story');
    assert(stories.length >= 1, 'the epoch story header must render');
    const text = goalSlot.textContent;
    assert(text.includes('Reduce off-topic drift'),
      `the goal text must render in the story header; got: ${text.slice(0, 300)}`);
    assert(text.includes('generations'), 'a generations chip must render');
    // v3 is the best-scalar promoted node — the current champion.
    assert(text.includes('v3'),
      `the champion (v3, best scalar) must surface; got: ${text.slice(0, 400)}`);
  } finally {
    restoreFetch();
  }
});

test('epoch story header shows the (no goal recorded) hint when goal is empty', () => {
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = null;
  state.epochDef = { epoch_id: 'e0', goal: '', experiments: TOUR_EXPERIMENTS };
  const restoreFetch = mockFetch(() => ({}));
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const goalSlot = document.getElementById('phase0-epoch-goal');
    assert(goalSlot.textContent.includes('no goal recorded'),
      'the empty-goal hint must render');
  } finally {
    restoreFetch();
  }
});

// ===================================================================
// (D) Tabbed heatmaps — entries / judges in one card
// ===================================================================

test('the two heatmaps fold into one card with an entries/judges tab toggle', async () => {
  resetEpochCaches();
  installEpochSlots();
  state.heartbeat = null;
  state.epochDef = {
    epoch_id: 'e0', goal: 'g',
    experiments: [
      { generation_id: 'v0', parent_generation_id: '', hypothesis: {}, outcome: null },
      {
        generation_id: 'v1', parent_generation_id: 'v0', hypothesis: {},
        outcome: { tournament_decision: 'promoted', scalar_score: 0.5 },
      },
    ],
  };
  const restoreFetch = mockFetch((url) => {
    if (url.includes('/per-judge-trend')) {
      return {
        epoch_id: 'e0',
        generations: ['v0', 'v1'],
        judges: [
          { judge_name: 'topicality', by_generation: { v0: 0.4, v1: 0.2 } },
        ],
      };
    }
    if (url.includes('/per-entry')) {
      return { entries: [{ entry_id: 'q1', drift_loss: 0.3 }] };
    }
    return {};
  });
  try {
    // First render + let trend fetches resolve, then repaint.
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    epoch.renderPhase0Epoch({ epochId: 'e0' });

    const slot = document.getElementById('phase0-epoch-heatmap-entries');
    // Exactly one heatmap card hosts both views via the tab strip.
    const tabs = descendantsWithAttr(slot, 'data-heatmap-tab');
    assert(tabs.length === 2,
      `exactly two heatmap tabs (entries, judges) expected; got ${tabs.length}`);
    const keys = tabs.map((t) => t.getAttribute('data-heatmap-tab')).sort();
    assert(keys[0] === 'entries' && keys[1] === 'judges',
      `tabs must be entries + judges; got ${keys.join(',')}`);

    // Default tab is "entries" — the per-entry row label q1 shows.
    assert(slot.textContent.includes('q1'),
      `entries tab must show the per-entry heatmap by default; got: ${slot.textContent.slice(0, 400)}`);

    // Activate the judges tab → the panel swaps to the per-judge view.
    const judgesTab = tabs.find((t) => t.getAttribute('data-heatmap-tab') === 'judges');
    judgesTab.dispatchEvent(makeEvent('click'));
    // The click repaints via the renderPhase0Epoch repaint callback only
    // when one is supplied; here we re-render explicitly to read the
    // post-toggle DOM (the toggle flips module state).
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const slotAfter = document.getElementById('phase0-epoch-heatmap-entries');
    assert(slotAfter.textContent.includes('topicality'),
      `judges tab must show the per-judge heatmap after toggle; got: ${slotAfter.textContent.slice(0, 400)}`);

    // The now-redundant judges slot must be cleared (folded into the card).
    const judgesSlot = document.getElementById('phase0-epoch-heatmap-judges');
    const judgeCards = descendantsWithClass(judgesSlot, 'card');
    assert(judgeCards.length === 0,
      `the standalone judges slot must be cleared; got ${judgeCards.length} cards`);
  } finally {
    restoreFetch();
  }
});

// ===================================================================
// (E) Recent experiments — full-width cards + verdict glyph
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
    assert(text.includes('v7'), 'card header must include gen id');
    // Verdict iconography speaks through the shared verdictGlyph — the
    // header carries a .vglyph with the "rejected" label.
    const glyphs = descendantsWithClass(expNode, 'vglyph');
    assert(glyphs.length >= 1, 'the verdict glyph must render in the card header');
    assert(text.includes('rejected'),
      'verdict glyph label must render the rejected wording');
    assert(text.includes('Δscalar') && text.includes('Δpass')
      && text.includes('Δdrift'),
      'metric tiles must render in the header');
    assert(text.includes('-15.643'),
      `Δscalar value must render with sign; got: ${text.slice(0, 400)}`);
    assert(text.includes('topicality constraints'),
      'core_idea must render in the body');
    assert(text.includes('why'), 'why lead label must render');
    assert(text.includes('predicted'), 'predicted lead label must render');
    assert(text.includes('off_topic decrease'),
      'predicted drift movement must render in the predicted line');
    assert(text.includes('rejected because'),
      `rejected cards must show the rejection reason lead; got: ${text.slice(0, 400)}`);
    assert(text.includes('challenger regressed'),
      'rejection reason body must render');
  } finally {
    restoreFetch();
  }
});

test('renderPhase0Epoch experiment cards use distinct decision classes', () => {
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
// (C') Journal Preview stays removed
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
    const body = document.body.textContent;
    assert(!body.includes('Journal preview'),
      `Journal preview heading must NOT render anywhere on L1; got body slice: ${body.slice(0, 200)}`);
  } finally {
    restoreFetch();
  }
});

test('index.html no longer carries the #phase0-epoch-journal slot', () => {
  const stale = document.getElementById('phase0-epoch-journal');
  assert(stale == null,
    'phase0-epoch-journal slot must NOT exist in this DOM');
});

// ===================================================================
// (F) Analysis Report section
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
    const hostNodes = descendantsWithClass(analysisNode, 'phase0-analysis-host');
    assert(hostNodes.length === 1,
      `analysis host wrapper must render exactly once; got ${hostNodes.length}`);
    assert(hostNodes[0].innerHTMLWriteCount() >= 1,
      'the inline HTML fragment must be injected via innerHTML');
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
    assert(!text.includes('Open full report'),
      `Open-full-report link must NOT render when html_available is false; got: ${text.slice(0, 400)}`);
  } finally {
    restoreFetch();
  }
});

await run();

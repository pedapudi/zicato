// test/phase1.test.mjs — phase-1 light-up frontend tests.
//
// Phase 1 wires real data into every previously-stubbed phase-0 view:
//   * L0 workspace identity table (structured ``state.workspace``).
//   * L0 lineage arrows from ``parent_epoch_id``.
//   * L1 epoch goal header reading ``epochs.goal``.
//   * L1 per-judge × generation heatmap.
//   * L2 per-judge and per-entry tables.
//   * L3 per-judge comparison + primary-driver call-out.
//   * L4 per-judge run breakdown.
//
// Each test installs a fresh DOM, seeds the module's cache or
// ``state`` slot the renderer reads, drives one render, and asserts
// that the resulting DOM contains the expected text. We never hit the
// network — caches are populated synthetically so the tests run in
// isolation from any server.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

const router = await import('../js/views/phase0_router.js');  // noqa: needed for hash + crumb
void router;

const { state } = await import('../js/core/state.js');
const ws = await import('../js/views/phase0_workspace.js');
const epoch = await import('../js/views/phase0_epoch.js');
const generation = await import('../js/views/phase0_generation.js');
const round = await import('../js/views/phase0_round.js');
const runV = await import('../js/views/phase0_run.js');

// Helper: install one node by id, return the node.
function installNode(id, tag = 'div') {
  const node = document.createElement(tag);
  node.id = id;
  document.body.appendChild(node);
  return node;
}

// --- L0 workspace: structured identity -------------------------------

test('renderPhase0Workspace renders structured workspace identity rows', () => {
  installNode('phase0-workspace-env');
  installNode('phase0-workspace-lineage');
  installNode('phase0-workspace-sparkline');
  state.workspace = {
    root: '/tmp/.zicato',
    adk_entrypoint: 'mod:agent',
    source_roots: ['/tmp/src_a', '/tmp/src_b'],
    board_path: '/tmp/.zicato/epochs/e0/board.jsonl',
    brief_path: '/tmp/.zicato/epochs/e0/brief.md',
    scoring_path: '/tmp/.zicato/epochs/e0/scoring.json',
    mutation_point_count: 7,
    instance_id: 'phase1',
    created_at: '2026-05-27T00:00:00Z',
  };
  ws.resetWorkspaceCache();
  ws.renderPhase0Workspace();
  const text = document.getElementById('phase0-workspace-env').textContent;
  assert(text.includes('mod:agent'), 'entrypoint must render');
  assert(text.includes('/tmp/src_a'), 'first source root must render');
  assert(text.includes('/tmp/src_b'), 'second source root must render');
  assert(text.includes('7'), 'mutation_point_count must render');
  assert(text.includes('phase1'), 'instance_id must render');
});

test('renderPhase0Workspace L0 lineage renders the ribbon with every epoch id', () => {
  // The L0 redesign replaced the epoch-timeline list (which drew an
  // inline "→ child" arrow) with the lineage ribbon. The ribbon encodes
  // the parent_epoch_id relationship as a connector in its SVG layer
  // rather than as text, so the contract is now: every epoch surfaces as
  // a ribbon node carrying its id, at epoch zoom.
  installNode('phase0-workspace-env');
  installNode('phase0-workspace-lineage');
  installNode('phase0-workspace-sparkline');
  state.workspace = { root: '/tmp/.zicato' };
  ws.resetWorkspaceCache();
  const fakePayload = {
    current_epoch_id: 'e1',
    epochs: [
      {
        epoch_id: 'e0', goal: 'Iterate the planner.',
        best_scalar: 0.42, generation_count: 3, promoted_count: 2,
        closed: true, parent_epoch_id: null,
      },
      {
        epoch_id: 'e1', goal: null,
        best_scalar: 0.40, generation_count: 2, promoted_count: 1,
        closed: false, parent_epoch_id: 'e0',
      },
    ],
  };
  const origFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: true, status: 200, headers: new Map(),
    json: async () => fakePayload,
    text: async () => JSON.stringify(fakePayload),
  });
  ws.resetWorkspaceCache();
  ws.renderPhase0Workspace();
  return new Promise((resolve) => {
    setTimeout(() => {
      ws.renderPhase0Workspace();
      const slot = document.getElementById('phase0-workspace-lineage');
      const lineageText = slot.textContent;
      const ribbon = slot.querySelector('[class="ribbon ribbon-zoom-epochs"]');
      assert(ribbon != null, 'the lineage slot must render a ribbon at epoch zoom');
      assert(lineageText.includes('e0'), 'first epoch must surface on the ribbon');
      assert(lineageText.includes('e1'), 'second epoch must surface on the ribbon');
      globalThis.fetch = origFetch;
      resolve();
    }, 20);
  });
});

test('workspace lineage ribbon degrades gracefully with no epochs', () => {
  // Goals are an L1 concern now; the L0 ribbon does not render per-epoch
  // goal text. The empty-workspace path must show the empty state rather
  // than throw.
  installNode('phase0-workspace-env');
  installNode('phase0-workspace-lineage');
  installNode('phase0-workspace-sparkline');
  state.workspace = { root: '/tmp/.zicato' };
  ws.resetWorkspaceCache();
  const fakePayload = { current_epoch_id: null, epochs: [] };
  const origFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: true, status: 200, headers: new Map(),
    json: async () => fakePayload,
    text: async () => JSON.stringify(fakePayload),
  });
  ws.renderPhase0Workspace();
  return new Promise((resolve) => {
    setTimeout(() => {
      ws.renderPhase0Workspace();
      const slot = document.getElementById('phase0-workspace-lineage');
      const text = slot.textContent;
      assert(text.includes('Epoch lineage'),
        `lineage card title must still render; got: ${text.slice(0, 200)}`);
      assert(text.toLowerCase().includes('no epochs'),
        `empty workspace must show an empty state; got: ${text.slice(0, 200)}`);
      // No ribbon node and no crash.
      assert(slot.querySelector('[class="ribbon ribbon-zoom-epochs"]') == null,
        'an empty workspace must not draw a populated ribbon');
      globalThis.fetch = origFetch;
      resolve();
    }, 20);
  });
});

// --- L1 epoch: goal + per-judge heatmap ------------------------------

test('renderPhase0Epoch renders the frozen goal when present', () => {
  installNode('phase0-epoch-goal');
  installNode('phase0-epoch-contract-diff');
  installNode('phase0-epoch-spine');
  installNode('phase0-epoch-heatmap-entries');
  installNode('phase0-epoch-heatmap-judges');
  installNode('phase0-epoch-experiments');
  installNode('phase0-epoch-analysis');
  epoch.resetContractDiffCache();
  epoch.resetPerJudgeTrendCache();
  epoch.resetAnalysisCache();
  state.epochDef = {
    epoch_id: 'e0',
    goal: 'Tighten the planner.',
    experiments: [],
  };
  epoch.renderPhase0Epoch({ epochId: 'e0' });
  const text = document.getElementById('phase0-epoch-goal').textContent;
  assert(text.includes('Tighten the planner'), `goal must render; got: ${text}`);
});

test('renderPhase0Epoch shows "(no goal recorded)" when empty', () => {
  installNode('phase0-epoch-goal');
  installNode('phase0-epoch-contract-diff');
  installNode('phase0-epoch-spine');
  installNode('phase0-epoch-heatmap-entries');
  installNode('phase0-epoch-heatmap-judges');
  installNode('phase0-epoch-experiments');
  installNode('phase0-epoch-analysis');
  epoch.resetContractDiffCache();
  epoch.resetPerJudgeTrendCache();
  epoch.resetAnalysisCache();
  state.epochDef = { epoch_id: 'e0', goal: '', experiments: [] };
  epoch.renderPhase0Epoch({ epochId: 'e0' });
  const text = document.getElementById('phase0-epoch-goal').textContent;
  assert(text.includes('(no goal recorded)'),
    `placeholder must render; got: ${text}`);
});

// The per-judge heatmap now lives behind the "judges" tab of the single
// folded heatmap card (rendered into the entries slot); the standalone
// judges slot is cleared. This test activates that tab and reads it.
test('renderPhase0Epoch renders the per-judge × generation heatmap (judges tab)', () => {
  installNode('phase0-epoch-goal');
  installNode('phase0-epoch-contract-diff');
  installNode('phase0-epoch-spine');
  installNode('phase0-epoch-heatmap-entries');
  installNode('phase0-epoch-heatmap-judges');
  installNode('phase0-epoch-experiments');
  installNode('phase0-epoch-analysis');
  epoch.resetContractDiffCache();
  epoch.resetPerJudgeTrendCache();
  epoch.resetAnalysisCache();
  epoch.resetHealthReportCache();
  epoch.resetHeatmapTab();
  state.epochDef = { epoch_id: 'e0', goal: 'g', experiments: [] };
  const payload = {
    epoch_id: 'e0',
    generations: ['v0', 'v1', 'v2'],
    judges: [
      { judge_name: 'critic_A', by_generation: { v0: 0.3, v1: 0.2, v2: 0.1 } },
      { judge_name: 'critic_B', by_generation: { v0: 0.4, v1: 0.5 } },
    ],
  };
  const origFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: true, status: 200, headers: new Map(),
    json: async () => payload, text: async () => JSON.stringify(payload),
  });
  epoch.renderPhase0Epoch({ epochId: 'e0' });
  return new Promise((resolve) => {
    setTimeout(() => {
      epoch.renderPhase0Epoch({ epochId: 'e0' });
      // Toggle to the judges tab inside the folded heatmap card.
      const slot = document.getElementById('phase0-epoch-heatmap-entries');
      let judgesTab = null;
      const walk = (n) => {
        if (!n || n.nodeType !== 1) return;
        if (typeof n.hasAttribute === 'function'
            && n.getAttribute('data-heatmap-tab') === 'judges') judgesTab = n;
        for (const c of n.children) walk(c);
      };
      walk(slot);
      assert(judgesTab != null, 'the judges tab must exist in the folded heatmap card');
      judgesTab.dispatchEvent(makeEvent('click'));
      epoch.renderPhase0Epoch({ epochId: 'e0' });
      const text = document.getElementById('phase0-epoch-heatmap-entries').textContent;
      assert(text.includes('critic_A'), 'critic_A row must render');
      assert(text.includes('critic_B'), 'critic_B row must render');
      assert(text.includes('v0') && text.includes('v1') && text.includes('v2'),
        'every spine generation must render as a column');
      globalThis.fetch = origFetch;
      resolve();
    }, 20);
  });
});

// --- L2 generation: per-judge + per-entry ----------------------------

test('renderPhase0Generation populates per-judge and per-entry tables', () => {
  installNode('phase0-gen-hypothesis');
  installNode('phase0-gen-patches');
  installNode('phase0-gen-entries');
  installNode('phase0-gen-judges');
  installNode('phase0-gen-compare');
  generation.resetGenerationCaches();
  state.epochDef = {
    epoch_id: 'e0',
    experiments: [{ generation_id: 'v1', parent_generation_id: 'v0', patches: {} }],
  };
  const perJudge = {
    epoch_id: 'e0', generation_id: 'v1',
    judges: [
      { judge_name: 'critic_A', weighted_loss: 0.2, raw_loss: 0.3, weight: 0.5, run_count: 1 },
    ],
  };
  const perEntry = {
    epoch_id: 'e0', generation_id: 'v1',
    tournament_id: 'e0:v0->v1',
    entries: [
      { entry_id: 'entry_alpha', run_id: 'run_v1', drift_loss: 0.2,
        pass_fail: 1, runtime_ms: 200, wall_clock_budget_exceeded: false },
    ],
  };
  const origFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const body = url.includes('/per-judge') ? perJudge : perEntry;
    return {
      ok: true, status: 200, headers: new Map(),
      json: async () => body, text: async () => JSON.stringify(body),
    };
  };
  generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v1' });
  return new Promise((resolve) => {
    setTimeout(() => {
      generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v1' });
      const jt = document.getElementById('phase0-gen-judges').textContent;
      assert(jt.includes('critic_A'),
        `per-judge row must render; got: ${jt.slice(0, 200)}`);
      const et = document.getElementById('phase0-gen-entries').textContent;
      assert(et.includes('entry_alpha'),
        `per-entry row must render; got: ${et.slice(0, 200)}`);
      // The redundant "tournament · e0:v0->v1" header line was dropped in
      // the L2 redesign — the "vs <champion>" column already conveys the
      // matchup context.
      assert(et.includes('vs v0'),
        `vs-champion delta column header must render; got: ${et.slice(0, 200)}`);
      globalThis.fetch = origFetch;
      resolve();
    }, 20);
  });
});

// --- L3 round: per-judge comparison + primary driver -----------------
//
// The legacy L3 smoke test that lived here was coupled to the pre-redesign
// round internals (the removed `roundEntriesPayload` accessor) and the old
// flat-table markup. The redesigned decision view (gate ladder, per-entry
// diverging A/B, scalar waterfall, and the primary-driver call-out) is
// covered comprehensively by `phase0_round_decision.test.mjs`.

// --- L4 run: per-judge breakdown -------------------------------------

test('renderPhase0Run populates the per-judge breakdown for the focused run', () => {
  installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  installNode('phase0-run-events');
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  const payload = {
    run_id: 'run_v1',
    judges: [
      { judge_name: 'critic_A', weighted_loss: 0.2, raw_loss: 0.4, weight: 0.5 },
    ],
  };
  const origFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: true, status: 200, headers: new Map(),
    json: async () => payload, text: async () => JSON.stringify(payload),
  });
  runV.renderPhase0Run({ epochId: 'e0', generationId: 'v1', entryId: 'entry_alpha' });
  return new Promise((resolve) => {
    setTimeout(() => {
      runV.renderPhase0Run({ epochId: 'e0', generationId: 'v1', entryId: 'entry_alpha' });
      const text = document.getElementById('phase0-run-judges').textContent;
      assert(text.includes('critic_A'),
        `per-judge row must render; got: ${text.slice(0, 200)}`);
      assert(text.includes('run_v1'), 'resolved run_id should surface');
      globalThis.fetch = origFetch;
      resolve();
    }, 20);
  });
});

await run();

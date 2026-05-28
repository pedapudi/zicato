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

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

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

test('renderPhase0Workspace L0 lineage shows arrow when parent_epoch_id set', () => {
  installNode('phase0-workspace-env');
  installNode('phase0-workspace-lineage');
  installNode('phase0-workspace-sparkline');
  state.workspace = { root: '/tmp/.zicato' };
  ws.resetWorkspaceCache();
  // Seed the workspace cache directly so the renderer paints from it
  // without going through the network.
  const fakePayload = {
    current_epoch_id: 'e0',
    epochs: [
      {
        epoch_id: 'e0', goal: 'Iterate the planner.',
        best_scalar: 0.42, generation_count: 3, promoted_count: 2,
        closed: false, parent_epoch_id: null,
      },
      {
        epoch_id: 'e1', goal: null,
        best_scalar: 0.40, generation_count: 2, promoted_count: 1,
        closed: false, parent_epoch_id: 'e0',
      },
    ],
    sparkline: [{ epoch_id: 'e0', scalar: 0.42 }, { epoch_id: 'e1', scalar: 0.40 }],
  };
  // Inject by calling renderPhase0Workspace twice: first call kicks off
  // the network request (which fails synchronously here), but to drive
  // the lineage we directly seed the cache via a re-export hook below.
  // Simpler: monkeypatch fetchJson via the module's internal cache. We
  // know resetWorkspaceCache + a synchronous renderer call repaints
  // from null payload — so we need a way to seed it. Add a small test
  // hook: call renderPhase0Workspace once with no cache (renders
  // empty), then mutate by re-exporting the cache slot via a side
  // door — but the module deliberately keeps the cache private. So:
  // assert the empty render path doesn't show arrows.
  ws.renderPhase0Workspace();
  // Manually re-seed by setting payload through the public API: the
  // workspacePayload() reader returns the cache; we don't have a
  // setter. Use the same hack the module's resetWorkspaceCache uses
  // and rely on the public render with a stubbed window.fetch.
  const origFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: true, status: 200, headers: new Map(),
    json: async () => fakePayload,
    text: async () => JSON.stringify(fakePayload),
  });
  // Trigger the async fetch; await a microtask so the promise resolves.
  ws.resetWorkspaceCache();
  ws.renderPhase0Workspace();
  return new Promise((resolve) => {
    setTimeout(() => {
      // The fetched payload is now in the cache; call render again to
      // paint it.
      ws.renderPhase0Workspace();
      const lineageText = document.getElementById('phase0-workspace-lineage').textContent;
      assert(lineageText.includes('e0'), 'first epoch row must render');
      assert(lineageText.includes('e1'), 'second epoch row must render');
      assert(lineageText.includes('→ e1'),
        `parent_epoch_id e0 → e1 arrow must render; got: ${lineageText.slice(0, 200)}`);
      globalThis.fetch = origFetch;
      resolve();
    }, 20);
  });
});

test('workspace lineage table falls back to "(no goal recorded)" when goal absent', () => {
  installNode('phase0-workspace-env');
  installNode('phase0-workspace-lineage');
  installNode('phase0-workspace-sparkline');
  state.workspace = { root: '/tmp/.zicato' };
  ws.resetWorkspaceCache();
  const fakePayload = {
    current_epoch_id: 'e0',
    epochs: [{
      epoch_id: 'e0', goal: null,
      best_scalar: null, generation_count: 0, promoted_count: 0,
      closed: false, parent_epoch_id: null,
    }],
    sparkline: [],
  };
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
      const text = document.getElementById('phase0-workspace-lineage').textContent;
      assert(text.includes('(no goal recorded)'),
        `fallback text must render; got: ${text.slice(0, 200)}`);
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
  installNode('phase0-epoch-journal');
  epoch.resetContractDiffCache();
  epoch.resetPerJudgeTrendCache();
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
  installNode('phase0-epoch-journal');
  epoch.resetContractDiffCache();
  epoch.resetPerJudgeTrendCache();
  state.epochDef = { epoch_id: 'e0', goal: '', experiments: [] };
  epoch.renderPhase0Epoch({ epochId: 'e0' });
  const text = document.getElementById('phase0-epoch-goal').textContent;
  assert(text.includes('(no goal recorded)'),
    `placeholder must render; got: ${text}`);
});

test('renderPhase0Epoch renders the per-judge × generation heatmap', () => {
  installNode('phase0-epoch-goal');
  installNode('phase0-epoch-contract-diff');
  installNode('phase0-epoch-spine');
  installNode('phase0-epoch-heatmap-entries');
  installNode('phase0-epoch-heatmap-judges');
  installNode('phase0-epoch-journal');
  epoch.resetContractDiffCache();
  epoch.resetPerJudgeTrendCache();
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
      const text = document.getElementById('phase0-epoch-heatmap-judges').textContent;
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
      assert(et.includes('e0:v0->v1'),
        'tournament_id FK should surface on the per-entry table');
      globalThis.fetch = origFetch;
      resolve();
    }, 20);
  });
});

// --- L3 round: per-judge comparison + primary driver -----------------

test('renderPhase0Round renders per-judge comparison with primary driver', () => {
  installNode('phase0-round-vs');
  installNode('phase0-round-entries');
  installNode('phase0-round-judges');
  installNode('phase0-round-decision');
  round.resetRoundCaches();
  state.epochDef = { epoch_id: 'e0' };
  state.bracket = { matchups: [{ champion: 'v1', challenger: 'v2', decision: 'promoted' }] };
  const perEntry = {
    epoch_id: 'e0', generation_id: 'v2', tournament_id: 'e0:v1->v2',
    entries: [
      { entry_id: 'entry_alpha', drift_loss: 0.2, pass_fail: 1 },
    ],
  };
  const perEntryChamp = {
    epoch_id: 'e0', generation_id: 'v1', tournament_id: 'e0:v0->v1',
    entries: [
      { entry_id: 'entry_alpha', drift_loss: 0.3, pass_fail: 1 },
    ],
  };
  const judgeCmp = {
    epoch_id: 'e0', champion: 'v1', challenger: 'v2',
    judges: [
      { judge_name: 'critic_A', champion_weighted_loss: 0.4,
        challenger_weighted_loss: 0.1, delta: -0.3 },
      { judge_name: 'critic_B', champion_weighted_loss: 0.2,
        challenger_weighted_loss: 0.25, delta: 0.05 },
    ],
    primary_driver: 'critic_A',
  };
  const origFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    let body = null;
    if (url.includes('/per-judge-comparison')) body = judgeCmp;
    else if (url.includes('/v1/per-entry')) body = perEntryChamp;
    else if (url.includes('/v2/per-entry')) body = perEntry;
    return {
      ok: true, status: 200, headers: new Map(),
      json: async () => body, text: async () => JSON.stringify(body),
    };
  };
  round.renderPhase0Round({ epochId: 'e0', championId: 'v1', challengerId: 'v2' });
  return new Promise((resolve, reject) => {
    // Poll: wait until both caches are populated, then assert.
    const params = { epochId: 'e0', championId: 'v1', challengerId: 'v2' };
    const start = Date.now();
    function tick() {
      try {
        const haveEntries = round.roundEntriesPayload('e0', 'v1', 'v2');
        const haveJudges = round.roundJudgesPayload('e0', 'v1', 'v2');
        if (haveEntries && haveJudges) {
          round.renderPhase0Round(params);
          const jt = document.getElementById('phase0-round-judges').textContent;
          assert(jt.includes('critic_A'), 'critic_A row must render');
          assert(jt.includes('primary driver'),
            `primary driver call-out must render; got: ${jt.slice(0, 200)}`);
          const et = document.getElementById('phase0-round-entries').textContent;
          assert(et.includes('entry_alpha'), 'per-entry row must render');
          assert(et.includes('e0:v1->v2'),
            'challenger tournament_id should surface on the per-entry header');
          globalThis.fetch = origFetch;
          resolve();
        } else if (Date.now() - start > 500) {
          globalThis.fetch = origFetch;
          reject(new Error('caches did not populate within 500ms: '
            + JSON.stringify({ haveEntries: !!haveEntries, haveJudges: !!haveJudges })));
        } else {
          setTimeout(tick, 10);
        }
      } catch (err) {
        globalThis.fetch = origFetch;
        reject(err);
      }
    }
    setTimeout(tick, 5);
  });
});

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

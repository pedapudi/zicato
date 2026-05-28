// test/phase1_5.test.mjs — phase-1.5 dashboard cleanup tests.
//
// Three gaps surfaced during the visual tour after Phase 1:
//   * L4 expectation outcomes table (was placeholder copy).
//   * L4 run-header metrics for completed runs (was placeholder copy).
//   * L1 Recent experiments rendered through the SAME compact
//     Proposed/Outcome helper L2 uses, so the two cannot drift.
//
// Each test installs a fresh DOM, drives one render with a synthetic
// payload, and asserts the resulting DOM contains the expected text.
// Tests for the shared ``renderHypothesisOutcomeCompact`` cover both
// the full (L2) and compact (L1) modes against the same fixture so the
// shared contract is pinned.

import { installDom, test, run, assert } from './harness.mjs';

installDom();

const router = await import('../js/views/phase0_router.js');
void router;

const { state } = await import('../js/core/state.js');
const epoch = await import('../js/views/phase0_epoch.js');
const generation = await import('../js/views/phase0_generation.js');
const runV = await import('../js/views/phase0_run.js');
const hypBlock = await import('../js/core/hypothesis_block.js');

function installNode(id, tag = 'div') {
  const node = document.createElement(tag);
  node.id = id;
  document.body.appendChild(node);
  return node;
}

// Helper: install a synchronous fetch mock that resolves immediately
// (no microtask gap). The caller passes a function ``(url) -> body``;
// every fetch resolves with a ``{ json, text }`` response wrapper.
//
// Returns a ``restore`` function the test calls before returning so
// every test leaves ``globalThis.fetch`` exactly as it found it.
// Cross-test fetch leakage is what made the harness hang when an
// earlier draft of this file ran alongside refresh.test.mjs — that
// test pumps render.js's full snapshot, which fires its own fetches.
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

// One synthetic experiment carrying every field the helper looks at —
// reused by the L1/L2 shared-helper tests below.
const SAMPLE_EXP = {
  generation_id: 'v3',
  hypothesis: {
    core_idea: 'Inject strict topicality constraints into the researcher prompt.',
    why: 'Off-topic drift dominates telemetry.',
    risks: 'Tightening scope may reduce creativity.',
    modulating: ['researcher_instruction'],
    expected_pass_rate_delta: '+0.10 to +0.25',
    expected_drift_movements: [
      { kind: 'off_topic', direction: 'decrease', magnitude: 'medium' },
    ],
  },
  outcome: {
    tournament_decision: 'promoted',
    scalar_score_delta: -24.331,
    pass_rate_delta: 0.333,
    drift_loss_delta: -24.0,
    rejection_reason: '',
    ran_at: '2026-05-20T02:06:22+00:00',
  },
};

// --- Shared helper: renderHypothesisOutcomeCompact -------------------

test('hypothesis_block compact mode renders core_idea, why, predicted drift, verdict', () => {
  const block = hypBlock.renderHypothesisOutcomeCompact(
    SAMPLE_EXP.hypothesis, SAMPLE_EXP.outcome, { compact: true },
  );
  const text = block.textContent;
  assert(text.includes('Proposed (before)'), 'must label the proposed block');
  assert(text.includes('Outcome (after)'), 'must label the outcome block');
  assert(text.includes('topicality constraints'),
    `core_idea must render; got: ${text.slice(0, 200)}`);
  assert(text.includes('Off-topic drift dominates'),
    'why must render');
  assert(text.includes('off_topic decrease'),
    'predicted drift movement must render as kind direction');
  assert(text.includes('+0.10 to +0.25'),
    'expected_pass_rate_delta must surface in compact mode');
  assert(text.includes('promoted'),
    'outcome verdict badge must render');
  assert(text.includes('-24.331'),
    'Δscalar metric must render');
});

test('hypothesis_block compact mode omits the long-form fields', () => {
  const block = hypBlock.renderHypothesisOutcomeCompact(
    SAMPLE_EXP.hypothesis, SAMPLE_EXP.outcome, { compact: true },
  );
  const text = block.textContent;
  // Compact mode drops the risks line, modulating sites and the
  // summary / ran_at trailer — the per-experiment list on L1 must stay
  // short so the page reads as a digest.
  assert(!text.includes('Risks.'),
    `compact mode must NOT render risks; got: ${text.slice(0, 400)}`);
  assert(!text.includes('Modulating.'),
    'compact mode must NOT render modulating sites');
  assert(!text.includes('evaluated 2026-05-20'),
    'compact mode must NOT render the ran_at trailer');
});

test('hypothesis_block full mode renders risks, modulating, ran_at', () => {
  const block = hypBlock.renderHypothesisOutcomeCompact(
    SAMPLE_EXP.hypothesis, SAMPLE_EXP.outcome, { compact: false },
  );
  const text = block.textContent;
  assert(text.includes('Risks.'), 'full mode must render risks lead');
  assert(text.includes('Tightening scope'),
    `risks body must render; got: ${text.slice(0, 400)}`);
  assert(text.includes('Modulating.'), 'full mode must render modulating lead');
  assert(text.includes('researcher_instruction'),
    'modulating site must render');
  assert(text.includes('evaluated 2026-05-20'),
    `ran_at must surface in full mode; got: ${text.slice(0, 400)}`);
});

test('hypothesis_block handles missing outcome with placeholder text', () => {
  const block = hypBlock.renderHypothesisOutcomeCompact(
    SAMPLE_EXP.hypothesis, null, { compact: true },
  );
  const text = block.textContent;
  assert(text.includes('No tournament verdict recorded'),
    `missing outcome placeholder must render; got: ${text.slice(0, 200)}`);
  assert(text.includes('incomplete'),
    'missing outcome placeholder must label as incomplete');
});

test('hypothesis_block handles missing hypothesis predictions gracefully', () => {
  // A bare hypothesis with only ``core_idea`` set still renders — the
  // predicted-drift / expected-pass-rate / risks rows all sit out.
  const block = hypBlock.renderHypothesisOutcomeCompact(
    { core_idea: 'do a thing' },
    { tournament_decision: 'rejected', scalar_score_delta: 1.0 },
    { compact: true },
  );
  const text = block.textContent;
  assert(text.includes('do a thing'),
    `core_idea must render even without predictions; got: ${text.slice(0, 200)}`);
  assert(text.includes('rejected'),
    'outcome verdict still renders');
  // No "Predicted drift." lead — we did not feed any predictions in.
  assert(!text.includes('Predicted drift.'),
    'absent expected_drift_movements must NOT render the lead');
});

// --- L1 epoch: Recent experiments through the shared helper ----------
//
// L1's renderPhase0Epoch fires several ensure*() fetches (per-judge
// trend, contract diff). They all fail-soft in the harness with an
// empty payload because our mockFetch returns benign objects. We don't
// drive the timers here — only the sync hypothesis-block path is under
// test on this code path, since the rest of the L1 page already has
// dedicated coverage in phase1.test.mjs.

test('renderPhase0Epoch renders Recent experiments as full-width cards', () => {
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
    goal: 'g',
    experiments: [SAMPLE_EXP],
  };
  const restoreFetch = mockFetch(() => ({}));
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const text = document.getElementById('phase0-epoch-experiments').textContent;
    assert(text.includes('Recent experiments'),
      `section header must render; got: ${text.slice(0, 200)}`);
    // Card layout: generation id, uppercase verdict pill, and the
    // labelled "why" / "predicted" inline rows.
    assert(text.includes('v3'),
      'generation id must render in the card header');
    assert(text.includes('PROMOTED'),
      `verdict pill must render as uppercase label; got: ${text.slice(0, 200)}`);
    assert(text.includes('topicality constraints'),
      'core_idea must render as the prominent first body line');
    assert(text.includes('why'),
      'why lead label must render');
    assert(text.includes('predicted'),
      'predicted lead label must render');
    // The old 2-column "Proposed (before)" split must be gone.
    assert(!text.includes('Proposed (before)'),
      `L1 must NOT render the old two-column compact helper; got: ${text.slice(0, 200)}`);
    // Compact still: no risks / modulating on L1.
    assert(!text.includes('Modulating.'),
      `L1 cards must NOT render the modulating sites; got: ${text.slice(0, 400)}`);
  } finally {
    restoreFetch();
  }
});

test('renderPhase0Epoch shows empty state when no experiments yet', () => {
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
  state.epochDef = { epoch_id: 'e0', goal: 'g', experiments: [] };
  const restoreFetch = mockFetch(() => ({}));
  try {
    epoch.renderPhase0Epoch({ epochId: 'e0' });
    const text = document.getElementById('phase0-epoch-experiments').textContent;
    assert(text.includes('Recent experiments'), 'section header still renders');
    assert(text.includes('No experiments recorded'),
      `empty-state copy must render; got: ${text}`);
  } finally {
    restoreFetch();
  }
});

// --- L2 generation: Hypothesis · Alignment two-column block ----------
//
// The L2 redesign (Task #200) replaced the shared "Proposed (before) /
// Outcome (after)" helper with a custom Hypothesis · Alignment block.
// The hypothesis column still owns the operator's prose; the alignment
// column compares predicted dimensions to the actual outcome. The
// outcome metric numbers live in the hero card above, not here.

test('renderPhase0Generation hypothesis card renders Hypothesis + Alignment columns', () => {
  installNode('phase0-gen-hypothesis');
  installNode('phase0-gen-patches');
  installNode('phase0-gen-entries');
  installNode('phase0-gen-judges');
  installNode('phase0-gen-compare');
  generation.resetGenerationCaches();
  state.epochDef = {
    epoch_id: 'e0',
    experiments: [SAMPLE_EXP],
  };
  const restoreFetch = mockFetch(() => ({}));
  try {
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    const text = document.getElementById('phase0-gen-hypothesis').textContent;
    assert(text.includes('Hypothesis') && text.includes('Alignment vs Outcome'),
      `both column headers must render; got: ${text.slice(0, 200)}`);
    // The Hypothesis column owns the operator's risks block (relocated
    // into the alignment column at the bottom). Modulating sites no
    // longer surface here — they are the same data as the Patches card.
    assert(text.includes('Risks (operator-stated)'),
      `risks block must render in the alignment column; got: ${text.slice(0, 400)}`);
    assert(!text.includes('Modulating.'),
      `redundant Modulating line must NOT render — it duplicates Patches; got: ${text.slice(0, 200)}`);
  } finally {
    restoreFetch();
  }
});

// --- L4 run: expectation outcomes table ------------------------------
//
// renderPhase0Run kicks off async ensure*() fetches that paint on the
// second render. We pin a synchronous mock, render twice in a microtask
// flush, and assert the painted DOM. The mock is restored before the
// test returns so refresh.test.mjs (which loads next under run-all.mjs)
// starts with the original globalThis.fetch.

test('renderPhase0Run renders the expectation outcomes table from /expectations', async () => {
  installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  installNode('phase0-run-events');
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  const expectationPayload = {
    epoch_id: 'e0', generation_id: 'v3', entry_id: 'predicate_fail',
    outcomes: [
      {
        kind: 'predicate', passed: false,
        detail: 'predicate returned False',
        judge_name: null, score: null,
      },
      {
        kind: 'rubric', passed: true,
        detail: 'scored above threshold',
        judge_name: 'presentation_quality', score: 0.875,
      },
    ],
  };
  const headerPayload = {
    epoch_id: 'e0', generation_id: 'v3', entry_id: 'predicate_fail',
    drift_loss: 0.5, pass_fail: false,
    runtime_ms: 83160, tokens_spent: 12345, output_chars: 5456,
    turns_completed: null, plan_revisions: 1,
    wall_clock_budget_exceeded: false, run_id: 'run_x',
  };
  const restoreFetch = mockFetch((url) => {
    if (url.includes('/expectations')) return expectationPayload;
    if (url.includes('/header')) return headerPayload;
    return { run_id: null, judges: [] };
  });
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'predicate_fail',
    });
    // Two microtask flushes drain the fetch + the cache write.
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'predicate_fail',
    });
    const text = document.getElementById('phase0-run-expectation').textContent;
    assert(text.includes('Expectation outcomes'),
      `section header must render; got: ${text.slice(0, 200)}`);
    assert(text.includes('predicate'),
      'first expectation kind must render');
    assert(text.includes('FAIL'),
      'failed verdict text must render');
    assert(text.includes('rubric'),
      'second expectation kind must render');
    assert(text.includes('PASS'),
      'passed verdict text must render');
    assert(text.includes('presentation_quality'),
      `rubric judge_name must surface in notes; got: ${text.slice(0, 400)}`);
    assert(text.includes('0.875'),
      'rubric score must surface in notes');
  } finally {
    restoreFetch();
  }
});

test('renderPhase0Run shows empty state when no expectations recorded', async () => {
  installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  installNode('phase0-run-events');
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  const restoreFetch = mockFetch((url) => {
    if (url.includes('/expectations')) {
      return { epoch_id: 'e0', generation_id: 'v0', entry_id: 'no_exp', outcomes: [] };
    }
    if (url.includes('/header')) {
      return {
        epoch_id: 'e0', generation_id: 'v0', entry_id: 'no_exp',
        drift_loss: null, pass_fail: null, runtime_ms: null,
        tokens_spent: null, output_chars: null, turns_completed: null,
        plan_revisions: null, wall_clock_budget_exceeded: null, run_id: null,
      };
    }
    return { run_id: null, judges: [] };
  });
  try {
    runV.renderPhase0Run({ epochId: 'e0', generationId: 'v0', entryId: 'no_exp' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({ epochId: 'e0', generationId: 'v0', entryId: 'no_exp' });
    const text = document.getElementById('phase0-run-expectation').textContent;
    assert(text.includes('(no expectations recorded for this run)'),
      `empty-state copy must render; got: ${text}`);
  } finally {
    restoreFetch();
  }
});

// --- L4 run: header metrics tiles ------------------------------------

test('renderPhase0Run renders run-header metrics tiles from /header for completed runs', async () => {
  installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  installNode('phase0-run-events');
  runV.resetRunCaches();
  state.activeRuns = []; // no live run
  state.logTail = { events: [] };
  const headerPayload = {
    epoch_id: 'e0', generation_id: 'v3', entry_id: 'predicate_fail',
    drift_loss: 0.5, pass_fail: false,
    runtime_ms: 83160, tokens_spent: 12345, output_chars: 5456,
    turns_completed: null, plan_revisions: 1,
    wall_clock_budget_exceeded: false, run_id: 'run_predicate_fail',
  };
  const restoreFetch = mockFetch((url) => {
    if (url.includes('/header')) return headerPayload;
    if (url.includes('/expectations')) return { outcomes: [] };
    return { run_id: null, judges: [] };
  });
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'predicate_fail',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'predicate_fail',
    });
    const text = document.getElementById('phase0-run-header').textContent;
    // The placeholder text must be GONE — Phase 1.5 replaces it with
    // real header tiles.
    assert(!text.includes('historical metrics land once L4 fetches'),
      `placeholder text must NOT render after wire-up; got: ${text.slice(0, 400)}`);
    assert(text.includes('verdict') && text.includes('FAIL'),
      'verdict tile must render');
    assert(text.includes('runtime'),
      'runtime tile must render');
    assert(text.includes('tokens'),
      'tokens tile must render');
    assert(text.includes('12345'),
      `tokens_spent value must render; got: ${text.slice(0, 400)}`);
    assert(text.includes('5456'),
      'output_chars value must render');
    assert(text.includes('plan revisions'),
      'plan revisions tile must render');
    assert(text.includes('run_predicate_fail'),
      'run_id must surface on the header');
  } finally {
    restoreFetch();
  }
});

test('renderPhase0Run surfaces wall-clock budget exceeded warning when flag set', async () => {
  installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  installNode('phase0-run-events');
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  const restoreFetch = mockFetch((url) => {
    if (url.includes('/header')) {
      return {
        epoch_id: 'e0', generation_id: 'v3', entry_id: 'rubric_pass',
        drift_loss: 0.0, pass_fail: true,
        runtime_ms: 360000, tokens_spent: 0, output_chars: 13726,
        turns_completed: 6, plan_revisions: 4,
        wall_clock_budget_exceeded: true, run_id: 'run_rubric_pass',
      };
    }
    if (url.includes('/expectations')) return { outcomes: [] };
    return { run_id: null, judges: [] };
  });
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'rubric_pass',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'rubric_pass',
    });
    const text = document.getElementById('phase0-run-header').textContent;
    assert(text.includes('Wall-clock budget exceeded'),
      `budget-exceeded warning must render; got: ${text.slice(0, 400)}`);
    assert(text.includes('verdict') && text.includes('PASS'),
      'pass verdict tile must render');
  } finally {
    restoreFetch();
  }
});

test('renderPhase0Run keeps live-run header when state.activeRuns has the entry', () => {
  installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  installNode('phase0-run-events');
  runV.resetRunCaches();
  state.activeRuns = [
    { entry_id: 'live_entry', progress: 0.4, elapsed_seconds: 12, status: 'running' },
  ];
  state.logTail = { events: [] };
  const restoreFetch = mockFetch(() => ({ outcomes: [] }));
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v0', entryId: 'live_entry',
    });
    const text = document.getElementById('phase0-run-header').textContent;
    // Live run prefers live snapshot — the completed-run tiles must NOT
    // appear (no "runtime" tile label, no "tokens" tile label, etc.).
    assert(text.includes('progress'),
      `live progress must render; got: ${text}`);
    assert(text.includes('40%'),
      'live progress percentage must render');
    assert(text.includes('status'),
      'live status must render');
    assert(text.includes('running'),
      'live status value must render');
    // Confirm completed-run tiles ARE absent (would say "verdict" tile).
    assert(!text.includes('plan revisions'),
      'completed-run tiles must NOT render for a live run');
  } finally {
    // Drain any pending async fetches the live-run render scheduled so
    // they cannot leak into the next test file.
    runV.resetRunCaches();
    state.activeRuns = [];
    restoreFetch();
  }
});

await run();

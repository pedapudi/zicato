// test/v2_run.test.mjs — the v2 Run view (DASHBOARD-V2 §4.5).
//
// Pins the evidence view's contract:
//   * lineage resolution — epoch + champion (parent) from the route's
//     entryId + generationId, cold-deep-link safe.
//   * header metrics — the dense strip from /header (verdict / drift /
//     runtime / tokens / chars / turns / plan-revisions / ids), the
//     wall-clock-exceeded flag, and the harmonograf deep-link.
//   * expectations — typed outcomes from /expectations.
//   * conversation — champion | challenger SIDE-BY-SIDE BY DEFAULT,
//     turns rendered with role / agent / tool calls, drift / steering /
//     judge / plan annotations INLINE, the honest zero-turn fallback.
//   * per-judge — the weighted-loss table; honest empty otherwise.
//   * honest states everywhere via stateBlock (broken / running / empty).

import { installDom, test, run, assert } from './harness.mjs';

installDom();

const { state } = await import('../js/core/state.js');
const runView = await import('../js/v2/views/run.js');

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------
function seedLineage() {
  state.lineage = {
    generations: [
      { generation_id: 'v0', epoch_id: 'e0', parent_generation_id: null },
      { generation_id: 'v1', epoch_id: 'e0', parent_generation_id: 'v0' },
      { generation_id: 'v2', epoch_id: 'e0', parent_generation_id: 'v1' },
    ],
    experiments: [],
  };
  state.epochDef = {
    epoch_id: 'e0',
    experiments: [
      { generation_id: 'v0', parent_generation_id: null },
      { generation_id: 'v1', parent_generation_id: 'v0' },
      { generation_id: 'v2', parent_generation_id: 'v1' },
    ],
  };
  state.epoch = { id: 'e0' };
  // A run is LIVE in this fixture (active tournament) so the lingering
  // harmonograf_url resolves to a valid deep-link — harmonograf's server
  // only exists during a live run (the dead-port liveness gate). Without
  // a live signal the link is correctly suppressed.
  state.activeRuns = [];
  state.activeTournament = { champion: 'v1', challenger: 'v2' };
  state.heartbeat = { harmonograf_url: 'http://hgraf.local' };
}

const TRANSCRIPTS = {
  v1: {
    run_id: 'r-v1', event_count: 3, complete: true,
    turns: [
      { seq: 1, ts: '2026-05-30T00:00:01Z', agent: '', role: 'user',
        kind: 'run_started', text: 'Make a Q3 outline.',
        tool_calls: [], tool_results: [], run_index: 1 },
      { seq: 2, ts: '2026-05-30T00:00:02Z', agent: 'coordinator', role: 'agent',
        kind: 'task_completed', text: 'champion v1 outline content',
        tool_calls: [{ name: 'research_agent', args: { q: 'q3' } }],
        tool_results: [], run_index: 1 },
    ],
    annotations: [
      { kind: 'judge', summary: 'incorporates_feedback verdict', anchor_seq: 2, detail: {} },
    ],
  },
  v2: {
    run_id: 'r-v2', event_count: 4, complete: true,
    turns: [
      { seq: 1, ts: '2026-05-30T00:00:01Z', agent: '', role: 'user',
        kind: 'run_started', text: 'Make a Q3 outline.',
        tool_calls: [], tool_results: [], run_index: 1 },
      { seq: 2, ts: '2026-05-30T00:00:03Z', agent: 'coordinator', role: 'agent',
        kind: 'plan_revised', text: 'challenger v2 attempt that regressed',
        tool_calls: [], tool_results: [], run_index: 1 },
    ],
    annotations: [
      { kind: 'drift', summary: 'drift spike on topic discipline', anchor_seq: 2, detail: {} },
      { kind: 'plan', summary: 'plan revised once', anchor_seq: 1, detail: {} },
    ],
  },
};

function transcriptFor(gen) {
  const t = TRANSCRIPTS[gen];
  if (!t) {
    return {
      epoch_id: 'e0', generation_id: gen, entry_id: 'waffles_single',
      run_id: null, turns: [], annotations: [], event_count: 0, complete: false,
    };
  }
  return { epoch_id: 'e0', generation_id: gen, entry_id: 'waffles_single', ...t };
}

function headerFor(gen) {
  return {
    epoch_id: 'e0', generation_id: gen, entry_id: 'waffles_single',
    drift_loss: 0.635, pass_fail: true, runtime_ms: 42000,
    tokens_spent: 12345, output_chars: 6789, turns_completed: 2,
    plan_revisions: 1, wall_clock_budget_exceeded: false,
    run_id: 'r-' + gen, adk_session_id: 'sess-' + gen,
  };
}

function baseHandler(url) {
  if (url.includes('/transcript')) {
    const m = url.match(/\/run\/e0\/([^/]+)\/waffles_single\/transcript/);
    return transcriptFor(m ? m[1] : 'unknown');
  }
  if (url.includes('/header')) {
    const m = url.match(/\/run\/e0\/([^/]+)\/waffles_single\/header/);
    return headerFor(m ? m[1] : 'unknown');
  }
  if (url.includes('/expectations')) {
    return { outcomes: [
      { kind: 'Predicate', passed: true, detail: 'has 5 slides' },
      { kind: 'Rubric', passed: false, judge_name: 'topic_discipline', score: 0.4 },
    ] };
  }
  if (url.includes('/per-judge')) {
    return { run_id: 'r-v2', judges: [
      { judge_name: 'topic_discipline', weighted_loss: 0.21, raw_loss: 0.42, weight: 0.5 },
    ] };
  }
  return {};
}

function mockFetch(handler) {
  const original = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const body = handler(url);
    if (body === '__throw__') throw new Error('HTTP 500: boom');
    return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
  };
  return () => { globalThis.fetch = original; };
}

function makeHost() {
  const host = document.createElement('div');
  host.id = 'v2-view';
  document.body.appendChild(host);
  return host;
}

function route(entryId, genId) {
  return { view: 'run', params: { entryId, generationId: genId }, raw: '' };
}

async function settle(host, r) {
  runView.renderRun(host, r);
  await new Promise((res) => setTimeout(res, 0));
  await new Promise((res) => setTimeout(res, 0));
  runView.renderRun(host, r);
  await new Promise((res) => setTimeout(res, 0));
  runView.renderRun(host, r);
}

function classCount(node, cls) {
  let n = 0;
  for (const c of node.querySelectorAll('[class]')) {
    if ((c.getAttribute('class') || '').split(/\s+/).includes(cls)) n += 1;
  }
  return n;
}

// --------------------------------------------------------------------------
// Lineage resolution
// --------------------------------------------------------------------------
test('Run: resolves epoch + champion from the lineage / epoch contract', () => {
  seedLineage();
  assert(runView.resolveEpochId('v2') === 'e0', 'epoch resolves from lineage');
  assert(runView.resolveChampionId('v2') === 'v1', 'champion is the parent v1');
  assert(runView.resolveChampionId('v0') === null, 'the seed has no champion');
});

// --------------------------------------------------------------------------
// Under-specified route
// --------------------------------------------------------------------------
test('Run: a route without a generation renders an honest not-yet, not a blank', () => {
  seedLineage();
  runView.resetRunView();
  const host = makeHost();
  runView.renderRun(host, route('waffles_single', null));
  const stateNode = host.querySelector('[data-kind]');
  assert(stateNode && stateNode.getAttribute('data-kind') === 'not_yet',
    'no generation → not_yet state block');
  assert(host.textContent.toLowerCase().includes('no run selected'));
});

// --------------------------------------------------------------------------
// Header metrics
// --------------------------------------------------------------------------
test('Run: header renders the dense metric strip + ids + harmonograf link', async () => {
  seedLineage();
  runView.resetRunView();
  const host = makeHost();
  const restore = mockFetch(baseHandler);
  try {
    await settle(host, route('waffles_single', 'v2'));
    const txt = host.textContent;
    assert(txt.includes('drift loss') && txt.includes('0.635'), 'drift loss metric present');
    assert(txt.includes('verdict') && txt.includes('PASS'), 'verdict metric present');
    assert(txt.includes('tokens') && txt.includes('12345'), 'tokens metric present');
    assert(txt.includes('plan revisions') && txt.includes('1'), 'plan revisions metric present');
    assert(txt.includes('run_id · r-v2'), 'run id surfaced');
    assert(txt.includes('adk_session_id · sess-v2'), 'adk session id surfaced');
    // The harmonograf deep-link uses the adk session id.
    const links = host.querySelectorAll('[class]');
    let found = null;
    for (const a of links) {
      if (a.localName === 'a' && (a.getAttribute('href') || '').includes('sess-v2')) found = a;
    }
    assert(found != null, 'harmonograf deep-link points at the adk session id');
    assert(found.getAttribute('href').includes('hgraf.local'), 'link uses the harmonograf base');
  } finally { restore(); runView.resetRunView(); }
});

test('Run: wall-clock-exceeded surfaces the abort flag', async () => {
  seedLineage();
  runView.resetRunView();
  const host = makeHost();
  const restore = mockFetch((url) => {
    if (url.includes('/header')) {
      return { ...headerFor('v2'), wall_clock_budget_exceeded: true };
    }
    return baseHandler(url);
  });
  try {
    await settle(host, route('waffles_single', 'v2'));
    assert(host.textContent.toLowerCase().includes('wall-clock budget exceeded'),
      'abort flag is shown');
  } finally { restore(); runView.resetRunView(); }
});

// --------------------------------------------------------------------------
// Expectations
// --------------------------------------------------------------------------
test('Run: expectation outcomes render with verdicts', async () => {
  seedLineage();
  runView.resetRunView();
  const host = makeHost();
  const restore = mockFetch(baseHandler);
  try {
    await settle(host, route('waffles_single', 'v2'));
    const txt = host.textContent;
    assert(txt.includes('Predicate') && txt.includes('PASS'), 'predicate pass shown');
    assert(txt.includes('Rubric') && txt.includes('FAIL'), 'rubric fail shown');
    assert(txt.includes('topic_discipline'), 'judge name shown for the rubric');
  } finally { restore(); runView.resetRunView(); }
});

// --------------------------------------------------------------------------
// The conversation — side-by-side by default
// --------------------------------------------------------------------------
test('Run: champion | challenger render SIDE-BY-SIDE BY DEFAULT (two columns)', async () => {
  seedLineage();
  runView.resetRunView();
  const host = makeHost();
  const restore = mockFetch(baseHandler);
  try {
    await settle(host, route('waffles_single', 'v2'));
    assert(classCount(host, 'v2-run-col') === 2, 'two transcript columns by default');
    const txt = host.textContent;
    assert(txt.includes('champion v1 outline content'), 'champion (v1) transcript on one side');
    assert(txt.includes('challenger v2 attempt that regressed'), 'challenger (v2) transcript on the other');
    assert(txt.includes('research_agent'), 'tool call rendered in a turn');
    // Column labels make the comparison explicit.
    assert(txt.includes('champion') && txt.includes('challenger'), 'columns are labeled');
  } finally { restore(); runView.resetRunView(); }
});

test('Run: drift / judge / plan annotations render INLINE', async () => {
  seedLineage();
  runView.resetRunView();
  const host = makeHost();
  const restore = mockFetch(baseHandler);
  try {
    await settle(host, route('waffles_single', 'v2'));
    assert(classCount(host, 'v2-run-annot') >= 3,
      'all three annotations (judge on v1, drift+plan on v2) render inline');
    const txt = host.textContent;
    assert(txt.includes('drift spike on topic discipline'), 'drift annotation summary inline');
    assert(txt.includes('incorporates_feedback verdict'), 'judge annotation summary inline');
    assert(txt.includes('plan revised once'), 'plan annotation summary inline');
  } finally { restore(); runView.resetRunView(); }
});

test('Run: the seed (no champion) renders a single honest column', async () => {
  seedLineage();
  runView.resetRunView();
  const host = makeHost();
  const restore = mockFetch(baseHandler);
  try {
    await settle(host, route('waffles_single', 'v0'));
    assert(classCount(host, 'v2-run-col') === 1, 'seed gen → single column');
  } finally { restore(); runView.resetRunView(); }
});

test('Run: a completed run with zero turns shows the honest fallback, not a blank', async () => {
  seedLineage();
  runView.resetRunView();
  const host = makeHost();
  // The challenger ran (has a run_id) but produced no turns — a
  // wall-clock timeout. The champion v1 still has turns.
  const restore = mockFetch((url) => {
    if (url.includes('/run/e0/v2/waffles_single/transcript')) {
      return { epoch_id: 'e0', generation_id: 'v2', entry_id: 'waffles_single',
        run_id: 'r-v2', turns: [], annotations: [], event_count: 0, complete: true };
    }
    return baseHandler(url);
  });
  try {
    await settle(host, route('waffles_single', 'v2'));
    assert(host.textContent.includes('This run produced no transcript turns.'),
      'zero-turn fallback headline present');
    assert(host.textContent.includes('run · r-v2'), 'the run_id fact is shown, not a blank');
  } finally { restore(); runView.resetRunView(); }
});

// --------------------------------------------------------------------------
// Per-judge
// --------------------------------------------------------------------------
test('Run: per-judge breakdown renders the weighted-loss table', async () => {
  seedLineage();
  runView.resetRunView();
  const host = makeHost();
  const restore = mockFetch(baseHandler);
  try {
    await settle(host, route('waffles_single', 'v2'));
    const txt = host.textContent;
    assert(txt.includes('weighted loss'), 'per-judge table header present');
    assert(txt.includes('topic_discipline') && txt.includes('0.210'), 'judge weighted loss shown');
  } finally { restore(); runView.resetRunView(); }
});

test('Run: per-judge with no data shows an honest empty state', async () => {
  seedLineage();
  runView.resetRunView();
  const host = makeHost();
  const restore = mockFetch((url) => {
    if (url.includes('/per-judge')) return { run_id: 'r-v2', judges: [], note: 'single-judge board' };
    return baseHandler(url);
  });
  try {
    await settle(host, route('waffles_single', 'v2'));
    const judges = host.querySelector('[class]') ? host : null;
    void judges;
    assert(host.textContent.toLowerCase().includes('no per-judge data'),
      'honest empty message for the per-judge section');
  } finally { restore(); runView.resetRunView(); }
});

// --------------------------------------------------------------------------
// Honest broken state
// --------------------------------------------------------------------------
test('Run: a failed fetch surfaces a broken state with the reason verbatim', async () => {
  seedLineage();
  runView.resetRunView();
  const host = makeHost();
  const restore = mockFetch((url) => {
    if (url.includes('/expectations')) return '__throw__';
    return baseHandler(url);
  });
  try {
    await settle(host, route('waffles_single', 'v2'));
    let broken = null;
    for (const n of host.querySelectorAll('[data-kind]')) {
      if (n.getAttribute('data-kind') === 'broken') broken = n;
    }
    assert(broken != null, 'a broken state block is rendered for the failed section');
    assert(broken.textContent.includes('HTTP 500: boom'), 'the failure reason is shown verbatim');
  } finally { restore(); runView.resetRunView(); }
});

// --------------------------------------------------------------------------
// Cold deep-link — epoch not yet hydrated
// --------------------------------------------------------------------------
test('Run: a cold deep-link before lineage hydrates shows running, not a blank', async () => {
  // No lineage / epochDef / epoch — the SSE snapshot has not landed.
  state.lineage = { generations: [], experiments: [] };
  state.epochDef = null;
  state.epoch = { id: '—' };
  state.activeRuns = [];
  runView.resetRunView();
  const host = makeHost();
  runView.renderRun(host, route('waffles_single', 'v2'));
  let running = null;
  for (const n of host.querySelectorAll('[data-kind]')) {
    if (n.getAttribute('data-kind') === 'running') running = n;
  }
  assert(running != null, 'cold deep-link shows a running state while the epoch resolves');
  assert(host.textContent.toLowerCase().includes('resolving epoch'));
});

await run();

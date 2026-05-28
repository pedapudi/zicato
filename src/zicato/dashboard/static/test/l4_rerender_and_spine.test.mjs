// test/l4_rerender_and_spine.test.mjs — task #195 split A.
//
// Two L1/L4 bugs found during live use are pinned here so they cannot
// regress silently:
//
//   (1) The L4 compare-to <select> picker closed its native dropdown
//       within ~1 second of being clicked. Root cause: the SSE
//       heartbeat ticks once per second and emits state:changed; the
//       view spine called renderPhase0Run on every emit, which fully
//       rebuilt the transcript card and replaced the <select> node.
//       Fix: per-card digest-gating in views/phase0_run.js, plus a
//       module-scoped picker reference so the <select> survives across
//       renders when its option list has not changed.
//
//   (2) The L1 generation-spine layout had connector arrows that did
//       not line up with the dots because columns carrying rejected-
//       challenger branch chips pushed their own spine-node below the
//       branches while columns with no branches kept their node at the
//       top. Fix: align-items: flex-end on .spine-row plus
//       justify-content: flex-end on .spine-col so every spine-node
//       bottom-aligns, putting every dot at the same Y. (CSS-only —
//       the layout invariants here pin the JS-side structure the CSS
//       hangs off.)

import { installDom, test, run, assert } from './harness.mjs';

installDom();

const { state } = await import('../js/core/state.js');
const runV = await import('../js/views/phase0_run.js');
const spine = await import('../js/components/spine.js');

function installNode(id, tag = 'div') {
  // The harness's getElementById walks both the registry AND the live
  // tree, so a stale node hangs around between tests if we do not
  // remove it first. Mirrors loading_states.test.mjs.
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

function installRunSlots() {
  installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  installNode('phase0-run-events');
}

function mockFetch(handler) {
  const original = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const body = handler(url);
    return {
      ok: true, status: 200, headers: new Map(),
      json: async () => body, text: async () => JSON.stringify(body),
    };
  };
  return () => { globalThis.fetch = original; };
}

function seedLineageThreeGens() {
  // Minimal sibling-picker fodder: focused entry on v3, two siblings.
  state.lineage = {
    generations: [
      { generation_id: 'v0', epoch_id: 'e0', parent_generation_id: null,
        promoted: true, created_at: '2026-05-20T00:00:00Z' },
      { generation_id: 'v1', epoch_id: 'e0', parent_generation_id: 'v0',
        promoted: false, created_at: '2026-05-20T00:10:00Z' },
      { generation_id: 'v3', epoch_id: 'e0', parent_generation_id: 'v1',
        promoted: true, created_at: '2026-05-20T00:30:00Z' },
    ],
    experiments: [],
  };
}

const FOCUSED_TRANSCRIPT = {
  epoch_id: 'e0', generation_id: 'v3', entry_id: 'sample_entry',
  run_id: 'r-focused',
  event_count: 12,
  complete: true,
  turns: [
    {
      seq: 1, ts: '2026-05-20T02:00:01Z', agent: '', role: 'user',
      kind: 'run_started', text: 'Make a q3 outline.',
      tool_calls: [], tool_results: [], run_id: 'r-focused', run_index: 1,
    },
  ],
  annotations: [],
};

function _baseFetchHandler(url) {
  if (url.includes('/transcript')) return FOCUSED_TRANSCRIPT;
  if (url.includes('/expectations')) return { outcomes: [] };
  if (url.includes('/header')) {
    return {
      epoch_id: 'e0', generation_id: 'v3', entry_id: 'sample_entry',
      drift_loss: null, pass_fail: null, runtime_ms: null,
      tokens_spent: null, output_chars: null, turns_completed: null,
      plan_revisions: null, wall_clock_budget_exceeded: null, run_id: null,
    };
  }
  return { run_id: null, judges: [] };
}

// ===================================================================
// (1) Compare-picker stability — heartbeat must NOT close the dropdown
// ===================================================================

// Walk the L4 transcript card looking for the <select> picker. The
// harness's querySelector is attribute-only so we cannot say
// `select[class=mono]`; do the walk by hand.
function findPicker(root) {
  const walk = (n) => {
    if (n && n.localName === 'select') return n;
    const kids = (n && n.children) || [];
    for (const k of kids) { const r = walk(k); if (r) return r; }
    return null;
  };
  return walk(root);
}

test('L4 compare picker <select> survives a second render with the same compare-gen', async () => {
  installRunSlots();
  seedLineageThreeGens();
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  state.heartbeat = null;
  const restore = mockFetch(_baseFetchHandler);
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    // Drain the fetch microtasks twice so the cache settles and the
    // follow-up render paints with real transcript data.
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    const tNode = document.getElementById('phase0-run-transcript');
    const picker1 = findPicker(tNode);
    assert(picker1 !== null, 'picker <select> must be present after first paint');

    // Second render with identical inputs — the picker node reference
    // must NOT change. Replacing the <select> would close any native
    // dropdown the user has open.
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    const picker2 = findPicker(tNode);
    assert(picker2 === picker1,
      'picker <select> must be the SAME node across two identical renders');
  } finally {
    restore();
  }
});

test('L4 heartbeat-only state change does NOT re-render the transcript card', async () => {
  installRunSlots();
  seedLineageThreeGens();
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  state.heartbeat = null;
  const restore = mockFetch(_baseFetchHandler);
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    const tNode = document.getElementById('phase0-run-transcript');
    const transcriptCardBefore = tNode.firstChild;
    const picker1 = findPicker(tNode);
    assert(picker1 !== null, 'picker must be present before heartbeat tick');

    // Capture the digest the renderer believes it owns, then change
    // ONLY heartbeat fields and re-render. If digest-gating is
    // correct the transcript card is untouched.
    const digestBefore = runV.runViewDigest({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });

    // Heartbeat ticks every second — only its timestamp churns. None
    // of the transcript card's inputs depend on that timestamp.
    state.heartbeat = {
      epoch_id: 'e0', generation_id: 'v3',
      last_heartbeat: '2026-05-20T03:00:00Z',
    };
    const digestAfter = runV.runViewDigest({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    assert(
      JSON.stringify(digestBefore.transcript)
        === JSON.stringify(digestAfter.transcript),
      'transcript digest must be insensitive to heartbeat-only changes; '
        + 'before=' + JSON.stringify(digestBefore.transcript)
        + ' after=' + JSON.stringify(digestAfter.transcript),
    );

    // Now drive a render — the transcript card MUST NOT be rebuilt,
    // so its top-level child element is the SAME reference and the
    // picker survives.
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    const transcriptCardAfter = tNode.firstChild;
    assert(transcriptCardAfter === transcriptCardBefore,
      'transcript card root must NOT be rebuilt on a heartbeat-only tick');
    const picker2 = findPicker(tNode);
    assert(picker2 === picker1,
      'picker <select> must be untouched across a heartbeat-only tick');
  } finally {
    restore();
  }
});

// ===================================================================
// (2) Spine layout — bottom-aligned dots + branch chips per parent
// ===================================================================

// Walk a node's descendants and return every element with the given
// class. The harness's querySelectorAll only matches attributes, not
// "class contains X"; walk manually.
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

test('renderSpine — tour fixture: v0+v1+v3+v8 spine columns; v3 carries v4..v7 branches', () => {
  // Mirror the live dogfood epoch — v0 baseline → v1 (promoted, v2
  // rejected) → v3 (promoted, v4..v7 rejected) → v8 LIVE. Pins the
  // column count + branch-chip count per parent column so the spine
  // never silently collapses or mis-buckets a rejected child.
  const node = spine.renderSpine({
    nodes: [
      { id: 'v0', scalar: null, promoted: true, parent_id: null },
      { id: 'v1', scalar: null, promoted: true, parent_id: 'v0' },
      { id: 'v2', scalar: null, promoted: false, parent_id: 'v1' },
      { id: 'v3', scalar: null, promoted: true, parent_id: 'v1' },
      { id: 'v4', scalar: null, promoted: false, parent_id: 'v3' },
      { id: 'v5', scalar: null, promoted: false, parent_id: 'v3' },
      { id: 'v6', scalar: null, promoted: false, parent_id: 'v3' },
      { id: 'v7', scalar: null, promoted: false, parent_id: 'v3' },
      { id: 'v8', scalar: null, promoted: false, live: true, parent_id: 'v3' },
    ],
  });
  // Promoted + live nodes form the spine row: v0, v1, v3, v8 LIVE.
  const cols = descendantsWithClass(node, 'spine-col');
  assert(cols.length === 4,
    'tour fixture must paint 4 spine columns (v0, v1, v3, v8 LIVE); '
      + 'got ' + cols.length);

  // Identify each column by the spine-node-label inside it.
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
    'each promoted spine column must be keyed by its own id');
  // The LIVE node label includes a leading "LIVE " tag prefix.
  assert(Object.keys(byId).some((k) => k.includes('v8')),
    'LIVE node v8 must render as a spine column');

  const v0Branches = columnBranchIds(byId.v0);
  const v1Branches = columnBranchIds(byId.v1);
  const v3Branches = columnBranchIds(byId.v3);
  assert(v0Branches.length === 0,
    'v0 column carries no branches (baseline); got ' + v0Branches.join(','));
  assert(v1Branches.length === 1 && v1Branches[0] === 'v2',
    'v1 column carries exactly v2; got ' + v1Branches.join(','));
  assert(v3Branches.length === 4,
    'v3 column must carry 4 branch chips (v4..v7); got '
      + v3Branches.length + ': ' + v3Branches.join(','));
  for (const want of ['v4', 'v5', 'v6', 'v7']) {
    assert(v3Branches.includes(want),
      'v3 column must include branch ' + want + '; got ' + v3Branches.join(','));
  }

  // Branch chips must sort by natural generation order (v4..v7), not
  // alphabetic — the renderer's _byGenId guarantees this.
  assert(v3Branches.join(',') === 'v4,v5,v6,v7',
    'v3 branches must order naturally; got ' + v3Branches.join(','));
});

test('renderSpine — a rejected node with no resolvable parent falls back to the footer footnote', () => {
  const node = spine.renderSpine({
    nodes: [
      { id: 'v0', scalar: null, promoted: true, parent_id: null },
      { id: 'v1', scalar: null, promoted: true, parent_id: 'v0' },
      // ``orphan`` has no parent on the spine — must NOT be silently
      // dropped, and must NOT be attached to v0 or v1 as a branch.
      { id: 'orphan', scalar: 0.42, promoted: false, parent_id: 'v_unknown' },
    ],
  });
  const text = node.textContent;
  assert(text.includes('rejected (no parent)'),
    'orphan-rejected footer label must render; got: ' + text.slice(0, 300));
  assert(text.includes('orphan'),
    'orphan id must render in the footer chip');
  // The orphan must NOT be attached as a branch on v0 or v1.
  const branchIds = descendantsWithClass(node, 'spine-branch-id')
    .map((n) => n.textContent);
  assert(!branchIds.includes('orphan'),
    'orphan must NOT render as a spine-branch chip; got branches: '
      + branchIds.join(','));
  // The 0.42 scalar must surface alongside the orphan id in the
  // footer chip.
  assert(text.includes('0.42') || text.includes('0.420'),
    'orphan scalar must render in the footer chip; got: ' + text.slice(0, 300));
});

await run();

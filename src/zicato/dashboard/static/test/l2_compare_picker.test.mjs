// test/l2_compare_picker.test.mjs — L2 generation page compare picker
// (Task #205).
//
// Pins the always-on compare-mode behaviour the L2 page grows from
// Task #205:
//   1. Picker enumerates every OTHER generation in the focused epoch.
//   2. Picker change drives a compare fetch (per-entry + per-judge).
//   3. Compare mode renders two hero strips (focused + compared).
//   4. Compare mode per-entry table carries focused + compared columns.
//   5. Clearing the picker (selecting "off") returns to single mode.
//   6. SSE heartbeat-only tick does NOT rebuild the hero card, so the
//      <select> dropdown survives the tick.

import { installDom, test, run, assert } from './harness.mjs';

installDom();

const router = await import('../js/views/phase0_router.js');
void router;

const { state } = await import('../js/core/state.js');
const generation = await import('../js/views/phase0_generation.js');

function installNode(id, tag = 'div') {
  // Mirror loading_states.test.mjs — strip any stale node first so
  // installing the same id between tests does not leave two on the
  // tree (the harness getElementById walks both the registry and the
  // live tree).
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

function installL2Slots() {
  installNode('phase0-gen-compare');
  installNode('phase0-gen-hypothesis');
  installNode('phase0-gen-patches');
  installNode('phase0-gen-entries');
  installNode('phase0-gen-judges');
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

// Fixture epoch with three generations on the same epoch — v1 (seed),
// v2 (rejected), v3 (promoted, focused). The picker should list v1 + v2
// when v3 is focused, and v2 + v3 when v1 is focused, etc.
const SEED_EXP = {
  generation_id: 'v1',
  parent_generation_id: null,
  hypothesis: {
    core_idea: 'Baseline researcher.',
    why: 'Establish the seed.',
  },
  outcome: {
    tournament_decision: 'promoted',
    scalar_score_delta: 0,
    pass_rate_delta: 0,
    drift_loss_delta: 0,
    rejection_reason: '',
    ran_at: '2026-05-20T00:00:00+00:00',
    scalar_score: 47.58,
  },
  patches: {},
};

const REJECTED_EXP = {
  generation_id: 'v2',
  parent_generation_id: 'v1',
  hypothesis: {
    core_idea: "Tighten the researcher's topical constraints.",
    why: 'Off-topic drift is a primary driver.',
    risks: 'Increasing topicality might reduce breadth.',
    expected_pass_rate_delta: '+0.05 to +0.15',
    expected_drift_movements: [
      { kind: 'off_topic', direction: 'decrease', magnitude: 'medium' },
    ],
  },
  outcome: {
    tournament_decision: 'rejected',
    scalar_score_delta: 10.123,
    pass_rate_delta: 0.167,
    drift_loss_delta: 9.571,
    rejection_reason:
      'challenger regressed: loss rose by 10.122619 '
      + '(champion 47.580429 -> challenger 57.703048); '
      + 'a promotion needs the loss to drop by at least 0.010000',
    ran_at: '2026-05-20T01:25:49+00:00',
    scalar_score: 57.70,
  },
  patches: {
    researcher_instruction: {
      mutation_id: 'researcher_instruction',
      op: 'replace',
      rationale: 'Adding explicit constraints against tangential content.',
    },
  },
};

const PROMOTED_EXP = {
  generation_id: 'v3',
  parent_generation_id: 'v1',
  hypothesis: {
    core_idea: 'Inject topicality constraints into the researcher prompt.',
    why: 'Off-topic drift dominates telemetry.',
    risks: 'Tightening scope might reduce creativity.',
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
    scalar_score: 23.25,
  },
  patches: {
    researcher_instruction: {
      mutation_id: 'researcher_instruction',
      op: 'replace',
      rationale: 'Tighten topicality constraints to reduce off_topic drift.',
    },
  },
};

const PER_ENTRY_V3 = {
  epoch_id: 'e0', generation_id: 'v3', tournament_id: 'e0:v1->v3',
  entries: [
    { entry_id: 'alpha', run_id: 'r3a', drift_loss: 12.5, pass_fail: 1 },
    { entry_id: 'beta',  run_id: 'r3b', drift_loss: 80.5, pass_fail: 0 },
  ],
};
const PER_ENTRY_V2 = {
  epoch_id: 'e0', generation_id: 'v2', tournament_id: 'e0:v1->v2',
  entries: [
    { entry_id: 'alpha', run_id: 'r2a', drift_loss: 18.7, pass_fail: 0 },
    { entry_id: 'beta',  run_id: 'r2b', drift_loss: 95.0, pass_fail: 0 },
  ],
};
const PER_ENTRY_V1 = {
  epoch_id: 'e0', generation_id: 'v1', tournament_id: null,
  entries: [
    { entry_id: 'alpha', run_id: 'r1a', drift_loss: 15.0, pass_fail: 1 },
    { entry_id: 'beta',  run_id: 'r1b', drift_loss: 92.5, pass_fail: 0 },
  ],
};
const PER_JUDGE_V3 = {
  epoch_id: 'e0', generation_id: 'v3',
  judges: [
    { judge_name: 'topicality', weighted_loss: 1.0, raw_loss: 1.0,
      weight: 1.0, run_count: 2 },
  ],
};
const PER_JUDGE_V2 = {
  epoch_id: 'e0', generation_id: 'v2',
  judges: [
    { judge_name: 'topicality', weighted_loss: 4.5, raw_loss: 4.5,
      weight: 1.0, run_count: 2 },
  ],
};
const PER_JUDGE_V1 = {
  epoch_id: 'e0', generation_id: 'v1',
  judges: [
    { judge_name: 'topicality', weighted_loss: 3.0, raw_loss: 3.0,
      weight: 1.0, run_count: 2 },
  ],
};

function seedEpochDef() {
  state.epochDef = {
    epoch_id: 'e0',
    experiments: [SEED_EXP, REJECTED_EXP, PROMOTED_EXP],
  };
}
function seedLineage() {
  state.lineage = {
    generations: [
      { generation_id: 'v1', epoch_id: 'e0', parent_generation_id: null,
        promoted: true, created_at: '2026-05-20T00:00:00Z' },
      { generation_id: 'v2', epoch_id: 'e0', parent_generation_id: 'v1',
        promoted: false, created_at: '2026-05-20T01:25:00Z' },
      { generation_id: 'v3', epoch_id: 'e0', parent_generation_id: 'v1',
        promoted: true, created_at: '2026-05-20T02:06:00Z' },
    ],
    experiments: [],
  };
}

function _baseFetchHandler(url) {
  if (url.includes('/per-entry')) {
    if (url.includes('/v3/per-entry')) return PER_ENTRY_V3;
    if (url.includes('/v2/per-entry')) return PER_ENTRY_V2;
    if (url.includes('/v1/per-entry')) return PER_ENTRY_V1;
    return { entries: [] };
  }
  if (url.includes('/per-judge')) {
    if (url.includes('/v3/per-judge')) return PER_JUDGE_V3;
    if (url.includes('/v2/per-judge')) return PER_JUDGE_V2;
    if (url.includes('/v1/per-judge')) return PER_JUDGE_V1;
    return { judges: [] };
  }
  return {};
}

// Walk a subtree looking for the first <select> — the harness's
// querySelector is attribute-only so we hand-roll.
function findPicker(root) {
  const walk = (n) => {
    if (n && n.localName === 'select') return n;
    const kids = (n && n.children) || [];
    for (const k of kids) { const r = walk(k); if (r) return r; }
    return null;
  };
  return walk(root);
}

// --- 1. Picker lists every OTHER generation in the focused epoch -----

test('L2 compare picker lists every sibling generation in the focused epoch', async () => {
  installL2Slots();
  generation.resetGenerationCaches();
  seedEpochDef();
  seedLineage();
  const restore = mockFetch(_baseFetchHandler);
  try {
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });

    const heroNode = document.getElementById('phase0-gen-compare');
    const picker = findPicker(heroNode);
    assert(picker !== null, 'compare picker <select> must be present on L2');
    const opts = picker.children.filter((c) => c.localName === 'option');
    // 1 "off" option + 2 sibling generations (v1, v2) = 3 total. v3
    // (the focused gen) must be filtered out.
    assert(opts.length === 3,
      `picker must have 3 options (off + v1 + v2); got ${opts.length}`);
    const values = opts.map((o) => o.getAttribute('value'));
    assert(values.includes('') && values.includes('v1') && values.includes('v2'),
      `picker values must include '', v1, v2; got ${JSON.stringify(values)}`);
    assert(!values.includes('v3'),
      `picker must NOT include the focused gen v3; got ${JSON.stringify(values)}`);
  } finally {
    restore();
  }
});

// --- 2. Selecting a value triggers a compare fetch -------------------

test('L2 selecting a compare target triggers per-entry + per-judge fetches', async () => {
  installL2Slots();
  generation.resetGenerationCaches();
  seedEpochDef();
  seedLineage();
  let v2EntryHits = 0;
  let v2JudgeHits = 0;
  const restore = mockFetch((url) => {
    if (url.includes('/v2/per-entry')) { v2EntryHits += 1; return PER_ENTRY_V2; }
    if (url.includes('/v2/per-judge')) { v2JudgeHits += 1; return PER_JUDGE_V2; }
    return _baseFetchHandler(url);
  });
  try {
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });

    // No compare target yet — v2's endpoints must not have been hit.
    assert(v2EntryHits === 0,
      `v2/per-entry must NOT be fetched in single mode; hits=${v2EntryHits}`);
    assert(v2JudgeHits === 0,
      `v2/per-judge must NOT be fetched in single mode; hits=${v2JudgeHits}`);

    // Set the compare target — both endpoints must land.
    generation.setCompareGenFor('e0', 'v3', 'v2');
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    assert(v2EntryHits >= 1,
      `v2/per-entry must be fetched after compare set; hits=${v2EntryHits}`);
    assert(v2JudgeHits >= 1,
      `v2/per-judge must be fetched after compare set; hits=${v2JudgeHits}`);
  } finally {
    restore();
    generation.setCompareGenFor('e0', 'v3', null);
  }
});

// --- 3. Side-by-side hero — two strips + a "vs" column ---------------

test('L2 compare mode renders two hero strips (focused + compared)', async () => {
  installL2Slots();
  generation.resetGenerationCaches();
  seedEpochDef();
  seedLineage();
  generation.setCompareGenFor('e0', 'v3', 'v2');
  const restore = mockFetch(_baseFetchHandler);
  try {
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });

    const heroNode = document.getElementById('phase0-gen-compare');
    const text = heroNode.textContent;
    // Both gens' titles must surface — each side carries its own
    // "Generation v?" title row.
    assert(text.includes('Generation v3'),
      `focused-side title must render; got: ${text.slice(0, 400)}`);
    assert(text.includes('Generation v2'),
      `compared-side title must render; got: ${text.slice(0, 400)}`);
    // Both decision pills must appear — v3 promoted, v2 rejected.
    assert(text.includes('PROMOTED'),
      `focused-side PROMOTED pill must render; got: ${text.slice(0, 600)}`);
    assert(text.includes('REJECTED'),
      `compared-side REJECTED pill must render; got: ${text.slice(0, 600)}`);
    // The vs column header surfaces.
    assert(text.includes('vs'),
      `hero compare must surface a "vs" column; got: ${text.slice(0, 600)}`);
    // Two .gen-compare-side wrappers — one focused, one compared.
    let sideCount = 0;
    const walk = (n) => {
      if (!n || n.nodeType !== 1) return;
      if (n.classList && n.classList.contains('gen-compare-side')) sideCount += 1;
      for (const c of n.children) walk(c);
    };
    walk(heroNode);
    assert(sideCount === 2,
      `compare hero must contain 2 .gen-compare-side wrappers; got ${sideCount}`);
  } finally {
    restore();
    generation.setCompareGenFor('e0', 'v3', null);
  }
});

// --- 4. Side-by-side per-entry table — both sides' values ------------

test('L2 compare mode per-entry table carries focused + compared drift columns', async () => {
  installL2Slots();
  generation.resetGenerationCaches();
  seedEpochDef();
  seedLineage();
  generation.setCompareGenFor('e0', 'v3', 'v2');
  const restore = mockFetch(_baseFetchHandler);
  try {
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });

    const entries = document.getElementById('phase0-gen-entries');
    const text = entries.textContent;
    // The "vs <champion>" column header should now read "vs v2" so the
    // operator is not misled into thinking the column is still vs the
    // lineage parent.
    assert(text.includes('vs v2'),
      `per-entry header must read "vs v2" in compare mode; got: ${text.slice(0, 600)}`);
    // Both sides' drift values must surface for the alpha entry:
    //   focused v3 → 12.500
    //   compared v2 → 18.700
    //   delta 12.5 - 18.7 = -6.200 (better)
    assert(text.includes('12.500'),
      `focused-side alpha drift 12.500 must render; got: ${text.slice(0, 600)}`);
    assert(text.includes('18.700'),
      `compared-side alpha drift 18.700 must render; got: ${text.slice(0, 600)}`);
    assert(text.includes('-6.200'),
      `Δ vs v2 for alpha must read -6.200; got: ${text.slice(0, 600)}`);
    // The "focused pass/fail" and "compared pass/fail" headers must
    // both render in compare mode.
    assert(text.includes('focused pass/fail') && text.includes('compared pass/fail'),
      `compare-mode pass/fail columns must render; got: ${text.slice(0, 600)}`);
  } finally {
    restore();
    generation.setCompareGenFor('e0', 'v3', null);
  }
});

// --- 5. Clearing the picker (selecting "off") returns to single mode -

test('L2 clearing the compare picker returns to single mode', async () => {
  installL2Slots();
  generation.resetGenerationCaches();
  seedEpochDef();
  seedLineage();
  generation.setCompareGenFor('e0', 'v3', 'v2');
  const restore = mockFetch(_baseFetchHandler);
  try {
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });

    const before = document.getElementById('phase0-gen-compare').textContent;
    assert(before.includes('Generation v2'),
      `compared-side body must render initially; got: ${before.slice(0, 400)}`);

    // Clear the picker — back to single mode.
    generation.setCompareGenFor('e0', 'v3', null);
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });

    const heroNode = document.getElementById('phase0-gen-compare');
    const after = heroNode.textContent;
    assert(!after.includes('Generation v2'),
      `compared-side body must NOT render after picker clear; got: ${after.slice(0, 400)}`);
    assert(after.includes('Generation v3'),
      `focused-side body must still render; got: ${after.slice(0, 400)}`);
    // Zero .gen-compare-side wrappers in single mode.
    let sideCount = 0;
    const walk = (n) => {
      if (!n || n.nodeType !== 1) return;
      if (n.classList && n.classList.contains('gen-compare-side')) sideCount += 1;
      for (const c of n.children) walk(c);
    };
    walk(heroNode);
    assert(sideCount === 0,
      `single mode must have 0 .gen-compare-side wrappers; got ${sideCount}`);
    // Per-entry header must revert from "vs v2" to "vs v1" (lineage parent).
    const entriesText = document.getElementById('phase0-gen-entries').textContent;
    assert(entriesText.includes('vs v1'),
      `per-entry header must revert to "vs v1" (lineage parent); got: ${entriesText.slice(0, 600)}`);
    assert(!entriesText.includes('vs v2'),
      `per-entry header must NOT read "vs v2" in single mode; got: ${entriesText.slice(0, 600)}`);
  } finally {
    restore();
  }
});

// --- 6. SSE heartbeat does NOT close the picker dropdown -------------

test('L2 heartbeat-only state change does NOT rebuild the hero card', async () => {
  installL2Slots();
  generation.resetGenerationCaches();
  seedEpochDef();
  seedLineage();
  state.heartbeat = null;
  const restore = mockFetch(_baseFetchHandler);
  try {
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    const heroNode = document.getElementById('phase0-gen-compare');
    const cardBefore = heroNode.firstChild;
    const picker1 = findPicker(heroNode);
    assert(picker1 !== null, 'picker must be present before heartbeat tick');

    // Capture the hero digest, mutate ONLY state.heartbeat, re-render.
    // If the digest is heartbeat-insensitive, the hero card is NOT
    // rebuilt and the <select> survives.
    const digestBefore = generation.generationViewDigest({
      epochId: 'e0', generationId: 'v3',
    });
    state.heartbeat = {
      epoch_id: 'e0', generation_id: 'v3',
      last_heartbeat: '2026-05-20T03:00:00Z',
    };
    const digestAfter = generation.generationViewDigest({
      epochId: 'e0', generationId: 'v3',
    });
    assert(
      JSON.stringify(digestBefore.hero) === JSON.stringify(digestAfter.hero),
      'hero digest must be insensitive to heartbeat-only changes; '
        + 'before=' + JSON.stringify(digestBefore.hero)
        + ' after=' + JSON.stringify(digestAfter.hero),
    );

    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    const cardAfter = heroNode.firstChild;
    assert(cardAfter === cardBefore,
      'hero card root must NOT be rebuilt on a heartbeat-only tick');
    const picker2 = findPicker(heroNode);
    assert(picker2 === picker1,
      'picker <select> must be untouched across a heartbeat-only tick');
  } finally {
    restore();
    state.heartbeat = null;
  }
});

// --- 7. Picker <select> survives identical re-renders (stable ref) ---

test('L2 picker <select> survives a second render with identical inputs', async () => {
  installL2Slots();
  generation.resetGenerationCaches();
  seedEpochDef();
  seedLineage();
  const restore = mockFetch(_baseFetchHandler);
  try {
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    const heroNode = document.getElementById('phase0-gen-compare');
    const picker1 = findPicker(heroNode);
    assert(picker1 !== null, 'picker must be present after first paint');

    // Force a fresh render via the picker-change path so the digest
    // gate fires (otherwise the digest sees no change and the hero
    // card is not rebuilt at all — also a valid path that proves the
    // picker survives).
    generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v3' });
    const picker2 = findPicker(heroNode);
    assert(picker2 === picker1,
      'picker <select> must be the SAME node across two identical renders');
  } finally {
    restore();
  }
});

await run();

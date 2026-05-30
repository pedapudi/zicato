// test/l4_cold_deeplink_rerender.test.mjs — task #207.
//
// Bug: when the user cold-deep-links to /#/run/... the L4 view renders
// BEFORE the SSE heartbeat hydrates state.heartbeat.harmonograf_url. At
// first render the URL is null, so the harmonograf deep-link does not
// paint. When SSE later delivers the URL, state:changed fires and the
// renderer runs again — but the per-card digest gate in renderPhase0Run
// (task #195) compared a fingerprint that EXCLUDED harmonograf_url, so
// the header card was treated as unchanged and never repainted. The
// link was therefore missing until the user manually navigated away
// and back.
//
// Fix: fold state.heartbeat.harmonograf_url into the header slice of
// runViewDigest so a URL-flip changes the digest and the header
// repaints on the next render tick.
//
// The three tests below pin:
//   (1) The digest changes when the URL flips from null → non-empty.
//   (2) After the digest change, the next render rebuilds the header
//       card (the slot's child reference changes).
//   (3) The harmonograf link is present in the header DOM once the URL
//       has hydrated, even though the very first render happened with
//       a null URL.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { state } = await import('../js/core/state.js');
const runV = await import('../js/views/phase0_run.js');

function installNode(id, tag = 'div') {
  // Drop any stale node from a prior test (the harness's
  // getElementById walks both the registry AND the live tree).
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

// Walk for the harmonograf deep-link <a> in the header slot. The
// harness's querySelector is attribute-only, so we walk by hand —
// mirrors the helper in harmonograf_links.test.mjs.
function findHarmonografLink(node) {
  const stack = [node];
  while (stack.length > 0) {
    const cur = stack.pop();
    if (!cur || !cur.childNodes) continue;
    for (const child of cur.childNodes) {
      if (!child) continue;
      const cls = child._attrs && child._attrs.class;
      if (child.tagName === 'A' && cls
          && cls.split(/\s+/).includes('harmonograf-link')) {
        return child;
      }
      stack.push(child);
    }
  }
  return null;
}

const HEADER_PAYLOAD = {
  epoch_id: '2026-05-20_presn', generation_id: 'v3',
  entry_id: 'every_expectation_kind_demo',
  drift_loss: 0.5, pass_fail: true,
  runtime_ms: 1000, tokens_spent: 100, output_chars: 50,
  turns_completed: 4, plan_revisions: 0,
  wall_clock_budget_exceeded: false,
  run_id: 'run_alpha',
  adk_session_id: 'adk-session-cold-deeplink',
};

function _baseFetchHandler(url) {
  if (url.includes('/header')) return HEADER_PAYLOAD;
  if (url.includes('/expectations')) return { outcomes: [] };
  if (url.includes('/transcript')) {
    return {
      epoch_id: '2026-05-20_presn', generation_id: 'v3',
      entry_id: 'every_expectation_kind_demo',
      run_id: null, turns: [], annotations: [],
      event_count: 0, complete: false,
    };
  }
  return { run_id: null, judges: [] };
}

// ===================================================================
// (1) Digest sensitivity to harmonograf_url
// ===================================================================

test('runViewDigest.header changes when heartbeat.harmonograf_url flips from null → non-empty', () => {
  installRunSlots();
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  // Cold deep-link condition: no heartbeat at all yet.
  state.heartbeat = null;
  const params = {
    epochId: '2026-05-20_presn', generationId: 'v3',
    entryId: 'every_expectation_kind_demo',
  };
  const digestBefore = runV.runViewDigest(params);

  // SSE delivers a heartbeat carrying harmonograf_url for the first
  // time. ONLY the URL changes — same epoch / generation / no run.
  state.heartbeat = {
    epoch_id: '2026-05-20_presn', generation_id: 'v3',
    harmonograf_url: 'https://harmonograf.example.com',
  };
  const digestAfter = runV.runViewDigest(params);

  // The header slice MUST change — that is the whole point of the
  // fix. The fingerprint must encode the URL so the gate fires.
  assert(
    JSON.stringify(digestBefore.header) !== JSON.stringify(digestAfter.header),
    'header digest must change when harmonograf_url hydrates from null → set; '
      + 'before=' + JSON.stringify(digestBefore.header)
      + ' after=' + JSON.stringify(digestAfter.header),
  );

  // And the change is specifically the harmonograf URL field — the
  // rest of the slice stays put so the digest is not noisy.
  assertEqual(digestBefore.header.headerSig, digestAfter.header.headerSig,
    'header payload signature must NOT churn when only the URL flipped');
  assertEqual(digestBefore.header.runStatus, digestAfter.header.runStatus,
    'run status must NOT churn when only the URL flipped');

  state.heartbeat = null;
});

// ===================================================================
// (2) After digest change, the header card is rebuilt on next render
// ===================================================================

test('L4 header card repaints on the render that follows a harmonograf_url hydration', async () => {
  installRunSlots();
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  // Cold deep-link: heartbeat has NOT arrived yet.
  state.heartbeat = null;
  const restore = mockFetch(_baseFetchHandler);
  try {
    const params = {
      epochId: '2026-05-20_presn', generationId: 'v3',
      entryId: 'every_expectation_kind_demo',
    };
    runV.renderPhase0Run(params);
    // Drain the fetch microtasks twice so the header cache settles
    // and the follow-up render paints with real header data.
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run(params);
    const slot = document.getElementById('phase0-run-header');
    const headerCardBefore = slot.firstChild;
    assert(headerCardBefore != null,
      'header card must paint on first render even without a heartbeat');

    // Now the SSE heartbeat lands carrying harmonograf_url. The
    // bus emits state:changed → the app calls renderPhase0Run again
    // with the same route params. The header digest must have
    // changed, so the card MUST be rebuilt — the slot's child is
    // a fresh element.
    state.heartbeat = {
      epoch_id: '2026-05-20_presn', generation_id: 'v3',
      harmonograf_url: 'https://harmonograf.example.com',
    };
    runV.renderPhase0Run(params);
    const headerCardAfter = slot.firstChild;
    assert(headerCardAfter !== headerCardBefore,
      'header card root MUST be rebuilt after harmonograf_url hydrates; '
        + 'digest must have flipped to force the repaint');
  } finally {
    restore();
    state.heartbeat = null;
  }
});

// ===================================================================
// (3) End-to-end: link is in the DOM after the deferred hydration
// ===================================================================

test('L4 harmonograf link appears in DOM after SSE hydrates harmonograf_url on a cold deep-link', async () => {
  installRunSlots();
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  // Cold deep-link to /#/run/<epoch>/v3/<entry>: no heartbeat at all.
  state.heartbeat = null;
  const restore = mockFetch(_baseFetchHandler);
  try {
    const params = {
      epochId: '2026-05-20_presn', generationId: 'v3',
      entryId: 'every_expectation_kind_demo',
    };
    runV.renderPhase0Run(params);
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run(params);

    // First render finished before the URL hydrated — assert the
    // link is NOT yet in the DOM. (This pins the bug condition;
    // without it, a regression that always renders the link would
    // pass the "appears after" check trivially.)
    const slot = document.getElementById('phase0-run-header');
    assertEqual(findHarmonografLink(slot), null,
      'before hydration, harmonograf_url is null so the link must NOT render');

    // SSE delivers the URL AND the live tournament (the run is in
    // flight — harmonograf's server is up). State changes. App scheduler
    // calls renderPhase0Run again with the same route params.
    state.activeTournament = { champion: 'v2', challenger: 'v3' };
    state.heartbeat = {
      epoch_id: '2026-05-20_presn', generation_id: 'v3',
      harmonograf_url: 'https://harmonograf.example.com',
    };
    runV.renderPhase0Run(params);

    const link = findHarmonografLink(slot);
    assert(link != null,
      'harmonograf link MUST appear in the header after the URL hydrates '
        + 'on a cold deep-link (task #207 fix)');
    // And the href is the correct session-scoped url derived from
    // the cached header payload's adk_session_id — proving the link
    // is not just a stub.
    assertEqual(
      link.getAttribute('href'),
      'https://harmonograf.example.com/#/session/adk-session-cold-deeplink',
      'hydrated link href must encode the adk_session_id under /#/session/');
    assertEqual(link.getAttribute('target'), '_blank',
      'hydrated link must open in a new tab');
    assertEqual(link.getAttribute('rel'), 'noopener',
      'hydrated link must carry rel=noopener');
  } finally {
    restore();
    state.heartbeat = null;
    state.activeTournament = null;
  }
});

await run();

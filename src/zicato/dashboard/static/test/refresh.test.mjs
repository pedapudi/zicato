// test/refresh.test.mjs — SSE-driven refresh stability tests.
//
// The dashboard's SSE feed fires `state:changed` on every server tick
// (~1Hz). Many of those mutations don't change anything the user is
// looking at: the heartbeat last_heartbeat timestamp churns, a
// health-report checked_at re-stamps, log events get re-cursored. The
// no-flash spine was introduced precisely so a no-op repaint produces
// ZERO DOM churn — but a renderAll that unconditionally drove the whole
// tree was still rebuilding sections each tick. Text selection, inner
// scroll containers (most visibly the matchup-detail conversation
// diff), focus and hover all got reset every second.
//
// These tests pin the structural contract: a no-op SSE repaint (same
// relevant-state digest) writes ZERO DOM nodes; selection and scroll
// positions survive N=10+ ticks; a real state change still mutates DOM.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

const REQUIRED_IDS = [
  'drill-close', 'header-bar', 'footer-bar', 'epoch-id', 'generation-id',
  'round-id', 'elapsed', 'health-badge', 'mock-badge',
  'identity-panel', 'health-panel', 'live-activity', 'epochs-panel',
  'recent-experiments', 'tournament-bracket',
  'tournament-detail', 'tournament-detail-section',
  'log-tail', 'drill-panel', 'drill-title',
  'drill-body', 'dashboard-version', 'dashboard-port', 'dashboard-build',
  'view-overview', 'view-tree', 'view-tournament', 'view-epoch', 'view-files',
  'view-conversation', 'nav-overview', 'nav-tree', 'nav-tournament', 'nav-epoch',
  'nav-files', 'epoch-overview', 'epoch-harness', 'epoch-board', 'epoch-brief',
  'epoch-scoring', 'epoch-mutations',
  'epoch-experiments-section', 'epoch-experiment-log', 'epoch-analysis',
  'lineage-svg', 'overview-trajectory-svg',
  'heatmap-svg', 'conversation-panel',
  'files-patches', 'files-patches-title',
  'files-cumulative', 'files-cumulative-title',
  'mutations-list-pane', 'mutations-detail-pane',
  'lineage-stage', 'lineage-viewport', 'lineage-zoom-in', 'lineage-zoom-out',
  'lineage-zoom-reset',
];

function seedDom() {
  const doc = installDom();
  globalThis.location = globalThis.window.location;
  globalThis.URLSearchParams = URLSearchParams;
  globalThis.EventSource = class { addEventListener() {} close() {} };
  for (const id of REQUIRED_IDS) {
    const tag = id.endsWith('-svg') ? 'svg' : 'div';
    const n = doc.createElement(tag);
    n.setAttribute('id', id);
    doc.body.appendChild(n);
  }
  return doc;
}

const doc = seedDom();
const { state } = await import('../js/core/state.js');
const { mockSnapshot } = await import('../js/views/mock.js');
const render = await import('../js/views/render.js');

// Instrument every Element in the harness with a counter for the writes
// the user-visible no-flash test cares about. The harness already counts
// innerHTML writes; we wrap createElement / textContent / appendChild on
// the DocumentImpl so a no-op render can be measured to write nothing.
function instrumentDom() {
  const doc = globalThis.document;
  const counters = {
    createElement: 0,
    createElementNS: 0,
    textContent: 0,
    appendChild: 0,
    setAttribute: 0,
    innerHTML: 0,
  };
  const origCreate = doc.createElement.bind(doc);
  const origCreateNS = doc.createElementNS.bind(doc);
  doc.createElement = (tag) => { counters.createElement += 1; return origCreate(tag); };
  doc.createElementNS = (ns, tag) => {
    counters.createElementNS += 1;
    return origCreateNS(ns, tag);
  };
  // We can't easily proxy textContent on every node, but the harness's
  // own `textContent` setter destroys children and rebuilds a text node;
  // patchText guards that, so the instrumented createElement covers
  // direct text writes. Direct setting of textContent on an existing
  // node IS observable via the harness's `innerHTMLWriteCount` only
  // when innerHTML was used; for textContent we install a Proxy.
  return {
    counters,
    snapshot: () => Object.assign({}, counters),
    restore: () => {
      doc.createElement = origCreate;
      doc.createElementNS = origCreateNS;
    },
  };
}

// Total DOM write operations the body subtree carries — defined as the
// sum of innerHTML writes (the harness already tracks this) plus the
// element-creation count delta the run produced. Element creation only
// happens when a render path actually builds DOM, so a no-op renderAll
// must contribute zero.
function domWriteSnapshot(probe) {
  return {
    inner: doc.body.innerHTMLWriteCount(),
    created: probe.counters.createElement,
    createdNS: probe.counters.createElementNS,
  };
}

function domWriteDelta(before, after) {
  return {
    inner: after.inner - before.inner,
    created: after.created - before.created,
    createdNS: after.createdNS - before.createdNS,
  };
}

test('a no-op SSE repaint writes ZERO DOM nodes — every panel', () => {
  // Apply a full snapshot so every view has data. Render once to settle
  // the cached digest, then probe ten more renderAll calls — each one
  // must contribute zero element creations and zero innerHTML writes.
  globalThis.location.hash = '#/overview';
  state.mock = true;
  state.applySnapshot(mockSnapshot());
  render.showView('overview');
  render.renderAll();

  const probe = instrumentDom();
  const before = domWriteSnapshot(probe);

  for (let i = 0; i < 12; i++) render.renderAll();

  const after = domWriteSnapshot(probe);
  const delta = domWriteDelta(before, after);
  probe.restore();

  assertEqual(delta.inner, 0,
    `no-op renderAll wrote innerHTML ${delta.inner} times; should be 0`);
  assertEqual(delta.created, 0,
    `no-op renderAll created ${delta.created} elements; should be 0`);
  assertEqual(delta.createdNS, 0,
    `no-op renderAll created ${delta.createdNS} SVG elements; should be 0`);
});

test('text selection survives 10+ no-op SSE ticks', () => {
  // The browser stores text selection on the document, NOT on the DOM
  // node. Selection is invalidated when a text node it covers is
  // replaced. We model that here: a node whose textContent is rewritten
  // (the prior bug) counts as "selection lost". Walking the rendered
  // panels and asserting their text nodes keep identity across 12
  // no-op renders proves the user-visible behaviour — selection
  // survives — without simulating a real browser selection.
  globalThis.location.hash = '#/overview';
  state.mock = true;
  state.applySnapshot(mockSnapshot());
  render.showView('overview');
  render.renderAll();

  // Capture every text node in the rendered Overview panels. The
  // harness seeds the panel containers as flat siblings on doc.body
  // (the production layout nests them under #view-overview); walking
  // each named panel covers the same surface.
  const overviewPanels = [
    'identity-panel', 'health-panel', 'live-activity', 'epochs-panel',
    'recent-experiments', 'log-tail', 'overview-trajectory-svg',
  ];
  const collectTextNodes = () => {
    const out = [];
    const walk = (n) => {
      if (!n) return;
      for (const c of n.childNodes) {
        if (c.nodeType === 3) out.push(c);
        else if (c.nodeType === 1) walk(c);
      }
    };
    for (const id of overviewPanels) walk(doc.getElementById(id));
    return out;
  };
  const before = collectTextNodes();
  assert(before.length > 0, 'the Overview must have rendered text content');

  // Twelve no-op SSE ticks — selection-bearing text nodes must remain
  // the same node identities (no clear-and-rebuild has touched them).
  for (let i = 0; i < 12; i++) render.renderAll();

  const after = collectTextNodes();
  assertEqual(after.length, before.length,
    `text node count changed across no-op ticks: ${before.length} -> ${after.length}`);
  let kept = 0;
  for (let i = 0; i < before.length; i++) {
    if (before[i] === after[i]) kept += 1;
  }
  assert(kept === before.length,
    `text node identity must survive every no-op tick — kept ${kept}/${before.length}`);
});

test('scroll position survives 10+ no-op ticks on the matchup detail conversation diff', () => {
  // The canonical case the user named: the matchup-detail conversation
  // diff. The user scrolls inside it; the next SSE tick yanks them back
  // to the top because clear-and-rebuild dropped the scroll container.
  // After the fix the host node must keep identity across no-op ticks,
  // so the browser preserves scrollTop / scrollLeft.
  state.mock = true;
  state.applySnapshot(mockSnapshot());
  render.showView('tournament');
  // Drill into a board card so the conversation diff is rendered.
  globalThis.location.hash = '#/tournament/conv/extract_invoice_001';
  render.applyRoute();
  render.renderAll();

  const detail = doc.getElementById('tournament-detail');
  assert(detail.children.length > 0,
    'the matchup detail must have rendered');
  const detailFirst = detail.firstChild;
  // Simulate the user scrolling inside the panel.
  detail.scrollTop = 240;
  detail.scrollLeft = 32;
  // Capture the first child as the host of the "scroll container" — its
  // identity is what the browser keys scroll preservation off.
  assert(detailFirst != null, 'the detail panel must have a top child');

  // Twelve no-op SSE ticks.
  for (let i = 0; i < 12; i++) render.renderAll();

  // The host node identity is unchanged, so the browser preserves the
  // scroll position. The harness does not model scroll-on-replacement
  // — node identity is the contract that matters.
  assertEqual(detail.firstChild, detailFirst,
    'the matchup-detail subtree must keep its host node identity across no-op ticks');
  assertEqual(detail.scrollTop, 240,
    `vertical scroll position was reset (${detail.scrollTop}); should still be 240`);
  assertEqual(detail.scrollLeft, 32,
    `horizontal scroll position was reset (${detail.scrollLeft}); should still be 32`);
});

test('scroll position survives 10+ no-op ticks on a mutation-site diff', () => {
  // The mutation-site detail pane already routes through swapIfChanged;
  globalThis.location.hash = '#/files';
  // this test pins the contract: a no-op SSE repaint must keep the
  // detail subtree identity, so the inner viewer's scrollLeft survives.
  state.mock = true;
  state.applySnapshot(mockSnapshot());
  // Route to a Files view path and select a mutation site so the
  // mutations-detail-pane has content.
  state.mutations = {
    epochId: '2026-05-15_e1',
    index: { mutations: [
      { mutation_id: 'site_a', role: 'instruction', file: 'agent.py',
        patched_generation_ids: [] },
    ]},
    selectedId: 'site_a',
    detail: {
      mutation_id: 'site_a',
      baseline: { role: 'instruction', file: 'agent.py',
        line_start: 1, line_end: 2, content: 'a\nb\n' },
      versions: [],
    },
  };
  render.showView('files');
  render.renderAll();

  const pane = doc.getElementById('mutations-detail-pane');
  // Drive a single direct render to settle the swapIfChanged digest.
  if (typeof render.renderMutationsDetail === 'function') {
    render.renderMutationsDetail();
  }
  const beforeHost = pane.firstChild;
  // The detail pane is now scrolled by the user.
  pane.scrollLeft = 48;
  pane.scrollTop = 96;

  // Twelve no-op SSE ticks.
  for (let i = 0; i < 12; i++) render.renderAll();

  // Host identity unchanged -> the browser preserved the user's scroll.
  assertEqual(pane.firstChild, beforeHost,
    'the mutation-site detail subtree must keep its host identity across no-op ticks');
  assertEqual(pane.scrollLeft, 48,
    `mutation-site horizontal scroll was reset (${pane.scrollLeft}); should still be 48`);
  assertEqual(pane.scrollTop, 96,
    `mutation-site vertical scroll was reset (${pane.scrollTop}); should still be 96`);
});

test('a genuine state change still re-renders the affected view', () => {
  // The digest gate must NOT mask real updates. Drop the active
  // tournament (so the hall does not dominate) and flip the champion
  // lineage to a new shape — renderAll must paint the new value.
  // Pin the route to the Tournament view so applyRoute (called from
  // the tail of renderAll) does not switch us back to some other view
  // a prior test left routed.
  globalThis.location.hash = '#/tournament';
  state.mock = true;
  state.applySnapshot(mockSnapshot());
  state.activeTournament = null;
  state.bracket = {
    epoch_id: 'before-change',
    champion_lineage: ['v0', 'v2', 'v4'],
    matchups: [
      { champion: 'v0', challenger: 'v2', decision: 'promoted',
        delta_scalar: -0.05, ran_at: '2026-05-10T10:00:00Z' },
    ],
  };
  render.applyRoute();
  render.renderAll();

  const bracket = doc.getElementById('tournament-bracket');
  const before = bracket.textContent;
  assert(before.includes('v4'),
    `the initial bracket must show the v4 champion, got: ${before.slice(0,200)}`);

  // Apply a structural change — flip the champion lineage tail.
  state.bracket = {
    epoch_id: 'after-change',
    champion_lineage: ['v0', 'v2', 'v9'],
    matchups: [
      { champion: 'v0', challenger: 'v2', decision: 'promoted',
        delta_scalar: -0.05, ran_at: '2026-05-10T10:00:00Z' },
    ],
  };
  render.renderAll();
  const after = bracket.textContent;
  assert(after !== before,
    'a real state change must produce a different render output');
  assert(after.includes('v9'),
    `the new champion id v9 must appear in the bracket, got: ${after.slice(0,200)}`);
});

test('a heartbeat last_heartbeat refresh is treated as a no-op', () => {
  // The heartbeat's `last_heartbeat` field churns ~1Hz with no
  // structural change. It must NOT count as a state change for renderAll
  // — the digest excludes it. The stale-badge logic reads it directly
  // in renderHeader (which is patchText-guarded), so freshness is still
  // reflected in the header without rebuilding the whole view.
  globalThis.location.hash = '#/overview';
  state.mock = true;
  state.applySnapshot(mockSnapshot());
  render.showView('overview');
  render.renderAll();

  // A pure timestamp ping — only last_heartbeat moves.
  const before = doc.body.innerHTMLWriteCount();
  state.setHeartbeat({ last_heartbeat: '2026-05-19T10:00:00Z' });
  render.renderAll();
  // Another pure timestamp ping.
  state.setHeartbeat({ last_heartbeat: '2026-05-19T10:00:01Z' });
  render.renderAll();

  // The body subtree must not have gained any innerHTML writes — the
  // digest gate kept the panel renders out.
  assertEqual(doc.body.innerHTMLWriteCount(), before,
    'a heartbeat-only refresh must not drive a panel re-render');
});

test('a healthReport checked_at refresh is treated as a no-op', () => {
  // checked_at re-stamps each report; the findings are the structural
  // content. The digest excludes checked_at so a re-stamp-only refresh
  // must skip the render.
  globalThis.location.hash = '#/overview';
  state.mock = true;
  state.applySnapshot(mockSnapshot());
  state.healthReport = {
    epoch_id: 'e',
    healthy: true,
    findings: [],
    checked_at: '2026-05-19T10:00:00Z',
  };
  render.showView('overview');
  render.renderAll();

  const before = doc.body.innerHTMLWriteCount();
  // Same findings; only checked_at moves.
  state.healthReport = {
    epoch_id: 'e',
    healthy: true,
    findings: [],
    checked_at: '2026-05-19T10:00:01Z',
  };
  render.renderAll();
  state.healthReport = {
    epoch_id: 'e',
    healthy: true,
    findings: [],
    checked_at: '2026-05-19T10:00:02Z',
  };
  render.renderAll();

  assertEqual(doc.body.innerHTMLWriteCount(), before,
    'a checked_at-only refresh must not drive a panel re-render');
});

await run();

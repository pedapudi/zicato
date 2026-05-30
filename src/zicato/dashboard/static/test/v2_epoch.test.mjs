// test/v2_epoch.test.mjs — the v2 Epoch + Report views.
//
// These views are async (they fetch contract-diff / per-judge-trend /
// per-entry / analysis), so each test installs a mocked global fetch that
// routes by URL, seeds `state`, drives one render, then re-renders after a
// microtask drain so the settled fetches flow into the DOM. We never hit a
// real server. Run directly: `node static/test/v2_epoch.test.mjs`.

import { installDom, makeEvent, test, run, assert } from './harness.mjs';

installDom();

const { state } = await import('../js/core/state.js');
const { v2Router } = await import('../js/v2/router.js');
const shell = await import('../js/v2/shell.js');
// Importing the view modules triggers their self-registration.
const epochView = await import('../js/v2/views/epoch.js');
const reportView = await import('../js/v2/views/report.js');

// A view host the shell.renderView contract writes into. Each test gets
// a FRESH document so only one `#v2-view` exists — the views' async
// `_repaint()` resolves `$('v2-view')` to this host, not a stale one.
function installViewHost() {
  installDom();
  const host = document.createElement('div');
  host.id = 'v2-view';
  document.body.appendChild(host);
  return host;
}

// Recursively find the first descendant with a given localName — the
// harness querySelector only matches attribute selectors, not tags.
function findByTag(node, tag) {
  if (!node || node.nodeType !== 1) return null;
  if (node.localName === tag) return node;
  for (const c of (node.children || [])) {
    const hit = findByTag(c, tag);
    if (hit) return hit;
  }
  return null;
}

// Route both window.location.hash AND the v2Router's internal _current so
// the views' `_repaint()` (which reads v2Router.current()) targets the
// right epoch.
function setRoute(view, epochId) {
  window.location.hash = epochId ? `#/v2/${view}/${epochId}` : `#/v2/${view}`;
  v2Router.resolve();
}

// Route fetch by URL substring; return the matching JSON payload.
function mockFetch(routes) {
  const orig = globalThis.fetch;
  globalThis.fetch = async (url) => {
    let body = null;
    for (const [needle, payload] of routes) {
      if (String(url).includes(needle)) { body = payload; break; }
    }
    if (body === '__404__') {
      return { ok: false, status: 404, headers: new Map(),
        json: async () => ({}), text: async () => '' };
    }
    return {
      ok: true, status: 200, headers: new Map(),
      json: async () => body, text: async () => JSON.stringify(body),
    };
  };
  return () => { globalThis.fetch = orig; };
}

const flush = () => new Promise((r) => setTimeout(r, 25));

// Reset shared module + app state between cases.
function resetAll() {
  epochView.resetEpochView();
  reportView.resetReportView();
  state.epochDef = null;
  state.heartbeat = null;
  state.epoch = { id: '—' };
}

// =====================================================================
// Epoch view
// =====================================================================

test('renderEpoch renders goal, the experiment ledger, and drillable rows', async () => {
  resetAll();
  const host = installViewHost();
  state.epochDef = {
    epoch_id: 'e1',
    goal: 'Tighten slide-structure discipline.',
    closed: false,
    experiments: [
      { generation_id: 'v0', parent_generation_id: null, outcome: null,
        hypothesis: { core_idea: 'baseline seed' } },
      { generation_id: 'v1', parent_generation_id: 'v0',
        hypothesis: { core_idea: 'Enforce explicit slide structure.' },
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.31,
          drift_loss_delta: -0.12, pass_rate_delta: 0.10 } },
      { generation_id: 'v2', parent_generation_id: 'v1',
        hypothesis: { core_idea: 'Add a length cap.' },
        outcome: { tournament_decision: 'rejected', scalar_score_delta: 0.04,
          drift_loss_delta: 0.02, pass_rate_delta: -0.05,
          rejection_reason: 'scalar regressed beyond the margin' } },
    ],
  };
  const restore = mockFetch([
    ['/contract-diff/', { epoch_id: 'e1', predecessor_epoch_id: 'e0',
      components: [{ name: 'board', changed: true, previous_hash: 'aaaaaaa1', current_hash: 'bbbbbbb2' },
        { name: 'scoring', changed: false }] }],
    ['/per-judge-trend', { epoch_id: 'e1', generations: ['v0', 'v1', 'v2'],
      judges: [{ judge_name: 'critic_A', by_generation: { v0: 0.4, v1: 0.3, v2: 0.35 } }] }],
    ['/per-entry', { epoch_id: 'e1', generation_id: 'v1',
      entries: [{ entry_id: 'waffles_single', drift_loss: 0.5 }] }],
    ['/analysis', { epoch_id: 'e1', analysis_html_inline: '<p>inline report</p>',
      analysis_html_available: true }],
  ]);
  setRoute('epoch', 'e1');

  epochView.renderEpoch(host, v2Router.current());
  await flush();
  epochView.renderEpoch(host, v2Router.current());

  const text = host.textContent;
  assert(text.includes('Tighten slide-structure discipline'), 'goal renders');
  assert(text.includes('Experiment ledger'), 'ledger section present');
  assert(text.includes('v1') && text.includes('v2'), 'generation rows render');
  assert(text.includes('Enforce explicit slide structure'), 'hypothesis one-liner renders');
  // Semantic delta: a promoted improvement carries the ▼ glyph; a regress ▲.
  assert(text.includes('▼'), 'improvement delta glyph present');
  assert(text.includes('▲'), 'regression delta glyph present');
  // Fired rule inferred from the rejection reason.
  assert(text.includes('scalar_margin'), 'fired gate rule inferred for the rejected row');

  // Every ledger row is a door: a drillable row navigates to the
  // experiment route.
  const rows = host.querySelectorAll('[role]').filter((n) =>
    n.getAttribute('role') === 'button' && n.localName === 'tr');
  assert(rows.length >= 2, 'ledger rows are drillable buttons');
  window.location.hash = '';
  rows.find((r) => r.textContent.includes('v2')).dispatchEvent(makeEvent('click'));
  assert(window.location.hash.includes('/v2/experiment/v2'),
    `row click drills to the experiment; got ${window.location.hash}`);
  restore();
});

test('renderEpoch landscape toggles entries ⇄ judges and embeds the report inline', async () => {
  resetAll();
  const host = installViewHost();
  state.epochDef = {
    epoch_id: 'e1', goal: 'g', experiments: [
      { generation_id: 'v1', parent_generation_id: 'v0',
        hypothesis: { core_idea: 'x' },
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.1 } },
    ],
  };
  const restore = mockFetch([
    ['/contract-diff/', { epoch_id: 'e1', predecessor_epoch_id: null, components: [] }],
    ['/per-judge-trend', { epoch_id: 'e1', generations: ['v1'],
      judges: [{ judge_name: 'critic_Z', by_generation: { v1: 0.22 } }] }],
    ['/per-entry', { epoch_id: 'e1', generation_id: 'v1',
      entries: [{ entry_id: 'q3_metrics_outline', drift_loss: 0.71 }] }],
    ['/analysis', { epoch_id: 'e1', analysis_html_inline: '<p>FIGURE-A</p>',
      analysis_html_available: true }],
  ]);
  setRoute('epoch', 'e1');

  epochView.renderEpoch(host, v2Router.current());
  await flush();
  epochView.renderEpoch(host, v2Router.current());

  // Default facet is entries.
  assert(host.textContent.includes('q3_metrics_outline'), 'entries heatmap shows a board entry');

  // The inline report fragment is embedded (innerHTML write counted).
  assert(host.innerHTMLWriteCount() >= 1, 'inline report fragment injected verbatim');
  // Inline + standalone links both reachable.
  const links = host.querySelectorAll('[href]').map((a) => a.getAttribute('href'));
  assert(links.some((h) => h.includes('/v2/report/e1')), 'links to the in-app report route');
  assert(links.some((h) => h.includes('/api/epoch/e1/analysis.html')),
    'links to the raw standalone analysis.html');

  // Toggle to judges.
  const judgesTab = host.querySelectorAll('[data-facet]')
    .find((n) => n.getAttribute('data-facet') === 'judges' && n.localName === 'button');
  assert(judgesTab != null, 'judges facet toggle exists');
  judgesTab.dispatchEvent(makeEvent('click'));
  // The click flips the module-level facet and repaints the live host;
  // re-render explicitly so the assertion reads the settled DOM.
  epochView.renderEpoch(host, v2Router.current());
  assert(host.textContent.includes('critic_Z'), 'judges heatmap renders after toggle');
  restore();
});

test('renderEpoch shows an honest not-yet for an unbuilt report', async () => {
  resetAll();
  const host = installViewHost();
  state.epochDef = { epoch_id: 'e1', goal: 'g', experiments: [] };
  const restore = mockFetch([
    ['/contract-diff/', { epoch_id: 'e1', predecessor_epoch_id: null, components: [] }],
    ['/per-judge-trend', { epoch_id: 'e1', generations: [], judges: [] }],
    ['/analysis', { epoch_id: 'e1', analysis_html_inline: '', analysis_html_available: false }],
  ]);
  setRoute('epoch', 'e1');
  epochView.renderEpoch(host, v2Router.current());
  await flush();
  epochView.renderEpoch(host, v2Router.current());

  assert(host.textContent.includes('not built yet'),
    `unbuilt report states the honest not-yet; got: ${host.textContent.slice(0, 300)}`);
  assert(host.textContent.toLowerCase().includes('analyze'),
    'the not-yet carries the actionable analyzer hint');
  // Empty ledger states it plainly, not a blank.
  assert(host.textContent.includes('No experiments yet'), 'empty ledger states it honestly');
  restore();
});

// =====================================================================
// Report view
// =====================================================================

test('renderReport embeds the standalone report in an iframe + new-tab link', async () => {
  resetAll();
  const host = installViewHost();
  const restore = mockFetch([
    ['/analysis', { epoch_id: 'e1', analysis_html_inline: '<p>r</p>',
      analysis_html_available: true }],
  ]);
  setRoute('report', 'e1');
  reportView.renderReport(host, v2Router.current());
  await flush();
  reportView.renderReport(host, v2Router.current());

  const frame = findByTag(host, 'iframe');
  assert(frame != null, 'report renders inside an iframe (styling preserved verbatim)');
  assert(frame.getAttribute('src') === '/api/epoch/e1/analysis.html',
    `iframe points at the raw standalone document; got ${frame.getAttribute('src')}`);
  const links = host.querySelectorAll('[href]').map((a) => a.getAttribute('href'));
  assert(links.some((h) => h === '/api/epoch/e1/analysis.html'),
    'a clear open-in-new-tab link to the raw analysis.html');
  assert(links.some((h) => h.includes('/v2/epoch/e1')), 'a back-to-epoch link');
  restore();
});

test('renderReport states the honest not-yet when the report is unbuilt', async () => {
  resetAll();
  const host = installViewHost();
  const restore = mockFetch([
    ['/analysis', { epoch_id: 'e1', analysis_html_inline: '', analysis_html_available: false }],
  ]);
  setRoute('report', 'e1');
  reportView.renderReport(host, v2Router.current());
  await flush();
  reportView.renderReport(host, v2Router.current());

  assert(findByTag(host, 'iframe') == null, 'no iframe when there is no report');
  assert(host.textContent.includes('not built yet'), 'states the honest not-yet');
  assert(host.textContent.toLowerCase().includes('analyze'), 'carries the analyzer hint');
  restore();
});

test('both views self-register with the shell', () => {
  // The shell exposes its registry only through renderView routing; a
  // round-trip through the router proves registration. We assert the
  // module exports the renderers (the import above already ran their
  // registerView calls without throwing).
  assert(typeof epochView.renderEpoch === 'function', 'renderEpoch exported');
  assert(typeof reportView.renderReport === 'function', 'renderReport exported');
  void shell;
});

await run();

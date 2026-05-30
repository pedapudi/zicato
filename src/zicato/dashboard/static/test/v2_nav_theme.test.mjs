// test/v2_nav_theme.test.mjs — the v2 chrome: theming + navigation.
//
// Pins the re-skin + re-navigation deliverables (DASHBOARD-V2 §3.1 + §4):
//   * THEME mechanism — three themes selected by data-theme on the root,
//     persisted to localStorage['zicato.theme'], solarized-dark default;
//     the switcher applies + persists; readTheme tolerates junk.
//   * NAVIGATION — the always-visible breadcrumb / level map (Overview ›
//     Epoch › Experiment › Run), every v2 route reachable, and the
//     permanent live→Bench affordance shown only while a run is in flight.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

const document = installDom();

const {
  parseV2Hash, v2Href, V2_VIEWS, crumbTrail, V2_VIEW_LABELS,
} = await import('../js/v2/router.js');
const shell = await import('../js/v2/shell.js');
const { state } = await import('../js/core/state.js');

const {
  THEME_KEY, V2_THEMES, V2_DEFAULT_THEME,
  readTheme, applyTheme, initTheme,
  renderShell, resetShellDigest, runIsLive,
} = shell;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function installV2Root() {
  // A fresh #v2-root for renderShell to mount into.
  const existing = document.getElementById('v2-root');
  if (existing && existing.parentNode) existing.parentNode.removeChild(existing);
  const root = document.createElement('div');
  root.id = 'v2-root';
  root.className = 'v2-root';
  document.body.appendChild(root);
  return root;
}

function descendants(node, pred) {
  const out = [];
  const walk = (n) => {
    if (!n || n.nodeType !== 1) return;
    if (pred(n)) out.push(n);
    for (const c of n.children) walk(c);
  };
  walk(node);
  return out;
}
function byClass(node, cls) {
  return descendants(node, (n) => n.classList && n.classList.contains(cls));
}
function byId(node, id) {
  return descendants(node, (n) => n.getAttribute && n.getAttribute('id') === id)[0] || null;
}

function resetState() {
  state.activeTournament = null;
  state.activeRuns = [];
  state.heartbeat = null;
  state.lineage = { generations: [], experiments: [] };
  resetShellDigest();
}

// ===========================================================================
// Theming
// ===========================================================================

test('theme: the default is solarized-dark when nothing is persisted', () => {
  window.localStorage.clear();
  assertEqual(readTheme(), 'solarized-dark');
  assertEqual(V2_DEFAULT_THEME, 'solarized-dark');
});

test('theme: exactly three themes ship', () => {
  assertEqual(V2_THEMES.length, 3);
  assert(V2_THEMES.includes('solarized-dark'));
  assert(V2_THEMES.includes('solarized-light'));
  assert(V2_THEMES.includes('monokai'));
});

test('theme: applyTheme stamps data-theme on <html> + #v2-root and persists', () => {
  window.localStorage.clear();
  installV2Root();
  applyTheme('monokai');
  assertEqual(document.documentElement.getAttribute('data-theme'), 'monokai',
    'data-theme must be stamped on the document root');
  assertEqual(document.getElementById('v2-root').getAttribute('data-theme'), 'monokai',
    'data-theme must also be stamped on #v2-root');
  assertEqual(window.localStorage.getItem(THEME_KEY), 'monokai',
    'the choice must persist to localStorage');
});

test('theme: an unknown / junk persisted value falls back to the default', () => {
  window.localStorage.setItem(THEME_KEY, 'hot-dog-stand');
  assertEqual(readTheme(), 'solarized-dark');
  // applyTheme also normalizes a junk argument.
  installV2Root();
  applyTheme('hot-dog-stand');
  assertEqual(document.documentElement.getAttribute('data-theme'), 'solarized-dark');
});

test('theme: initTheme applies the persisted choice', () => {
  window.localStorage.clear();
  window.localStorage.setItem(THEME_KEY, 'solarized-light');
  installV2Root();
  initTheme();
  assertEqual(document.documentElement.getAttribute('data-theme'), 'solarized-light');
});

test('theme: the switcher renders a select with all three themes + applies on change', () => {
  window.localStorage.clear();
  installV2Root();
  resetState();
  renderShell(parseV2Hash('#/v2/overview'));

  const root = document.getElementById('v2-root');
  const select = byId(root, 'v2-theme-select');
  assert(select != null, 'the top bar must carry a theme <select>');
  const options = select.children.filter((c) => c.tagName === 'OPTION');
  assertEqual(options.length, 3, 'the switcher must offer all three themes');
  const values = options.map((o) => o.getAttribute('value'));
  for (const t of V2_THEMES) assert(values.includes(t), `option for ${t} must exist`);

  // Simulate a user choosing monokai.
  const ev = makeEvent('change', { target: { value: 'monokai' } });
  select.dispatchEvent(ev);
  assertEqual(document.documentElement.getAttribute('data-theme'), 'monokai',
    'choosing a theme must apply it to the root');
  assertEqual(window.localStorage.getItem(THEME_KEY), 'monokai',
    'choosing a theme must persist it');
});

// ===========================================================================
// Breadcrumb / level map
// ===========================================================================

test('nav: every v2 view is reachable via a labeled href', () => {
  for (const v of V2_VIEWS) {
    assert(V2_VIEW_LABELS[v], `view ${v} must have a human label`);
    const href = v2Href(v);
    assertEqual(parseV2Hash(href).view, v,
      `v2Href(${v}) must round-trip back to the same view`);
  }
});

test('nav: the breadcrumb roots at Overview and marks the leaf current', () => {
  const trail = crumbTrail(parseV2Hash('#/v2/overview'));
  assertEqual(trail[0].view, 'overview');
  assert(trail[trail.length - 1].current, 'the leaf crumb must be current');
});

test('nav: a run crumb trail descends Overview › Experiment › Run', () => {
  const trail = crumbTrail(parseV2Hash('#/v2/run/waffles_single/v3'));
  const views = trail.map((c) => c.view);
  assertEqual(views[0], 'overview');
  assert(views.includes('experiment'),
    'a run under a generation trails through its Experiment');
  assertEqual(views[views.length - 1], 'run');
  // The Experiment ancestor is a link back up (not current); Run is current.
  const exp = trail.find((c) => c.view === 'experiment');
  assert(exp && !exp.current && exp.href.includes('v3'),
    'the Experiment ancestor must link back up to its generation');
  assert(trail[trail.length - 1].current, 'Run is the current leaf');
});

test('nav: the report crumb trail descends from its epoch', () => {
  const trail = crumbTrail(parseV2Hash('#/v2/report/2026-05-30_e0'));
  const views = trail.map((c) => c.view);
  assert(views.includes('epoch'), 'a report trails through its Epoch');
  assertEqual(views[views.length - 1], 'report');
});

test('nav: the breadcrumb renders into the chrome with separators', () => {
  installV2Root();
  resetState();
  renderShell(parseV2Hash('#/v2/run/waffles_single/v3'));
  const root = document.getElementById('v2-root');
  const crumbsHost = byId(root, 'v2-crumbs');
  assert(crumbsHost != null, 'the chrome must carry a breadcrumb host');
  const crumbs = byClass(crumbsHost, 'v2-crumb');
  assert(crumbs.length >= 3, `expected at least 3 crumbs; got ${crumbs.length}`);
  const seps = byClass(crumbsHost, 'v2-crumb-sep');
  assertEqual(seps.length, crumbs.length - 1,
    'there must be exactly one separator between adjacent crumbs');
  // The current leaf must carry aria-current and NOT be a link.
  const current = crumbs.find((c) => c.getAttribute('aria-current') === 'page');
  assert(current != null, 'the active crumb must be aria-current=page');
  assertEqual(current.tagName, 'SPAN', 'the current crumb is not a link');
});

// ===========================================================================
// The live → Bench affordance
// ===========================================================================

test('nav: the live→Bench affordance is hidden when no run is live', () => {
  installV2Root();
  resetState();
  assert(!runIsLive(), 'no run should be live with empty state');
  renderShell(parseV2Hash('#/v2/overview'));
  const root = document.getElementById('v2-root');
  const go = byId(root, 'v2-live-go');
  assert(go != null, 'the live→Bench affordance element must exist in the chrome');
  assertEqual(go.getAttribute('hidden'), 'hidden',
    'the affordance must be hidden when nothing is live');
});

test('nav: the live→Bench affordance appears (→ bench) while a run is live', () => {
  installV2Root();
  resetState();
  state.activeTournament = { champion: 'v2', challenger: 'v3' };
  assert(runIsLive(), 'an active tournament means a run is live');
  renderShell(parseV2Hash('#/v2/overview'));
  const root = document.getElementById('v2-root');
  const go = byId(root, 'v2-live-go');
  assert(go != null, 'the affordance must exist');
  assert(go.getAttribute('hidden') == null,
    'the affordance must be visible while a run is live');
  assertEqual(go.getAttribute('href'), v2Href('bench'),
    'the affordance must link to the Bench — one click away from anywhere');
});

test('nav: active runs also count as live for the Bench affordance', () => {
  installV2Root();
  resetState();
  state.activeRuns = [{ entry_id: 'e1', progress: 0.3 }];
  assert(runIsLive(), 'a non-empty activeRuns means a run is live');
  renderShell(parseV2Hash('#/v2/epoch'));
  const root = document.getElementById('v2-root');
  const go = byId(root, 'v2-live-go');
  assert(go.getAttribute('hidden') == null,
    'active runs must surface the live→Bench affordance');
});

test('nav: the Bench is reachable from the chrome when idle (the broken-nav fix)', () => {
  installV2Root();
  resetState();
  // Idle: no run live. A plain Bench link must be present + visible so
  // the Bench is never unreachable (the v2 miss this fixes).
  renderShell(parseV2Hash('#/v2/epoch'));
  const root = document.getElementById('v2-root');
  const benchLink = byId(root, 'v2-bench-link');
  assert(benchLink != null, 'a permanent Bench link must exist in the chrome');
  assert(benchLink.getAttribute('hidden') == null,
    'the Bench link must be visible when idle (Bench reachable from anywhere)');
  assertEqual(benchLink.getAttribute('href'), v2Href('bench'));
});

test('nav: the plain Bench link defers to the loud affordance while live', () => {
  installV2Root();
  resetState();
  state.activeTournament = { champion: 'v2', challenger: 'v3' };
  renderShell(parseV2Hash('#/v2/overview'));
  const root = document.getElementById('v2-root');
  const benchLink = byId(root, 'v2-bench-link');
  const go = byId(root, 'v2-live-go');
  assertEqual(benchLink.getAttribute('hidden'), 'hidden',
    'the plain Bench link hides when the loud live→Bench affordance is showing');
  assert(go.getAttribute('hidden') == null, 'the loud affordance shows while live');
});

test('nav: the live→Bench affordance hides itself when already on the Bench', () => {
  installV2Root();
  resetState();
  state.activeTournament = { champion: 'v2', challenger: 'v3' };
  renderShell(parseV2Hash('#/v2/bench'));
  const root = document.getElementById('v2-root');
  const go = byId(root, 'v2-live-go');
  assertEqual(go.getAttribute('hidden'), 'hidden',
    'on the Bench the affordance is a no-op self-link, so it hides');
});

await run();

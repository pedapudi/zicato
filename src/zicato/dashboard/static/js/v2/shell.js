// js/v2/shell.js — the v2 shell.
//
// DASHBOARD-V2 §4: v2 is TWO MODES (Bench/live + Notebook/post-hoc)
// unified by the lineage spine. The shell is the persistent frame:
//
//   ┌ head:  zicato · mode indicator (BENCH ● / NOTEBOOK) ──────────┐
//   ├ spine: the trajectory primitive — persistent lineage nav      │
//   ├ view:  the active view container (overview/epoch/…)           │
//
// The shell is SSE-driven and re-render-safe: it reuses the v1 render
// spine (mount / patch / digest-gating) so a heartbeat tick that only
// re-stamps a timestamp writes ZERO DOM — no flash (the discipline the
// v1 shell established).
//
// This is the FOUNDATION skeleton: the spine + mode indicator + view
// routing are live; the per-view bodies are placeholders rendered with
// `stateBlock('not_yet')` until the Bench/Notebook waves land their view
// modules. A view module registers itself with `registerView(name, fn)`;
// `fn(host, route)` renders into the view's container. Unregistered
// views fall back to the honest not-yet placeholder.

import { $, el, patchText, patchAttr, clearChildren, mount } from '../core/dom.js';
import { state } from '../core/state.js';
import { fmtScalar } from '../core/format.js';
import { v2Router, V2_VIEWS, V2_MODE, v2Href, crumbTrail } from './router.js';
import { trajectory } from './components/trajectory.js';
import { stateBlock } from './components/stateBlock.js';
import { harmonografIsLive } from '../core/harmonograf.js';

const ROOT_ID = 'v2-root';

// ---------------------------------------------------------------------------
// Theming (DASHBOARD-V2 §3.1). THREE switchable themes selected by a
// `data-theme` attribute on the document root, persisted to
// localStorage['zicato.theme']. solarized-dark is the DEFAULT. The token
// sets live in css/v2/tokens.css; this is purely the mechanism + switcher.
// ---------------------------------------------------------------------------
export const THEME_KEY = 'zicato.theme';
export const V2_THEMES = ['solarized-dark', 'solarized-light', 'monokai'];
export const V2_DEFAULT_THEME = 'solarized-dark';
const THEME_LABELS = {
  'solarized-dark': 'Solarized Dark',
  'solarized-light': 'Solarized Light',
  monokai: 'Monokai',
};

function normalizeTheme(t) {
  return V2_THEMES.includes(t) ? t : V2_DEFAULT_THEME;
}

// The persisted choice (or the default). Tolerant of a private-mode
// localStorage that throws.
export function readTheme() {
  try {
    const stored = window.localStorage && window.localStorage.getItem(THEME_KEY);
    return normalizeTheme(stored);
  } catch { return V2_DEFAULT_THEME; }
}

// Apply a theme: stamp data-theme on <html> AND #v2-root (the spec allows
// either; stamping both makes the selector robust regardless of where a
// rule is scoped), and persist the choice.
export function applyTheme(theme) {
  const t = normalizeTheme(theme);
  const root = (typeof document !== 'undefined' && document.documentElement) || null;
  if (root && typeof root.setAttribute === 'function') root.setAttribute('data-theme', t);
  const v2root = $(ROOT_ID);
  if (v2root) v2root.setAttribute('data-theme', t);
  try {
    if (window.localStorage) window.localStorage.setItem(THEME_KEY, t);
  } catch { /* private mode — the in-DOM attribute still wins for the session */ }
  return t;
}

// Idempotent init — call once at boot before the first paint so the
// page starts on the persisted (or default) theme.
export function initTheme() {
  return applyTheme(readTheme());
}

// The top-bar theme switcher: a labeled <select> whose change applies +
// persists the theme. Built once; renderTheme() syncs its value on paint.
function buildThemeSwitcher() {
  const select = el('select', {
    class: 'v2-theme-select', id: 'v2-theme-select', 'aria-label': 'Color theme',
    onchange: (ev) => {
      const t = ev && ev.target ? ev.target.value : null;
      applyTheme(t);
    },
  }, V2_THEMES.map((t) => el('option', { value: t }, [THEME_LABELS[t] || t])));
  // Seed the control to the active theme.
  select.value = readTheme();
  return el('label', { class: 'v2-theme' }, [
    el('span', { class: 'v2-theme-label', 'aria-hidden': 'true' }, ['theme']),
    select,
  ]);
}

// ---------------------------------------------------------------------------
// Navigation anchors. A plain `<a href="#/v2/...">` is a real link (so
// middle-click / open-in-new-tab / "copy link" all work), but as a SPA
// "home" it has a sharp edge: clicking an anchor whose target equals the
// CURRENT hash fires NO `hashchange`, so the view never re-resolves — the
// brand looks dead when you are already on (or returning to) Overview.
//
// `navAnchor` keeps the href AND routes the click through `v2Router.go()`,
// which re-`resolve()`s on a same-hash click and otherwise sets the hash.
// That makes the brand + breadcrumb ancestors a DEPENDABLE home from every
// view (including the Bench), independent of whether the browser bothers
// to fire `hashchange`. We preventDefault only for a plain left click so
// modified clicks (new tab / window) still get the browser's native href.
function navOnClick(navigate) {
  return (ev) => {
    if (ev && (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey
      || (ev.button != null && ev.button !== 0))) {
      return; // let the browser honor the href (new tab / window / etc.)
    }
    if (ev && ev.preventDefault) ev.preventDefault();
    navigate();
  };
}

function navAnchor(props, children, view, ...segs) {
  return el('a', {
    ...props,
    href: v2Href(view, ...segs),
    onclick: navOnClick(() => v2Router.go(view, ...segs)),
  }, children);
}

// A nav anchor for a breadcrumb crumb: it already knows its href + the
// (view, …segs) needed to drive the router on a same-hash click.
function crumbAnchor(props, children, href, view, segs) {
  return el('a', {
    ...props,
    href,
    onclick: navOnClick(() => v2Router.go(view, ...(segs || []))),
  }, children);
}

// View module registry. The foundation ships the frame; later waves
// register real renderers here. `fn(host, route)` owns the host's body.
const _views = new Map();
export function registerView(name, fn) {
  if (V2_VIEWS.includes(name) && typeof fn === 'function') _views.set(name, fn);
}

// ---------------------------------------------------------------------------
// Spine nodes — map the lineage into the trajectory primitive's contract.
//
//   id       — generation id (the node's identity + onSelect arg)
//   parentId — the parent generation (lineage edge / branch anchor)
//   scalar   — drives the y-trajectory (lower = higher)
//   verdict  — 'promoted' | 'rejected' | 'open'
//   live     — the in-flight challenger pulses
//   label    — id (generations zoom leads with the gen id)
//
// Sourced from state.lineage.generations (the /api/lineage shape). The
// live challenger, when a tournament is in flight, is marked live so the
// spine pulses during a run.
// ---------------------------------------------------------------------------
function verdictKey(raw) {
  const v = String(raw || '').toLowerCase();
  if (v.startsWith('prom') || v === 'accepted') return 'promoted';
  if (v.startsWith('rej')) return 'rejected';
  return 'open';
}

export function spineNodes() {
  const lin = state.lineage || {};
  const gens = Array.isArray(lin.generations) ? lin.generations : [];
  const liveChallenger = state.activeTournament
    && (state.activeTournament.challenger_id
        || state.activeTournament.challenger
        || (state.heartbeat && state.heartbeat.generation_id))
    || null;

  const nodes = gens
    .filter((g) => g && (g.id != null || g.generation_id != null))
    .map((g) => {
      const id = String(g.id != null ? g.id : g.generation_id);
      const scalarRaw = g.scalar != null ? g.scalar
        : (g.best_scalar != null ? g.best_scalar : null);
      const scalar = (typeof scalarRaw === 'number' && isFinite(scalarRaw))
        ? scalarRaw : (scalarRaw != null && isFinite(Number(scalarRaw)) ? Number(scalarRaw) : null);
      return {
        id,
        parentId: g.parent_id != null ? String(g.parent_id)
          : (g.parentId != null ? String(g.parentId) : null),
        scalar,
        verdict: verdictKey(g.verdict || g.outcome || g.tournament_decision),
        live: liveChallenger != null && id === String(liveChallenger),
        label: id,
      };
    });

  // If the live challenger is not yet in the lineage list (mid-first-run,
  // before its row materializes), synthesize a live node so the spine
  // shows the run in flight rather than going blank.
  if (liveChallenger != null && !nodes.some((n) => n.live)) {
    const champ = state.activeTournament
      && (state.activeTournament.champion_id || state.activeTournament.champion);
    nodes.push({
      id: String(liveChallenger),
      parentId: champ != null ? String(champ) : null,
      scalar: null,
      verdict: 'open',
      live: true,
      label: String(liveChallenger),
    });
  }
  return nodes;
}

// ---------------------------------------------------------------------------
// Frame build — idempotent. mount() builds the frame once; later renders
// patch in place. The frame is: head (brand + mode) · spine host · view
// host. View containers are created lazily under the view host.
// ---------------------------------------------------------------------------
function buildFrame(root) {
  const shell = el('div', { class: 'v2-shell' });

  // Head: brand · mode indicator · breadcrumb · [live→Bench] · theme.
  const head = el('div', { class: 'v2-shell-head' });
  // The brand is the dependable "home" from EVERYWHERE — it always routes
  // to Overview, even when the current hash already is Overview (a plain
  // anchor would no-op there). navAnchor drives the click through the
  // router so the view re-resolves regardless.
  head.appendChild(navAnchor(
    { class: 'v2-brand', 'aria-label': 'zicato — overview' },
    ['zicato'], 'overview',
  ));
  const mode = el('span', { class: 'v2-mode', 'data-mode': 'notebook', id: 'v2-mode' }, [
    el('span', { class: 'v2-mode-dot', 'aria-hidden': 'true' }),
    el('span', { class: 'v2-mode-label', id: 'v2-mode-label' }, ['Notebook']),
  ]);
  head.appendChild(mode);

  // Breadcrumb / level map — the primary "where am I" cue. Filled by
  // renderCrumbs() per route.
  head.appendChild(el('nav', {
    class: 'v2-crumbs', id: 'v2-crumbs', 'aria-label': 'Breadcrumb',
  }));

  // Right cluster: the Bench link · the live→Bench affordance · theme.
  const right = el('div', { class: 'v2-chrome-right' });
  // A permanent, plain Bench entry so the Bench is ALWAYS reachable from
  // the chrome (the v2 miss: the Bench was unreachable when idle). It is
  // hidden only while the louder live→Bench affordance is showing, to
  // avoid two Bench links side by side.
  right.appendChild(el('a', {
    class: 'v2-crumb v2-bench-link', id: 'v2-bench-link', href: v2Href('bench'),
    'aria-label': 'Open the Bench (live operations view)',
  }, ['Bench']));
  // The permanent "● live → Bench" affordance — visible only while a run
  // is in flight; always one click to the Bench.
  right.appendChild(el('a', {
    class: 'v2-live-go', id: 'v2-live-go', href: v2Href('bench'),
    hidden: 'hidden', 'aria-label': 'A run is live — open the Bench',
  }, [
    el('span', { class: 'v2-live-go-dot', 'aria-hidden': 'true' }),
    el('span', {}, ['live → Bench']),
  ]));
  right.appendChild(buildThemeSwitcher());
  head.appendChild(right);

  shell.appendChild(head);

  // Spine host — the persistent trajectory nav.
  shell.appendChild(el('div', {
    class: 'v2-spine-host', id: 'v2-spine', role: 'navigation',
    'aria-label': 'Lineage trajectory',
  }));

  // View host — the active view container.
  shell.appendChild(el('div', {
    class: 'v2-view-host', id: 'v2-view', role: 'main',
  }));

  root.appendChild(shell);
}

// ---------------------------------------------------------------------------
// Digest-gated paints. Each surface only re-renders when its inputs
// actually change, so a heartbeat tick that re-stamps a timestamp writes
// zero DOM.
// ---------------------------------------------------------------------------
let _lastModeDigest = null;
let _lastSpineDigest = null;
let _lastViewKey = null;
let _lastCrumbDigest = null;
let _lastLiveGoDigest = null;
let _lastThemeDigest = null;

// Is a run in flight? The chrome's live→Bench affordance shows whenever
// this is true (DASHBOARD-V2 §4). Reuses the single liveness predicate.
export function runIsLive() {
  return harmonografIsLive();
}

// The breadcrumb / level map — the always-visible "where am I" cue.
// Ancestors are links back up the spine; the active leaf is non-link.
function renderCrumbs(route) {
  const host = $('v2-crumbs');
  if (!host) return;
  const trail = crumbTrail(route);
  const digest = JSON.stringify(trail.map((c) => [c.view, c.label, c.href, c.current]));
  if (digest === _lastCrumbDigest) return;
  _lastCrumbDigest = digest;
  clearChildren(host);
  trail.forEach((c, i) => {
    if (i > 0) {
      host.appendChild(el('span', { class: 'v2-crumb-sep', 'aria-hidden': 'true' }, ['›']));
    }
    if (c.current) {
      host.appendChild(el('span', {
        class: 'v2-crumb', 'aria-current': 'page',
      }, [c.label]));
    } else {
      // Ancestor crumbs (the root "Overview" crumb included) route through
      // the router so they are a dependable jump even on a same-hash click.
      host.appendChild(crumbAnchor(
        { class: 'v2-crumb' }, [c.label], c.href, c.view, c.segs,
      ));
    }
  });
}

// The permanent live→Bench affordance: shown only while a run is in
// flight, and dimmed (not duplicated) when already on the Bench.
function renderLiveGo(route) {
  const go = $('v2-live-go');
  const benchLink = $('v2-bench-link');
  if (!go) return;
  const live = runIsLive();
  const onBench = route && route.view === 'bench';
  const digest = (live ? '1' : '0') + (onBench ? 'b' : '');
  if (digest === _lastLiveGoDigest) return;
  _lastLiveGoDigest = digest;
  // The loud live→Bench affordance: only while a run is live and we are
  // NOT already on the Bench (avoid a no-op self-link).
  const showLoud = live && !onBench;
  patchAttr(go, 'hidden', showLoud ? null : 'hidden');
  // The plain Bench link is the always-reachable fallback; hide it only
  // when the loud affordance is showing (one Bench link at a time) or
  // when we are already on the Bench.
  if (benchLink) patchAttr(benchLink, 'hidden', (showLoud || onBench) ? 'hidden' : null);
}

// Keep the switcher's value in sync if the theme changed out-of-band
// (e.g. another tab, or a programmatic applyTheme). Digest-gated.
function renderTheme() {
  const select = $('v2-theme-select');
  if (!select) return;
  const current = readTheme();
  if (current === _lastThemeDigest) return;
  _lastThemeDigest = current;
  if (select.value !== current) select.value = current;
}

function renderMode(route) {
  const label = $('v2-mode-label');
  const mode = $('v2-mode');
  if (!label || !mode) return;
  const m = V2_MODE[route.view] || 'notebook';
  const digest = m + '|' + (m === 'bench' ? (state.activeTournament ? 'live' : 'idle') : '');
  if (digest === _lastModeDigest) return;
  _lastModeDigest = digest;
  patchAttr(mode, 'data-mode', m);
  patchText(label, m === 'bench' ? 'Bench' : 'Notebook');
}

function spineDigest(nodes, route) {
  // Only the structural facts the spine draws + the selected view (so an
  // active-node highlight could key off the route later). Excludes raw
  // heartbeat timestamps so a steady tick is a no-op.
  return JSON.stringify({
    view: route.view,
    nodes: nodes.map((n) => [n.id, n.parentId, n.scalar == null ? null : fmtScalar(n.scalar), n.verdict, !!n.live]),
  });
}

function renderSpine(route) {
  const host = $('v2-spine');
  if (!host) return;
  const nodes = spineNodes();
  const digest = spineDigest(nodes, route);
  if (digest === _lastSpineDigest) return;
  _lastSpineDigest = digest;
  clearChildren(host);
  host.appendChild(trajectory({
    nodes,
    zoom: 'generations',
    onSelect: (id) => v2Router.go('experiment', id),
  }));
}

function renderView(route) {
  const host = $('v2-view');
  if (!host) return;
  // Coarse swap when the view changes; within a view, the registered
  // renderer owns its own incremental updates.
  const viewKey = route.view + '|' + JSON.stringify(route.params || {});
  const registered = _views.get(route.view);
  if (registered) {
    registered(host, route);
    _lastViewKey = viewKey;
    return;
  }
  // No registered renderer yet (foundation skeleton): honest not-yet
  // placeholder, swapped only when the view actually changes.
  if (viewKey === _lastViewKey) return;
  _lastViewKey = viewKey;
  clearChildren(host);
  host.appendChild(el('h1', { class: 'v2-view-title' }, [viewTitle(route.view)]));
  host.appendChild(stateBlock('not_yet', {
    label: `${viewTitle(route.view)} view`,
    detail: 'This view ships in a later wave; the foundation frame is in place.',
  }));
}

function viewTitle(view) {
  switch (view) {
    case 'overview': return 'Overview';
    case 'tournament': return 'Tournament';
    case 'bench': return 'Bench';
    case 'epoch': return 'Epoch';
    case 'experiment': return 'Experiment';
    case 'run': return 'Run';
    case 'report': return 'Report';
    default: return 'Overview';
  }
}

// The full shell paint. Idempotent + digest-gated throughout.
export function renderShell(route) {
  const root = $(ROOT_ID);
  if (!root) return;
  // The persisted theme is applied before the first paint so the page
  // never flashes the wrong palette. Idempotent.
  initTheme();
  // Build the frame exactly once.
  mount(root, 'v2-frame', () => {
    const wrap = el('div', { 'data-node': 'v2-frame' });
    buildFrame(wrap);
    return wrap;
  });
  const r = route || v2Router.current();
  renderMode(r);
  renderCrumbs(r);
  renderLiveGo(r);
  renderTheme();
  renderSpine(r);
  renderView(r);
}

// Reset the digest caches — tests share module state across cases.
export function resetShellDigest() {
  _lastModeDigest = null;
  _lastSpineDigest = null;
  _lastViewKey = null;
  _lastCrumbDigest = null;
  _lastLiveGoDigest = null;
  _lastThemeDigest = null;
}

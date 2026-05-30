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
import { v2Router, V2_VIEWS, V2_MODE, v2Href } from './router.js';
import { trajectory } from './components/trajectory.js';
import { stateBlock } from './components/stateBlock.js';

const ROOT_ID = 'v2-root';

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

  // Head: brand + mode indicator.
  const head = el('div', { class: 'v2-shell-head' });
  head.appendChild(el('a', {
    class: 'v2-brand', href: v2Href('overview'), 'aria-label': 'zicato — overview',
  }, ['zicato']));
  const mode = el('span', { class: 'v2-mode', 'data-mode': 'notebook', id: 'v2-mode' }, [
    el('span', { class: 'v2-mode-dot', 'aria-hidden': 'true' }),
    el('span', { class: 'v2-mode-label', id: 'v2-mode-label' }, ['Notebook']),
  ]);
  head.appendChild(mode);
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
  // Build the frame exactly once.
  mount(root, 'v2-frame', () => {
    const wrap = el('div', { 'data-node': 'v2-frame' });
    buildFrame(wrap);
    return wrap;
  });
  const r = route || v2Router.current();
  renderMode(r);
  renderSpine(r);
  renderView(r);
}

// Reset the digest caches — tests share module state across cases.
export function resetShellDigest() {
  _lastModeDigest = null;
  _lastSpineDigest = null;
  _lastViewKey = null;
}

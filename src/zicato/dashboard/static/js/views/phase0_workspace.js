// views/phase0_workspace.js — L0 (workspace-level) view.
//
// Renders the whole-environment ribbon: env config block, multi-epoch
// lineage with a single best-scalar per epoch, and a tiny cross-epoch
// sparkline. Data source: ``/api/workspace`` (state_reader.build_workspace_view).
// The fetch is owned here (lazy: only when this view becomes active)
// so the L0 endpoint never fans out across the dashboard.

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';
import { phase0Href } from './phase0_router.js';

// Cached workspace payload (per-tab; refetched when the L0 view opens).
let _workspaceCache = null;
let _workspaceLoading = false;

// Reset on demand so a fresh visit re-fetches; mostly here for tests.
export function resetWorkspaceCache() {
  _workspaceCache = null;
  _workspaceLoading = false;
}

// Surface the in-memory workspace payload — primarily for tests.
export function workspacePayload() { return _workspaceCache; }

async function ensureWorkspace(repaint) {
  if (_workspaceCache || _workspaceLoading) return _workspaceCache;
  _workspaceLoading = true;
  try {
    const data = await fetchJson('/api/workspace');
    if (data && typeof data === 'object') {
      _workspaceCache = data;
    } else {
      _workspaceCache = { epochs: [], sparkline: [], current_epoch_id: null };
    }
  } catch {
    _workspaceCache = { epochs: [], sparkline: [], current_epoch_id: null };
  } finally {
    _workspaceLoading = false;
    if (typeof repaint === 'function') repaint();
  }
  return _workspaceCache;
}

// Render the environment config block. Sources its data from the
// ``state.workspace`` field that the environment payload already
// populates; degrades to a placeholder when missing.
function renderEnv() {
  const node = $('phase0-workspace-env');
  if (!node) return;
  clearChildren(node);
  const ws = state.workspace;
  if (!ws) {
    node.appendChild(el('p', { class: 'empty' }, ['No environment loaded.']));
    return;
  }
  const rows = [];
  // ``state.workspace`` can be either a string (legacy) or an object
  // payload; render the few keyed fields we know are stable.
  if (typeof ws === 'string') {
    rows.push({ k: 'root', v: ws });
  } else if (typeof ws === 'object') {
    for (const k of Object.keys(ws)) {
      const v = ws[k];
      if (typeof v === 'string' || typeof v === 'number' || v == null) {
        rows.push({ k, v: v == null ? '—' : String(v) });
      }
    }
  }
  if (rows.length === 0) {
    node.appendChild(el('p', { class: 'empty' }, ['No environment fields.']));
    return;
  }
  const tbl = el('table', { class: 'phase0-kv-table' });
  const tbody = el('tbody');
  for (const r of rows) {
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'phase0-kv-key mono' }, [r.k]),
      el('td', { class: 'phase0-kv-val mono' }, [r.v]),
    ]));
  }
  tbl.appendChild(tbody);
  node.appendChild(tbl);
}

function fmtScalar(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  return v.toFixed(3);
}

function renderLineage() {
  const node = $('phase0-workspace-lineage');
  if (!node) return;
  clearChildren(node);
  const payload = _workspaceCache || {};
  const rows = Array.isArray(payload.epochs) ? payload.epochs : [];
  if (rows.length === 0) {
    node.appendChild(el('p', { class: 'empty' }, ['No epochs recorded.']));
    return;
  }
  const tbl = el('table', { class: 'phase0-lineage-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['epoch']),
    el('th', null, ['goal']),
    el('th', null, ['best scalar']),
    el('th', null, ['gens']),
    el('th', null, ['promoted']),
    el('th', null, ['status']),
  ])]));
  const tbody = el('tbody');
  const cur = payload.current_epoch_id;
  for (const r of rows) {
    const tr = el('tr', null, [
      el('td', null, [el('a', {
        href: phase0Href('epoch', { epochId: r.epoch_id }),
        class: 'mono',
      }, [r.epoch_id])]),
      el('td', null, [r.goal || '—']),
      el('td', { class: 'mono' }, [fmtScalar(r.best_scalar)]),
      el('td', { class: 'mono' }, [String(r.generation_count || 0)]),
      el('td', { class: 'mono' }, [String(r.promoted_count || 0)]),
      el('td', { class: 'mono' }, [
        r.epoch_id === cur ? 'live' : (r.closed ? 'closed' : 'open'),
      ]),
    ]);
    tbody.appendChild(tr);
  }
  tbl.appendChild(tbody);
  node.appendChild(tbl);
}

function renderSparkline() {
  const node = $('phase0-workspace-sparkline');
  if (!node) return;
  clearChildren(node);
  const payload = _workspaceCache || {};
  const pts = Array.isArray(payload.sparkline) ? payload.sparkline : [];
  if (pts.length === 0) {
    node.appendChild(el('p', { class: 'empty' }, ['No scalar history yet.']));
    return;
  }
  // A textual sparkline keeps the dependency surface zero — full SVG
  // rendering is downstream work, but the L0 view needs SOMETHING to
  // render here so the layout is honest about the data being present.
  const line = el('div', { class: 'phase0-sparkline mono' }, []);
  for (const p of pts) {
    line.appendChild(el('span', { class: 'phase0-sparkline-cell' }, [
      (p.epoch_id || '—') + ' ',
      el('span', { class: 'phase0-sparkline-val' }, [fmtScalar(p.scalar)]),
      '  ',
    ]));
  }
  node.appendChild(line);
}

export function renderPhase0Workspace(repaint) {
  ensureWorkspace(repaint);
  renderEnv();
  renderLineage();
  renderSparkline();
}

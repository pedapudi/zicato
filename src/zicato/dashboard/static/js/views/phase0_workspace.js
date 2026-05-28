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
// ``state.workspace`` identity payload that the environment endpoint
// surfaces — root, adk_entrypoint, source_roots, contract paths, and
// the live mutation-point count. Degrades cleanly when missing.
function renderEnv() {
  const node = $('phase0-workspace-env');
  if (!node) return;
  clearChildren(node);
  const ws = state.workspace;
  if (!ws) {
    node.appendChild(el('p', { class: 'empty' }, ['No environment loaded.']));
    return;
  }
  // Legacy: ``state.workspace`` might still be a bare path string when
  // a snapshot was captured before the identity block landed. Surface
  // it minimally rather than failing.
  if (typeof ws === 'string') {
    const tbl = el('table', { class: 'phase0-kv-table' });
    const tbody = el('tbody');
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'phase0-kv-key mono' }, ['root']),
      el('td', { class: 'phase0-kv-val mono' }, [ws]),
    ]));
    tbl.appendChild(tbody);
    node.appendChild(tbl);
    return;
  }
  if (typeof ws !== 'object') {
    node.appendChild(el('p', { class: 'empty' }, ['No environment fields.']));
    return;
  }
  // Render fields in a stable, semantically grouped order — the
  // operator scans top-to-bottom for identity → entrypoint → sources →
  // contract files → mutation surface metadata.
  const ordered = [
    ['root', ws.root],
    ['adk_entrypoint', ws.adk_entrypoint],
    ['instance_id', ws.instance_id],
    ['created_at', ws.created_at],
    ['board_path', ws.board_path],
    ['brief_path', ws.brief_path],
    ['scoring_path', ws.scoring_path],
    ['mutation_point_count',
      typeof ws.mutation_point_count === 'number' ? ws.mutation_point_count : null],
  ];
  const tbl = el('table', { class: 'phase0-kv-table' });
  const tbody = el('tbody');
  for (const [k, v] of ordered) {
    const display = (v == null || v === '') ? '—' : String(v);
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'phase0-kv-key mono' }, [k]),
      el('td', { class: 'phase0-kv-val mono' }, [display]),
    ]));
  }
  // Source roots — multi-value, render as one row per path.
  const roots = Array.isArray(ws.source_roots) ? ws.source_roots : [];
  if (roots.length === 0) {
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'phase0-kv-key mono' }, ['source_roots']),
      el('td', { class: 'phase0-kv-val mono' }, ['—']),
    ]));
  } else {
    for (let i = 0; i < roots.length; i += 1) {
      tbody.appendChild(el('tr', null, [
        el('td', { class: 'phase0-kv-key mono' },
          [i === 0 ? 'source_roots' : '']),
        el('td', { class: 'phase0-kv-val mono' }, [roots[i]]),
      ]));
    }
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
  // Build a set of (parent -> child) edges so the lineage column can
  // render a "→ <child_id>" arrow under each parent row. The index's
  // ``epochs.parent_epoch_id`` column carries this directly; legacy
  // workspaces fall back to directory order (no arrows rendered).
  const childOf = new Map();
  for (const r of rows) {
    if (r && r.parent_epoch_id && r.epoch_id) {
      // First-wins so a duplicated edge does not flip later rows.
      if (!childOf.has(r.parent_epoch_id)) {
        childOf.set(r.parent_epoch_id, r.epoch_id);
      }
    }
  }
  const tbl = el('table', { class: 'phase0-lineage-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['epoch']),
    el('th', null, ['lineage']),
    el('th', null, ['goal']),
    el('th', null, ['best scalar']),
    el('th', null, ['gens']),
    el('th', null, ['promoted']),
    el('th', null, ['status']),
  ])]));
  const tbody = el('tbody');
  const cur = payload.current_epoch_id;
  for (const r of rows) {
    const child = childOf.get(r.epoch_id);
    const lineageCell = child
      ? el('span', { class: 'mono phase0-lineage-edge' }, ['→ ', child])
      : el('span', { class: 'mono' }, ['—']);
    const tr = el('tr', null, [
      el('td', null, [el('a', {
        href: phase0Href('epoch', { epochId: r.epoch_id }),
        class: 'mono',
      }, [r.epoch_id])]),
      el('td', null, [lineageCell]),
      el('td', null, [r.goal || '(no goal recorded)']),
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

// views/phase0_workspace.js — L0 (workspace-level) view.
//
// The cross-environment overview, redesigned around two unifying ideas:
//
//   1. A loop-health banner at the very top answers "is this loop even
//      meaningful right now" before anything else. It is fed by
//      GET /api/health-report and rendered through the shared
//      ``healthBanner`` component.
//
//   2. A single lineage ribbon (``lineageRibbon`` at epoch zoom)
//      replaces what used to be TWO separate pictures of the same data:
//      the epoch-lineage timeline and the cross-epoch best-scalar
//      sparkline. The ribbon encodes the scalar in each node's
//      y-position, so the spine literally traces the optimization curve
//      — it SUBSUMES the sparkline. The old ``epoch-timeline`` and the
//      separate trend/sparkline card are both gone.
//
// What remains:
//   * env slot — loop-health banner (full width, top) + a tightened
//     identity strip: Environment card (left) + "Workspace at a glance"
//     totals tile (right).
//   * lineage slot — the unified lineage ribbon (epoch zoom). Clicking a
//     node drills into that epoch (L1) via ``phase0Href``.
//   * sparkline slot — intentionally cleared. The slot still exists in
//     index.html (we must not edit it) but L0 renders nothing into it
//     now that the ribbon owns the trajectory.
//   * recent slot — Recent decisions, the chronological companion to the
//     ribbon's topological view. Verdict marks now speak through the
//     shared ``verdictGlyph`` so they read identically everywhere.
//
// The in-content Live Activity card was retired earlier (#206); the
// sidebar owns live-run state across every level.
//
// L0-layout styling lives in ``css/phase0_workspace.css`` (a new file;
// the integrator wires its <link> into index.html alongside
// structural.css). Component styling (ribbon, banner, glyph) lives in
// ``css/structural.css`` / ``css/decision.css`` and is owned by the
// component teams — this view never touches it.

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';
import { phase0Href } from './phase0_router.js';
import { renderCard } from '../components/card.js';
import { renderMetricTile } from '../components/tile.js';
import { renderLoadingState, renderEmptyState } from '../components/loading.js';
// Shipped components — imported by direct path, consumed as-is.
import { lineageRibbon } from '../components/lineage_ribbon.js';
import { healthBanner } from '../components/health_banner.js';
import { verdictGlyph } from '../components/verdict_glyph.js';

// Cached workspace payload (per-tab; refetched when the L0 view opens).
let _workspaceCache = null;
let _workspaceLoading = false;

// Cached loop-health report. ``undefined`` = never fetched, ``null`` =
// fetched-but-empty/failed (the banner degrades gracefully on null).
let _healthCache;
let _healthLoading = false;

export function resetWorkspaceCache() {
  _workspaceCache = null;
  _workspaceLoading = false;
  _healthCache = undefined;
  _healthLoading = false;
}

export function workspacePayload() { return _workspaceCache; }

async function ensureWorkspace(repaint) {
  if (_workspaceCache || _workspaceLoading) return _workspaceCache;
  _workspaceLoading = true;
  try {
    const data = await fetchJson('/api/workspace');
    if (data && typeof data === 'object') {
      _workspaceCache = data;
    } else {
      _workspaceCache = { epochs: [], current_epoch_id: null };
    }
  } catch {
    _workspaceCache = { epochs: [], current_epoch_id: null };
  } finally {
    _workspaceLoading = false;
    if (typeof repaint === 'function') repaint();
  }
  return _workspaceCache;
}

async function ensureHealth(repaint) {
  if (_healthCache !== undefined || _healthLoading) return _healthCache;
  _healthLoading = true;
  try {
    const data = await fetchJson('/api/health-report');
    _healthCache = (data && typeof data === 'object') ? data : null;
  } catch {
    _healthCache = null;
  } finally {
    _healthLoading = false;
    if (typeof repaint === 'function') repaint();
  }
  return _healthCache;
}

function _fmtScalar(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  return v.toFixed(3);
}

// -- Loop-health banner ------------------------------------------------
function _renderHealthBanner() {
  // ``_healthCache === undefined`` → fetch still in flight; render the
  // banner's own muted "not yet evaluated" state by passing null.
  const report = _healthCache === undefined ? null : _healthCache;
  return healthBanner({ report });
}

// -- Environment + workspace totals -----------------------------------
function _renderEnvKv() {
  const ws = state.workspace;
  // null/undefined → still waiting for the SSE snapshot or /api/environment
  // to land. Saying "No environment loaded." here is misleading; it
  // implies the workspace is empty when in fact the dashboard simply
  // has not seen the first heartbeat yet.
  if (ws == null) return renderLoadingState({ label: 'Loading environment' });
  if (typeof ws === 'string') {
    return el('div', { class: 'kv-list' }, [
      el('div', { class: 'kv-list-key' }, ['root']),
      el('div', { class: 'kv-list-value' }, [ws]),
    ]);
  }
  if (typeof ws !== 'object') {
    return el('p', { class: 'empty' }, ['No environment fields.']);
  }
  // Identity strip — the durable "where am I" facts. We keep the load-
  // bearing identifiers (entrypoint, instance, roots, mutation count)
  // and drop the path triplet that the L1+ views already surface, so
  // the strip stays a tight identity card next to the ribbon.
  const ordered = [
    ['root', ws.root],
    ['adk_entrypoint', ws.adk_entrypoint],
    ['instance_id', ws.instance_id],
    ['created_at', ws.created_at],
    ['mutation_point_count',
      typeof ws.mutation_point_count === 'number' ? ws.mutation_point_count : null],
  ];
  const wrap = el('div', { class: 'kv-list' });
  for (const [k, v] of ordered) {
    wrap.appendChild(el('div', { class: 'kv-list-key' }, [k]));
    wrap.appendChild(el('div', { class: 'kv-list-value' },
      [v == null || v === '' ? '—' : String(v)]));
  }
  const roots = Array.isArray(ws.source_roots) ? ws.source_roots : [];
  if (roots.length > 0) {
    wrap.appendChild(el('div', { class: 'kv-list-key' }, ['source_roots']));
    const list = el('div', { class: 'kv-list-value' });
    for (let i = 0; i < roots.length; i += 1) {
      list.appendChild(el('div', null, [roots[i]]));
    }
    wrap.appendChild(list);
  }
  return wrap;
}

// "Workspace at a glance" — a tile strip with workspace-wide totals
// computed from the lineage payload. The sidebar owns live-run state, so
// L0 main is strictly summary.
function _renderGlanceBody() {
  if (_workspaceCache == null) {
    return renderLoadingState({ label: 'Loading totals' });
  }
  const payload = _workspaceCache;
  const rows = Array.isArray(payload.epochs) ? payload.epochs : [];
  if (rows.length === 0) {
    return renderEmptyState('No epochs recorded.');
  }
  let totalGens = 0;
  let totalPromoted = 0;
  let openEpochs = 0;
  let closedEpochs = 0;
  for (const r of rows) {
    if (!r) continue;
    totalGens += (typeof r.generation_count === 'number') ? r.generation_count : 0;
    totalPromoted += (typeof r.promoted_count === 'number') ? r.promoted_count : 0;
    if (r.closed) {
      closedEpochs += 1;
    } else {
      openEpochs += 1;
    }
  }
  const current = payload.current_epoch_id || null;
  const currentRow = current
    ? rows.find((r) => r && r.epoch_id === current) || null
    : null;
  const currentBest = currentRow
    ? _fmtScalar(currentRow.best_scalar) : '—';

  const tiles = el('div', { class: 'tile-strip' }, [
    renderMetricTile({ label: 'epochs', value: rows.length }),
    renderMetricTile({ label: 'generations', value: totalGens }),
    renderMetricTile({ label: 'promoted', value: totalPromoted }),
  ]);
  const meta = el('div', { class: 'phase0-glance-meta' }, [
    el('div', { class: 'phase0-glance-line' }, [
      el('span', { class: 'phase0-glance-label' }, ['open']),
      el('span', { class: 'mono' }, [String(openEpochs)]),
      el('span', { class: 'phase0-glance-sep' }, ['·']),
      el('span', { class: 'phase0-glance-label' }, ['closed']),
      el('span', { class: 'mono' }, [String(closedEpochs)]),
    ]),
    el('div', { class: 'phase0-glance-line' }, [
      el('span', { class: 'phase0-glance-label' }, ['current']),
      current
        ? el('a', {
            class: 'phase0-glance-current',
            href: phase0Href('epoch', { epochId: current }),
          }, [el('span', { class: 'mono' }, [current])])
        : el('span', { class: 'mono' }, ['—']),
      el('span', { class: 'phase0-glance-sep' }, ['·']),
      el('span', { class: 'phase0-glance-label' }, ['best']),
      el('span', { class: 'mono' }, [currentBest]),
    ]),
  ]);

  return el('div', { class: 'phase0-glance-body' }, [tiles, meta]);
}

function _renderEnvSection() {
  const node = $('phase0-workspace-env');
  if (!node) return;
  clearChildren(node);
  // Loop-health banner spans the full width at the very top, then the
  // two-card identity strip: Environment (left) + Workspace at a glance
  // (right).
  node.appendChild(el('div', { class: 'phase0-health-banner-wrap' }, [
    _renderHealthBanner(),
  ]));
  const strip = el('div', { class: 'phase0-view-strip phase0-view-strip-2' }, [
    renderCard({
      title: 'Environment',
      body: _renderEnvKv(),
    }),
    renderCard({
      title: 'Workspace at a glance',
      subtitle: 'Lifetime totals across every epoch.',
      body: _renderGlanceBody(),
    }),
  ]);
  node.appendChild(strip);
}

// -- Lineage ribbon (epoch zoom) --------------------------------------
//
// Builds ribbon nodes from the workspace epochs. The ribbon's geometry
// (x = order, y = scalar) makes this single picture do the work of the
// old timeline AND the old cross-epoch sparkline.
//
//   * id       — epoch_id (what onSelect drills into)
//   * parentId — parent_epoch_id (lineage edges)
//   * scalar   — best_scalar (drives the y-trajectory)
//   * verdict  — 'promoted' when the epoch promoted anything, else
//                'open' (epochs do not get "rejected"); the live epoch
//                additionally carries live:true so the ribbon dashes its
//                final hop and pulses the node.
//   * live     — current_epoch_id
//   * label    — epoch_id (epoch zoom leads with the id + its scalar)
function _ribbonNodes() {
  const payload = _workspaceCache;
  const rows = Array.isArray(payload && payload.epochs) ? payload.epochs : [];
  const cur = payload ? payload.current_epoch_id : null;
  return rows
    .filter((r) => r && r.epoch_id != null)
    .map((r) => ({
      id: r.epoch_id,
      parentId: r.parent_epoch_id != null ? r.parent_epoch_id : null,
      scalar: (typeof r.best_scalar === 'number' && isFinite(r.best_scalar))
        ? r.best_scalar : null,
      verdict: (r.promoted_count || 0) > 0 ? 'promoted' : 'open',
      live: r.epoch_id === cur,
      label: r.epoch_id,
    }));
}

function _renderLineageBody() {
  // Null cache = the /api/workspace fetch is still in flight.
  if (_workspaceCache == null) {
    return renderLoadingState({ label: 'Loading lineage' });
  }
  const nodes = _ribbonNodes();
  if (nodes.length === 0) {
    return renderEmptyState('No epochs recorded.');
  }
  return lineageRibbon({
    nodes,
    zoom: 'epochs',
    onSelect: (epochId) => {
      // Drill into the epoch (L1) via the hash router — the same
      // ``phase0Href`` contract the rest of L0 uses for its links.
      if (epochId == null) return;
      if (typeof window !== 'undefined' && window.location) {
        window.location.hash = phase0Href('epoch', { epochId });
      }
    },
  });
}

function _renderLineageSection() {
  const node = $('phase0-workspace-lineage');
  if (!node) return;
  clearChildren(node);
  node.appendChild(renderCard({
    title: 'Epoch lineage',
    subtitle: 'Each epoch placed by its best scalar — lower sits higher, '
      + 'so the spine traces the optimization curve.',
    body: _renderLineageBody(),
  }));
}

// -- Removed: the standalone cross-epoch trend / sparkline card --------
//
// The ribbon's y-axis IS the trajectory, so a second sparkline would be
// redundant. The ``phase0-workspace-sparkline`` slot still exists in
// index.html (not ours to edit), so we explicitly clear it to leave no
// stale chrome behind across re-renders.
function _clearSparklineSection() {
  const node = $('phase0-workspace-sparkline');
  if (!node) return;
  clearChildren(node);
}

// -- Recent decisions --------------------------------------------------
//
// Pulls the most recent experiments off ``state.epochDef.experiments``,
// reverse-chronological, capped at 10. Each row is clickable → L2 for
// that generation, and wears the shared ``verdictGlyph`` mark. This is
// the CHRONOLOGICAL companion to the ribbon's topological view: the
// ribbon shows the whole-lineage shape; this shows the last N decisions
// with their scalar deltas.

const RECENT_DECISIONS_CAP = 10;

function _recentDecisionsRows() {
  const def = state.epochDef;
  if (!def || typeof def !== 'object') return [];
  const xs = Array.isArray(def.experiments) ? def.experiments : [];
  // Reverse so latest-first (experiments are appended in chronological
  // order in the journal); cap at 10 so the card stays a glance.
  return xs.slice().reverse().slice(0, RECENT_DECISIONS_CAP);
}

// Normalize a journal experiment's outcome to the shared verdict
// vocabulary ('promoted' | 'rejected' | 'open').
function _verdictKey(exp) {
  const out = exp.outcome && typeof exp.outcome === 'object' ? exp.outcome : {};
  const raw = exp.verdict
    || (typeof exp.outcome === 'string' ? exp.outcome : null)
    || out.tournament_decision
    || '';
  const v = String(raw).toLowerCase();
  if (v.startsWith('prom') || v === 'accepted') return 'promoted';
  if (v.startsWith('rej')) return 'rejected';
  return 'open';
}

function _recentDecisionsBody() {
  const rows = _recentDecisionsRows();
  if (state.epochDef == null) {
    return renderLoadingState({ label: 'Loading recent decisions' });
  }
  if (rows.length === 0) {
    return renderEmptyState('No recent decisions yet.');
  }
  const epochId = (state.epochDef && state.epochDef.epoch_id)
    || (state.heartbeat && state.heartbeat.epoch_id) || null;
  const list = el('div', { class: 'phase0-recent-decisions' });
  for (const exp of rows) {
    const genId = exp.generation_id || exp.gen_id || exp.id || '—';
    const out = exp.outcome && typeof exp.outcome === 'object' ? exp.outcome : {};
    const verdict = _verdictKey(exp);
    const scalarRaw = exp.scalar != null ? exp.scalar
      : (out.scalar_score_delta != null ? out.scalar_score_delta : null);
    const scalar = scalarRaw != null ? Number(scalarRaw) : null;
    const scalarFmt = scalar == null || !isFinite(scalar)
      ? '—'
      : (scalar > 0 ? '+' : '') + scalar.toFixed(2);
    const href = epochId
      ? phase0Href('generation', { epochId, generationId: genId })
      : phase0Href('workspace');
    list.appendChild(el('a', {
      class: 'phase0-recent-decisions-row',
      'data-variant': verdict,
      href,
      'data-gen-id': genId,
    }, [
      el('span', { class: 'phase0-recent-decisions-id' }, [genId]),
      // Shared verdict glyph (mark only — the row already labels the
      // generation; the word would crowd a glance row).
      el('span', { class: 'phase0-recent-decisions-mark' }, [
        verdictGlyph(verdict, { withLabel: false }),
      ]),
      el('span', { class: 'phase0-recent-decisions-verdict' }, [verdict]),
      el('span', { class: 'phase0-recent-decisions-scalar' }, [scalarFmt]),
    ]));
  }
  return list;
}

function _renderRecentDecisionsSection() {
  const node = $('phase0-workspace-recent');
  if (!node) return;
  clearChildren(node);
  const epochId = (state.epochDef && state.epochDef.epoch_id)
    || (state.heartbeat && state.heartbeat.epoch_id) || null;
  const footer = epochId
    ? el('a', {
        class: 'phase0-recent-decisions-allgens',
        href: phase0Href('epoch', { epochId }),
      }, ['view all generations →'])
    : null;
  node.appendChild(renderCard({
    title: 'RECENT DECISIONS',
    subtitle: 'Most recent experiments from the current epoch — the '
      + 'chronological view; the ribbon above is the topological one.',
    body: _recentDecisionsBody(),
    footer,
  }));
}

export function renderPhase0Workspace(repaint) {
  ensureWorkspace(repaint);
  ensureHealth(repaint);
  _renderEnvSection();
  _renderLineageSection();
  _clearSparklineSection();
  _renderRecentDecisionsSection();
}

// Exported for tests — the count of rows the card will render.
export function recentDecisionsCount() {
  return _recentDecisionsRows().length;
}

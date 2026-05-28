// views/phase0_workspace.js — L0 (workspace-level) view.
//
// The cross-environment overview: identity / environment card, a live-
// activity card with a liveness indicator, an epoch lineage timeline,
// and a cross-epoch best-scalar sparkline. The page is composed from
// the design-system components (card, tile, pill, sparkline,
// live indicator). Each section renders INTO its pre-existing slot
// (`phase0-workspace-env` etc.) so tests reading those slots see the
// expected text; the slots themselves were defined in index.html.

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';
import { phase0Href } from './phase0_router.js';
import { renderCard } from '../components/card.js';
import { renderPill } from '../components/pill.js';
import { renderSparkline } from '../components/sparkline.js';
import { renderLiveIndicator } from '../components/live_indicator.js';
import { renderMetricTile } from '../components/tile.js';
import { renderLoadingState, renderEmptyState } from '../components/loading.js';

// Cached workspace payload (per-tab; refetched when the L0 view opens).
let _workspaceCache = null;
let _workspaceLoading = false;

export function resetWorkspaceCache() {
  _workspaceCache = null;
  _workspaceLoading = false;
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

function _fmtScalar(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  return v.toFixed(3);
}

function _hasLiveHeartbeat() {
  const hb = state.heartbeat;
  if (!hb || typeof hb !== 'object') return false;
  if (!hb.epoch_id && !hb.generation_id) return false;
  return true;
}

// -- Environment + Live Activity ---------------------------------------
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
  // Phase 1 tests look for specific values rendered as text — entrypoint,
  // source roots, mutation count, instance id. We preserve those.
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

function _renderEnvSection() {
  const node = $('phase0-workspace-env');
  if (!node) return;
  clearChildren(node);
  // The L0 page now uses a two-card top strip. The environment slot
  // renders the env card; the lineage slot below renders the lineage
  // + the live-activity panel + the trend (so we keep three slots).
  // For env we render a card-wrapped kv block.
  const live = _hasLiveHeartbeat();
  // Heartbeat has not arrived yet — distinct from "heartbeat arrived
  // but reports no active run". The latter is genuinely empty; the
  // former is loading.
  const heartbeatLoading = state.heartbeat == null;
  const hb = state.heartbeat || {};
  const sparkPts = (_workspaceCache && Array.isArray(_workspaceCache.sparkline))
    ? _workspaceCache.sparkline : [];
  const sparkValues = sparkPts.map((p) => p && p.scalar)
    .filter((v) => typeof v === 'number' && isFinite(v));

  // Live activity column body.
  const liveChildren = [];
  liveChildren.push(el('div', { class: 'phase0-live-header' }, [
    renderLiveIndicator({ live, label: live ? 'live' : 'idle' }),
    renderPill(live ? 'active' : 'no run', live ? 'live' : 'stale'),
  ]));
  if (heartbeatLoading) {
    liveChildren.push(renderLoadingState({ label: 'Loading live activity' }));
  } else if (live) {
    if (hb.epoch_id) {
      liveChildren.push(el('div', { class: 'phase0-live-line' }, [
        el('span', { class: 'phase0-live-line-label' }, ['epoch']),
        el('span', { class: 'mono' }, [hb.epoch_id]),
      ]));
    }
    if (hb.generation_id) {
      liveChildren.push(el('div', { class: 'phase0-live-line' }, [
        el('span', { class: 'phase0-live-line-label' }, ['gen']),
        el('span', { class: 'mono' }, [hb.generation_id]),
      ]));
    }
    const activeRuns = Array.isArray(state.activeRuns) ? state.activeRuns.length : 0;
    liveChildren.push(el('div', { class: 'phase0-live-line' }, [
      el('span', { class: 'phase0-live-line-label' }, ['runs']),
      el('span', { class: 'mono' }, [String(activeRuns)]),
    ]));
    if (hb.epoch_id) {
      liveChildren.push(el('a', {
        class: 'phase0-live-jump',
        href: phase0Href('epoch', { epochId: hb.epoch_id }),
      }, ['jump to current epoch →']));
    }
  } else {
    liveChildren.push(renderEmptyState('No active run.'));
  }
  if (sparkValues.length >= 2) {
    liveChildren.push(el('div', {
      style: 'margin-top:var(--space-2); display:flex; align-items:center; gap:var(--space-2);',
    }, [
      el('span', { class: 'phase0-live-line-label' }, ['trend']),
      renderSparkline(sparkValues, {
        width: 120, height: 22, ariaLabel: 'cross-epoch best-scalar trend',
      }),
    ]));
  }

  const strip = el('div', { class: 'phase0-view-strip phase0-view-strip-2' }, [
    renderCard({
      title: 'Environment',
      body: _renderEnvKv(),
    }),
    renderCard({
      title: 'Live activity',
      accent: live ? 'accent' : 'default',
      body: el('div', { class: 'phase0-live-body' }, liveChildren),
    }),
  ]);
  node.appendChild(strip);
}

// -- Epoch lineage -----------------------------------------------------
function _renderLineageTimeline() {
  // The workspace cache is the source of truth for the lineage. Null
  // means the /api/workspace fetch is still in flight; an empty epochs
  // array means the workspace is loaded but actually has no epochs.
  if (_workspaceCache == null) {
    return renderLoadingState({ label: 'Loading lineage' });
  }
  const payload = _workspaceCache;
  const rows = Array.isArray(payload.epochs) ? payload.epochs : [];
  if (rows.length === 0) {
    return renderEmptyState('No epochs recorded.');
  }
  // Build a child-of map so the lineage column can carry an "→ <child>"
  // arrow (the Phase 1 test asserts this exact text).
  const childOf = new Map();
  for (const r of rows) {
    if (r && r.parent_epoch_id && r.epoch_id) {
      if (!childOf.has(r.parent_epoch_id)) {
        childOf.set(r.parent_epoch_id, r.epoch_id);
      }
    }
  }
  const cur = payload.current_epoch_id;
  const tl = el('div', { class: 'epoch-timeline' });
  for (const r of rows) {
    const isLive = r.epoch_id === cur;
    const isClosed = !!r.closed;
    const hasPromoted = (r.promoted_count || 0) > 0;
    const variantClass = isLive ? 'is-live' : (hasPromoted ? 'is-promoted' : '');
    const statusPill = isLive
      ? renderPill('live', 'live')
      : (isClosed ? renderPill('closed', 'neutral') : renderPill('open', 'info'));
    const child = childOf.get(r.epoch_id);
    const arrowSuffix = child ? ' → ' + child : '';

    const item = el('a', {
      class: 'epoch-timeline-item ' + variantClass,
      href: phase0Href('epoch', { epochId: r.epoch_id }),
    }, [
      el('span', { class: 'epoch-timeline-dot', 'aria-hidden': 'true' }),
      el('div', null, [
        el('div', { class: 'epoch-timeline-name' }, [r.epoch_id + arrowSuffix]),
        el('div', { class: 'epoch-timeline-goal' },
          [r.goal || '(no goal recorded)']),
      ]),
      el('span', { class: 'epoch-timeline-scalar' },
        ['best · ' + _fmtScalar(r.best_scalar)
          + ' · ' + (r.generation_count || 0) + ' gens'
          + ' · ' + (r.promoted_count || 0) + ' promoted']),
      el('span', { class: 'epoch-timeline-status' }, [statusPill]),
    ]);
    tl.appendChild(item);
  }
  return tl;
}

function _renderLineageSection() {
  const node = $('phase0-workspace-lineage');
  if (!node) return;
  clearChildren(node);
  node.appendChild(renderCard({
    title: 'Epoch lineage',
    subtitle: 'The full history of this workspace, oldest to newest.',
    body: _renderLineageTimeline(),
  }));
}

// -- Cross-epoch trend -------------------------------------------------
function _renderTrendBody() {
  if (_workspaceCache == null) {
    return renderLoadingState({ label: 'Loading trend' });
  }
  const payload = _workspaceCache;
  const pts = Array.isArray(payload.sparkline) ? payload.sparkline : [];
  if (pts.length === 0) {
    return renderEmptyState('No scalar history yet.');
  }
  const values = pts.map((p) => p && p.scalar)
    .filter((v) => typeof v === 'number' && isFinite(v));
  const firstFinite = values.length > 0 ? values[0] : null;
  const lastFinite = values.length > 0 ? values[values.length - 1] : null;
  const delta = (firstFinite != null && lastFinite != null)
    ? lastFinite - firstFinite : null;
  const sentiment = delta == null ? 'flat'
    : (delta < 0 ? 'good' : (delta > 0 ? 'bad' : 'flat'));

  return el('div', { class: 'trend-card-body' }, [
    el('div', { class: 'trend-card-meta' }, [
      el('div', { class: 'tile-strip' }, [
        renderMetricTile({ label: 'epochs', value: pts.length }),
        renderMetricTile({ label: 'latest best', value: _fmtScalar(lastFinite) }),
        renderMetricTile({
          label: 'Δ vs first',
          value: delta == null ? '—' : (delta > 0 ? '+' : '') + delta.toFixed(3),
          sentiment,
        }),
      ]),
    ]),
    el('div', { class: 'trend-card-spark' }, [
      values.length >= 2
        ? renderSparkline(values, {
            width: 220, height: 56, ariaLabel: 'cross-epoch best-scalar curve',
            fill: 'color-mix(in srgb, var(--color-accent) 12%, transparent)',
          })
        : el('span', { class: 'empty' }, ['need ≥ 2 epochs for a trend line']),
    ]),
  ]);
}

function _renderSparklineSection() {
  const node = $('phase0-workspace-sparkline');
  if (!node) return;
  clearChildren(node);
  node.appendChild(renderCard({
    title: 'Cross-epoch trend',
    subtitle: 'Best (lowest) scalar per epoch — lower is better.',
    body: _renderTrendBody(),
  }));
}

export function renderPhase0Workspace(repaint) {
  ensureWorkspace(repaint);
  _renderEnvSection();
  _renderLineageSection();
  _renderSparklineSection();
}

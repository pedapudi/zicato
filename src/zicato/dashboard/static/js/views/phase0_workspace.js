// views/phase0_workspace.js — L0 (workspace-level) view.
//
// The cross-environment overview: identity / environment card, a
// "Workspace at a glance" tile strip with workspace-wide totals, an
// epoch lineage timeline, and a cross-epoch best-scalar sparkline.
//
// The in-content Live Activity card was retired once the redesigned
// sidebar (post-#198) made Live Activity a persistent fixture of the
// left rail. L0 main is now purely a workspace-level summary; the
// sidebar carries the live-run state across every level. Each section
// renders INTO its pre-existing slot (``phase0-workspace-env`` etc.)
// so tests reading those slots see the expected text; the slots
// themselves were defined in index.html.
//
// Layout (post-dedup):
//   * env slot — Environment card (left) + "Workspace at a glance"
//     totals tile (right) in a 2-up strip.
//   * lineage slot — full-width Epoch lineage card.
//   * sparkline slot — full-width Cross-epoch trend card with a wider
//     sparkline (the freed vertical space lets the curve breathe).

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';
import { phase0Href } from './phase0_router.js';
import { renderCard } from '../components/card.js';
import { renderPill } from '../components/pill.js';
import { renderSparkline } from '../components/sparkline.js';
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

// "Workspace at a glance" — a tile strip with workspace-wide totals
// computed from the lineage payload. Replaces the in-content Live
// Activity card that used to occupy this column; the sidebar owns
// live-run state, so L0 main is now strictly summary.
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
  // Two-card top strip: Environment (left) + Workspace at a glance
  // (right). The lineage slot below renders the lineage; the sparkline
  // slot at the bottom renders the trend (so we keep three slots).
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
      // The freed vertical space (the in-content Live Activity card
      // is gone) lets the trend sparkline take a noticeably wider
      // canvas — 360×72 reads as a real chart, not a thumbnail.
      values.length >= 2
        ? renderSparkline(values, {
            width: 360, height: 72, ariaLabel: 'cross-epoch best-scalar curve',
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

// -- Recent decisions --------------------------------------------------
//
// Pulls the most recent experiments off ``state.epochDef.experiments``,
// reverse-chronological, capped at 10. Each row is clickable → L2 for
// that generation. Card title "RECENT DECISIONS" with a "view all
// generations →" link to L1 in the card footer.

const RECENT_DECISIONS_CAP = 10;

function _recentDecisionsRows() {
  const def = state.epochDef;
  if (!def || typeof def !== 'object') return [];
  const xs = Array.isArray(def.experiments) ? def.experiments : [];
  // Reverse so latest-first (experiments are appended in chronological
  // order in the journal); cap at 10 so the card stays a glance.
  return xs.slice().reverse().slice(0, RECENT_DECISIONS_CAP);
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
    // ``outcome`` is the journal payload — an object with
    // ``tournament_decision`` + ``scalar_score_delta`` in production.
    // ``verdict`` is the short form used in tests + legacy fixtures.
    const out = exp.outcome && typeof exp.outcome === 'object'
      ? exp.outcome : {};
    const verdictRaw = exp.verdict
      || (typeof exp.outcome === 'string' ? exp.outcome : null)
      || out.tournament_decision
      || '';
    const verdict = String(verdictRaw).toLowerCase();
    const scalarRaw = exp.scalar != null ? exp.scalar
      : (out.scalar_score_delta != null ? out.scalar_score_delta : null);
    const scalar = scalarRaw != null ? Number(scalarRaw) : null;
    const scalarFmt = scalar == null || !isFinite(scalar)
      ? '—'
      : (scalar > 0 ? '+' : '') + scalar.toFixed(2);
    let mark = '·';
    let dataVariant = 'open';
    if (verdict.startsWith('prom') || verdict === 'accepted') {
      mark = '✓';
      dataVariant = 'promoted';
    } else if (verdict.startsWith('rej')) {
      mark = '✗';
      dataVariant = 'rejected';
    }
    const href = epochId
      ? phase0Href('generation', { epochId, generationId: genId })
      : phase0Href('workspace');
    list.appendChild(el('a', {
      class: 'phase0-recent-decisions-row',
      'data-variant': dataVariant,
      href,
      'data-gen-id': genId,
    }, [
      el('span', { class: 'phase0-recent-decisions-id' }, [genId]),
      el('span', { class: 'phase0-recent-decisions-mark' }, [mark]),
      el('span', { class: 'phase0-recent-decisions-verdict' },
        [verdict || 'open']),
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
    subtitle: 'Most recent experiments from the current epoch.',
    body: _recentDecisionsBody(),
    footer,
  }));
}

export function renderPhase0Workspace(repaint) {
  ensureWorkspace(repaint);
  _renderEnvSection();
  _renderLineageSection();
  _renderSparklineSection();
  _renderRecentDecisionsSection();
}

// Exported for tests — the count of rows the card will render.
export function recentDecisionsCount() {
  return _recentDecisionsRows().length;
}

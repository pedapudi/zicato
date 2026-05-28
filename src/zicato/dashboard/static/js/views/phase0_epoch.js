// views/phase0_epoch.js — L1 (epoch-level) view.
//
// Renders the epoch shell: goal header (now wired to ``epochs.goal``),
// contract diff vs predecessor (computed from
// /api/contract-diff/{epoch}), generation spine, per-entry × generation
// heatmap, per-judge × generation heatmap (now wired to
// /api/epoch/{id}/per-judge-trend), and the journal preview. The
// gauntlet renderer (renderBracket) is treated as a black box per the
// task #175 coordination — this view does NOT modify its internals;
// it can call it as-is when full integration lands.

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';

const _contractDiffCache = new Map(); // epochId -> payload
const _loadingDiff = new Set();

// Per-judge × generation matrix, keyed by epoch_id. Lazy fetch, mirrors
// the contract-diff cache shape so a re-render does not refetch.
const _perJudgeTrendCache = new Map();
const _loadingTrend = new Set();

export function resetPerJudgeTrendCache() {
  _perJudgeTrendCache.clear();
  _loadingTrend.clear();
}

export function perJudgeTrendPayload(epochId) {
  return _perJudgeTrendCache.get(epochId) || null;
}

async function ensurePerJudgeTrend(epochId, repaint) {
  if (!epochId) return null;
  if (_perJudgeTrendCache.has(epochId)) return _perJudgeTrendCache.get(epochId);
  if (_loadingTrend.has(epochId)) return null;
  _loadingTrend.add(epochId);
  try {
    const data = await fetchJson('/api/epoch/' + encodeURIComponent(epochId) + '/per-judge-trend');
    if (data && typeof data === 'object') {
      _perJudgeTrendCache.set(epochId, data);
    }
  } catch {
    _perJudgeTrendCache.set(epochId, {
      epoch_id: epochId, generations: [], judges: [],
    });
  } finally {
    _loadingTrend.delete(epochId);
    if (typeof repaint === 'function') repaint();
  }
  return _perJudgeTrendCache.get(epochId);
}

export function resetContractDiffCache() {
  _contractDiffCache.clear();
  _loadingDiff.clear();
}

export function contractDiffPayload(epochId) {
  return _contractDiffCache.get(epochId) || null;
}

async function ensureContractDiff(epochId, repaint) {
  if (!epochId) return null;
  if (_contractDiffCache.has(epochId)) return _contractDiffCache.get(epochId);
  if (_loadingDiff.has(epochId)) return null;
  _loadingDiff.add(epochId);
  try {
    const data = await fetchJson('/api/contract-diff/' + encodeURIComponent(epochId));
    if (data && typeof data === 'object') {
      _contractDiffCache.set(epochId, data);
    }
  } catch {
    _contractDiffCache.set(epochId, {
      epoch_id: epochId,
      predecessor_epoch_id: null,
      components: [],
      any_changed: false,
    });
  } finally {
    _loadingDiff.delete(epochId);
    if (typeof repaint === 'function') repaint();
  }
  return _contractDiffCache.get(epochId);
}

function renderGoal() {
  const node = $('phase0-epoch-goal');
  if (!node) return;
  clearChildren(node);
  const def = state.epochDef;
  const goal = (def && typeof def.goal === 'string') ? def.goal.trim() : '';
  if (goal) {
    node.appendChild(el('h3', { class: 'phase0-h3' }, ['Goal']));
    node.appendChild(el('p', { class: 'phase0-epoch-goal-text' }, [goal]));
    return;
  }
  // No goal recorded — surface the slot with a clearly labelled
  // placeholder and a hint that the goal is set by the operator at
  // epoch-creation time (``zicato epoch set-goal --epoch <id> --goal
  // "..."``). The inline-set affordance is intentionally NOT a wire-up
  // here: a write-side endpoint that mutates the on-disk
  // ``config.json`` would be a Phase 2 control surface (see #181's
  // control-channel design) and is not part of this light-up scope.
  node.appendChild(el('h3', { class: 'phase0-h3' }, ['Goal']));
  node.appendChild(el('p', { class: 'empty' }, ['(no goal recorded)']));
  node.appendChild(el('p', { class: 'panel-subheader' }, [
    'Set the goal via the CLI: ',
    el('code', { class: 'mono' }, [
      'zicato epoch set-goal --epoch <id> --goal "..."',
    ]),
  ]));
}

function renderContractDiff(epochId) {
  const node = $('phase0-epoch-contract-diff');
  if (!node) return;
  clearChildren(node);
  if (!epochId) {
    node.appendChild(el('p', { class: 'empty' }, ['Select an epoch from the workspace view.']));
    return;
  }
  const data = _contractDiffCache.get(epochId);
  if (!data) {
    node.appendChild(el('p', { class: 'empty' }, ['loading contract diff…']));
    return;
  }
  if (!data.predecessor_epoch_id) {
    node.appendChild(el('p', { class: 'empty' },
      ['No predecessor epoch on disk — first epoch in the workspace.']));
    return;
  }
  const tbl = el('table', { class: 'phase0-contract-diff' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['component']),
    el('th', null, ['previous (' + data.predecessor_epoch_id + ')']),
    el('th', null, ['current (' + epochId + ')']),
    el('th', null, ['changed']),
  ])]));
  const tbody = el('tbody');
  const comps = Array.isArray(data.components) ? data.components : [];
  for (const c of comps) {
    tbody.appendChild(el('tr', {
      class: c.changed ? 'phase0-contract-diff-changed' : '',
    }, [
      el('td', { class: 'mono' }, [c.name]),
      el('td', { class: 'mono' }, [c.previous_hash ? c.previous_hash.slice(0, 8) : '—']),
      el('td', { class: 'mono' }, [c.current_hash ? c.current_hash.slice(0, 8) : '—']),
      el('td', null, [c.changed ? 'yes' : 'no']),
    ]));
  }
  tbl.appendChild(tbody);
  node.appendChild(tbl);
}

function renderSpine() {
  const node = $('phase0-epoch-spine');
  if (!node) return;
  clearChildren(node);
  // Coordinated with task #175 (which is fixing renderBracket vertical
  // alignment): Phase 0 only LAYS OUT the slot for the gauntlet. The
  // hand-off lands when both branches integrate — the renderBracket
  // function is treated as a black box here.
  node.appendChild(el('p', { class: 'panel-subheader' },
    ['Generation spine (gauntlet) renders here once #175 lands.']));
}

function renderEntryHeatmap() {
  const node = $('phase0-epoch-heatmap-entries');
  if (!node) return;
  clearChildren(node);
  const def = state.epochDef;
  const experiments = (def && Array.isArray(def.experiments)) ? def.experiments : [];
  if (experiments.length === 0) {
    node.appendChild(el('p', { class: 'empty' }, ['No experiments yet.']));
    return;
  }
  // Minimal phase-0 placeholder: tabulate which experiments carry a
  // scalar delta so the operator can see the data dimension. The real
  // heatmap (per-entry × generation, sourced from loss_profiles) lands
  // as a downstream visual polish step.
  const tbl = el('table', { class: 'phase0-mini-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['gen']),
    el('th', null, ['Δscalar']),
    el('th', null, ['decision']),
  ])]));
  const tbody = el('tbody');
  for (const e of experiments) {
    const out = e.outcome || {};
    const ds = out.scalar_score_delta;
    const dec = out.tournament_decision || out.decision || '—';
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'mono' }, [e.generation_id || '—']),
      el('td', { class: 'mono' },
        [typeof ds === 'number' && isFinite(ds) ? ds.toFixed(3) : '—']),
      el('td', { class: 'mono' }, [String(dec)]),
    ]));
  }
  tbl.appendChild(tbody);
  node.appendChild(tbl);
}

function _fmtLoss(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  return v.toFixed(3);
}

function renderJudgeHeatmap(epochId) {
  const node = $('phase0-epoch-heatmap-judges');
  if (!node) return;
  clearChildren(node);
  if (!epochId) {
    node.appendChild(el('p', { class: 'empty' }, ['Select an epoch.']));
    return;
  }
  const data = _perJudgeTrendCache.get(epochId);
  if (!data) {
    node.appendChild(el('p', { class: 'empty' }, ['loading per-judge trend…']));
    return;
  }
  const generations = Array.isArray(data.generations) ? data.generations : [];
  const judges = Array.isArray(data.judges) ? data.judges : [];
  if (generations.length === 0 || judges.length === 0) {
    const msg = data.note
      ? '(no per-judge data: ' + data.note + ')'
      : '(no per-judge data recorded for this epoch yet)';
    node.appendChild(el('p', { class: 'empty' }, [msg]));
    return;
  }
  // Header row: blank judge column + one column per generation in
  // spine order. Cells are weighted losses; missing data renders "—".
  const tbl = el('table', { class: 'phase0-judge-heatmap' });
  const thead = el('thead');
  const headRow = el('tr', null, [el('th', null, ['judge'])]);
  for (const gid of generations) {
    headRow.appendChild(el('th', { class: 'mono' }, [gid]));
  }
  thead.appendChild(headRow);
  tbl.appendChild(thead);
  const tbody = el('tbody');
  for (const j of judges) {
    const tr = el('tr', null, [el('td', { class: 'mono' }, [j.judge_name || '—'])]);
    const byGen = j.by_generation || {};
    for (const gid of generations) {
      tr.appendChild(el('td', { class: 'mono' }, [_fmtLoss(byGen[gid])]));
    }
    tbody.appendChild(tr);
  }
  tbl.appendChild(tbody);
  node.appendChild(tbl);
  node.appendChild(el('p', { class: 'panel-subheader' }, [
    'Spine-only: rejected lineage is folded into the totals on the parent ',
    'generation where it was scored.',
  ]));
}

function renderJournal() {
  const node = $('phase0-epoch-journal');
  if (!node) return;
  clearChildren(node);
  const def = state.epochDef;
  const journal = def && typeof def.journal === 'string' ? def.journal : '';
  if (!journal.trim()) {
    node.appendChild(el('p', { class: 'empty' }, ['No journal preview.']));
    return;
  }
  const pre = el('pre', { class: 'phase0-journal-preview mono' },
    [journal.slice(0, 1200)]);
  node.appendChild(pre);
}

export function renderPhase0Epoch(params, repaint) {
  const epochId = (params && params.epochId)
    || (state.heartbeat && state.heartbeat.epoch_id)
    || (state.epochDef && state.epochDef.epoch_id)
    || null;
  ensureContractDiff(epochId, repaint);
  ensurePerJudgeTrend(epochId, repaint);
  renderGoal();
  renderContractDiff(epochId);
  renderSpine();
  renderEntryHeatmap();
  renderJudgeHeatmap(epochId);
  renderJournal();
}

// views/phase0_epoch.js — L1 (epoch-level) view.
//
// Renders the epoch shell: goal header (stub until #178 lands), contract
// diff vs predecessor (computed from /api/contract-diff/{epoch}), and
// shells for the generation spine + per-entry × generation heatmap +
// per-judge × generation heatmap (stub until #179 lands) and the journal
// preview. The gauntlet renderer (renderBracket) is treated as a black
// box per the task #175 coordination — this view does NOT modify its
// internals; it can call it as-is when full integration lands.

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';

const _contractDiffCache = new Map(); // epochId -> payload
const _loadingDiff = new Set();

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
  // Task #178 introduces a frozen ``epochs.goal`` column. Until it
  // lands, the goal-of-the-epoch is unrecorded — surface a clearly
  // labelled placeholder so the operator knows the slot exists.
  node.appendChild(el('p', { class: 'empty phase0-stub-msg' },
    ['(goal not yet recorded — populated once #178 lands)']));
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

function renderJudgeHeatmap() {
  const node = $('phase0-epoch-heatmap-judges');
  if (!node) return;
  clearChildren(node);
  node.appendChild(el('p', { class: 'empty phase0-stub-msg' },
    ['(per-judge × generation heatmap — populated once #179 lands)']));
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
  renderGoal();
  renderContractDiff(epochId);
  renderSpine();
  renderEntryHeatmap();
  renderJudgeHeatmap();
  renderJournal();
}

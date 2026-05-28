// views/phase0_run.js — L4 (run-level) view.
//
// Renders the per-run shell: header metrics, expectation outcomes,
// per-judge breakdown (via /api/run/{epoch}/{gen}/{entry}/per-judge),
// transcript (single-run layout for phase 0; side-by-side toggle is
// phase 2), and a live events stream. Live transcript rendering
// belongs ONLY at L4 per the design agreement — every other level
// shows summary data.

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';

const _runJudgeCache = new Map(); // "epoch/gen/entry" -> payload
const _loadingRunJudges = new Set();

export function resetRunCaches() {
  _runJudgeCache.clear();
  _loadingRunJudges.clear();
}

export function runJudgePayload(epochId, generationId, entryId) {
  return _runJudgeCache.get(epochId + '/' + generationId + '/' + entryId) || null;
}

async function ensureRunJudges(epochId, generationId, entryId, repaint) {
  if (!epochId || !generationId || !entryId) return null;
  const key = epochId + '/' + generationId + '/' + entryId;
  if (_runJudgeCache.has(key)) return _runJudgeCache.get(key);
  if (_loadingRunJudges.has(key)) return null;
  _loadingRunJudges.add(key);
  try {
    const data = await fetchJson('/api/run/'
      + encodeURIComponent(epochId) + '/'
      + encodeURIComponent(generationId) + '/'
      + encodeURIComponent(entryId) + '/per-judge');
    if (data && typeof data === 'object') _runJudgeCache.set(key, data);
  } catch {
    _runJudgeCache.set(key, { run_id: null, judges: [] });
  } finally {
    _loadingRunJudges.delete(key);
    if (typeof repaint === 'function') repaint();
  }
  return _runJudgeCache.get(key);
}

function _fmtNum(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  return v.toFixed(3);
}

function findActiveRun(entryId) {
  if (!entryId) return null;
  const runs = Array.isArray(state.activeRuns) ? state.activeRuns : [];
  for (const r of runs) {
    if (r && r.entry_id === entryId) return r;
  }
  return null;
}

function renderRunHeaderMetrics(params, run) {
  const node = $('phase0-run-header');
  if (!node) return;
  clearChildren(node);
  if (!params || (!params.entryId && !params.generationId)) {
    node.appendChild(el('p', { class: 'empty' }, ['No run selected.']));
    return;
  }
  const wrap = el('div', { class: 'phase0-run-header-inner' });
  if (params.epochId) {
    wrap.appendChild(el('div', { class: 'mono' },
      ['epoch · ', params.epochId]));
  }
  if (params.generationId) {
    wrap.appendChild(el('div', { class: 'mono' },
      ['gen · ', params.generationId]));
  }
  if (params.entryId) {
    wrap.appendChild(el('div', { class: 'mono' },
      ['entry · ', params.entryId]));
  }
  if (run) {
    if (typeof run.progress === 'number') {
      wrap.appendChild(el('div', { class: 'mono' },
        ['progress · ', String(Math.round((run.progress || 0) * 100)), '%']));
    }
    if (typeof run.elapsed_seconds === 'number') {
      wrap.appendChild(el('div', { class: 'mono' },
        ['elapsed · ', String(Math.round(run.elapsed_seconds)), 's']));
    }
    if (run.status) {
      wrap.appendChild(el('div', { class: 'mono' },
        ['status · ', String(run.status)]));
    }
  } else {
    wrap.appendChild(el('p', { class: 'panel-subheader' },
      ['Run is not currently active — historical metrics land once L4 fetches them.']));
  }
  node.appendChild(wrap);
}

function renderExpectation() {
  const node = $('phase0-run-expectation');
  if (!node) return;
  clearChildren(node);
  // The expectation outcome is sourced from the per-run loss.json,
  // which the matchup-conversations endpoint already projects. Phase 0
  // surfaces the slot; the wire-up lands once the L4 fetch path
  // migrates from the legacy conversation view.
  node.appendChild(el('p', { class: 'panel-subheader' },
    ['Expectation outcomes land once the L4 fetch path migrates.']));
}

function renderJudges(epochId, generationId, entryId) {
  const node = $('phase0-run-judges');
  if (!node) return;
  clearChildren(node);
  if (!epochId || !generationId || !entryId) {
    node.appendChild(el('p', { class: 'empty' }, ['No run selected.']));
    return;
  }
  const data = _runJudgeCache.get(epochId + '/' + generationId + '/' + entryId);
  if (!data) {
    node.appendChild(el('p', { class: 'empty' }, ['loading per-judge breakdown…']));
    return;
  }
  const judges = Array.isArray(data.judges) ? data.judges : [];
  if (judges.length === 0) {
    const msg = data.note ? '(no per-judge data: ' + data.note + ')'
      : '(no per-judge data recorded for this run)';
    node.appendChild(el('p', { class: 'empty' }, [msg]));
    return;
  }
  if (data.run_id) {
    node.appendChild(el('p', { class: 'panel-subheader mono' },
      ['run_id · ', data.run_id]));
  }
  const tbl = el('table', { class: 'phase0-mini-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['judge']),
    el('th', null, ['weighted loss']),
    el('th', null, ['raw loss']),
    el('th', null, ['weight']),
  ])]));
  const tbody = el('tbody');
  for (const j of judges) {
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'mono' }, [String(j.judge_name || '—')]),
      el('td', { class: 'mono' }, [_fmtNum(j.weighted_loss)]),
      el('td', { class: 'mono' }, [_fmtNum(j.raw_loss)]),
      el('td', { class: 'mono' }, [_fmtNum(j.weight)]),
    ]));
  }
  tbl.appendChild(tbody);
  node.appendChild(tbl);
}

function renderTranscript() {
  const node = $('phase0-run-transcript');
  if (!node) return;
  clearChildren(node);
  node.appendChild(el('p', { class: 'panel-subheader' },
    ['Transcript renders here (single-run layout for phase 0; '
      + 'side-by-side compare toggles in phase 2).']));
}

function renderEvents() {
  const node = $('phase0-run-events');
  if (!node) return;
  clearChildren(node);
  // Reuse the same run-log tail the legacy log panel reads from. The
  // live transcript only belongs at L4 per the design agreement;
  // every other level shows summary data.
  const events = (state.logTail && Array.isArray(state.logTail.events))
    ? state.logTail.events.slice(-12) : [];
  if (events.length === 0) {
    node.appendChild(el('p', { class: 'empty' }, ['No events yet.']));
    return;
  }
  const list = el('div', { class: 'phase0-events-list mono' });
  for (const ev of events) {
    const line = el('div', { class: 'phase0-events-line' }, [
      ev.kind || '—',
      ' · ',
      ev.summary || '',
    ]);
    list.appendChild(line);
  }
  node.appendChild(list);
}

export function renderPhase0Run(params, repaint) {
  const run = findActiveRun(params && params.entryId);
  const epochId = (params && params.epochId) || null;
  const generationId = (params && params.generationId) || null;
  const entryId = (params && params.entryId) || null;
  ensureRunJudges(epochId, generationId, entryId, repaint);
  renderRunHeaderMetrics(params, run);
  renderExpectation();
  renderJudges(epochId, generationId, entryId);
  renderTranscript();
  renderEvents();
}

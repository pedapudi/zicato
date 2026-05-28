// views/phase0_run.js — L4 (run-level) view.
//
// Header slot: metric tile strip with the run's headline numbers.
// Expectation slot: pass/fail outcomes (Phase 1.5 owns inner structure).
// Judges slot: per-judge weighted-loss table.
// Transcript slot: Phase 1.5 owns inner structure.
// Events slot: event chip stream with colored type chips.

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';
import { renderCard } from '../components/card.js';
import { renderMetricTile } from '../components/tile.js';
import { renderPill, renderEventChip } from '../components/pill.js';

const _runJudgeCache = new Map();
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

function _renderHeader(params, run) {
  const node = $('phase0-run-header');
  if (!node) return;
  clearChildren(node);
  if (!params || (!params.entryId && !params.generationId)) {
    node.appendChild(renderCard({
      title: 'Run',
      body: el('p', { class: 'empty' }, ['No run selected.']),
    }));
    return;
  }
  const tiles = el('div', { class: 'tile-strip' });
  if (params.epochId) {
    tiles.appendChild(renderMetricTile({
      label: 'epoch', value: params.epochId, size: 'sm',
    }));
  }
  if (params.generationId) {
    tiles.appendChild(renderMetricTile({
      label: 'generation', value: params.generationId, size: 'sm',
    }));
  }
  if (params.entryId) {
    tiles.appendChild(renderMetricTile({
      label: 'entry', value: params.entryId, size: 'sm',
    }));
  }
  if (run && typeof run.elapsed_seconds === 'number') {
    tiles.appendChild(renderMetricTile({
      label: 'elapsed',
      value: Math.round(run.elapsed_seconds),
      unit: 's',
    }));
  }
  if (run && typeof run.progress === 'number') {
    tiles.appendChild(renderMetricTile({
      label: 'progress',
      value: Math.round((run.progress || 0) * 100),
      unit: '%',
    }));
  }
  if (run && run.status) {
    tiles.appendChild(renderMetricTile({
      label: 'status', value: run.status, size: 'sm',
    }));
  }

  const body = el('div');
  body.appendChild(tiles);
  if (!run) {
    body.appendChild(el('p', {
      style: 'margin:var(--space-3) 0 0; font-size:var(--font-size-12); color:var(--color-text-muted);',
    }, ['Run is not currently active — historical metrics land once L4 fetches them.']));
  } else if (run.status === 'running') {
    body.appendChild(el('div', {
      style: 'margin-top:var(--space-3); display:flex; gap:var(--space-2);',
    }, [renderPill('live', 'live')]));
  }
  node.appendChild(renderCard({
    title: 'Run header',
    body,
  }));
}

function _renderExpectation() {
  const node = $('phase0-run-expectation');
  if (!node) return;
  clearChildren(node);
  // Phase 1.5 owns the inner structure. We card-wrap for layout.
  node.appendChild(renderCard({
    title: 'Expectations',
    body: el('p', { class: 'empty' },
      ['Expectation outcomes land once the L4 fetch path migrates (Phase 1.5).']),
  }));
}

function _renderJudges(epochId, generationId, entryId) {
  const node = $('phase0-run-judges');
  if (!node) return;
  clearChildren(node);
  let body;
  if (!epochId || !generationId || !entryId) {
    body = el('p', { class: 'empty' }, ['No run selected.']);
  } else {
    const data = _runJudgeCache.get(epochId + '/' + generationId + '/' + entryId);
    if (!data) {
      body = el('p', { class: 'empty' }, ['loading per-judge breakdown…']);
    } else {
      const judges = Array.isArray(data.judges) ? data.judges : [];
      if (judges.length === 0) {
        const msg = data.note ? '(no per-judge data: ' + data.note + ')'
          : '(no per-judge data recorded for this run)';
        body = el('p', { class: 'empty' }, [msg]);
      } else {
        const wrap = el('div');
        if (data.run_id) {
          wrap.appendChild(el('p', {
            style: 'font-size:var(--font-size-11); color:var(--color-text-muted); margin:0 0 var(--space-2); font-family:var(--font-mono);',
          }, ['run · ', data.run_id]));
        }
        const tbl = el('table', { class: 'ds-table' });
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
        wrap.appendChild(tbl);
        body = wrap;
      }
    }
  }
  node.appendChild(renderCard({
    title: 'Per-judge breakdown',
    body,
  }));
}

function _renderTranscript() {
  const node = $('phase0-run-transcript');
  if (!node) return;
  clearChildren(node);
  node.appendChild(renderCard({
    title: 'Transcript',
    body: el('p', { class: 'empty' },
      ['Transcript renders here (single-run for phase 0; side-by-side compare in phase 2).']),
  }));
}

function _renderEvents() {
  const node = $('phase0-run-events');
  if (!node) return;
  clearChildren(node);
  const events = (state.logTail && Array.isArray(state.logTail.events))
    ? state.logTail.events.slice(-30) : [];
  let body;
  if (events.length === 0) {
    body = el('p', { class: 'empty' }, ['No events yet.']);
  } else {
    const list = el('div', { class: 'events-list' });
    for (const ev of events) {
      const ts = ev.ts || ev.timestamp || '';
      list.appendChild(el('div', { class: 'events-row' }, [
        renderEventChip(ev.kind || 'event'),
        el('span', { class: 'events-row-ts' }, [String(ts).slice(11, 19)]),
        el('span', { class: 'events-row-summary' }, [ev.summary || '']),
      ]));
    }
    body = list;
  }
  node.appendChild(renderCard({
    title: 'Events stream',
    body,
  }));
}

export function renderPhase0Run(params, repaint) {
  const run = findActiveRun(params && params.entryId);
  const epochId = (params && params.epochId) || null;
  const generationId = (params && params.generationId) || null;
  const entryId = (params && params.entryId) || null;
  ensureRunJudges(epochId, generationId, entryId, repaint);
  _renderHeader(params, run);
  _renderExpectation();
  _renderJudges(epochId, generationId, entryId);
  _renderTranscript();
  _renderEvents();
}

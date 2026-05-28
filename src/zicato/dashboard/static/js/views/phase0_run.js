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
const _runExpectationsCache = new Map(); // same key -> payload
const _loadingRunExpectations = new Set();
const _runHeaderCache = new Map(); // same key -> payload
const _loadingRunHeader = new Set();

export function resetRunCaches() {
  _runJudgeCache.clear();
  _loadingRunJudges.clear();
  _runExpectationsCache.clear();
  _loadingRunExpectations.clear();
  _runHeaderCache.clear();
  _loadingRunHeader.clear();
}

export function runJudgePayload(epochId, generationId, entryId) {
  return _runJudgeCache.get(epochId + '/' + generationId + '/' + entryId) || null;
}

export function runExpectationsPayload(epochId, generationId, entryId) {
  return _runExpectationsCache.get(
    epochId + '/' + generationId + '/' + entryId,
  ) || null;
}

export function runHeaderPayload(epochId, generationId, entryId) {
  return _runHeaderCache.get(
    epochId + '/' + generationId + '/' + entryId,
  ) || null;
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

async function ensureRunExpectations(epochId, generationId, entryId, repaint) {
  if (!epochId || !generationId || !entryId) return null;
  const key = epochId + '/' + generationId + '/' + entryId;
  if (_runExpectationsCache.has(key)) return _runExpectationsCache.get(key);
  if (_loadingRunExpectations.has(key)) return null;
  _loadingRunExpectations.add(key);
  try {
    const data = await fetchJson('/api/run/'
      + encodeURIComponent(epochId) + '/'
      + encodeURIComponent(generationId) + '/'
      + encodeURIComponent(entryId) + '/expectations');
    if (data && typeof data === 'object') _runExpectationsCache.set(key, data);
  } catch {
    _runExpectationsCache.set(key, {
      epoch_id: epochId,
      generation_id: generationId,
      entry_id: entryId,
      outcomes: [],
    });
  } finally {
    _loadingRunExpectations.delete(key);
    if (typeof repaint === 'function') repaint();
  }
  return _runExpectationsCache.get(key);
}

async function ensureRunHeader(epochId, generationId, entryId, repaint) {
  if (!epochId || !generationId || !entryId) return null;
  const key = epochId + '/' + generationId + '/' + entryId;
  if (_runHeaderCache.has(key)) return _runHeaderCache.get(key);
  if (_loadingRunHeader.has(key)) return null;
  _loadingRunHeader.add(key);
  try {
    const data = await fetchJson('/api/run/'
      + encodeURIComponent(epochId) + '/'
      + encodeURIComponent(generationId) + '/'
      + encodeURIComponent(entryId) + '/header');
    if (data && typeof data === 'object') _runHeaderCache.set(key, data);
  } catch {
    _runHeaderCache.set(key, {
      epoch_id: epochId,
      generation_id: generationId,
      entry_id: entryId,
      drift_loss: null, pass_fail: null, runtime_ms: null,
      tokens_spent: null, output_chars: null, turns_completed: null,
      plan_revisions: null, wall_clock_budget_exceeded: null, run_id: null,
    });
  } finally {
    _loadingRunHeader.delete(key);
    if (typeof repaint === 'function') repaint();
  }
  return _runHeaderCache.get(key);
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

function _fmtDuration(ms) {
  if (typeof ms !== 'number' || !isFinite(ms)) return '—';
  if (ms < 1000) return Math.round(ms) + ' ms';
  if (ms < 60_000) return (ms / 1000).toFixed(1) + ' s';
  const totalSecs = Math.round(ms / 1000);
  const mins = Math.floor(totalSecs / 60);
  const secs = totalSecs % 60;
  return mins + ' m ' + secs + ' s';
}

function _fmtInt(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  return String(v);
}

function _fmtPassFail(v) {
  if (v === true) return 'PASS';
  if (v === false) return 'FAIL';
  return '—';
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
    // Completed run — pull the loss.json-backed header payload and fan
    // its metrics into the same tile-strip shape used by live runs.
    const header = _runHeaderCache.get(
      (params.epochId || '') + '/' + (params.generationId || '')
        + '/' + (params.entryId || ''),
    );
    if (!header) {
      body.appendChild(el('p', { class: 'panel-subheader' },
        ['loading run header…']));
    } else if (header.run_id == null && header.runtime_ms == null) {
      body.appendChild(el('p', {
        style: 'margin:var(--space-3) 0 0; font-size:var(--font-size-12); color:var(--color-text-muted);',
      }, ['No completed-run metrics recorded for this entry yet.']));
    } else {
      const completed = el('div', { class: 'tile-strip' });
      const verdictCls = header.pass_fail === true ? 'good'
        : header.pass_fail === false ? 'bad' : '';
      completed.appendChild(renderMetricTile({
        label: 'verdict',
        value: _fmtPassFail(header.pass_fail),
        emphasis: verdictCls,
      }));
      completed.appendChild(renderMetricTile({
        label: 'drift loss',
        value: (typeof header.drift_loss === 'number' && isFinite(header.drift_loss))
          ? header.drift_loss.toFixed(3) : '—',
      }));
      completed.appendChild(renderMetricTile({
        label: 'runtime', value: _fmtDuration(header.runtime_ms),
      }));
      completed.appendChild(renderMetricTile({
        label: 'tokens', value: _fmtInt(header.tokens_spent),
      }));
      completed.appendChild(renderMetricTile({
        label: 'output chars', value: _fmtInt(header.output_chars),
      }));
      completed.appendChild(renderMetricTile({
        label: 'turns', value: _fmtInt(header.turns_completed),
      }));
      completed.appendChild(renderMetricTile({
        label: 'plan revisions', value: _fmtInt(header.plan_revisions),
      }));
      body.appendChild(completed);
      if (header.wall_clock_budget_exceeded === true) {
        body.appendChild(el('p', { class: 'panel-subheader bad' }, [
          'Wall-clock budget exceeded — the run was force-aborted.',
        ]));
      }
      if (header.run_id) {
        body.appendChild(el('p', {
          style: 'margin:var(--space-2) 0 0; font-size:var(--font-size-11); color:var(--color-text-muted); font-family:var(--font-mono);',
        }, ['run_id · ', header.run_id]));
      }
    }
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

function _renderExpectation(epochId, generationId, entryId) {
  const node = $('phase0-run-expectation');
  if (!node) return;
  clearChildren(node);
  let body;
  if (!epochId || !generationId || !entryId) {
    body = el('p', { class: 'empty' }, ['No run selected.']);
  } else {
    const data = _runExpectationsCache.get(
      epochId + '/' + generationId + '/' + entryId,
    );
    if (!data) {
      body = el('p', { class: 'empty' }, ['loading expectations…']);
    } else {
      const outcomes = Array.isArray(data.outcomes) ? data.outcomes : [];
      if (outcomes.length === 0) {
        body = el('p', { class: 'empty' },
          ['(no expectations recorded for this run)']);
      } else {
        const tbl = el('table', { class: 'ds-table' });
        tbl.appendChild(el('thead', null, [el('tr', null, [
          el('th', null, ['expectation']),
          el('th', null, ['kind']),
          el('th', null, ['verdict']),
          el('th', null, ['notes (judge / score if Rubric)']),
        ])]));
        const tbody = el('tbody');
        outcomes.forEach((o, idx) => {
          const verdict = o.passed === true ? 'PASS'
            : o.passed === false ? 'FAIL' : '—';
          const verdictCls = o.passed === true ? 'good'
            : o.passed === false ? 'bad' : '';
          const notesBits = [];
          if (o.judge_name) notesBits.push('judge: ' + o.judge_name);
          if (typeof o.score === 'number' && isFinite(o.score)) {
            notesBits.push('score: ' + o.score.toFixed(3));
          }
          if (o.detail) notesBits.push(o.detail);
          const notes = notesBits.length ? notesBits.join(' · ') : '—';
          tbody.appendChild(el('tr', null, [
            el('td', { class: 'mono' }, ['#' + String(idx + 1)]),
            el('td', { class: 'mono' }, [String(o.kind || '—')]),
            el('td', { class: 'mono ' + verdictCls }, [verdict]),
            el('td', null, [notes]),
          ]));
        });
        tbl.appendChild(tbody);
        body = tbl;
      }
    }
  }
  node.appendChild(renderCard({
    title: 'Expectation outcomes',
    body,
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
  ensureRunExpectations(epochId, generationId, entryId, repaint);
  // Only fetch the header endpoint when there is no live state for the
  // run — a live run draws its progress / elapsed / status from the
  // active-runs snapshot and the loss.json does not exist yet.
  if (!run) ensureRunHeader(epochId, generationId, entryId, repaint);
  _renderHeader(params, run);
  _renderExpectation(epochId, generationId, entryId);
  _renderJudges(epochId, generationId, entryId);
  _renderTranscript();
  _renderEvents();
}

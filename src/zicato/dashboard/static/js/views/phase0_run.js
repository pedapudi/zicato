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
import { renderLoadingState, renderEmptyState } from '../components/loading.js';

const _runJudgeCache = new Map();
const _loadingRunJudges = new Set();
const _runExpectationsCache = new Map(); // same key -> payload
const _loadingRunExpectations = new Set();
const _runHeaderCache = new Map(); // same key -> payload
const _loadingRunHeader = new Set();
// L4 transcript caches — one map keyed by the full (epoch, gen, entry)
// triple so the focused run and any compare-target run share the same
// pool. The compare picker only toggles which entry is fetched; the
// cache is generic.
const _transcriptCache = new Map();
const _loadingTranscript = new Set();
// Compare-mode state — which generation is the picker pointing at, keyed
// per (focused epoch, entry) so navigating to another L4 entry resets
// the picker to "off" instead of carrying a stale compare target.
const _compareGenByEntry = new Map();

export function resetRunCaches() {
  _runJudgeCache.clear();
  _loadingRunJudges.clear();
  _runExpectationsCache.clear();
  _loadingRunExpectations.clear();
  _runHeaderCache.clear();
  _loadingRunHeader.clear();
  _transcriptCache.clear();
  _loadingTranscript.clear();
  _compareGenByEntry.clear();
}

export function transcriptPayload(epochId, generationId, entryId) {
  return _transcriptCache.get(
    epochId + '/' + generationId + '/' + entryId,
  ) || null;
}

export function compareGenFor(epochId, entryId) {
  return _compareGenByEntry.get(epochId + '/' + entryId) || null;
}

export function setCompareGenFor(epochId, entryId, generationId) {
  const key = epochId + '/' + entryId;
  if (!generationId) _compareGenByEntry.delete(key);
  else _compareGenByEntry.set(key, generationId);
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

async function ensureTranscript(epochId, generationId, entryId, repaint) {
  if (!epochId || !generationId || !entryId) return null;
  const key = epochId + '/' + generationId + '/' + entryId;
  if (_transcriptCache.has(key)) return _transcriptCache.get(key);
  if (_loadingTranscript.has(key)) return null;
  _loadingTranscript.add(key);
  try {
    const data = await fetchJson('/api/run/'
      + encodeURIComponent(epochId) + '/'
      + encodeURIComponent(generationId) + '/'
      + encodeURIComponent(entryId) + '/transcript');
    if (data && typeof data === 'object') _transcriptCache.set(key, data);
  } catch {
    _transcriptCache.set(key, {
      epoch_id: epochId, generation_id: generationId, entry_id: entryId,
      run_id: null, turns: [], annotations: [],
      event_count: 0, complete: false,
    });
  } finally {
    _loadingTranscript.delete(key);
    if (typeof repaint === 'function') repaint();
  }
  return _transcriptCache.get(key);
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
      body.appendChild(renderLoadingState({ label: 'Loading run header' }));
    } else if (header.run_id == null && header.runtime_ms == null) {
      body.appendChild(renderEmptyState(
        'No completed-run metrics recorded for this entry yet.',
      ));
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
      body = renderLoadingState({ label: 'Loading expectations' });
    } else {
      const outcomes = Array.isArray(data.outcomes) ? data.outcomes : [];
      if (outcomes.length === 0) {
        body = renderEmptyState('(no expectations recorded for this run)');
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
      body = renderLoadingState({ label: 'Loading per-judge breakdown' });
    } else {
      const judges = Array.isArray(data.judges) ? data.judges : [];
      if (judges.length === 0) {
        const msg = data.note ? '(no per-judge data: ' + data.note + ')'
          : '(no per-judge data recorded for this run)';
        body = renderEmptyState(msg);
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

// Build a turn card for one transcript turn. The renderer is shared
// between single-run mode (one column) and compare mode (two columns),
// and between the focused side and the compare side; the only
// per-context decoration is the column wrapper class up the tree.
function _turnCard(turn) {
  const meta = el('div', { class: 'conversation-turn-meta' });
  const agent = (turn && turn.agent) ? String(turn.agent) : '';
  const role = (turn && turn.role) ? String(turn.role) : 'agent';
  const kindRaw = (turn && turn.kind) ? String(turn.kind) : '';
  // Map the underlying event kind to one of the kind- decorator classes
  // the existing CSS understands. Anything plan/policy/judge-shaped picks
  // up a colour; everything else stays neutral.
  let kindCls = '';
  if (kindRaw.includes('plan')) kindCls = ' kind-plan';
  else if (kindRaw.includes('tool') || kindRaw.includes('delegation')) {
    kindCls = ' kind-tool';
  }
  if (agent) meta.appendChild(el('span', { class: 'conversation-turn-agent' }, [agent]));
  meta.appendChild(el('span', { class: 'conversation-turn-role' }, [role]));
  if (kindRaw) {
    meta.appendChild(el(
      'span', { class: 'conversation-turn-kind' + kindCls }, [kindRaw],
    ));
  }
  if (turn && turn.seq != null) {
    meta.appendChild(el('span', { class: 'conversation-turn-seq mono' },
      ['#' + String(turn.seq)]));
  }
  if (turn && turn.ts) {
    meta.appendChild(el('span', { class: 'conversation-turn-ts mono' },
      [String(turn.ts).slice(11, 19)]));
  }

  const card = el('div', { class: 'conversation-turn' }, [meta]);

  if (turn && turn.text) {
    card.appendChild(el('div', { class: 'conversation-turn-text' },
      [String(turn.text)]));
  }
  // Tool calls / results — compact rows, same selectors the existing
  // matchup conversation diff uses.
  const tcs = (turn && Array.isArray(turn.tool_calls)) ? turn.tool_calls : [];
  for (const tc of tcs) {
    const argsText = tc && tc.args != null
      ? (typeof tc.args === 'string' ? tc.args : JSON.stringify(tc.args))
      : '';
    card.appendChild(el('div', { class: 'conversation-tool tool-call' }, [
      el('span', { class: 'conversation-tool-glyph' }, ['→']),
      el('span', { class: 'conversation-tool-name' },
        [tc && tc.name ? String(tc.name) : 'tool']),
      argsText ? el('span', { class: 'conversation-tool-args mono' },
        [argsText.slice(0, 240)]) : null,
    ].filter(Boolean)));
  }
  const trs = (turn && Array.isArray(turn.tool_results)) ? turn.tool_results : [];
  for (const tr of trs) {
    const resultText = tr && tr.result ? String(tr.result) : '';
    card.appendChild(el('div', { class: 'conversation-tool tool-result' }, [
      el('span', { class: 'conversation-tool-glyph' }, ['←']),
      el('span', { class: 'conversation-tool-name' },
        [tr && tr.name ? String(tr.name) : 'result']),
      resultText ? el('span', { class: 'conversation-tool-args' },
        [resultText.slice(0, 240)]) : null,
    ].filter(Boolean)));
  }
  return card;
}

// Build a one-column transcript body — used by single-run mode AND by
// each side of the side-by-side diff. ``data`` is the cache payload
// (either still loading == null/undefined, or the API JSON).
function _transcriptColumnBody(data, opts) {
  const placeholder = (opts && opts.placeholder) || 'compare';
  if (!data) {
    return renderLoadingState({ label: 'Loading transcript' });
  }
  const turns = Array.isArray(data.turns) ? data.turns : [];
  if (turns.length === 0) {
    if (data.run_id == null) {
      return renderEmptyState(
        placeholder === 'compare'
          ? 'No transcript available for the compare target.'
          : 'No transcript recorded for this run.',
      );
    }
    // The reducer returned a run_id but no turns — the canonical
    // "completed but zero turns" case (eg. wall-clock timeout). Surface
    // an honest panel instead of the loading spinner.
    const panel = el('div', { class: 'conversation-no-turns-panel' }, [
      el('div', { class: 'conversation-no-turns-headline' },
        ['This run produced no transcript turns.']),
      el('div', { class: 'conversation-no-turns-fact mono' },
        ['run · ' + String(data.run_id)]),
    ]);
    return panel;
  }
  const body = el('div', { class: 'conversation-column-body' });
  // Multi-run boundary — when a multi_turn_emulated run lands here the
  // reducer carries run_index on each turn; emit a divider on rollover.
  let lastRunIndex = null;
  let lastRunId = null;
  for (const turn of turns) {
    const ri = (turn && typeof turn.run_index === 'number') ? turn.run_index : 1;
    const rid = (turn && turn.run_id) ? String(turn.run_id) : null;
    if (lastRunIndex !== null && ri !== lastRunIndex) {
      body.appendChild(el('div', { class: 'conversation-run-separator' }, [
        el('span', { class: 'conversation-run-separator-label' },
          ['turn ' + ri + ' of multi-run entry']),
        rid ? el('span', { class: 'conversation-run-separator-run-id mono' },
          ['run · ' + rid]) : null,
      ].filter(Boolean)));
    }
    lastRunIndex = ri;
    lastRunId = rid;
    body.appendChild(_turnCard(turn));
  }
  void lastRunId; // pacify the linter — meaningful only inside the loop.
  return body;
}

// Build the sibling-generation picker. Lists every generation in the
// focused epoch except the focused one; emitting an empty string value
// = "no compare". Single source for the picker behaviour so the change
// handler stays in lockstep with the options.
function _buildComparePicker(epochId, focusedGen, selectedCompare, onChange) {
  const select = el('select', { class: 'mono' });
  select.appendChild(el('option', { value: '' }, ['compare to … (off)']));
  const lineage = state.lineage || {};
  const generations = Array.isArray(lineage.generations) ? lineage.generations : [];
  // Filter to this epoch; skip the focused generation itself.
  const inEpoch = generations
    .filter((g) => g && g.epoch_id === epochId && g.generation_id !== focusedGen)
    .map((g) => g.generation_id);
  // Stable order: generation id is monotonically increasing (v0, v1, …);
  // sort by natural order so the picker reads left-to-right in age.
  inEpoch.sort();
  for (const gid of inEpoch) {
    const opt = el('option', { value: gid }, [gid]);
    if (gid === selectedCompare) opt.setAttribute('selected', 'selected');
    select.appendChild(opt);
  }
  select.addEventListener('change', (ev) => {
    const v = (ev && ev.target && ev.target.value) ? String(ev.target.value) : '';
    onChange(v || null);
  });
  return select;
}

function _renderTranscript(epochId, generationId, entryId, repaint) {
  const node = $('phase0-run-transcript');
  if (!node) return;
  clearChildren(node);

  if (!epochId || !generationId || !entryId) {
    node.appendChild(renderCard({
      title: 'Transcript',
      body: el('p', { class: 'empty' }, ['No run selected.']),
    }));
    return;
  }

  // Drive the focused-side fetch every render — the cache makes the
  // call cheap.
  ensureTranscript(epochId, generationId, entryId, repaint);

  const compareGen = compareGenFor(epochId, entryId);
  if (compareGen) {
    ensureTranscript(epochId, compareGen, entryId, repaint);
  }

  const focusedKey = epochId + '/' + generationId + '/' + entryId;
  const focusedData = _transcriptCache.get(focusedKey) || null;

  // -- Header: compare picker + run-id chip ----------------------------
  const header = el('div', { class: 'conversation-column-head' });
  header.appendChild(el('span', { class: 'conversation-column-label' },
    ['compare']));
  header.appendChild(_buildComparePicker(
    epochId, generationId, compareGen, (next) => {
      setCompareGenFor(epochId, entryId, next);
      if (typeof repaint === 'function') repaint();
      else _renderTranscript(epochId, generationId, entryId, repaint);
    },
  ));

  let body;
  if (!compareGen) {
    // Single-run mode — one column.
    const col = el('div', { class: 'conversation-column' });
    const colHead = el('div', { class: 'conversation-column-head' }, [
      el('span', { class: 'conversation-column-label' }, ['focused']),
      el('span', { class: 'conversation-run-id mono' }, [
        'v · ' + generationId,
      ]),
      (focusedData && focusedData.run_id)
        ? el('span', { class: 'conversation-run-id mono' },
          ['run · ' + String(focusedData.run_id)])
        : null,
      (focusedData && typeof focusedData.event_count === 'number')
        ? el('span', { class: 'conversation-event-count mono' },
          [String(focusedData.event_count) + ' events'])
        : null,
    ].filter(Boolean));
    col.appendChild(colHead);
    col.appendChild(_transcriptColumnBody(focusedData, { placeholder: 'focused' }));
    body = el('div', { class: 'conversation-panel' }, [header, col]);
  } else {
    // Compare mode — two columns aligned by turn index.
    const compareKey = epochId + '/' + compareGen + '/' + entryId;
    const compareData = _transcriptCache.get(compareKey) || null;
    const focusedCol = el('div', { class: 'conversation-column champion' });
    focusedCol.appendChild(el('div', { class: 'conversation-column-head' }, [
      el('span', { class: 'conversation-column-label' }, ['focused']),
      el('span', { class: 'conversation-run-id mono' },
        ['v · ' + generationId]),
      (focusedData && focusedData.run_id)
        ? el('span', { class: 'conversation-run-id mono' },
          ['run · ' + String(focusedData.run_id)])
        : null,
    ].filter(Boolean)));
    focusedCol.appendChild(
      _transcriptColumnBody(focusedData, { placeholder: 'focused' }),
    );

    const compareCol = el('div', { class: 'conversation-column challenger' });
    compareCol.appendChild(el('div', { class: 'conversation-column-head' }, [
      el('span', { class: 'conversation-column-label' }, ['compare']),
      el('span', { class: 'conversation-run-id mono' },
        ['v · ' + compareGen]),
      (compareData && compareData.run_id)
        ? el('span', { class: 'conversation-run-id mono' },
          ['run · ' + String(compareData.run_id)])
        : null,
    ].filter(Boolean)));
    compareCol.appendChild(
      _transcriptColumnBody(compareData, { placeholder: 'compare' }),
    );

    const cols = el('div', { class: 'conversation-columns' }, [
      focusedCol, compareCol,
    ]);
    body = el('div', { class: 'conversation-panel' }, [header, cols]);
  }
  node.appendChild(renderCard({
    title: 'Transcript',
    body,
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
    // logEventsPath / logCursor are populated by the first /api/log-tail
    // response; if neither has landed the event stream is still loading
    // rather than genuinely empty.
    const eventsLoaded = state.logEventsPath != null || state.logCursor != null;
    body = eventsLoaded
      ? renderEmptyState('No events yet.')
      : renderLoadingState({ label: 'Loading events' });
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
  _renderTranscript(epochId, generationId, entryId, repaint);
  _renderEvents();
}

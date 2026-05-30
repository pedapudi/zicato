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
import { harmonografLink } from '../core/harmonograf.js';

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
// Parallel "user explicitly touched the picker" flag, same key shape.
// When false, compareGenFor() auto-defaults to the focused gen's
// parent_generation_id (the champion-at-time-of-challenge — the side
// the user wants to see when they navigate to a rejected challenger).
// Once the user picks anything (including "(off)"), the flag flips
// true and subsequent renders honour their explicit choice instead.
const _compareUserOverride = new Map();

// Per-card render digests — the SSE heartbeat ticks once per second
// and emits state:changed every time. The transcript card (which
// hosts a native <select> picker) must NOT be rebuilt on every tick;
// rebuilding closes the native dropdown the moment a user clicks it.
//
// Each card has its own digest covering only the inputs it actually
// reads — the header card includes the elapsed clock, the transcript
// card does not. A heartbeat tick that only re-stamps the elapsed
// counter rebuilds the header tile-strip but leaves the transcript
// card (and its open dropdown) untouched.
let _lastHeaderDigest = null;
let _lastExpectationDigest = null;
let _lastJudgesDigest = null;
let _lastTranscriptDigest = null;
let _lastEventsDigest = null;
// Force-render override — picker-change handlers and fetch-completion
// callbacks set this to bypass the per-card digest gates so the very
// next render repaints unconditionally. Cleared at the top of each
// renderPhase0Run after the gates consult it.
let _forceNextRunRender = false;
// Compare-picker state — see _buildComparePicker for the full
// rationale. Hoisted here so resetRunCaches() can reset them in the
// canonical "scrub everything" path.
let _comparePicker = null;
let _comparePickerSig = null;
let _comparePickerHandler = null;

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
  _compareUserOverride.clear();
  _lastHeaderDigest = null;
  _lastExpectationDigest = null;
  _lastJudgesDigest = null;
  _lastTranscriptDigest = null;
  _lastEventsDigest = null;
  _forceNextRunRender = false;
  // Picker state lives outside the data caches but resetRunCaches() is
  // the canonical "scrub everything" hook every L4 test calls, so it
  // resets here too.
  _comparePicker = null;
  _comparePickerSig = null;
  _comparePickerHandler = null;
  // The events filter toggle is operator UI state; the canonical
  // "scrub everything" reset clears it back to the full-feed default.
  _eventsKeyOnly = false;
}

// Reset every per-card digest — used by tests that want to assert a
// no-op render is gated without clearing the transcript / expectations
// cache the previous render populated.
export function resetRunRenderDigest() {
  _lastHeaderDigest = null;
  _lastExpectationDigest = null;
  _lastJudgesDigest = null;
  _lastTranscriptDigest = null;
  _lastEventsDigest = null;
  _forceNextRunRender = false;
}

// Build a per-card digest object so each card can independently gate
// its own repaint. Heartbeat-timestamp churn MUST be excluded from
// every card's digest; the transcript card additionally excludes the
// elapsed clock (which would otherwise rebuild the <select> every
// second on a live run).
//
// Exported so tests can pin which fields are part of the contract.
export function runViewDigest(params) {
  const epochId = (params && params.epochId) || null;
  const generationId = (params && params.generationId) || null;
  const entryId = (params && params.entryId) || null;
  const key = (epochId || '') + '/' + (generationId || '') + '/' + (entryId || '');
  const compareGen = compareGenFor(epochId, entryId, generationId) || null;
  const compareKey = compareGen
    ? (epochId || '') + '/' + compareGen + '/' + (entryId || '')
    : null;

  // -- Active-run snapshot --------------------------------------------
  // elapsed_seconds ticks every second on a live run; bucketed to the
  // nearest second so a sub-second jitter does not churn the digest.
  const run = findActiveRun(entryId);
  const runStatus = run ? (run.status || '') : '';
  const runProgress = run && typeof run.progress === 'number'
    ? Math.round((run.progress || 0) * 100) : null;
  const runElapsed = run && typeof run.elapsed_seconds === 'number'
    ? Math.round(run.elapsed_seconds) : null;
  const runPresent = run != null;

  // -- Heartbeat-derived fields the header consumes -------------------
  // The header card paints a harmonograf deep-link whose presence/href
  // depends on state.heartbeat.harmonograf_url. On a cold deep-link to
  // /#/run/... the first render happens BEFORE the SSE heartbeat lands,
  // so harmonograf_url is null; once it arrives, the header MUST
  // re-render to surface the link. Folding the URL into the header
  // digest makes the gate at the render site fire that repaint.
  const harmonografUrl = (state.heartbeat && state.heartbeat.harmonograf_url)
    ? String(state.heartbeat.harmonograf_url) : '';

  // -- Cached payload signatures --------------------------------------
  const focusedTx = _transcriptCache.get(key);
  const focusedTxSig = focusedTx
    ? (focusedTx.run_id || '') + ':'
      + (typeof focusedTx.event_count === 'number' ? focusedTx.event_count : -1)
    : null;
  const compareTx = compareKey ? _transcriptCache.get(compareKey) : null;
  const compareTxSig = compareTx
    ? (compareTx.run_id || '') + ':'
      + (typeof compareTx.event_count === 'number' ? compareTx.event_count : -1)
    : null;
  const judges = _runJudgeCache.get(key);
  const judgesSig = judges
    ? (judges.run_id || '') + ':'
      + (Array.isArray(judges.judges) ? judges.judges.length : 0)
    : null;
  const expectations = _runExpectationsCache.get(key);
  const expSig = expectations
    ? (Array.isArray(expectations.outcomes) ? expectations.outcomes.length : 0)
    : null;
  const header = _runHeaderCache.get(key);
  const headerSig = header
    ? (header.run_id || '') + ':' + (header.runtime_ms != null ? header.runtime_ms : '')
      + ':' + (header.pass_fail != null ? String(header.pass_fail) : '')
    : null;

  // -- Events stream --------------------------------------------------
  const eventsLen = (state.logTail && Array.isArray(state.logTail.events))
    ? state.logTail.events.length : 0;
  const eventsLoaded = state.logEventsPath != null || state.logCursor != null;
  // The filter toggle is part of what the events card paints, so a
  // toggle flip must change the digest even when the feed length is
  // unchanged. (A toggle handler also force-renders, but folding it in
  // keeps the gate honest for any indirect re-render path.)
  const eventsKeyOnlyFlag = _eventsKeyOnly;

  // -- Lineage --------------------------------------------------------
  // Number of generations on the focused epoch — the compare picker's
  // option list reads that and nothing else. Filtering here so the
  // digest is independent of other epochs' generations.
  let lineageEpochGenLen = 0;
  if (state.lineage && Array.isArray(state.lineage.generations)) {
    for (const g of state.lineage.generations) {
      if (g && g.epoch_id === epochId) lineageEpochGenLen += 1;
    }
  }

  return {
    // Each card consumes only the slice it depends on. JSON.stringify
    // collapses the slice into a hashable string at the gate site.
    header: {
      epochId, generationId, entryId,
      runStatus, runProgress, runElapsed, runPresent, headerSig,
      harmonografUrl,
    },
    expectation: { epochId, generationId, entryId, expSig },
    judges: { epochId, generationId, entryId, judgesSig },
    transcript: {
      // Transcript card MUST be independent of the elapsed clock so a
      // live run's per-second tick does not close the open compare
      // <select> dropdown.
      epochId, generationId, entryId, compareGen,
      focusedTxSig, compareTxSig, lineageEpochGenLen,
    },
    events: { eventsLen, eventsLoaded, eventsKeyOnly: eventsKeyOnlyFlag },
  };
}

export function transcriptPayload(epochId, generationId, entryId) {
  return _transcriptCache.get(
    epochId + '/' + generationId + '/' + entryId,
  ) || null;
}

// Recover the matchup champion carried into L4 from an L3 decision.
//
// When the user drills into an entry FROM a round/decision (L3) the
// link carries the matchup context as a 4th hash segment on the run
// route: ``#/run/<epoch>/<challenger>/<entry>/vs-<champion>``. The
// phase0_router only parses the first three run segments (epoch / gen /
// entry) and ignores the rest, so the matchup hint is recovered here by
// reading the live hash directly — no router change, fully shareable as
// a URL. Returns the champion id, or null for a plain (non-matchup)
// deep-link.
//
// Exported so tests can pin the parse without driving a render.
export function matchupChampionFromHash(hash) {
  const raw = (typeof hash === 'string')
    ? hash
    : ((typeof window !== 'undefined' && window.location && window.location.hash) || '');
  const segs = raw.replace(/^#\/?/, '').split('/').filter(Boolean);
  // run route grammar: run / epoch / gen / entry / vs-<champion>
  if (segs[0] !== 'run' || segs.length < 5) return null;
  const tail = decodeURIComponent(segs[4]);
  if (tail.indexOf('vs-') !== 0) return null;
  const champ = tail.slice('vs-'.length);
  return champ.length > 0 ? champ : null;
}

// Look up the focused gen's parent_generation_id on the epoch contract.
// state.epochDef.experiments is the canonical list — the same place the
// L2 generation view reads from. Returns null when:
//   * state.epochDef has not been hydrated yet (cold deep-link),
//   * the focused gen has no experiment record (eg. the v0 seed), or
//   * the experiment's parent_generation_id is null / empty.
// Exported so tests can assert the helper independently of the picker.
export function defaultCompareGenFor(generationId) {
  if (!generationId) return null;
  const def = state.epochDef;
  if (!def || !Array.isArray(def.experiments)) return null;
  for (const exp of def.experiments) {
    if (exp && exp.generation_id === generationId) {
      const parent = exp.parent_generation_id;
      if (typeof parent === 'string' && parent.length > 0) return parent;
      return null;
    }
  }
  return null;
}

// Resolve the compare-picker target for a (focused epoch, entry, gen)
// triple. When the user has touched the picker, their explicit choice
// wins (including "(off)"). Otherwise auto-default to the focused gen's
// parent — when navigating to a rejected challenger or a promoted
// challenger that beat a champion, the side-by-side is the useful view,
// so the picker should land there without a click.
//
// The third argument (focused generationId) is optional for backwards
// compatibility with call sites that don't have it handy; when omitted
// the auto-default is skipped and only an explicit override is honoured.
export function compareGenFor(epochId, entryId, generationId) {
  const key = epochId + '/' + entryId;
  if (_compareUserOverride.get(key)) {
    return _compareGenByEntry.get(key) || null;
  }
  // No explicit pick yet. Context-preserving L3→L4: when the user
  // arrived from a decision the run hash carries the matchup champion
  // (``…/vs-<champion>``) — default the picker to THAT champion so the
  // side-by-side opens on the exact matchup the operator was judging,
  // not the lineage parent. A champion equal to the focused gen (a
  // degenerate self-matchup) is ignored.
  const matchupChamp = matchupChampionFromHash();
  if (matchupChamp && matchupChamp !== generationId) return matchupChamp;
  // Plain deep-link — fall back to the parent on the epoch contract.
  // Returns null gracefully when the contract is not yet loaded or the
  // focused gen has no parent (eg. v0).
  return defaultCompareGenFor(generationId);
}

export function setCompareGenFor(epochId, entryId, generationId) {
  const key = epochId + '/' + entryId;
  // The user touched the picker — flag the override so subsequent
  // renders honour the choice (including null = explicit "(off)") and
  // stop auto-defaulting to the parent.
  _compareUserOverride.set(key, true);
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
      // Harmonograf deep-link for the completed run — uses the
      // adk_session_id surfaced by loss.json (via build_run_header); the
      // helper falls back to the bare harmonograf base url when the id
      // is absent. Renders nothing when no harmonograf_url is configured.
      const hgLink = harmonografLink(header, 'Open in harmonograf');
      if (hgLink) {
        body.appendChild(el('p', {
          style: 'margin:var(--space-2) 0 0; font-size:var(--font-size-12);',
        }, [hgLink]));
      }
    }
  } else if (run.status === 'running') {
    body.appendChild(el('div', {
      style: 'margin-top:var(--space-3); display:flex; gap:var(--space-2);',
    }, [renderPill('live', 'live')]));
    // For live runs the active-run record may carry adk_session_id once
    // the worker has surfaced it; degrades gracefully to the bare base
    // url, and renders nothing if no harmonograf_url is configured.
    const hgLink = harmonografLink(run, 'Open in harmonograf');
    if (hgLink) {
      body.appendChild(el('p', {
        style: 'margin:var(--space-2) 0 0; font-size:var(--font-size-12);',
      }, [hgLink]));
    }
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

// Build (or update in place) the sibling-generation picker. Lists every
// generation in the focused epoch except the focused one; emitting an
// empty string value = "no compare". Returns the <select> element —
// the SAME element across renders unless its option list changed.
function _buildComparePicker(epochId, focusedGen, selectedCompare, onChange) {
  const lineage = state.lineage || {};
  const generations = Array.isArray(lineage.generations) ? lineage.generations : [];
  const inEpoch = generations
    .filter((g) => g && g.epoch_id === epochId && g.generation_id !== focusedGen)
    .map((g) => g.generation_id);
  inEpoch.sort();
  const sig = JSON.stringify({
    epochId, focusedGen, options: inEpoch,
  });

  // Rebuild only when the option list changed. The existing <select>
  // is preserved across renders so an open native dropdown survives a
  // heartbeat-triggered repaint.
  if (_comparePicker == null || _comparePickerSig !== sig) {
    const select = el('select', { class: 'mono' });
    select.appendChild(el('option', { value: '' }, ['compare to … (off)']));
    for (const gid of inEpoch) {
      const opt = el('option', { value: gid }, [gid]);
      select.appendChild(opt);
    }
    // Bind the change handler ONCE — a closure over a mutable ref so a
    // later render can swap in a fresh onChange without re-wiring.
    select.addEventListener('change', (ev) => {
      const v = (ev && ev.target && ev.target.value) ? String(ev.target.value) : '';
      const handler = _comparePickerHandler;
      if (typeof handler === 'function') handler(v || null);
    });
    _comparePicker = select;
    _comparePickerSig = sig;
  }

  // Keep the picker's selected value in sync with the caller's
  // intent. Setting .value on a native <select> does not close an open
  // dropdown unless the value actually changes; we still gate the
  // write to avoid spurious churn.
  const wantValue = selectedCompare || '';
  if (_comparePicker.value !== wantValue) {
    // Also reflect on the option's selected attribute so the harness
    // (and any DOM-introspection test) sees the chosen value.
    const opts = _comparePicker.children;
    for (const opt of opts) {
      const isMatch = (opt.getAttribute && opt.getAttribute('value')) === wantValue;
      if (isMatch) opt.setAttribute('selected', 'selected');
      else opt.removeAttribute && opt.removeAttribute('selected');
    }
    _comparePicker.value = wantValue;
  }

  // Refresh the handler closure each render so it captures the latest
  // route + repaint without rebuilding the <select>.
  _comparePickerHandler = onChange;

  return _comparePicker;
}

// Reset the picker module state. Used by tests that share state across
// renders, and by resetRunCaches() so a fresh route gets a fresh
// picker.
export function resetComparePicker() {
  _comparePicker = null;
  _comparePickerSig = null;
  _comparePickerHandler = null;
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

  const compareGen = compareGenFor(epochId, entryId, generationId);
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

// High-signal event kinds — drift spikes, plan revisions, and steering
// interventions are the events an operator scans for when triaging a
// live run. Everything else (run_started, judgement_emitted, …) is
// routine feed noise. Exported so the filter contract is testable
// independent of the DOM.
export function isKeyEvent(ev) {
  const k = String((ev && ev.kind) || '').toLowerCase();
  if (!k) return false;
  // Drift spikes.
  if (k.includes('drift')) return true;
  // Plan revisions / replans.
  if (k.includes('plan') || k.includes('replan')) return true;
  // Steering interventions (operator control-file nudges, steer events).
  if (k.includes('steer') || k.includes('intervention')) return true;
  // Hard failures are always worth surfacing.
  if (k.includes('fail') || k === 'error') return true;
  return false;
}

// Filter state — false (default) shows the full feed; true narrows to
// the high-signal events. Module-level so the toggle survives the
// per-card digest gate (a digest tick must not reset the operator's
// filter). resetRunCaches() clears it back to the default.
let _eventsKeyOnly = false;

// Exported so a test can drive the toggle without simulating a click.
export function setEventsKeyOnly(on) { _eventsKeyOnly = !!on; }
export function eventsKeyOnly() { return _eventsKeyOnly; }

function _renderEvents(repaint) {
  const node = $('phase0-run-events');
  if (!node) return;
  clearChildren(node);
  const allEvents = (state.logTail && Array.isArray(state.logTail.events))
    ? state.logTail.events : [];
  const keyCount = allEvents.reduce((n, ev) => n + (isKeyEvent(ev) ? 1 : 0), 0);

  // The visible window: last 30 of either the full feed or the
  // key-only subset, depending on the toggle.
  const source = _eventsKeyOnly ? allEvents.filter(isKeyEvent) : allEvents;
  const events = source.slice(-30);

  let body;
  if (allEvents.length === 0) {
    // logEventsPath / logCursor are populated by the first /api/log-tail
    // response; if neither has landed the event stream is still loading
    // rather than genuinely empty.
    const eventsLoaded = state.logEventsPath != null || state.logCursor != null;
    body = eventsLoaded
      ? renderEmptyState('No events yet.')
      : renderLoadingState({ label: 'Loading events' });
  } else {
    body = el('div');

    // -- filter toggle ------------------------------------------------
    const checkbox = el('input', {
      type: 'checkbox',
      'data-events-filter': 'key-only',
    });
    if (_eventsKeyOnly) checkbox.setAttribute('checked', 'checked');
    checkbox.addEventListener('change', (ev) => {
      _eventsKeyOnly = !!(ev && ev.target && ev.target.checked);
      // Re-render via the app's scheduler when available; otherwise
      // repaint this card directly so a standalone driver still updates.
      if (typeof repaint === 'function') {
        _forceNextRunRender = true;
        repaint();
      } else {
        _renderEvents(repaint);
      }
    });
    const filterBar = el('div', { class: 'events-filter' }, [
      el('label', { class: 'events-filter-toggle' }, [
        checkbox,
        el('span', null, ['key events only']),
      ]),
      el('span', { class: 'events-filter-count' }, [
        keyCount + ' key / ' + allEvents.length + ' total',
      ]),
    ]);
    body.appendChild(filterBar);

    const list = el('div', { class: 'events-list' });
    if (events.length === 0) {
      // Toggle on but no key events in the feed.
      list.appendChild(renderEmptyState('No key events yet.'));
    } else {
      for (const ev of events) {
        const ts = ev.ts || ev.timestamp || '';
        const key = isKeyEvent(ev);
        list.appendChild(el('div', {
          class: 'events-row' + (key ? ' events-row-key' : ''),
          'data-key-event': key ? '1' : '0',
        }, [
          renderEventChip(ev.kind || 'event'),
          el('span', { class: 'events-row-ts' }, [String(ts).slice(11, 19)]),
          el('span', { class: 'events-row-summary' }, [ev.summary || '']),
        ]));
      }
    }
    body.appendChild(list);
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
  // Fetches still fire on every entry — the cache short-circuits the
  // ones already settled, and a fetch-completion callback flips
  // _forceNextRunRender so the data lands on the very next render.
  ensureRunJudges(epochId, generationId, entryId, repaint);
  ensureRunExpectations(epochId, generationId, entryId, repaint);
  // Only fetch the header endpoint when there is no live state for the
  // run — a live run draws its progress / elapsed / status from the
  // active-runs snapshot and the loss.json does not exist yet.
  if (!run) ensureRunHeader(epochId, generationId, entryId, repaint);
  // Transcript fetches (focused + optional compare) are kicked off
  // here so the per-card digest below sees the cache hit when the
  // payload eventually arrives.
  ensureTranscript(epochId, generationId, entryId, repaint);
  const compareGen = compareGenFor(epochId, entryId, generationId);
  if (compareGen) ensureTranscript(epochId, compareGen, entryId, repaint);

  // Per-card digest gates — a heartbeat timestamp tick changes neither
  // the transcript digest nor the events digest, so an open <select>
  // dropdown in the transcript card survives the tick. The header
  // card's elapsed clock IS allowed to tick because the header has no
  // interactive form widget.
  const digests = runViewDigest(params);
  const force = _forceNextRunRender;
  _forceNextRunRender = false;

  const headerDigest = JSON.stringify(digests.header);
  if (force || headerDigest !== _lastHeaderDigest) {
    _lastHeaderDigest = headerDigest;
    _renderHeader(params, run);
  }
  const expDigest = JSON.stringify(digests.expectation);
  if (force || expDigest !== _lastExpectationDigest) {
    _lastExpectationDigest = expDigest;
    _renderExpectation(epochId, generationId, entryId);
  }
  const judgesDigest = JSON.stringify(digests.judges);
  if (force || judgesDigest !== _lastJudgesDigest) {
    _lastJudgesDigest = judgesDigest;
    _renderJudges(epochId, generationId, entryId);
  }
  const transcriptDigest = JSON.stringify(digests.transcript);
  if (force || transcriptDigest !== _lastTranscriptDigest) {
    _lastTranscriptDigest = transcriptDigest;
    _renderTranscript(epochId, generationId, entryId, repaint);
  }
  const eventsDigest = JSON.stringify(digests.events);
  if (force || eventsDigest !== _lastEventsDigest) {
    _lastEventsDigest = eventsDigest;
    _renderEvents(repaint);
  }
}

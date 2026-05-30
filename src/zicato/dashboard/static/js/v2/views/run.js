// js/v2/views/run.js — the v2 Run view (DASHBOARD-V2 §4.5).
//
//   "What actually happened?" — the EVIDENCE.
//
// The Run view answers, for one board entry under one generation, what
// the loop actually did: the headline metrics, the expectation
// outcomes, the conversation itself, and the per-judge attribution. Its
// organizing principle is §2's comparison-by-default: the focused
// generation (the CHALLENGER) is shown next to its parent (the
// CHAMPION) side-by-side, aligned turn-by-turn — never behind an opt-in
// toggle. The champion side is the DEFAULT; the picker only swaps in an
// alternate comparison.
//
// The route (router.js) carries `entryId` and an optional
// `generationId`. The epoch is resolved from the lineage (a generation
// carries its `epoch_id`) and the champion from the epoch contract's
// `parent_generation_id`. Everything async flows through `stateBlock`
// (not-yet / running / empty / broken) — never an ad-hoc "No data".
//
// All metrics + transcripts come from the per-run REST endpoints:
//   GET /api/run/{epoch}/{gen}/{entry}/header        — header metrics
//   GET /api/run/{epoch}/{gen}/{entry}/expectations  — expectation outcomes
//   GET /api/run/{epoch}/{gen}/{entry}/transcript     — the conversation
//   GET /api/run/{epoch}/{gen}/{entry}/per-judge      — per-judge loss
//
// The view is a pure factory + a registry self-registration; the shell
// calls renderRun(host, route) on every state tick. We re-fetch lazily
// (cache short-circuits settled reads), and a fetch-completion callback
// re-renders the SAME host so streaming data lands without a flash.

import { el, clearChildren } from '../../core/dom.js';
import { fetchJson } from '../../core/api.js';
import { state } from '../../core/state.js';
import { fmtScalar } from '../../core/format.js';
import { harmonografLink } from '../../core/harmonograf.js';
import { stateBlock } from '../components/stateBlock.js';

// ---------------------------------------------------------------------------
// Async caches — keyed by the (epoch, gen, entry) triple so the focused
// run and the champion run share one pool. `_pending` guards re-entrant
// fetches; the cache itself short-circuits a settled read. There is NO
// permanent cache over live data — `resetRunView()` (and a route change)
// scrubs everything so a re-fetch re-runs as the run progresses (§2 p4).
// ---------------------------------------------------------------------------
const _headerCache = new Map();
const _expectationsCache = new Map();
const _transcriptCache = new Map();
const _judgesCache = new Map();
const _broken = new Map();       // key -> { kind, reason } for a failed fetch
const _pending = new Set();

// The host the active route is mounted into — a fetch-completion
// callback re-renders THIS host so data lands on the next tick.
let _host = null;
let _route = null;

function runKey(epochId, genId, entryId) {
  return (epochId || '') + '/' + (genId || '') + '/' + (entryId || '');
}

// Scrub all view state. Exported so tests share module state across
// cases the way the v1 suite's resetRunCaches() does.
export function resetRunView() {
  _headerCache.clear();
  _expectationsCache.clear();
  _transcriptCache.clear();
  _judgesCache.clear();
  _broken.clear();
  _pending.clear();
  _host = null;
  _route = null;
}

// ---------------------------------------------------------------------------
// Lineage resolution — the route only carries entryId + optional genId.
// The epoch and the champion (parent) generation are recovered from the
// hydrated state so a deep-link to `#/v2/run/{entry}/{gen}` works cold.
// ---------------------------------------------------------------------------

function lineageGenerations() {
  const lin = state.lineage || {};
  return Array.isArray(lin.generations) ? lin.generations : [];
}

function genRecord(genId) {
  if (!genId) return null;
  for (const g of lineageGenerations()) {
    if (g && (g.generation_id === genId || String(g.id) === String(genId))) return g;
  }
  return null;
}

// The epoch a generation belongs to. Prefers the generation's own
// epoch_id from the lineage; falls back to the live epoch contract /
// header summary so a cold deep-link still resolves.
export function resolveEpochId(genId) {
  const g = genRecord(genId);
  if (g && g.epoch_id) return g.epoch_id;
  if (state.epochDef && state.epochDef.epoch_id) return state.epochDef.epoch_id;
  if (state.epoch && state.epoch.id && state.epoch.id !== '—') return state.epoch.id;
  return null;
}

// The champion (parent) generation for the focused generation. This is
// the comparison-by-default side. Resolution order, most-authoritative
// first: the epoch contract's experiments record, then the lineage
// record's parent edge. Returns null for the seed (no parent) — the
// view then renders a single column honestly.
export function resolveChampionId(genId) {
  if (!genId) return null;
  const def = state.epochDef;
  if (def && Array.isArray(def.experiments)) {
    for (const exp of def.experiments) {
      if (exp && exp.generation_id === genId) {
        const p = exp.parent_generation_id;
        if (typeof p === 'string' && p.length > 0) return p;
        return null;
      }
    }
  }
  const g = genRecord(genId);
  if (g) {
    const p = g.parent_generation_id != null ? g.parent_generation_id : g.parent_id;
    if (typeof p === 'string' && p.length > 0) return p;
  }
  return null;
}

// A live active-run record for this entry, when a run is in flight.
function findActiveRun(entryId) {
  if (!entryId) return null;
  const runs = Array.isArray(state.activeRuns) ? state.activeRuns : [];
  for (const r of runs) {
    if (r && r.entry_id === entryId) return r;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Fetch layer — lazy, cached, re-render on completion. Each returns the
// cached payload synchronously (null while in flight) and kicks the
// fetch when the cache is cold. The completion callback repaints the
// host so the section flips from `stateBlock('running')` to the data.
// ---------------------------------------------------------------------------
function repaint() {
  if (_host && _route) renderRun(_host, _route);
}

function ensure(kind, cache, urlSuffix, epochId, genId, entryId) {
  if (!epochId || !genId || !entryId) return null;
  const key = runKey(epochId, genId, entryId);
  if (cache.has(key)) return cache.get(key);
  // A broken fetch is terminal until the view is reset — never re-issue
  // it on a repaint (which a fetch-completion callback triggers), or a
  // failing endpoint would spin an infinite re-fetch loop.
  if (_broken.has(key + ':' + kind)) return null;
  const pendKey = kind + ':' + key;
  if (_pending.has(pendKey)) return null;
  _pending.add(pendKey);
  const url = '/api/run/'
    + encodeURIComponent(epochId) + '/'
    + encodeURIComponent(genId) + '/'
    + encodeURIComponent(entryId) + '/' + urlSuffix;
  fetchJson(url)
    .then((data) => {
      if (data && typeof data === 'object') cache.set(key, data);
      else cache.set(key, {});
      _broken.delete(key + ':' + kind);
    })
    .catch((err) => {
      // An honest broken state — surface the reason verbatim (§2 p4).
      _broken.set(key + ':' + kind, String((err && err.message) || err));
    })
    .finally(() => {
      _pending.delete(pendKey);
      repaint();
    });
  return null;
}

function brokenReason(epochId, genId, entryId, kind) {
  return _broken.get(runKey(epochId, genId, entryId) + ':' + kind) || null;
}

// ---------------------------------------------------------------------------
// Formatting helpers (total functions — bad input → '—').
// ---------------------------------------------------------------------------
function fmtInt(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  return String(v);
}
function fmtDuration(ms) {
  if (typeof ms !== 'number' || !isFinite(ms)) return '—';
  if (ms < 1000) return Math.round(ms) + ' ms';
  if (ms < 60_000) return (ms / 1000).toFixed(1) + ' s';
  const totalSecs = Math.round(ms / 1000);
  const mins = Math.floor(totalSecs / 60);
  const secs = totalSecs % 60;
  return mins + ' m ' + secs + ' s';
}
function passFailLabel(v) {
  if (v === true) return 'PASS';
  if (v === false) return 'FAIL';
  return '—';
}

// ---------------------------------------------------------------------------
// Header metrics — terse, dense (§4.5). One labeled metric strip with
// drift_loss / verdict / runtime / tokens / output_chars / turns /
// plan_revisions, the budget-exceeded flag, the run / session ids, and
// the harmonograf deep-link.
// ---------------------------------------------------------------------------
function metric(label, value, signal) {
  const cls = 'v2-run-metric'
    + (signal === 'improve' ? ' is-improve'
      : signal === 'regress' ? ' is-regress'
        : signal === 'caution' ? ' is-caution' : '');
  return el('div', { class: cls }, [
    el('div', { class: 'v2-run-metric-label' }, [label]),
    el('div', { class: 'v2-run-metric-value v2-num' }, [String(value)]),
  ]);
}

function renderHeaderSection(epochId, genId, entryId) {
  const section = el('section', { class: 'v2-run-section v2-run-header' });
  section.appendChild(el('h2', { class: 'v2-run-section-title' }, ['Header']));

  const run = findActiveRun(entryId);
  // A live run draws its progress from the active-runs snapshot; the
  // loss.json header does not exist yet, so only fetch it post-hoc.
  const header = run ? null : ensure(
    'header', _headerCache, 'header', epochId, genId, entryId,
  );
  const broke = brokenReason(epochId, genId, entryId, 'header');

  if (run) {
    // Live run — the honest running state with progress + a harmonograf
    // link as soon as the worker surfaces a session id.
    const total = (typeof run.total === 'number' && run.total > 0) ? run.total : null;
    const done = (typeof run.progress === 'number')
      ? Math.round((run.progress || 0) * (total || 100)) : null;
    section.appendChild(stateBlock('running', {
      label: 'Run in flight',
      detail: run.status ? ('status · ' + run.status) : null,
      done: total != null ? done : (typeof run.progress === 'number'
        ? Math.round((run.progress || 0) * 100) : undefined),
      total: total != null ? total : (typeof run.progress === 'number' ? 100 : undefined),
    }));
    const hg = harmonografLink(run, 'Open in harmonograf');
    if (hg) section.appendChild(el('div', { class: 'v2-run-harmonograf' }, [hg]));
    return section;
  }
  if (broke) {
    section.appendChild(stateBlock('broken', { reason: broke }));
    return section;
  }
  if (!header) {
    section.appendChild(stateBlock('running', { label: 'Loading header' }));
    return section;
  }
  if (header.run_id == null && header.runtime_ms == null
      && header.drift_loss == null) {
    // The honest zero-evidence case — no completed-run metrics recorded.
    section.appendChild(stateBlock('empty', {
      label: 'No completed-run metrics',
      detail: 'This entry has no loss.json projection yet.',
    }));
    return section;
  }

  const strip = el('div', { class: 'v2-run-metrics' });
  const verdictSignal = header.pass_fail === true ? 'improve'
    : header.pass_fail === false ? 'regress' : null;
  strip.appendChild(metric('verdict', passFailLabel(header.pass_fail), verdictSignal));
  strip.appendChild(metric('drift loss',
    (typeof header.drift_loss === 'number' && isFinite(header.drift_loss))
      ? fmtScalar(header.drift_loss) : '—'));
  strip.appendChild(metric('runtime', fmtDuration(header.runtime_ms)));
  strip.appendChild(metric('tokens', fmtInt(header.tokens_spent)));
  strip.appendChild(metric('output chars', fmtInt(header.output_chars)));
  strip.appendChild(metric('turns', fmtInt(header.turns_completed)));
  strip.appendChild(metric('plan revisions', fmtInt(header.plan_revisions)));
  section.appendChild(strip);

  if (header.wall_clock_budget_exceeded === true) {
    section.appendChild(el('p', { class: 'v2-run-budget-flag' }, [
      'Wall-clock budget exceeded — the run was force-aborted.',
    ]));
  }

  // ids — run_id + adk_session_id, in the data face.
  const ids = el('div', { class: 'v2-run-ids' });
  if (header.run_id) {
    ids.appendChild(el('span', { class: 'v2-run-id v2-num' },
      ['run_id · ' + String(header.run_id)]));
  }
  if (header.adk_session_id) {
    ids.appendChild(el('span', { class: 'v2-run-id v2-num' },
      ['adk_session_id · ' + String(header.adk_session_id)]));
  }
  if (ids.children.length) section.appendChild(ids);

  // The harmonograf deep-link for the run's adk_session_id.
  const hg = harmonografLink(header, 'Open in harmonograf');
  if (hg) section.appendChild(el('div', { class: 'v2-run-harmonograf' }, [hg]));

  return section;
}

// ---------------------------------------------------------------------------
// Expectation outcomes — the typed Predicate / Rubric verdicts (§4.5).
// ---------------------------------------------------------------------------
function renderExpectationsSection(epochId, genId, entryId) {
  const section = el('section', { class: 'v2-run-section v2-run-expectations' });
  section.appendChild(el('h2', { class: 'v2-run-section-title' }, ['Expectations']));

  const data = ensure('exp', _expectationsCache, 'expectations', epochId, genId, entryId);
  const broke = brokenReason(epochId, genId, entryId, 'exp');
  if (broke) { section.appendChild(stateBlock('broken', { reason: broke })); return section; }
  if (!data) { section.appendChild(stateBlock('running', { label: 'Loading expectations' })); return section; }

  const outcomes = Array.isArray(data.outcomes) ? data.outcomes : [];
  if (outcomes.length === 0) {
    section.appendChild(stateBlock('empty', { label: 'No expectations recorded' }));
    return section;
  }

  const table = el('table', { class: 'v2-run-table v2-num' });
  table.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['#']),
    el('th', null, ['kind']),
    el('th', null, ['verdict']),
    el('th', { class: 'v2-run-th-notes' }, ['notes (judge / score)']),
  ])]));
  const tbody = el('tbody');
  outcomes.forEach((o, idx) => {
    const passed = o.passed === true ? 'PASS' : o.passed === false ? 'FAIL' : '—';
    const vcls = o.passed === true ? 'is-improve' : o.passed === false ? 'is-regress' : '';
    const bits = [];
    if (o.judge_name) bits.push('judge: ' + o.judge_name);
    if (typeof o.score === 'number' && isFinite(o.score)) bits.push('score: ' + o.score.toFixed(3));
    if (o.detail) bits.push(String(o.detail));
    tbody.appendChild(el('tr', null, [
      el('td', null, ['#' + String(idx + 1)]),
      el('td', null, [String(o.kind || '—')]),
      el('td', { class: 'v2-run-verdict ' + vcls }, [passed]),
      el('td', { class: 'v2-run-notes' }, [bits.length ? bits.join(' · ') : '—']),
    ]));
  });
  table.appendChild(tbody);
  section.appendChild(table);
  return section;
}

// ---------------------------------------------------------------------------
// The conversation as evidence — champion | challenger SIDE-BY-SIDE.
// One column per side, aligned turn-by-turn, with drift / steering /
// judge-verdict / plan-revision annotations rendered INLINE (anchored to
// the nearest preceding turn via anchor_seq). The honest zero-turn /
// aborted fallback shows the loss projection, not a blank.
// ---------------------------------------------------------------------------

// Bucket annotations by the turn seq they anchor to. An annotation with
// no (or an unmatched) anchor_seq is hoisted to a leading "pre-turn"
// bucket so it is never silently dropped.
function annotationsBySeq(annotations) {
  const map = new Map();
  const lead = [];
  for (const a of (Array.isArray(annotations) ? annotations : [])) {
    if (a && a.anchor_seq != null) {
      const k = Number(a.anchor_seq);
      if (!map.has(k)) map.set(k, []);
      map.get(k).push(a);
    } else if (a) {
      lead.push(a);
    }
  }
  return { map, lead };
}

const _ANNOTATION_GLYPH = {
  drift: '≈', steering: '⇅', judge: '⚖', plan: '✎',
};

function annotationNode(a) {
  const kind = String((a && a.kind) || '').toLowerCase();
  const glyph = _ANNOTATION_GLYPH[kind] || '•';
  // Judge verdict / drift carry a signal; steering / plan stay neutral.
  // A judge annotation flips to caution/regress when its detail says so;
  // we keep it neutral unless the summary clearly fired.
  const summary = (a && a.summary) ? String(a.summary) : kind;
  return el('div', {
    class: 'v2-run-annot', 'data-kind': kind || 'note',
  }, [
    el('span', { class: 'v2-run-annot-glyph', 'aria-hidden': 'true' }, [glyph]),
    el('span', { class: 'v2-run-annot-kind' }, [kind || 'note']),
    el('span', { class: 'v2-run-annot-summary' }, [summary]),
  ]);
}

function turnNode(turn) {
  const agent = (turn && turn.agent) ? String(turn.agent) : '';
  const role = (turn && turn.role) ? String(turn.role) : 'agent';
  const kindRaw = (turn && turn.kind) ? String(turn.kind) : '';

  const meta = el('div', { class: 'v2-run-turn-meta' });
  if (agent) meta.appendChild(el('span', { class: 'v2-run-turn-agent' }, [agent]));
  meta.appendChild(el('span', { class: 'v2-run-turn-role', 'data-role': role }, [role]));
  if (kindRaw) meta.appendChild(el('span', { class: 'v2-run-turn-kind v2-num' }, [kindRaw]));
  if (turn && turn.seq != null) {
    meta.appendChild(el('span', { class: 'v2-run-turn-seq v2-num' }, ['#' + String(turn.seq)]));
  }
  if (turn && turn.ts) {
    meta.appendChild(el('span', { class: 'v2-run-turn-ts v2-num' }, [String(turn.ts).slice(11, 19)]));
  }

  const card = el('div', { class: 'v2-run-turn', 'data-role': role }, [meta]);
  if (turn && turn.text) {
    card.appendChild(el('div', { class: 'v2-run-turn-text' }, [String(turn.text)]));
  }

  const tcs = (turn && Array.isArray(turn.tool_calls)) ? turn.tool_calls : [];
  for (const tc of tcs) {
    const argsText = tc && tc.args != null
      ? (typeof tc.args === 'string' ? tc.args : JSON.stringify(tc.args)) : '';
    card.appendChild(el('div', { class: 'v2-run-tool is-call' }, [
      el('span', { class: 'v2-run-tool-glyph', 'aria-hidden': 'true' }, ['→']),
      el('span', { class: 'v2-run-tool-name v2-num' }, [tc && tc.name ? String(tc.name) : 'tool']),
      argsText ? el('span', { class: 'v2-run-tool-args v2-num' }, [argsText.slice(0, 240)]) : null,
    ].filter(Boolean)));
  }
  const trs = (turn && Array.isArray(turn.tool_results)) ? turn.tool_results : [];
  for (const tr of trs) {
    const resultText = tr && tr.result ? String(tr.result) : '';
    card.appendChild(el('div', { class: 'v2-run-tool is-result' }, [
      el('span', { class: 'v2-run-tool-glyph', 'aria-hidden': 'true' }, ['←']),
      el('span', { class: 'v2-run-tool-name v2-num' }, [tr && tr.name ? String(tr.name) : 'result']),
      resultText ? el('span', { class: 'v2-run-tool-args' }, [resultText.slice(0, 240)]) : null,
    ].filter(Boolean)));
  }
  return card;
}

// One transcript column. `data` is the cached payload (null = loading);
// `side` ∈ 'champion' | 'challenger' drives the column class + label.
// Renders annotations inline, anchored to the turn they sit after.
function transcriptColumn(side, genId, data, brokenMsg) {
  const isChamp = side === 'champion';
  const col = el('div', { class: 'v2-run-col is-' + side });
  const head = el('div', { class: 'v2-run-col-head' }, [
    el('span', { class: 'v2-run-col-label' }, [isChamp ? 'champion' : 'challenger']),
    genId ? el('span', { class: 'v2-run-col-gen v2-num' }, [String(genId)]) : null,
    (data && data.run_id) ? el('span', { class: 'v2-run-col-runid v2-num' },
      ['run · ' + String(data.run_id)]) : null,
    (data && typeof data.event_count === 'number')
      ? el('span', { class: 'v2-run-col-events v2-num' }, [String(data.event_count) + ' ev']) : null,
  ].filter(Boolean));
  col.appendChild(head);

  if (brokenMsg) { col.appendChild(stateBlock('broken', { reason: brokenMsg })); return col; }
  if (!data) { col.appendChild(stateBlock('running', { label: 'Loading transcript' })); return col; }

  const turns = Array.isArray(data.turns) ? data.turns : [];
  if (turns.length === 0) {
    if (data.run_id == null) {
      // No run on this side at all — genuinely empty (eg. the seed has
      // no champion, or the compare target never ran).
      col.appendChild(stateBlock('empty', {
        label: isChamp ? 'No champion run' : 'No transcript recorded',
        detail: isChamp ? 'This generation has no parent to compare against.' : null,
      }));
      return col;
    }
    // The honest zero-turn / aborted case: the reducer returned a run_id
    // but no turns (eg. a wall-clock timeout). Show the loss projection
    // (the run_id fact), not a blank panel.
    col.appendChild(el('div', { class: 'v2-run-no-turns' }, [
      el('div', { class: 'v2-run-no-turns-headline' }, ['This run produced no transcript turns.']),
      el('div', { class: 'v2-run-no-turns-fact v2-num' }, ['run · ' + String(data.run_id)]),
    ]));
    return col;
  }

  const { map, lead } = annotationsBySeq(data.annotations);
  const body = el('div', { class: 'v2-run-col-body' });
  for (const a of lead) body.appendChild(annotationNode(a));

  let lastRunIndex = null;
  for (const turn of turns) {
    const ri = (turn && typeof turn.run_index === 'number') ? turn.run_index : 1;
    if (lastRunIndex !== null && ri !== lastRunIndex) {
      body.appendChild(el('div', { class: 'v2-run-multi-sep' }, [
        'turn ' + ri + ' of multi-turn entry',
      ]));
    }
    lastRunIndex = ri;
    body.appendChild(turnNode(turn));
    // Inline annotations anchored to this turn's seq, immediately after.
    const seq = (turn && turn.seq != null) ? Number(turn.seq) : null;
    if (seq != null && map.has(seq)) {
      for (const a of map.get(seq)) body.appendChild(annotationNode(a));
    }
  }
  col.appendChild(body);
  return col;
}

function renderTranscriptSection(epochId, genId, championId, entryId) {
  const section = el('section', { class: 'v2-run-section v2-run-transcript' });
  section.appendChild(el('h2', { class: 'v2-run-section-title' }, [
    'Conversation', el('span', { class: 'v2-run-section-sub' },
      [championId ? ' — champion | challenger' : ' — challenger only']),
  ]));

  // Fetch both sides (champion first so it lands as the left column).
  const champData = championId
    ? ensure('tx', _transcriptCache, 'transcript', epochId, championId, entryId) : null;
  const champBroke = championId
    ? brokenReason(epochId, championId, entryId, 'tx') : null;
  const focusData = ensure('tx', _transcriptCache, 'transcript', epochId, genId, entryId);
  const focusBroke = brokenReason(epochId, genId, entryId, 'tx');

  if (championId) {
    // Side-by-side BY DEFAULT — champion left, challenger right.
    const cols = el('div', { class: 'v2-run-cols' }, [
      transcriptColumn('champion', championId, champData, champBroke),
      transcriptColumn('challenger', genId, focusData, focusBroke),
    ]);
    section.appendChild(cols);
  } else {
    // Seed generation (no champion) — a single honest column.
    const cols = el('div', { class: 'v2-run-cols is-solo' }, [
      transcriptColumn('challenger', genId, focusData, focusBroke),
    ]);
    section.appendChild(cols);
  }
  return section;
}

// ---------------------------------------------------------------------------
// Per-judge attribution — the weighted-loss breakdown for the focused
// run. Honest message when not available (§4.5).
// ---------------------------------------------------------------------------
function renderJudgesSection(epochId, genId, entryId) {
  const section = el('section', { class: 'v2-run-section v2-run-judges' });
  section.appendChild(el('h2', { class: 'v2-run-section-title' }, ['Per-judge']));

  const data = ensure('judges', _judgesCache, 'per-judge', epochId, genId, entryId);
  const broke = brokenReason(epochId, genId, entryId, 'judges');
  if (broke) { section.appendChild(stateBlock('broken', { reason: broke })); return section; }
  if (!data) { section.appendChild(stateBlock('running', { label: 'Loading per-judge breakdown' })); return section; }

  const judges = Array.isArray(data.judges) ? data.judges : [];
  if (judges.length === 0) {
    section.appendChild(stateBlock('empty', {
      label: 'No per-judge data',
      detail: data.note ? String(data.note) : null,
    }));
    return section;
  }

  const table = el('table', { class: 'v2-run-table v2-num' });
  table.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['judge']),
    el('th', null, ['weighted loss']),
    el('th', null, ['raw loss']),
    el('th', null, ['weight']),
  ])]));
  const tbody = el('tbody');
  for (const j of judges) {
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'v2-run-judge-name' }, [String(j.judge_name || '—')]),
      el('td', null, [fmtScalar(j.weighted_loss)]),
      el('td', null, [fmtScalar(j.raw_loss)]),
      el('td', null, [fmtScalar(j.weight)]),
    ]));
  }
  table.appendChild(tbody);
  section.appendChild(table);
  return section;
}

// ---------------------------------------------------------------------------
// The view entry point. Rebuilds the host's body from scratch each call;
// the shell's coarse view-swap owns mount/unmount, and fetch-completion
// repaints flow back through here. Idempotent + safe to call on any tick.
// ---------------------------------------------------------------------------
export function renderRun(host, route) {
  if (!host) return;
  _host = host;
  _route = route;

  const params = (route && route.params) || {};
  const entryId = params.entryId || null;
  const genId = params.generationId || null;

  clearChildren(host);
  host.appendChild(el('h1', { class: 'v2-view-title' }, ['Run']));

  if (!entryId || !genId) {
    // The route is under-specified — a Run needs both the entry and the
    // generation to resolve a run. Honest not-yet rather than a blank.
    host.appendChild(stateBlock('not_yet', {
      label: 'No run selected',
      detail: 'Open a run from the Experiment view or a tournament cell.',
    }));
    return;
  }

  const epochId = resolveEpochId(genId);
  const championId = resolveChampionId(genId);

  // A run identity strip so the operator always knows what they are
  // looking at, even before any async section lands.
  host.appendChild(el('div', { class: 'v2-run-identity v2-num' }, [
    el('span', { class: 'v2-run-identity-bit' }, ['entry · ' + entryId]),
    el('span', { class: 'v2-run-identity-bit' }, ['gen · ' + genId]),
    championId ? el('span', { class: 'v2-run-identity-bit' }, ['vs · ' + championId]) : null,
    epochId ? el('span', { class: 'v2-run-identity-bit v2-run-identity-epoch' }, ['epoch · ' + epochId]) : null,
  ].filter(Boolean)));

  if (!epochId) {
    // The epoch has not hydrated yet (cold deep-link before the SSE
    // snapshot). Honest running state — re-renders once lineage lands.
    host.appendChild(stateBlock('running', {
      label: 'Resolving epoch',
      detail: 'Waiting for the lineage snapshot to identify this run.',
    }));
    return;
  }

  const body = el('div', { class: 'v2-run-body' });
  body.appendChild(renderHeaderSection(epochId, genId, entryId));
  body.appendChild(renderExpectationsSection(epochId, genId, entryId));
  body.appendChild(renderTranscriptSection(epochId, genId, championId, entryId));
  body.appendChild(renderJudgesSection(epochId, genId, entryId));
  host.appendChild(body);
}

// Self-register with the shell so a `#/v2/run/...` route resolves here.
import { registerView } from '../shell.js';
registerView('run', renderRun);

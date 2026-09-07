// js/views/evals.js — the top-level EVALS view (EVAL-VIEW.md §5, WS-MATRIX).
//
// THE OUTCOMES LENS. The transpose of the candidate-centric UI: ROWS are board
// entries (the measurement instrument), COLUMNS are candidates (what the
// instrument measured). One cell = how candidate c scored on entry e, read off
// `/api/epoch/{id}/evals` (query.build_eval_matrix). This is the entries ×
// candidates matrix rendered in the shipped `dn-mtx` grid grammar.
//
// STATISTICAL HONESTY (EVAL-VIEW.md §4 — the view MUST obey the SERVED verdict,
// never re-derive it):
//   * SHADE BY EVIDENCE rather than by verdict — a single-sample cell renders
//     FAINT (`dn-faint`), a replicated one FIRM. The tier is the SERVED
//     `cell.evidence` (none/single/replicated); the view never counts replicates.
//   * A FAILURE renders beside its row's flip-rate context — every entry row
//     carries its `flip_rate` badge (or "unmeasured"), so a lone red cell is
//     never read as truth on a noisy channel.
//   * NO FABRICATED NUMBERS — `flip_rate_measured: false` prints "unmeasured",
//     NEVER 0.0.
//
// RENDER DISCIPLINE. Fetch-then-gatedSwap: the payload + the active filters +
// the liveness of the harmonograf link fold into ONE content digest and repaint
// via gatedSwap, so a no-op SSE beat (identical digest) rebuilds ZERO DOM (the
// house rule). Toggling a filter changes the digest → one repaint. A cold index
// / unknown epoch / empty board degrades to an honest empty state, never an
// error. The matrix scrolls in its OWN `dn-table-scroll` container — the page
// body never scrolls horizontally.
//
// STRUCTURE-AGNOSTIC. The served payload is the same shape for a gauntlet and a
// multi-challenger (racing / swiss / elim) epoch — this view reads columns and
// cells verbatim and renders both without branching on structure.

import { el, svgEl } from '../core/dom.js';
import { state } from '../core/state.js';
import * as D from '../data.js';
import * as M from '../matrix.js';
import { section, empty, gatedSwap, verdictPill } from '../ui.js';
import { epochIsLive } from '../livestatus.js';
import { CROWN, fmt } from '../svg.js';
import { harmonografMini, harmonografIsLive } from '../core/harmonograf.js';
import { mount as mountEvalHealth } from '../panels/evals_health.js';

// ── module-level filter state ─────────────────────────────────────────
// Persists across the shell's SSE-driven re-dispatch so a steady beat keeps the
// operator's chosen filters rather than resetting them each tick (the logs.js
// idiom). All three are CLIENT-SIDE over the served payload.
let _failuresOnly = false;   // rows with at least one failing cell
// flips-only = a CROSS-COLUMN verdict change (a cell whose verdict differs from
// the previous non-null column) — "what did this candidate MOVE" (EVAL-VIEW.md
// §5). This is NOT the entry-noise (A/A flip-rate) signal, which lives on the
// per-row flip badge; a noisy channel with no cross-column change is excluded.
let _flipsOnly = false;
let _holdoutOnly = false;    // rows in the holdout slice

// A short display id for a generation column (the ids are already short —
// 'v0' / 'gen-0042' — so this only guards a pathological long id).
function shortId(s, n) {
  const str = String(s == null ? '' : s);
  const cap = n || 14;
  return str.length > cap ? str.slice(0, cap - 1) + '…' : str;
}

// The flip-rate badge for an entry row (EVAL-VIEW.md §4.2 / §4.4). Measured →
// the percentage, toned by magnitude; unmeasured → the honest "unmeasured"
// word, NEVER a fabricated 0. Read straight off the served entry — the view
// never computes a flip rate.
function flipBadge(entry) {
  if (!entry || entry.flip_rate_measured !== true || typeof entry.flip_rate !== 'number') {
    return el('span', {
      class: 'dn-eval-flip dn-eval-flip-unmeasured dn-faint',
      title: 'A/A flip rate unmeasured — no calibration was run for this epoch',
    }, ['flip unmeasured']);
  }
  const pct = Math.round(entry.flip_rate * 100);
  // a noisy channel (any flip) earns caution; a clean 0% reads quiet-good.
  const tone = pct === 0 ? 'dn-eval-flip-clean' : (pct >= 20 ? 'dn-eval-flip-hot' : 'dn-eval-flip-warm');
  // N4: name the calibrated champion so a STALE flip rate (measured on an older
  // champion than the current spine tip) is visible in the badge tooltip.
  const onGen = entry.calibration_generation ? ' on ' + entry.calibration_generation : '';
  return el('span', {
    class: 'dn-eval-flip ' + tone,
    title: 'A/A flip rate ' + pct + '% over ' + (entry.calibration_runs || 0)
      + ' calibration draws' + onGen
      + ' — the fraction of self-duel draws whose verdict flipped',
  }, ['flip ' + pct + '%']);
}

// Does an entry row survive the active filters? Row-level (the matrix stays
// intact): failures-only keeps a row with any failing cell; flips-only keeps a
// row whose verdict differs between two adjacent columns; holdout-only keeps a
// holdout-slice row. The filters compose (AND).
function rowPasses(entry, row) {
  if (_holdoutOnly && entry.slice !== 'holdout') return false;
  if (_failuresOnly && !row.some((c) => c && c.pass_fail === false)) return false;
  if (_flipsOnly && !rowHasFlip(row)) return false;
  return true;
}

// A flip: a cell whose verdict differs from the PREVIOUS NON-NULL column (§5) —
// the candidate moved this entry's verdict. Null cells are skipped (not a move).
function rowHasFlip(row) {
  let prev = null;
  for (const c of row) {
    if (c && typeof c.pass_fail === 'boolean') {
      if (prev !== null && c.pass_fail !== prev) return true;
      prev = c.pass_fail;
    }
  }
  return false;
}

// ── GHOST ROWS (TRAJECTORY-UI.md §2.2b — the "board being created") ────
// A stable content digest — the served fields the render reads, the filter
// state, and the harmonograf liveness (the deep-link appears/disappears with
// it). NO timestamps, NO raw floats beyond a rounded drift, so a no-op beat is
// byte-identical. The ghost feed is appended ONLY when present (byte-identical
// no-ghost pin).
function digestOf(matrix, live, epochLive) {
  if (!matrix) return 'evals|null|' + fbits();
  if (!matrix.found) return 'evals|notfound|' + (matrix.epoch_id || '') + '|' + fbits();
  // promoted is TRISTATE (true / false / null) — fold a 3-state token so a
  // never-raced null candidate is DISTINCT from a rejected false one (the
  // Class-B bug: null must never collapse into false).
  const promo3 = (p) => (p === true ? 1 : p === false ? 0 : 'n');
  const cands = (matrix.candidates || []).map((c) =>
    [c.generation_id, c.round_index, c.champion_spine ? 1 : 0, promo3(c.promoted), c.seed ? 1 : 0]);
  const rows = (matrix.entries || []).map((e) =>
    [e.entry_id, e.slice, e.flip_rate_measured ? Math.round((e.flip_rate || 0) * 100) : 'u']);
  const cells = (matrix.cells || []).map((row) => (row || []).map((c) =>
    c ? [c.evidence, c.pass_fail === true ? 1 : c.pass_fail === false ? 0 : 'n',
      c.cached ? 1 : 0, Math.round((c.drift_loss || 0) * 100)] : 0));
  const cal = matrix.calibration || {};
  return 'evals|' + JSON.stringify({
    ep: matrix.epoch_id, c: cands, r: rows, x: cells,
    cal: [cal.measured ? 1 : 0, cal.runs || 0, Math.round((cal.max_abs_delta || 0) * 1000)],
    // BOTH liveness reads are view-visible: the harmonograf deep-link appears
    // with `live`, and the pending pill's TENSE ("racing…" vs "undecided")
    // moves with `epochLive`. A digest blind to either would freeze the stale
    // wording on the beat that settles the loop.
    live: live ? 1 : 0, el: epochLive ? 1 : 0, f: fbits(),
  });
}
function fbits() {
  return (_failuresOnly ? 'F' : '-') + (_flipsOnly ? 'L' : '-') + (_holdoutOnly ? 'H' : '-');
}

export async function render(host, ctx, params, _route) {
  if (!host) return;
  const epochId = (params && params.epochId) || null;
  const matrix = epochId ? await D.evalMatrix(epochId) : null;
  const live = harmonografIsLive();
  // Is the loop running FOR THIS EPOCH? The verdict pills' tense hangs off it:
  // an undecided candidate in an epoch nothing is racing did not stay in the
  // race — the race ended without deciding it (issue #207 §2).
  const epochLive = epochIsLive(state, epochId);
  gatedSwap(host, digestOf(matrix, live, epochLive),
    () => build(host, ctx, matrix, epochId, live, epochLive));
}

function build(host, ctx, matrix, epochId, live, epochLive) {
  const nodes = [];
  nodes.push(el('div', { class: 'dn-pagehead' }, [
    el('h1', { class: 'dn-h1', text: 'Evals' }),
    el('p', { class: 'dn-lede', text: 'The board as the measurement instrument: rows are entries (the channels), columns are candidates (what they measured). Each cell is how a candidate scored on an entry — shaded by evidence, not by verdict, with the entry’s A/A flip-rate context beside every row.' }),
  ]));

  // A null payload is a transport failure; a found:false payload is the honest
  // cold-index / unknown-epoch degrade.
  if (!matrix) {
    nodes.push(section('Matrix', el('div', { class: 'dn-panel' }, [
      empty('The eval matrix is unavailable right now.'),
    ])));
    return nodes;
  }
  if (!matrix.found) {
    nodes.push(section('Matrix', el('div', { class: 'dn-panel' }, [
      empty(matrix.note || 'No such epoch, or this workspace has never been indexed.'),
    ])));
    return nodes;
  }

  const candidates = Array.isArray(matrix.candidates) ? matrix.candidates : [];
  const entries = Array.isArray(matrix.entries) ? matrix.entries : [];
  const cells = Array.isArray(matrix.cells) ? matrix.cells : [];

  // ── the instrument-health panel (EVAL-VIEW.md §5) ──
  // The panel (panels/evals_health.js) paints a strip ABOVE the matrix and a
  // section BELOW it. This view owns the two host containers; the panel owns
  // their contents and fetches its own payload, so its mount is not awaited:
  // the matrix paints first and the panel fills its hosts when its read lands.
  const stripHost = el('div', { class: 'dn-evals-health-strip' });
  const sectionHost = el('section', { class: 'dn-evals-health-section' });
  nodes.push(stripHost);
  mountEvalHealth({ strip: stripHost, section: sectionHost }, matrix, ctx);

  if (!candidates.length && !entries.length) {
    nodes.push(section('Matrix', el('div', { class: 'dn-panel' }, [
      empty('This epoch has no scored candidates or board entries yet.'),
    ])));
    nodes.push(sectionHost);
    return nodes;
  }

  // ── the calibration caption + filter toolbar ──────────────────────────
  nodes.push(buildToolbar(host, ctx, matrix));

  // ── the matrix ────────────────────────────────────────────────────────
  nodes.push(buildMatrix(ctx, epochId, candidates, entries, cells, live, epochLive));

  // the health SECTION (ranked lists) sits below the matrix.
  nodes.push(sectionHost);
  return nodes;
}

function buildToolbar(host, ctx, matrix) {
  const cal = matrix.calibration || {};
  const toolbar = el('div', { class: 'dn-evals-toolbar' });

  // the calibration provenance line — the measured floor the flip rates ride on
  // (honest "unmeasured" when no calibration was run; never a fabricated bound).
  const calText = cal.measured
    ? ('A/A calibration · ' + (cal.runs || 0) + ' draws on '
       + (cal.generation_id || 'champion')
       + (typeof cal.max_abs_delta === 'number' ? ' · floor ' + fmt(cal.max_abs_delta, 3) : ''))
    : 'A/A calibration unmeasured — flip rates are unmeasured for this epoch';
  toolbar.appendChild(el('span', { class: 'dn-evals-cal dn-faint', text: calText }));

  const chips = el('div', { class: 'dn-evals-filters', role: 'group', 'aria-label': 'Matrix filters' });
  const defs = [
    ['failures', 'failures only', () => _failuresOnly, (v) => { _failuresOnly = v; }],
    ['flips', 'flips only', () => _flipsOnly, (v) => { _flipsOnly = v; }],
    ['holdout', 'holdout only', () => _holdoutOnly, (v) => { _holdoutOnly = v; }],
  ];
  for (const [key, label, get, set] of defs) {
    const active = get();
    const chip = el('button', {
      class: 'dn-evals-chip' + (active ? ' dn-evals-chip-on' : ''),
      type: 'button', 'data-filter': key,
      'aria-pressed': active ? 'true' : 'false',
      text: label,
    });
    chip.addEventListener('click', () => {
      set(!get());
      render(host, ctx, { epochId: matrix.epoch_id });
    });
    chips.appendChild(chip);
  }
  toolbar.appendChild(chips);
  return toolbar;
}

function buildMatrix(ctx, epochId, candidates, entries, cells, live, epochLive) {
  const table = M.matrixTable('dn-evalmtx');

  // ── column group header (round_index grouped) + the candidate header ──
  const thead = el('thead');
  const groupRow = roundGroupRow(candidates);
  if (groupRow) thead.appendChild(groupRow);
  const hr = el('tr', { class: 'dn-evalmtx-headrow' });
  hr.appendChild(M.matrixCorner('entry · candidate →'));
  for (const c of candidates) {
    hr.appendChild(candidateHeader(ctx, epochId, c, epochLive));
  }
  thead.appendChild(hr);
  table.appendChild(thead);

  // ── body: one row per entry, filtered client-side ─────────────────────
  const tbody = el('tbody');
  let shown = 0;
  entries.forEach((entry, ri) => {
    const row = Array.isArray(cells[ri]) ? cells[ri] : [];
    if (!rowPasses(entry, row)) return;
    shown += 1;
    const tr = M.matrixRow({ extra: 'dn-evalmtx-row' + (entry.slice === 'holdout' ? ' dn-evalmtx-holdout' : '') });
    tr.appendChild(entryHeader(entry));
    candidates.forEach((c, ci) => {
      tr.appendChild(cellNode(ctx, epochId, entry, c, row[ci], live));
    });
    tbody.appendChild(tr);
  });

  table.appendChild(tbody);

  const wrap = el('div', { class: 'dn-evalmtx-wrap' });
  wrap.appendChild(M.matrixScroll(table));
  if (!shown) {
    wrap.appendChild(el('p', { class: 'dn-empty', text: 'No entries match the active filters.' }));
  }
  wrap.appendChild(el('p', { class: 'dn-faint dn-evalmtx-legend', text: 'row = board entry · column = candidate · ' + CROWN.current + ' = champion spine · faint cell = single-sample (unreplicated) · click a cell for its transcript' }));
  return section('Matrix', wrap);
}

function roundGroupRow(candidates) {
  if (!candidates.some((c) => Number.isInteger(c.round_index))) return null;
  const tr = el('tr', { class: 'dn-evalmtx-grouprow' });
  tr.appendChild(M.matrixCorner(null, { extra: 'dn-evalmtx-groupcorner', attrs: { 'aria-hidden': 'true' } }));
  let i = 0;
  while (i < candidates.length) {
    const r = candidates[i].round_index;
    let span = 1;
    while (i + span < candidates.length && candidates[i + span].round_index === r) span += 1;
    tr.appendChild(el('th', {
      class: 'dn-evalmtx-group', colspan: String(span), scope: 'colgroup',
      text: Number.isInteger(r) ? ('round ' + r) : '—',
    }));
    i += span;
  }
  return tr;
}

// A candidate column header: the champion-spine crown + the gen id + the served
// decision verdict pill (reusing the shipped dn-pill vocabulary — NO new chip).
//
// The decision comes from the server-owned candidate payload rather than from a
// local re-reading of `promoted`. Deriving it inline here cannot see the seed or
// the settle-time lineage record, so an epoch whose challengers were all
// rejected renders every column as "racing…". The server stamps `promoted` off
// the one lineage authority and flags the `seed`; `epochLive` puts the
// still-undecided ones in the past
// tense when the loop that would decide them is not running.
function candidateHeader(ctx, epochId, c, epochLive) {
  const spine = c.champion_spine === true;
  const seed = c.seed === true;
  const kids = [];
  if (spine) {
    kids.push(el('span', {
      class: 'dn-evalmtx-crown', 'aria-label': 'champion spine',
      title: seed ? 'the seed — the champion this epoch started from' : 'on the promoted-champion spine',
      text: CROWN.current,
    }));
  }
  kids.push(M.matrixColumnLabel(shortId(c.generation_id, 14), {
    extra: 'dn-evalmtx-genlink',
    href: ctx.href('candidate', { epochId, gen: c.generation_id }),
  }));
  // the shipped decision vocabulary, TRISTATE (§3.1 / F1): the seed → 'baseline'
  // (it faced no gate, so it never WON one), promoted → dn-promoted, rejected →
  // dn-rejected, null (in-flight / never raced) → the shipped 'pending' pill —
  // NEVER collapse a null into rejected (the Class-B bug).
  const decision = c.decision || 'pending';
  kids.push(verdictPill(decision, { live: epochLive, label: c.decision_label }));
  return M.matrixColumnHeader({
    extra: 'dn-evalmtx-gen' + (spine ? ' dn-evalmtx-spine' : ''),
    attrs: { scope: 'col', 'data-gen': String(c.generation_id) },
  }, [el('div', { class: 'dn-evalmtx-genhead' }, kids)]);
}

// An entry row header: the entry id + the holdout marker + the flip-rate badge.
function entryHeader(entry) {
  const kids = [el('span', { class: 'dn-mtx-file dn-evalmtx-entry', text: entry.entry_id })];
  if (entry.slice === 'holdout') {
    kids.push(el('span', { class: 'dn-evalmtx-holdout-tag', title: 'held-out entry (not scored into the gate)', text: 'holdout' }));
  }
  kids.push(flipBadge(entry));
  return M.matrixRowHeader({
    extra: 'dn-evalmtx-site',
    attrs: { 'data-entry': String(entry.entry_id) },
  }, [el('div', { class: 'dn-evalmtx-sitehead' }, kids)]);
}

// One matrix cell. A missing cell (null) is a blank dot. A present cell renders
// a verdict mark toned by pass/fail, SHADED BY EVIDENCE (single → faint), with
// a cached marker when the result was carried over (never a fresh measurement),
// clickable through to the run transcript + a harmonograf deep-link when live.
function cellNode(ctx, epochId, entry, cand, cell, live) {
  if (!cell) {
    return M.matrixCell(false, { extra: 'dn-evalmtx-cell dn-evalmtx-none' }, [M.matrixBlank()]);
  }
  const pass = cell.pass_fail;
  const tone = pass === true ? 'dn-evalmtx-pass' : pass === false ? 'dn-evalmtx-fail' : 'dn-evalmtx-neutral';
  // SHADE BY EVIDENCE (EVAL-VIEW.md §4.1): a single-sample verdict renders
  // FAINT; a replicated one FIRM. The tier is the SERVED evidence, never a
  // client-side replicate count.
  const evid = cell.evidence === 'replicated' ? 'dn-evalmtx-firm'
    : cell.evidence === 'single' ? 'dn-evalmtx-single dn-faint' : 'dn-evalmtx-single dn-faint';
  const td = M.matrixCell(false, {
    extra: 'dn-evalmtx-cell ' + tone + ' ' + evid + (cell.cached ? ' dn-evalmtx-cached' : ''),
    attrs: {
      'data-entry': String(entry.entry_id), 'data-gen': String(cand.generation_id),
      'data-evidence': String(cell.evidence || ''),
      'data-pass': pass === true ? 'pass' : pass === false ? 'fail' : 'none',
    },
  });

  const drift = typeof cell.drift_loss === 'number' ? fmt(cell.drift_loss, 2) : '—';
  const title = [
    entry.entry_id + ' × ' + cand.generation_id,
    'verdict ' + (pass === true ? 'pass' : pass === false ? 'fail' : 'unresolved'),
    'evidence ' + (cell.evidence || 'none') + ' (' + (cell.replicates || 0) + ' run'
      + ((cell.replicates || 0) === 1 ? '' : 's') + ')',
    'drift ' + drift,
    typeof cell.pass_ratio === 'number' ? 'pass-ratio ' + fmt(cell.pass_ratio, 2) : null,
    cell.cached ? 'cached (carried over — not a fresh measurement)' : null,
  ].filter(Boolean).join(' · ');

  // the click-through into the run transcript — the board view's existing
  // navigation (an anchor so the href is real + assertable, the shipped idiom).
  const link = el('a', {
    class: 'dn-evalmtx-celllink',
    href: ctx.href('board', { epochId, entry: entry.entry_id, gen: cand.generation_id }),
    title,
    'aria-label': title,
  }, [
    M.matrixMarkFrame(14, { extra: 'dn-evalmtx-mark' }, [
      pass === false
        ? svgEl('path', { class: 'dn-evalmtx-glyph', d: 'M3 3 L11 11 M11 3 L3 11', 'stroke-width': 2, fill: 'none' })
        : svgEl('rect', { x: 3, y: 3, width: 8, height: 8, rx: 2, class: 'dn-evalmtx-square' }),
    ]),
    el('span', { class: 'dn-evalmtx-drift', text: drift }),
  ]);
  td.appendChild(link);
  if (cell.cached) {
    td.appendChild(el('span', { class: 'dn-evalmtx-cachemark dn-faint', title: 'carried-over cached result', 'aria-hidden': 'true', text: '↻' }));
  }
  // the harmonograf deep-link — rendered ONLY while a run is live (the helper
  // returns null otherwise), keyed on the cell's latest run id.
  if (live) {
    const hg = harmonografMini({ run_id: cell.latest_run_id }, 'h', 'open the harmonograf trace for this run');
    if (hg) {
      hg.classList.add('dn-evalmtx-hg');
      td.appendChild(hg);
    }
  }
  return td;
}

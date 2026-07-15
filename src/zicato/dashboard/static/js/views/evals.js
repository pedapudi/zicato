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
//   * SHADE BY EVIDENCE, not by verdict — a single-sample cell renders FAINT
//     (`dn-faint`), a replicated one FIRM. The tier is the SERVED `cell.evidence`
//     (none/single/replicated); the view never counts replicates itself (DQ1).
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
import * as D from '../data.js';
import { section, empty, gatedSwap, verdictPill } from '../ui.js';
import { CROWN, fmt } from '../svg.js';
import { harmonografMini, harmonografIsLive } from '../core/harmonograf.js';

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

// A stable content digest — the served fields the render reads, the filter
// state, and the harmonograf liveness (the deep-link appears/disappears with
// it). NO timestamps, NO raw floats beyond a rounded drift, so a no-op beat is
// byte-identical.
function digestOf(matrix, live) {
  if (!matrix) return 'evals|null|' + fbits();
  if (!matrix.found) return 'evals|notfound|' + (matrix.epoch_id || '') + '|' + fbits();
  // promoted is TRISTATE (true / false / null) — fold a 3-state token so a
  // never-raced null candidate is DISTINCT from a rejected false one (the
  // Class-B bug: null must never collapse into false).
  const promo3 = (p) => (p === true ? 1 : p === false ? 0 : 'n');
  const cands = (matrix.candidates || []).map((c) =>
    [c.generation_id, c.round_index, c.champion_spine ? 1 : 0, promo3(c.promoted)]);
  const rows = (matrix.entries || []).map((e) =>
    [e.entry_id, e.slice, e.flip_rate_measured ? Math.round((e.flip_rate || 0) * 100) : 'u']);
  const cells = (matrix.cells || []).map((row) => (row || []).map((c) =>
    c ? [c.evidence, c.pass_fail === true ? 1 : c.pass_fail === false ? 0 : 'n',
      c.cached ? 1 : 0, Math.round((c.drift_loss || 0) * 100)] : 0));
  const cal = matrix.calibration || {};
  return 'evals|' + JSON.stringify({
    ep: matrix.epoch_id, c: cands, r: rows, x: cells,
    cal: [cal.measured ? 1 : 0, cal.runs || 0, Math.round((cal.max_abs_delta || 0) * 1000)],
    live: live ? 1 : 0, f: fbits(),
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
  gatedSwap(host, digestOf(matrix, live), () => build(host, ctx, matrix, epochId, live));
}

function build(host, ctx, matrix, epochId, live) {
  const nodes = [];
  nodes.push(el('div', { class: 'dt-pagehead' }, [
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

  // ── the WS-HEALTH mount seam (adjudicated placement, EVAL-VIEW.md §5) ──
  // The instrument panel lives as a strip ABOVE the matrix + a section BELOW,
  // built by a SEPARATE module (health-a's evals_health.js). We own the two
  // host containers; its module owns their contents, so the two branches merge
  // without editing each other's lines. A guarded dynamic import: if the module
  // exists and exports `mount`, we call mount({strip, section}, matrix, ctx);
  // absent module / export → the hosts stay empty (nothing extra renders).
  const stripHost = el('div', { class: 'dn-evals-health-strip' });
  const sectionHost = el('section', { class: 'dn-evals-health-section' });
  nodes.push(stripHost);
  mountHealth(stripHost, sectionHost, matrix, ctx);

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
  nodes.push(buildMatrix(ctx, epochId, candidates, entries, cells, live));

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

function buildMatrix(ctx, epochId, candidates, entries, cells, live) {
  const table = el('table', { class: 'dn-mtx dn-evalmtx' });

  // ── column group header (round_index grouped) + the candidate header ──
  const thead = el('thead');
  const groupRow = roundGroupRow(candidates);
  if (groupRow) thead.appendChild(groupRow);
  const hr = el('tr', { class: 'dn-evalmtx-headrow' });
  hr.appendChild(el('th', { class: 'dn-mtx-corner', text: 'entry · candidate →' }));
  for (const c of candidates) {
    hr.appendChild(candidateHeader(ctx, epochId, c));
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
    const tr = el('tr', { class: 'dn-mtx-row dn-evalmtx-row' + (entry.slice === 'holdout' ? ' dn-evalmtx-holdout' : '') });
    tr.appendChild(entryHeader(entry));
    candidates.forEach((c, ci) => {
      tr.appendChild(cellNode(ctx, epochId, entry, c, row[ci], live));
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);

  const wrap = el('div', { class: 'dn-evalmtx-wrap' });
  wrap.appendChild(el('div', { class: 'dn-table-scroll' }, [table]));
  if (!shown) {
    wrap.appendChild(el('p', { class: 'dn-empty', text: 'No entries match the active filters.' }));
  }
  wrap.appendChild(el('p', { class: 'dn-faint dn-evalmtx-legend', text: 'row = board entry · column = candidate · ' + CROWN.current + ' = champion spine · faint cell = single-sample (unreplicated) · click a cell for its transcript' }));
  return section('Matrix', wrap);
}

// A super-header row grouping consecutive columns that share a round_index into
// one spanning cell ("round 0" / "round 1"). Returns null when no column
// carries a round_index (nothing to group — a flat gauntlet epoch).
function roundGroupRow(candidates) {
  if (!candidates.some((c) => Number.isInteger(c.round_index))) return null;
  const tr = el('tr', { class: 'dn-evalmtx-grouprow' });
  tr.appendChild(el('th', { class: 'dn-mtx-corner dn-evalmtx-groupcorner', 'aria-hidden': 'true' }));
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
function candidateHeader(ctx, epochId, c) {
  const spine = c.champion_spine === true;
  const kids = [];
  if (spine) {
    kids.push(el('span', { class: 'dn-evalmtx-crown', 'aria-label': 'champion spine', title: 'on the promoted-champion spine', text: CROWN.current }));
  }
  kids.push(el('a', {
    class: 'dn-mtx-genlink dn-evalmtx-genlink',
    href: ctx.href('candidate', { epochId, gen: c.generation_id }),
    text: shortId(c.generation_id, 14),
  }));
  // the shipped decision vocabulary, TRISTATE (§3.1 / F1): promoted → dn-promoted,
  // rejected → dn-rejected, null (in-flight / never raced) → the shipped 'pending'
  // pill ("racing…") — NEVER collapse a null into rejected (the Class-B bug).
  const decision = spine || c.promoted === true ? 'promoted'
    : c.promoted === false ? 'rejected' : 'pending';
  kids.push(verdictPill(decision));
  return el('th', {
    class: 'dn-mtx-gen dn-evalmtx-gen' + (spine ? ' dn-evalmtx-spine' : ''),
    scope: 'col', 'data-gen': String(c.generation_id),
  }, [el('div', { class: 'dn-evalmtx-genhead' }, kids)]);
}

// An entry row header: the entry id + the holdout marker + the flip-rate badge.
function entryHeader(entry) {
  const kids = [el('span', { class: 'dn-mtx-file dn-evalmtx-entry', text: entry.entry_id })];
  if (entry.slice === 'holdout') {
    kids.push(el('span', { class: 'dn-evalmtx-holdout-tag', title: 'held-out entry (not scored into the gate)', text: 'holdout' }));
  }
  kids.push(flipBadge(entry));
  return el('th', {
    class: 'dn-mtx-site dn-evalmtx-site', scope: 'row',
    'data-entry': String(entry.entry_id),
  }, [el('div', { class: 'dn-evalmtx-sitehead' }, kids)]);
}

// One matrix cell. A missing cell (null) is a blank dot. A present cell renders
// a verdict mark toned by pass/fail, SHADED BY EVIDENCE (single → faint), with
// a cached marker when the result was carried over (never a fresh measurement),
// clickable through to the run transcript + a harmonograf deep-link when live.
function cellNode(ctx, epochId, entry, cand, cell, live) {
  if (!cell) {
    return el('td', { class: 'dn-mtx-cell dn-evalmtx-cell dn-evalmtx-none' }, [
      el('span', { class: 'dn-mtx-blank', 'aria-hidden': 'true', text: '·' }),
    ]);
  }
  const pass = cell.pass_fail;
  const tone = pass === true ? 'dn-evalmtx-pass' : pass === false ? 'dn-evalmtx-fail' : 'dn-evalmtx-neutral';
  // SHADE BY EVIDENCE (EVAL-VIEW.md §4.1): a single-sample verdict renders
  // FAINT; a replicated one FIRM. The tier is the SERVED evidence, never a
  // client-side replicate count (DQ1).
  const evid = cell.evidence === 'replicated' ? 'dn-evalmtx-firm'
    : cell.evidence === 'single' ? 'dn-evalmtx-single dn-faint' : 'dn-evalmtx-single dn-faint';
  const cls = 'dn-mtx-cell dn-evalmtx-cell ' + tone + ' ' + evid
    + (cell.cached ? ' dn-evalmtx-cached' : '');
  const td = el('td', {
    class: cls,
    'data-entry': String(entry.entry_id), 'data-gen': String(cand.generation_id),
    'data-evidence': String(cell.evidence || ''),
    'data-pass': pass === true ? 'pass' : pass === false ? 'fail' : 'none',
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
    svgEl('svg', { class: 'dn-mtx-mark dn-evalmtx-mark', width: 14, height: 14, viewBox: '0 0 14 14', role: 'img' }, [
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

// The WS-HEALTH mount (health-a's module). A guarded dynamic import so the view
// degrades cleanly when the module is absent (this branch alone) and lights up
// when it lands (the merged branch). Any import / call failure is swallowed —
// the instrument panel is additive, never load-bearing for the matrix.
function mountHealth(strip, sectionEl, matrix, ctx) {
  import('./evals_health.js').then((mod) => {
    const fn = mod && (mod.mount || mod.mountEvalsHealth || mod.default);
    if (typeof fn === 'function') {
      try { fn({ strip, section: sectionEl }, matrix, ctx); } catch (e) { /* additive — never break the matrix */ }
    }
  }).catch(() => { /* module absent (this branch) → render nothing extra */ });
}

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
import { flipWhisker, discriminationPips, vizFromFeedAdmission } from '../core/admission_viz.js';
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

// The suggestions feed for the GHOST ROWS, cached per epoch (fetched once, off
// the hot path; on arrival it re-renders so the ghosts appear). null → no feed
// (a pre-feature / read-only backend, or none fetched yet) → zero ghost rows.
let _sugFeed = null;
let _sugFeedEpoch = undefined;   // the epochId `_sugFeed` was fetched for
let _builtRoot = null;           // the staleness sentinel for the async ghost repaint
function ensureSugFeed(host, ctx, epochId) {
  if (_sugFeedEpoch === epochId) return;   // already fetched / fetching for this epoch
  _sugFeedEpoch = epochId;
  _sugFeed = null;
  D.builderSuggestions().then((feed) => {
    _sugFeed = feed;
    // STALENESS GUARD (the shell's _renderToken discipline, applied here): the
    // shell reuses ONE persistent view host and clears its children on any
    // selection change, so the sentinel is the node THIS view mounted — if it
    // is disconnected (the operator navigated while the fetch was in flight),
    // repainting would clobber whatever view is showing now.
    const stale = _sugFeedEpoch !== epochId || !_builtRoot || !_builtRoot.isConnected;
    if (!stale) render(host, ctx, { epochId });   // digest-gated: no ghosts → no-op
  }).catch(() => { _sugFeed = null; });
}
// A test seam: seed the feed synchronously so a ghost-row render is deterministic
// (no fetch race), and reset it between cases.
export function _setGhostFeedForTest(feed, epochId) { _sugFeed = feed; _sugFeedEpoch = epochId; }
export function _resetGhostFeedForTest() { _sugFeed = null; _sugFeedEpoch = undefined; }

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
// The suggestions feed drafts board entries that do NOT exist on the board yet.
// Each such pending entry renders as a ghost row appended below the real rows,
// carrying its admission visuals (never a scored verdict). This joins the eval
// matrix with the SAME `/builder/suggestions` feed the inbox reads, client-side:
// a board_entry suggestion whose drafted id is not already a matrix row, scoped
// to THIS epoch (the feed carries its epoch_id). The no-ghost case adds nothing
// (byte-identical to the pre-feature matrix, digest + DOM).
function ghostEntriesFrom(feed, entries, epochId) {
  if (!feed || typeof feed !== 'object') return [];
  // scope to this epoch — never surface another epoch's drafts on this matrix.
  if (epochId != null && feed.epoch_id != null && String(feed.epoch_id) !== String(epochId)) return [];
  const items = Array.isArray(feed.suggestions) ? feed.suggestions : [];
  const have = new Set((entries || []).map((e) => String(e.entry_id)));
  const ghosts = [];
  for (const s of items) {
    if (!s || s.artifact_kind !== 'board_entry') continue;
    const id = ghostEntryId(s);
    if (!id || have.has(String(id))) continue;
    have.add(String(id));   // dedupe repeated drafts of the same id
    ghosts.push({
      entry_id: id,
      suggestion_id: s.suggestion_id || '',
      target_slice: s.target_slice || 'train',
      viz: vizFromFeedAdmission(s.admission),
      href_builder: true,
    });
  }
  return ghosts;
}

// The drafted entry id for a board_entry suggestion — the draft artifact's id,
// falling back to the proposed_op's entry args (the shape the inbox stages).
function ghostEntryId(s) {
  const art = s.draft_artifact;
  if (art && typeof art === 'object' && art.id) return art.id;
  const op = s.proposed_op;
  if (op && op.args && op.args.entry && op.args.entry.id) return op.args.entry.id;
  return null;
}

// The ghost component for the digest — folded ONLY when ghosts exist, so the
// no-ghost digest is byte-identical to the pre-feature string (the pin).
function ghostDigestPart(ghosts) {
  return ghosts.map((g) => {
    const v = g.viz || {};
    const f = v.flip || {};
    const d = v.discrimination || {};
    return [g.entry_id, v.evidence_tier,
      f.measured ? Math.round((f.rate || 0) * 100) : 'u', f.over_ceiling ? 1 : 0,
      d.measured ? [d.separated, d.pairs] : 'u'];
  });
}

// A stable content digest — the served fields the render reads, the filter
// state, and the harmonograf liveness (the deep-link appears/disappears with
// it). NO timestamps, NO raw floats beyond a rounded drift, so a no-op beat is
// byte-identical. The ghost feed is appended ONLY when present (byte-identical
// no-ghost pin).
function digestOf(matrix, live, ghosts, epochLive) {
  const gp = (ghosts && ghosts.length) ? '|g' + JSON.stringify(ghostDigestPart(ghosts)) : '';
  if (!matrix) return 'evals|null|' + fbits() + gp;
  if (!matrix.found) return 'evals|notfound|' + (matrix.epoch_id || '') + '|' + fbits() + gp;
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
  }) + gp;
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
  // the suggestions feed powers the GHOST ROWS. It is fetched ONCE per epoch into
  // a module cache (never awaited in the hot path — the gated render keeps the
  // matrix-only microtask cadence) and re-renders once when it lands; a
  // pre-feature / read-only backend degrades to null, so no ghosts render and
  // the matrix is byte-identical to before the feature.
  ensureSugFeed(host, ctx, epochId);
  const entries = (matrix && Array.isArray(matrix.entries)) ? matrix.entries : [];
  const ghosts = ghostEntriesFrom(_sugFeed, entries, epochId);
  gatedSwap(host, digestOf(matrix, live, ghosts, epochLive),
    () => build(host, ctx, matrix, epochId, live, ghosts, epochLive));
  // The staleness sentinel for the async ghost-feed repaint (see ensureSugFeed):
  // whatever node this render mounted — disconnected means the operator left.
  _builtRoot = host.firstChild || null;
}

function build(host, ctx, matrix, epochId, live, ghosts, epochLive) {
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
  nodes.push(buildMatrix(ctx, epochId, candidates, entries, cells, live, ghosts, epochLive));

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

function buildMatrix(ctx, epochId, candidates, entries, cells, live, ghosts, epochLive) {
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

  // ── GHOST ROWS — the board being created (TRAJECTORY-UI.md §2.2b) ──────
  // Suggested board entries not yet on the board, appended below the real rows,
  // pending-styled + visually UNAMBIGUOUS (§4 honesty): a ghost row never reads
  // as a scored channel. Unaffected by the scored-row filters (they are drafts,
  // not measurements). When there are no ghosts, NOTHING is appended (the matrix
  // is byte-identical to before the feature).
  if (ghosts && ghosts.length) {
    const width = candidates.length + 1;
    const cap = el('tr', { class: 'dn-evalmtx-ghosthead' });
    cap.appendChild(el('th', {
      class: 'dn-evalmtx-ghostcaption dn-faint', colspan: String(width), scope: 'colgroup',
      text: 'proposed entries — drafts, not scored (default to train). Stage in the builder to seal.',
    }));
    tbody.appendChild(cap);
    for (const g of ghosts) tbody.appendChild(ghostRow(ctx, g, candidates.length));
  }
  table.appendChild(tbody);

  const wrap = el('div', { class: 'dn-evalmtx-wrap' });
  wrap.appendChild(M.matrixScroll(table));
  if (!shown) {
    wrap.appendChild(el('p', { class: 'dn-empty', text: 'No entries match the active filters.' }));
  }
  wrap.appendChild(el('p', { class: 'dn-faint dn-evalmtx-legend', text: 'row = board entry · column = candidate · ' + CROWN.current + ' = champion spine · faint cell = single-sample (unreplicated) · click a cell for its transcript' }));
  return section('Matrix', wrap);
}

// One GHOST ROW for a suggested (not-yet-on-the-board) entry. Pending-styled +
// dashed/faint so it never reads as measured tournament data: the entry id + a
// "suggested" marker + the flip-rate whisker in the row header (where the flip
// badge sits), then a single spanning cell carrying the discrimination pips +
// evidence tier (where cells would be — nothing ran it, so there is NO verdict)
// + the apply affordance. No scored glyphs, no drift numbers.
function ghostRow(ctx, g, candCount) {
  const viz = g.viz || vizFromFeedAdmission(null);
  const tr = M.matrixRow({
    extra: 'dn-evalmtx-row dn-evalmtx-ghost',
    attrs: { 'data-ghost': '1', 'data-entry': String(g.entry_id) },
  });
  // the row header — the entry id + the suggested marker + the flip whisker.
  const head = el('div', { class: 'dn-evalmtx-sitehead dn-evalmtx-ghosthead-cell' }, [
    el('span', { class: 'dn-mtx-file dn-evalmtx-entry', text: g.entry_id }),
    el('span', {
      class: 'dn-evalmtx-ghost-tag', title: 'suggested entry — a draft, not yet on the board',
      text: 'suggested',
    }),
    el('span', { class: 'dn-evalmtx-ghost-flip', title: 'A/A flip rate the instrument would measure (admission)' },
      [flipWhisker(viz.flip)]),
  ]);
  tr.appendChild(M.matrixRowHeader({
    extra: 'dn-evalmtx-site dn-evalmtx-ghost-site',
    attrs: { 'data-entry': String(g.entry_id) },
  }, [head]));
  // one spanning cell — the admission evidence WHERE CELLS WOULD BE, never a
  // fabricated verdict (nothing scored this draft).
  const probed = viz.evidence_tier === 'probed';
  const body = el('div', { class: 'dn-evalmtx-ghostbody' }, [
    el('span', { class: 'dn-faint dn-evalmtx-ghost-note', text: 'proposed — not yet on the board' }),
    el('span', { class: 'dn-evalmtx-ghost-marks' }, [
      el('span', { class: 'dn-faint dn-evalmtx-ghost-lab', text: 'sep' }),
      discriminationPips(viz.discrimination),
      el('span', {
        class: 'dn-evalmtx-ghost-tier' + (probed ? '' : ' dn-faint'),
        title: probed ? 'probed — an admission probe was spent' : 'planned — no probe spent (unmeasured)',
        text: probed ? 'probed' : 'planned',
      }),
    ]),
  ]);
  if (ctx && typeof ctx.href === 'function') {
    body.appendChild(el('a', {
      class: 'dn-evalmtx-ghost-apply dn-mono',
      href: ctx.href('builder', {}),
      title: 'stage this suggested entry to a builder draft you seal',
      'aria-label': 'stage suggested entry ' + g.entry_id + ' in the builder',
      text: 'stage in builder →',
    }));
  }
  tr.appendChild(M.matrixCell(false, {
    extra: 'dn-evalmtx-cell dn-evalmtx-ghostcell',
    attrs: { colspan: String(Math.max(1, candCount)) },
  }, [body]));
  return tr;
}

// A super-header row grouping consecutive columns that share a round_index into
// one spanning cell ("round 0" / "round 1"). Returns null when no column
// carries a round_index (nothing to group — a flat gauntlet epoch).
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

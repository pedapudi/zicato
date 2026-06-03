// variants/T/views/board.js — PER-BOARD cross-candidate view + INLINE
// side-by-side transcript (fixes #4, #5, #7).
//
// One board ENTRY, every candidate. Trellis cards and heatmap cells route HERE
// (keyed by entry id) — never to an arbitrary candidate. The view shows:
//   * per-candidate drift loss + pass / fail / timeout, as a sorted comparative
//     dot-plot (champion reference rule) and a tabular breakdown;
//   * fix #5 — selecting a run shows its transcript INLINE within THIS view,
//     side by side with the CHAMPION's transcript on the same board (two
//     candidates' transcripts on that board), NOT a navigation to a separate
//     run page. The selected gen lives in the URL (#/e/<e>/board/<entry>/<gen>)
//     so the inline transcript rebuilds only on a route change, never a beat.
//
// Bind: /api/generation/{e}/{g}/per-entry pivoted by entry_id across gens;
//       /api/conversation/{run_id} per candidate (the two inline transcripts).

import { el, clearChildren } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, normaliseDecision, decisionFor, densityTokens } from '../ui.js';

// In-flight board-units for THIS entry, read from /api/active-runs (folded
// into AppState by /api/environment). Each carries a generation_id / run_id /
// progress; some payloads key the board unit as `entry_id`, others as
// `board_entry_id` / `entry`. Filter to the one entry the board page is on.
export function inflightForEntry(activeRuns, entryId) {
  const runs = Array.isArray(activeRuns) ? activeRuns : [];
  if (!entryId) return [];
  return runs.filter((r) => {
    if (!r || typeof r !== 'object') return false;
    const e = r.entry_id != null ? r.entry_id : (r.board_entry_id != null ? r.board_entry_id : r.entry);
    return e === entryId;
  });
}

// 0..1 progress (some payloads send 0..100 — clamp + normalise).
function progressRatio(r) {
  let p = r && (r.progress != null ? r.progress : r.fraction);
  if (!svg.isNum(p)) {
    if (r && svg.isNum(r.elapsed_seconds) && svg.isNum(r.budget_seconds) && r.budget_seconds > 0) {
      p = r.elapsed_seconds / r.budget_seconds;
    } else return null;
  }
  if (p > 1) p = p / 100;
  if (p < 0) p = 0;
  if (p > 1) p = 1;
  return p;
}

const KIND_LABEL = {
  single_turn: 'single-turn', multi_turn_scripted: 'scripted multi-turn',
  multi_turn_emulated: 'emulated multi-turn',
};

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading board entry…' }));
  const entryId = params && params.entry;
  const selGen = (params && params.gen) || null;
  const routeEpoch = (params && params.epochId) || null;

  // Class A: scope every read to the viewed epoch (route param first).
  const ep = await D.epoch(routeEpoch);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Board entry' }), empty('No current epoch.')]);
    return;
  }
  if (!entryId) {
    gatedSwap(host, 'no-entry', () => [el('h1', { class: 'dn-h1', text: 'Board entry' }),
      empty('No board entry selected — open one from the Boards trellis or the epoch heatmap.')]);
    return;
  }
  const epochId = routeEpoch || ep.epoch_id;
  const board = Array.isArray(ep.board) ? ep.board : [];
  const def = board.find((b) => (b.entry_id || b.id) === entryId) || null;

  const [rows0, traj] = await Promise.all([D.generationsForEpoch(epochId), D.scoreTrajectory(epochId)]);
  const genList = rows0.length
    ? rows0.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: g.promoted == null ? null : !!g.promoted }))
    : (Array.isArray(ep.experiments) ? ep.experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' })) : []);

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  // In-flight runs currently executing on THIS board entry (live, any
  // structure). Folded in BEFORE the per-entry pivot so a RUNNING candidate is
  // a first-class, SELECTABLE row even though it has no loss.json yet — the
  // active-runs feed carries its run_id / generation_id directly, so its live
  // transcript resolves by the (epoch, gen, entry) triple (the events.jsonl is
  // already growing on disk). Keyed by gen so a row can be matched to its run.
  const inflight = inflightForEntry(state.activeRuns, entryId);
  const runningByGen = new Map();
  for (const r of inflight) {
    const g = r.generation_id || r.gen;
    if (g != null && !runningByGen.has(g)) runningByGen.set(g, r);
  }

  // While a candidate is RUNNING on the board the operator is watching, its
  // transcript must re-read the still-growing events.jsonl on every beat —
  // invalidateLive() only fires on a VIEW change, so bust just these live
  // transcript cache keys here. The transcript host stays gated on CONTENT, so
  // a re-read with no new turn is still a no-op repaint (scroll preserved).
  for (const r of inflight) {
    const g = r.generation_id || r.gen;
    if (g != null) D.invalidateRunTranscript(epochId, g, entryId, r.run_id || null);
  }

  // Pivot per-entry across every generation for THIS entry.
  const perEntries = await Promise.all(genList.map((g) => D.perEntry(epochId, g.id)));
  const rows = [];
  const seenGens = new Set();
  genList.forEach((g, i) => {
    const pe = perEntries[i];
    const r = (pe && Array.isArray(pe.entries)) ? pe.entries.find((e) => e.entry_id === entryId) : null;
    const live = runningByGen.get(g.id) || null;
    seenGens.add(g.id);
    rows.push({
      gen: g.id, promoted: g.promoted, parent: g.parent,
      loss: r && svg.isNum(r.drift_loss) ? r.drift_loss : NaN,
      pass: r ? r.pass_fail : null,
      timeout: r ? !!r.wall_clock_budget_exceeded : false,
      // Prefer the per-entry run_id (the scored record); fall back to the live
      // active-run's run_id so a RUNNING candidate's transcript resolves.
      runId: (r && r.run_id) || (live && live.run_id) || null,
      ran: !!r,
      running: !!live,
    });
  });
  // A candidate RUNNING on this entry with NO completed row yet (no per-entry
  // record in any generation's pivot) is still a real candidate — synthesise a
  // selectable row from the active-run so the operator can read its live
  // transcript. Lineage promoted/parent come from genList when known.
  for (const [g, live] of runningByGen) {
    if (seenGens.has(g)) continue;
    const meta = genList.find((x) => x.id === g) || null;
    rows.push({
      gen: g, promoted: meta ? meta.promoted : null, parent: meta ? meta.parent : null,
      loss: NaN, pass: null, timeout: false,
      runId: live.run_id || null, ran: false, running: true,
    });
  }

  const champ = genList.find((g) => g.promoted) || genList.find((g) => !g.parent) || null;
  const championId = champ ? champ.id : null;
  const champRow = rows.find((r) => r.gen === championId);
  const champLoss = champRow && svg.isNum(champRow.loss) ? champRow.loss : null;

  // fix #5 — resolve the two transcripts to show side by side: the SELECTED
  // candidate and the CHAMPION on the same board. If the selection IS the
  // champion, the right side falls back to the worst challenger for contrast.
  let leftSel = null, rightSel = null, leftConv = null, rightConv = null;
  if (selGen) {
    leftSel = rows.find((r) => r.gen === selGen) || null;
    let rightGen = championId && championId !== selGen ? championId : null;
    if (!rightGen) {
      const others = rows.filter((r) => r.gen !== selGen && svg.isNum(r.loss)).sort((a, b) => b.loss - a.loss);
      rightGen = others.length ? others[0].gen : null;
    }
    rightSel = rightGen ? rows.find((r) => r.gen === rightGen) || null : null;
    const convs = await Promise.all([
      resolveTranscript(epochId, entryId, leftSel),
      resolveTranscript(epochId, entryId, rightSel),
    ]);
    leftConv = convs[0]; rightConv = convs[1];
  }

  // `inflight` was read above (folded into `rows` so a running candidate is a
  // selectable row). The completed-results rendering below covers finished
  // runs; an entry mid-run with no completed results yet reads "N running",
  // never blank — the in-flight set folds into both the digest and the view.
  const ranCount = rows.filter((r) => r.ran).length;

  // Two SEPARATE digests, two SEPARATE persistent sub-hosts (the live-beat
  // scroll-reset fix). The OUTER (upper) digest folds in everything that should
  // repaint live — the in-flight set + its advancing progressRatio, the
  // dot-plot, the breakdown table. The TRANSCRIPT digest folds in ONLY the
  // selected candidates and their transcript content. Because the in-flight
  // fields are EXCLUDED from the transcript digest, a beat that only advances
  // in-flight progress repaints the upper host but leaves the transcript host
  // (and its scroll position) untouched — mirrors compare.js's per-side hosts.
  const upperDigest = JSON.stringify({
    epochId, entryId, selGen,
    def: def ? [def.kind, def.weight, def.budget_s] : null,
    champ: championId,
    rows: rows.map((r) => [r.gen, svg.isNum(r.loss) ? r.loss.toFixed(3) : null, r.pass, r.timeout, r.promoted, r.runId, !!r.running]),
    inflight: inflight.map((r) => {
      const pr = progressRatio(r);
      return [r.generation_id || r.gen || null, r.run_id || null, pr != null ? pr.toFixed(2) : null];
    }),
  });
  // The transcript digest folds in ONLY the selected candidates and their
  // transcript CONTENT (transcriptDigest = per-turn seq/role/text-length/tool
  // count) plus the live flag — NEVER the in-flight progress %. So a beat that
  // merely advances progress changes upperDigest but leaves xscriptDigest
  // untouched (no transcript repaint, scroll preserved); a beat where the live
  // transcript actually GAINS a turn changes the content signal and repaints.
  const xscriptDigest = JSON.stringify({
    epochId, entryId, selGen, champ: championId,
    left: leftSel ? [leftSel.gen, !!leftSel.running, transcriptDigest(leftConv)] : null,
    right: rightSel ? [rightSel.gen, !!rightSel.running, transcriptDigest(rightConv)] : null,
  });

  // Persistent sub-hosts: created ONCE under `host`, reused every render so the
  // transcript host survives an upper-only (in-flight) repaint. An earlier
  // error/empty route may have written `host` directly via gatedSwap; if our
  // persistent scaffold is absent (or was torn down), (re)build it.
  let upperHost = host.querySelector(':scope > [data-node="board-upper"]');
  let xscriptHost = host.querySelector(':scope > [data-node="board-xscript"]');
  if (!upperHost || !xscriptHost) {
    clearChildren(host);
    upperHost = el('div', { 'data-node': 'board-upper' });
    xscriptHost = el('div', { 'data-node': 'board-xscript' });
    host.appendChild(upperHost);
    host.appendChild(xscriptHost);
    host.removeAttribute('data-t-digest');
  }

  gatedSwap(upperHost, upperDigest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: 'Board · ' + entryId }),
      el('p', { class: 'dn-lede', text: 'How every candidate performed on this one board entry — lower drift loss is better. Select a candidate to read its transcript inline, side by side with the champion’s.' }),
    ]));

    nodes.push(el('div', { class: 'dn-panel dn-row' }, [
      stat(def ? (KIND_LABEL[def.kind] || def.kind || '—') : '—', 'kind'),
      stat(def && svg.isNum(def.weight) ? svg.fmt(def.weight, 1) : '—', 'weight'),
      stat(def && svg.isNum(def.budget_s) ? def.budget_s + 's' : '—', 'budget'),
      stat(String(rows.filter((r) => r.ran).length) + '/' + String(rows.length), 'candidates ran'),
    ]));
    if (def && def.input_preview) {
      nodes.push(el('div', { class: 'dn-panel' }, [
        el('div', { class: 'dn-faint', style: 'font-size:10px;text-transform:uppercase;letter-spacing:0.06em;', text: 'input preview' }),
        el('div', { style: 'margin-top:4px;line-height:1.4;', text: '“' + def.input_preview + '”' }),
      ]));
    }

    // LIVE — candidates currently executing on THIS board entry. Rendered for
    // ANY tournament structure (the active-runs feed is structure-agnostic). A
    // board mid-run with no completed results yet now reads "N candidates
    // running" with each candidate's progress, rather than appearing empty.
    if (inflight.length) {
      const liveCard = el('div', { class: 'dn-panel dn-board-inflight' });
      liveCard.appendChild(el('div', { class: 'dn-inflight-head' }, [
        el('span', { class: 'dn-inflight-pulse', 'aria-hidden': 'true' }),
        el('span', { class: 'dn-inflight-count', text: String(inflight.length) + (inflight.length === 1 ? ' candidate running' : ' candidates running') }),
        el('span', { class: 'dn-faint', text: ' on this board entry' }),
      ]));
      const tbl = el('table', { class: 'dn-board-table dn-inflight-table' });
      tbl.appendChild(el('thead', null, [el('tr', null, [
        el('th', { text: 'candidate' }), el('th', { text: 'run' }), el('th', { text: 'progress' }),
      ])]));
      const tbody = el('tbody');
      for (const r of inflight) {
        const gen = r.generation_id || r.gen || '—';
        const pr = progressRatio(r);
        const pct = pr != null ? Math.round(pr * 100) : null;
        tbody.appendChild(el('tr', { class: 'dn-inflight-row' }, [
          el('td', { class: 'dn-mono', text: String(gen) }),
          el('td', { class: 'dn-mono dn-faint', text: r.run_id ? String(r.run_id) : 'pending' }),
          el('td', null, [
            el('span', { class: 'dn-progress' }, [
              el('span', { class: 'dn-progress-fill', style: 'width:' + (pct != null ? pct : 6) + '%' + (pct == null ? ';opacity:0.4' : '') }),
            ]),
            el('span', { class: 'dn-mono dn-faint dn-progress-pct', text: pct != null ? ' ' + pct + '%' : ' running…' }),
          ]),
        ]));
      }
      tbl.appendChild(tbody);
      liveCard.appendChild(tbl);
      nodes.push(section('Live · candidates running on this board entry', liveCard));
    } else if (!ranCount) {
      // No completed run AND nothing in flight — honest empty (still not blank).
      nodes.push(section('Status', el('div', { class: 'dn-panel' }, [
        empty('No candidate has run on this board entry yet.'),
      ])));
    }

    // sorted comparative dot-plot, worst-first, champion reference rule.
    const scoreCard = el('div', { class: 'dn-panel' });
    const items = rows
      .filter((r) => svg.isNum(r.loss))
      .sort((a, b) => b.loss - a.loss)
      .map((r) => ({ label: r.gen + (r.promoted ? ' ♛' : ''), value: r.loss, id: r.gen, pass: r.pass, timeout: r.timeout }));
    if (items.length) {
      const bdt = densityTokens();
      scoreCard.appendChild(svg.valueDotPlot({
        width: 560, rowHeight: bdt.dotRow, labelWidth: 140, items,
        reference: champLoss != null ? { value: champLoss, label: `champion ${championId}` } : null,
        // fix #5: select → INLINE transcript on THIS view (same entry, +gen).
        // TOGGLE: clicking the already-selected candidate's dot collapses it
        // (drop the gen) — kept consistent with the breakdown-row button.
        onClick: (it) => ctx.navigate('board', it.id === selGen ? { epochId, entry: entryId } : { epochId, entry: entryId, gen: it.id }),
      }));
      scoreCard.appendChild(el('div', { class: 'dn-legend' }, [
        champLoss != null ? el('span', null, [el('i', { class: 'spine', style: 'border-color:var(--v2-ink-faint);border-top-style:dashed;' }), `champion ${championId} = ${svg.fmt(champLoss, 1)}`]) : null,
        el('span', null, [el('i', { class: 'dotact' }), 'pass']),
        el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'fail']),
        el('span', { class: 'dn-faint', text: '⏱ timeout · click a candidate → its transcript inline (vs champion)' }),
      ].filter(Boolean)));
    } else {
      scoreCard.appendChild(empty('No candidate has a scored run for this entry yet.'));
    }
    nodes.push(section('Per-candidate loss · sorted worst-first, vs champion', scoreCard));

    // tabular breakdown — rows select the inline transcript (no route away).
    const tblCard = el('div', { class: 'dn-panel' });
    const tbl = el('table', { class: 'dn-board-table' });
    tbl.appendChild(el('thead', null, [el('tr', null, [
      el('th', { text: 'candidate' }), el('th', { class: 'dn-num', text: 'drift loss' }),
      el('th', { text: 'predicate' }), el('th', { text: 'budget' }), el('th', { text: 'transcript' }),
    ])]));
    const tbody = el('tbody');
    for (const r of rows.slice().sort((a, b) => (svg.isNum(b.loss) ? b.loss : -1) - (svg.isNum(a.loss) ? a.loss : -1))) {
      const isSel = r.gen === selGen;
      // A RUNNING candidate is selectable too: its events.jsonl is already
      // growing on disk, so its (epoch, gen, entry) transcript resolves live.
      const selectable = r.ran || r.running;
      tbody.appendChild(el('tr', { class: (r.promoted ? 'dn-board-champ' : '') + (isSel ? ' dn-board-sel' : '') + (r.running ? ' dn-board-running' : '') }, [
        el('td', { class: 'dn-mono', text: r.gen + (r.promoted ? ' ♛' : '') }),
        el('td', { class: 'dn-num dn-mono', text: svg.isNum(r.loss) ? svg.fmt(r.loss, 1) : (r.running ? 'running' : '—') }),
        el('td', { class: passClass(r.pass), text: r.running && !r.ran ? 'live' : passLabel(r.pass) }),
        el('td', { class: 'dn-mono', text: r.timeout ? 'timed out' : 'ok' }),
        el('td', null, [selectable
          // TOGGLE: an already-selected candidate's button collapses its inline
          // transcript — its href drops the gen (back to the bare board route),
          // so clicking "showing ↓" closes it and a reload won't reopen it. A
          // RUNNING candidate reads "watch live →" so the operator knows the
          // transcript will stream as new turns land.
          ? el('a', { class: 'dn-linkbtn dn-board-run' + (isSel ? ' dn-linkbtn-on' : '') + (r.running ? ' dn-board-run-live' : ''), href: ctx.href('board', isSel ? { epochId, entry: entryId } : { epochId, entry: entryId, gen: r.gen }), text: isSel ? 'showing ↓' : (r.running ? 'watch live →' : 'show inline →') })
          : el('span', { class: 'dn-faint', text: 'no run' })]),
      ]));
    }
    tbl.appendChild(tbody);
    tblCard.appendChild(tbl);
    nodes.push(section('Breakdown · select a candidate to read its transcript inline', tblCard));
    return nodes;
  });

  // fix #5 — the INLINE side-by-side transcript pane, gated on its OWN digest in
  // its OWN persistent host. A beat that only advanced in-flight progress leaves
  // upperDigest changed but xscriptDigest unchanged, so this gatedSwap is a true
  // no-op: the transcript scroll containers are NOT recreated and the scroll
  // position is preserved. The pane re-renders only when selGen / the resolved
  // candidates / their transcript content actually change.
  //
  // LIVE GROWTH: when the SELECTED candidate is RUNNING, its transcript grows
  // turn-by-turn — transcriptDigest folds turn count + text length into
  // xscriptDigest, so this gatedSwap fires only when the transcript actually
  // GROWS (never on a progress-only beat). Because that growth rebuilds the
  // scroll containers, capture each column's scroll position BEFORE the swap and
  // restore it AFTER: a user who scrolled up stays put, and one already pinned to
  // the bottom is kept pinned so new turns stay in view (live-tail behaviour).
  const scrollState = captureScroll(xscriptHost);
  const repainted = gatedSwap(xscriptHost, xscriptDigest, () => {
    if (!selGen) return [];
    return [section(
      `Transcripts · ${leftSel ? leftSel.gen : selGen} vs ${rightSel ? rightSel.gen : '—'} on ${entryId}`,
      sideBySideTranscripts(leftSel, leftConv, rightSel, rightConv, championId),
    )];
  });
  if (repainted) restoreScroll(xscriptHost, scrollState);
}

// Per-column scroll containers, keyed by their stable side (left / right) via
// the `data-node` tag — so capture + restore land on the SAME column across a
// content-growth rebuild even if one side is absent.
const XSCRIPT_SIDES = ['left', 'right'];
function xscriptScroller(host, side) {
  if (!host) return null;
  const found = host.querySelectorAll('[data-node="xscript-scroll-' + side + '"]');
  return (found && found[0]) || null;
}

// Capture each column's scroll position so a content-growth repaint does not
// yank the operator around. `atBottom` records whether the column was pinned to
// the bottom (within a small slop) so a live-tail column keeps following new
// turns. Guarded for the test DOM (no layout → numeric scroll props absent).
function captureScroll(host) {
  const out = {};
  for (const side of XSCRIPT_SIDES) {
    const s = xscriptScroller(host, side);
    if (!s || typeof s.scrollTop !== 'number') continue;
    const slop = 4;
    const top = s.scrollTop;
    const atBottom = typeof s.scrollHeight === 'number' && typeof s.clientHeight === 'number'
      ? (s.scrollHeight - top - s.clientHeight) <= slop
      : false;
    out[side] = { scrollTop: top, atBottom };
  }
  return out;
}

function restoreScroll(host, state) {
  if (!state) return;
  for (const side of XSCRIPT_SIDES) {
    const prev = state[side];
    if (!prev) continue;
    const s = xscriptScroller(host, side);
    if (!s || typeof s.scrollTop !== 'number') continue;
    // Pinned-to-bottom column follows the new turns (live tail); otherwise hold
    // the exact prior offset so the reader is not pulled away from what they
    // were reading.
    s.scrollTop = prev.atBottom && typeof s.scrollHeight === 'number' ? s.scrollHeight : prev.scrollTop;
  }
}

// Two transcripts on the same board, side by side, in ONE constrained pane —
// the heart of fix #5. Each column is a constrained-scroll turn list; no
// absolute positioning, no route change.
function sideBySideTranscripts(leftSel, leftConv, rightSel, rightConv, championId) {
  const card = el('div', { class: 'dn-panel dn-xscript' });
  const grid = el('div', { class: 'dn-xscript-grid' }, [
    transcriptColumn(leftSel, leftConv, championId, 'left'),
    transcriptColumn(rightSel, rightConv, championId, 'right'),
  ]);
  card.appendChild(grid);
  card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'two candidates’ transcripts on this board, side by side — rendered inline, no navigation away' }));
  return card;
}

function transcriptColumn(sel, conv, championId, side) {
  const col = el('div', { class: 'dn-xscript-col' });
  if (!sel) {
    col.appendChild(el('div', { class: 'dn-xscript-head dn-faint', text: 'no second candidate to compare' }));
    return col;
  }
  const role = sel.gen === championId ? 'champion' : (sel.parent ? 'challenger' : 'seed');
  // Class B: the pill's COLOUR follows the candidate's decision, so an unscored
  // challenger reads pending (neutral), never rejected.
  const pillCls = decisionFor({ promoted: sel.promoted, parent: sel.parent });
  col.appendChild(el('div', { class: 'dn-xscript-head' }, [
    el('span', { class: 'dn-mono', text: sel.gen + (sel.promoted ? ' ♛' : '') }),
    el('span', { class: 'dn-pill dn-' + pillCls, text: role }),
    // A RUNNING candidate gets a live marker so the operator reads the column
    // as a streaming transcript (it repaints as new turns land), not a final one.
    sel.running ? el('span', { class: 'dn-pill dn-live dn-xscript-live' }, [
      el('span', { class: 'dn-inflight-pulse', 'aria-hidden': 'true' }),
      el('span', { text: 'live' }),
    ]) : null,
    el('span', { class: 'dn-faint dn-mono', text: svg.isNum(sel.loss) ? ' · loss ' + svg.fmt(sel.loss, 1) : '' }),
  ].filter(Boolean)));

  const turns = (conv && Array.isArray(conv.turns)) ? conv.turns : [];
  // The transcript is keyed on the (epoch, gen, entry) triple, NOT the
  // per-entry run_id — so a candidate whose row carries no run_id can still
  // render its transcript when the gen×entry events.jsonl exists. Only fall
  // through to the honest messages when the triple genuinely resolves to
  // nothing.
  if (conv && conv.error) { col.appendChild(empty(conv.error)); return col; }
  if (!conv) {
    // A RUNNING candidate that has not emitted its first turn yet is waiting,
    // not unavailable — the events.jsonl will grow and the next beat repaints.
    col.appendChild(empty(sel.running ? 'Waiting for the first turn… (this transcript streams as the run produces turns)' : 'Transcript unavailable (could not be reconstructed).'));
    return col;
  }
  if (!turns.length) {
    col.appendChild(empty(sel.running ? 'Waiting for the first turn… (this transcript streams as the run produces turns)' : 'No turns reconstructed for this run.'));
    return col;
  }

  const anns = (conv && Array.isArray(conv.annotations)) ? conv.annotations : [];
  const annBySeq = new Map();
  for (const a of anns) {
    const k = a.anchor_seq;
    if (!annBySeq.has(k)) annBySeq.set(k, []);
    annBySeq.get(k).push(a);
  }
  // `data-node` tags the scroller with its stable side so a content-growth
  // repaint can capture + restore each column's scroll position by side.
  const scroller = el('div', { class: 'dn-transcript dn-xscript-scroll', 'data-node': 'xscript-scroll-' + (side || 'left') });
  for (const t of turns) {
    const turn = el('div', { class: 'dn-turn dn-turn-' + (t.role || 'agent') }, [
      el('div', { class: 'dn-turn-head dn-faint dn-mono' }, [
        el('span', { text: t.agent || t.role || 'turn' }),
        t.kind ? el('span', { text: ' · ' + t.kind }) : null,
      ].filter(Boolean)),
      t.text ? el('div', { class: 'dn-turn-text', text: t.text }) : null,
    ].filter(Boolean));
    if (Array.isArray(t.tool_calls)) for (const tc of t.tool_calls) {
      turn.appendChild(el('div', { class: 'dn-tool dn-mono', text: '⚙ ' + (tc.name || tc.tool || 'tool') }));
    }
    for (const a of (annBySeq.get(t.seq) || [])) {
      turn.appendChild(el('div', { class: 'dn-annot dn-annot-' + (a.kind || 'note'), text: '◂ ' + (a.summary || a.kind) }));
    }
    scroller.appendChild(turn);
  }
  col.appendChild(scroller);
  return col;
}

// Resolve one side's transcript by the DETERMINISTIC (epoch, gen, entry)
// triple FIRST — the pane already knows all three. The events file lives at
// generations/<gen>/runs/<entry>/events.jsonl, so the gen×entry transcript
// always resolves to the one real events.jsonl on disk regardless of how the
// per-entry row's run_id was minted. This inverts the old run_id-FIRST order,
// which kept failing whenever the run_id was a successive-halving reuse
// record, an index-only id, or one of several records for a re-raced pair.
//
// The candidate's run_id is only a DISAMBIGUATOR: when a gen×entry has been
// re-raced across rungs (multiple runs under one entry), it picks the
// specific rung. We pass it along so the backend can select that rung, then
// fall back to the run_id-keyed /api/conversation ONLY when the triple
// genuinely resolves to nothing (e.g. a pre-feature workspace). A
// genuinely-absent gen×entry stays empty → the honest "unavailable" message.
async function resolveTranscript(epochId, entryId, sel) {
  if (!sel || !sel.gen) return null;
  let conv = await D.runTranscript(epochId, sel.gen, entryId, sel.runId);
  if (!hasTurns(conv) && sel.runId) {
    const byRun = await D.conversation(sel.runId, sel.gen, entryId);
    if (hasTurns(byRun)) conv = byRun;
    else if (conv == null) conv = byRun;
  }
  return conv;
}

function hasTurns(conv) {
  return !!(conv && Array.isArray(conv.turns) && conv.turns.length);
}

function transcriptDigest(conv) {
  const turns = (conv && Array.isArray(conv.turns)) ? conv.turns : [];
  if (conv && conv.error) return 'err:' + conv.error;
  if (!conv) return 'none';
  return turns.map((t) => [t.seq, t.role, (t.text || '').length, Array.isArray(t.tool_calls) ? t.tool_calls.length : 0]).join(';');
}

function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}
function passClass(pf) {
  if (pf === 1 || pf === true) return 'dn-good-t';
  if (pf === 0 || pf === false) return 'dn-bad-t';
  return 'dn-faint';
}

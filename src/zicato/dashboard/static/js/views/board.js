// js/views/board.js — PER-BOARD cross-candidate view + INLINE
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

import { el, clearChildren } from '../core/dom.js';
import { state } from '../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, decisionFor, densityTokens, prText, metricsDigest, scoreFmt, pill, dataTable, deltaCell } from '../ui.js';
import { splitFrame, captureScroll, restoreScroll } from '../compare.js';

// In-flight board-units for THIS entry, read from /api/active-runs (folded
// into AppState by /api/environment). Each carries a generation_id / run_id /
// progress; some payloads key the board unit as `entry_id`, others as
// the canonical `entry_id`. Filter to the one entry the board page is on.
export function inflightForEntry(activeRuns, entryId) {
  const runs = Array.isArray(activeRuns) ? activeRuns : [];
  if (!entryId) return [];
  return runs.filter((r) => {
    if (!r || typeof r !== 'object') return false;
    const e = r.entry_id;
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

// The orchestrator progress-seq at our LAST render — the seam that gates the
// live transcript refetch on a genuine seq ADVANCE (never a no-op heartbeat).
// Module-scoped: the board view is a singleton render target. `null` forces the
// first render to bust (adopt the current seq); state.lastSeq === -1 (no seq
// seen — a pre-RUNTIME-V2 server) degrades to the legacy always-bust path.
let _lastBoardSeq = null;

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
    : (Array.isArray(ep.experiments) ? ep.experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: x.promoted === true })) : []);

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
    const g = r.generation_id;
    if (g != null && !runningByGen.has(g)) runningByGen.set(g, r);
  }

  // While a candidate is RUNNING on the board the operator is watching, its
  // transcript must re-read the still-growing events.jsonl — but ONLY on a
  // genuine progress-seq ADVANCE, never a no-op heartbeat. state.lastSeq is the
  // orchestrator's liveness cursor (advances only on a real transition); we bust
  // the live transcript cache keys just when it moved since our last render, so a
  // burst of heartbeats at a stable seq issues ZERO refetches. A pre-RUNTIME-V2
  // server that never sends a seq (lastSeq === -1) DEGRADES to the always-bust
  // path. invalidateLive() only fires on a VIEW change, so this is the sole
  // live-transcript busting seam; the transcript host stays gated on CONTENT, so
  // even a forced re-read with no new turn is a no-op repaint (scroll preserved).
  const seqAdvanced = state.lastSeq < 0 || state.lastSeq !== _lastBoardSeq;
  _lastBoardSeq = state.lastSeq;
  if (seqAdvanced) {
    for (const r of inflight) {
      const g = r.generation_id;
      if (g != null) D.invalidateRunTranscript(epochId, g, entryId, r.run_id || null);
    }
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
      // continuous per-entry outcome + its precision/recall decomposition (#18);
      // null/absent on a bool-only run — the row then shows just pass/fail.
      score: r && svg.isNum(r.score) ? r.score : null,
      metrics: (r && r.metrics) || null,
      timeout: r ? !!r.wall_clock_budget_exceeded : false,
      // Prefer the per-entry run_id (the scored record); fall back to the live
      // active-run's run_id so a RUNNING candidate's transcript resolves.
      runId: (r && r.run_id) || (live && live.run_id) || null,
      ran: !!r,
      running: !!live,
      // live 0..1 board progress for a RUNNING row (null when settled) — the
      // breakdown's live-gated progress column reads this (C4: the separate
      // in-flight table folded into the breakdown as a progress column).
      progress: live ? progressRatio(live) : null,
      // CACHED-champion provenance: this row's scalar was reused (fast mode)
      // from a prior epoch/run, not re-executed this round.
      cached: !!(r && r.cached),
      sourceEpoch: (r && r.source_epoch) || null,
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
      progress: progressRatio(live),
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
    // rows fold the continuous score + its precision/recall metrics (#18) so a
    // scored board repaints when a score moves; a bool-only row contributes
    // null for both (back-compat: digest unchanged vs the pre-score path).
    rows: rows.map((r) => [r.gen, svg.isNum(r.loss) ? r.loss.toFixed(3) : null, r.pass, r.timeout, r.promoted, r.runId, !!r.running, !!r.cached, r.sourceEpoch || null, svg.isNum(r.score) ? r.score.toFixed(3) : null, metricsDigest(r.metrics)]),
    inflight: inflight.map((r) => {
      const pr = progressRatio(r);
      return [r.generation_id || null, r.run_id || null, pr != null ? pr.toFixed(2) : null];
    }),
  });
  // The transcript STRUCTURE digest gates the frame (headers, columns, scroller
  // shells, the streaming caption) — deliberately EXCLUDING the growing turn
  // content. It folds only what changes the frame's SHAPE: the selected
  // candidates, their live flag + loss, and each column's coarse STATE
  // (nosel / err / wait / turns). Growing turns do NOT change this digest, so a
  // live beat that only APPENDS turns leaves the frame untouched and the turns
  // are reconciled (appended) in below — never a wholesale thread rebuild. The
  // frame rebuilds only on a genuine structural change: a new selection, the
  // empty→first-turn transition, or the run completing (running flips false, the
  // loss lands), which is where the final transcript cleanly replaces the partial.
  const xscriptDigest = JSON.stringify({
    epochId, entryId, selGen, champ: championId,
    left: leftSel ? [leftSel.gen, !!leftSel.running, leftSel.promoted, columnStateSig(leftSel, leftConv), svg.isNum(leftSel.loss) ? leftSel.loss.toFixed(1) : null] : null,
    right: rightSel ? [rightSel.gen, !!rightSel.running, rightSel.promoted, columnStateSig(rightSel, rightConv), svg.isNum(rightSel.loss) ? rightSel.loss.toFixed(1) : null] : null,
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

    // LIVE — candidates currently executing on THIS board entry surface as
    // RUNNING rows in the breakdown table below (folded into `rows`), with a
    // live-gated progress column (C4: the separate in-flight table was merged
    // into the breakdown — the same rows were shown twice). A live "N running"
    // banner rides the breakdown section title. Nothing ran AND nothing in
    // flight → an honest empty (never blank).
    if (!ranCount && !inflight.length) {
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
    // the continuous-score column (#18) only appears when AT LEAST ONE
    // candidate scored this board; a wholly bool-only board keeps the
    // pre-score column set so the table reads exactly as before.
    const anyScored = rows.some((r) => svg.isNum(r.score));
    // the live-gated progress column appears only while a candidate is running
    // on this entry (C4); a wholly settled board keeps the pre-C4 column set.
    const anyLive = inflight.length > 0;
    const tbl = dataTable({
      class: 'dn-board-table',
      columns: [
        { label: 'candidate' }, { label: 'drift loss', class: 'dn-num' }, { label: 'predicate' },
        anyLive ? { label: 'progress' } : null,
        anyScored ? { label: 'score', class: 'dn-num' } : null,
        anyScored ? { label: 'P / R' } : null,
        { label: 'budget' }, { label: 'transcript' },
      ],
      rows: rows.slice().sort((a, b) => (svg.isNum(b.loss) ? b.loss : -1) - (svg.isNum(a.loss) ? a.loss : -1)).map((r) => {
        const isSel = r.gen === selGen;
        // A RUNNING candidate is selectable too: its events.jsonl is already
        // growing on disk, so its (epoch, gen, entry) transcript resolves live.
        const selectable = r.ran || r.running;
        const pct = r.running && r.progress != null ? Math.round(r.progress * 100) : null;
        return {
          class: (r.promoted ? 'dn-board-champ' : '') + (isSel ? ' dn-board-sel' : '') + (r.running ? ' dn-board-running' : ''),
          cells: [
            { class: 'dn-mono', el: [
              el('span', { text: r.gen + (r.promoted ? ' ♛' : '') }),
              r.cached ? el('span', { class: 'dn-cached-badge-mark', title: r.sourceEpoch ? 'cached · from ' + r.sourceEpoch : 'cached champion result',
                text: r.sourceEpoch ? ' cached · ' + r.sourceEpoch : ' cached' }) : null,
            ] },
            { class: 'dn-num dn-mono', text: svg.isNum(r.loss) ? svg.fmt(r.loss, 1) : (r.running ? 'running' : '—') },
            { class: passClass(r.pass), text: r.running && !r.ran ? 'live' : passLabel(r.pass) },
            // live-gated progress column (C4): a running row shows its board
            // progress bar; a settled row leaves an em-dash. Absent entirely
            // when nothing is in flight (anyLive false).
            anyLive ? { class: 'dn-board-progress-cell dn-inflight-row', el: r.running
              ? [
                  el('span', { class: 'dn-progress' }, [
                    el('span', { class: 'dn-progress-fill', style: 'width:' + (pct != null ? pct : 6) + '%' + (pct == null ? ';opacity:0.4' : '') }),
                  ]),
                  el('span', { class: 'dn-mono dn-faint dn-progress-pct', text: pct != null ? ' ' + pct + '%' : ' running…' }),
                ]
              : [el('span', { class: 'dn-faint', text: '—' })] } : null,
            // continuous score + precision/recall (#18): only when this board
            // has a scored candidate. A bool-only row leaves these cells '—' /
            // '·' beside its pass/fail predicate above (which stays the verdict).
            anyScored ? { class: 'dn-num dn-mono dn-score-cell', text: svg.isNum(r.score) ? scoreFmt(r.score, 2) : '—' } : null,
            anyScored ? { class: 'dn-mono dn-faint dn-pr-cell', text: prText(r.metrics) || '·' } : null,
            { class: 'dn-mono', text: r.timeout ? 'timed out' : 'ok' },
            { el: selectable
              // TOGGLE: an already-selected candidate's button collapses its
              // inline transcript — its href drops the gen (back to the bare
              // board route), so clicking "showing ↓" closes it and a reload
              // won't reopen it. A RUNNING candidate reads "watch live →" so the
              // operator knows the transcript will stream as new turns land.
              ? el('a', { class: 'dn-linkbtn dn-board-run' + (isSel ? ' dn-linkbtn-on' : '') + (r.running ? ' dn-board-run-live' : ''), href: ctx.href('board', isSel ? { epochId, entry: entryId } : { epochId, entry: entryId, gen: r.gen }), text: isSel ? 'showing ↓' : (r.running ? 'watch live →' : 'show inline →') })
              : el('span', { class: 'dn-faint', text: 'no run' }) },
          ],
        };
      }),
    });
    tblCard.appendChild(tbl);
    nodes.push(section('Breakdown · select a candidate to read its transcript inline'
      + (inflight.length ? ` · ${inflight.length} running` : ''), tblCard));
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
  // Scroll-preservation across the live-growth repaint is compare.js's shared
  // mechanism (C5): the transcript columns tag their scrollers data-scroll-side,
  // captureScroll/restoreScroll wrap the gatedSwap so a scrolled-up reader stays
  // put and a bottom-pinned column keeps live-tailing new turns.
  const scrollState = captureScroll(xscriptHost);
  const rebuilt = gatedSwap(xscriptHost, xscriptDigest, () => {
    if (!selGen) return [];
    return [section(
      `Transcripts · ${leftSel ? leftSel.gen : selGen} vs ${rightSel ? rightSel.gen : '—'} on ${entryId}`,
      sideBySideTranscripts(leftSel, leftConv, rightSel, rightConv, championId),
    )];
  });
  // Reconcile the turn CONTENT into each column's persistent scroller AFTER the
  // frame settled: APPEND only newly-landed turns (never a thread rebuild) and
  // update the streaming caption. This runs every render — on a no-op beat it
  // finds no new turns and writes ZERO DOM (scroll preserved); on a live beat it
  // appends the tail and, if the reader was pinned to the bottom, keeps it there
  // (live-tail). After a structural rebuild the scrollers are fresh (no rendered
  // turns yet), so the same reconcile fills them in one pass — no double paint.
  reconcileTranscript(xscriptHost, 'left', leftSel, leftConv);
  reconcileTranscript(xscriptHost, 'right', rightSel, rightConv);
  // Only a STRUCTURAL rebuild needs the wholesale scroll restore (new scrollers);
  // pure-append reconciles preserve scroll themselves. Restore after both so a
  // selection-change rebuild keeps a bottom-pinned column tailing.
  if (rebuilt) restoreScroll(xscriptHost, scrollState);
}

// Two transcripts on the same board, side by side, in ONE constrained pane —
// the heart of fix #5. The two-column framing is compare.js's splitFrame (C5:
// the hand-rolled copy retired); each side paints a constrained-scroll turn
// list, no absolute positioning, no route change.
function sideBySideTranscripts(leftSel, leftConv, rightSel, rightConv, championId) {
  const card = el('div', { class: 'dn-panel dn-xscript' });
  card.appendChild(splitFrame({
    a: { build: (h) => h.appendChild(transcriptColumn(leftSel, leftConv, championId, 'left')) },
    b: { build: (h) => h.appendChild(transcriptColumn(rightSel, rightConv, championId, 'right')) },
  }));
  card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'two candidates’ transcripts on this board, side by side — rendered inline, no navigation away' }));
  return card;
}

// Build one column's FRAME only — the header, the streaming caption shell (for a
// running column), and an EMPTY scroller shell. The turn nodes are NOT built
// here: they are reconciled/appended into the scroller by reconcileTranscript so
// a later live beat appends the tail rather than rebuilding the thread. The
// column carries `data-xscript-col` so reconcileTranscript can locate its
// scroller + caption from the host.
function transcriptColumn(sel, conv, championId, side) {
  const col = el('div', { class: 'dn-xscript-col', 'data-xscript-col': side || 'left' });
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
    pill(pillCls, role),
    // A RUNNING candidate gets a live marker so the operator reads the column
    // as a streaming transcript (it appends as new turns land), not a final one.
    sel.running ? el('span', { class: 'dn-pill dn-live dn-xscript-live' }, [
      el('span', { class: 'dn-inflight-pulse', 'aria-hidden': 'true' }),
      el('span', { text: 'live' }),
    ]) : null,
    el('span', { class: 'dn-faint dn-mono', text: svg.isNum(sel.loss) ? ' · loss ' + svg.fmt(sel.loss, 1) : '' }),
  ].filter(Boolean)));

  // The transcript is keyed on the (epoch, gen, entry) triple, NOT the per-entry
  // run_id — so a candidate whose row carries no run_id can still render its
  // transcript when the gen×entry events.jsonl exists. Only fall through to the
  // honest messages when the triple genuinely resolves to nothing.
  const sig = columnStateSig(sel, conv);
  if (sig.startsWith('err')) { col.appendChild(empty(conv.error)); return col; }
  if (sig === 'wait') {
    // A RUNNING candidate that has not emitted its first turn yet is waiting, not
    // unavailable — the events.jsonl will grow and the next seq-advance repaints.
    col.appendChild(empty(sel.running
      ? 'Waiting for the first turn… (this transcript streams as the run produces turns)'
      : (conv ? 'No turns reconstructed for this run.' : 'Transcript unavailable (could not be reconstructed).')));
    return col;
  }
  // sig === 'turns'. A running column carries a subtle "streaming — through turn
  // N" caption (its count is filled by reconcileTranscript); it is part of the
  // running FRAME, so it disappears cleanly once the run completes (running flips
  // false → the frame rebuilds without it). Reuses the shipped live vocabulary
  // (the in-flight pulse + faint mono) — no new chrome.
  if (sel.running) {
    col.appendChild(el('div', { class: 'dn-xscript-stream dn-faint dn-mono', 'data-stream-caption': '' }, [
      el('span', { class: 'dn-inflight-pulse', 'aria-hidden': 'true' }),
      el('span', { 'data-stream-count': '', text: 'streaming…' }),
    ]));
  }
  // `data-scroll-side` tags the scroller with its stable side so compare.js's
  // captureScroll/restoreScroll (C5) can preserve each column's scroll position
  // across a structural rebuild; the scroller starts EMPTY and is filled by
  // reconcileTranscript.
  col.appendChild(el('div', { class: 'dn-transcript dn-xscript-scroll', 'data-scroll-side': side || 'left' }));
  return col;
}

// Coarse column STATE for the structure digest + the frame builder: the shape of
// the column (which message vs a scroller), NOT its growing turn content. So a
// beat that only appends turns leaves this stable and the frame is kept.
function columnStateSig(sel, conv) {
  if (!sel) return 'nosel';
  if (conv && conv.error) return 'err:' + conv.error;
  const turns = (conv && Array.isArray(conv.turns)) ? conv.turns : [];
  return turns.length ? 'turns' : 'wait';
}

// Reconcile one column's turn CONTENT into its persistent scroller. Appends ONLY
// the newly-landed turns (the rendered prefix is stable — dedup only folds
// consecutive duplicates, and turns arrive append-only), so a live beat adds the
// tail nodes without touching the existing turn DOM (no thread rebuild); a no-op
// beat writes ZERO DOM. A bottom-pinned reader keeps tailing new turns. When the
// prefix genuinely DIVERGES (a completed run's final transcript replacing a
// partial with different structure, or a later annotation on an existing turn),
// it falls back to a full rebuild of the scroller.
function reconcileTranscript(hostEl, side, sel, conv) {
  const col = hostEl.querySelector('[data-xscript-col="' + side + '"]');
  if (!col) return;
  const scroller = col.querySelector('[data-scroll-side="' + side + '"]');
  if (!scroller) return; // err / wait / nosel column — no scroller to fill.

  // DEDUP CONSECUTIVE IDENTICAL TURNS. goldfive emits the goal twice — on
  // `runStarted.goalSummary` and again on `goalDerived` (the LiteralGoalDeriver
  // echoes the same string) — so the goal reads twice; collapse the literal
  // duplicate (see dedupConsecutiveTurns).
  const turns = dedupConsecutiveTurns((conv && Array.isArray(conv.turns)) ? conv.turns : []);
  const anns = (conv && Array.isArray(conv.annotations)) ? conv.annotations : [];
  const annBySeq = new Map();
  for (const a of anns) {
    const k = a.anchor_seq;
    if (!annBySeq.has(k)) annBySeq.set(k, []);
    annBySeq.get(k).push(a);
  }

  const wantSig = turns.map((t) => turnSig(t, annBySeq));
  const haveSig = Array.isArray(scroller._turnSig) ? scroller._turnSig : [];
  let prefixOk = haveSig.length <= wantSig.length;
  if (prefixOk) for (let i = 0; i < haveSig.length; i += 1) { if (haveSig[i] !== wantSig[i]) { prefixOk = false; break; } }

  if (prefixOk && haveSig.length === wantSig.length) {
    // No content change — ZERO DOM (scroll untouched).
  } else if (prefixOk) {
    // APPEND the tail turns only — the existing turn nodes stay in place.
    const pinned = nearBottom(scroller);
    for (let i = haveSig.length; i < turns.length; i += 1) scroller.appendChild(buildTurnNode(turns[i], annBySeq));
    scroller._turnSig = wantSig;
    if (pinned && typeof scroller.scrollHeight === 'number') scroller.scrollTop = scroller.scrollHeight;
  } else {
    // The rendered prefix diverged — rebuild the scroller wholesale.
    clearChildren(scroller);
    for (const t of turns) scroller.appendChild(buildTurnNode(t, annBySeq));
    scroller._turnSig = wantSig;
  }

  // The streaming caption reads "streaming — through turn N" while running; the
  // caption shell exists only on a running column, so it is gone once complete.
  const cap = col.querySelector('[data-stream-count]');
  if (cap) cap.textContent = 'streaming — through turn ' + turns.length;
}

// Per-turn content signature for the append reconcile — seq / role / text length
// / tool-call count / annotation count. Two turns with the same signature render
// identically, so an unchanged prefix is a true no-op; a changed one rebuilds.
function turnSig(t, annBySeq) {
  const na = annBySeq && annBySeq.get(t.seq);
  return [t.seq, t.role, (t.text || '').length, Array.isArray(t.tool_calls) ? t.tool_calls.length : 0, na ? na.length : 0].join(':');
}

// Whether the scroller is pinned at (or within a hair of) the bottom — the
// live-tail signal. A headless test DOM without scroll metrics defaults to tail.
function nearBottom(scroller) {
  const sh = scroller.scrollHeight, st = scroller.scrollTop, ch = scroller.clientHeight;
  if (typeof sh !== 'number' || typeof ch !== 'number' || typeof st !== 'number') return true;
  return (sh - st - ch) <= 8;
}

// Build ONE turn's DOM node — the shared turn renderer for both the initial fill
// and the live append, so an appended turn is byte-identical to a rebuilt one.
function buildTurnNode(t, annBySeq) {
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
  for (const a of ((annBySeq && annBySeq.get(t.seq)) || [])) {
    turn.appendChild(el('div', { class: 'dn-annot dn-annot-' + (a.kind || 'note'), text: '◂ ' + (a.summary || a.kind) }));
  }
  return turn;
}

// Drop a turn that EXACTLY repeats the one immediately before it — same role,
// identical non-empty text, neither carrying its own tool calls — folding the
// literal goal duplicate (runStarted then goalDerived) to ONE read. Genuinely-
// distinct turns (different text, a tool call, an empty turn) are kept, and only
// CONSECUTIVE duplicates fold (a later echo across intervening turns is kept).
export function dedupConsecutiveTurns(turns) {
  const list = Array.isArray(turns) ? turns : [];
  const out = [];
  for (const t of list) {
    const prev = out[out.length - 1];
    if (prev && isDuplicateTurn(prev, t)) continue;
    out.push(t);
  }
  return out;
}

function isDuplicateTurn(a, b) {
  if (!a || !b) return false;
  const aText = (a.text || '').trim();
  const bText = (b.text || '').trim();
  if (aText === '' || aText !== bText) return false;
  if ((a.role || '') !== (b.role || '')) return false;
  const aTools = Array.isArray(a.tool_calls) && a.tool_calls.length;
  const bTools = Array.isArray(b.tool_calls) && b.tool_calls.length;
  if (aTools || bTools) return false;
  return true;
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

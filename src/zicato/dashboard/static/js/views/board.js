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
import { livenessFor } from '../livestatus.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, decisionFor, densityTokens, prText, metricsDigest, scoreFmt, pill, dataTable, deltaCell } from '../ui.js';
import { splitFrame, captureScroll, restoreScroll } from '../compare.js';
import * as facets from '../facets.js';

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

// The FULL entry-kind vocabulary (core/board.py::BoardEntryKind) — a missing
// key renders the entry unlabelled.
const KIND_LABEL = {
  single_turn: 'single-turn', multi_turn_scripted: 'scripted multi-turn',
  multi_turn_emulated: 'emulated multi-turn',
  synthetic_adversarial: 'synthetic adversarial', synthetic_clean: 'synthetic clean',
};

// The orchestrator progress-seq at our LAST render of a GIVEN (epoch, entry) —
// the seam that gates the live transcript refetch on a genuine seq ADVANCE
// (never a no-op heartbeat). Keyed PER-ENTRY: the board view is a singleton
// render target, so a bare module scalar would leak one entry's now-current seq
// into a return visit to another entry and (seeing "seq unchanged") serve its
// warm-but-stale cache without a refetch. A missing key (first render, or a
// return after the seq advanced elsewhere) reads undefined ≠ lastSeq and so
// BUSTS; state.lastSeq === -1 (no seq seen — a pre-RUNTIME-V2 server) degrades to
// the legacy always-bust path.
const _lastBoardSeqByEntry = new Map();

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

  const [rows0, traj, dossier, roster] = await Promise.all([
    D.generationsForEpoch(epochId), D.scoreTrajectory(epochId), D.evalDossier(epochId, entryId),
    D.judgeRoster(epochId),
  ]);
  // The AUTHORED half of the Judges panel — this entry's custom judges, off the
  // epoch payload's additive `board_judges` map (omitted entirely by a board
  // whose entries declare none, so `[]` here is the honest read, not a failure).
  const entryJudges = (ep.board_judges && ep.board_judges[entryId]) || [];
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
  // Only while the run is actually live: an `active_runs` record left behind by
  // a torn-down run would otherwise synthesise a permanent "running" row with a
  // progress bar that never completes (issue #194 SS1).
  const inflight = livenessFor(state).liveness.live
    ? inflightForEntry(state.activeRuns, entryId) : [];
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
  const seqKey = epochId + '\x00' + entryId;
  const seqAdvanced = state.lastSeq < 0 || state.lastSeq !== _lastBoardSeqByEntry.get(seqKey);
  _lastBoardSeqByEntry.set(seqKey, state.lastSeq);
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

  // The FACET slices this entry feeds, and how each candidate scores on them.
  // Both halves ride on the per-entry payload already fetched above: the entry
  // row names its facets, and each candidate's `facet_scores` carries that
  // candidate's aggregate per facet. So the drill-down answers "this entry is
  // part of data_cleaning — how is data_cleaning doing across candidates?"
  // without a second round trip and without the client aggregating anything
  // (DQ1: the server already did the grouping).
  const entryFacets = [];
  const facetByGen = new Map();
  genList.forEach((g, i) => {
    const pe = perEntries[i];
    if (!pe) return;
    const row = Array.isArray(pe.entries) ? pe.entries.find((e) => e.entry_id === entryId) : null;
    if (row && Array.isArray(row.facets)) {
      for (const f of row.facets) if (typeof f === 'string' && !entryFacets.includes(f)) entryFacets.push(f);
    }
    const fs = pe.facet_scores && pe.facet_scores.facets;
    if (fs && typeof fs === 'object') facetByGen.set(g.id, fs);
  });
  entryFacets.sort();

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
    def: def ? [def.kind, def.weight, def.budget_s, def.expectation_kind || null, (def.tags || []).join(',')] : null,
    champ: championId,
    // rows fold the continuous score + its precision/recall metrics (#18) so a
    // scored board repaints when a score moves; a bool-only row contributes
    // null for both (back-compat: digest unchanged vs the pre-score path).
    rows: rows.map((r) => [r.gen, svg.isNum(r.loss) ? r.loss.toFixed(3) : null, r.pass, r.timeout, r.promoted, r.runId, !!r.running, !!r.cached, r.sourceEpoch || null, svg.isNum(r.score) ? r.score.toFixed(3) : null, metricsDigest(r.metrics)]),
    inflight: inflight.map((r) => {
      const pr = progressRatio(r);
      return [r.generation_id || null, r.run_id || null, pr != null ? pr.toFixed(2) : null];
    }),
    // The facet panel folds at RENDERED precision, so a no-op beat leaves the
    // digest byte-identical and the panel's DOM survives (G10). An untagged
    // entry contributes an empty list — unchanged digest vs the pre-facet path.
    // The coverage suffix is part of the rendered string, so the digest folds
    // the whole cell text rather than the scalar alone — otherwise a slice
    // that gained runs between beats would keep a stale `1/4` on screen.
    facets: entryFacets.map((f) => [f, ...genList.map((g) => {
      const cell = (facetByGen.get(g.id) || {})[f];
      return cell ? facets.facetScalarText(cell) : null;
    })]),
    // The Judges panel is contract-frozen for the epoch, so its contribution is
    // a constant across a round's beats — it can never be the thing that busts
    // this digest, and it repaints only when the panel genuinely changed.
    judges: judgesDigest(roster, entryJudges),
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

    // The drill-down used to show LESS of the entry than the overview it is
    // reached from: `expectation_kind` and `tags` ride the same ep.board row
    // the trellis already reads, and this page — the one an operator opens to
    // ask what this entry actually checks — dropped both.
    nodes.push(el('div', { class: 'dn-panel dn-row' }, [
      stat(def ? (KIND_LABEL[def.kind] || def.kind || '—') : '—', 'kind'),
      stat(def && def.expectation_kind ? String(def.expectation_kind) : '—', 'oracle'),
      stat(def && svg.isNum(def.weight) ? svg.fmt(def.weight, 1) : '—', 'weight'),
      stat(def && svg.isNum(def.budget_s) ? def.budget_s + 's' : '—', 'budget'),
      stat(String(rows.filter((r) => r.ran).length) + '/' + String(rows.length), 'candidates ran'),
    ]));
    const tags = (def && Array.isArray(def.tags)) ? def.tags : [];
    if (tags.length) {
      nodes.push(el('p', { class: 'dn-faint dn-board-tags', text: 'tags · ' + tags.join(' · ') }));
    }
    if (def && def.input_preview) {
      nodes.push(el('div', { class: 'dn-panel' }, [
        el('div', { class: 'dn-faint', style: 'font-size:10px;text-transform:uppercase;letter-spacing:0.06em;', text: 'input preview' }),
        el('div', { style: 'margin-top:4px;line-height:1.4;', text: '“' + def.input_preview + '”' }),
      ]));
    }

    // JUDGES (#194 §5) — the process half of the contract, beside the outcome
    // half (kind / oracle / weight) the stat row above already names. It sits in
    // the CONTRACT region of the page, before any result: what grades a run is
    // part of the question, not part of the answer. A pre-feature server serves
    // no roster AND the epoch payload carries no board_judges, so nothing is
    // known and nothing is drawn — the page reads byte-identical to before.
    if (roster || entryJudges.length) {
      nodes.push(section('Judges · what grades this entry’s process',
        judgesPanel(roster, entryJudges, ctx, epochId)));
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

    // The facet slices this entry feeds, each candidate's SCALAR across them —
    // placed directly under the per-candidate loss it extends: that section
    // answers "how did each candidate do on THIS entry", this one answers "how
    // are the slices this entry belongs to doing across candidates". Same
    // quantity, same units, one level up. Diagnostic only, as on the dossier.
    if (entryFacets.length) {
      const ran = genList.filter((g) => facetByGen.has(g.id));
      // CANDIDATES are the rows, facets the columns — the orientation the
      // rest of this page uses, and the one that scales: an epoch grows
      // candidates without bound, while an entry's facet tags are fixed and
      // few. One column per candidate would run off the page by round twenty.
      //
      // Every label, format and explanation comes from ../facets.js, shared
      // with the candidate dossier's facet table. The two screens ask
      // different questions and so differ in ORIENTATION — nothing else.
      const facetTbl = dataTable({
        class: 'dn-facet-table',
        // The unit rides on the row header: every cell is that candidate's
        // scalar for that facet.
        columns: [
          { label: 'candidate · ' + facets.SCALAR_LABEL },
          ...entryFacets.map((f) => ({ label: f, class: 'dn-num' })),
        ],
        rows: ran.map((g) => [
          // The champion is NAMED here (the `○` this view already uses), never
          // marked by weighting its numbers: dimming a column reads as emphasis
          // on the others, which is a verdict this table must not imply.
          { text: g.id === championId ? g.id + ' ○' : g.id },
          // The scalar carries its coverage when the slice is not whole: this
          // table has no room for a count column, and a scalar resting on one
          // run of a four-entry slice must not print identically to one
          // resting on all four.
          ...entryFacets.map((f) => {
            const cell = (facetByGen.get(g.id) || {})[f];
            return { text: cell ? facets.facetScalarText(cell) : facets.facetNum(null), class: 'dn-num' };
          }),
        ]),
      });
      facets.attachFacetHover(facets.tableHeaderCells(facetTbl)[0], 'scalar');
      nodes.push(section('Facets this entry feeds · candidate scalar per slice', el('div', { class: 'dn-panel' }, [
        facets.facetCaption('the slices this entry feeds, per candidate'),
        facetTbl,
      ])));
    }


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

  // EVAL DOSSIER (EVAL-VIEW.md §3.2 / WS-DOSSIER) — the board-as-instrument lens
  // for THIS entry across every candidate. ADDITIVE: it mounts in its OWN
  // persistent host BETWEEN the breakdown and the side-by-side transcript, gated
  // on its OWN digest, so the live-streaming transcript render path below is
  // untouched (its host, digest, scroll capture, and turn reconcile are never
  // perturbed by a dossier repaint). A pre-feature server (no /eval endpoint →
  // `dossier` null) mounts NO host, so the board reads byte-identical to before
  // the feature; a served-but-empty payload (cold index / unknown entry) mounts
  // the host and honest-empties every section (a null is truth; §4).
  let dossierHost = host.querySelector(':scope > [data-node="board-dossier"]');
  if (dossier && !dossierHost) {
    dossierHost = el('div', { 'data-node': 'board-dossier' });
    host.insertBefore(dossierHost, xscriptHost);
  }
  if (dossierHost) {
    gatedSwap(dossierHost, evalDossierDigest(dossier, epochId, entryId),
      () => evalDossierSections(dossier, ctx, epochId, entryId));
  }

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
// ONLY divergence is the final rendered turn growing (a merged llmCall reasoning
// turn whose text grows across two seqs — the ROUTINE streaming case), just that
// one node is re-rendered in place, preserving every prefix node + the scroll
// position. A GENUINE prefix divergence (an earlier turn changed, or the list
// shrank — a completed run's final transcript) falls back to a full rebuild that
// still preserves the reader's scroll discipline (pinned stays pinned, scrolled-
// up keeps its offset).
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
  // The FIRST index at which the rendered signatures diverge from the desired
  // (within the overlap). -1 ⇒ the rendered prefix is intact and the desired
  // list only extends it (pure append) or is identical.
  let diverge = -1;
  const overlap = Math.min(haveSig.length, wantSig.length);
  for (let i = 0; i < overlap; i += 1) { if (haveSig[i] !== wantSig[i]) { diverge = i; break; } }

  if (diverge === -1 && haveSig.length <= wantSig.length) {
    if (haveSig.length === wantSig.length) {
      // No content change — ZERO DOM (scroll untouched).
    } else {
      // APPEND the tail turns only — the existing turn nodes stay in place.
      const pinned = nearBottom(scroller);
      for (let i = haveSig.length; i < turns.length; i += 1) scroller.appendChild(buildTurnNode(turns[i], annBySeq));
      scroller._turnSig = wantSig;
      if (pinned && typeof scroller.scrollHeight === 'number') scroller.scrollTop = scroller.scrollHeight;
    }
  } else if (diverge === haveSig.length - 1 && wantSig.length >= haveSig.length) {
    // LAST-TURN-GREW — the ONLY divergence is the final rendered turn, the
    // ROUTINE streaming case (goldfive's llmCallStart→llmCallEnd merge into ONE
    // turn whose text grows across two seqs, flipping just the last turnSig while
    // every earlier turn is byte-stable). Re-render JUST that node in place +
    // append any tail; the prefix nodes and scroll position are preserved (no
    // clamp-to-0 as the wholesale rebuild below would cause). It is the last
    // rendered node, so remove-then-append keeps document order.
    const pinned = nearBottom(scroller);
    const idx = haveSig.length - 1;
    const oldNode = scroller.childNodes[idx];
    if (oldNode) scroller.removeChild(oldNode);
    scroller.appendChild(buildTurnNode(turns[idx], annBySeq));
    for (let i = haveSig.length; i < turns.length; i += 1) scroller.appendChild(buildTurnNode(turns[i], annBySeq));
    scroller._turnSig = wantSig;
    if (pinned && typeof scroller.scrollHeight === 'number') scroller.scrollTop = scroller.scrollHeight;
  } else {
    // GENUINE prefix divergence — an earlier turn changed, or the list shrank (a
    // completed run's final transcript). Rebuild wholesale, but preserve the
    // reader's scroll DISCIPLINE across the clear: a bottom-pinned reader stays
    // pinned (keeps live-tailing), a scrolled-up reader keeps their offset.
    // Capture BEFORE clearChildren; the outer restoreScroll (~:421) fires only on
    // a FRAME rebuild — never on a content-blind beat — so the pin lands here.
    const pinned = nearBottom(scroller);
    const prevTop = typeof scroller.scrollTop === 'number' ? scroller.scrollTop : null;
    clearChildren(scroller);
    for (const t of turns) scroller.appendChild(buildTurnNode(t, annBySeq));
    scroller._turnSig = wantSig;
    if (typeof scroller.scrollHeight === 'number') {
      if (pinned) scroller.scrollTop = scroller.scrollHeight;
      else if (prevTop != null) scroller.scrollTop = prevTop;
    }
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
// EXPORTED so the Traces view (views/traces.js) reconstructs a foreign trace's
// conversation with the IDENTICAL transcript turn vocabulary (WS-TRACES · reuse,
// don't fork): a trace turn ({role, text}) renders through this same builder with
// an empty annotation map (foreign traces carry no per-seq annotations).
export function buildTurnNode(t, annBySeq) {
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

// ── JUDGES (#194 §5) ──────────────────────────────────────────────────
//
// What actually grades a run on THIS entry. Two payloads, joined here: the
// entry's own custom judges ride the epoch payload (`board_judges`, the
// authored half) and the built-in roster after `disable_drift` suppression
// rides `/api/epoch/{id}/judge-roster` (the derived half). The panel keeps the
// suppressed built-ins ON screen, struck through — a shorter list would show
// the header's effect by omission, which is to say not at all.

// Quiet precision: an integral weight prints bare, a fractional one to two
// places — `×2`, `×0.75`, never `×2.00`.
export function weightText(w) {
  if (!svg.isNum(w)) return null;
  return '×' + (Number.isInteger(w) ? String(w) : w.toFixed(2));
}

// Severity → the shipped pill TONE. Reuses the colour vocabulary rather than
// minting a severity palette: red for critical, caution for warning, neutral
// for info and for anything the board spells differently.
export function severityTone(severity) {
  if (severity === 'critical') return 'rejected';
  if (severity === 'warning') return 'deferred';
  return 'baseline';
}

// The one line that says what the board's `disable_drift` header ACTUALLY did.
// Null when the board names no kinds. A named kind that no built-in judge emits
// suppresses NOTHING, and the line says so by name — the alternative (silence)
// reads as "suppressed", which is the misreading this panel exists to prevent.
export function suppressionText(roster) {
  const kinds = (roster && Array.isArray(roster.disable_drift)) ? roster.disable_drift : [];
  if (!kinds.length) return null;
  const off = ((roster && Array.isArray(roster.builtins)) ? roster.builtins : [])
    .filter((b) => b && b.suppressed).map((b) => b.name);
  const unmapped = (roster && Array.isArray(roster.unmapped_drift_kinds)) ? roster.unmapped_drift_kinds : [];
  const clauses = [];
  if (off.length) clauses.push('suppresses ' + off.join(', '));
  if (unmapped.length) {
    clauses.push('no built-in judge emits ' + unmapped.join(', ')
      + ', so ' + (unmapped.length === 1 ? 'it suppresses' : 'they suppress') + ' nothing');
  }
  return 'disable_drift · ' + kinds.join(' · ') + (clauses.length ? ' — ' + clauses.join('; ') : '');
}

// The faint uppercase micro-label the entry's input-preview panel already uses.
// `spaced` opens a gap above it — the second label in a panel needs to separate
// from the block it follows, the first sits flush against the panel's own padding.
function microLabel(text, spaced) {
  return el('div', { class: 'dn-faint', text,
    style: 'font-size:10px;text-transform:uppercase;letter-spacing:0.06em;' + (spaced ? 'margin-top:14px;' : '') });
}

// A link to a judge's reflection scorecard — only when a reflection scored it.
// The Instrument lens routes epoch → reflection → judge, so the link lands on
// that judge's card rather than the reflection's front page.
function scorecardLink(roster, name, ctx, epochId) {
  const rid = (roster && roster.scorecards && roster.scorecards[name]) || null;
  if (!rid) return el('span', { class: 'dn-faint', text: '—' });
  return el('a', {
    class: 'dn-linkbtn dn-mono', href: ctx.href('instrument', { epochId, reflectionId: rid, judge: name }),
    title: 'open this judge’s scorecard in the Instrument lens (' + rid + ')', text: 'scorecard →',
  });
}

// One built-in's chip. A suppressed built-in carries the reason IN the chip, not
// only in a tooltip: an operator scanning the strip must be able to read why a
// judge is dark without hovering it.
function builtinChip(b, weights) {
  const by = Array.isArray(b.suppressed_by) ? b.suppressed_by : [];
  const w = weightText(weights[b.name]);
  return el('span', {
    class: 'dn-pill ' + (b.suppressed ? 'dn-judge-off' : 'dn-baseline'),
    title: b.suppressed
      ? 'suppressed by disable_drift' + (by.length ? ' · ' + by.join(' · ') : '')
      : 'armed for every run on this board',
  }, [
    el('span', { class: 'dn-judge-name', text: b.name }),
    b.suppressed ? el('span', { class: 'dn-faint', text: 'suppressed by disable_drift' }) : null,
    w ? el('span', { class: 'dn-faint', text: w }) : null,
  ].filter(Boolean));
}

// The Judges panel: armed built-ins, then this entry's custom judges. Both
// halves degrade to a sentence that carries information — "no judges
// configured" is a fact about the contract, not a missing payload.
export function judgesPanel(roster, entryJudges, ctx, epochId) {
  const weights = (roster && roster.per_judge_weights && typeof roster.per_judge_weights === 'object')
    ? roster.per_judge_weights : {};
  const builtins = (roster && Array.isArray(roster.builtins)) ? roster.builtins : [];
  const judges = Array.isArray(entryJudges) ? entryJudges : [];
  const card = el('div', { class: 'dn-panel' });

  card.appendChild(microLabel('armed built-ins'));
  if (builtins.length) {
    card.appendChild(el('div', { class: 'dn-judges-strip' }, builtins.map((b) => builtinChip(b, weights))));
  } else {
    // Never guess a roster we could not read: the server names the reason.
    card.appendChild(empty((roster && roster.builtins_note)
      || 'Built-in roster not served for this epoch.'));
  }
  const supp = suppressionText(roster);
  if (supp) card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:6px 0 0;', text: supp }));

  card.appendChild(microLabel('this entry’s judges', true));
  if (!judges.length) {
    card.appendChild(empty('predicate/rubric only — no judges configured on this entry.'));
  } else {
    card.appendChild(dataTable({
      class: 'dn-board-table',
      columns: [{ label: 'judge' }, { label: 'mode' }, { label: 'severity' }, { label: 'reflection' }],
      rows: judges.map((j) => { const w = weightText(weights[j.name]); return [
        { class: 'dn-mono', el: [
          el('span', { text: j.name }),
          // The weight rides BESIDE the name (per_judge_weights keys on judge
          // name, across built-ins and custom judges alike); absent when the
          // contract configured none for this judge.
          w ? el('span', { class: 'dn-faint', text: ' ' + w }) : null,
          // A python judge's body is its dotted import path — the callable's
          // identity, and the only thing telling two python judges apart. An
          // inline judge's body is the criterion PROMPT and stays off-screen.
          j.path ? el('div', { class: 'dn-faint', style: 'font-size:10.5px;', text: j.path }) : null,
        ].filter(Boolean) },
        { class: 'dn-mono dn-faint', text: j.mode || '—' },
        { el: j.severity ? pill(severityTone(j.severity), j.severity) : el('span', { class: 'dn-faint', text: '—' }) },
        { el: scorecardLink(roster, j.name, ctx, epochId) },
      ]; }),
    }));
  }

  const dflt = weightText(roster && roster.default_judge_weight);
  card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;',
    text: 'built-ins are armed for every entry on this board; the judges above are this entry’s own'
      + (dflt ? ' · any judge without a weight counts ' + dflt : '') }));
  return card;
}

// The panel's digest contribution — value-only, folded into the board's upper
// digest so a no-op beat leaves the panel's DOM untouched (G10).
export function judgesDigest(roster, entryJudges) {
  const r = roster || {};
  return JSON.stringify({
    note: r.builtins_note || null,
    builtins: (Array.isArray(r.builtins) ? r.builtins : []).map((b) => [b && b.name, !!(b && b.suppressed), (b && b.suppressed_by) || []]),
    kinds: Array.isArray(r.disable_drift) ? r.disable_drift : [],
    unmapped: Array.isArray(r.unmapped_drift_kinds) ? r.unmapped_drift_kinds : [],
    weights: r.per_judge_weights || {},
    dflt: svg.isNum(r.default_judge_weight) ? r.default_judge_weight : null,
    cards: r.scorecards || {},
    entry: (Array.isArray(entryJudges) ? entryJudges : []).map((j) => [j && j.name, (j && j.mode) || null, (j && j.severity) || null, (j && j.path) || null]),
  });
}

// ── EVAL DOSSIER (EVAL-VIEW.md §3.2 / WS-DOSSIER) ─────────────────────
//
// The board-as-instrument lens for ONE entry across every candidate, rendered
// beside (never inside) the transcript surface. Four honest sections: the
// champion-spine TRAJECTORY, the INSTRUMENT stats, the first-passed / regressed
// ATTRIBUTION, and links into the REFLECTION findings. Every field degrades
// honestly — a null / "unmeasured" is truth, a fabricated 0.0 is a lie (§4).

// The champion-spine trajectory series: the entry's outcome along the promoted
// spine in round order (the axis the matrix columns order by). Pure + exported
// for the known-answer test. NON-spine candidates are excluded (the spine is the
// instrument's reference trajectory); a spine cell that never ran contributes a
// NaN so the sparkline lifts its pen rather than draw to a fabricated point.
export function trajectorySeries(dossier) {
  const traj = (dossier && Array.isArray(dossier.trajectory)) ? dossier.trajectory : [];
  const spine = traj.filter((t) => t && t.champion_spine);
  return {
    gens: spine.map((t) => t.generation_id),
    loss: spine.map((t) => (svg.isNum(t.drift_loss) ? t.drift_loss : NaN)),
    passRatio: spine.map((t) => (svg.isNum(t.pass_ratio) ? t.pass_ratio : NaN)),
  };
}

// The trajectory figure — the shipped sparkline grammar (aspect-locked
// responsive per the house rule), drift loss along the spine, end dot coloured
// by whether the instrument improved (lower = better). Honest-empty when no
// spine cell ran.
function trajectoryFigure(dossier) {
  const card = el('div', { class: 'dn-panel' });
  const series = trajectorySeries(dossier);
  if (!series.loss.some((v) => svg.isNum(v))) {
    card.appendChild(empty('No champion-spine trajectory for this entry yet.'));
    return card;
  }
  card.appendChild(svg.sparkline({
    values: series.loss, responsive: true, markers: true, minSpan: 1, padY: 0.18,
    goodDirection: 'down',
  }));
  card.appendChild(el('div', { class: 'dn-legend' }, [
    el('span', { class: 'dn-faint', text: 'drift loss along the champion spine · round order · lower is better · the end dot turns green when the instrument’s reading improved across the reign' }),
  ]));
  return card;
}

// A human ms label — seconds over 1s, else raw ms.
function msLabel(ms) {
  if (!svg.isNum(ms)) return '—';
  return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms';
}

// The INSTRUMENT stats row — is this eval a good measurement channel? Flip rate
// (with the unmeasured degrade), discrimination, runtime aggregates, replicate
// total, cached share, and the slice/holdout membership — all in the dn-stat
// idiom. §4: an unmeasured flip rate reads "unmeasured", NEVER 0.0.
function instrumentStats(dossier) {
  const ins = (dossier && dossier.instrument) || {};
  const measured = !!ins.flip_rate_measured;
  const flipVal = measured && svg.isNum(ins.flip_rate) ? svg.fmt(ins.flip_rate, 2) : 'unmeasured';
  const flipKey = measured
    ? 'flip rate (A/A)' + (ins.calibration_runs ? ' · ' + ins.calibration_runs + ' draws' : '')
    : 'flip rate · unmeasured';
  const discKey = 'discrimination'
    + (ins.discrimination_pairs ? ' · ' + ins.discrimination_pairs + ' pairs' : '');
  const slice = (dossier && dossier.slice) || 'train';
  return el('div', { class: 'dn-panel dn-row' }, [
    stat(flipVal, flipKey),
    stat(svg.isNum(ins.discrimination) ? svg.fmt(ins.discrimination, 2) : '—', discKey),
    stat(msLabel(ins.runtime_ms_mean), 'runtime mean'),
    stat(msLabel(ins.runtime_ms_p50), 'runtime p50'),
    stat(msLabel(ins.runtime_ms_max), 'runtime max'),
    stat(String(ins.replicate_total || 0), 'replicates'),
    stat(svg.isNum(ins.cached_share) ? Math.round(ins.cached_share * 100) + '%' : '—', 'cached share'),
    stat(slice, slice === 'holdout' ? 'slice · holdout' : 'slice'),
  ]);
}

// A link to another candidate's dossier (the attribution targets).
function candLink(gen, ctx, epochId) {
  return el('a', { class: 'dn-linkbtn dn-mono', href: ctx.href('candidate', { epochId, gen }), text: gen });
}

// Interleave candidate links with faint separators (the regressed-by list).
function interleaveLinks(gens, ctx, epochId) {
  const out = [];
  gens.forEach((g, i) => {
    if (i) out.push(el('span', { class: 'dn-faint', text: ' · ' }));
    out.push(candLink(g, ctx, epochId));
  });
  return out;
}

// One quiet verdict-led attribution row — a tone glyph + headline + the linked
// candidate(s) + a dn-faint rationale (NOT a chip). Mirrors the Instrument lens'
// loop-health row grammar.
function attributionRow(tone, label, linkNodes, rationale) {
  const glyphCls = tone === 'good' ? 'dn-good-t' : tone === 'bad' ? 'dn-bad-t' : 'dn-faint';
  const glyph = tone === 'good' ? '▲' : tone === 'bad' ? '▼' : '·';
  return el('div', { class: 'dn-eval-attr-row' }, [
    el('div', { class: 'dn-eval-attr-head' }, [
      el('span', { class: glyphCls, text: glyph + ' ' }),
      el('span', { text: label + ' ' }),
      ...(linkNodes || [el('span', { class: 'dn-faint', text: '—' })]),
    ]),
    el('p', { class: 'dn-faint dn-eval-attr-why', text: rationale }),
  ]);
}

// The ATTRIBUTION section — the served first-passed-by / regressed-by, each one
// quiet line linking to the named candidate's dossier. All degrade honestly.
function attributionSection(dossier, ctx, epochId) {
  const attr = (dossier && dossier.attribution) || {};
  const firstBy = attr.first_passed_by || null;
  const regressed = Array.isArray(attr.regressed_by) ? attr.regressed_by.filter(Boolean) : [];
  const card = el('div', { class: 'dn-panel' });
  card.appendChild(attributionRow(
    firstBy ? 'good' : 'faint', 'first passed by',
    firstBy ? [candLink(firstBy, ctx, epochId)] : null,
    firstBy
      ? 'the first champion-spine generation whose verdict on this entry passed'
      : 'no champion-spine generation has passed this entry yet',
  ));
  card.appendChild(attributionRow(
    regressed.length ? 'bad' : 'faint', 'regressed by',
    regressed.length ? interleaveLinks(regressed, ctx, epochId) : null,
    regressed.length
      ? 'champion-spine generations that flipped a prior pass back to a fail on this entry'
      : 'no champion-spine generation regressed a prior pass on this entry',
  ));
  return card;
}

// REFLECTION-finding links — when the served payload carries reflection findings
// that NAME this entry, list them linking into the Instrument view for the
// adjudicated detail. Recommend-only framing (the word "recommend" stays with
// reflect). Honest-empty otherwise.
function reflectionLinks(dossier, ctx, epochId) {
  const findings = (dossier && Array.isArray(dossier.reflection_findings)) ? dossier.reflection_findings : [];
  const card = el('div', { class: 'dn-panel' });
  if (!findings.length) {
    card.appendChild(empty('No reflection findings touch this entry. Run zicato reflect to diagnose the instrument (recommend-only — it never edits the contract).'));
    return card;
  }
  for (const item of findings) {
    const rid = item && item.reflection_id;
    const f = (item && item.finding) || {};
    const title = f.title || f.finding_id || 'finding';
    card.appendChild(el('div', { class: 'dn-eval-refl-row' }, [
      el('a', {
        class: 'dn-instr-link dn-mono',
        href: ctx.href('instrument', { epochId, reflectionId: rid }),
        title: 'open the Instrument view for ' + (rid || 'this reflection'),
        text: title,
      }),
      rid ? el('span', { class: 'dn-faint', text: ' · ' + rid }) : null,
    ].filter(Boolean)));
  }
  card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'reflection findings that name this entry — open the Instrument view for the adjudicated detail (recommend-only; reflect never edits the contract)' }));
  return card;
}

// The four dossier sections, in reading order: trajectory → instrument stats →
// attribution → reflection links. Exported for the render + the tests.
export function evalDossierSections(dossier, ctx, epochId, entryId) {
  return [
    section('Trajectory · outcome along the champion spine', trajectoryFigure(dossier)),
    section('Instrument stats · is this eval a good measurement channel', instrumentStats(dossier)),
    section('Attribution · who first passed it & who regressed', attributionSection(dossier, ctx, epochId)),
    section('Reflection findings', reflectionLinks(dossier, ctx, epochId)),
  ];
}

// The dossier digest — folds only the served, view-visible fields so a no-op SSE
// beat (identical payload) produces a byte-identical string and the gate does
// ZERO DOM. Volatile object identity never enters it (value-only).
export function evalDossierDigest(dossier, epochId, entryId) {
  const d = dossier || {};
  const ins = d.instrument || {};
  const round = (v) => (svg.isNum(v) ? Number(v.toFixed(3)) : null);
  return JSON.stringify({
    epochId: epochId == null ? null : String(epochId),
    entryId: entryId == null ? null : String(entryId),
    found: d.found == null ? null : !!d.found,
    slice: d.slice == null ? null : String(d.slice),
    instrument: [
      !!ins.flip_rate_measured, round(ins.flip_rate), ins.calibration_runs || 0,
      round(ins.discrimination), ins.discrimination_pairs || 0,
      round(ins.runtime_ms_mean), round(ins.runtime_ms_p50), round(ins.runtime_ms_max),
      ins.replicate_total || 0, round(ins.cached_share),
    ],
    trajectory: (Array.isArray(d.trajectory) ? d.trajectory : []).map((t) => [
      t && t.generation_id, !!(t && t.champion_spine), round(t && t.drift_loss),
      round(t && t.pass_ratio), (t && t.replicates) || 0, !!(t && t.cached),
    ]),
    attribution: [
      (d.attribution && d.attribution.first_passed_by) || null,
      (d.attribution && Array.isArray(d.attribution.regressed_by)) ? d.attribution.regressed_by.slice() : [],
    ],
    reflection: (Array.isArray(d.reflection_findings) ? d.reflection_findings : []).map((r) => [
      r && r.reflection_id, r && r.finding && (r.finding.finding_id || r.finding.title || null),
    ]),
  });
}

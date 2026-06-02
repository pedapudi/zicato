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

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, normaliseDecision, densityTokens } from '../ui.js';

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

  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Board entry' }), empty('No current epoch.')]);
    return;
  }
  if (!entryId) {
    gatedSwap(host, 'no-entry', () => [el('h1', { class: 'dn-h1', text: 'Board entry' }),
      empty('No board entry selected — open one from the Boards trellis or the epoch heatmap.')]);
    return;
  }
  const epochId = ep.epoch_id;
  const board = Array.isArray(ep.board) ? ep.board : [];
  const def = board.find((b) => (b.entry_id || b.id) === entryId) || null;

  const genList = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : (Array.isArray(ep.experiments) ? ep.experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' })) : []);

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  // Pivot per-entry across every generation for THIS entry.
  const perEntries = await Promise.all(genList.map((g) => D.perEntry(epochId, g.id)));
  const rows = [];
  genList.forEach((g, i) => {
    const pe = perEntries[i];
    const r = (pe && Array.isArray(pe.entries)) ? pe.entries.find((e) => e.entry_id === entryId) : null;
    rows.push({
      gen: g.id, promoted: g.promoted, parent: g.parent,
      loss: r && svg.isNum(r.drift_loss) ? r.drift_loss : NaN,
      pass: r ? r.pass_fail : null,
      timeout: r ? !!r.wall_clock_budget_exceeded : false,
      runId: r ? r.run_id : null,
      ran: !!r,
    });
  });

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
      leftSel && leftSel.runId ? D.conversation(leftSel.runId) : Promise.resolve(null),
      rightSel && rightSel.runId ? D.conversation(rightSel.runId) : Promise.resolve(null),
    ]);
    leftConv = convs[0]; rightConv = convs[1];
  }

  // In-flight runs currently executing on THIS board entry (live, any
  // structure). The completed-results rendering below covers finished runs; an
  // entry mid-run with no completed results yet must read as "N running", never
  // blank — so the in-flight set folds into both the digest and the view.
  const inflight = inflightForEntry(state.activeRuns, entryId);
  const ranCount = rows.filter((r) => r.ran).length;

  const digest = JSON.stringify({
    epochId, entryId, selGen,
    def: def ? [def.kind, def.weight, def.budget_s] : null,
    champ: championId,
    rows: rows.map((r) => [r.gen, svg.isNum(r.loss) ? r.loss.toFixed(3) : null, r.pass, r.timeout, r.promoted, r.runId]),
    left: leftSel ? [leftSel.gen, transcriptDigest(leftConv)] : null,
    right: rightSel ? [rightSel.gen, transcriptDigest(rightConv)] : null,
    inflight: inflight.map((r) => {
      const pr = progressRatio(r);
      return [r.generation_id || r.gen || null, r.run_id || null, pr != null ? pr.toFixed(2) : null];
    }),
  });

  gatedSwap(host, digest, () => {
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
      tbody.appendChild(el('tr', { class: (r.promoted ? 'dn-board-champ' : '') + (isSel ? ' dn-board-sel' : '') }, [
        el('td', { class: 'dn-mono', text: r.gen + (r.promoted ? ' ♛' : '') }),
        el('td', { class: 'dn-num dn-mono', text: svg.isNum(r.loss) ? svg.fmt(r.loss, 1) : '—' }),
        el('td', { class: passClass(r.pass), text: passLabel(r.pass) }),
        el('td', { class: 'dn-mono', text: r.timeout ? 'timed out' : 'ok' }),
        el('td', null, [r.ran
          // TOGGLE: an already-selected candidate's button collapses its inline
          // transcript — its href drops the gen (back to the bare board route),
          // so clicking "showing ↓" closes it and a reload won't reopen it.
          ? el('a', { class: 'dn-linkbtn dn-board-run' + (isSel ? ' dn-linkbtn-on' : ''), href: ctx.href('board', isSel ? { epochId, entry: entryId } : { epochId, entry: entryId, gen: r.gen }), text: isSel ? 'showing ↓' : 'show inline →' })
          : el('span', { class: 'dn-faint', text: 'no run' })]),
      ]));
    }
    tbl.appendChild(tbody);
    tblCard.appendChild(tbl);
    nodes.push(section('Breakdown · select a candidate to read its transcript inline', tblCard));

    // fix #5 — the INLINE side-by-side transcript pane (no navigation away).
    if (selGen) {
      nodes.push(section(
        `Transcripts · ${leftSel ? leftSel.gen : selGen} vs ${rightSel ? rightSel.gen : '—'} on ${entryId}`,
        sideBySideTranscripts(leftSel, leftConv, rightSel, rightConv, championId),
      ));
    }
    return nodes;
  });
}

// Two transcripts on the same board, side by side, in ONE constrained pane —
// the heart of fix #5. Each column is a constrained-scroll turn list; no
// absolute positioning, no route change.
function sideBySideTranscripts(leftSel, leftConv, rightSel, rightConv, championId) {
  const card = el('div', { class: 'dn-panel dn-xscript' });
  const grid = el('div', { class: 'dn-xscript-grid' }, [
    transcriptColumn(leftSel, leftConv, championId),
    transcriptColumn(rightSel, rightConv, championId),
  ]);
  card.appendChild(grid);
  card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'two candidates’ transcripts on this board, side by side — rendered inline, no navigation away' }));
  return card;
}

function transcriptColumn(sel, conv, championId) {
  const col = el('div', { class: 'dn-xscript-col' });
  if (!sel) {
    col.appendChild(el('div', { class: 'dn-xscript-head dn-faint', text: 'no second candidate to compare' }));
    return col;
  }
  const role = sel.gen === championId ? 'champion' : (sel.parent ? 'challenger' : 'seed');
  col.appendChild(el('div', { class: 'dn-xscript-head' }, [
    el('span', { class: 'dn-mono', text: sel.gen + (sel.promoted ? ' ♛' : '') }),
    el('span', { class: 'dn-pill dn-' + (sel.promoted ? 'promoted' : sel.parent ? 'rejected' : 'baseline'), text: role }),
    el('span', { class: 'dn-faint dn-mono', text: svg.isNum(sel.loss) ? ' · loss ' + svg.fmt(sel.loss, 1) : '' }),
  ]));

  const turns = (conv && Array.isArray(conv.turns)) ? conv.turns : [];
  if (!sel.runId) { col.appendChild(empty('No run id for this candidate on this board.')); return col; }
  if (conv && conv.error) { col.appendChild(empty(conv.error)); return col; }
  if (!conv) { col.appendChild(empty('Transcript unavailable (could not be reconstructed).')); return col; }
  if (!turns.length) { col.appendChild(empty('No turns reconstructed for this run.')); return col; }

  const anns = (conv && Array.isArray(conv.annotations)) ? conv.annotations : [];
  const annBySeq = new Map();
  for (const a of anns) {
    const k = a.anchor_seq;
    if (!annBySeq.has(k)) annBySeq.set(k, []);
    annBySeq.get(k).push(a);
  }
  const scroller = el('div', { class: 'dn-transcript dn-xscript-scroll' });
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

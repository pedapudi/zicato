// variants/Q/views/board.js — BOARDS, first-class from the tree (fix #4).
//
// Two modes:
//   * NO entry selected → the board TRELLIS (small multiples, fix #6 — the
//     trellis lives HERE, not on the epoch overview) + a legible field of the
//     board entries. Click a trellis card → that entry's cross-candidate view.
//   * one entry selected → the per-board cross-candidate detail: per-candidate
//     drift loss + pass/fail/timeout (sorted dot-plot + table), AND an INLINE
//     side-by-side TRANSCRIPT (fix #5) — two candidates' transcripts on this
//     board, shown WITHIN the board view, NOT a navigation to a separate run
//     page. Champion (left) vs a challenger (right); pick the challenger by
//     clicking a candidate row.
//
// Bind: /api/epoch.board, /api/generation/{e}/{g}/per-entry pivoted by entry_id
// across generations; /api/conversation/{run_id} per candidate (the transcript).

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, subhead, normaliseDecision } from '../ui.js';

const KIND_ORDER = { multi_turn_emulated: 0, multi_turn_scripted: 1, single_turn: 2 };
const KIND_LABEL = {
  single_turn: 'single-turn', multi_turn_scripted: 'scripted multi-turn',
  multi_turn_emulated: 'emulated multi-turn',
};

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dq-empty', text: 'Reading boards…' }));
  const entryId = params && params.entry;

  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dq-h1', text: 'Boards' }), empty('No current epoch.')]);
    return;
  }
  const epochId = (params && params.epochId) || ep.epoch_id;
  const board = Array.isArray(ep.board) ? ep.board : [];

  const genList = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.filter((g) => !g.epoch_id || g.epoch_id === epochId)
      .map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : (Array.isArray(ep.experiments) ? ep.experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' })) : []);

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  const perEntries = await Promise.all(genList.map((g) => D.perEntry(epochId, g.id)));
  const rowByGenEntry = new Map();
  genList.forEach((g, i) => {
    const pe = perEntries[i];
    if (pe && Array.isArray(pe.entries)) for (const r of pe.entries) rowByGenEntry.set(`${g.id}|${r.entry_id}`, r);
  });
  const allLoss = [];
  for (const v of rowByGenEntry.values()) if (svg.isNum(v.drift_loss)) allLoss.push(v.drift_loss);
  const domain = allLoss.length ? svg.extent(allLoss) : null;

  const champ = genList.find((g) => g.promoted) || genList.find((g) => !g.parent) || null;
  const championId = champ ? champ.id : null;

  // ---- entry NOT selected → the trellis field (fix #6) ----
  if (!entryId) {
    const digest = JSON.stringify({ mode: 'trellis', epochId,
      board: board.map((b) => [b.entry_id || b.id, b.kind, b.weight, b.budget_s]),
      gens: genList.map((g) => g.id),
      loss: [...rowByGenEntry.entries()].map(([k, r]) => [k, svg.isNum(r.drift_loss) ? r.drift_loss.toFixed(2) : null, r.pass_fail, !!r.wall_clock_budget_exceeded]).sort() });
    gatedSwap(host, digest, () => {
      const nodes = [];
      nodes.push(el('div', { class: 'dq-pagehead' }, [
        el('h1', { class: 'dq-h1', text: 'Boards' }),
        el('p', { class: 'dq-lede', text: 'The fixed task board this epoch — a field of tests every candidate faces. Each card shows drift loss across the generations; open one for the cross-candidate detail and side-by-side transcripts.' }),
      ]));
      nodes.push(section('Board trellis · drift loss across candidates', boardTrellis(board, genList, rowByGenEntry, domain, ctx, epochId)));
      return nodes;
    });
    return;
  }

  // ---- one entry selected → cross-candidate detail + INLINE transcript ----
  const def = board.find((b) => (b.entry_id || b.id) === entryId) || null;
  const rows = genList.map((g) => {
    const r = rowByGenEntry.get(`${g.id}|${entryId}`);
    return {
      gen: g.id, promoted: g.promoted, parent: g.parent,
      loss: r && svg.isNum(r.drift_loss) ? r.drift_loss : NaN,
      pass: r ? r.pass_fail : null,
      timeout: r ? !!r.wall_clock_budget_exceeded : false,
      runId: r ? r.run_id : null,
      ran: !!r,
    };
  });
  const champRow = rows.find((r) => r.gen === championId);
  const champLoss = champRow && svg.isNum(champRow.loss) ? champRow.loss : null;

  // INLINE side-by-side transcript (fix #5): champion (left) vs a challenger
  // (right). The challenger defaults to the worst-scoring candidate that ran
  // (most instructive contrast); a row click swaps it via ?gen on the route is
  // not used — instead we keep both transcripts on THIS page.
  const ranNonChamp = rows.filter((r) => r.ran && r.gen !== championId);
  const cmpGen = (params && params.cmp && rows.find((r) => r.gen === params.cmp && r.ran))
    ? params.cmp
    : (ranNonChamp.slice().sort((a, b) => (svg.isNum(b.loss) ? b.loss : -1) - (svg.isNum(a.loss) ? a.loss : -1))[0] || {}).gen || null;

  const champConvRow = champRow && champRow.ran ? champRow : null;
  const cmpRow = cmpGen ? rows.find((r) => r.gen === cmpGen) : null;
  const [champConv, cmpConv] = await Promise.all([
    champConvRow && champConvRow.runId ? D.conversation(champConvRow.runId) : Promise.resolve(null),
    cmpRow && cmpRow.runId ? D.conversation(cmpRow.runId) : Promise.resolve(null),
  ]);

  const digest = JSON.stringify({
    mode: 'entry', epochId, entryId, champ: championId, cmp: cmpGen,
    def: def ? [def.kind, def.weight, def.budget_s] : null,
    rows: rows.map((r) => [r.gen, svg.isNum(r.loss) ? r.loss.toFixed(3) : null, r.pass, r.timeout, r.promoted]),
    champTurns: convDigest(champConv),
    cmpTurns: convDigest(cmpConv),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dq-pagehead' }, [
      el('h1', { class: 'dq-h1', text: 'Board · ' + entryId }),
      el('p', { class: 'dq-lede', text: 'How every candidate performed on this one board entry — and the two transcripts side by side, inline. Lower drift loss is better.' }),
    ]));

    nodes.push(el('div', { class: 'dq-panel dq-row' }, [
      stat(def ? (KIND_LABEL[def.kind] || def.kind || '—') : '—', 'kind'),
      stat(def && svg.isNum(def.weight) ? svg.fmt(def.weight, 1) : '—', 'weight'),
      stat(def && svg.isNum(def.budget_s) ? def.budget_s + 's' : '—', 'budget'),
      stat(String(rows.filter((r) => r.ran).length) + '/' + String(rows.length), 'candidates ran'),
    ]));
    if (def && def.input_preview) {
      nodes.push(el('div', { class: 'dq-panel' }, [
        el('div', { class: 'dq-faint', style: 'font-size:10px;text-transform:uppercase;letter-spacing:0.06em;', text: 'input preview' }),
        el('div', { style: 'margin-top:6px;line-height:1.5;', text: '“' + def.input_preview + '”' }),
      ]));
    }

    // sorted comparative dot-plot — clicking a row sets the inline cmp.
    const scoreCard = el('div', { class: 'dq-panel' });
    const items = rows
      .filter((r) => svg.isNum(r.loss))
      .sort((a, b) => b.loss - a.loss)
      .map((r) => ({ label: r.gen + (r.promoted ? ' ♛' : ''), value: r.loss, id: r.gen, pass: r.pass, timeout: r.timeout }));
    if (items.length) {
      scoreCard.appendChild(svg.valueDotPlot({
        width: 560, rowHeight: 24, labelWidth: 150, items,
        reference: champLoss != null ? { value: champLoss, label: `champion ${championId}` } : null,
        onClick: (it) => ctx.navigate('board', { epochId, entry: entryId, cmp: it.id }),
      }));
      scoreCard.appendChild(el('div', { class: 'dq-legend' }, [
        champLoss != null ? el('span', null, [el('i', { class: 'spine', style: 'border-color:var(--v2-ink-faint);border-top-style:dashed;' }), `champion ${championId} = ${svg.fmt(champLoss, 1)}`]) : null,
        el('span', null, [el('i', { class: 'dotact' }), 'pass']),
        el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'fail']),
        el('span', { class: 'dq-faint', text: '⏱ timeout · click a candidate → load its transcript beside the champion’s' }),
      ].filter(Boolean)));
    } else {
      scoreCard.appendChild(empty('No candidate has a scored run for this entry yet.'));
    }
    nodes.push(section('Per-candidate loss · sorted worst-first, vs champion', scoreCard));

    // tabular breakdown.
    const tblCard = el('div', { class: 'dq-panel' });
    const tbl = el('table', { class: 'dq-board-table' });
    tbl.appendChild(el('thead', null, [el('tr', null, [
      el('th', { text: 'candidate' }), el('th', { class: 'dq-num', text: 'drift loss' }),
      el('th', { text: 'predicate' }), el('th', { text: 'budget' }), el('th', { text: 'transcript' }),
    ])]));
    const tbody = el('tbody');
    for (const r of rows.slice().sort((a, b) => (svg.isNum(b.loss) ? b.loss : -1) - (svg.isNum(a.loss) ? a.loss : -1))) {
      const tr = el('tr', { class: (r.promoted ? 'dq-board-champ' : '') + (r.gen === cmpGen ? ' dq-board-cmp' : '') }, [
        el('td', { class: 'dq-mono', text: r.gen + (r.promoted ? ' ♛' : '') }),
        el('td', { class: 'dq-num dq-mono', text: svg.isNum(r.loss) ? svg.fmt(r.loss, 1) : '—' }),
        el('td', { class: passClass(r.pass), text: passLabel(r.pass) }),
        el('td', { class: 'dq-mono', text: r.timeout ? 'timed out' : 'ok' }),
        el('td', null, [r.ran
          ? el('a', { class: 'dq-linkbtn dq-board-run', href: ctx.href('board', { epochId, entry: entryId, cmp: r.gen }), text: 'compare inline →' })
          : el('span', { class: 'dq-faint', text: 'no run' })]),
      ]);
      tbody.appendChild(tr);
    }
    tbl.appendChild(tbody);
    tblCard.appendChild(tbl);
    nodes.push(section('Breakdown · pick a candidate to compare', tblCard));

    // FIX #5 — the INLINE side-by-side transcript (no navigation away).
    nodes.push(section('Transcripts · side by side, inline',
      inlineTranscripts(championId, champConvRow, champConv, cmpGen, cmpRow, cmpConv)));
    return nodes;
  });
}

function inlineTranscripts(champId, champRow, champConv, cmpId, cmpRow, cmpConv) {
  const card = el('div', { class: 'dq-panel' });
  card.appendChild(el('div', { class: 'dq-xscript-grid' }, [
    transcriptColumn(champId + (champId ? ' ♛' : ''), champRow, champConv, 'champion'),
    transcriptColumn(cmpId || '—', cmpRow, cmpConv, 'challenger'),
  ]));
  card.appendChild(el('p', { class: 'dq-faint', style: 'font-size:11px;margin:10px 0 0;', text: 'two candidates’ runs on THIS board, side by side — no navigation away (fix #5) · click a candidate in the table above to swap the right column' }));
  return card;
}

function transcriptColumn(label, row, conv, role) {
  const col = el('div', { class: 'dq-xscript-col dq-xscript-' + role });
  col.appendChild(el('div', { class: 'dq-xscript-head' }, [
    el('span', { class: 'dq-xscript-role', text: role }),
    el('span', { class: 'dq-mono', text: label }),
    row && svg.isNum(row.loss) ? el('span', { class: 'dq-faint', text: 'loss ' + svg.fmt(row.loss, 1) }) : null,
  ].filter(Boolean)));
  const scroller = el('div', { class: 'dq-transcript' });
  const turns = (conv && Array.isArray(conv.turns)) ? conv.turns : [];
  const anns = (conv && Array.isArray(conv.annotations)) ? conv.annotations : [];
  if (!row || !row.ran) {
    scroller.appendChild(el('p', { class: 'dq-empty', text: 'no run on this board' }));
  } else if (conv && conv.error) {
    scroller.appendChild(el('p', { class: 'dq-empty', text: conv.error }));
  } else if (!turns.length) {
    scroller.appendChild(el('p', { class: 'dq-empty', text: 'transcript unavailable (no turns reconstructed)' }));
  } else {
    const annBySeq = new Map();
    for (const a of anns) { const k = a.anchor_seq; if (!annBySeq.has(k)) annBySeq.set(k, []); annBySeq.get(k).push(a); }
    for (const t of turns) {
      const turn = el('div', { class: 'dq-turn dq-turn-' + (t.role || 'agent') }, [
        el('div', { class: 'dq-turn-head dq-faint dq-mono' }, [
          el('span', { text: t.agent || t.role || 'turn' }),
          t.kind ? el('span', { text: ' · ' + t.kind }) : null,
        ].filter(Boolean)),
        t.text ? el('div', { class: 'dq-turn-text', text: t.text }) : null,
      ].filter(Boolean));
      if (Array.isArray(t.tool_calls)) for (const tc of t.tool_calls) {
        turn.appendChild(el('div', { class: 'dq-tool dq-mono', text: '⚙ ' + (tc.name || tc.tool || 'tool') }));
      }
      for (const a of (annBySeq.get(t.seq) || [])) {
        turn.appendChild(el('div', { class: 'dq-annot dq-annot-' + (a.kind || 'note'), text: '◂ ' + (a.summary || a.kind) }));
      }
      scroller.appendChild(turn);
    }
  }
  col.appendChild(scroller);
  return col;
}

function boardTrellis(board, gens, rowByGenEntry, domain, ctx, epochId) {
  if (!board.length) return el('div', { class: 'dq-panel' }, [empty('No board entries recorded.')]);
  const sorted = board.slice().sort((a, b) => {
    const ka = KIND_ORDER[a.kind] ?? 9; const kb = KIND_ORDER[b.kind] ?? 9;
    if (ka !== kb) return ka - kb;
    const wa = svg.isNum(a.weight) ? a.weight : 0; const wb = svg.isNum(b.weight) ? b.weight : 0;
    if (wa !== wb) return wb - wa;
    return String(a.entry_id || a.id).localeCompare(String(b.entry_id || b.id));
  });
  const trellis = el('div', { class: 'dq-trellis' });
  const genIds = gens.map((g) => g.id);
  for (const b of sorted) {
    const eid = b.entry_id || b.id;
    const bars = genIds.map((g) => {
      const r = rowByGenEntry.get(`${g}|${eid}`);
      return { label: g, value: r && svg.isNum(r.drift_loss) ? r.drift_loss : NaN, fail: r ? r.pass_fail === 0 : false, timeout: r ? !!r.wall_clock_budget_exceeded : false };
    });
    const cells = genIds.map((g) => {
      const r = rowByGenEntry.get(`${g}|${eid}`);
      return r ? { label: g, pass: r.pass_fail, timeout: !!r.wall_clock_budget_exceeded, ran: true } : { label: g, ran: false };
    });
    const cell = el('figure', { class: 'dq-trellis-cell' }, [
      el('figcaption', { class: 'dq-trellis-cap' }, [
        el('span', { class: 'dq-trellis-id', text: String(eid) }),
        el('span', { class: 'dq-trellis-meta' }, [
          el('span', { class: 'dq-kind-tag dq-kind-' + (b.kind || 'unknown'), text: KIND_LABEL[b.kind] || b.kind || '—' }),
          b.expectation_kind ? el('span', { class: 'dq-faint', text: ' · ' + b.expectation_kind }) : null,
        ].filter(Boolean)),
      ]),
      svg.sparkbar({ width: 210, height: 44, bars, domain: domain || undefined }),
      svg.genDots({ width: 210, height: 14, cells }),
      el('div', { class: 'dq-trellis-foot dq-faint' }, [
        el('span', { text: svg.isNum(b.budget_s) ? `${b.budget_s}s budget` : 'no budget' }),
        el('span', { text: svg.isNum(b.weight) ? `w ${svg.fmt(b.weight, 1)}` : 'w —' }),
        Array.isArray(b.tags) && b.tags.length ? el('span', { class: 'dq-trellis-tags', text: b.tags.slice(0, 3).join(' ') }) : null,
      ].filter(Boolean)),
      b.input_preview ? el('div', { class: 'dq-trellis-preview dq-faint', title: b.input_preview, text: '“' + (b.input_preview.length > 64 ? b.input_preview.slice(0, 63) + '…' : b.input_preview) + '”' }) : null,
    ].filter(Boolean));
    cell.style.cursor = 'pointer';
    cell.addEventListener('click', () => ctx.navigate('board', { epochId, entry: eid }));
    trellis.appendChild(cell);
  }
  const card = el('div', { class: 'dq-panel' });
  card.appendChild(trellis);
  card.appendChild(el('div', { class: 'dq-legend' }, [
    el('span', null, [el('i', { class: 'spine' }), 'one bar per candidate · drift loss (shared scale)']),
    el('span', null, [el('i', { class: 'dotact' }), 'pass']),
    el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'fail']),
    el('span', { class: 'dq-faint', text: '⏱ timeout · click a board → that entry across every candidate + transcripts' }),
  ]));
  return card;
}

function convDigest(conv) {
  if (!conv) return null;
  if (conv.error) return 'err:' + conv.error;
  const turns = Array.isArray(conv.turns) ? conv.turns : [];
  return turns.map((t) => [t.seq, t.role, (t.text || '').length]);
}
function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}
function passClass(pf) {
  if (pf === 1 || pf === true) return 'dq-good-t';
  if (pf === 0 || pf === false) return 'dq-bad-t';
  return 'dq-faint';
}

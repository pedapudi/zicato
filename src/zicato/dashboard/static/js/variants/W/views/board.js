// variants/W/views/board.js — PER-BOARD cross-candidate view + the INLINE
// side-by-side transcript (the carried-forward signature).
//
// First-class from the tree's Boards group. For ONE board entry it shows how
// EVERY candidate performed (sorted comparative dot-plot + a tabular breakdown),
// and — folded in from S — TWO candidates' transcripts on that board shown SIDE
// BY SIDE, INLINE in the detail pane. Selecting runs does NOT navigate away: it
// sets the `runs` route param (URL-encoded, `~runs=A,B`) and the two transcripts
// fill two independently-scrollable columns within this same view. Heatmap cells
// and trellis cards route HERE keyed by entry id (never an arbitrary candidate).
//
// Data: /api/epoch, /api/lineage, /api/generation/{e}/{g}/per-entry pivoted by
// entry_id, /api/conversation/{run_id} per candidate.

import { el } from '../../../core/dom.js';
import * as D from '../../P/data.js';
import * as svg from '../../P/svg.js';
import * as model from '../model.js';
import { gatedSwap, section, empty, stat } from '../ui.js';
import { comparePicker, splitFrame } from '../../S/compare.js';

const KIND_LABEL = {
  single_turn: 'single-turn', multi_turn_scripted: 'scripted multi-turn', multi_turn_emulated: 'emulated multi-turn',
};

export async function render(host, ctx, route) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading board entry…' }));
  const params = (route && route.params) || route || {};
  const entryId = params.entry;

  const [ep, { gens, championId }] = await Promise.all([D.epoch(), model.generations()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Board entry' }), empty('No current epoch.')]);
    return;
  }
  const epochId = params.epochId || ep.epoch_id;
  if (!entryId) {
    gatedSwap(host, 'no-entry', () => [el('h1', { class: 'dn-h1', text: 'Board entry' }),
      empty('No board entry selected — open one from the tree’s Boards group, the trellis, or the epoch heatmap.')]);
    return;
  }
  const board = Array.isArray(ep.board) ? ep.board : [];
  const def = board.find((b) => (b.entry_id || b.id) === entryId) || null;

  // pivot per-entry across every generation for THIS entry.
  const perEntries = await Promise.all(gens.map((g) => D.perEntry(ep.epoch_id, g.id)));
  const rows = [];
  gens.forEach((g, i) => {
    const pe = perEntries[i];
    const r = (pe && Array.isArray(pe.entries)) ? pe.entries.find((e) => e.entry_id === entryId) : null;
    rows.push({
      gen: g.id, promoted: g.promoted, parent: g.parent,
      loss: r && svg.isNum(r.drift_loss) ? r.drift_loss : NaN,
      pass: r ? r.pass_fail : null, timeout: r ? !!r.wall_clock_budget_exceeded : false,
      runId: r ? r.run_id : null, ran: !!r,
    });
  });
  const champRow = rows.find((r) => r.gen === championId);
  const champLoss = champRow && svg.isNum(champRow.loss) ? champRow.loss : null;
  const ranGens = rows.filter((r) => r.ran).map((r) => r.gen);

  // resolve the two transcript SIDES. Default to champion vs the first
  // challenger that ran when no explicit pair is in the route.
  const runSel = (route && route.runs && route.runs.length) ? route.runs.slice(0, 2) : null;
  let genA = null; let genB = null;
  if (runSel) { genA = runSel[0] || null; genB = runSel[1] || null; }
  if (!genA) genA = (championId && ranGens.includes(championId)) ? championId : (ranGens[0] || null);
  if (!genB) genB = ranGens.find((g) => g !== genA) || null;

  const rowByGen = new Map(rows.map((r) => [r.gen, r]));
  const convA = genA && rowByGen.get(genA) && rowByGen.get(genA).runId ? await D.conversation(rowByGen.get(genA).runId) : null;
  const convB = genB && rowByGen.get(genB) && rowByGen.get(genB).runId ? await D.conversation(rowByGen.get(genB).runId) : null;

  const digest = JSON.stringify({
    epochId, entryId, def: def ? [def.kind, def.weight, def.budget_s] : null, champ: championId,
    rows: rows.map((r) => [r.gen, svg.isNum(r.loss) ? r.loss.toFixed(3) : null, r.pass, r.timeout, r.promoted, r.runId]),
    genA, genB, convA: convDigest(convA), convB: convDigest(convB),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: 'Board · ' + entryId }),
      el('p', { class: 'dn-lede', text: 'How every candidate performed on this one board entry — then read two candidates’ transcripts on it side by side, inline below.' }),
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

    // sorted comparative dot-plot, worst-first, champion reference rule.
    const scoreCard = el('div', { class: 'dn-panel' });
    const items = rows.filter((r) => svg.isNum(r.loss)).sort((a, b) => b.loss - a.loss)
      .map((r) => ({ label: r.gen + (r.promoted ? ' ♛' : ''), value: r.loss, id: r.gen, pass: r.pass, timeout: r.timeout }));
    if (items.length) {
      scoreCard.appendChild(svg.valueDotPlot({
        width: 560, rowHeight: 22, labelWidth: 150, items,
        reference: champLoss != null ? { value: champLoss, label: `champion ${championId}` } : null,
        onClick: (it) => ctx.navigate('board', { epochId, entry: entryId }, { runs: [it.id, genB && genB !== it.id ? genB : (genA !== it.id ? genA : null)].filter(Boolean) }),
      }));
      scoreCard.appendChild(el('div', { class: 'dn-legend' }, [
        champLoss != null ? el('span', null, [el('i', { class: 'spine', style: 'border-color:var(--v2-ink-faint);border-top-style:dashed;' }), `champion ${championId} = ${svg.fmt(champLoss, 1)}`]) : null,
        el('span', null, [el('i', { class: 'dotact' }), 'pass']),
        el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'fail']),
        el('span', { class: 'dn-faint', text: '⏱ timeout · click a candidate → its transcript, side by side below' }),
      ].filter(Boolean)));
    } else {
      scoreCard.appendChild(empty('No candidate has a scored run for this entry yet.'));
    }
    nodes.push(section('Per-candidate loss · sorted worst-first, vs champion', scoreCard));

    // the board TRELLIS small-multiple for THIS entry (trellis lives in Boards).
    nodes.push(section('Trellis · drift loss across candidates (this entry)', trellisCell(def, entryId, rows)));

    // tabular breakdown — rows set the inline transcript (no route away).
    const tblCard = el('div', { class: 'dn-panel' });
    const tbl = el('table', { class: 'dn-board-table' });
    tbl.appendChild(el('thead', null, [el('tr', null, [
      el('th', { text: 'candidate' }), el('th', { class: 'dn-num', text: 'drift loss' }),
      el('th', { text: 'predicate' }), el('th', { text: 'budget' }), el('th', { text: 'transcript' }),
    ])]));
    const tbody = el('tbody');
    for (const r of rows.slice().sort((a, b) => (svg.isNum(b.loss) ? b.loss : -1) - (svg.isNum(a.loss) ? a.loss : -1))) {
      const isSel = r.gen === genA || r.gen === genB;
      const targetRuns = [r.gen, genB && genB !== r.gen ? genB : (genA !== r.gen ? genA : null)].filter(Boolean);
      const setRun = el('a', { class: 'dn-linkbtn dn-board-run' + (isSel ? ' dn-linkbtn-on' : ''),
        href: ctx.href('board', { epochId, entry: entryId }, { runs: targetRuns }),
        text: isSel ? 'showing ↓' : 'show inline →',
        onclick: (ev) => { ev.preventDefault(); ctx.navigate('board', { epochId, entry: entryId }, { runs: targetRuns }); } });
      tbody.appendChild(el('tr', { class: (r.promoted ? 'dn-board-champ' : '') + (isSel ? ' dn-board-sel' : '') }, [
        el('td', { class: 'dn-mono', text: r.gen + (r.promoted ? ' ♛' : '') }),
        el('td', { class: 'dn-num dn-mono', text: svg.isNum(r.loss) ? svg.fmt(r.loss, 1) : '—' }),
        el('td', { class: passClass(r.pass), text: passLabel(r.pass) }),
        el('td', { class: 'dn-mono', text: r.timeout ? 'timed out' : 'ok' }),
        el('td', null, [r.ran ? setRun : el('span', { class: 'dn-faint', text: 'no run' })]),
      ]));
    }
    tbl.appendChild(tbody);
    tblCard.appendChild(tbl);
    nodes.push(section('Breakdown · pick two candidates to compare transcripts', tblCard));

    // the INLINE side-by-side transcripts.
    const cmpBar = el('div', { class: 'vs-cmp-bar' }, [
      comparePicker({
        label: 'side A', noneLabel: '— pick A —',
        options: ranGens.map((g) => ({ id: g, label: g + (g === championId ? ' ♛' : '') })),
        current: null, value: genA,
        onChange: (v) => ctx.navigate('board', { epochId, entry: entryId }, { runs: [v, genB].filter(Boolean) }),
      }),
      comparePicker({
        label: 'side B', noneLabel: '— pick B —',
        options: ranGens.map((g) => ({ id: g, label: g + (g === championId ? ' ♛' : '') })),
        current: null, value: genB,
        onChange: (v) => ctx.navigate('board', { epochId, entry: entryId }, { runs: [genA, v].filter(Boolean) }),
      }),
    ]);
    const split = splitFrame({
      a: { title: genA ? genA + (genA === championId ? ' ♛' : '') : 'side A', sub: 'transcript',
        build: (h) => transcriptInto(h, convA, rowByGen.get(genA)) },
      b: genB ? { title: genB + (genB === championId ? ' ♛' : ''), sub: 'transcript',
        build: (h) => transcriptInto(h, convB, rowByGen.get(genB)) } : null,
      emptyTitle: 'side B',
      emptyPrompt: 'Pick a second candidate (above, or from the breakdown) to read both transcripts on this board side by side.',
    });
    const transWrap = el('div', { class: 'vs-transcripts' }, [cmpBar, split]);
    nodes.push(section('Transcripts · side by side, inline · ' + entryId, transWrap));
    return nodes;
  });
}

function transcriptInto(host, conv, row) {
  if (!row || !row.runId) { host.appendChild(empty('No run id for this candidate on this entry.')); return; }
  if (conv && conv.error) { host.appendChild(empty(conv.error)); return; }
  if (!conv) { host.appendChild(empty('Transcript unavailable (the conversation could not be reconstructed).')); return; }
  const turns = Array.isArray(conv.turns) ? conv.turns : [];
  const anns = Array.isArray(conv.annotations) ? conv.annotations : [];
  if (!turns.length) { host.appendChild(empty('No turns reconstructed for this run.')); return; }

  host.appendChild(el('div', { class: 'dn-row', style: 'margin-bottom:8px;gap:18px;' }, [
    stat(svg.isNum(row.loss) ? svg.fmt(row.loss, 1) : '—', 'drift loss'),
    stat(passLabel(row.pass), 'predicate'),
    stat(row.runId ? row.runId.slice(0, 10) + '…' : '—', 'run'),
  ]));

  const annBySeq = new Map();
  for (const a of anns) { const k = a.anchor_seq; if (!annBySeq.has(k)) annBySeq.set(k, []); annBySeq.get(k).push(a); }

  const scroller = el('div', { class: 'dn-transcript vs-transcript' });
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
  host.appendChild(scroller);
}

function trellisCell(def, entryId, rows) {
  const card = el('div', { class: 'dn-panel' });
  const trellis = el('div', { class: 'dn-trellis' });
  const bars = rows.map((r) => ({ label: r.gen, value: svg.isNum(r.loss) ? r.loss : NaN, fail: r.pass === 0, timeout: r.timeout }));
  const cells = rows.map((r) => (r.ran ? { label: r.gen, pass: r.pass, timeout: r.timeout, ran: true } : { label: r.gen, ran: false }));
  const finite = bars.map((b) => b.value).filter(svg.isNum);
  const domain = finite.length ? svg.extent(finite) : undefined;
  const cell = el('figure', { class: 'dn-trellis-cell' }, [
    el('figcaption', { class: 'dn-trellis-cap' }, [
      el('span', { class: 'dn-trellis-id', text: String(entryId) }),
      def ? el('span', { class: 'dn-trellis-meta' }, [
        el('span', { class: 'dn-kind-tag dn-kind-' + (def.kind || 'unknown'), text: KIND_LABEL[def.kind] || def.kind || '—' }),
      ]) : null,
    ].filter(Boolean)),
    svg.sparkbar({ width: 240, height: 44, bars, domain }),
    svg.genDots({ width: 240, height: 14, cells }),
  ]);
  trellis.appendChild(cell);
  card.appendChild(trellis);
  card.appendChild(el('div', { class: 'dn-legend' }, [
    el('span', null, [el('i', { class: 'spine' }), 'one bar per candidate · drift loss']),
    el('span', null, [el('i', { class: 'dotact' }), 'pass']),
    el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'fail']),
    el('span', { class: 'dn-faint', text: '⏱ timeout · the trellis lives here in the Boards view' }),
  ]));
  return card;
}

function convDigest(conv) {
  if (!conv) return null;
  if (conv.error) return { err: conv.error };
  const turns = Array.isArray(conv.turns) ? conv.turns : [];
  return turns.map((t) => [t.seq, t.role, t.agent, (t.text || '').length, Array.isArray(t.tool_calls) ? t.tool_calls.length : 0]);
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

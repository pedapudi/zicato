// variants/R/views/board.js — per-board cross-candidate detail (FIX #4).
//
// One board ENTRY, every candidate — reached by selecting an entry in the
// third Miller column (boards are first-class). It carries:
//   * the per-candidate drift-loss dot-plot (champion reference rule) + a
//     tabular breakdown;
//   * the per-candidate TRELLIS small-multiple (FIX #6: the trellis lives HERE,
//     the heatmap stays at the epoch overview — never both on one page);
//   * an INLINE SIDE-BY-SIDE transcript (FIX #5): selecting a candidate's run
//     renders TWO transcripts on this board side by side — the selected
//     candidate against the champion — IN THIS pane, no navigation away.
//
// Bind: /api/epoch (board defs), /api/generation/{e}/{g}/per-entry pivoted by
// entry, /api/conversation/{run_id} per candidate.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, normaliseDecision } from '../ui.js';

const KIND_LABEL = { single_turn: 'single-turn', multi_turn_scripted: 'scripted multi-turn', multi_turn_emulated: 'emulated multi-turn' };

export async function render(host, ctx, path) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dr-empty', text: 'Reading board entry…' }));
  const entryId = path && path.entry;
  const runGen = path && path.runGen;

  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dr-h1', text: 'Board entry' }), empty('No current epoch.')]);
    return;
  }
  if (!entryId) {
    gatedSwap(host, 'no-entry', () => [el('h1', { class: 'dr-h1', text: 'Board entry' }), empty('No board entry selected — pick one from the Boards column.')]);
    return;
  }
  const epochId = ep.epoch_id;
  const board = Array.isArray(ep.board) ? ep.board : [];
  const def = board.find((b) => (b.entry_id || b.id) === entryId) || null;

  const genList = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : (Array.isArray(ep.experiments) ? ep.experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' })) : []);

  const perEntries = await Promise.all(genList.map((g) => D.perEntry(epochId, g.id)));
  const rows = [];
  genList.forEach((g, i) => {
    const pe = perEntries[i];
    const r = (pe && Array.isArray(pe.entries)) ? pe.entries.find((e) => e.entry_id === entryId) : null;
    rows.push({
      gen: g.id, promoted: g.promoted, parent: g.parent,
      loss: r && svg.isNum(r.drift_loss) ? r.drift_loss : NaN,
      pass: r ? r.pass_fail : null, timeout: r ? !!r.wall_clock_budget_exceeded : false,
      runId: r ? r.run_id : null, ran: !!r,
    });
  });
  const champ = genList.find((g) => g.promoted) || genList.find((g) => !g.parent) || null;
  const championId = champ ? champ.id : null;
  const champRow = rows.find((r) => r.gen === championId);
  const champLoss = champRow && svg.isNum(champRow.loss) ? champRow.loss : null;
  const domain = svg.extent(rows.map((r) => r.loss));

  // INLINE side-by-side transcript (FIX #5): the selected candidate vs the
  // champion, both transcripts on THIS board, fetched per run_id.
  let leftConv = null, rightConv = null, leftRun = null, rightRun = null;
  if (runGen) {
    const rightRow = rows.find((r) => r.gen === runGen) || null;
    const leftRow = (championId && championId !== runGen) ? rows.find((r) => r.gen === championId) : null;
    rightRun = rightRow ? rightRow.runId : null;
    leftRun = leftRow ? leftRow.runId : null;
    [leftConv, rightConv] = await Promise.all([
      leftRun ? D.conversation(leftRun) : Promise.resolve(null),
      rightRun ? D.conversation(rightRun) : Promise.resolve(null),
    ]);
  }

  const digest = JSON.stringify({
    epochId, entryId, def: def ? [def.kind, def.weight, def.budget_s] : null, champ: championId,
    rows: rows.map((r) => [r.gen, svg.isNum(r.loss) ? r.loss.toFixed(3) : null, r.pass, r.timeout, r.promoted]),
    runGen: runGen || null,
    left: leftConv && Array.isArray(leftConv.turns) ? leftConv.turns.map((t) => [t.seq, t.role, (t.text || '').length]) : null,
    right: rightConv && Array.isArray(rightConv.turns) ? rightConv.turns.map((t) => [t.seq, t.role, (t.text || '').length]) : null,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dr-pagehead' }, [
      el('h1', { class: 'dr-h1', text: 'Board · ' + entryId }),
      el('p', { class: 'dr-lede', text: 'How every candidate performed on this one board entry — lower drift loss is better.' }),
    ]));

    nodes.push(el('div', { class: 'dr-panel dr-row' }, [
      stat(def ? (KIND_LABEL[def.kind] || def.kind || '—') : '—', 'kind'),
      stat(def && svg.isNum(def.weight) ? svg.fmt(def.weight, 1) : '—', 'weight'),
      stat(def && svg.isNum(def.budget_s) ? def.budget_s + 's' : '—', 'budget'),
      stat(String(rows.filter((r) => r.ran).length) + '/' + String(rows.length), 'candidates ran'),
    ]));
    if (def && def.input_preview) {
      nodes.push(el('div', { class: 'dr-panel' }, [
        el('div', { class: 'dr-faint', style: 'font-size:10px;text-transform:uppercase;letter-spacing:0.06em;', text: 'input preview' }),
        el('div', { style: 'margin-top:4px;line-height:1.4;', text: '“' + def.input_preview + '”' }),
      ]));
    }

    // per-candidate dot-plot
    const scoreCard = el('div', { class: 'dr-panel' });
    const items = rows.filter((r) => svg.isNum(r.loss)).sort((a, b) => b.loss - a.loss)
      .map((r) => ({ label: r.gen + (r.promoted ? ' ♛' : ''), value: r.loss, id: r.gen, pass: r.pass, timeout: r.timeout }));
    if (items.length) {
      scoreCard.appendChild(svg.valueDotPlot({
        width: 560, rowHeight: 22, labelWidth: 140, items,
        reference: champLoss != null ? { value: champLoss, label: `champion ${championId}` } : null,
        onClick: (it) => ctx.navigate({ section: 'boards', entry: entryId, runGen: it.id }),
      }));
      scoreCard.appendChild(el('p', { class: 'dr-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'click a candidate → its run, shown inline side-by-side against the champion below' }));
    } else {
      scoreCard.appendChild(empty('No candidate has a scored run for this entry yet.'));
    }
    nodes.push(section('Per-candidate loss · sorted worst-first, vs champion', scoreCard));

    // the TRELLIS small-multiple lives HERE (FIX #6) — one bar per candidate.
    nodes.push(section('Trellis · this board across every candidate', trellisCard(rows, domain, def)));

    // tabular breakdown
    const tblCard = el('div', { class: 'dr-panel' });
    const tbl = el('table', { class: 'dr-board-table' });
    tbl.appendChild(el('thead', null, [el('tr', null, [
      el('th', { text: 'candidate' }), el('th', { class: 'dr-num', text: 'drift loss' }),
      el('th', { text: 'predicate' }), el('th', { text: 'budget' }), el('th', { text: 'run' }),
    ])]));
    const tbody = el('tbody');
    for (const r of rows.slice().sort((a, b) => (svg.isNum(b.loss) ? b.loss : -1) - (svg.isNum(a.loss) ? a.loss : -1))) {
      const runBtn = r.ran
        ? el('button', { class: 'dr-linkbtn dr-board-run', type: 'button', onClick: () => ctx.navigate({ section: 'boards', entry: entryId, runGen: r.gen }), text: 'inline transcript →' })
        : el('span', { class: 'dr-faint', text: 'no run' });
      tbody.appendChild(el('tr', { class: (r.promoted ? 'dr-board-champ ' : '') + (r.gen === runGen ? 'dr-board-active' : '') }, [
        el('td', { class: 'dr-mono', text: r.gen + (r.promoted ? ' ♛' : '') }),
        el('td', { class: 'dr-num dr-mono', text: svg.isNum(r.loss) ? svg.fmt(r.loss, 1) : '—' }),
        el('td', { class: passClass(r.pass), text: passLabel(r.pass) }),
        el('td', { class: 'dr-mono', text: r.timeout ? 'timed out' : 'ok' }),
        el('td', null, [runBtn]),
      ]));
    }
    tbl.appendChild(tbody);
    tblCard.appendChild(tbl);
    nodes.push(section('Breakdown · drill to each run', tblCard));

    // INLINE side-by-side transcript (FIX #5)
    if (runGen) {
      nodes.push(section('Transcripts · side-by-side on this board (inline)', transcriptCompare({
        entryId, leftGen: (championId && championId !== runGen) ? championId : null, rightGen: runGen,
        leftConv, rightConv, leftRun, rightRun,
      })));
    }
    return nodes;
  });
}

function trellisCard(rows, domain, def) {
  const card = el('div', { class: 'dr-panel' });
  const bars = rows.map((r) => ({ label: r.gen, value: svg.isNum(r.loss) ? r.loss : NaN, fail: r.pass === 0, timeout: r.timeout }));
  const cells = rows.map((r) => (r.ran ? { label: r.gen, pass: r.pass, timeout: r.timeout, ran: true } : { label: r.gen, ran: false }));
  const fig = el('figure', { class: 'dr-trellis-cell' }, [
    el('figcaption', { class: 'dr-trellis-cap' }, [
      el('span', { class: 'dr-trellis-id', text: def ? (KIND_LABEL[def.kind] || def.kind || 'board') : 'board' }),
    ]),
    svg.sparkbar({ width: 320, height: 52, bars, domain: domain || undefined }),
    svg.genDots({ width: 320, height: 16, cells }),
  ]);
  card.appendChild(fig);
  card.appendChild(el('p', { class: 'dr-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'one bar per candidate (shared loss scale) · glyph row = pass / fail / ⏱ timeout · the heatmap lives at the epoch overview (de-dup)' }));
  return card;
}

function transcriptCompare(o) {
  const card = el('div', { class: 'dr-panel dr-xscript' });
  const cols = el('div', { class: 'dr-xscript-cols' });
  cols.appendChild(transcriptColumn(o.leftGen, o.leftConv, o.leftRun, 'champion'));
  cols.appendChild(transcriptColumn(o.rightGen, o.rightConv, o.rightRun, 'selected'));
  card.appendChild(cols);
  card.appendChild(el('p', { class: 'dr-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'both transcripts reconstructed from /api/conversation · rendered inline — no navigation away' }));
  return card;
}

function transcriptColumn(gen, conv, runId, role) {
  const col = el('div', { class: 'dr-xscript-col' });
  col.appendChild(el('div', { class: 'dr-xscript-head' }, [
    el('span', { class: 'dr-mono', text: gen || '(no champion)' }),
    el('span', { class: 'dr-faint', style: 'font-size:10px;', text: role }),
    runId ? el('span', { class: 'dr-faint dr-mono', style: 'font-size:9.5px;', text: runId.slice(0, 10) + '…' }) : null,
  ].filter(Boolean)));
  if (!gen) { col.appendChild(empty('No champion distinct from the selected candidate (this IS the champion).')); return col; }
  if (!runId) { col.appendChild(empty('No run for this candidate on this entry.')); return col; }
  if (!conv) { col.appendChild(empty('Transcript unavailable.')); return col; }
  if (conv.error) { col.appendChild(empty(conv.error)); return col; }
  const turns = Array.isArray(conv.turns) ? conv.turns : [];
  const anns = Array.isArray(conv.annotations) ? conv.annotations : [];
  if (!turns.length) { col.appendChild(empty('No turns reconstructed.')); return col; }
  const annBySeq = new Map();
  for (const a of anns) { const k = a.anchor_seq; if (!annBySeq.has(k)) annBySeq.set(k, []); annBySeq.get(k).push(a); }
  const scroller = el('div', { class: 'dr-transcript' });
  for (const t of turns) {
    const turn = el('div', { class: 'dr-turn dr-turn-' + (t.role || 'agent') }, [
      el('div', { class: 'dr-turn-head dr-faint dr-mono' }, [
        el('span', { text: t.agent || t.role || 'turn' }),
        t.kind ? el('span', { text: ' · ' + t.kind }) : null,
      ].filter(Boolean)),
      t.text ? el('div', { class: 'dr-turn-text', text: t.text }) : null,
    ].filter(Boolean));
    if (Array.isArray(t.tool_calls)) for (const tc of t.tool_calls) turn.appendChild(el('div', { class: 'dr-tool dr-mono', text: '⚙ ' + (tc.name || tc.tool || 'tool') }));
    for (const a of (annBySeq.get(t.seq) || [])) turn.appendChild(el('div', { class: 'dr-annot dr-annot-' + (a.kind || 'note'), text: '◂ ' + (a.summary || a.kind) }));
    scroller.appendChild(turn);
  }
  col.appendChild(scroller);
  return col;
}

function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}
function passClass(pf) {
  if (pf === 1 || pf === true) return 'dr-good-t';
  if (pf === 0 || pf === false) return 'dr-bad-t';
  return 'dr-faint';
}

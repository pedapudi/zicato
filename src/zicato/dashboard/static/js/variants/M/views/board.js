// variants/M/views/board.js — PER-BOARD CROSS-CANDIDATE DETAIL (NEW).
//
// CONVERGENCE-II FIX #7 — a dedicated page for ONE board entry showing how
// EVERY candidate performed on it. The board trellis cards AND the heatmap
// cells route HERE (keyed by entry id), NOT to an arbitrary candidate view (a
// trellis click used to dump the user on candidate v2 with no fidelity).
//
// It pivots /api/generation/{e}/{g}/per-entry by `entry_id` across
// generations: per-candidate loss + pass/fail/timeout, a sorted comparative
// dot-plot, and drill to each candidate's run/transcript for that board.
//
// With no entry selected, it lists the board so the user can pick one.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, normaliseDecision, pageHead, figure } from '../ui.js';

const KIND_LABEL = {
  single_turn: 'single-turn', multi_turn_scripted: 'scripted multi-turn',
  multi_turn_emulated: 'emulated multi-turn',
};

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'd-empty', text: 'Reading the board…' }));

  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [pageHead('Board', 'Board', ''), empty('No current epoch.')]);
    return;
  }
  const epochId = ep.epoch_id;
  const board = Array.isArray(ep.board) ? ep.board : [];
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];

  const gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' }));
  const champ = gens.find((g) => g.promoted) || gens.find((g) => !g.parent) || null;
  const championId = champ ? champ.id : null;

  // pivot per-entry across generations
  const perEntries = await Promise.all(gens.map((g) => D.perEntry(epochId, g.id)));
  const rowByGenEntry = new Map();
  const entryIds = new Set();
  gens.forEach((g, i) => {
    const pe = perEntries[i];
    if (pe && Array.isArray(pe.entries)) for (const r of pe.entries) {
      rowByGenEntry.set(`${g.id}|${r.entry_id}`, r);
      entryIds.add(r.entry_id);
    }
  });
  for (const b of board) { const id = b.entry_id || b.id; if (id) entryIds.add(id); }

  const selEntry = params && params.entry;
  const boardMeta = board.find((b) => (b.entry_id || b.id) === selEntry) || null;

  const digest = JSON.stringify({
    epochId, sel: selEntry || null, championId,
    gens: gens.map((g) => g.id),
    board: board.map((b) => [b.entry_id || b.id, b.kind, b.weight, b.budget_s]),
    rows: selEntry ? gens.map((g) => {
      const r = rowByGenEntry.get(`${g.id}|${selEntry}`);
      return r ? [g.id, svg.isNum(r.drift_loss) ? r.drift_loss.toFixed(3) : null, r.pass_fail, !!r.wall_clock_budget_exceeded] : [g.id, null, null, null];
    }) : [...entryIds].sort(),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    if (!selEntry) {
      nodes.push(pageHead('Board · the field of tests', 'Board',
        'Every board entry the candidates face this epoch. Pick one to see how every candidate fared on it.'));
      nodes.push(boardList(board, [...entryIds].sort(), rowByGenEntry, gens, ctx));
      return nodes;
    }

    nodes.push(pageHead('Board entry · cross-candidate', selEntry,
      boardMeta
        ? [`${KIND_LABEL[boardMeta.kind] || boardMeta.kind || 'entry'} · how every candidate scored on this board entry.`]
        : ['How every candidate scored on this board entry.']));

    // entry contract stats
    if (boardMeta) {
      nodes.push(el('div', { class: 'd-panel d-row' }, [
        stat(KIND_LABEL[boardMeta.kind] || boardMeta.kind || '—', 'kind'),
        stat(svg.isNum(boardMeta.budget_s) ? boardMeta.budget_s + 's' : '—', 'budget'),
        stat(svg.isNum(boardMeta.weight) ? svg.fmt(boardMeta.weight, 1) : '—', 'weight'),
        stat(boardMeta.expectation_kind || 'none', 'expectation'),
      ]));
      if (boardMeta.input_preview) nodes.push(el('blockquote', { class: 'm-pullquote m-pq-hyp' }, [el('p', { class: 'm-pullquote-text', text: '“' + boardMeta.input_preview + '”' })]));
    }

    // per-candidate rows
    const rows = gens.map((g) => {
      const r = rowByGenEntry.get(`${g.id}|${selEntry}`);
      return {
        id: g.id, promoted: g.promoted,
        value: r && svg.isNum(r.drift_loss) ? r.drift_loss : NaN,
        pass: r ? r.pass_fail : null, timeout: r ? !!r.wall_clock_budget_exceeded : false,
        ran: !!r, runId: r ? r.run_id : null,
      };
    });
    const scored = rows.filter((r) => svg.isNum(r.value));

    // comparative dot-plot (sorted worst-first), vs champion's loss here
    const champRow = rows.find((r) => r.id === championId);
    const champLoss = champRow && svg.isNum(champRow.value) ? champRow.value : null;
    const card = el('div', { class: 'd-panel m-fig-md' });
    if (scored.length) {
      const items = scored.slice().sort((a, b) => b.value - a.value).map((r) => ({
        label: r.id + (r.promoted ? ' ♛' : ''), value: r.value, id: r.id,
        pass: r.pass, timeout: r.timeout,
      }));
      const dot = svg.valueDotPlot({
        width: 560, rowHeight: 24, labelWidth: 150, items,
        reference: champLoss != null ? { value: champLoss, label: 'champion ' + championId } : null,
        onClick: (it) => ctx.navigate('run', { gen: it.id, entry: selEntry }),
      });
      card.appendChild(figure(dot,
        (champLoss != null ? `The dashed reference is champion ${championId} = ${svg.fmt(champLoss, 1)}. ` : '')
        + 'Each candidate’s drift loss on this one board entry, sorted worst-first; the trailing glyph reads pass / fail / timeout. Click a candidate to open its run on this board.',
        { label: 'Figure 1.' }));
    } else {
      card.appendChild(empty('No candidate has a scored run for this board entry yet.'));
    }
    nodes.push(section('Per-candidate loss on this entry', card));

    // a tidy table with drill links
    nodes.push(section('Each candidate’s run on ' + selEntry, candidateTable(rows, championId, selEntry, ctx)));
    return nodes;
  });
}

function boardList(board, entryIds, rowByGenEntry, gens, ctx) {
  const ids = board.length ? board.map((b) => b.entry_id || b.id) : entryIds;
  if (!ids.length) return el('div', { class: 'd-panel' }, [empty('No board entries recorded for this epoch.')]);
  const metaById = new Map(board.map((b) => [b.entry_id || b.id, b]));
  const grid = el('div', { class: 'm-board-grid' });
  for (const id of ids) {
    const b = metaById.get(id) || {};
    const ran = gens.some((g) => rowByGenEntry.has(`${g.id}|${id}`));
    grid.appendChild(el('a', { class: 'm-board-card', href: ctx.href('board', { entry: id }) }, [
      el('div', { class: 'm-board-card-id', text: String(id) }),
      el('div', { class: 'm-board-card-meta' }, [
        el('span', { class: 'd-kind-tag d-kind-' + (b.kind || 'unknown'), text: KIND_LABEL[b.kind] || b.kind || '—' }),
        svg.isNum(b.budget_s) ? el('span', { class: 'd-faint', text: ' · ' + b.budget_s + 's' }) : null,
      ].filter(Boolean)),
      el('div', { class: 'd-faint m-board-card-foot', text: ran ? 'scored across candidates →' : 'no runs yet' }),
    ]));
  }
  return el('div', { class: 'd-panel' }, [grid]);
}

function candidateTable(rows, championId, entryId, ctx) {
  const card = el('div', { class: 'd-panel' });
  const table = el('table', { class: 'm-cand-table' });
  table.appendChild(el('thead', null, [el('tr', null, [
    el('th', { text: 'candidate' }), el('th', { text: 'drift loss' }),
    el('th', { text: 'predicate' }), el('th', { text: 'runtime' }), el('th', { text: '' }),
  ])]));
  const tbody = el('tbody');
  for (const r of rows) {
    tbody.appendChild(el('tr', { class: r.id === championId ? 'm-cand-champ' : '' }, [
      el('td', { class: 'd-mono', text: r.id + (r.promoted ? ' ♛' : '') }),
      el('td', { class: 'd-mono', text: svg.isNum(r.value) ? svg.fmt(r.value, 1) : '—' }),
      el('td', null, [passPill(r)]),
      el('td', { class: 'd-mono d-faint', text: r.timeout ? 'timed out' : '—' }),
      el('td', null, [r.ran
        ? el('a', { class: 'm-link', href: ctx.href('run', { gen: r.id, entry: entryId }), text: 'run →' })
        : el('span', { class: 'd-faint', text: 'no run' })]),
    ]));
  }
  table.appendChild(tbody);
  card.appendChild(table);
  return card;
}

function passPill(r) {
  if (!r.ran) return el('span', { class: 'd-pill d-baseline', text: 'no run' });
  if (r.timeout) return el('span', { class: 'd-pill d-deferred', text: 'timeout' });
  if (r.pass === 1 || r.pass === true) return el('span', { class: 'd-pill d-promoted', text: 'pass' });
  if (r.pass === 0 || r.pass === false) return el('span', { class: 'd-pill d-rejected', text: 'fail' });
  return el('span', { class: 'd-pill d-baseline', text: 'no predicate' });
}

// variants/L/views/board.js — BOARD: ONE entry across EVERY candidate (NEW).
//
// The convergence-II view the round-3 variants lacked (fix #7). For a single
// board entry it shows how EVERY candidate performed on it: a per-candidate
// loss + pass/fail/timeout table, a sorted comparative bar chart, and a
// drill into each candidate's run/transcript for that board. Board trellis
// cards AND heatmap cells route HERE, keyed by entry id — never to an
// arbitrary candidate view.
//
// Data: /api/epoch (the board entry's contract), /api/lineage (the candidate
// set), /api/generation/{e}/{g}/per-entry pivoted by entry_id across
// generations, /api/tournaments (the promoted champion for context), drill
// via the per-entry run_id → /api/conversation/{run_id} (the run view).

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, linkButton } from '../ui.js';

const KIND_LABEL = { single_turn: 'single-turn', multi_turn_scripted: 'scripted multi-turn', multi_turn_emulated: 'emulated multi-turn' };

export async function render(host, ctx, params) {
  const entryId = params && params.entry;
  if (!host.firstChild) host.appendChild(el('p', { class: 'vl-empty', text: 'Reading board entry…' }));

  if (!entryId) {
    gatedSwap(host, 'no-board', () => [
      el('h1', { class: 'vl-h1', text: 'Board entry' }),
      empty('No board entry selected — open one from the epoch trellis or the drift heatmap.'),
    ]);
    return;
  }

  const ep = await D.epoch();
  const epochId = (ep && ep.epoch_id) || (state.epochDef && state.epochDef.epoch_id) || null;
  if (!epochId) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'vl-h1', text: 'Board entry' }), empty('No current epoch.')]);
    return;
  }

  const board = Array.isArray(ep.board) ? ep.board : [];
  const meta = board.find((b) => (b.entry_id || b.id) === entryId) || null;

  const lin = await D.lineage(epochId);
  const gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : [];
  const championId = (gens.find((g) => g.promoted) || {}).id || null;

  // Pivot: for THIS entry, the row from every generation's per-entry grid.
  const perEntries = await Promise.all(gens.map((g) => D.perEntry(epochId, g.id)));
  const cand = [];
  gens.forEach((g, i) => {
    const pe = perEntries[i];
    const row = (pe && Array.isArray(pe.entries)) ? pe.entries.find((e) => e.entry_id === entryId) : null;
    cand.push({
      gen: g.id, promoted: g.promoted, champion: g.id === championId,
      drift_loss: row ? row.drift_loss : null,
      pass_fail: row ? row.pass_fail : null,
      timeout: row ? !!row.wall_clock_budget_exceeded : false,
      runtime_ms: row ? row.runtime_ms : null,
      run_id: row ? row.run_id : null,
      ran: !!row,
    });
  });

  const losses = cand.map((c) => c.drift_loss).filter(svg.isNum);
  const bestLoss = losses.length ? Math.min(...losses) : null;

  const digest = JSON.stringify({
    entryId, epochId, championId,
    meta: meta ? [meta.kind, meta.expectation_kind, meta.budget_s, meta.weight] : null,
    cand: cand.map((c) => [c.gen, svg.isNum(c.drift_loss) ? c.drift_loss.toFixed(3) : null, c.pass_fail, c.timeout, c.ran]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'vl-pagehead' }, [
      el('h1', { class: 'vl-h1', text: 'Board · ' + entryId }),
      el('p', { class: 'vl-lede', text: 'How every candidate performed on this single board entry — drift loss, pass/fail/timeout, and a drill into each run.' }),
    ]));

    // contract summary
    nodes.push(el('div', { class: 'vl-panel vl-row' }, [
      stat(meta ? (KIND_LABEL[meta.kind] || meta.kind || '—') : '—', 'kind'),
      stat(meta && meta.expectation_kind ? meta.expectation_kind : 'none', 'expectation'),
      stat(meta && svg.isNum(meta.budget_s) ? `${meta.budget_s}s` : '—', 'budget'),
      stat(meta && svg.isNum(meta.weight) ? svg.fmt(meta.weight, 1) : '—', 'weight'),
    ]));
    if (meta && meta.input_preview) {
      nodes.push(el('div', { class: 'vl-panel vl-board-input vl-faint', text: '“' + meta.input_preview + '”' }));
    }

    // comparative bar chart (sorted, best-first)
    const chartCard = el('div', { class: 'vl-panel' });
    const items = cand.slice().sort((a, b) => (svg.isNum(a.drift_loss) ? a.drift_loss : 1e9) - (svg.isNum(b.drift_loss) ? b.drift_loss : 1e9))
      .map((c) => ({
        label: c.gen + (c.champion ? ' ♛' : ''), id: c.gen,
        value: svg.isNum(c.drift_loss) ? c.drift_loss : null,
        pass: c.pass_fail, timeout: c.timeout, ran: c.ran,
        best: svg.isNum(c.drift_loss) && bestLoss != null && c.drift_loss === bestLoss,
      }));
    if (items.some((i) => svg.isNum(i.value)) || items.length) {
      chartCard.appendChild(svg.comparativeBars({
        width: 560, rowHeight: 28, labelWidth: 90, items,
        onClick: (it) => ctx.navigate('run', { gen: it.id, entry: entryId }),
      }));
      chartCard.appendChild(el('p', { class: 'vl-faint vl-fignote', text: 'shorter bar = lower loss (better) · ♛ champion · ✓ pass · ✕ fail · ⏱ timeout · click a candidate → its run' }));
    } else {
      chartCard.appendChild(empty('No candidate ran this board entry yet.'));
    }
    nodes.push(section('Loss across candidates · sorted', chartCard));

    // per-candidate table with drill links
    const tblCard = el('div', { class: 'vl-panel' });
    const tbl = el('table', { class: 'vl-board-table' });
    tbl.appendChild(el('thead', null, [el('tr', null, [
      el('th', { text: 'candidate' }),
      el('th', { class: 'vl-num', text: 'drift loss' }),
      el('th', { text: 'predicate' }),
      el('th', { text: 'runtime' }),
      el('th', { text: 'run' }),
    ])]));
    const tb = el('tbody');
    for (const c of cand) {
      tb.appendChild(el('tr', { class: c.champion ? 'vl-is-champ' : '' }, [
        el('td', { class: 'vl-mono' }, [
          el('a', { class: 'vl-md-link', href: ctx.href('candidate', { gen: c.gen }), text: c.gen }),
          c.champion ? el('span', { class: 'vl-faint', text: ' ♛' }) : null,
        ].filter(Boolean)),
        el('td', { class: 'vl-num' + (c.best ? ' vl-good-t' : ''), text: svg.isNum(c.drift_loss) ? svg.fmt(c.drift_loss, 1) : '—' }),
        el('td', null, [predBadge(c.pass_fail, c.timeout)]),
        el('td', { class: 'vl-faint', text: c.timeout ? 'timed out' : (svg.isNum(c.runtime_ms) ? `${(c.runtime_ms / 1000).toFixed(0)}s` : '—') }),
        el('td', null, [c.ran
          ? linkButton('run →', ctx.href('run', { gen: c.gen, entry: entryId }), () => ctx.navigate('run', { gen: c.gen, entry: entryId }))
          : el('span', { class: 'vl-faint', text: 'no run' })]),
      ]));
    }
    tbl.appendChild(tb);
    tblCard.appendChild(tbl);
    nodes.push(section('Per-candidate detail', tblCard));
    return nodes;
  });
}

function predBadge(pf, timeout) {
  if (timeout) return el('span', { class: 'vl-pill vl-rejected', text: 'timeout' });
  if (pf === 1 || pf === true) return el('span', { class: 'vl-pill vl-promoted', text: 'pass' });
  if (pf === 0 || pf === false) return el('span', { class: 'vl-pill vl-rejected', text: 'fail' });
  return el('span', { class: 'vl-faint', text: 'none' });
}

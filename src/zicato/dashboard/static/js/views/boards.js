// js/views/boards.js — BOARDS group: the board TRELLIS (small-multiples).
//
// The detail pane for the tree's "Boards" group. Per the round-5 de-dup
// decision (fix #6) the board TRELLIS lives HERE — not at the epoch overview
// (where the compact heatmap stays). One small-multiple per board entry: a
// per-candidate drift-loss sparkbar + a pass/fail/timeout dot strip. Every
// card routes to the per-board cross-candidate view (views/board.js) by its
// entry id (fix #7 — never an arbitrary candidate).
//
// Data: /api/epoch (the board), /api/lineage, /api/generation/{e}/{g}/per-entry.

import { el } from '../core/dom.js';
import { state } from '../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { section, empty, stat, densityTokens, renderView, figCaption, ENTRY_KIND_LABEL } from '../ui.js';
import { inflightForActiveEpoch, inflightForEntryGen, runProgressRatio } from '../tournament_model.js';
import { livenessFor } from '../livestatus.js';

// The trellis sort key over the entry-kind vocabulary
// (core/board.py::BoardEntryKind): richest conversation shape first, the two
// synthetic kinds last. The LABELS are ui.js's ENTRY_KIND_LABEL — this map
// only orders. An unranked kind sorts after every ranked one.
const KIND_ORDER = {
  multi_turn_emulated: 0, multi_turn_scripted: 1, single_turn: 2,
  synthetic_adversarial: 3, synthetic_clean: 4,
};

export async function render(host, ctx, params) {
  // Class A: scope to the viewed epoch (route param first).
  const epochParam = (params && params.epochId) || null;
  await renderView(host, ctx, {
    loading: 'Reading board trellis…',
    epoch: true, routeEpoch: epochParam, title: 'Boards',
    load: async ({ ep, epochId }) => {
  const board = Array.isArray(ep.board) ? ep.board : [];
  const rows = await D.generationsForEpoch(epochId);
  const gens = rows.length
    ? rows.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: g.promoted == null ? null : !!g.promoted }))
    : (Array.isArray(ep.experiments) ? ep.experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: x.promoted === true })) : []);

  const perEntries = await Promise.all(gens.map((g) => D.perEntry(epochId, g.id)));
  const rowByGenEntry = new Map();
  gens.forEach((g, i) => {
    const pe = perEntries[i];
    if (pe && Array.isArray(pe.entries)) for (const r of pe.entries) {
      rowByGenEntry.set(`${g.id}|${r.entry_id}`, r);
    }
  });
  // WHICH CHANNEL the trellis bars read. The continuous per-entry score when the
  // board is scored (higher is better), the drift loss otherwise. The server
  // says whether the drift channel carries information at all: an adapter that
  // emits no drift stream writes a structural 0.000 on every entry, and a
  // trellis of empty bars states nothing about any candidate.
  const scored = [...rowByGenEntry.values()].some((r) => svg.isNum(r.score));
  // The drift channel's presence is the SERVER's answer, and only the server's:
  // a payload that does not carry the flag predates it, so the pre-feature
  // behaviour (drift shown) stands rather than the view guessing "absent" and
  // hiding a real measurement.
  const driftKnown = perEntries.some((pe) => pe && typeof pe.drift_present === 'boolean');
  const driftPresent = !driftKnown || perEntries.some((pe) => pe && pe.drift_present === true);
  const channel = scored ? 'score' : driftPresent ? 'drift' : 'pass';
  const valueOf = (r) => (channel === 'score' ? r.score : channel === 'drift' ? r.drift_loss : NaN);
  const allValues = [];
  for (const r of rowByGenEntry.values()) if (svg.isNum(valueOf(r))) allValues.push(valueOf(r));
  const domain = allValues.length ? svg.extent(allValues) : null;

  // LIVE — in-flight board runs, CURRENT-EPOCH-SCOPED. A foreign-epoch run must
  // not light up this trellis, so the set is gated on the live run belonging to
  // the viewed epoch (mirrors gens.js / candidate.js).
  const epochInflight = inflightForActiveEpoch(state.activeRuns, {
    heartbeat: state.heartbeat, activeTournament: state.activeTournament,
    // Liveness comes from the served tri-state rather than from file presence:
    // `active_runs` records outlive their process, so reading them directly
    // leaves a trellis cell saying "3 running" months after the run died.
    running: livenessFor(state).liveness.live, epochId,
  });
  // per-entry in-flight tally (count + summed progress) for the trellis cells.
  const inflightByEntry = new Map();
  for (const r of epochInflight) {
    const eid = r.entry_id != null ? r.entry_id : null;
    if (eid == null) continue;
    const pr = runProgressRatio(r);
    const cur = inflightByEntry.get(eid) || { count: 0, sumProgress: 0, hasProgress: false };
    cur.count += 1;
    if (pr != null) { cur.sumProgress += pr; cur.hasProgress = true; }
    inflightByEntry.set(eid, cur);
  }

      return { epochId, board, gens, rowByGenEntry, domain, channel, valueOf, inflightByEntry };
    },
    digest: (d) => JSON.stringify({
      epochId: d.epochId,
      board: d.board.map((b) => [b.entry_id, b.kind, b.weight, b.budget_s]),
      gens: d.gens.map((g) => g.id),
      channel: d.channel,
      value: [...d.rowByGenEntry.entries()].map(([k, r]) => [k, svg.isNum(d.valueOf(r)) ? d.valueOf(r).toFixed(3) : null, r.pass_fail, !!r.wall_clock_budget_exceeded]).sort(),
      // LIVE in-flight per entry — folded in so a beat that advances progress
      // repaints the lit cells, but a no-op heartbeat leaves the digest equal.
      inflight: [...d.inflightByEntry.entries()].map(([eid, v]) => [eid, v.count, v.hasProgress ? v.sumProgress.toFixed(2) : null]).sort(),
    }),
    build: (d) => {
    const { epochId, board, gens, rowByGenEntry, domain, channel, valueOf, inflightByEntry } = d;
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: `Boards · ${epochId}` }),
      el('p', { class: 'dn-lede', text: 'The fixed task board for this epoch as small-multiples — one card per entry, every candidate’s '
        + (channel === 'score' ? 'score (higher is better)' : channel === 'drift' ? 'drift loss (lower is better)' : 'pass outcome')
        + ' on a shared scale. Open a card for the per-board cross-candidate view + inline transcripts.' }),
    ]));

    // The kind counters partition the WHOLE vocabulary. Testing only
    // `=== 'single_turn'` and `startsWith('multi')` would make an all-synthetic
    // board read "0 single-turn · 0 multi-turn" over N entries. The synthetic
    // tile appears only on a board that has synthetic entries.
    const synthetic = board.filter((b) => b.kind && b.kind.startsWith('synthetic')).length;
    nodes.push(el('div', { class: 'dn-panel dn-row' }, [
      stat(String(board.length), 'board entries'),
      stat(String(gens.length), 'candidates'),
      stat(String(board.filter((b) => b.kind === 'single_turn').length), 'single-turn'),
      stat(String(board.filter((b) => b.kind && b.kind.startsWith('multi')).length), 'multi-turn'),
      ...(synthetic ? [stat(String(synthetic), 'synthetic')] : []),
    ]));

    const trellisTitle = channel === 'score' ? 'Board trellis · score across candidates'
      : channel === 'drift' ? 'Board trellis · drift loss across candidates'
      : 'Board trellis · pass outcome across candidates';
    nodes.push(section(trellisTitle, trellis(board, gens, rowByGenEntry, domain, valueOf, epochId, ctx, inflightByEntry, channel)));
    return nodes;
    },
  });
}

// One trellis cell's dim caption: ONE short key line (budget · weight, uniform
// width across cells so the grid reads as a grid), with the prompt and the tags
// behind the "?". Stacking two dim blocks under the figure — a budget/weight/tags
// row and the entry's prompt — would spend a third of every cell's height on
// metadata. The cell is already TITLED by its entry_id, which names the task, so
// the prompt confirms rather than identifies: detail on demand.
//
// Exported so the collapse is testable without building the whole trellis.
export function trellisCaption(b) {
  const lead = [
    svg.isNum(b.budget_s) ? `${b.budget_s}s budget` : 'no budget',
    svg.isNum(b.weight) ? `w ${svg.fmt(b.weight, 1)}` : 'w —',
  ].join(' · ');
  const prompt = typeof b.input_preview === 'string' ? b.input_preview.trim() : '';
  const tags = (Array.isArray(b.tags) && b.tags.length) ? b.tags.join(' · ') : '';
  return figCaption([lead, prompt ? `“${prompt}”` : '', tags], {
    class: 'dn-trellis-foot', title: 'This board entry’s prompt and tags',
  });
}

function trellis(board, gens, rowByGenEntry, domain, valueOf, epochId, ctx, inflightByEntry, channel) {
  if (!board.length) return el('div', { class: 'dn-panel' }, [empty('No board entries recorded.')]);
  const channelLabel = channel === 'score' ? 'score, higher is better'
    : channel === 'drift' ? 'drift loss, lower is better' : 'pass outcome';
  const dt = densityTokens();
  const sorted = board.slice().sort((a, b) => {
    const ka = KIND_ORDER[a.kind] ?? 9; const kb = KIND_ORDER[b.kind] ?? 9;
    if (ka !== kb) return ka - kb;
    const wa = svg.isNum(a.weight) ? a.weight : 0; const wb = svg.isNum(b.weight) ? b.weight : 0;
    if (wa !== wb) return wb - wa;
    return String(a.entry_id).localeCompare(String(b.entry_id));
  });
  const grid = el('div', { class: 'dn-trellis' });
  const genIds = gens.map((g) => g.id);
  for (const b of sorted) {
    const eid = b.entry_id;
    const bars = genIds.map((g) => {
      const r = rowByGenEntry.get(`${g}|${eid}`);
      return { label: g, value: r && svg.isNum(valueOf(r)) ? valueOf(r) : NaN, fail: r ? r.pass_fail === false : false, timeout: r ? !!r.wall_clock_budget_exceeded : false };
    });
    const cells = genIds.map((g) => {
      const r = rowByGenEntry.get(`${g}|${eid}`);
      return r ? { label: g, pass: r.pass_fail, timeout: !!r.wall_clock_budget_exceeded, ran: true } : { label: g, ran: false };
    });
    // LIVE — this board entry has in-flight runs right now (current-epoch).
    const inf = inflightByEntry && inflightByEntry.get(eid);
    const cellCls = 'dn-trellis-cell' + (inf && inf.count ? ' dn-trellis-live' : '');
    const cell = el('figure', { class: cellCls }, [
      el('figcaption', { class: 'dn-trellis-cap' }, [
        el('span', { class: 'dn-trellis-id', text: String(eid) }),
        el('span', { class: 'dn-trellis-meta' }, [
          el('span', { class: 'dn-kind-tag dn-kind-' + (b.kind || 'unknown'), text: ENTRY_KIND_LABEL[b.kind] || b.kind || '—' }),
          b.expectation_kind ? el('span', { class: 'dn-faint', text: ' · ' + b.expectation_kind }) : null,
          inf && inf.count ? el('span', { class: 'dn-trellis-live-tag', title: inf.count + ' running' }, [
            el('span', { class: 'dn-inflight-pulse', 'aria-hidden': 'true' }),
            el('span', { text: inf.count + ' running' }),
          ]) : null,
        ].filter(Boolean)),
      ]),
      svg.sparkbar({ width: 200, height: dt.sparkbarH, bars, domain: domain || undefined }),
      svg.genDots({ width: 200, height: Math.round(14 * dt.sizeScale), cells }),
      trellisCaption(b),
    ].filter(Boolean));
    cell.style.cursor = 'pointer';
    // fix #7: route by entry id → per-board cross-candidate view (never v2).
    cell.addEventListener('click', () => ctx.navigate('board', { epochId, entry: eid }));
    grid.appendChild(cell);
  }
  const card = el('div', { class: 'dn-panel' });
  card.appendChild(grid);
  card.appendChild(el('div', { class: 'dn-legend' }, [
    el('span', null, [el('i', { class: 'spine' }), 'one bar per candidate · ' + channelLabel + ' (shared scale)']),
    el('span', null, [el('i', { class: 'dotact' }), 'pass']),
    el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'fail']),
    el('span', { class: 'dn-faint', text: '⏱ timeout · click a board → that entry across every candidate' }),
    inflightByEntry && inflightByEntry.size ? el('span', null, [el('span', { class: 'dn-inflight-pulse', 'aria-hidden': 'true' }), 'in-flight now (live run)']) : null,
  ].filter(Boolean)));
  return card;
}

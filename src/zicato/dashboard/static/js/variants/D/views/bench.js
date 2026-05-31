// variants/D/views/bench.js — THEME 2: the boards a candidate faces.
//
// The fixed per-epoch board as a TRELLIS of small multiples — one micro
// chart per board entry, not a wall of rows. Each entry's cell carries:
//   * its kind (single_turn / multi_turn_scripted / multi_turn_emulated),
//     budget and weight as a quiet caption;
//   * a SPARKBAR of that entry's drift loss across the candidate
//     generations (one bar per generation) on a SHARED loss scale, so the
//     whole trellis is directly comparable;
//   * a per-generation pass / fail / timeout dot row beneath the bars.
// The trellis is SORTED meaningfully: by kind, then descending weight,
// then id — so the heaviest, most structured tests read first. The board
// becomes a comparative figure, not a table.
//
// Click an entry's cell → that entry's per-board scoring drill-down
// (Scoring view) for the reigning / latest candidate.
//
// Data: /api/epoch (board, experiments), /api/lineage,
// /api/generation/{e}/{g}/per-entry (loss + pass/fail + timeout).

import { el, clearChildren } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { section, crumb, empty, loading, stat } from '../ui.js';

const KIND_ORDER = { multi_turn_emulated: 0, multi_turn_scripted: 1, single_turn: 2 };
const KIND_LABEL = {
  single_turn: 'single-turn', multi_turn_scripted: 'scripted multi-turn',
  multi_turn_emulated: 'emulated multi-turn',
};

export async function render(host, ctx) {
  clearChildren(host);
  host.appendChild(crumb([{ label: 'environment', view: 'environment' }, { label: 'boards' }]));
  host.appendChild(el('h1', { class: 'd-h1', text: 'The boards a candidate faces' }));
  host.appendChild(el('p', { class: 'd-lede', text: 'The fixed task board for this epoch — one small multiple per entry, every candidate runs all of them. Bars share a loss scale so the field reads at a glance.' }));

  const body = el('div'); host.appendChild(body);
  body.appendChild(loading('Reading board…'));
  const [ep, lin] = await Promise.all([D.epoch(), D.lineage()]);
  if (!ep || ep.epoch_id == null) { clearChildren(body); body.appendChild(empty('No current epoch.')); return; }
  const epochId = ep.epoch_id;
  const board = Array.isArray(ep.board) ? ep.board : [];
  clearChildren(body);
  if (!board.length) { body.appendChild(empty('No board entries recorded.')); return; }

  // Candidate generations (ordered).
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  let gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => g.generation_id)
    : experiments.map((x) => x.generation_id);
  gens = gens.filter(Boolean);

  // Per-entry losses for every generation.
  const perEntries = await Promise.all(gens.map((g) => D.perEntry(epochId, g)));
  const rowByGenEntry = new Map(); // `${gen}|${entry}` -> row
  const allLoss = [];
  gens.forEach((g, i) => {
    const pe = perEntries[i];
    if (pe && Array.isArray(pe.entries)) {
      for (const r of pe.entries) {
        rowByGenEntry.set(`${g}|${r.entry_id}`, r);
        if (svg.isNum(r.drift_loss)) allLoss.push(r.drift_loss);
      }
    }
  });
  const domain = allLoss.length ? svg.extent(allLoss) : null;

  // ---- headline ----
  const kinds = new Set(board.map((b) => b.kind).filter(Boolean));
  const totalWeight = board.reduce((a, b) => a + (svg.isNum(b.weight) ? b.weight : 0), 0);
  body.appendChild(el('div', { class: 'd-panel d-row' }, [
    stat(String(board.length), 'entries'),
    stat(String(kinds.size), 'kinds'),
    stat(String(gens.length), 'candidates'),
    stat(svg.fmt(totalWeight, 1), 'total weight'),
  ]));

  // ---- sorted trellis ----
  const sorted = board.slice().sort((a, b) => {
    const ka = KIND_ORDER[a.kind] ?? 9; const kb = KIND_ORDER[b.kind] ?? 9;
    if (ka !== kb) return ka - kb;
    const wa = svg.isNum(a.weight) ? a.weight : 0; const wb = svg.isNum(b.weight) ? b.weight : 0;
    if (wa !== wb) return wb - wa;
    return String(a.entry_id || a.id).localeCompare(String(b.entry_id || b.id));
  });

  const trellis = el('div', { class: 'd-trellis' });
  for (const b of sorted) {
    const eid = b.entry_id || b.id;
    const bars = gens.map((g) => {
      const r = rowByGenEntry.get(`${g}|${eid}`);
      return {
        label: g,
        value: r && svg.isNum(r.drift_loss) ? r.drift_loss : NaN,
        fail: r ? (r.pass_fail === 0) : false,
        timeout: r ? !!r.wall_clock_budget_exceeded : false,
      };
    });
    const cell = el('figure', { class: 'd-trellis-cell' });
    cell.appendChild(el('figcaption', { class: 'd-trellis-cap' }, [
      el('span', { class: 'd-trellis-id', text: String(eid) }),
      el('span', { class: 'd-trellis-meta' }, [
        el('span', { class: 'd-kind-tag d-kind-' + (b.kind || 'unknown'), text: KIND_LABEL[b.kind] || b.kind || '—' }),
        b.expectation_kind ? el('span', { class: 'd-faint', text: ' · ' + b.expectation_kind }) : null,
      ]),
    ]));
    cell.appendChild(svg.sparkbar({ width: 200, height: 42, bars, domain: domain || undefined }));
    // pass/fail/timeout dot row across generations
    const cells = gens.map((g) => {
      const r = rowByGenEntry.get(`${g}|${eid}`);
      return r
        ? { label: g, pass: r.pass_fail, timeout: !!r.wall_clock_budget_exceeded, ran: true }
        : { label: g, ran: false };
    });
    cell.appendChild(svg.genDots({ width: 200, height: 14, cells }));
    // budget + weight footer
    cell.appendChild(el('div', { class: 'd-trellis-foot d-faint' }, [
      el('span', { text: svg.isNum(b.budget_s) ? `${b.budget_s}s budget` : 'no budget' }),
      el('span', { text: svg.isNum(b.weight) ? `w ${svg.fmt(b.weight, 1)}` : 'w —' }),
      Array.isArray(b.tags) && b.tags.length
        ? el('span', { class: 'd-trellis-tags', text: b.tags.slice(0, 3).join(' ') }) : null,
    ]));
    if (b.input_preview) {
      cell.appendChild(el('div', { class: 'd-trellis-preview d-faint',
        title: b.input_preview,
        text: '“' + (b.input_preview.length > 64 ? b.input_preview.slice(0, 63) + '…' : b.input_preview) + '”' }));
    }
    cell.style.cursor = 'pointer';
    cell.addEventListener('click', () => ctx.navigate('run', { gen: gens[gens.length - 1], entry: eid }));
    trellis.appendChild(cell);
  }
  const card = el('div', { class: 'd-panel' });
  card.appendChild(trellis);
  card.appendChild(el('div', { class: 'd-legend' }, [
    el('span', null, [el('i', { class: 'spine' }), 'one bar per candidate · drift loss (shared scale)']),
    el('span', null, [el('i', { class: 'dotact' }), 'pass']),
    el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'fail']),
    el('span', { class: 'd-faint', text: '⏱ timeout · click a board → its run scoring' }),
  ]));
  body.appendChild(section('Board trellis · drift loss across candidates', card));
}

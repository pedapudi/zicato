// variants/L/views/epoch.js — EPOCH: the data substrate of one epoch.
//
// Leads with the objective, then the three figures, PROPORTIONALLY sized so
// no figure dwarfs its neighbours (fix #6): the lineage BUMPS, the board ×
// generation drift-loss HEATMAP whose ramp is read from the ACTIVE colour
// theme's tokens at draw time (fix #4), and the board TRELLIS small
// multiples. The heatmap CELLS and the trellis CARDS both route to the NEW
// per-board cross-candidate view, keyed by ENTRY ID (fix #7) — never an
// arbitrary candidate.
//
// Data: /api/epoch, /api/lineage, /api/score-trajectory,
// /api/generation/{e}/{g}/per-entry.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, renderMarkdown, normaliseDecision, themeRamp } from '../ui.js';

const KIND_ORDER = { multi_turn_emulated: 0, multi_turn_scripted: 1, single_turn: 2 };
const KIND_LABEL = { single_turn: 'single-turn', multi_turn_scripted: 'scripted multi-turn', multi_turn_emulated: 'emulated multi-turn' };

export async function render(host, ctx) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'vl-empty', text: 'Reading epoch contract…' }));

  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'vl-h1', text: 'Epoch' }), empty('No current epoch.')]);
    return;
  }
  const epochId = ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const board = Array.isArray(ep.board) ? ep.board : [];

  const gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' }));

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  const perEntries = await Promise.all(gens.map((g) => D.perEntry(epochId, g.id)));
  const rowByGenEntry = new Map();
  const lossLookup = new Map();
  const entryIds = new Set();
  const allLoss = [];
  gens.forEach((g, i) => {
    const pe = perEntries[i];
    if (pe && Array.isArray(pe.entries)) for (const r of pe.entries) {
      rowByGenEntry.set(`${g.id}|${r.entry_id}`, r);
      entryIds.add(r.entry_id);
      if (svg.isNum(r.drift_loss)) { lossLookup.set(`${r.entry_id}|${g.id}`, r.drift_loss); allLoss.push(r.drift_loss); }
    }
  });
  for (const b of board) { const id = b.entry_id || b.id; if (id) entryIds.add(id); }
  const domain = allLoss.length ? svg.extent(allLoss) : null;
  // The ramp is theme-keyed; include it in the digest so a theme switch that
  // changed the tokens repaints (a no-op when unchanged).
  const ramp = themeRamp();

  const digest = JSON.stringify({
    epochId, goal: ep.goal || '', briefLen: (ep.brief || '').length, closed: !!ep.closed,
    gens: gens.map((g) => [g.id, g.parent, g.promoted, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
    loss: [...lossLookup.entries()].sort(),
    board: board.map((b) => [b.entry_id || b.id, b.kind, b.weight, b.budget_s]),
    ramp,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'vl-pagehead' }, [
      el('h1', { class: 'vl-h1', text: `Epoch ${epochId}` }),
      el('div', { class: 'vl-objective' }, [
        el('div', { class: 'vl-objective-lab', text: 'objective' }),
        el('div', { class: 'vl-objective-txt', text: ep.goal && ep.goal.trim() ? ep.goal : '(no objective recorded)' }),
      ]),
    ]));

    const promotedCount = gens.filter((g) => g.promoted).length;
    nodes.push(el('div', { class: 'vl-panel vl-row', style: 'margin-top:12px;' }, [
      stat(String(board.length), 'board entries'),
      stat(String(experiments.length), 'experiments'),
      stat(String(promotedCount), 'promoted'),
      stat(ep.closed ? 'closed' : 'open', 'state'),
    ]));

    const briefText = ep.brief || '';
    const briefDetails = el('details', { class: 'vl-brief', open: briefText.length < 1200 ? '' : null }, [
      el('summary', null, [
        el('span', { class: 'vl-chev', text: '▸' }), 'Proposer brief',
        el('span', { class: 'vl-faint', style: 'font-weight:400;font-size:11px;', text: briefText ? `· ${briefText.split(/\n/).length} lines` : '· none' }),
      ]),
      renderMarkdown(briefText),
    ]);
    nodes.push(section('Operator’s brief to the proposer', briefDetails));

    // ---- two figures side by side, PROPORTIONALLY sized -------------
    const bumpNodes = gens.map((g, i) => ({ id: g.id, x: i, promoted: g.promoted, scalar: scalarByGen.get(g.id), parent: g.parent }));
    if (bumpNodes.length && !bumpNodes.some((n) => n.promoted)) bumpNodes[0].promoted = true;

    const bumpsFig = figCard('Lineage', svg.bumps({ width: 460, height: 220, nodes: bumpNodes, onClick: (n) => ctx.navigate('candidate', { gen: n.id }) }),
      'champion spine + challenger lane · click a node → its candidate');

    const rows = [...entryIds].sort().map((id) => ({ id, label: id }));
    const cols = gens.map((g) => ({ id: g.id, label: g.id }));
    let hmMark;
    if (rows.length && cols.length) {
      hmMark = svg.heatmap({
        rows, cols, ramp, cellW: 40, cellH: 20, labelWidth: 160,
        value: (r, c) => (lossLookup.has(`${r}|${c}`) ? lossLookup.get(`${r}|${c}`) : null),
        // route to the per-board cross-candidate view, keyed by ENTRY ID.
        onClick: (rId) => ctx.navigate('board', { entry: rId }),
      });
    } else {
      hmMark = empty('No per-entry loss profiles yet (the index may not be built).');
    }
    const hmFig = figCard('Board × generation · drift loss', hmMark,
      'cell = drift loss · theme-aware ramp · click → that board entry across all candidates');

    nodes.push(section('Figures', el('div', { class: 'vl-figrow' }, [bumpsFig, hmFig])));

    // ---- board trellis (small multiples) → board view ---------------
    nodes.push(section('Board trellis · drift loss across candidates', boardTrellis(board, gens, rowByGenEntry, domain, ctx)));
    return nodes;
  });
}

// A figure cell with a consistent max-width so neighbours stay proportional.
function figCard(title, mark, note) {
  return el('figure', { class: 'vl-figcard' }, [
    el('figcaption', { class: 'vl-figcard-cap', text: title }),
    el('div', { class: 'vl-figcard-mark' }, [mark]),
    note ? el('p', { class: 'vl-faint vl-fignote', text: note }) : null,
  ].filter(Boolean));
}

function boardTrellis(board, gens, rowByGenEntry, domain, ctx) {
  if (!board.length) return el('div', { class: 'vl-panel' }, [empty('No board entries recorded.')]);
  const sorted = board.slice().sort((a, b) => {
    const ka = KIND_ORDER[a.kind] ?? 9; const kb = KIND_ORDER[b.kind] ?? 9;
    if (ka !== kb) return ka - kb;
    const wa = svg.isNum(a.weight) ? a.weight : 0; const wb = svg.isNum(b.weight) ? b.weight : 0;
    if (wa !== wb) return wb - wa;
    return String(a.entry_id || a.id).localeCompare(String(b.entry_id || b.id));
  });
  const trellis = el('div', { class: 'vl-trellis' });
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
    const cell = el('figure', { class: 'vl-trellis-cell', tabindex: '0', 'data-vl': 'trellis-cell', 'data-entry': String(eid) }, [
      el('figcaption', { class: 'vl-trellis-cap' }, [
        el('span', { class: 'vl-trellis-id', text: String(eid) }),
        el('span', { class: 'vl-trellis-meta' }, [
          el('span', { class: 'vl-kind-tag vl-kind-' + (b.kind || 'unknown'), text: KIND_LABEL[b.kind] || b.kind || '—' }),
          b.expectation_kind ? el('span', { class: 'vl-faint', text: ' · ' + b.expectation_kind }) : null,
        ].filter(Boolean)),
      ]),
      svg.sparkbar({ width: 200, height: 42, bars, domain: domain || undefined }),
      svg.genDots({ width: 200, height: 14, cells }),
      el('div', { class: 'vl-trellis-foot vl-faint' }, [
        el('span', { text: svg.isNum(b.budget_s) ? `${b.budget_s}s budget` : 'no budget' }),
        el('span', { text: svg.isNum(b.weight) ? `w ${svg.fmt(b.weight, 1)}` : 'w —' }),
        Array.isArray(b.tags) && b.tags.length ? el('span', { class: 'vl-trellis-tags', text: b.tags.slice(0, 3).join(' ') }) : null,
      ].filter(Boolean)),
      b.input_preview ? el('div', { class: 'vl-trellis-preview vl-faint', title: b.input_preview, text: '“' + (b.input_preview.length > 64 ? b.input_preview.slice(0, 63) + '…' : b.input_preview) + '”' }) : null,
    ].filter(Boolean));
    cell.style.cursor = 'pointer';
    // route to the per-board cross-candidate view, keyed by ENTRY ID.
    const go = () => ctx.navigate('board', { entry: eid });
    cell.addEventListener('click', go);
    cell.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(); } });
    trellis.appendChild(cell);
  }
  const card = el('div', { class: 'vl-panel' });
  card.appendChild(trellis);
  card.appendChild(el('p', { class: 'vl-faint vl-fignote', text: 'one bar per candidate · drift loss (shared scale) · ✓ pass · ✕ fail · ⏱ timeout · click a board → its cross-candidate view' }));
  return card;
}

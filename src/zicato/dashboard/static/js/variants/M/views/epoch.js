// variants/M/views/epoch.js — EPOCH: the data substrate of one epoch.
//
// Ledger II's epoch screen reads like the opening pages of a paper: an
// eyebrow + serif title, the objective as a lede, the proposer brief, then
// PROPORTIONALLY-SIZED figures (convergence-II fix #6 — the drift heatmap no
// longer dwarfs its neighbours; figures share a max-width envelope), each
// with an editorial caption: the non-colliding lineage BUMPS, the board ×
// generation drift HEATMAP, and the board TRELLIS. Rails link to the epoch's
// per-board cross-candidate view and its ACM publication.
//
// CONVERGENCE-II FIX #7 — the board trellis cards AND the heatmap cells route
// to the NEW per-board cross-candidate view (keyed by ENTRY id), NOT to an
// arbitrary candidate (the operator flagged a trellis click dumping the user
// on candidate v2 with no fidelity).
//
// Data: /api/epoch, /api/lineage, /api/score-trajectory, /api/generation/per-entry.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, renderMarkdown, normaliseDecision, pageHead, figure } from '../ui.js';

const KIND_ORDER = { multi_turn_emulated: 0, multi_turn_scripted: 1, single_turn: 2 };
const KIND_LABEL = {
  single_turn: 'single-turn', multi_turn_scripted: 'scripted multi-turn',
  multi_turn_emulated: 'emulated multi-turn',
};

export async function render(host, ctx) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'd-empty', text: 'Reading epoch contract…' }));

  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [pageHead('Epoch', 'Epoch', ''), empty('No current epoch.')]);
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

  const digest = JSON.stringify({
    epochId, goal: ep.goal || '', briefLen: (ep.brief || '').length, closed: !!ep.closed,
    gens: gens.map((g) => [g.id, g.parent, g.promoted, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
    loss: [...lossLookup.entries()].sort(),
    board: board.map((b) => [b.entry_id || b.id, b.kind, b.weight, b.budget_s]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(pageHead('Epoch report · ' + epochId, 'Epoch ' + epochId,
      [ep.goal && ep.goal.trim() ? ep.goal : '(no objective recorded for this epoch)']));

    // rails: per-board view + publication
    nodes.push(el('div', { class: 'm-rail-row' }, [
      el('div', { class: 'm-rail' }, [
        el('span', { class: 'm-rail-lab', text: 'Board' }),
        el('span', { class: 'd-faint', text: 'How every candidate fared on each board entry — the per-board cross-candidate view.' }),
        el('a', { class: 'm-btn m-btn-ghost', href: ctx.href('board', {}), text: 'Open the board →' }),
      ]),
      el('div', { class: 'm-rail' }, [
        el('span', { class: 'm-rail-lab', text: 'Publication' }),
        el('span', { class: 'd-faint', text: 'The full ACM-style write-up of this epoch, with live figures.' }),
        el('a', { class: 'm-btn', href: ctx.href('paper', { epochId }), text: 'Read the publication →' }),
      ]),
    ]));

    const promotedCount = gens.filter((g) => g.promoted).length;
    nodes.push(el('div', { class: 'd-panel d-row', style: 'margin-top:12px;' }, [
      stat(String(board.length), 'board entries'),
      stat(String(experiments.length), 'experiments'),
      stat(String(promotedCount), 'promoted'),
      stat(ep.closed ? 'closed' : 'open', 'state'),
    ]));

    const briefText = ep.brief || '';
    const briefDetails = el('details', { class: 'd-brief', open: briefText.length < 1200 ? '' : null }, [
      el('summary', null, [
        el('span', { class: 'chev', text: '▸' }), 'Proposer brief',
        el('span', { class: 'd-faint', style: 'font-weight:400;font-size:11px;', text: briefText ? `· ${briefText.split(/\n/).length} lines` : '· none' }),
      ]),
      renderMarkdown(briefText),
    ]);
    nodes.push(section('Operator’s brief to the proposer', briefDetails));

    // lineage bumps (figure) — proportional sizing envelope
    const bumpNodes = gens.map((g, i) => ({ id: g.id, x: i, promoted: g.promoted, scalar: scalarByGen.get(g.id), parent: g.parent }));
    if (bumpNodes.length && !bumpNodes.some((n) => n.promoted)) bumpNodes[0].promoted = true;
    const bumpsMark = svg.bumps({ width: 720, height: 190, nodes: bumpNodes, onClick: (n) => ctx.navigate('candidate', { gen: n.id }) });
    nodes.push(section('Lineage', el('div', { class: 'd-panel m-fig-md' }, [
      figure(bumpsMark, 'The champion spine (promoted lineage) versus rejected challengers, de-collided. Click a node to open its candidate.', { label: 'Figure 2.' }),
    ])));

    // board × generation heatmap (figure) — fix #4 (theme tokens) + #6 (sizing)
    const rows = [...entryIds].sort().map((id) => ({ id, label: id }));
    const cols = gens.map((g) => ({ id: g.id, label: g.id }));
    if (rows.length && cols.length) {
      const hmInner = el('div', { class: 'm-scroll-x' });
      hmInner.appendChild(svg.heatmap({
        rows, cols, diverging: false, cellW: 40, cellH: 22, labelWidth: 170,
        value: (r, c) => (lossLookup.has(`${r}|${c}`) ? lossLookup.get(`${r}|${c}`) : null),
        // FIX #7: a heatmap cell routes to the per-board view (keyed by entry).
        onClick: (rId) => ctx.navigate('board', { entry: rId }),
      }));
      nodes.push(section('Board entries × generations · drift loss', el('div', { class: 'd-panel m-fig-md' }, [
        figure(hmInner, 'Each cell is the drift loss for one board entry in one generation; the ramp derives from the active colour theme. Click a cell to open that board entry across all candidates.', { label: 'Figure 3.' }),
      ])));
    } else {
      nodes.push(section('Board entries × generations · drift loss', el('div', { class: 'd-panel' }, [empty('No per-entry loss profiles yet (the index may not be built).')])));
    }

    // board trellis
    nodes.push(section('Board trellis · drift loss across candidates', boardTrellis(board, gens, rowByGenEntry, domain, ctx)));
    return nodes;
  });
}

function boardTrellis(board, gens, rowByGenEntry, domain, ctx) {
  if (!board.length) return el('div', { class: 'd-panel' }, [empty('No board entries recorded.')]);
  const sorted = board.slice().sort((a, b) => {
    const ka = KIND_ORDER[a.kind] ?? 9; const kb = KIND_ORDER[b.kind] ?? 9;
    if (ka !== kb) return ka - kb;
    const wa = svg.isNum(a.weight) ? a.weight : 0; const wb = svg.isNum(b.weight) ? b.weight : 0;
    if (wa !== wb) return wb - wa;
    return String(a.entry_id || a.id).localeCompare(String(b.entry_id || b.id));
  });
  const trellis = el('div', { class: 'd-trellis' });
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
    const cell = el('figure', { class: 'd-trellis-cell' }, [
      el('figcaption', { class: 'd-trellis-cap' }, [
        el('span', { class: 'd-trellis-id', text: String(eid) }),
        el('span', { class: 'd-trellis-meta' }, [
          el('span', { class: 'd-kind-tag d-kind-' + (b.kind || 'unknown'), text: KIND_LABEL[b.kind] || b.kind || '—' }),
          b.expectation_kind ? el('span', { class: 'd-faint', text: ' · ' + b.expectation_kind }) : null,
        ].filter(Boolean)),
      ]),
      svg.sparkbar({ width: 200, height: 42, bars, domain: domain || undefined }),
      svg.genDots({ width: 200, height: 14, cells }),
      el('div', { class: 'd-trellis-foot d-faint' }, [
        el('span', { text: svg.isNum(b.budget_s) ? `${b.budget_s}s budget` : 'no budget' }),
        el('span', { text: svg.isNum(b.weight) ? `w ${svg.fmt(b.weight, 1)}` : 'w —' }),
        Array.isArray(b.tags) && b.tags.length ? el('span', { class: 'd-trellis-tags', text: b.tags.slice(0, 3).join(' ') }) : null,
      ].filter(Boolean)),
      b.input_preview ? el('div', { class: 'd-trellis-preview d-faint', title: b.input_preview, text: '“' + (b.input_preview.length > 64 ? b.input_preview.slice(0, 63) + '…' : b.input_preview) + '”' }) : null,
    ].filter(Boolean));
    cell.style.cursor = 'pointer';
    // FIX #7: a trellis card routes to the per-board view (keyed by entry).
    cell.addEventListener('click', () => ctx.navigate('board', { entry: eid }));
    trellis.appendChild(cell);
  }
  const card = el('div', { class: 'd-panel' });
  card.appendChild(trellis);
  card.appendChild(el('p', { class: 'm-figcap', text: 'One micro bar per candidate, drift loss on a shared scale; the glyph row reads pass / fail / timeout. Click a board to open its per-board cross-candidate view.' }));
  return card;
}

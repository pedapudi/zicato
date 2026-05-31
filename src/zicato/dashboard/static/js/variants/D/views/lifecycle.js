// variants/D/views/lifecycle.js — THEME 1: the candidate lifecycle.
//
// One generation's life as a small-multiple STRIP: one tiny multiple per
// candidate, each a per-board loss-profile SPARKBAR (one bar per board
// entry, height = drift loss on a SHARED domain so the strip is directly
// comparable across candidates) topped by a gate-verdict glyph (▲ promoted
// / ▼ rejected). Below it, the lineage as a non-colliding BUMPS chart —
// the champion spine in its own lane, rejected challengers branched into a
// distinct lane — so "who is the parent, who competed, who holds the crown"
// reads in one pass against the champion baseline.
//
// Click a candidate's small multiple → its per-board scoring (Scoring
// view); click a bumps node → its experiment.
//
// Data: /api/lineage (the candidate set + parent + promoted),
// /api/epoch (board + experiments + outcomes), /api/score-trajectory
// (per-gen scalar), /api/generation/{e}/{g}/per-entry (loss profile).

import { el, clearChildren } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { section, crumb, empty, loading, stat, verdictPill, normaliseDecision } from '../ui.js';

export async function render(host, ctx, params) {
  clearChildren(host);
  host.appendChild(crumb([{ label: 'environment', view: 'environment' }, { label: 'lifecycle' }]));
  host.appendChild(el('h1', { class: 'd-h1', text: 'Candidate lifecycle' }));
  host.appendChild(el('p', { class: 'd-lede' }, [
    'Each candidate is born from a parent by a patch, faces the fixed board, scores a per-entry loss profile, and meets the champion at the gate. ',
    el('em', { text: 'Lower loss is better' }),
    '. One small multiple per candidate; the lineage shows who holds the crown.',
  ]));

  const body = el('div'); host.appendChild(body);
  body.appendChild(loading('Reading lineage…'));

  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  clearChildren(body);
  if (!ep || ep.epoch_id == null) { body.appendChild(empty('No current epoch.')); return; }
  const epochId = ep.epoch_id;

  // Candidate set: prefer the lineage endpoint (carries parent + promoted),
  // fall back to the epoch's experiments.
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const expByGen = new Map(experiments.map((x) => [x.generation_id, x]));
  let gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({
        id: g.generation_id,
        parent: g.parent_generation_id || null,
        promoted: !!g.promoted,
      }))
    : experiments.map((x) => ({
        id: x.generation_id,
        parent: x.parent_generation_id || null,
        promoted: normaliseDecision(x.outcome) === 'promoted',
      }));
  if (!gens.length) { body.appendChild(empty('No candidates recorded for this epoch.')); return; }

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) {
    for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  }

  // ---- headline ----
  const promotedCount = gens.filter((g) => g.promoted).length;
  body.appendChild(el('div', { class: 'd-panel d-row' }, [
    stat(String(gens.length), 'candidates'),
    stat(String(promotedCount), 'crowned'),
    stat(String(gens.length - promotedCount), 'dead branches'),
    stat(epochId, 'epoch'),
  ]));

  // ---- per-candidate per-board loss profiles (shared domain) ----
  const perEntries = await Promise.all(gens.map((g) => D.perEntry(epochId, g.id)));
  // Board order: the epoch board defines a stable entry order; fall back
  // to the union of entry ids seen across candidates.
  const board = Array.isArray(ep.board) ? ep.board : [];
  const boardOrder = board.map((b) => b.entry_id || b.id).filter(Boolean);
  const seen = new Set(boardOrder);
  for (const pe of perEntries) {
    if (pe && Array.isArray(pe.entries)) {
      for (const r of pe.entries) if (!seen.has(r.entry_id)) { seen.add(r.entry_id); boardOrder.push(r.entry_id); }
    }
  }
  // Shared loss domain across the whole strip.
  const allLoss = [];
  for (const pe of perEntries) {
    if (pe && Array.isArray(pe.entries)) for (const r of pe.entries) if (svg.isNum(r.drift_loss)) allLoss.push(r.drift_loss);
  }
  const domain = allLoss.length ? svg.extent(allLoss) : null;

  const strip = el('div', { class: 'd-sm-grid d-lifecycle-strip' });
  gens.forEach((g, i) => {
    const pe = perEntries[i];
    const byEntry = new Map();
    if (pe && Array.isArray(pe.entries)) for (const r of pe.entries) byEntry.set(r.entry_id, r);
    const bars = boardOrder.map((eid) => {
      const r = byEntry.get(eid);
      return {
        label: eid,
        value: r && svg.isNum(r.drift_loss) ? r.drift_loss : NaN,
        fail: r ? (r.pass_fail === 0) : false,
        timeout: r ? !!r.wall_clock_budget_exceeded : false,
      };
    });
    const exp = expByGen.get(g.id);
    const decision = g.promoted ? 'promoted'
      : (exp ? normaliseDecision(exp.outcome) : (g.parent ? 'rejected' : null));
    const verdict = decision === 'promoted' ? 'promoted' : decision === 'rejected' ? 'rejected' : null;
    const mark = svg.sparkbar({
      width: 168, height: 38, bars, domain: domain || undefined, verdict,
    });
    const scalar = scalarByGen.get(g.id);
    const sub = svg.isNum(scalar) ? svg.fmt(scalar, 1) : '—';
    const cap = (g.promoted ? '♛ ' : '') + g.id;
    const fig = svg.smallMultiple(cap, mark, sub);
    fig.classList.add('d-lifecycle-card');
    fig.style.cursor = 'pointer';
    fig.title = `${g.id}: ${g.parent ? 'from ' + g.parent : 'seed'} · ${verdict || 'baseline'}`;
    fig.addEventListener('click', () => ctx.navigate('run', { gen: g.id }));
    // life-story footer: parent → verdict pill.
    fig.appendChild(el('div', { class: 'd-lifecycle-foot' }, [
      el('span', { class: 'd-faint', style: 'font-size:10px;',
        text: g.parent ? `← ${g.parent}` : 'seed (no parent)' }),
      verdictPill(g.parent ? (verdict || 'rejected') : 'baseline'),
    ]));
    strip.appendChild(fig);
  });
  const stripCard = el('div', { class: 'd-panel' });
  stripCard.appendChild(strip);
  stripCard.appendChild(el('div', { class: 'd-legend' }, [
    el('span', null, [el('i', { class: 'spine' }), 'bar = one board entry · height = drift loss (shared scale)']),
    el('span', null, [el('i', { class: 'good', style: 'border:none;width:0;height:0;border-left:4px solid transparent;border-right:4px solid transparent;border-bottom:6px solid var(--v2-good);' }), '▲ promoted']),
    el('span', null, [el('i', { class: 'bad', style: 'border:none;width:0;height:0;border-left:4px solid transparent;border-right:4px solid transparent;border-top:6px solid var(--v2-bad);' }), '▼ rejected']),
    el('span', { class: 'd-faint', text: 'click a candidate → its per-board scoring' }),
  ]));
  body.appendChild(section('Per-candidate loss profile · the board each faced', stripCard));

  // ---- lineage bumps (champion baseline) ----
  const nodes = gens.map((g, i) => ({
    id: g.id, x: i, promoted: g.promoted,
    scalar: scalarByGen.get(g.id), parent: g.parent,
  }));
  if (nodes.length && !nodes.some((n) => n.promoted)) nodes[0].promoted = true;
  const bumpsCard = el('div', { class: 'd-panel' });
  bumpsCard.appendChild(svg.bumps({
    width: 720, height: 190, nodes,
    onClick: (n) => ctx.navigate('experiment', { gen: n.id }),
  }));
  bumpsCard.appendChild(el('div', { class: 'd-legend' }, [
    el('span', null, [el('i', { class: 'spine' }), 'champion spine (the crown)']),
    el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'rejected challenger (dead branch)']),
    el('span', { class: 'd-faint', text: 'click a node → its experiment' }),
  ]));
  body.appendChild(section('Lineage · where each generation landed vs the champion', bumpsCard));
}

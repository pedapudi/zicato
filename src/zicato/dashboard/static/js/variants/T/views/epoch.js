// variants/T/views/epoch.js — EPOCH OVERVIEW: the dense substrate of one epoch.
//
// Console IV's epoch overview leads with the OBJECTIVE, the collapsible
// proposer brief, the SLIM REEL (the rounds along the champion spine — adopted
// from V, trimmed to a compact fit-to-width spine), then the COMPACT
// board×generation drift-loss HEATMAP. The slim reel REPLACES the old lineage
// BUMPS (same champion-vs-challenger-over-rounds story — we show one, never
// both). Per the round-5 de-dup decision (fix #6) the heatmap STAYS here at the
// epoch overview; the board TRELLIS (small-multiples) lives in the Boards view
// (views/boards.js) — never both on one page.
//
// Data: /api/epoch, /api/lineage, /api/tournaments, /api/score-trajectory,
// /api/generation/{e}/{g}/per-entry.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { reel, reelDigest } from '../reel.js';
import { gatedSwap, section, empty, stat, renderMarkdown, normaliseDecision, densityTokens } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading epoch contract…' }));

  const [ep, lin, traj, bracket] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory(), D.bracket()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Epoch' }), empty('No current epoch.')]);
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
  const lossLookup = new Map();
  const entryIds = new Set();
  gens.forEach((g, i) => {
    const pe = perEntries[i];
    if (pe && Array.isArray(pe.entries)) for (const r of pe.entries) {
      entryIds.add(r.entry_id);
      if (svg.isNum(r.drift_loss)) lossLookup.set(`${r.entry_id}|${g.id}`, r.drift_loss);
    }
  });
  for (const b of board) { const id = b.entry_id || b.id; if (id) entryIds.add(id); }

  // ---- the slim reel's rounds (champion spine + ticks) ----
  // The rounds are the actual tournament match-ups (round-ordered by ran_at);
  // fall back to the rejected/promoted challenger lineage when there is no
  // tournament payload. The champion is the promoted (or seed) generation.
  const champ = gens.find((g) => g.promoted) || gens.find((g) => !g.parent) || null;
  const championId = champ ? champ.id : null;
  const matchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups.slice() : [];
  matchups.sort((a, b) => String(a.ran_at || '').localeCompare(String(b.ran_at || '')));
  const rounds = matchups.length
    ? matchups.map((m) => ({ challenger: m.challenger, decision: m.decision, deltaScalar: m.delta_scalar }))
    : gens.filter((g) => g.parent).map((g) => ({
        challenger: g.id, decision: g.promoted ? 'promoted' : 'rejected',
        deltaScalar: (svg.isNum(scalarByGen.get(g.id)) && svg.isNum(championId ? scalarByGen.get(championId) : NaN))
          ? scalarByGen.get(g.id) - scalarByGen.get(championId) : null,
      }));
  const reelSpec = { championId, rounds };

  const digest = JSON.stringify({
    epochId, goal: ep.goal || '', briefLen: (ep.brief || '').length, closed: !!ep.closed,
    gens: gens.map((g) => [g.id, g.parent, g.promoted, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
    reel: reelDigest(reelSpec),
    loss: [...lossLookup.entries()].sort(),
    board: board.map((b) => [b.entry_id || b.id, b.kind, b.weight, b.budget_s]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: `Epoch ${epochId}` }),
      el('div', { class: 'dn-objective' }, [
        el('div', { class: 'lab', text: 'objective' }),
        el('div', { class: 'txt', text: ep.goal && ep.goal.trim() ? ep.goal : '(no objective recorded)' }),
      ]),
    ]));

    const promotedCount = gens.filter((g) => g.promoted).length;
    nodes.push(el('div', { class: 'dn-panel dn-row', style: 'margin-top:12px;' }, [
      stat(String(board.length), 'board entries'),
      stat(String(experiments.length), 'experiments'),
      stat(String(promotedCount), 'promoted'),
      stat(ep.closed ? 'closed' : 'open', 'state'),
    ]));

    nodes.push(el('div', { class: 'dn-quicklinks' }, [
      el('a', { class: 'dn-linkbtn', href: ctx.href('gens', { epochId }), text: 'Generations →' }),
      el('a', { class: 'dn-linkbtn', href: ctx.href('boards', { epochId }), text: 'Boards (trellis) →' }),
      el('a', { class: 'dn-linkbtn', href: ctx.href('mutations', { epochId }), text: 'Mutation surface + diff →' }),
      el('a', { class: 'dn-linkbtn', href: ctx.href('publication', { epochId }), text: 'Epoch publication (ACM) →' }),
    ]));

    const briefText = ep.brief || '';
    const briefDetails = el('details', { class: 'dn-brief', open: briefText.length < 1200 ? '' : null }, [
      el('summary', null, [
        el('span', { class: 'chev', text: '▸' }), 'Proposer brief',
        el('span', { class: 'dn-faint', style: 'font-weight:400;font-size:11px;', text: briefText ? `· ${briefText.split(/\n/).length} lines` : '· none' }),
      ]),
      renderMarkdown(briefText),
    ]);
    nodes.push(section('Operator’s brief to the proposer', briefDetails));

    // ---- the slim reel: rounds along the champion spine (replaces bumps) ----
    // Fit-to-width, no pan/zoom: the ticks compress as rounds grow. Clicking a
    // station → that round's candidate; the seed → the champion. The big
    // per-challenger detail intentionally lives in the generations match cards,
    // not hung off the reel (that does not scale to many rounds).
    const reelCard = el('div', { class: 'dn-panel' });
    reelCard.appendChild(reel({
      championId, rounds,
      selected: null,
      onSelect: (id) => ctx.navigate('candidate', { epochId, gen: id }),
      onSeed: (id) => { if (id) ctx.navigate('candidate', { epochId, gen: id }); },
    }));
    nodes.push(section('Reel · the rounds along the champion spine', reelCard));

    // ---- COMPACT board entries × generations heatmap (stays here, fix #6) ----
    const rows = [...entryIds].sort().map((id) => ({ id, label: id }));
    const cols = gens.map((g) => ({ id: g.id, label: g.id }));
    // FIT-TO-WIDTH: the heatmap is a responsive SVG (width:100% + viewBox), so
    // it scales to the pane — NO overflow-x wrapper. Density scales cell size.
    const hmt = densityTokens();
    const hmCard = el('div', { class: 'dn-panel dn-figpane' });
    if (rows.length && cols.length) {
      hmCard.appendChild(svg.heatmap({
        rows, cols, cellW: Math.round(hmt.heatCell * 1.6), cellH: hmt.heatCell,
        value: (r, c) => (lossLookup.has(`${r}|${c}`) ? lossLookup.get(`${r}|${c}`) : null),
        // fix #7: a cell routes to the PER-BOARD cross-candidate view, keyed by
        // the entry id (the row) — NOT to an arbitrary candidate.
        onClick: (rId) => ctx.navigate('board', { epochId, entry: rId }),
      }));
      hmCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'cell = drift loss for one board entry in one generation · denser ink = more drift · click a row → that board across every candidate · the small-multiples trellis lives in Boards' }));
    } else {
      hmCard.appendChild(empty('No per-entry loss profiles yet (the index may not be built).'));
    }
    nodes.push(section('Board entries × generations · drift loss (heatmap)', hmCard));
    return nodes;
  });
}

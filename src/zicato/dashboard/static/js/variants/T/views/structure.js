// variants/T/views/structure.js — the configured tournament STRUCTURE.
//
// The match-ups page (views/gens.js) renders the gauntlet ladder for the
// default structure. When the epoch's contract names a NON-gauntlet
// structure (single_elim / double_elim / swiss / racing), this module
// renders the ACTUAL configured topology from the new
// /api/tournament-structure response (§3.2): a fit-to-width bracket for
// the elimination structures, a round-by-round standings table for swiss,
// and a successive-halving rung ladder for racing.
//
// Every renderer is driven by the structure payload and degrades
// gracefully when a field is absent (the live workspace is gauntlet-only,
// so the non-gauntlet renderers are exercised with mock payloads in the
// test suite). The SVG marks reuse svg.js's fit-to-width discipline
// (width:100% + viewBox, no pan/zoom, token-themed across all 9 themes,
// scaling with the page-scale pill).

import { el } from '../../../core/dom.js';
import * as svg from '../svg.js';
import { section, empty, verdictPill } from '../ui.js';

// A friendly label + key params for a structure.
export function structureLabel(structure, params) {
  const s = String(structure || 'gauntlet');
  const p = (params && typeof params === 'object') ? params : {};
  switch (s) {
    case 'gauntlet': {
      const reps = p.replications;
      return 'Gauntlet' + (svg.isNum(reps) && reps > 1 ? ` · ${reps}× replications` : '');
    }
    case 'single_elim':
      return 'Single elimination' + (p.seed_order ? ` · seed: ${p.seed_order}` : '');
    case 'double_elim':
      return 'Double elimination' + (p.grand_final_reset === false ? ' · no GF reset' : '');
    case 'swiss':
      return 'Swiss' + (svg.isNum(p.rounds) ? ` · ${p.rounds} rounds` : '');
    case 'racing': {
      const rungs = Array.isArray(p.rungs) ? p.rungs.length : null;
      return 'Racing (successive halving)' + (rungs ? ` · ${rungs} rungs` : '');
    }
    default:
      return s;
  }
}

// The structure pill (shown in the epoch header + the match-ups header).
export function structurePill(structure, params) {
  return el('span', { class: 'dt-structure-pill', 'data-structure': String(structure || 'gauntlet') }, [
    el('span', { class: 'dt-structure-pill-k', text: 'structure' }),
    el('span', { class: 'dt-structure-pill-v', text: structureLabel(structure, params) }),
  ]);
}

// Is this a structure the dedicated renderers below handle (i.e. NOT the
// default gauntlet, which views/gens.js renders with its own ladder)?
export function isNonGauntlet(structure) {
  const s = String(structure || 'gauntlet');
  return s === 'single_elim' || s === 'double_elim' || s === 'swiss' || s === 'racing';
}

// Normalize EITHER shape — the LIVE /api/active-tournament (carries `phase`
// while in flight) OR the COMPLETED /api/tournament-structure — into ONE
// renderer input. They already share the {structure, competitors, rounds,
// standings} shape; this just stamps `live` (so the renderers can suppress
// "rejected"/"eliminated" verdicts for an in-flight tournament — the promote
// decision only commits at the very end, so a half-finished record would
// otherwise mislabel the eventual winner as a dead branch).
//
//   st   — the raw structure payload (live or completed), or null.
//   live — true when this came from /api/active-tournament with a non-idle phase.
export function normalizeStructure(st, live) {
  if (!st || typeof st !== 'object') return null;
  const phase = String(st.phase || '').toLowerCase();
  const running = !!live && phase !== '' && phase !== 'idle'
    && phase !== 'complete' && phase !== 'completed' && phase !== 'done';
  return {
    structure: st.structure || 'gauntlet',
    structure_params: st.structure_params || st.params || {},
    competitors: Array.isArray(st.competitors) ? st.competitors : [],
    rounds: Array.isArray(st.rounds) ? st.rounds : [],
    standings: Array.isArray(st.standings) ? st.standings : [],
    source: running ? 'live' : (st.source || 'index'),
    phase: st.phase != null ? String(st.phase) : null,
    live: running,
  };
}

// A stable digest of a structure payload so the gated swap re-renders only
// on a real change.
export function structureDigest(st) {
  if (!st || typeof st !== 'object') return 'no-structure';
  return JSON.stringify({
    structure: st.structure,
    live: !!st.live, phase: st.phase || null,
    competitors: (Array.isArray(st.competitors) ? st.competitors : []).map((c) => [c.generation_id, c.seed, c.role]),
    rounds: (Array.isArray(st.rounds) ? st.rounds : []).map((r) => [
      r.round_index, r.label,
      (Array.isArray(r.matches) ? r.matches : []).map((m) => [m.match_id, (m.competitors || []).join('/'), m.winner, m.decision, m.bracket_slot, m.bye, m.survivors && m.survivors.join('/'), m.cut && m.cut.join('/')]),
    ]),
    standings: (Array.isArray(st.standings) ? st.standings : []).map((s) => [s.generation_id, s.rank, s.scalar, s.wins, s.losses, s.status]),
    source: st.source,
  });
}

// ── the structure render dispatch ──────────────────────────────────
//
// Returns an array of DOM nodes (sections) for the configured structure.
// `ctx` carries navigate/href; `epochId` routes a competitor / match to
// the candidate page (the gate is per-match, §3.3).
export function renderStructure(st, ctx, epochId) {
  const structure = String((st && st.structure) || 'gauntlet');
  if (structure === 'swiss') return renderSwiss(st, ctx, epochId);
  if (structure === 'racing') return renderRacing(st, ctx, epochId);
  // single_elim + double_elim share the bracket renderer.
  return renderBracket(st, ctx, epochId, structure);
}

// ---- single / double elimination — a fit-to-width bracket ----------

function renderBracket(st, ctx, epochId, structure) {
  const rounds = (st && Array.isArray(st.rounds)) ? st.rounds : [];
  const open = (m) => {
    // open the (decided) winner's candidate, else the first competitor.
    const gen = m.winner || (Array.isArray(m.competitors) && m.competitors[0]) || null;
    if (gen) ctx.navigate('candidate', { epochId, gen });
  };
  const nodes = [];
  if (structure === 'double_elim') {
    // Split the rounds into the winners' / losers' bands by bracket_slot
    // prefix; the grand final (GF) rides with the winners' band.
    const wb = splitBand(rounds, (slot) => !slot.startsWith('LB'));
    const lb = splitBand(rounds, (slot) => slot.startsWith('LB'));
    const wbCard = el('div', { class: 'dn-panel dn-figpane' });
    wbCard.appendChild(wb.length ? svg.structureBracket({ rounds: wb, onMatch: open }) : empty('No winners-bracket matches yet.'));
    nodes.push(section('Winners’ bracket', wbCard));
    if (lb.length) {
      const lbCard = el('div', { class: 'dn-panel dn-figpane' });
      lbCard.appendChild(svg.structureBracket({ rounds: lb, onMatch: open }));
      nodes.push(section('Losers’ bracket', lbCard));
    }
  } else {
    const card = el('div', { class: 'dn-panel dn-figpane' });
    card.appendChild(rounds.length ? svg.structureBracket({ rounds, onMatch: open }) : empty('No bracket rounds recorded yet.'));
    nodes.push(section('Bracket · click a match to open the candidate', card));
  }
  const standings = standingsTable(st, ctx, epochId, !!(st && st.live));
  if (standings) nodes.push(section('Standings', standings));
  return nodes;
}

// Re-shape the structure rounds keeping only matches whose bracket_slot
// passes `keep`; drops any round left empty.
function splitBand(rounds, keep) {
  const out = [];
  for (const r of (Array.isArray(rounds) ? rounds : [])) {
    const matches = (Array.isArray(r.matches) ? r.matches : []).filter((m) => keep(String(m.bracket_slot || '')));
    if (matches.length) out.push({ round_index: r.round_index, label: r.label, matches });
  }
  return out;
}

// ---- swiss — a standings table (hero) + per-round pairings ---------

function renderSwiss(st, ctx, epochId) {
  const nodes = [];
  const standings = standingsTable(st, ctx, epochId, !!(st && st.live));
  const sCard = el('div', { class: 'dn-panel' });
  sCard.appendChild(standings || empty('No standings recorded yet.'));
  nodes.push(section('Standings · the Swiss leaderboard', sCard));

  const rounds = (st && Array.isArray(st.rounds)) ? st.rounds : [];
  const pCard = el('div', { class: 'dn-panel' });
  if (!rounds.length) {
    pCard.appendChild(empty('No rounds paired yet.'));
  } else {
    for (const r of rounds) {
      pCard.appendChild(el('div', { class: 'dt-swiss-round-h', text: r.label || `Round ${(r.round_index || 0) + 1}` }));
      const matches = Array.isArray(r.matches) ? r.matches : [];
      const tbl = el('table', { class: 'dn-board-table dt-swiss-pairings' });
      tbl.appendChild(el('thead', null, [el('tr', null, [
        el('th', { text: 'pairing' }), el('th', { text: 'winner' }), el('th', { class: 'dn-num', text: 'Δ scalar' }),
      ])]));
      const tbody = el('tbody');
      for (const m of matches) {
        const comps = Array.isArray(m.competitors) ? m.competitors : [];
        const delta = svg.isNum(m.delta_scalar) ? m.delta_scalar : null;
        tbody.appendChild(el('tr', null, [
          el('td', { class: 'dn-mono' }, [
            linkGen(comps[0], ctx, epochId), comps.length > 1 ? el('span', { class: 'dn-faint', text: ' vs ' }) : null, comps.length > 1 ? linkGen(comps[1], ctx, epochId) : null,
          ].filter(Boolean)),
          el('td', { class: 'dn-mono', text: m.winner || (m.bye ? 'bye' : '—') }),
          el('td', { class: 'dn-num dn-mono ' + (delta > 0 ? 'dn-bad-t' : delta < 0 ? 'dn-good-t' : ''), text: svg.isNum(delta) ? svg.fmtSigned(delta, 1) : '—' }),
        ]));
      }
      tbl.appendChild(tbody);
      pCard.appendChild(tbl);
    }
  }
  nodes.push(section('Pairings · round by round', pCard));
  return nodes;
}

// ---- racing — a successive-halving rung ladder ---------------------

function renderRacing(st, ctx, epochId) {
  const nodes = [];
  const live = !!(st && st.live);
  const rounds = (st && Array.isArray(st.rounds)) ? st.rounds : [];
  // the eventual champion = the last id in champion_lineage when present, else
  // the lone survivor of the final rung. (During a LIVE run the gate has not
  // committed, so we crown the final-rung survivor purely for the ladder mark.)
  const finalSurvivors = (() => {
    for (let i = rounds.length - 1; i >= 0; i--) {
      const m = (Array.isArray(rounds[i].matches) && rounds[i].matches[0]) ? rounds[i].matches[0] : {};
      const s = Array.isArray(m.survivors) ? m.survivors : [];
      if (s.length) return s;
    }
    return [];
  })();
  const championId = finalSurvivors.length === 1 ? finalSurvivors[0] : null;
  // each racing round has ONE match whose competitors/survivors/cut carry
  // the rung; flatten to a rung list for the ladder mark.
  const rungs = rounds.map((r) => {
    const m = (Array.isArray(r.matches) && r.matches[0]) ? r.matches[0] : {};
    return {
      label: r.label || `Rung ${(r.round_index || 0) + 1}`,
      match_id: m.match_id,
      competitors: Array.isArray(m.competitors) ? m.competitors : [],
      survivors: Array.isArray(m.survivors) ? m.survivors : [],
      cut: Array.isArray(m.cut) ? m.cut : [],
      board_fraction: svg.isNum(m.board_fraction) ? m.board_fraction : null,
      // a rung with NO recorded survivors/cut yet is still in flight — the mark
      // shows its field as racing (neutral), never as eliminated.
      pending: !(Array.isArray(m.survivors) && m.survivors.length) && !(Array.isArray(m.cut) && m.cut.length),
    };
  });
  const card = el('div', { class: 'dn-panel dn-figpane' });
  card.appendChild(rungs.length
    ? svg.racingLadder({
        rungs, championId, live,
        onCompetitor: (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); },
      })
    : empty(live ? 'The race is being seeded — the first rung fills in as runs land.' : 'No rungs evaluated yet.'));
  if (rungs.length) {
    card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      'each rung races the field on a fraction of the board, then cuts the worst by η · ✕ = cut · ↑ = survives · ♛ = final survivor → champion-gate · click a competitor → open'
      + (live ? ' · LIVE — the eventual winner is not committed until the final gate' : '') }));
  }
  nodes.push(section(live ? 'Racing ladder · LIVE — rungs, board fractions, cuts' : 'Racing ladder · rungs, board fractions, cuts', card));
  const standings = standingsTable(st, ctx, epochId, live);
  if (standings) nodes.push(section('Standings', standings));
  return nodes;
}

// ---- shared: the standings leaderboard table -----------------------

function standingsTable(st, ctx, epochId, live) {
  const standings = (st && Array.isArray(st.standings)) ? st.standings.slice() : [];
  if (!standings.length) return null;
  standings.sort((a, b) => (svg.isNum(a.rank) ? a.rank : 1e9) - (svg.isNum(b.rank) ? b.rank : 1e9));
  const tbl = el('table', { class: 'dn-board-table dt-standings' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', { text: 'rank' }), el('th', { text: 'generation' }), el('th', { text: 'status' }),
    el('th', { class: 'dn-num', text: 'scalar' }), el('th', { class: 'dn-num', text: 'W' }), el('th', { class: 'dn-num', text: 'L' }), el('th', { text: '' }),
  ])]));
  const tbody = el('tbody');
  for (const s of standings) {
    let status = String(s.status || '').toLowerCase();
    // LIVE — the verdicts have not committed; a standing tagged champion /
    // eliminated mid-run is the EVENTUAL outcome read from a half-finished
    // record. Treat everyone as still racing so nobody is mislabeled.
    if (live && (status === 'champion' || status === 'eliminated')) status = 'racing';
    const rowCls = status === 'champion' ? 'dn-board-champ' : status === 'eliminated' ? 'dt-standings-out' : '';
    tbody.appendChild(el('tr', { class: rowCls }, [
      el('td', { class: 'dn-mono', text: svg.isNum(s.rank) ? String(s.rank) : '—' }),
      el('td', { class: 'dn-mono', text: (s.generation_id || '—') + (status === 'champion' ? ' ♛' : '') }),
      el('td', null, [statusPill(status)]),
      el('td', { class: 'dn-num dn-mono', text: svg.isNum(s.scalar) ? svg.fmt(s.scalar, 1) : '—' }),
      el('td', { class: 'dn-num dn-mono', text: svg.isNum(s.wins) ? String(s.wins) : '—' }),
      el('td', { class: 'dn-num dn-mono', text: svg.isNum(s.losses) ? String(s.losses) : '—' }),
      el('td', null, [s.generation_id ? el('a', { class: 'dn-linkbtn', href: ctx.href('candidate', { epochId, gen: s.generation_id }), text: 'open →' }) : null].filter(Boolean)),
    ]));
  }
  tbl.appendChild(tbody);
  return tbl;
}

function statusPill(status) {
  const s = status || 'alive';
  // map the standings vocabulary onto verdict-pill semantics so the pill
  // reads in every theme: champion→promoted, eliminated→rejected, else→deferred
  // (alive / racing — still in contention).
  const verdict = s === 'champion' ? 'promoted' : s === 'eliminated' ? 'rejected' : 'deferred';
  const pill = verdictPill(verdict);
  pill.textContent = s;
  return pill;
}

function linkGen(gen, ctx, epochId) {
  if (!gen) return el('span', { class: 'dn-faint', text: 'bye' });
  return el('a', { class: 'dn-linkbtn dn-mono', href: ctx.href('candidate', { epochId, gen }), text: String(gen) });
}

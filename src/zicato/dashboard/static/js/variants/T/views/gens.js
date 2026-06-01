// variants/T/views/gens.js — GENERATIONS group landing.
//
// The detail pane for the tree's "Generations" group. Console IV folds in
// Variant W's ARENA standings as the hero of this page:
//
//   * a CHAMPION-DEFENDS banner — the reigning champion id · loss · N title
//     defences · a promoted badge;
//   * a RESPONSIVE WRAPPING GRID of COMPACT challenger MATCH CARDS (one per
//     challenger round): `<challenger> vs <champion>` · verdict pill · Δscalar
//     · a ONE-LINE (truncated) hypothesis · the decisive-driver judge · a status
//     link (dead-branch / promoted) that opens the candidate.
//
// The match cards stay compact (the full hypothesis lives on the candidate
// page), so the grid wraps tidily for 3 OR ~30 generations. Below the cards the
// dense ROSTER table is retained for the at-a-glance scan + Δ-vs-champion. Both
// the cards and the roster double as navigation — opening a candidate's detail.
// These match cards appear here (and on the epoch reel's siblings) but NEVER on
// the environment / workspace view.
//
// Data: /api/epoch, /api/lineage, /api/tournaments, /api/score-trajectory,
// /api/round/{e}/{champ}/{chall}/gate.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, verdictPill, normaliseDecision } from '../ui.js';
import { renderStructure, structurePill, structureDigest, isNonGauntlet, normalizeStructure } from './structure.js';
import { deriveLiveStatus } from '../livestatus.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading generations…' }));
  const epochId = (params && params.epochId) || null;

  const [ep, lin, traj, bracket] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory(), D.bracket()]);
  const id = epochId || (ep && ep.epoch_id) || null;
  if (!id) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Generations' }), empty('No epoch selected.')]);
    return;
  }

  // The CONFIGURED tournament structure for this epoch (§3.1). Default to
  // gauntlet — the existing ladder render below — when the contract names
  // no structure (every gauntlet epoch on disk today). For a non-gauntlet
  // structure, fetch the full structure state and render the real
  // bracket / standings / racing ladder instead of the gauntlet ladder.
  const tournament = (ep && ep.tournament && typeof ep.tournament === 'object') ? ep.tournament : null;
  let structure = (tournament && tournament.structure) || 'gauntlet';
  // a LIVE non-gauntlet run governs the dispatch even if the epoch contract
  // has not yet recorded its structure block (the active-tournament names it).
  const liveStruct = (state.activeTournament && state.activeTournament.structure) || null;
  const liveActive = deriveLiveStatus({
    heartbeat: state.heartbeat, activeRuns: state.activeRuns, activeTournament: state.activeTournament,
  }).running;
  if (structure === 'gauntlet' && liveActive && liveStruct && isNonGauntlet(liveStruct)) structure = liveStruct;
  if (isNonGauntlet(structure)) {
    await renderConfiguredStructure(host, ctx, id, ep, bracket, structure, tournament && tournament.params);
    return;
  }
  const experiments = (ep && Array.isArray(ep.experiments)) ? ep.experiments : [];
  const gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' }));

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  const champ = gens.find((g) => g.promoted) || gens.find((g) => !g.parent) || null;
  const championId = champ ? champ.id : null;
  const champScalar = championId ? scalarByGen.get(championId) : null;

  // the match-ups (the actual gauntlet rounds), round-ordered by ran_at.
  const matchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups.slice() : [];
  matchups.sort((a, b) => String(a.ran_at || '').localeCompare(String(b.ran_at || '')));
  // resolve each round's gate (cached) for the decisive-driver line.
  const gates = await Promise.all(matchups.map((m) => (m.champion && m.challenger)
    ? D.gate(id, m.champion, m.challenger) : Promise.resolve(null)));
  const promotedCount = gens.filter((g) => g.promoted).length;

  const digest = JSON.stringify({
    id, championId,
    champScalar: svg.isNum(champScalar) ? champScalar.toFixed(3) : null,
    rounds: matchups.map((m, i) => [m.champion, m.challenger, m.decision,
      svg.isNum(m.delta_scalar) ? m.delta_scalar.toFixed(2) : null,
      (m.hypothesis_core_idea || '').slice(0, 90),
      gates[i] && gates[i].primary_driver ? gates[i].primary_driver.judge : null]),
    gens: gens.map((g) => [g.id, g.parent, g.promoted, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: `Generations · ${id}` }),
      el('p', { class: 'dn-lede', text: 'Every candidate in this epoch. Open one for its lifecycle, promote gate, all match-ups, per-board scoring, and patch diff.' }),
    ]));

    // ── the champion-defends banner (adopted from W) ──────────────────
    nodes.push(championBanner(championId, champScalar, matchups.length, gens.length, promotedCount, ctx, id));

    // ── the responsive wrapping grid of compact match cards ───────────
    const cards = el('div', { class: 'dt-matchcards' });
    if (!matchups.length) {
      cards.appendChild(empty('No challenger has entered the ring yet — the seed champion stands undefeated.'));
    } else {
      matchups.forEach((m, i) => cards.appendChild(matchCard(m, gates[i], ctx, id)));
    }
    nodes.push(section('Match-ups · the champion defends · one compact card per challenger round', cards));

    // ── the dense roster table (retained for the at-a-glance scan) ────
    const tblCard = el('div', { class: 'dn-panel' });
    if (!gens.length) {
      tblCard.appendChild(empty('No generations recorded for this epoch.'));
    } else {
      const tbl = el('table', { class: 'dn-board-table' });
      tbl.appendChild(el('thead', null, [el('tr', null, [
        el('th', { text: 'generation' }), el('th', { text: 'role' }), el('th', { text: 'parent' }),
        el('th', { class: 'dn-num', text: 'scalar (loss)' }), el('th', { class: 'dn-num', text: 'Δ vs champion' }), el('th', { text: '' }),
      ])]));
      const tbody = el('tbody');
      for (const g of gens) {
        const sc = scalarByGen.get(g.id);
        const baseline = !g.parent;
        const decision = baseline ? 'baseline' : (g.promoted ? 'promoted' : 'rejected');
        const delta = (svg.isNum(sc) && svg.isNum(champScalar) && !baseline) ? sc - champScalar : null;
        tbody.appendChild(el('tr', { class: g.promoted ? 'dn-board-champ' : '' }, [
          el('td', { class: 'dn-mono', text: g.id + (g.promoted ? ' ♛' : '') }),
          el('td', null, [verdictPill(decision)]),
          el('td', { class: 'dn-mono', text: g.parent || 'seed' }),
          el('td', { class: 'dn-num dn-mono', text: svg.isNum(sc) ? svg.fmt(sc, 1) : '—' }),
          el('td', { class: 'dn-num dn-mono ' + (delta > 0 ? 'dn-bad-t' : delta < 0 ? 'dn-good-t' : ''), text: svg.isNum(delta) ? svg.fmtSigned(delta, 1) : '—' }),
          el('td', null, [el('a', { class: 'dn-linkbtn', href: ctx.href('candidate', { epochId: id, gen: g.id }), text: 'open →' })]),
        ]));
      }
      tbl.appendChild(tbody);
      tblCard.appendChild(tbl);
    }
    nodes.push(section('Roster · click a candidate to open its detail', tblCard));
    return nodes;
  });
}

// Render the ACTUAL configured (non-gauntlet) structure.
//
// LIVE-FIRST: when a run is in flight (the structure-agnostic live status from
// the heartbeat / active-runs / active-tournament), prefer the LIVE
// /api/active-tournament topology — so the ladder fills in rung-by-rung and
// the in-flight competitors are NOT mislabeled "rejected/eliminated" (the
// promote decision only commits at the very end, so a completed record read
// mid-run would crown the eventual winner prematurely). When idle, fall back
// to the COMPLETED record: pick the epoch's most-recent tournament from the
// `tournaments[]` array on /api/tournaments and fetch its full structure
// state; when that is empty (a pre-feature index) derive the crowning-pair id
// so a completed tournament still resolves via the loss-file fallback chain.
async function renderConfiguredStructure(host, ctx, id, ep, bracket, structure, params) {
  // is a run live right now? (the same structure-agnostic verdict the chrome reads)
  const status = deriveLiveStatus({
    heartbeat: state.heartbeat, activeRuns: state.activeRuns, activeTournament: state.activeTournament,
  });
  // the LIVE topology (full {structure,phase,competitors,rounds,standings}).
  const liveRaw = status.running ? await D.activeTournament() : null;
  const liveSt = normalizeStructure(liveRaw, true);
  const liveUsable = !!(liveSt && liveSt.live);

  // the COMPLETED record (only fetched when we are NOT showing the live one).
  let tournamentId = null;
  let st = null;
  if (!liveUsable) {
    const tournaments = (bracket && Array.isArray(bracket.tournaments)) ? bracket.tournaments : [];
    const nonGaunt = tournaments.filter((t) => t && t.structure && t.structure !== 'gauntlet');
    if (nonGaunt.length) tournamentId = nonGaunt[nonGaunt.length - 1].tournament_id;
    else if (tournaments.length) tournamentId = tournaments[tournaments.length - 1].tournament_id;
    else {
      const matchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups : [];
      const last = matchups[matchups.length - 1];
      if (last && last.challenger) tournamentId = `${id}:${last.champion || ''}->${last.challenger}`;
    }
    st = tournamentId ? normalizeStructure(await D.tournamentStructure(id, tournamentId), false) : null;
  }

  const shown = liveUsable ? liveSt : st;
  const shownStructure = (shown && shown.structure) || structure;
  const digest = JSON.stringify({
    id, structure: shownStructure, tournamentId, live: liveUsable, st: structureDigest(shown),
  });
  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: `Match-ups · ${id}` }),
      el('div', { class: 'dt-structure-line' }, [
        structurePill(shownStructure, (shown && shown.structure_params) || params),
        liveUsable ? el('span', { class: 'dt-live-pill', text: 'LIVE' }) : null,
      ].filter(Boolean)),
      el('p', { class: 'dn-lede', text: liveUsable
        ? 'A run is in flight — the live tournament fills in as runs land. In-flight competitors are shown racing, not rejected; the winner is not committed until the final gate.'
        : 'The configured tournament structure for this epoch. Open a match or competitor for its candidate detail, promote gate, per-board scoring, and patch diff.' }),
    ]));
    if (!shown) {
      nodes.push(empty(status.running
        ? 'A run is starting — the live tournament topology is not available yet.'
        : (tournamentId ? 'The tournament structure is unavailable (the index may not be built).'
                        : 'No tournament has run for this structure yet.')));
      return nodes;
    }
    for (const n of renderStructure(shown, ctx, id)) nodes.push(n);
    return nodes;
  });
}

// The champion-defends banner: champion id · loss · N title defences · promoted.
function championBanner(championId, champScalar, defended, total, promoted, ctx, epochId) {
  if (!championId) {
    return el('div', { class: 'dt-champ-banner dt-champ-empty' }, [
      el('span', { class: 'dn-faint', text: 'No reigning champion yet — the seed has not been challenged.' }),
    ]);
  }
  return el('a', {
    class: 'dt-champ-banner', href: ctx.href('candidate', { epochId, gen: championId }),
    'aria-label': 'Champion ' + championId + ' — open its detail',
  }, [
    el('span', { class: 'dt-champ-crown', 'aria-hidden': 'true', text: '♛' }),
    el('div', { class: 'dt-champ-body' }, [
      el('div', { class: 'dt-champ-rank', text: 'CHAMPION · defending the title' }),
      el('div', { class: 'dt-champ-id', text: championId }),
      el('div', { class: 'dt-champ-meta' }, [
        el('span', { text: 'loss ' + (svg.isNum(champScalar) ? svg.fmt(champScalar, 1) : '—') }),
        el('span', { text: defended + ' title defence' + (defended === 1 ? '' : 's') }),
        el('span', { text: total + ' generation' + (total === 1 ? '' : 's') + ' · ' + promoted + ' promoted' }),
      ]),
    ]),
    el('span', { class: 'dt-champ-pill' }, [verdictPill('promoted')]),
  ]);
}

// One compact challenger MATCH CARD: `<challenger> vs <champion>` · verdict ·
// Δscalar · a ONE-LINE (truncated) hypothesis · decisive-driver judge · status
// link. Clicking opens that challenger's candidate detail.
function matchCard(m, gate, ctx, epochId) {
  const dec = String(m.decision || (gate && gate.decision) || 'rejected').toLowerCase();
  const won = dec.includes('promot');
  const verdict = dec.includes('promot') ? 'promoted' : dec.includes('defer') ? 'deferred' : 'rejected';
  const delta = svg.isNum(m.delta_scalar) ? m.delta_scalar
    : (gate && svg.isNum(gate.delta_scalar) ? gate.delta_scalar : null);
  const driver = gate && gate.primary_driver && gate.primary_driver.judge ? gate.primary_driver.judge : null;
  const idea = m.hypothesis_core_idea ? String(m.hypothesis_core_idea) : null;

  return el('a', {
    class: 'dt-match-card' + (won ? ' dt-match-won' : ' dt-match-lost'),
    href: ctx.href('candidate', { epochId, gen: m.challenger }),
    'aria-label': 'Round ' + m.champion + ' vs ' + m.challenger + ' — open challenger ' + m.challenger,
  }, [
    el('div', { class: 'dt-match-head' }, [
      el('span', { class: 'dt-match-versus' }, [
        el('span', { class: 'dt-match-chall', text: m.challenger }),
        el('span', { class: 'dt-match-vs', text: 'vs' }),
        el('span', { class: 'dt-match-champ', text: m.champion }),
      ]),
      verdictPill(verdict),
    ]),
    el('div', { class: 'dt-match-score' }, [
      el('span', { class: 'dt-match-delta ' + (svg.isNum(delta) ? (delta > 0 ? 'dn-bad-t' : delta < 0 ? 'dn-good-t' : '') : ''),
        text: svg.isNum(delta) ? svg.fmtSigned(delta, 1) : '—' }),
      el('span', { class: 'dt-match-delta-k', text: 'Δ scalar' }),
    ]),
    idea
      ? el('div', { class: 'dt-match-idea', title: idea }, [
          el('span', { class: 'dt-match-idea-lead', text: 'Hypothesis. ' }),
          el('span', { class: 'dt-match-idea-txt', text: idea }),
        ])
      : el('div', { class: 'dt-match-idea dn-faint', text: 'No hypothesis recorded for this round.' }),
    driver ? el('div', { class: 'dt-match-driver dn-faint' }, [
      'decisive driver · ', el('span', { class: 'dn-mono', text: driver }),
    ]) : null,
    el('div', { class: 'dt-match-foot' }, [
      el('span', { class: 'dt-match-open', text: won ? 'new champion → open' : 'dead branch → open' }),
    ]),
  ].filter(Boolean));
}

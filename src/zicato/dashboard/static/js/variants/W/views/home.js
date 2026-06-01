// variants/W/views/home.js — the ARENA: broadcast STANDINGS + MATCH CARDS.
//
// Variant W's hero and home. The tournament rendered as a live broadcast:
//
//   * a billboard HEADER — epoch · the reigning champion (defending the title) ·
//     round count · live/idle status;
//   * the CHAMPION card at the top — the title-holder, "defending";
//   * one MATCH CARD per challenger round — Δscalar, a verdict pill, and the
//     hypothesis core idea — bound to /api/tournaments + /api/lineage + the
//     per-round /api/round/.../gate decision.
//
// The standings DOUBLE AS NAVIGATION: clicking a match card opens that
// challenger's candidate detail (its lifecycle / match-ups / promote gate).
// Clicking the champion card opens the champion's detail. Fit-to-width (NO
// pan/zoom), digest-gated (structural only). Cold deep-link safe.
//
// Bind: /api/tournaments + /api/lineage + /api/score-trajectory +
// /api/round/{e}/{champ}/{chall}/gate. The board the candidates faced is the
// fixed epoch board — the standings are the round results over it.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../../P/data.js';
import * as svg from '../../P/svg.js';
import { gatedSwap, section, empty, verdictPill, normaliseDecision } from '../ui.js';

export async function render(host, ctx) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Tuning the broadcast…' }));

  const [ep, lin, traj, bracket] = await Promise.all([
    D.epoch(), D.lineage(), D.scoreTrajectory(), D.bracket(),
  ]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Arena' }), empty('No current epoch — no tournament to broadcast yet.')]);
    return;
  }
  const epochId = ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' }));

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  const champ = gens.find((g) => g.promoted) || gens.find((g) => !g.parent) || null;
  const championId = champ ? champ.id : null;
  const championScalar = championId ? scalarByGen.get(championId) : null;

  // The match-ups (the actual gauntlet rounds), ordered by ran_at when present.
  const matchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups.slice() : [];
  matchups.sort((a, b) => String(a.ran_at || '').localeCompare(String(b.ran_at || '')));

  // Resolve each round's gate decision (cached) for the decisive driver line.
  const gates = await Promise.all(matchups.map((m) => (m.champion && m.challenger)
    ? D.gate(epochId, m.champion, m.challenger) : Promise.resolve(null)));

  const live = !!state.activeTournament;
  const board = Array.isArray(ep.board) ? ep.board : [];

  const digest = JSON.stringify({
    epochId, championId, live,
    champScalar: svg.isNum(championScalar) ? championScalar.toFixed(3) : null,
    boardN: board.length,
    rounds: matchups.map((m, i) => [m.champion, m.challenger, m.decision,
      svg.isNum(m.delta_scalar) ? m.delta_scalar.toFixed(2) : null,
      (m.hypothesis_core_idea || '').slice(0, 80),
      gates[i] && gates[i].primary_driver ? gates[i].primary_driver.judge : null]),
    gens: gens.map((g) => [g.id, g.parent, g.promoted, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];

    // ── the billboard header ──────────────────────────────────────────
    nodes.push(billboard(epochId, championId, championScalar, matchups.length, live, ep, board.length));

    // ── the standings: champion defends; challengers are match cards ──
    const standings = el('div', { class: 'dw-standings' });

    if (championId) {
      standings.appendChild(championCard(championId, championScalar, matchups.length, ctx, epochId));
    }

    const cards = el('div', { class: 'dw-matchcards' });
    if (!matchups.length) {
      cards.appendChild(empty('No challenger has entered the ring yet — the seed champion stands undefeated.'));
    } else {
      matchups.forEach((m, i) => cards.appendChild(matchCard(m, gates[i], scalarByGen, ctx, epochId, championId)));
    }
    standings.appendChild(cards);

    nodes.push(section('Standings · the champion defends · each challenger a match card', standings));

    // ── the cross-epoch trend, kept compact (the "season so far") ──
    const trendVals = gens.map((g) => scalarByGen.get(g.id)).filter(svg.isNum);
    if (trendVals.length >= 2) {
      const card = el('div', { class: 'dn-panel' });
      card.appendChild(svg.sparkline({ width: 760, height: 70, values: trendVals, band: true, goodDirection: 'down' }));
      card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'scalar (loss) per generation across the season · lower is better' }));
      nodes.push(section('Season trajectory', card));
    }
    return nodes;
  });
}

// The broadcast billboard: epoch, reigning champion, round count, status.
function billboard(epochId, championId, championScalar, rounds, live, ep, boardN) {
  return el('div', { class: 'dw-billboard' }, [
    el('div', { class: 'dw-bb-top' }, [
      el('span', { class: 'dw-bb-kicker', text: 'ZICATO · ARENA' }),
      el('span', { class: 'dw-bb-status dw-bb-' + (live ? 'live' : 'idle') }, [
        el('span', { class: 'dw-bb-status-dot', 'aria-hidden': 'true' }),
        el('span', { text: live ? 'ON AIR · tournament live' : 'between rounds' }),
      ]),
    ]),
    el('h1', { class: 'dw-bb-title', text: 'Epoch ' + epochId }),
    ep && ep.goal && ep.goal.trim()
      ? el('p', { class: 'dw-bb-goal', text: ep.goal }) : null,
    el('div', { class: 'dw-bb-stats' }, [
      bbStat(championId || '—', 'reigning champion', 'champ'),
      bbStat(svg.isNum(championScalar) ? svg.fmt(championScalar, 1) : '—', 'title loss (lower wins)'),
      bbStat(String(rounds), 'rounds fought'),
      bbStat(String(boardN), 'board entries'),
    ]),
  ].filter(Boolean));
}

function bbStat(value, key, tone) {
  return el('div', { class: 'dw-bb-stat' + (tone ? ' dw-bb-stat-' + tone : '') }, [
    el('span', { class: 'dw-bb-stat-v', text: value }),
    el('span', { class: 'dw-bb-stat-k', text: key }),
  ]);
}

// The champion "defending the title" — the top of the standings.
function championCard(championId, championScalar, defended, ctx, epochId) {
  return el('a', {
    class: 'dw-champ-card', href: ctx.href('candidate', { epochId, gen: championId }),
    'aria-label': 'Champion ' + championId + ' — open its detail',
  }, [
    el('div', { class: 'dw-champ-crown', 'aria-hidden': 'true', text: '♛' }),
    el('div', { class: 'dw-champ-body' }, [
      el('div', { class: 'dw-champ-rank', text: 'CHAMPION · defending the title' }),
      el('div', { class: 'dw-champ-id', text: championId }),
      el('div', { class: 'dw-champ-meta' }, [
        el('span', { class: 'dw-champ-loss', text: 'loss ' + (svg.isNum(championScalar) ? svg.fmt(championScalar, 1) : '—') }),
        el('span', { class: 'dw-champ-defended', text: defended + ' title defence' + (defended === 1 ? '' : 's') }),
      ]),
    ]),
    el('div', { class: 'dw-champ-pill' }, [verdictPill('promoted')]),
  ]);
}

// One challenger MATCH CARD: Δscalar, verdict, the hypothesis core idea. Clicking
// opens that challenger's candidate detail (standings double as navigation).
function matchCard(m, gate, scalarByGen, ctx, epochId, championId) {
  const dec = String(m.decision || (gate && gate.decision) || 'rejected').toLowerCase();
  const won = dec.includes('promot');
  const delta = svg.isNum(m.delta_scalar) ? m.delta_scalar
    : (gate && svg.isNum(gate.delta_scalar) ? gate.delta_scalar : null);
  const driver = gate && gate.primary_driver && gate.primary_driver.judge ? gate.primary_driver.judge : null;
  const idea = m.hypothesis_core_idea ? String(m.hypothesis_core_idea) : null;

  const card = el('a', {
    class: 'dw-match-card' + (won ? ' dw-match-won' : ' dw-match-lost'),
    href: ctx.href('candidate', { epochId, gen: m.challenger }),
    'aria-label': 'Round ' + m.champion + ' vs ' + m.challenger + ' — open challenger ' + m.challenger,
  }, [
    el('div', { class: 'dw-match-head' }, [
      el('span', { class: 'dw-match-versus' }, [
        el('span', { class: 'dw-match-champ', text: m.champion }),
        el('span', { class: 'dw-match-vs', text: 'vs' }),
        el('span', { class: 'dw-match-chall', text: m.challenger }),
      ]),
      verdictPill(dec.includes('promot') ? 'promoted' : dec.includes('defer') ? 'deferred' : 'rejected'),
    ]),
    el('div', { class: 'dw-match-score' }, [
      el('span', { class: 'dw-match-delta ' + (svg.isNum(delta) ? (delta > 0 ? 'dn-bad-t' : delta < 0 ? 'dn-good-t' : '') : ''),
        text: svg.isNum(delta) ? svg.fmtSigned(delta, 1) : '—' }),
      el('span', { class: 'dw-match-delta-k', text: 'Δ scalar vs champion' }),
    ]),
    idea ? el('div', { class: 'dw-match-idea' }, [
      el('span', { class: 'dw-match-idea-lead', text: 'Hypothesis. ' }),
      el('span', { text: idea }),
    ]) : el('div', { class: 'dw-match-idea dn-faint', text: 'No hypothesis recorded for this round.' }),
    driver ? el('div', { class: 'dw-match-driver dn-faint' }, [
      'decisive driver · ', el('span', { class: 'dn-mono', text: driver }),
    ]) : null,
    el('div', { class: 'dw-match-foot' }, [
      el('span', { class: 'dw-match-open', text: won ? 'new champion → open' : 'dead branch → open' }),
    ]),
  ].filter(Boolean));
  return card;
}

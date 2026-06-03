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
import { gatedSwap, section, empty, verdictPill, normaliseDecision, decisionFor } from '../ui.js';
import { renderStructure, structurePill, structureDigest, isNonGauntlet, normalizeStructure, reconstructRacing, buildLiveRacingModel, buildLiveSwissModel, buildLiveElimModel } from './structure.js';
import { deriveLiveStatus } from '../livestatus.js';
import { epochRoundModel, roundModelDigest } from './rounds.js';

// Does the LIVE run (active tournament / heartbeat) belong to the epoch being
// VIEWED? The live topology is the ACTIVE epoch's — adopting it under a
// different epoch's header (e.g. a closed e0 while e1 races) is BUG 2. Keyed
// off the active tournament's epoch_id, falling back to the heartbeat's epoch
// id. When NEITHER live signal carries an epoch tag it is a legacy
// single-epoch payload, so we trust it for the viewed epoch (mirrors
// views/epoch.js's `liveForThisEpoch` guard). A run must also actually be
// running for this to be true.
function liveBelongsToEpoch(epochId) {
  const running = deriveLiveStatus({
    heartbeat: state.heartbeat, activeRuns: state.activeRuns, activeTournament: state.activeTournament,
  }).running;
  if (!running) return false;
  const at = state.activeTournament;
  const hb = state.heartbeat;
  const atEpoch = (at && at.epoch_id != null) ? at.epoch_id : null;
  const hbEpoch = (hb && hb.epoch_id != null) ? hb.epoch_id : null;
  if (atEpoch != null) return String(atEpoch) === String(epochId);
  if (hbEpoch != null) return String(hbEpoch) === String(epochId);
  return true; // no epoch tag ⇒ legacy single-epoch payload, trust it.
}

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading generations…' }));
  const epochId = (params && params.epochId) || null;

  // Class A: scope every read to the VIEWED epoch (route param first).
  const ep = await D.epoch(epochId);
  const id = epochId || (ep && ep.epoch_id) || null;
  if (!id) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Generations' }), empty('No epoch selected.')]);
    return;
  }
  const [rows, traj, bracket] = await Promise.all([
    D.generationsForEpoch(id), D.scoreTrajectory(id), D.bracket(id),
  ]);

  // ── ROUND DRILL-DOWN (Task 4): the epoch timeline indexes rounds; a `round`
  // param scopes THIS view to ONE evolve round's tournament. We render the
  // selected round's field-tournament (its bracket tree / swiss ladder / racing
  // ladder + the per-round flow + per-board scoring) via renderStructure. The
  // full (all-rounds) Match-ups view is unchanged when no round is selected.
  const roundParam = (params && params.round != null) ? params.round : null;
  if (roundParam != null) {
    await renderRoundDrilldown(host, ctx, id, ep, bracket, traj, rows, roundParam);
    return;
  }

  // The CONFIGURED tournament structure for this epoch (§3.1). Default to
  // gauntlet — the existing ladder render below — when the contract names
  // no structure (every gauntlet epoch on disk today). For a non-gauntlet
  // structure, fetch the full structure state and render the real
  // bracket / standings / racing ladder instead of the gauntlet ladder.
  const tournament = (ep && ep.tournament && typeof ep.tournament === 'object') ? ep.tournament : null;
  let structure = (tournament && tournament.structure) || 'gauntlet';
  // THE PER-EPOCH LIVE GUARD. The live topology (active-tournament / heartbeat /
  // active-runs) belongs to the ACTIVE epoch only. We treat the run as live FOR
  // THIS VIEW only when the epoch on screen IS the active one — keyed off the
  // active tournament's epoch_id (and/or the heartbeat's, when it carries one).
  // Viewing a NON-active (e.g. closed e0) epoch while a different epoch (e1)
  // races must render e0's COMPLETED structure, never e1's live "being seeded"
  // ladder leaking onto e0. (epoch.js already guards its racing funnel the same
  // way.) When the live signals carry NO epoch tag it is a legacy single-epoch
  // payload — trust it for the viewed epoch.
  const isLiveForThisEpoch = liveBelongsToEpoch(id);
  // a LIVE non-gauntlet run governs the dispatch even if the epoch contract
  // has not yet recorded its structure block (the active-tournament names it) —
  // but ONLY when that live run is for the epoch being viewed.
  const liveStruct = (state.activeTournament && state.activeTournament.structure) || null;
  if (structure === 'gauntlet' && isLiveForThisEpoch && liveStruct && isNonGauntlet(liveStruct)) structure = liveStruct;
  if (isNonGauntlet(structure)) {
    await renderConfiguredStructure(host, ctx, id, ep, bracket, structure, tournament && tournament.params, isLiveForThisEpoch);
    return;
  }
  const experiments = (ep && Array.isArray(ep.experiments)) ? ep.experiments : [];
  const gens = rows.length
    ? rows.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: g.promoted == null ? null : !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' ? true : (normaliseDecision(x.outcome) === 'rejected' ? false : null) }));

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

    // ── the round's structure-flow graphic (gauntlet → duel flow) ─────
    // The field as Δ-vs-champion lanes (replaces the boxed banner + match
    // cards): each challenger a lane vs the crowned champion-gate, Δ encoded
    // good-below / bad-above the reference rule, status as a glyph; the per-
    // challenger hypothesis + exact Δ on HOVER. The champion summary is a
    // compact accent header integrated above the figure, not a boxed banner.
    nodes.push(section('Field · the champion defends · Δ-vs-champion lanes (hover for the hypothesis + Δ)',
      fieldFlow(championId, champScalar, matchups, gates, gens.length, promotedCount, ctx, id)));

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
        // Class B: an unscored candidate is PENDING, not rejected.
        const decision = decisionFor({ promoted: g.promoted, parent: g.parent });
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

// ── ONE round's tournament — the round drill-down (Task 4) ──────────
//
// The epoch timeline (views/epoch.js) indexes the rounds along the champion
// spine; clicking a round routes here with `?round`. We render JUST that
// round's tournament: for a non-gauntlet structure, the round's field record
// → renderStructure (the bracket tree / swiss ladder / racing ladder + the
// per-round flow + per-board scoring); for gauntlet, the round's match card(s).
// Degrades when round_index is absent (the round model falls back to field
// records / matchups / a single round 0), and when the selected round is out of
// range it reads as an honest empty.
async function renderRoundDrilldown(host, ctx, id, ep, bracket, traj, rows, roundParam) {
  const tournament = (ep && ep.tournament && typeof ep.tournament === 'object') ? ep.tournament : null;
  const structure = (tournament && tournament.structure) || 'gauntlet';
  const experiments = (ep && Array.isArray(ep.experiments)) ? ep.experiments : [];
  const gens = rows.length
    ? rows.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: g.promoted == null ? null : !!g.promoted, round_index: svg.isNum(g.round_index) ? g.round_index : null }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' ? true : (normaliseDecision(x.outcome) === 'rejected' ? false : null), round_index: svg.isNum(x.round_index) ? x.round_index : null }));
  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  const championId = (gens.find((g) => g.promoted) || gens.find((g) => !g.parent) || {}).id || null;

  const epochRounds = epochRoundModel({ gens, scalarBy: scalarByGen, bracket, structure, championId });
  const want = String(roundParam);
  const round = epochRounds.find((r) => String(r.round_index) === want) || null;

  // normalize the round's own field-tournament record (non-gauntlet only).
  const st = (round && round.tournamentRef)
    ? normalizeStructure({
        structure: round.tournamentRef.structure || structure,
        structure_params: round.tournamentRef.structure_params || (tournament && tournament.params) || {},
        competitors: round.tournamentRef.competitors, rounds: round.tournamentRef.rounds,
        standings: round.tournamentRef.standings,
        champion_lineage: bracket && bracket.champion_lineage, source: 'index',
      }, false)
    : null;

  // the gauntlet match-ups for this round's challengers (no field record).
  const matchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups : [];
  const challengerSet = round ? new Set(round.challengers.map((c) => String(c.id))) : new Set();
  const roundMatchups = matchups.filter((m) => challengerSet.has(String(m.challenger)));

  const digest = JSON.stringify({
    id, roundParam, structure, rounds: roundModelDigest(epochRounds),
    st: st ? structureDigest(st) : null,
    roundMatchups: roundMatchups.map((m) => [m.champion, m.challenger, m.decision, svg.isNum(m.delta_scalar) ? m.delta_scalar.toFixed(2) : null]),
  });
  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: `Match-ups · ${id} · round ${roundParam}` }),
      el('div', { class: 'dt-structure-line' }, [structurePill(structure, (tournament && tournament.params) || (st && st.structure_params))]),
      el('p', { class: 'dn-lede', text: 'One evolve round of this epoch: its incoming champion, the field minted that round, the tournament, and the gate. The epoch timeline indexes every round; this view shows ONE.' }),
      el('a', { class: 'dn-linkbtn', href: ctx.href('gens', { epochId: id }), text: '← all rounds' }),
    ]));
    if (!round) {
      nodes.push(empty(`No round ${roundParam} in this epoch (the timeline ran fewer rounds).`));
      return nodes;
    }
    const roundChampId = round.champion ? round.champion.id : championId;
    const roundChampScalar = round.champion ? round.champion.scalar : null;
    if (st) {
      // a non-gauntlet round: render its full tournament structure.
      for (const n of renderStructure(st, ctx, id)) nodes.push(n);
    } else if (roundMatchups.length) {
      // a gauntlet round: the field as Δ-vs-champion lanes (structure-flow).
      nodes.push(section('Field · this round · Δ-vs-champion lanes',
        fieldFlow(roundChampId, roundChampScalar, roundMatchups, roundMatchups.map(() => null),
          round.challengers.length, round.gateOutcome && round.gateOutcome.kind === 'promoted' ? 1 : 0, ctx, id)));
    } else {
      // no tournament record nor matchups → the minted field as duel lanes.
      const synthMatchups = round.challengers.map((c) => ({
        champion: roundChampId, challenger: c.id,
        decision: c.promoted ? 'promoted' : null, delta_scalar: null,
      }));
      nodes.push(section('Field minted this round',
        round.challengers.length
          ? fieldFlow(roundChampId, roundChampScalar, synthMatchups, synthMatchups.map(() => null),
              round.challengers.length, round.gateOutcome && round.gateOutcome.kind === 'promoted' ? 1 : 0, ctx, id)
          : empty('No challengers minted this round.')));
    }
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
async function renderConfiguredStructure(host, ctx, id, ep, bracket, structure, params, isLiveForThisEpoch) {
  // is a run live right now? (the same structure-agnostic verdict the chrome
  // reads) — but adopting the LIVE topology is gated on the run belonging to
  // the epoch ON SCREEN (BUG 2). When viewing a non-active epoch we never read
  // the live active-tournament; we fall through to the COMPLETED record below,
  // so a closed e0 shows its own bracket and never e1's live "being seeded"
  // ladder. The caller passes `isLiveForThisEpoch`; recompute defensively when
  // it is omitted (a direct call).
  const liveForThisEpoch = isLiveForThisEpoch == null ? liveBelongsToEpoch(id) : !!isLiveForThisEpoch;
  const status = deriveLiveStatus({
    heartbeat: state.heartbeat, activeRuns: state.activeRuns, activeTournament: state.activeTournament,
  });
  // the LIVE topology (full {structure,phase,competitors,rounds,standings}) —
  // adopted ONLY for the active epoch's view.
  const liveRaw = (liveForThisEpoch && status.running) ? await D.activeTournament() : null;
  // RACING — the live active-tournament `rounds` are EMPTY until each matchup
  // COMPLETES, so a plain normalize would yield no rungs and the ladder would
  // sit on the "being seeded" empty state for the whole race. Build a
  // PROGRESSIVE, accumulating model from the field (competitors) + the
  // heartbeat's active rung + per-gen board progress (/api/active-runs) +
  // partial aggregates + any completed rounds, so the ladder fills rung-by-rung
  // and never discards a finished rung. The epoch gen scope is the live field
  // itself (only the field's gens race), so foreign in-flight runs are excluded.
  // SWISS + ELIM share racing's problem: the live `rounds` carry pairings whose
  // winner only lands as boards complete, so a plain normalize shows empty/being-
  // seeded pairings. The progressive builders accumulate completed rounds, fill
  // the active round board-by-board, and queue the future (racing's discipline).
  let liveSt;
  const liveStructure = liveRaw ? String(liveRaw.structure) : null;
  if (liveRaw && (liveStructure === 'racing' || liveStructure === 'swiss'
      || liveStructure === 'single_elim' || liveStructure === 'double_elim')) {
    const epochGens = (Array.isArray(liveRaw.competitors) ? liveRaw.competitors : [])
      .map((c) => c && c.generation_id).filter((g) => g != null).map(String);
    const args = {
      at: liveRaw, heartbeat: state.heartbeat, activeRuns: state.activeRuns,
      epochGens: epochGens.length ? epochGens : null,
    };
    const built = liveStructure === 'racing' ? buildLiveRacingModel(args)
      : liveStructure === 'swiss' ? buildLiveSwissModel(args)
      : buildLiveElimModel(args);
    liveSt = built || normalizeStructure(liveRaw, true);
  } else {
    liveSt = normalizeStructure(liveRaw, true);
  }
  const liveUsable = !!(liveSt && liveSt.live);

  // the COMPLETED record (only fetched/reconstructed when NOT showing live).
  let tournamentId = null;
  let st = null;
  if (!liveUsable) {
    // RACING is persisted as ONE record PER CHALLENGER — the per-tournament
    // structure fetch only sees a single challenger's flattened rounds, which
    // cannot rebuild the rung/field/cut/survivor ladder. Aggregate every
    // racing record on /api/tournaments and group matches by their `match_id`
    // rung prefix to reconstruct the whole ladder + champion-gate outcome.
    if (structure === 'racing') {
      st = reconstructRacing(bracket, id);
      tournamentId = st ? 'racing:reconstructed' : null;
    }
    if (!st) {
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
      nodes.push(empty((liveForThisEpoch && status.running)
        ? 'A run is starting — the live tournament topology is not available yet.'
        : (tournamentId ? 'The tournament structure is unavailable (the index may not be built).'
                        : 'No completed tournament is recorded for this structure — any minted field appears on the epoch’s round timeline, but no bracket matches were committed (the run was torn down first).')));
      return nodes;
    }
    for (const n of renderStructure(shown, ctx, id)) nodes.push(n);
    return nodes;
  });
}

// The gauntlet FIELD as a structure-flow GRAPHIC (replaces the boxed champion
// banner + the per-challenger match cards). A compact ACCENT header integrated
// above the duel-flow figure carries the champion summary; the figure itself is
// `svg.duelFlow` — each challenger a lane vs the crowned champion-gate, its
// Δ-vs-champion encoded good-below / bad-above the reference rule, status as a
// glyph (↑ promoted / ✕ cut / ○ pending). The per-challenger hypothesis + exact
// Δ live ON HOVER. Clicking a lane opens that challenger's candidate.
function fieldFlow(championId, champScalar, matchups, gates, total, promoted, ctx, epochId) {
  const wrap = el('div', { class: 'dn-panel dn-figpane dt-fieldflow' });

  // the compact accent champion header — integrated with the graphic, not a box.
  const head = el('div', { class: 'dt-fieldflow-head' });
  if (championId) {
    head.appendChild(el('a', {
      class: 'dt-fieldflow-champ', href: ctx.href('candidate', { epochId, gen: championId }),
      'aria-label': 'Champion ' + championId + ' — open its detail',
    }, [
      el('span', { class: 'dt-fieldflow-crown', 'aria-hidden': 'true', text: svg.CROWN.current }),
      el('span', { class: 'dt-fieldflow-champid dn-mono', text: championId }),
      el('span', { class: 'dt-fieldflow-champmeta dn-faint', text:
        'defending · loss ' + (svg.isNum(champScalar) ? svg.fmt(champScalar, 1) : '—')
        + ' · ' + matchups.length + ' challenger' + (matchups.length === 1 ? '' : 's')
        + ' · ' + promoted + ' promoted' }),
    ]));
  } else {
    head.appendChild(el('span', { class: 'dn-faint', text: 'No reigning champion yet — the seed has not been challenged.' }));
  }
  wrap.appendChild(head);

  // the challenger lanes (one per match-up), Δ-vs-champion + verdict + hypothesis.
  const challengers = matchups.map((m, i) => {
    const gate = gates && gates[i] ? gates[i] : null;
    const verdict = normaliseDecision(m) || normaliseDecision(gate) || 'pending';
    const delta = svg.isNum(m.delta_scalar) ? m.delta_scalar
      : (gate && svg.isNum(gate.delta_scalar) ? gate.delta_scalar : null);
    const driver = gate && gate.primary_driver && gate.primary_driver.judge ? gate.primary_driver.judge : null;
    return {
      id: m.challenger, delta, verdict,
      hypothesis: m.hypothesis_core_idea ? String(m.hypothesis_core_idea) : null,
      driver,
    };
  });

  if (!matchups.length) {
    wrap.appendChild(empty('No challenger has entered the ring yet — the seed champion stands undefeated.'));
    return wrap;
  }
  wrap.appendChild(svg.duelFlow({
    championId, championScalar: champScalar, challengers,
    onCompetitor: (id) => ctx.navigate('candidate', { epochId, gen: id }),
  }));
  wrap.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
    'each lane is a challenger duelling the champion · ' + svg.CROWN.current + ' = the crowned champion-gate · a dot below the Δ=0 rule = improvement (good), above = regression (bad) · ↑ promoted · ✕ cut · ○ pending · hover a lane for its hypothesis + exact Δ · click → its candidate' }));
  return wrap;
}

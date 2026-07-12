// js/views/gens.js — GENERATIONS group landing.
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

import { el } from '../core/dom.js';
import { state } from '../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, verdictPill, decisionFor, decisionOf, dataTable, deltaCell, ratingCellEl, ratingTripleDigest } from '../ui.js';
import { renderStructure, structurePill, structureDigest, isNonGauntlet, normalizeStructure, resolveNonGauntletSt } from './structure.js';
import { deriveLiveStatus } from '../livestatus.js';
import { roundsFromTimeline, roundModelDigest } from '../rounds.js';

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
  const [rows, traj, bracket, timeline] = await Promise.all([
    D.generationsForEpoch(id), D.scoreTrajectory(id), D.bracket(id), D.roundTimeline(id),
  ]);

  // ── ROUND DRILL-DOWN (Task 4): the epoch timeline indexes rounds; a `round`
  // param scopes THIS view to ONE evolve round's tournament. We render the
  // selected round's field-tournament (its bracket tree / swiss ladder / racing
  // ladder + the per-round flow + per-board scoring) via renderStructure. The
  // full (all-rounds) Match-ups view is unchanged when no round is selected.
  const roundParam = (params && params.round != null) ? params.round : null;
  if (roundParam != null) {
    await renderRoundDrilldown(host, ctx, id, ep, bracket, traj, rows, roundParam, timeline);
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
  // the visibility rating triple rides the lineage rows (server-joined; the
  // Rust view / experiments fallback simply omit it -> unrated, renders '—').
  const gens = rows.length
    ? rows.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: g.promoted == null ? null : !!g.promoted, elo: g.elo, elo_se: g.elo_se, elo_games: g.elo_games }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: x.promoted == null ? null : !!x.promoted }));

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  // The REIGNING champion — the server-stamped pointer (never re-scanned).
  const championId = (ep && ep.current_champion != null) ? String(ep.current_champion) : null;
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
    gens: gens.map((g) => [g.id, g.parent, g.promoted, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null,
      // the visibility rating (int register) — a reindex that moves a rating
      // repaints; an unrated row folds null (pre-rating digest shape).
      ratingTripleDigest(g)]),
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
      const tbl = dataTable({
        class: 'dn-board-table',
        columns: [{ label: 'generation' }, { label: 'role' }, { label: 'parent' },
          { label: 'scalar (loss)', class: 'dn-num' }, { label: 'rating', class: 'dn-num' }, { label: 'Δ vs champion', class: 'dn-num' }, { label: '' }],
        rows: gens.map((g) => {
          const sc = scalarByGen.get(g.id);
          const baseline = !g.parent;
          // Class B: an unscored candidate is PENDING, not rejected.
          const decision = decisionFor({ promoted: g.promoted, parent: g.parent });
          const delta = (svg.isNum(sc) && svg.isNum(champScalar) && !baseline) ? sc - champScalar : null;
          return {
            class: g.promoted ? 'dn-board-champ' : '',
            cells: [
              { class: 'dn-mono', text: g.id + (g.promoted ? ' ♛' : '') },
              { el: verdictPill(decision) },
              { class: 'dn-mono', text: g.parent || 'seed' },
              { class: 'dn-num dn-mono', text: svg.isNum(sc) ? svg.fmt(sc, 1) : '—' },
              // the visibility rating (server-joined; never the gate).
              { class: 'dn-num', el: [ratingCellEl(g)] },
              deltaCell(delta, { base: 'dn-num dn-mono', text: svg.isNum(delta) ? svg.fmtSigned(delta, 1) : '—' }),
              { el: el('a', { class: 'dn-linkbtn', href: ctx.href('candidate', { epochId: id, gen: g.id }), text: 'open →' }) },
            ],
          };
        }),
      });
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
async function renderRoundDrilldown(host, ctx, id, ep, bracket, traj, rows, roundParam, timeline) {
  const tournament = (ep && ep.tournament && typeof ep.tournament === 'object') ? ep.tournament : null;
  const structure = (tournament && tournament.structure) || 'gauntlet';
  const experiments = (ep && Array.isArray(ep.experiments)) ? ep.experiments : [];
  const gens = rows.length
    ? rows.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: g.promoted == null ? null : !!g.promoted, round_index: svg.isNum(g.round_index) ? g.round_index : null }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: x.promoted == null ? null : !!x.promoted, round_index: svg.isNum(x.round_index) ? x.round_index : null }));
  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  const championId = (ep && ep.current_champion != null) ? String(ep.current_champion) : null;

  // the live PROJECTED standing for an in-flight round's challenger (current
  // epoch only) — falls back to the projected scalar when no settled one exists.
  const liveForThisEpoch = liveBelongsToEpoch(id);
  const liveAt = state.activeTournament;
  const liveProjected = (liveForThisEpoch && liveAt && liveAt.projected && typeof liveAt.projected === 'object') ? liveAt.projected : {};
  // the live envelope (this epoch only) so a still-proposing NEW round is drillable
  // as its own in-flight round, not folded under the prior round (issue #16).
  const liveInflight = liveForThisEpoch ? liveAt : null;
  // The SETTLED rounds come off the SERVED round timeline; only the live
  // overlay (projected standings + the in-flight round) is applied here.
  const epochRounds = roundsFromTimeline({ timeline, bracket, gens, scalarBy: scalarByGen, structure, championId, projected: liveProjected, inflight: liveInflight });
  const want = String(roundParam);
  const round = epochRounds.find((r) => String(r.round_index) === want) || null;

  // THE ROUND'S TOURNAMENT PAYLOAD — for a non-gauntlet structure, resolve it
  // THROUGH THE SHARED resolver (live-first → reconstructRacing → per-round
  // record) so the round view and the all-rounds Match-ups / epoch view CANNOT
  // DRIFT. The old code read `round.tournamentRef.rounds` directly, but a RACING
  // field record carries `rounds: []` by design (rungs live in the per-challenger
  // records + the live envelope, not the aggregate field record) — so the round
  // view came up with zero rungs and rendered "No rungs evaluated yet." while the
  // epoch view, which went live-first → reconstruct, showed them. The per-round
  // field record is now only the COMPLETED-record fallback (swiss/elim, whose
  // rounds DO live in the record); racing is rebuilt by reconstructRacing.
  let st = null;
  if (isNonGauntlet(structure)) {
    // the settled record: RACING reads the SERVED racing-field payload (the
    // per-challenger join lives server-side); swiss/elim read the round's own
    // field record (its `rounds` are authoritative there).
    const recordSt = structure === 'racing'
      ? normalizeStructure(await D.racingField(id), false)
      : ((round && round.tournamentRef)
        ? normalizeStructure({
            structure: round.tournamentRef.structure || structure,
            structure_params: round.tournamentRef.structure_params || (tournament && tournament.params) || {},
            competitors: round.tournamentRef.competitors, rounds: round.tournamentRef.rounds,
            // the SERVED elim model rides the /api/tournaments record too.
            gen_states: round.tournamentRef.gen_states,
            standings: round.tournamentRef.standings,
            champion_lineage: bracket && bracket.champion_lineage, source: 'index',
          }, false)
        : null);
    const status = deriveLiveStatus({
      heartbeat: state.heartbeat, activeRuns: state.activeRuns, activeTournament: state.activeTournament,
    });
    const liveRaw = (liveForThisEpoch && status.running) ? await D.activeTournament() : null;
    const resolved = resolveNonGauntletSt({
      structure, epochId: id, liveRaw,
      heartbeat: state.heartbeat, activeRuns: state.activeRuns,
      params: (tournament && tournament.params) || {},
      completedRecord: recordSt,
    });
    st = resolved.st;
  } else if (round && round.tournamentRef) {
    // a gauntlet round that nonetheless carries a field record — normalize it
    // directly (back-compat; gauntlet rungs are not reconstructed).
    st = normalizeStructure({
      structure: round.tournamentRef.structure || structure,
      structure_params: round.tournamentRef.structure_params || (tournament && tournament.params) || {},
      competitors: round.tournamentRef.competitors, rounds: round.tournamentRef.rounds,
      gen_states: round.tournamentRef.gen_states,
      standings: round.tournamentRef.standings,
      champion_lineage: bracket && bracket.champion_lineage, source: 'index',
    }, false);
  }

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
      // Derive each challenger's Δ-vs-champion from its own scalar so the lanes
      // encode magnitude+direction (rather than collapsing onto the rule); falls
      // back to null when a scalar is missing.
      const synthMatchups = round.challengers.map((c) => ({
        champion: roundChampId, challenger: c.id,
        decision: c.promoted ? 'promoted' : null,
        delta_scalar: (svg.isNum(c.scalar) && svg.isNum(roundChampScalar))
          ? c.scalar - roundChampScalar : null,
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
  // adopted ONLY for the active epoch's view. The progressive live builders, the
  // racing reconstruction, and the live-vs-record adoption decision ALL live in
  // the SHARED resolver (resolveNonGauntletSt) so this all-rounds page and the
  // per-round drill-down can never drift (see the resolver's header). We pre-fetch
  // the COMPLETED per-tournament record here (the resolver is sync + I/O-free) and
  // hand it in as the recorded fallback the resolver uses when no live run is
  // adopted AND (for racing) the bracket reconstruction does not resolve.
  const liveRaw = (liveForThisEpoch && status.running) ? await D.activeTournament() : null;

  // pre-fetch the settled record. RACING reads the SERVED racing-field payload
  // (`/api/epoch/{id}/racing-field` — the per-challenger join lives
  // server-side); swiss/elim read the epoch's most-recent per-tournament
  // structure record. Cached + failure-tolerant, so probing is cheap and keeps
  // the resolver sync.
  let tournamentId = null;
  let completedRecord = null;
  if (structure === 'racing') {
    completedRecord = normalizeStructure(await D.racingField(id), false);
    if (completedRecord) tournamentId = 'racing:served';
  } else {
    const tournaments = (bracket && Array.isArray(bracket.tournaments)) ? bracket.tournaments : [];
    const matchStruct = tournaments.filter((t) => t && t.structure === structure);
    const nonGaunt = tournaments.filter((t) => t && t.structure && t.structure !== 'gauntlet');
    if (matchStruct.length) tournamentId = matchStruct[matchStruct.length - 1].tournament_id;
    else if (nonGaunt.length) tournamentId = nonGaunt[nonGaunt.length - 1].tournament_id;
    else if (tournaments.length) tournamentId = tournaments[tournaments.length - 1].tournament_id;
    else {
      const matchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups : [];
      const last = matchups[matchups.length - 1];
      if (last && last.challenger) tournamentId = `${id}:${last.champion || ''}->${last.challenger}`;
    }
    completedRecord = tournamentId ? normalizeStructure(await D.tournamentStructure(id, tournamentId), false) : null;
  }

  const resolved = resolveNonGauntletSt({
    structure, epochId: id, liveRaw,
    heartbeat: state.heartbeat, activeRuns: state.activeRuns,
    params, completedRecord,
  });
  const shown = resolved.st;
  const liveUsable = resolved.source === 'live';
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
    const verdict = decisionOf(m) || decisionOf(gate) || 'pending';
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
    'each lane is a challenger duelling the champion · the vertical rule is the champion (Δ=0) · a lane reaching RIGHT toward the ' + svg.CROWN.current + ' gate improved on the champion (good); one reaching LEFT regressed (bad); bar length = |Δ| · ↑ promoted · ✕ cut · ○ pending · hover a lane for its hypothesis + exact Δ · click → its candidate' }));
  return wrap;
}

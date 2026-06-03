// variants/T/views/epoch.js — EPOCH OVERVIEW: the dense substrate of one epoch.
//
// Leads with the OBJECTIVE, the collapsible proposer brief, the SLIM REEL
// (rounds along the champion spine; for a non-gauntlet structure a compact
// structure OVERVIEW instead), then the board×generation drift-loss HEATMAP.
// Data: /api/epoch, /api/lineage, /api/tournaments, /api/score-trajectory,
// /api/generation/{e}/{g}/per-entry.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { deriveLiveStatus } from '../livestatus.js';
import { gatedSwap, section, empty, stat, renderMarkdown, normaliseDecision, densityTokens } from '../ui.js';
import { structurePill, isNonGauntlet, structureLabel, reconstructRacing, normalizeStructure, racingModel, swissOverviewModel, elimModel, buildLiveSwissModel, buildLiveElimModel, structureDigest } from './structure.js';
import { epochRoundModel, roundModelDigest, waterfallModel } from './rounds.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading epoch contract…' }));

  // HEADER SCOPING: derive the VIEWED epoch from the route param, then fetch its
  // contract via the epoch-SCOPED accessor (NEVER bare `D.epoch()`, which returns
  // the CURRENT epoch) so viewing e0 shows e0 even while e1 is live.
  const routeEpoch = (params && params.epochId) || null;
  const [ep, lin, traj, bracket] = await Promise.all([
    D.epoch(routeEpoch), D.lineage(), D.scoreTrajectory(routeEpoch), D.bracket(routeEpoch),
  ]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Epoch' }), empty('No current epoch.')]);
    return;
  }
  const epochId = routeEpoch || ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const board = Array.isArray(ep.board) ? ep.board : [];

  // SCOPE TO THE VIEWED EPOCH: /api/lineage spans the whole workspace, so filter
  // to this epoch's generations (fall back to the scoped ep.experiments; dedupe
  // by id) — otherwise a sibling epoch's gens leak into the heatmap + field count.
  const lineageRows = (lin && Array.isArray(lin.generations)) ? lin.generations : [];
  const scopedLineage = lineageRows.filter((g) => g && g.epoch_id === epochId);
  const rawGens = scopedLineage.length
    ? scopedLineage.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted, round_index: svg.isNum(g.round_index) ? g.round_index : null }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted', round_index: svg.isNum(x.round_index) ? x.round_index : null }));
  const gens = [];
  const seenGen = new Set();
  for (const g of rawGens) {
    if (g.id == null || seenGen.has(g.id)) continue;
    seenGen.add(g.id);
    gens.push(g);
  }
  // carry round_index from the experiments fallback when the lineage rows lack it
  // (the experiments fallback path may carry the stamp the bare lineage did not).
  if (scopedLineage.length && experiments.length) {
    const expRound = new Map();
    for (const x of experiments) if (svg.isNum(x.round_index)) expRound.set(String(x.generation_id), x.round_index);
    for (const g of gens) if (!svg.isNum(g.round_index) && expRound.has(String(g.id))) g.round_index = expRound.get(String(g.id));
  }

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

  // The reigning champion: the promoted (or seed) generation — round 0's seed.
  const champ = gens.find((g) => g.promoted) || gens.find((g) => !g.parent) || null;
  const championId = champ ? champ.id : null;

  // The configured tournament structure (§3.1) — surfaced as a one-line
  // header pill. Absent ⇒ no pill (a gauntlet epoch that predates the
  // feature reads byte-identically — the block is simply omitted upstream).
  const tournament = (ep && ep.tournament && typeof ep.tournament === 'object') ? ep.tournament : null;
  // Is this a NON-gauntlet epoch? Racing/swiss/elim are NOT N sequential
  // champion-vs-challenger rounds, so the gauntlet "champion spine" reel does
  // not describe them — we swap in a structure-appropriate strip instead.
  const structure = (tournament && tournament.structure) || 'gauntlet';
  const nonGauntlet = isNonGauntlet(structure);

  // ---- NON-GAUNTLET OVERVIEW data (LIVE-FIRST, else completed record) ----
  // Each non-gauntlet structure renders a compact at-a-glance overview from the
  // SAME normalized `st` the Match-ups ladder uses: the LIVE active-tournament
  // topology when a run for THIS epoch is in flight, else the completed record
  // (reconstructRacing for racing; the matching /api/tournaments entry for
  // swiss/elim). Null (→ honest brief line) when there is no data.
  let racingFunnel = null;
  let swissOver = null;
  let elimOver = null;
  if (nonGauntlet) {
    const status = deriveLiveStatus({
      heartbeat: state.heartbeat, activeRuns: state.activeRuns, activeTournament: state.activeTournament,
    });
    const liveRaw = status.running ? await D.activeTournament() : null;
    const liveForThisEpoch = (liveRaw && liveRaw.epoch_id != null)
      ? String(liveRaw.epoch_id) === String(epochId) : !!liveRaw;
    if (structure === 'racing') {
      const liveSt = liveForThisEpoch ? normalizeStructure(liveRaw, true) : null;
      const racingSt = (liveSt && liveSt.live) ? liveSt : reconstructRacing(bracket, epochId);
      const model = racingModel(racingSt);
      if (model && model.hasRungs) racingFunnel = { st: racingSt, model };
    } else {
      let st = null;
      if (liveForThisEpoch && liveRaw && String(liveRaw.structure) === structure) {
        const epochGens = (Array.isArray(liveRaw.competitors) ? liveRaw.competitors : [])
          .map((c) => c && c.generation_id).filter((g) => g != null).map(String);
        const args = { at: liveRaw, heartbeat: state.heartbeat, activeRuns: state.activeRuns, epochGens: epochGens.length ? epochGens : null };
        st = (structure === 'swiss' ? buildLiveSwissModel(args) : buildLiveElimModel(args)) || normalizeStructure(liveRaw, true);
      }
      if (!st) {
        const tournaments = (bracket && Array.isArray(bracket.tournaments)) ? bracket.tournaments : [];
        const rec = tournaments.filter((t) => t && String(t.structure) === structure).pop();
        if (rec) st = normalizeStructure({
          structure, structure_params: rec.structure_params || (tournament && tournament.params) || {},
          competitors: rec.competitors, rounds: rec.rounds, standings: rec.standings,
          champion_lineage: bracket && bracket.champion_lineage, source: 'index',
        }, false);
      }
      if (st && structure === 'swiss') { const m = swissOverviewModel(st); if (m) swissOver = { st, model: m }; }
      else if (st) { const m = elimModel(st); if (m && m.hasMatches !== false && m.winners.length) elimOver = { st, model: m }; }
    }
  }

  // ---- THE EPOCH ROUND MODEL (the champion-spine timeline's source) ----
  // The epoch is N evolve rounds along the champion spine. Derive the rounds
  // from per-gen round_index, else the per-round field records, else the
  // gauntlet matchups, else a single round 0 (every run so far). The timeline
  // SUBSUMES the old gauntlet reel + the non-gauntlet structure strip — one
  // renderer for all structures, degrading to a single episode for --rounds 1.
  const epochRounds = epochRoundModel({ gens, scalarBy: scalarByGen, bracket, structure, championId });

  const digest = JSON.stringify({
    epochId, goal: ep.goal || '', objective: objectiveText(ep), briefLen: (ep.brief || '').length, closed: !!ep.closed,
    structure: tournament ? [tournament.structure, JSON.stringify(tournament.params || {})] : null,
    nonGauntlet,
    racingFunnel: racingFunnel ? structureDigest(racingFunnel.st) : null,
    swissOver: swissOver ? structureDigest(swissOver.st) : null,
    elimOver: elimOver ? structureDigest(elimOver.st) : null,
    gens: gens.map((g) => [g.id, g.parent, g.promoted, svg.isNum(g.round_index) ? g.round_index : null, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
    rounds: roundModelDigest(epochRounds),
    waterfall: waterfallModel(epochRounds).map((s) => [s.round_index, svg.isNum(s.from) ? s.from.toFixed(2) : null, svg.isNum(s.to) ? s.to.toFixed(2) : null, s.promoted, s.gen]),
    loss: [...lossLookup.entries()].sort(),
    board: board.map((b) => [b.entry_id || b.id, b.kind, b.weight, b.budget_s]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: `Epoch ${epochId}` }),
      el('div', { class: 'dn-objective' }, [
        el('div', { class: 'lab', text: 'objective' }),
        el('div', { class: 'txt', text: objectiveText(ep) }),
      ]),
      tournament ? el('div', { class: 'dt-structure-line' }, [
        structurePill(tournament.structure, tournament.params),
      ]) : null,
    ].filter(Boolean)));

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

    // ---- the CHAMPION-SPINE ROUND TIMELINE (the epoch overview hero) ----
    // ONE renderer for ALL structures: the champion spine across the epoch's
    // evolve rounds, each round an episode (incoming champion + a fan of that
    // round's challengers + a COMPACT per-round structure figure + the gate
    // outcome). This SUBSUMES the old gauntlet reel + non-gauntlet strip — a
    // single round (--rounds 1, every run so far) degrades to ONE episode.
    const params = (tournament && tournament.params) || {};
    const open = (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); };
    const drill = (roundIndex) => ctx.navigate('gens', { epochId, round: roundIndex });
    const single = epochRounds.length <= 1;

    // The per-round STRUCTURE FIGURE. For a SINGLE round the aggregate
    // (whole-epoch) model IS the round's tournament — reuse the live-first
    // racing/swiss/elim models already built above. For MULTI-round epochs,
    // normalize each round's own field-tournament record. Gauntlet → null (the
    // spine + the challenger fan already tell that round's one-duel story).
    const figureForRound = (r) => {
      // normalize the round's OWN field-tournament record (multi-round path).
      const stFromRef = r.tournamentRef
        ? normalizeStructure({
            structure: r.tournamentRef.structure || structure,
            structure_params: r.tournamentRef.structure_params || params,
            competitors: r.tournamentRef.competitors, rounds: r.tournamentRef.rounds,
            standings: r.tournamentRef.standings,
            champion_lineage: bracket && bracket.champion_lineage, source: 'index',
          }, false)
        : null;
      // elim PARITY (#1): the per-round figure for elim is elimFlow (the
      // generations-across-rounds slopegraph), matching racing→funnel /
      // swiss→bump — NOT the compact mini-bracket. The full bracket tree lives
      // in the round drill-down (Match-ups). Prefer the aggregate (live-first)
      // model for a single round; fall back to the round's own record.
      if (structure === 'racing') {
        let m = single && racingFunnel ? racingFunnel.model : (stFromRef ? racingModel(stFromRef) : null);
        if (m && m.hasRungs) return svg.survivalFunnel({
          rungs: m.rungs, championId: m.championId, benchmarkId: m.benchmarkId, live: m.live,
          gateState: m.gateState, gateDelta: m.gateDelta, onCompetitor: open,
        });
      } else if (structure === 'swiss') {
        const m = single && swissOver ? swissOver.model : (stFromRef ? swissOverviewModel(stFromRef) : null);
        if (m) return svg.swissOverview({
          series: m.series, bars: m.bars, labels: m.labels,
          championId: m.championId, benchmarkId: m.benchmarkId, live: m.live,
          gateState: m.gateState, gateDelta: m.gateDelta, onCompetitor: open,
        });
      } else if (structure === 'single_elim' || structure === 'double_elim') {
        const m = single && elimOver ? elimOver.model : (stFromRef ? elimModel(stFromRef) : null);
        if (m && m.hasMatches !== false && m.winners.length) return svg.elimFlow({
          winners: m.winners, championId: m.championId, benchmarkId: m.benchmarkId,
          gateState: m.gateState, live: m.live, onCompetitor: open,
        });
      }
      return null;
    };

    const timelineCard = el('div', { class: 'dn-panel' });

    // ── the LOSS-FLOOR WATERFALL (the headline "is it improving + what drove
    // each gain" figure) — one step per round, sized by its promotion Δ; a held
    // round is flat. The running floor is annotated; the spine baseline is accent;
    // the winning mutation per step lives on hover. Derived from the SAME epoch
    // round model the spine timeline reads (single source). Only for ≥1 scored
    // round (else the descent has no floor to plot).
    const waterfallSteps = waterfallModel(epochRounds);
    if (waterfallSteps.some((s) => svg.isNum(s.to) || svg.isNum(s.from))) {
      timelineCard.appendChild(el('div', { class: 'dn-roundtl-waterfall dn-figpane' }, [
        svg.waterfall({ steps: waterfallSteps, onRound: drill, onCompetitor: open }),
      ]));
      timelineCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:6px 0 10px;', text:
        'the loss-floor descent across rounds — each step a promotion Δ (good = lower floor), a held round flat · the spine baseline is the champion floor · hover a step for its winning mutation' }));
    }

    timelineCard.appendChild(svg.roundTimeline({
      rounds: epochRounds, selected: null,
      figureFor: figureForRound, onRound: drill, onCompetitor: open,
    }));
    timelineCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      'one node per round on the champion spine — the descending loss floor reads as "is it improving?" · each episode is one round (incoming champion + its minted field + the tournament figure + the gate) · '
      + svg.CROWN.current + ' = the round\'s champion · click a round → its full tournament (Match-ups)' }));
    nodes.push(section(single
      ? 'Round timeline · ' + structureLabel(structure, params)
      : `Round timeline · the champion spine across ${epochRounds.length} rounds · ` + structureLabel(structure, params),
      timelineCard));

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

// The epoch OBJECTIVE line: the explicit `goal` if set, else a display-only
// fallback to the epoch's OWN brief TITLE — the first H1 (`# …`) of brief.md,
// stripping a leading "Epoch eN — " / "Epoch eN: " prefix. This reads the
// epoch's self-contained brief (no cross-epoch reach), so an auto-rolled epoch
// whose goal was never frozen no longer reads "(no objective recorded)". Only
// when neither a goal nor a brief title exists does it fall back to the honest
// "(no objective recorded)".
export function objectiveText(ep) {
  if (ep && typeof ep.goal === 'string' && ep.goal.trim()) return ep.goal.trim();
  const title = briefTitle(ep && ep.brief);
  return title || '(no objective recorded)';
}

// The first H1 of a brief, with a leading "Epoch eN — "/"Epoch eN: " prefix
// stripped. Null when the brief has no H1.
export function briefTitle(brief) {
  if (!brief || typeof brief !== 'string') return null;
  const lines = brief.replace(/\r\n/g, '\n').split('\n');
  for (const raw of lines) {
    const line = raw.trim();
    const m = /^#\s+(.+)$/.exec(line);   // an H1 (NOT ## / ###)
    if (m) {
      return m[1].trim().replace(/^Epoch\s+\S+\s*[—:-]\s*/i, '').trim() || null;
    }
  }
  return null;
}

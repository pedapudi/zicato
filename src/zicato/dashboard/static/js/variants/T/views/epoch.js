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
import { reel, reelDigest } from '../reel.js';
import { deriveLiveStatus } from '../livestatus.js';
import { gatedSwap, section, empty, stat, renderMarkdown, normaliseDecision, densityTokens } from '../ui.js';
import { structurePill, isNonGauntlet, structureLabel, reconstructRacing, normalizeStructure, racingModel, swissOverviewModel, elimModel, buildLiveSwissModel, buildLiveElimModel, structureDigest } from './structure.js';

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
    ? scopedLineage.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' }));
  const gens = [];
  const seenGen = new Set();
  for (const g of rawGens) {
    if (g.id == null || seenGen.has(g.id)) continue;
    seenGen.add(g.id);
    gens.push(g);
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

  const digest = JSON.stringify({
    epochId, goal: ep.goal || '', briefLen: (ep.brief || '').length, closed: !!ep.closed,
    structure: tournament ? [tournament.structure, JSON.stringify(tournament.params || {})] : null,
    nonGauntlet,
    racingFunnel: racingFunnel ? structureDigest(racingFunnel.st) : null,
    swissOver: swissOver ? structureDigest(swissOver.st) : null,
    elimOver: elimOver ? structureDigest(elimOver.st) : null,
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

    // ---- the structure-aware reel ------------------------------------
    // GAUNTLET keeps the slim reel (rounds along the champion spine). A
    // NON-gauntlet epoch swaps in a compact structure OVERVIEW (the survival
    // funnel / swiss bump+bar / elim mini-bracket) with a "See Match-ups" link.
    if (nonGauntlet) {
      const fieldN = gens.length;
      const params = (tournament && tournament.params) || {};
      const rungs = Array.isArray(params.rungs) ? params.rungs.length : null;
      const facts = [el('span', { class: 'dt-struct-strip-lab', text: structureLabel(structure, params) })];
      facts.push(el('span', { class: 'dt-struct-strip-fact', text: `field of ${fieldN}` }));
      if (structure === 'racing' && rungs) facts.push(el('span', { class: 'dt-struct-strip-fact', text: `${rungs} rung${rungs === 1 ? '' : 's'}` }));
      else if (structure === 'swiss' && svg.isNum(params.rounds)) facts.push(el('span', { class: 'dt-struct-strip-fact', text: `${params.rounds} round${params.rounds === 1 ? '' : 's'}` }));

      // helper: a structure-overview card (facts header + LIVE pill + figure +
      // caption + "See Match-ups →"). Each structure passes its figure + caption.
      const overviewCard = (live, figure, caption) => {
        const card = el('div', { class: 'dn-panel dn-figpane dt-struct-over dt-struct-strip' });
        card.appendChild(el('div', { class: 'dt-struct-strip-row' }, [
          ...facts, live ? el('span', { class: 'dt-live-pill', text: 'LIVE' }) : null,
        ].filter(Boolean)));
        card.appendChild(figure);
        if (caption) card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: caption }));
        card.appendChild(el('a', { class: 'dn-linkbtn dt-struct-strip-link', href: ctx.href('gens', { epochId }), text: 'See Match-ups →' }));
        return card;
      };
      const open = (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); };
      // shared champion-gate suffix for every overview caption.
      const gateNote = (m, crown) => (m.gateState === 'crowned' ? ` · champion-gate: ${m.championId} promoted ${crown}`
        : m.gateState === 'stands' ? ' · champion-gate: champion stands'
        : m.gateState === 'deciding' ? ' · champion-gate: deciding…' : '')
        + (m.live ? ' · LIVE — the winner is not committed until the gate' : '');
      const pushOverview = (m, fig, caption) =>
        nodes.push(section('Tournament structure · ' + structureLabel(structure, params), overviewCard(m.live, fig, caption)));

      if (structure === 'racing' && racingFunnel) {
        // the interactive SURVIVAL FUNNEL (field → cuts → survivor → gate).
        const m = racingFunnel.model;
        pushOverview(m, svg.survivalFunnel({
          rungs: m.rungs, championId: m.championId, benchmarkId: m.benchmarkId, live: m.live,
          gateState: m.gateState, gateDelta: m.gateDelta, onCompetitor: open,
        }), (m.benchmarkId ? `the field is raced vs the champion v0 = ${m.benchmarkId}; every Δ is Δ-vs-v0 and v0 defends at the gate · ` : '')
          + 'successive halving — each rung races the field on a growing board fraction, then cuts the worst by η · ✕ = cut · ↑ = survives · ♚ = crowned at the full-board gate · click a competitor → open'
          + gateNote(m, '♚'));
      } else if (structure === 'swiss' && swissOver) {
        // the SWISS OVERVIEW: standings bump chart + ranked Copeland bar.
        const m = swissOver.model;
        pushOverview(m, svg.swissOverview({
          series: m.series, bars: m.bars, labels: m.labels,
          championId: m.championId, benchmarkId: m.benchmarkId, live: m.live,
          gateState: m.gateState, gateDelta: m.gateDelta, onCompetitor: open,
        }), 'each line tracks one competitor’s standings rank round-to-round (rank 1 = top) — the leader emerges as lines cross · the bar ranks final Copeland points (win 1 / draw ½) · ♛ = champion · ♔ = former champion (displaced incumbent)'
          + gateNote(m, '♛'));
      } else if ((structure === 'single_elim' || structure === 'double_elim') && elimOver) {
        // the ELIM OVERVIEW: a compact mini-bracket — elimBracket at small scale.
        const m = elimOver.model;
        const isDouble = !!(m.losers && m.losers.length);
        pushOverview(m, svg.elimBracket({
          compact: true, winners: m.winners, losers: m.losers,
          championId: m.championId, benchmarkId: m.benchmarkId, live: m.live,
          gateState: m.gateState, gateDelta: m.gateDelta, onCompetitor: open,
        }), 'the bracket shape + who advanced — ✦ = match winner · the bracket winner must beat the incumbent at the champion-gate ♚'
          + (isDouble ? ' · the losers’ bracket gives a second life (double-elim)' : '')
          + gateNote(m, '♚'));
      } else {
        // NO DATA (no record yet, or mid-proposing) → an HONEST brief line — the
        // structure facts + a pointer to Match-ups, NEVER the old negative
        // "this epoch is not a gauntlet" placeholder.
        const stripCard = el('div', { class: 'dn-panel dt-struct-strip' });
        stripCard.appendChild(el('div', { class: 'dt-struct-strip-row' }, facts));
        stripCard.appendChild(el('a', { class: 'dn-linkbtn dt-struct-strip-link', href: ctx.href('gens', { epochId }), text: 'See Match-ups →' }));
        const line = structure === 'racing'
          ? 'no rungs have raced yet — the survival funnel fills in once the field runs · the full ladder / standings live in Match-ups'
          : structure === 'swiss'
            ? 'no swiss rounds have scored yet — the standings overview fills in as pairings land · the full ladder lives in Match-ups'
            : 'the bracket has not been seeded yet — the mini-bracket fills in as matches land · the full bracket lives in Match-ups';
        stripCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: line }));
        nodes.push(section('Tournament structure · ' + structureLabel(structure, params), stripCard));
      }
    } else {
      const reelCard = el('div', { class: 'dn-panel' });
      reelCard.appendChild(reel({
        championId, rounds,
        selected: null,
        onSelect: (id) => ctx.navigate('candidate', { epochId, gen: id }),
        onSeed: (id) => { if (id) ctx.navigate('candidate', { epochId, gen: id }); },
      }));
      nodes.push(section('Reel · the rounds along the champion spine', reelCard));
    }

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

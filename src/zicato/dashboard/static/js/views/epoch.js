// js/views/epoch.js — EPOCH OVERVIEW: the dense substrate of one epoch.
//
// Leads with the OBJECTIVE, the collapsible proposer brief, the SLIM REEL
// (rounds along the champion spine; for a non-gauntlet structure a compact
// structure OVERVIEW instead), then the board×generation drift-loss HEATMAP.
// Data: /api/epoch, /api/lineage, /api/tournaments, /api/score-trajectory,
// /api/generation/{e}/{g}/per-entry.

import { el } from '../core/dom.js';
import { state } from '../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { livenessFor, epochIsLive } from '../livestatus.js';
import { gatedSwap, section, empty, stat, renderMarkdown, densityTokens, chip, dataTable, figCaption,
  loopVerdict, promotionRateLabel, costPerPromotionLabel, fmtDurationMs, noiseBandFor } from '../ui.js';
import { structurePill, isNonGauntlet, structureLabel, normalizeStructure, racingModel, swissOverviewModel, elimModel, resolveNonGauntletSt, structureDigest } from './structure.js';
import { roundsFromTimeline, roundModelDigest, waterfallSteps } from '../rounds.js';
import { boardStatusModel, boardStatusDigest, renderBoardStatus } from './boardstatus.js';
import { buildExperimentsLedger, ledgerDigest } from './ledger.js';

// The user's last expand/collapse of the proposer brief, keyed by epoch. The
// epoch view is digest-gated: a live heartbeat that moves ANY data rebuilds the
// DOM via gatedSwap, which would otherwise recreate the brief <details> at its
// length-based DEFAULT and silently discard a manual expand (the "expand →
// auto-collapses again" bug). Remembering the choice here keeps an expanded
// brief expanded across those re-renders.
const _briefOpen = new Map();

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading epoch contract…' }));

  // HEADER SCOPING: derive the VIEWED epoch from the route param, then fetch its
  // contract via the epoch-SCOPED accessor (NEVER bare `D.epoch()`, which returns
  // the CURRENT epoch) so viewing e0 shows e0 even while e1 is live.
  const routeEpoch = (params && params.epochId) || null;
  const [ep, traj, bracket] = await Promise.all([
    D.epoch(routeEpoch), D.scoreTrajectory(routeEpoch), D.bracket(routeEpoch),
  ]);
  // (the round timeline is fetched below once the epoch id is resolved.)
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Epoch' }), empty('No current epoch.')]);
    return;
  }
  const epochId = routeEpoch || ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const board = Array.isArray(ep.board) ? ep.board : [];

  // The LOOP-COMMUNICATION reads for this epoch: the promoted-lineage
  // trajectory (promotion rate + uncertainty-honest verdict + noise floor),
  // the tournament cost accounting, and the per-judge loss trend (the reader
  // + endpoint shipped long ago with zero view consumers — this is its view).
  // All null-degrade (the Rust supervisor serves none of the three) → the
  // panels are simply omitted.
  // The EXPERIMENTS LEDGER (§3) rides the same fan-out: one row per experiment
  // (idea · sites · decision · Δ · reason · round), joined server-side. Null on
  // a backend that does not serve it → the section is simply omitted.
  // `calib` joins the fan-out for the MEASUREMENT BAND (below): the compact
  // calibration mini sits beside the heatmap and the per-judge trend, all three
  // being figures that measure the INSTRUMENT rather than the candidates.
  const [loopTraj, loopCost, judgeTrend, ledger, calib] = await Promise.all([
    D.trajectory(epochId), D.tournamentCost(epochId), D.perJudgeTrend(epochId),
    D.experimentsLedger(epochId), D.calibrationTrend(epochId),
  ]);

  // THE EPOCH-SCOPED GENERATIONS FEED (server-scoped `/api/lineage?epoch=`),
  // falling back to the scoped ep.experiments; dedupe by id. The SETTLED round
  // timeline is SERVED (`/api/epoch/{id}/round-timeline`).
  const [scopedLineage, timeline] = await Promise.all([
    D.generationsForEpoch(epochId), D.roundTimeline(epochId),
  ]);
  const rawGens = scopedLineage.length
    ? scopedLineage.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted, round_index: svg.isNum(g.round_index) ? g.round_index : null }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: x.promoted === true, round_index: svg.isNum(x.round_index) ? x.round_index : null }));
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
  for (const b of board) { if (b.entry_id) entryIds.add(b.entry_id); }

  // The REIGNING champion — the server-stamped pointer (never re-scanned).
  const championId = (ep && ep.current_champion != null) ? String(ep.current_champion) : null;

  // The configured tournament structure (§3.1) — surfaced as a one-line
  // header pill. Absent ⇒ no pill (a gauntlet epoch that predates the
  // feature reads byte-identically — the block is simply omitted upstream).
  const tournament = (ep && ep.tournament && typeof ep.tournament === 'object') ? ep.tournament : null;
  // Is this a NON-gauntlet epoch? Racing/swiss/elim are NOT N sequential
  // champion-vs-challenger rounds, so the gauntlet "champion spine" reel does
  // not describe them — we swap in a structure-appropriate strip instead.
  const structure = (tournament && tournament.structure) || 'gauntlet';
  const nonGauntlet = isNonGauntlet(structure);

  // ---- NON-GAUNTLET OVERVIEW data (the SHARED resolver — one source of truth) ----
  // Each non-gauntlet structure renders a compact at-a-glance overview from the
  // SAME normalized `st` the Match-ups ladder + per-round drill-down use. Building
  // it HERE through the SHARED resolveNonGauntletSt (live-first → reconstructRacing
  // → completed per-tournament record) — instead of a divergent inline
  // construction — is the SINGLE-SOURCE-OF-TRUTH guarantee: the epoch overview
  // funnel/bump/flow can never drift from the Match-ups figure for the same run
  // (the old inline racing path used `normalizeStructure(liveRaw,true)` — no
  // progressive overlay / projected-standing re-rank / seeded-champ benchmark —
  // and bypassed the completed per-tournament record, so it diverged live AND
  // settled). Null model (→ honest brief line) when there is no data.
  let racingFunnel = null;
  let swissOver = null;
  let elimOver = null;
  // the live PROJECTED standing map ({gen: {scalar, boards_done, boards_total}})
  // from the current-epoch active tournament — threaded into the cross-round
  // timeline so an in-flight round's challenger shows a climbing projected
  // scalar (marked "projected") rather than a blank "—".
  let liveProjected = {};
  // the live active-tournament envelope SCOPED to this epoch — fed to
  // epochRoundModel so a NEW round that is only proposing/applying (not yet
  // settled) surfaces as its OWN in-flight round (issue #16). Fetched for EVERY
  // structure (a multi-challenger gauntlet has in-flight rounds too), not only
  // the non-gauntlet overview path.
  let liveInflight = null;
  let liveEnvelope = null;
  let liveIsLive = false;
  {
    // SCOPE vs CLOCK (issue #194 §1). The envelope is read whenever it is this
    // epoch's — an interrupted round's topology lives nowhere else, and
    // dropping it would blank the page rather than tell the operator what
    // happened. Liveness gates the present-tense OVERLAYS instead: the
    // projected standings and the in-flight round both claim "right now".
    liveIsLive = livenessFor(state).liveness.live;
    const liveRaw = await D.activeTournament();
    const belongs = (liveRaw && liveRaw.epoch_id != null)
      ? String(liveRaw.epoch_id) === String(epochId) : !!liveRaw;
    if (belongs && liveRaw) liveEnvelope = liveRaw;
    const liveForThisEpoch = belongs && liveIsLive;
    if (liveForThisEpoch && liveRaw && liveRaw.projected && typeof liveRaw.projected === 'object') liveProjected = liveRaw.projected;
    if (liveForThisEpoch && liveRaw) liveInflight = liveRaw;
  }
  if (nonGauntlet) {
    const liveRaw = liveInflight;
    const liveForThisEpoch = !!liveInflight;

    // pre-fetch the SETTLED record. RACING reads the SERVED racing-field
    // payload (the per-challenger join lives server-side); swiss/elim read the
    // epoch's most-recent per-tournament structure record. Same selection
    // gens.js uses, so the recorded source is identical across the two
    // callers. Cached + failure-tolerant.
    let completedRecord = null;
    if (structure === 'racing') {
      completedRecord = normalizeStructure(await D.racingField(epochId), false);
    } else {
      const tournaments = (bracket && Array.isArray(bracket.tournaments)) ? bracket.tournaments : [];
      const matchStruct = tournaments.filter((t) => t && t.structure === structure);
      const nonGaunt = tournaments.filter((t) => t && t.structure && t.structure !== 'gauntlet');
      let tournamentId = null;
      if (matchStruct.length) tournamentId = matchStruct[matchStruct.length - 1].tournament_id;
      else if (nonGaunt.length) tournamentId = nonGaunt[nonGaunt.length - 1].tournament_id;
      else if (tournaments.length) tournamentId = tournaments[tournaments.length - 1].tournament_id;
      if (tournamentId) completedRecord = normalizeStructure(await D.tournamentStructure(epochId, tournamentId), false);
    }

    const resolved = resolveNonGauntletSt({
      // The envelope supplies the topology whenever it is this epoch's; `live`
      // tells the resolver which TENSE to hand it back in.
      structure, epochId, liveRaw: liveEnvelope, live: liveIsLive,
      heartbeat: state.heartbeat, activeRuns: state.activeRuns,
      params: (tournament && tournament.params) || {},
      completedRecord,
    });
    const st = resolved.st;
    if (st) {
      if (structure === 'racing') {
        const model = racingModel(st);
        if (model && model.hasRungs) racingFunnel = { st, model };
      } else if (structure === 'swiss') {
        const m = swissOverviewModel(st);
        if (m) swissOver = { st, model: m };
      } else {
        const m = elimModel(st);
        if (m && m.hasMatches !== false && m.winners.length) elimOver = { st, model: m };
      }
    }
  }

  // ---- THE EPOCH ROUND MODEL (the champion-spine timeline's source) ----
  // The SETTLED rounds are SERVED by /api/epoch/{id}/round-timeline; only the
  // LIVE overlay (projected standings + the in-flight proposing round) is
  // applied client-side. The timeline SUBSUMES the old gauntlet reel + the
  // non-gauntlet structure strip — one renderer for all structures, degrading
  // to a single episode for --rounds 1 (and an honest empty when unserved).
  const epochRounds = roundsFromTimeline({ timeline, bracket, gens, scalarBy: scalarByGen, structure, championId, projected: liveProjected, inflight: liveInflight });

  // The BOARD-STATUS surface (train/holdout split + ladder + generalization
  // gap). Derived DEFENSIVELY from the epoch payload — graceful empty states
  // when the overfitting `#2`/`#5` fields are absent.
  const boardStatus = boardStatusModel(ep);

  // WHICH CONTRACT THIS EPOCH FROZE — `contract_hash` off `config.json`
  // (build_epoch_view). An epoch is a frozen evaluation contract; the hash is
  // its identity, and until now the epoch view never said which one it was
  // running. Shortened for the header, full value on hover.
  const contractHash = (ep && typeof ep.contract_hash === 'string' && ep.contract_hash.trim())
    ? ep.contract_hash.trim() : null;
  // The Δscalar AGGREGATES the epoch payload computes for exactly this header
  // (`delta_scalar_summary` = {champion_spine, gross}). The spine sum is the
  // meta-loop's ACTUAL progress (promoted hops only); `gross` sums every
  // experiment including rejected challengers, so it is the SECONDARY read and
  // is labelled as such — never the headline.
  const deltaSummary = (ep && ep.delta_scalar_summary && typeof ep.delta_scalar_summary === 'object')
    ? ep.delta_scalar_summary : null;
  const spineDelta = deltaSummary && svg.isNum(deltaSummary.champion_spine) ? deltaSummary.champion_spine : null;
  const grossDelta = deltaSummary && svg.isNum(deltaSummary.gross) ? deltaSummary.gross : null;

  const digest = JSON.stringify({
    epochId, goal: ep.goal || '', objective: objectiveText(ep), briefLen: (ep.brief || '').length, closed: !!ep.closed,
    // the frozen contract identity rendered on the header (A18).
    contractHash,
    // the two Δscalar tiles (A11) at their RENDERED precision, so a no-op beat
    // is byte-identical and a settled round that moves either sum repaints.
    deltaSummary: [
      spineDelta == null ? null : spineDelta.toFixed(3),
      grossDelta == null ? null : grossDelta.toFixed(3),
    ],
    structure: tournament ? [tournament.structure, JSON.stringify(tournament.params || {})] : null,
    nonGauntlet,
    racingFunnel: racingFunnel ? structureDigest(racingFunnel.st) : null,
    swissOver: swissOver ? structureDigest(swissOver.st) : null,
    elimOver: elimOver ? structureDigest(elimOver.st) : null,
    gens: gens.map((g) => [g.id, g.parent, g.promoted, svg.isNum(g.round_index) ? g.round_index : null, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
    rounds: roundModelDigest(epochRounds),
    waterfall: waterfallSteps(timeline).map((s) => [s.round_index, svg.isNum(s.from) ? s.from.toFixed(2) : null, svg.isNum(s.to) ? s.to.toFixed(2) : null, s.promoted, s.gen]),
    loss: [...lossLookup.entries()].sort(),
    board: board.map((b) => [b.entry_id, b.kind, b.weight, b.budget_s]),
    boardStatus: boardStatusDigest(boardStatus),
    // loop-communication panels: content-gated on their own rounded folds so
    // a no-op heartbeat (identical trajectory/cost/judge-trend) churns no DOM.
    loopTraj: trajectoryPanelDigest(loopTraj),
    loopCost: costPanelDigest(loopCost),
    judgeTrend: judgeTrendDigest(judgeTrend),
    calib: calib ? svg.calibrationTrendDigest(calib) : null,
    ledger: ledgerDigest(ledger),
    // the ledger's pending verdict pills read "racing…" only while THIS epoch's
    // loop runs, so their tense is rendered content and belongs in the digest.
    ledgerLive: epochIsLive(state, epochId) ? 1 : 0,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: `Epoch ${epochId}` }),
      el('div', { class: 'dn-objective' }, [
        el('div', { class: 'lab', text: 'objective' }),
        el('div', { class: 'txt', text: objectiveText(ep) }),
      ]),
      (tournament || contractHash) ? el('div', { class: 'dt-structure-line' }, [
        tournament ? structurePill(tournament.structure, tournament.params) : null,
        // the frozen CONTRACT identity — shortened like every other hash in the
        // tree (builder.js `shorten`), full value on hover.
        contractHash ? el('span', {
          class: 'dt-contract-hash dn-mono dn-faint', title: contractHash,
          text: 'contract ' + shortHash(contractHash),
        }) : null,
      ].filter(Boolean)) : null,
    ].filter(Boolean)));

    const promotedCount = gens.filter((g) => g.promoted).length;
    nodes.push(el('div', { class: 'dn-panel dn-row', style: 'margin-top:12px;' }, [
      stat(String(board.length), 'board entries'),
      stat(String(experiments.length), 'experiments'),
      stat(String(promotedCount), 'promoted'),
      stat(ep.closed ? 'closed' : 'open', 'state'),
      // ── the Δscalar AGGREGATES the payload computes for this header ──
      // Rendered as "—" tiles when the epoch recorded no finite delta of that
      // kind, exactly as build_epoch_view documents. The champion-spine sum
      // leads (it is the meta-loop's real progress); `gross` follows, captioned
      // so it is never mistaken for the headline.
      stat(spineDelta == null ? '—' : svg.fmtSigned(spineDelta, 2), 'Δ scalar · champion spine'),
      stat(grossDelta == null ? '—' : svg.fmtSigned(grossDelta, 2), 'Δ scalar · gross (all experiments)'),
    ]));
    nodes.push(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:6px 0 0;',
      text: 'Δ scalar sums the per-experiment scalar_score_delta · the champion-spine sum counts PROMOTED hops only — the meta-loop’s actual progress · the gross sum includes rejected challengers and never enters the lineage (lower = better)' }));

    // (NO in-content nav row here. The four buttons that used to sit under the
    // stat tiles — Generations / Boards / Mutation surface / Publication —
    // restated the rail's own epoch children one line below the rail that was
    // already showing them, selected state and all. The RAIL is canonical: it
    // carries every child this row did, plus Evals and Instrument, and it shows
    // where you are. They carried no state the rail lacks, so nothing folded in.)

    const briefText = ep.brief || '';
    // Default-open a short brief; but if the user has toggled it this session,
    // honour THEIR choice so a live re-render doesn't snap it shut again.
    const briefDefaultOpen = briefText.length < 1200;
    const briefOpen = _briefOpen.has(epochId) ? _briefOpen.get(epochId) : briefDefaultOpen;
    const briefDetails = el('details', { class: 'dn-brief', open: briefOpen ? '' : null }, [
      el('summary', null, [
        el('span', { class: 'chev', text: '▸' }), 'Proposer brief',
        el('span', { class: 'dn-faint', style: 'font-weight:400;font-size:11px;', text: briefText ? `· ${briefText.split(/\n/).length} lines` : '· none' }),
      ]),
      renderMarkdown(briefText),
    ]);
    // Capture the user's expand/collapse so the next re-render restores it.
    briefDetails.addEventListener('toggle', () => { _briefOpen.set(epochId, !!briefDetails.open); });

    // ---- THE EXPERIMENTS LEDGER (§3) — the epoch's roster of ideas ----
    // One row per experiment: the idea, the sites it touched, the verdict, its
    // Δ, and the reason. WHERE it sits is the point: a CLOSED epoch IS its
    // ledger — the whole story, settled — so it leads the page; an OPEN epoch's
    // live question is "what is happening now", so there the ledger follows the
    // round timeline. Null (a backend that does not serve the read) → omitted.
    const ledgerPanel = buildExperimentsLedger(ledger, {
      epochId, hrefFor: (gen) => ctx.href('candidate', { epochId, gen }),
      // an experiment with no recorded decision is "racing…" only while the
      // loop is running for this epoch; otherwise it went undecided (#207 §2).
      live: epochIsLive(state, epochId),
    });
    const ledgerSection = ledgerPanel
      ? section('Experiments · every idea this epoch tried', ledgerPanel) : null;
    if (ledgerSection && ep.closed) nodes.push(ledgerSection);

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
      // an IN-FLIGHT round (still proposing/applying, no settled record) has no
      // tournament figure yet — the proposing-step chips + the live banner ARE
      // its read. Returning null here also stops it from borrowing the SETTLED
      // aggregate model (racingFunnel/swissOver/elimOver) as a phantom figure.
      if (r.inflight) return null;
      // normalize the round's OWN field-tournament record (multi-round path).
      const stFromRef = r.tournamentRef
        ? normalizeStructure({
            structure: r.tournamentRef.structure || structure,
            structure_params: r.tournamentRef.structure_params || params,
            competitors: r.tournamentRef.competitors, rounds: r.tournamentRef.rounds,
            // the SERVED elim model rides the /api/tournaments record too.
            gen_states: r.tournamentRef.gen_states,
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
        // RACING: a per-round FIELD record carries `rounds: []` by design (rungs
        // live in the per-challenger records the resolver reconstructs), so
        // racingModel(stFromRef) is empty — never render that as an empty funnel.
        // Prefer the resolver-built aggregate model (single-round epochs, the
        // common case) and fall back to it when the round's own record has no
        // rungs, so this path can never diverge into the round-view-empty bug.
        let m = (stFromRef ? racingModel(stFromRef) : null);
        if (!m || !m.hasRungs) m = racingFunnel ? racingFunnel.model : m;
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
          rounds: m.rounds, gen_states: m.gen_states,
          championId: m.championId, benchmarkId: m.benchmarkId,
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
    // The waterfall (and the spine, below) plot a TRAJECTORY across rounds — they
    // need ≥2 rounds to mean anything. A single-round epoch (every run so far)
    // has no descent to draw, so we skip both and show just the round's episode
    // card; rendering an empty h=220 waterfall + a one-node spine read as broken.
    const wfSteps = waterfallSteps(timeline);
    if (!single && wfSteps.some((s) => svg.isNum(s.to) || svg.isNum(s.from))) {
      timelineCard.appendChild(el('div', { class: 'dn-roundtl-waterfall dn-figpane' }, [
        svg.waterfall({ steps: wfSteps, onRound: drill, onCompetitor: open }),
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

    // an OPEN epoch's ledger follows the timeline (see its construction above).
    if (ledgerSection && !ep.closed) nodes.push(ledgerSection);

    // ---- LOOP COMMUNICATION: the optimization trajectory + tournament cost ----
    // Rendered only when the read resolved with content (absent endpoint /
    // never-indexed workspace → byte-identical to today). The trajectory panel
    // is UNCERTAINTY-HONEST: a below-noise-floor window reads "no detectable
    // signal", and the measured floor is drawn as a band on the sparkline.
    const trajPanel = buildTrajectoryPanel(loopTraj, {
      onGen: (gid) => ctx.navigate('candidate', { epochId, gen: gid }),
    });
    if (trajPanel) nodes.push(section('Optimization trajectory · promoted lineage', trajPanel));
    const costPanel = buildCostPanel(loopCost, {
      onGen: (gid) => ctx.navigate('candidate', { epochId, gen: gid }),
    });
    if (costPanel) nodes.push(section('Tournament cost · wall-clock per promotion', costPanel));

    // ---- THE MEASUREMENT BAND: heatmap | per-judge trend | calibration ----
    // Three COMPACT figures that measure the instrument share one wrapping
    // multi-column band instead of each owning a full-width panel for a ~300px
    // figure. Each card keeps its own collapsed "?"-caption, so packing them
    // together costs no explanation.
    const rows = [...entryIds].sort().map((id) => ({ id, label: id }));
    const cols = gens.map((g) => ({ id: g.id, label: g.id }));
    const hmt = densityTokens();
    const hmCard = el('div', { class: 'dn-measure-card dn-figpane' }, [
      el('div', { class: 'dn-measure-head', text: 'Board entries × generations · drift loss' }),
    ]);
    if (rows.length && cols.length) {
      // The heatmap draws at its INTRINSIC cell size (viewBox + 'meet'): the
      // band column bounds it, it never inflates to fill one.
      hmCard.appendChild(svg.heatmap({
        rows, cols, cellW: Math.round(hmt.heatCell * 1.6), cellH: hmt.heatCell,
        value: (r, c) => (lossLookup.has(`${r}|${c}`) ? lossLookup.get(`${r}|${c}`) : null),
        // fix #7: a cell routes to the PER-BOARD cross-candidate view, keyed by
        // the entry id (the row) — NOT to an arbitrary candidate.
        onClick: (rId) => ctx.navigate('board', { epochId, entry: rId }),
      }));
      hmCard.appendChild(figCaption([
        'cell = drift loss for one board entry in one generation · denser ink = more drift',
        'click a row → that board across every candidate',
        'the small-multiples trellis lives in Boards',
      ]));
    } else {
      hmCard.appendChild(empty('No per-entry loss profiles yet (the index may not be built).'));
    }

    // Consumes the long-shipped /api/epoch/{id}/per-judge-trend read (its
    // first view consumer). Absent / empty → the card is simply omitted.
    const judgePanel = buildJudgeTrendPanel(judgeTrend);
    const calibPanel = buildCalibrationMini(calib, {
      onGen: (gid) => ctx.navigate('candidate', { epochId, gen: gid }),
    });
    const band = el('div', { class: 'dn-measure-band' },
      [hmCard, judgePanel, calibPanel].filter(Boolean));
    nodes.push(section('Measurement · where the loss lands, which judge carries it, is the proposer calibrated', band));

    // ---- BOARD STATUS: the train/holdout split + ladder + generalization gap.
    // Self-contained component; a board entry routes to its cross-candidate
    // view (the same target the heatmap rows use).
    nodes.push(renderBoardStatus(boardStatus, {
      onEntry: (entryId) => ctx.navigate('board', { epochId, entry: entryId }),
    }));
    return nodes;
  });
}

// A hash shortened for a header line — the same idiom the builder uses for
// `new_contract_hash` (views/builder.js `shorten`): first 12 chars + an
// ellipsis, with the caller putting the full value in a `title`.
export function shortHash(h, n) {
  const s = String(h == null ? '' : h).trim();
  const len = n || 12;
  return s.length > len ? s.slice(0, len) + '…' : s;
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

// ---- LOOP COMMUNICATION panels (pure builders — node-testable) --------

// The verdict WORD the trajectory panel prints (full honesty phrasing).
// loopVerdict owns every word that reports a problem, so the two surfaces
// cannot disagree about one; this panel adds the good news the fleet card
// deliberately stays quiet about. "improving" is spoken only when the promoted
// spine actually advanced — the reader owns that test, and a stalled loop now
// arrives here as "stalled", never as a green chip.
function verdictLine(traj) {
  const v = loopVerdict(traj);
  if (v) return v;
  if (traj && traj.verdict === 'improving') return { word: 'improving', cls: 'open' };
  return null;
}

// DELIBERATE NON-READ — `points[].namespace_values`.
//
// `/api/epoch/{id}/trajectory` carries a per-namespace value map on every spine
// point (query/loop_view.py build_optimization_trajectory). This panel reads
// `generation_id` + `scalar` and DROPS `namespace_values`, on purpose:
//
//   * A namespace value is only actionable at the moment it REGRESSES, and that
//     moment is already surfaced where the operator can act on it — the promote
//     gate's `namespace_monotonicity` rule, whose detail names the regressed
//     namespace (views/candidate.js gatePanel → the rules ladder).
//   * A namespace × spine table here would re-use the per-judge trend panel's
//     grammar (one row per component, a sparkline across the spine) without new
//     signal, and would grow with every namespace an epoch declares — the epoch
//     overview is already the densest page in the tree.
//
// This is recorded so the next "served but read by nothing" audit reads it as a
// decision rather than a gap. Revisit if an epoch ever gates on a namespace the
// gate ladder does not name.
//
// The OPTIMIZATION-TRAJECTORY panel for one /api/epoch/{id}/trajectory read.
// Null when the read is absent (Rust supervisor) or carries no points AND no
// promotion stats — the epoch view is then byte-identical to today.
export function buildTrajectoryPanel(traj, opts) {
  if (!traj || typeof traj !== 'object') return null;
  const points = Array.isArray(traj.points) ? traj.points : [];
  const vals = points.map((p) => (p && svg.isNum(p.scalar) ? p.scalar : null));
  const finite = vals.filter((v) => svg.isNum(v));
  const promo = promotionRateLabel(traj);
  if (!finite.length && !promo) return null;
  const o = opts || {};
  const card = el('div', { class: 'dn-panel dn-figpane dn-looptraj-pane' });

  // the stat row: promotion rate + the honest verdict + the floor readout.
  const rowKids = [];
  if (promo) rowKids.push(stat(promo, 'promotion rate'));
  const v = verdictLine(traj);
  if (v) {
    rowKids.push(el('div', { class: 'dn-stat' }, [
      el('span', { class: 'v' }, [chip(v.cls, v.word, 'dn-looptraj-verdict')]),
      el('span', { class: 'k', text: 'trajectory' }),
    ]));
  }
  const nf = traj.noise_floor;
  if (nf && svg.isNum(nf.max_abs_delta)) {
    rowKids.push(stat('±' + (Math.round(nf.max_abs_delta * 1000) / 1000), 'measured noise floor'));
  }
  if (svg.isNum(traj.recent_movement)) {
    rowKids.push(stat(String(Math.round(traj.recent_movement * 1000) / 1000), 'recent movement'));
  }
  if (rowKids.length) card.appendChild(el('div', { class: 'dn-row', style: 'margin-bottom:8px;' }, rowKids));

  // the promoted-lineage scalar sparkline with the noise-floor band.
  if (finite.length >= 1) {
    card.appendChild(svg.sparkline({
      width: 420, height: 64, values: vals, markers: true, goodDirection: 'down',
      responsive: true, noiseBand: noiseBandFor(traj, finite),
    }));
    const last = points[points.length - 1];
    if (last && last.generation_id && o.onGen) {
      // the spine ids as a compact clickable strip under the sparkline.
      const strip = el('div', { class: 'dn-looptraj-strip dn-mono' });
      for (const p of points) {
        const gid = p && p.generation_id;
        if (!gid) continue;
        const b = el('button', { class: 'dn-looptraj-gen', type: 'button', text: String(gid) });
        b.addEventListener('click', () => o.onGen(String(gid)));
        strip.appendChild(b);
      }
      card.appendChild(strip);
    }
  }
  card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
    'the champion floor across the promoted lineage (lower = better) · the shaded band is the measured A/A noise floor — movement inside it is indistinguishable from a re-roll'
    + (traj.verdict === 'no_signal' ? ' · recent movement sits below the floor: no detectable signal (below noise floor)' : '') }));
  return card;
}

// Digest fold for the trajectory panel: rounded, timestamp-free.
export function trajectoryPanelDigest(traj) {
  if (!traj || typeof traj !== 'object') return null;
  return [
    (Array.isArray(traj.points) ? traj.points : []).map((p) => [
      p && p.generation_id, p && svg.isNum(p.scalar) ? p.scalar.toFixed(3) : null,
    ]),
    traj.verdict || null,
    svg.isNum(traj.promotion_rate) ? traj.promotion_rate.toFixed(3) : null,
    svg.isNum(traj.challenger_count) ? traj.challenger_count : null,
    (traj.noise_floor && svg.isNum(traj.noise_floor.max_abs_delta))
      ? traj.noise_floor.max_abs_delta.toFixed(4) : null,
    svg.isNum(traj.recent_movement) ? traj.recent_movement.toFixed(4) : null,
  ];
}

// The TOURNAMENT-COST panel for one /api/epoch/{id}/cost read. Null when the
// read is absent or the epoch recorded no runs at all.
export function buildCostPanel(cost, opts) {
  if (!cost || typeof cost !== 'object') return null;
  const matchups = Array.isArray(cost.per_matchup) ? cost.per_matchup : [];
  const totalRuns = svg.isNum(cost.total_run_count) ? cost.total_run_count : 0;
  if (!matchups.length && totalRuns === 0) return null;
  const o = opts || {};
  const card = el('div', { class: 'dn-panel dn-loopcost-pane' });

  const cpp = costPerPromotionLabel(cost);
  card.appendChild(el('div', { class: 'dn-row', style: 'margin-bottom:8px;' }, [
    stat(cpp || '—', 'cost / promotion'),
    stat(fmtDurationMs(cost.total_runtime_ms || 0), 'total runtime'),
    stat(String(totalRuns), 'runs'),
    stat(String(cost.total_aborted_count || 0), 'aborted'),
  ]));

  if (matchups.length) {
    const table = dataTable({
      class: 'dn-loopcost-table',
      columns: [{ label: 'challenger' }, { label: 'decision' }, { label: 'runtime' }, { label: 'runs' }, { label: 'aborted' }],
      rows: matchups.filter((m) => m && typeof m === 'object').map((m) => {
        const gid = m.challenger_generation_id != null ? String(m.challenger_generation_id) : '—';
        let genEl;
        if (o.onGen && gid !== '—') {
          genEl = el('button', { class: 'dn-loopcost-gen', type: 'button', text: gid });
          genEl.addEventListener('click', () => o.onGen(gid));
        } else {
          genEl = el('span', { text: gid });
        }
        return {
          class: 'dn-loopcost-row', dataset: { gen: gid },
          cells: [
            { class: 'dn-mono', el: genEl },
            { class: m.decision === 'promoted' ? 'dn-good-t' : (m.decision === 'rejected' ? 'dn-faint' : ''), text: m.decision || '—' },
            { class: 'dn-mono', text: fmtDurationMs(m.runtime_ms || 0) },
            { class: 'dn-mono', text: String(m.run_count || 0) },
            { class: 'dn-mono' + ((m.aborted_count || 0) > 0 ? ' dn-bad-t' : ''), text: String(m.aborted_count || 0) },
          ],
        };
      }),
    });
    card.appendChild(table);
  }
  card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;',
    text: 'what a promotion costs in wall-clock — total challenger runtime ÷ promotions · per-challenger runtime/run/abort accounting' }));
  return card;
}

// Digest fold for the cost panel: integers only, timestamp-free.
export function costPanelDigest(cost) {
  if (!cost || typeof cost !== 'object') return null;
  return [
    svg.isNum(cost.cost_per_promotion_ms) ? Math.round(cost.cost_per_promotion_ms) : null,
    svg.isNum(cost.total_runtime_ms) ? Math.round(cost.total_runtime_ms) : 0,
    cost.total_run_count || 0,
    cost.total_aborted_count || 0,
    (Array.isArray(cost.per_matchup) ? cost.per_matchup : []).map((m) => [
      m && m.challenger_generation_id, m && m.decision,
      m && svg.isNum(m.runtime_ms) ? Math.round(m.runtime_ms) : 0,
      m && m.run_count, m && m.aborted_count,
    ]),
  ];
}

// ---- the PER-JUDGE TREND panel (pure builder — node-testable) ---------

// One row per judge: name · a sparkline of its weighted loss across the spine
// generations · the last value. Consumes the /api/epoch/{id}/per-judge-trend
// shape verbatim ({generations: [spine ids], judges: [{judge_name,
// by_generation}]}). Null when the read is absent (Rust supervisor), degraded
// (note, empty judges), or carries no judge with a plottable value — the
// epoch view is then byte-identical to today.
export function buildJudgeTrendPanel(trend) {
  if (!trend || typeof trend !== 'object') return null;
  const gens = Array.isArray(trend.generations) ? trend.generations : [];
  const judges = Array.isArray(trend.judges) ? trend.judges : [];
  if (!gens.length || !judges.length) return null;
  const rows = [];
  for (const j of judges) {
    if (!j || typeof j !== 'object' || !j.judge_name) continue;
    const by = (j.by_generation && typeof j.by_generation === 'object') ? j.by_generation : {};
    const vals = gens.map((g) => (svg.isNum(by[g]) ? by[g] : null));
    if (!vals.some((v) => svg.isNum(v))) continue;
    rows.push({ name: String(j.judge_name), vals });
  }
  if (!rows.length) return null;
  const card = el('div', { class: 'dn-measure-card dn-judgetrend-pane' }, [
    el('div', { class: 'dn-measure-head', text: 'Per-judge trend · weighted loss across the spine' }),
  ]);
  for (const r of rows) {
    const finite = r.vals.filter((v) => svg.isNum(v));
    const last = finite.length ? finite[finite.length - 1] : null;
    card.appendChild(el('div', { class: 'dn-judgetrend-row', 'data-judge': r.name }, [
      el('span', { class: 'dn-judgetrend-name', title: r.name, text: r.name }),
      // INTRINSIC width (the fleet-card treatment): the spark is 280px of real
      // trend, not a lane stretched to the pane — a stretched 'none' scale
      // flattens every slope and smears the end dot into an ellipse.
      svg.sparkline({ width: 280, height: 26, intrinsic: true, values: r.vals, markers: true, goodDirection: 'down' }),
      el('span', { class: 'dn-judgetrend-last', text: svg.isNum(last) ? last.toFixed(3) : '—' }),
    ]));
  }
  card.appendChild(figCaption([
    'each judge’s weighted loss across the promoted spine (lower = better)',
    'a diverging judge names WHICH pressure the loop is trading away',
  ]));
  return card;
}

// ---- the CALIBRATION MINI (pure builder — node-testable) --------------

// The third card of the Measurement band: the proposer's prediction-accuracy
// fraction across this epoch's lineage. Same read and figure the home view
// mounts (/api/calibration-trend), at card scale. Null when the read is absent
// (Rust supervisor) or carries no SCORED point, so an epoch with no falsifiable
// claims yet shows a two-card band rather than an empty frame.
export function buildCalibrationMini(calib, opts) {
  if (!calib || typeof calib !== 'object') return null;
  const points = Array.isArray(calib.points) ? calib.points : [];
  if (!points.some((p) => p && svg.isNum(p.score_fraction))) return null;
  const o = opts || {};
  const card = el('div', { class: 'dn-measure-card dn-figpane dn-caltrend-pane' }, [
    el('div', { class: 'dn-measure-head', text: 'Calibration · proposer prediction accuracy' }),
  ]);
  card.appendChild(svg.calibrationTrend({
    points, rolling_mean: calib.rolling_mean, trend_sign: calib.trend_sign,
    latest_fraction: calib.latest_fraction, n_scored: calib.n_scored,
    onGen: typeof o.onGen === 'function' ? o.onGen : null,
  }));
  const tsign = svg.isNum(calib.trend_sign) ? calib.trend_sign : 0;
  const trendWord = tsign > 0 ? 'improving' : tsign < 0 ? 'regressing' : 'flat / too few';
  const rm = svg.isNum(calib.rolling_mean) ? Math.round(calib.rolling_mean * 100) + '%' : '—';
  const lf = svg.isNum(calib.latest_fraction) ? Math.round(calib.latest_fraction * 100) + '%' : '—';
  card.appendChild(figCaption([
    'diagnostic — does not affect the gate · calibration ' + trendWord,
    'epoch mean ' + rm + ' of claims landed · latest ' + lf + ' · higher = better-calibrated',
    'click a generation → its candidate dossier',
  ]));
  return card;
}

// Digest fold for the per-judge trend: rounded per-judge series over the
// spine, timestamp-free — a no-op beat is byte-identical, a new generation
// column / a moved loss flips it.
export function judgeTrendDigest(trend) {
  if (!trend || typeof trend !== 'object') return null;
  const gens = Array.isArray(trend.generations) ? trend.generations : [];
  const judges = Array.isArray(trend.judges) ? trend.judges : [];
  return [
    gens.map((g) => String(g)),
    judges.map((j) => [
      j && j.judge_name,
      gens.map((g) => {
        const v = j && j.by_generation ? j.by_generation[g] : null;
        return svg.isNum(v) ? v.toFixed(3) : null;
      }),
    ]),
  ];
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

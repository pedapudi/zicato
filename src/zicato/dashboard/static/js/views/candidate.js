// js/views/candidate.js — CANDIDATE (one generation), comparison-first.
//
// Console IV's candidate screen is the P anchor folded with S's first-class
// side-by-side COMPARE. By default it reads ONE candidate: the lifecycle DAG,
// the per-board scoring dot-plot, ALL match-ups, and the STACKED promote gate.
// A "compare with…" picker SPLITS the detail into TWO candidates read side by
// side (lifecycle · match-ups · per-board scoring · promote gate, A | B). Each
// side paints into its own host so its digest gate fires independently.
//
// Round-5 fixes carried on EVERY candidate panel (A and B):
//   * fix #1 — the STACKED, non-overlapping PROMOTE GATE lives on THIS page.
//   * fix #2 — the lifecycle "patch" node is clickable → this candidate's
//     SIDE-BY-SIDE diff (views/diff.js), preserving the compare target.
//   * fix #3 — ALL match-ups the candidate was in (champion==gen ||
//     challenger==gen), not just one. v0 shows v0→v1 AND v0→v2.
//
// The compare target arrives as `route.cmp` (the 4th render arg the shell
// passes alongside `route.params`); a test may also call render with
// `{ epochId, gen, cmp }` as the 3rd arg.
//
// Data: /api/epoch, /api/lineage, /api/score-trajectory,
// /api/generation/{e}/{g}/per-entry, /api/tournaments,
// /api/round/{e}/{champ}/{chall}/gate,
// /api/run/{e}/{g}/{entry}/{expectations,per-judge}.

import { el, svgEl } from '../core/dom.js';
import { state } from '../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { attachHovercard } from '../hovercard.js';
import { lifecycleDag, rungProgression } from '../dag.js';
import { gatedSwap, section, subhead, empty, stat, verdictPill, pill, overrideChip, overrideDigest, decisionFor, decisionOf, densityTokens, prText, metricsDigest, truncate, hovercardBody, dataTable, deltaCell, ratingModel, ratingTripleDigest } from '../ui.js';
import { comparePicker, splitFrame } from '../compare.js';
import { candidateProgression, inflightForActiveEpoch, inflightForEntryGen, runProgressRatio, liveMatchupsForCandidate, liveBelongsToEpoch, resolveNonGauntletSt, racingModel, structureDigest, normalizeStructure } from './structure.js';
import { roundsFromTimeline, reignModel } from '../rounds.js';
import { deriveLiveStatus } from '../livestatus.js';
import { harmonografIsLive, harmonografLink, harmonografMini } from '../core/harmonograf.js';
import * as facets from '../facets.js';

export async function render(host, ctx, params, route) {
  params = params || {};
  // the compare target rides on the route (4th arg) or, for tests, on params.
  const cmpTarget = (route && route.cmp) || params.cmp || null;
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading candidate…' }));

  // Class A: derive the VIEWED epoch from the route param first, the current
  // epoch only as a fallback — and scope every read to it.
  const routeEpoch = params.epochId || null;
  const ep = await D.epoch(routeEpoch);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Candidate' }), empty('No current epoch.')]);
    return;
  }
  const epochId = routeEpoch || ep.epoch_id;
  const [rows, traj, bracket, timeline] = await Promise.all([
    D.generationsForEpoch(epochId), D.scoreTrajectory(epochId), D.bracket(epochId),
    D.roundTimeline(epochId),
  ]);
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const genList = rows.length
    ? rows.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: g.promoted == null ? null : !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: x.promoted == null ? null : !!x.promoted }));
  const allIds = genList.map((g) => g.id);
  const genId = (params.gen && allIds.includes(params.gen)) ? params.gen : (allIds[allIds.length - 1] || params.gen || null);

  if (!genId) {
    gatedSwap(host, 'no-cand', () => [el('h1', { class: 'dn-h1', text: 'Candidate' }), empty('No candidate selected.')]);
    return;
  }
  // A valid compare target: a DIFFERENT, known generation.
  const cmpId = (cmpTarget && allIds.includes(cmpTarget) && cmpTarget !== genId) ? cmpTarget : null;

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  // The REIGNING champion — the server-stamped pointer on the epoch payload
  // (the end of the promoted spine, or the seed). Never re-scanned client-side.
  const championId = (ep && ep.current_champion != null) ? String(ep.current_champion) : null;
  const championScalar = championId ? scalarByGen.get(championId) : null;
  // The match-ups: the COMPLETED feed (bracket.matchups) UNION the LIVE published
  // rounds (current-epoch-scoped). A candidate running its FIRST round shows up
  // in the live rounds before any match commits to bracket.matchups, so a live
  // run no longer reads "did not run in any round" while it is plainly racing.
  const staticMatchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups : [];
  const at = state.activeTournament;
  const liveForThisEpoch = liveBelongsToEpoch(epochId, { heartbeat: state.heartbeat, activeTournament: at });
  const liveMatchups = (at && liveForThisEpoch) ? liveMatchupsForCandidate(at, null) : [];
  // dedupe by champion>challenger>match_id — the static feed wins (it has the
  // hypothesis + committed decision); a live match only fills a NEW pair.
  const seenMatch = new Set(staticMatchups.map((m) => `${m.champion}>${m.challenger}`));
  const allMatchups = staticMatchups.slice();
  for (const m of liveMatchups) {
    const key = `${m.champion}>${m.challenger}`;
    if (seenMatch.has(key)) continue;
    seenMatch.add(key);
    allMatchups.push(m);
  }

  // The epoch's CONFIGURED tournament structure — a LIVE non-gauntlet run for
  // THIS epoch governs even before the contract records the block. Used for the
  // structure-aware pending terminal label on the lifecycle DAG.
  const tournament = (ep && ep.tournament && typeof ep.tournament === 'object') ? ep.tournament : null;
  let structure = (tournament && tournament.structure) || 'gauntlet';
  const liveStruct = (state.activeTournament && state.activeTournament.structure) || null;

  // LIVE in-flight board runs, CURRENT-EPOCH-SCOPED. A run in flight for a
  // FOREIGN epoch must not light up this candidate's board/dot-plot, so the set
  // is gated on the live run belonging to the viewed epoch (mirrors gens.js).
  const liveStatus = deriveLiveStatus({
    heartbeat: state.heartbeat, activeRuns: state.activeRuns, activeTournament: state.activeTournament,
  });
  const epochInflight = inflightForActiveEpoch(state.activeRuns, {
    heartbeat: state.heartbeat, activeTournament: state.activeTournament,
    running: liveStatus.running, epochId,
  });
  if (structure === 'gauntlet' && liveStruct && epochInflight.length) structure = liveStruct;

  // the CHAMPION REIGN model (the "reign ribbon"): one bar per champion across
  // the epoch's rounds — shown on a candidate panel only when THAT generation
  // became champion. Read off the SERVED round timeline (same source as the
  // epoch view); a null timeline yields no reigns (the ribbon is omitted).
  const reigns = reignModel(roundsFromTimeline({
    timeline, bracket, gens: genList, scalarBy: scalarByGen, structure, championId,
  }));

  // the live PROJECTED standing map ({gen: {scalar, boards_done, boards_total}})
  // from the current-epoch active tournament — so a candidate with NO settled
  // scalar yet shows its climbing PROJECTED scalar / Δ (marked "projected").
  const liveProjected = (at && liveForThisEpoch && at.projected && typeof at.projected === 'object') ? at.projected : {};

  // THE RACING FIELD MODEL — resolved through the SHARED resolveNonGauntletSt
  // (live-first → the SERVED settled racing field) so the candidate dossier's
  // field-relative racing panels read the SAME `st` the Match-ups / epoch /
  // per-round views do. The settled ladder comes off
  // `/api/epoch/{id}/racing-field` (the per-challenger join lives server-side
  // now); absent (null) → the dossier renders its honest empty racing state.
  const racingSt = (String(structure) === 'racing')
    ? resolveNonGauntletSt({
        structure: 'racing', epochId,
        liveRaw: liveForThisEpoch ? at : null,
        heartbeat: state.heartbeat, activeRuns: state.activeRuns,
        params: (tournament && tournament.params) || {},
        completedRecord: normalizeStructure(await D.racingField(epochId), false),
      }).st
    : null;

  // Resolve each side's full panel data (cached). Side B only when comparing.
  // The primary side (A) honours the entry drill-down param; the compare side
  // (B) reads its lifecycle clean.
  const sideA = await resolveCandidate(epochId, genId, genList, experiments, scalarByGen, championId, championScalar, allMatchups, params.entry || null, racingSt, epochInflight, liveProjected);
  const sideB = cmpId ? await resolveCandidate(epochId, cmpId, genList, experiments, scalarByGen, championId, championScalar, allMatchups, null, racingSt, epochInflight, liveProjected) : null;

  // the per-CANDIDATE visibility rating (the server-joined lineage triple;
  // distinct from the per-PAIR gate ratingBlock below). Absent on the Rust
  // lineage view / a pre-rating payload -> null -> the stat renders '—'.
  const ratingByGen = new Map(rows.map((g) => [String(g.generation_id), { elo: g.elo, elo_se: g.elo_se, elo_games: g.elo_games }]));
  sideA.rating = ratingByGen.get(String(genId)) || null;
  if (sideB) sideB.rating = ratingByGen.get(String(cmpId)) || null;

  const digest = JSON.stringify({
    epochId, genId, cmpId, entry: params.entry || null, structure,
    reigns: reigns.map((r) => [r.id, r.fromRound, r.toRound, r.current]),
    a: candidateDigest(sideA), b: sideB ? candidateDigest(sideB) : null,
    // the racing FIELD model the field-relative panels draw — folded in so a real
    // rung/field/projection change (the whole field, not just THIS candidate's
    // scalars) repaints the panels, but a no-op heartbeat stays byte-identical.
    racing: racingSt ? structureDigest(racingSt) : null,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1' }, [(sideA.node.promoted ? '♛ ' : '') + 'Candidate ' + genId + (cmpId ? `  vs  ${cmpId}` : '')]),
      el('p', { class: 'dn-lede', text: cmpId
        ? 'Two candidates side by side — lifecycle, promote gate, match-ups, and per-board scoring, A against B.'
        : (sideA.baseline
          ? 'The seed candidate (no parent) — it defines the loss floor for the epoch. Use “compare with…” to split this pane and read two candidates side by side.'
          : `Born from ${sideA.node.parent} by a patch; faced the board; met the champion at the gate. Use “compare with…” to read two candidates side by side.`) }),
    ]));

    // the compare affordance — sets the cmp route param (URL-encoded).
    nodes.push(el('div', { class: 'dt-cmp-bar' }, [
      comparePicker({
        label: 'compare with…',
        options: genList.map((g) => ({ id: g.id, label: g.id + (g.promoted ? ' ♛' : '') })),
        current: genId, value: cmpId,
        onChange: (v) => ctx.navigate('candidate', { epochId, gen: genId }, { cmp: v }),
      }),
      cmpId ? el('button', { class: 'dt-cmp-clear', type: 'button', text: 'clear comparison',
        onclick: () => ctx.navigate('candidate', { epochId, gen: genId }) }) : null,
    ].filter(Boolean)));

    nodes.push(splitFrame({
      a: { title: genId + (sideA.node.promoted ? ' ♛' : ''), sub: sideA.decision, build: (h) => paintCandidate(h, ctx, epochId, sideA, cmpId, true, !!cmpId, structure, reigns, bracket, liveProjected, racingSt) },
      b: cmpId ? { title: cmpId + (sideB.node.promoted ? ' ♛' : ''), sub: sideB.decision, build: (h) => paintCandidate(h, ctx, epochId, sideB, null, false, true, structure, reigns, bracket, liveProjected, racingSt) } : null,
      emptyTitle: 'no comparison',
      emptyPrompt: 'Choose a candidate above to compare its lifecycle, gate, match-ups and per-board scoring against ' + genId + '.',
    }));
    return nodes;
  });
}

// Resolve one candidate's full panel data (all cached reads). `entryParam`
// only applies to the primary (A) side's drill-down.
async function resolveCandidate(epochId, genId, genList, experiments, scalarByGen, championId, championScalar, allMatchups, entryParam, racingSt, epochInflight, liveProjected) {
  const node = genList.find((g) => g.id === genId) || { id: genId, parent: null, promoted: null };
  const baseline = !node.parent;
  const exp = experiments.find((x) => x.generation_id === genId) || null;
  // Class B: an unscored candidate (promoted == null, no resolved outcome) is
  // PENDING, never "rejected/dead branch".
  const decision = decisionFor({ promoted: node.promoted, parent: node.parent, exp });
  const mpts = exp && exp.hypothesis && Array.isArray(exp.hypothesis.mutation_points) ? exp.hypothesis.mutation_points.length
    : (exp && Array.isArray(exp.mutation_points) ? exp.mutation_points.length : null);

  const [pe, scorecard] = await Promise.all([
    D.perEntry(epochId, genId),
    // the proposer's PREDICTION-ACCURACY scorecard (DIAGNOSTIC — never the
    // gate): predicted-vs-realised movements + the calibration fraction. A
    // baseline (seed) made no falsifiable claim, so we skip the read for it.
    baseline ? Promise.resolve(null) : D.hypothesisAccuracy(epochId, genId),
  ]);
  const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];
  // per-generation mean continuous outcome (#18); null on the pre-score path.
  const meanScore = pe && svg.isNum(pe.mean_score) ? pe.mean_score : null;
  // The per-facet means this candidate RECORDED when it was scored
  // (BOARD-FORMAT.md §1.4) — read, never derived here. `[]` when the board
  // carries no `facet:` tag, so the facet table simply does not paint.
  // DIAGNOSTIC: a facet number is a place to look, never a verdict, so it
  // gets no verdict colour and no Δ-vs-champion treatment.
  const facetScores = facetRows(pe && pe.facet_scores);

  // The CHAMPION's per-board loss on the SAME boards/slice — so each lifecycle
  // circle, and the Σ node, can show candidate-vs-champion · Δ (the comparison
  // the gate actually performs). Matched by entry_id. When the candidate IS the
  // champion (or there is no champion / no parent), there is nothing to compare.
  let championLoss = {}, championSigma = null;
  if (championId && championId !== genId && !baseline) {
    const champPe = await D.perEntry(epochId, championId);
    const champEntries = (champPe && Array.isArray(champPe.entries)) ? champPe.entries : [];
    // representative (last) champion loss per entry_id — the racing-final /
    // full-board run, mirroring the lifecycle node's representative pick.
    const champByEntry = new Map();
    for (const ce of champEntries) {
      if (ce && ce.entry_id != null && svg.isNum(ce.drift_loss)) champByEntry.set(ce.entry_id, ce.drift_loss);
    }
    // restrict to the candidate's slice (its sampled boards) so Σ aligns.
    const sliceIds = new Set(entries.filter((e) => e && e.entry_id != null).map((e) => e.entry_id));
    let cs = 0, any = false;
    for (const id of sliceIds) {
      if (champByEntry.has(id)) { championLoss[id] = champByEntry.get(id); cs += champByEntry.get(id); any = true; }
    }
    championSigma = any ? cs : null;
  }
  // the candidate's Σ over its own slice (matches the lifecycle total).
  let candidateSigma = null;
  for (const e of entries) if (e && svg.isNum(e.drift_loss)) candidateSigma = (candidateSigma || 0) + e.drift_loss;
  const deltaSigma = (svg.isNum(candidateSigma) && svg.isNum(championSigma)) ? candidateSigma - championSigma : null;

  // the candidate's PATH through the tournament rungs (rung 0 → rung 1 →
  // racing-final, each Δ + survived/cut) — a pure projection of the SERVED
  // racing field payload. null for a gauntlet candidate / when unserved.
  const progression = candidateProgression(racingSt, genId);

  // fix #3 — EVERY matchup the candidate was in (as champion OR challenger).
  const mine = allMatchups.filter((m) => m.champion === genId || m.challenger === genId);

  // fix #1 — the gate(s) for this candidate. As challenger: its own round.
  // As champion: each defended round.
  const gateKeys = [];
  if (!baseline && node.parent) gateKeys.push({ champ: node.parent, chall: genId, role: 'as challenger' });
  for (const m of mine) {
    if (m.champion === genId && m.challenger) gateKeys.push({ champ: genId, chall: m.challenger, role: 'defended' });
  }
  const seenK = new Set();
  const gateSpecs = gateKeys.filter((k) => { const id = k.champ + '>' + k.chall; if (seenK.has(id)) return false; seenK.add(id); return true; });
  // WHICH JUDGE DECIDED THE ROUND. The gate payload names ONE `primary_driver`;
  // this read carries the ledger it was picked from — every judge's champion vs
  // challenger weighted loss and the signed Δ between them — so "the round
  // turned on judge X" is auditable rather than asserted. Fetched beside the
  // gates (same round coordinates, same cached failure-tolerant class); a
  // never-indexed workspace / the Rust supervisor reads null and the block is
  // simply omitted.
  const [gates, judgeComparisons] = await Promise.all([
    Promise.all(gateSpecs.map((k) => D.gate(epochId, k.champ, k.chall))),
    Promise.all(gateSpecs.map((k) => D.perJudgeComparison(epochId, k.champ, k.chall))),
  ]);
  const primaryGate = gates.find((g, i) => g && gateSpecs[i].role === 'as challenger') || null;
  const primaryDelta = primaryGate && svg.isNum(primaryGate.delta_scalar) ? primaryGate.delta_scalar : null;
  // The gate EXPLANATION for the lifecycle GATE node: which of the 3 rules was
  // the primary driver + the decisive numbers, read from the SAME gate payload
  // the Promote-gate panel renders (D.gate). The deciding rule is the first
  // rule that FIRED / failed (rules short-circuit in order); the scalar-margin
  // detail carries "needs ≤ <margin>", a monotonicity rule carries the
  // regressed predicate / namespace in its detail.
  const gateExplain = primaryGate ? deriveGateExplain(primaryGate) : null;

  let exps = null, judges = null, drillRow = null, drillHeader = null;
  if (entryParam) {
    [exps, judges, drillHeader] = await Promise.all([
      D.expectations(epochId, genId, entryParam),
      D.perJudgeForRun(epochId, genId, entryParam),
      // the run HEADER carries adk_session_id — the harmonograf deep-link
      // key (the per-entry index rows above do not surface it).
      D.runHeader(epochId, genId, entryParam),
    ]);
    drillRow = entries.find((e) => e.entry_id === entryParam) || null;
  }

  // LIVE — this candidate's in-flight board runs (current-epoch-scoped set,
  // filtered to THIS gen). Drives the BOARD lifecycle node + the per-board
  // dot-plot's "N running" live indicators.
  const inflight = inflightForEntryGen(epochInflight, null, genId);

  // CACHED-CHAMPION provenance (epoch-local): in fast mode the champion's
  // per-board scalars are REUSED from a prior epoch/run rather than re-executed
  // this round. The epoch's OWN per-entry rows carry `cached`/`source_epoch`/
  // `source_run`, so a cached champion shows its results with a "cached · from
  // <source_epoch>" badge — never "no board entries scored". The header tag
  // reflects the eval mode: any cached entry ⇒ "fast — champion reused".
  const cachedEntries = entries.filter((e) => e && e.cached);
  const cached = cachedEntries.length > 0;
  const cachedProvenance = cached ? {
    sourceEpoch: cachedEntries.find((e) => e.source_epoch)?.source_epoch || null,
    sourceRun: cachedEntries.find((e) => e.source_run)?.source_run || null,
  } : null;

  // the live PROJECTED standing for THIS candidate: shown on the headline only
  // when the gen has NO settled scalar yet (boards still streaming). `projDelta`
  // = projected − champion (lower is better), the projected analogue of Δ.
  const projRow = (liveProjected && typeof liveProjected === 'object') ? liveProjected[String(genId)] : null;
  const hasSettled = scalarByGen.has(String(genId)) && svg.isNum(scalarByGen.get(String(genId)));
  const projected = (!hasSettled && projRow && svg.isNum(projRow.scalar)) ? {
    scalar: projRow.scalar,
    boards_done: svg.isNum(projRow.boards_done) ? projRow.boards_done : null,
    boards_total: svg.isNum(projRow.boards_total) ? projRow.boards_total : null,
    delta: svg.isNum(championScalar) ? projRow.scalar - championScalar : null,
  } : null;

  // ── the RADAR SILHOUETTE model (single-generation study opt 2's folded-in
  // panel): the candidate's SHAPE vs the champion across the heterogeneous axes
  // the gate weighs — scalar (inverse), pass-rate, and each per-judge drift
  // component — with OUTER = better. Built from the SAME gate payload + per-board
  // slice the rest of the dossier reads, so a settled silhouette converges
  // byte-identically whether resolved live or from record. Lifecycle-aware: a
  // candidate with no settled scalar (boards still streaming) feeds its PROJECTED
  // scalar and ghosts the candidate polygon (live:true).
  const radar = buildRadarModel({
    primaryGate, championScalar,
    settledScalar: hasSettled ? scalarByGen.get(String(genId)) : null,
    projected, entries,
    // the BT rating rides off the primary gate; when present + fit, the radar's
    // scalar vertex carries the credible-interval band (absent → no band).
    rating: primaryGate && primaryGate.rating,
  });

  // the train→holdout GENERALIZATION triplet for THIS candidate — read off its
  // own experiment outcome record (issue #5: train_loss / holdout_loss /
  // generalization_gap, absent until the detector lands, so every read is
  // type-guarded). Rendered as a SMALL, width-capped supporting panel (the study
  // shrank it from a hero figure), never a crash when the triplet is absent.
  const generalization = buildGeneralizationModel(exp);

  // THE CHAMPION-GATE OPPONENT (racing). The racing-final match names both
  // sides in `competitors`; the one that is not this candidate is who it faced
  // at the gate. The candidate's tournament-path strip previously said "final ·
  // Δ · promoted/rejected" without ever naming the opponent.
  const finalOpponent = racingFinalOpponent(racingSt, genId);

  return {
    node, baseline, decision, mpts, entries, meanScore, facetScores, mine, gateSpecs, gates,
    judgeComparisons, finalOpponent,
    primaryDelta, championId, championScalar, scalarByGen, progression,
    championLoss, championSigma, candidateSigma, deltaSigma, gateExplain,
    entryParam, exps, judges, drillRow, drillHeader, inflight, cached, cachedProvenance,
    projected, radar, generalization, scorecard,
  };
}

// WHO THE CANDIDATE FACED AT THE CHAMPION GATE, from a normalized racing
// structure payload — the `racing-final` match's OTHER competitor. Returns null
// for a gauntlet candidate, an unserved field, or a candidate that never
// reached the final. PURE (node-testable).
export function racingFinalOpponent(st, genId) {
  if (!st || typeof st !== 'object' || genId == null) return null;
  if (String(st.structure || '') !== 'racing') return null;
  const id = String(genId);
  for (const r of (Array.isArray(st.rounds) ? st.rounds : [])) {
    const m = (r && Array.isArray(r.matches) && r.matches[0]) ? r.matches[0] : null;
    if (!m || String(m.match_id || '') !== 'racing-final') continue;
    const comps = (Array.isArray(m.competitors) ? m.competitors : []).map(String);
    if (comps.indexOf(id) < 0) return null;
    const other = comps.find((c) => c !== id);
    return other || null;
  }
  return null;
}

// The candidate's train→holdout generalization triplet from its experiment
// outcome — `{train, holdout, gap, tolerance}` — or null when none recorded.
// `gap` falls back to `holdout − train` when only the pair is present; the
// tolerance reads the per-experiment value when carried.
function buildGeneralizationModel(exp) {
  if (!exp || typeof exp !== 'object') return null;
  const train = svg.isNum(exp.train_loss) ? exp.train_loss : null;
  const holdout = svg.isNum(exp.holdout_loss) ? exp.holdout_loss : null;
  let gap = svg.isNum(exp.generalization_gap) ? exp.generalization_gap : null;
  if (gap == null && train != null && holdout != null) gap = holdout - train;
  if (train == null && holdout == null && gap == null) return null;
  const tolerance = svg.isNum(exp.generalization_tolerance) ? exp.generalization_tolerance
    : (svg.isNum(exp.tolerance) ? exp.tolerance : null);
  return { train, holdout, gap, tolerance };
}

// Prettify a raw scalar_components key for display (radar spoke, rule label).
// The component maps carry machine keys (`diff_complexity`, a judge_name, a
// namespace token); a known key gets a Title-cased human label, anything else
// passes through verbatim so a per-judge / namespace axis keeps its own name.
// Lean (a small map + a default), so it does not move the bundle-size budget.
const COMPONENT_LABELS = { diff_complexity: 'Diff complexity' };
function prettyComponentLabel(key) {
  const k = String(key == null ? '' : key);
  return COMPONENT_LABELS[k] || k;
}

// Build the radar silhouette model — `{axes:[{label,chal,champ}], raw:[{chal,
// champ,unit,better}], live}` — from a candidate's gate data the way the study
// computes it. Each axis is normalised 0..1 with OUTER = better:
//   • scalar      — champion vs candidate (settled or projected) Σ-loss, INVERSE
//                   normalised (lower loss → larger radius).
//   • pass-rate   — candidate pass-rate (from its per-board pass_fail) vs the
//                   champion's, recovered via the gate's delta_pass_rate; HIGHER
//                   is better so it maps directly (no inverse).
//   • per-judge   — each scalar component (per-judge weighted drift + any
//                   pass-rate / schema term) from gate.scalar_components,
//                   champion vs challenger, INVERSE normalised.
// A loss axis pair shares a padded local [lo,hi] so both points stay visible;
// the `raw` parallel array carries the underlying numbers for the hover tooltip.
// Returns null when fewer than 3 plottable axes exist (the radar needs ≥3).
export function buildRadarModel({ primaryGate, championScalar, settledScalar, projected, entries, rating }) {
  const axes = [];
  const raw = [];
  // a loss-type axis (lower = better) → inverse-normalised radius within a padded
  // local range spanning both points, so champ + cand are both legible.
  const lossAxis = (label, champ, chal, unit) => {
    if (!svg.isNum(champ) || !svg.isNum(chal)) return;
    let lo = Math.min(champ, chal), hi = Math.max(champ, chal);
    const pad = (hi - lo) * 0.6 || Math.max(0.02, Math.abs(hi) * 0.15) || 0.05;
    lo -= pad; hi += pad;
    const span = (hi - lo) || 1;
    const norm = (v) => Math.max(0.05, Math.min(1, 1 - (v - lo) / span));
    axes.push({ label, champ: norm(champ), chal: norm(chal) });
    raw.push({ champ, chal, unit: unit || 'loss', better: 'lower' });
  };

  // (1) scalar axis — candidate's settled scalar, else its projected scalar.
  const candScalar = svg.isNum(settledScalar) ? settledScalar
    : (projected && svg.isNum(projected.scalar) ? projected.scalar : null);
  const live = !svg.isNum(settledScalar) && projected && svg.isNum(projected.scalar);
  lossAxis('scalar', championScalar, candScalar, 'loss');

  // (1b) the BRADLEY–TERRY credible-interval BAND on the SCALAR axis vertex —
  // the candidate's strength is not a point but an interval, so the radar's
  // scalar vertex carries the CI as a radial band [chalLo, chalHi] in the SAME
  // 0..1 radius space as the vertex. We map the θ̂ CI half-width onto the axis
  // PROPORTIONALLY: the fractional uncertainty (ci half-width / |θ̂| spread) scales
  // the band around the plotted candidate radius. Attached only when the rating
  // is present AND the challenger CI fits AND a scalar axis was plotted; absent
  // → no band (byte-identical to the pre-rating radar).
  if (rating && rating.present && axes.length) {
    const chall = rating.challenger;
    const scAxis = axes[0]; // the scalar axis is plotted first.
    if (scAxis && scAxis.label === 'scalar' && chall && svg.isNum(chall.theta)
        && svg.isNum(chall.ci_lo) && svg.isNum(chall.ci_hi) && chall.ci_hi > chall.ci_lo) {
      // fractional CI half-width relative to the θ̂ magnitude (a unitless
      // uncertainty), clamped so a wild interval can't swamp the axis.
      const half = (chall.ci_hi - chall.ci_lo) / 2;
      const denom = Math.max(Math.abs(chall.theta), half, 1e-6);
      const frac = Math.min(0.45, half / denom);
      const r = scAxis.chal;
      scAxis.chalBand = { lo: Math.max(0.05, r - frac), hi: Math.min(1, r + frac) };
      raw[0] = Object.assign({}, raw[0], {
        ciLo: chall.ci_lo, ciHi: chall.ci_hi, theta: chall.theta,
      });
    }
  }

  // (2) pass-rate axis — higher = better, so it maps directly (no inverse). The
  // candidate's pass-rate is read off its own per-board pass_fail; the champion's
  // is recovered from the gate's delta_pass_rate (delta = challenger − champion).
  const passable = (Array.isArray(entries) ? entries : []).filter((e) => e && typeof e.pass_fail === 'boolean');
  if (passable.length) {
    const candRate = passable.filter((e) => e.pass_fail === true).length / passable.length;
    const dpr = primaryGate && svg.isNum(primaryGate.delta_pass_rate) ? primaryGate.delta_pass_rate : null;
    const champRate = dpr != null ? Math.max(0, Math.min(1, candRate - dpr)) : null;
    if (champRate != null) {
      // a rate axis: both already in 0..1; map a small band around them so the
      // shape reads (a 0-vs-0 or 1-vs-1 pair still plots at the rim / centre).
      axes.push({ label: 'pass-rate', champ: Math.max(0.05, champRate), chal: Math.max(0.05, candRate) });
      raw.push({ champ: champRate, chal: candRate, unit: 'rate', better: 'higher' });
    }
  }

  // (3) per-judge / per-component axes — from the gate's scalar_components, the
  // exact per-component champion-vs-challenger contributions the gate weighs.
  const sc = primaryGate && primaryGate.scalar_components;
  if (sc && sc.champion && sc.challenger) {
    const keys = [...new Set([...Object.keys(sc.champion), ...Object.keys(sc.challenger)])].sort();
    for (const k of keys) {
      // a known machine key (e.g. diff_complexity) gets its human label on the
      // spoke + the hover tip; a per-judge / namespace key passes through.
      lossAxis(prettyComponentLabel(k), sc.champion[k], sc.challenger[k], 'drift');
    }
  }

  // ≥3 axes → a plottable silhouette (settled or ghosted-projected). A LIVE
  // (racing / in-flight) candidate with only the scalar-inverse + a landed
  // pass-rate axis (2) is below the radar's plottable minimum, but we still
  // return the model with `projectedOnly:true` so the dossier renders the
  // racing affordance ("settled comparisons appear once boards finish") instead
  // of dropping the panel entirely.
  if (axes.length >= 3) return { axes, raw, live: !!live, projectedOnly: false };
  if (live && axes.length >= 1) return { axes, raw, live: true, projectedOnly: true };
  return null;
}

// Read one gate payload (D.gate) into the lifecycle GATE node's explanation:
// the deciding rule (first fired / failed, since rules short-circuit in order),
// its label, the Δ scalar, the promote margin (parsed from the scalar-margin
// rule's detail), and — for a monotonicity rejection — the regressed
// predicate / namespace (parsed from that rule's detail). This is the data that
// resolves "smaller Σ but rejected": a challenger can lose on rule 2 / 3 even
// when its scalar is better.
function deriveGateExplain(gate) {
  const rules = Array.isArray(gate.rules) ? gate.rules : [];
  const decision = decisionOf(gate) || 'pending';
  // THE SERVER NAMES THE DECIDING RULE (`deciding_rule` — the one rule it set
  // `fired` on). The client only looks the rule row up for its label/detail;
  // it never re-infers the verdict from the rule list or scrapes free text.
  const decidingId = (typeof gate.deciding_rule === 'string' && gate.deciding_rule) ? gate.deciding_rule : null;
  const deciding = decidingId ? (rules.find((r) => r && r.id === decidingId) || null) : null;
  const deltaScalar = svg.isNum(gate.delta_scalar) ? gate.delta_scalar : null;
  // structured server fields — the promote margin + the regressed predicate /
  // namespace a fired monotonicity rule named. Absent ⇒ unknown (never parsed
  // out of the display-only `detail` string).
  const margin = svg.isNum(gate.margin) ? gate.margin : null;
  let regressed = (typeof gate.regressed_predicate === 'string' && gate.regressed_predicate) ? gate.regressed_predicate
    : ((typeof gate.regressed_namespace === 'string' && gate.regressed_namespace) ? gate.regressed_namespace : null);
  // the gate's own primary_driver (a judge name) is the fallback regressed
  // identifier when no monotonicity rule named one.
  if (!regressed && gate.primary_driver && gate.primary_driver.judge) regressed = gate.primary_driver.judge;
  return {
    decision,
    decidingRule: decidingId,
    decidingLabel: deciding ? (deciding.label || deciding.id || null) : (decidingId || null),
    // The deciding rule's raw detail string, scope-agnostic — display only.
    detail: deciding ? (deciding.detail || null) : null,
    deltaScalar, margin, regressed,
    reason: gate.reason || null,
  };
}

// A headline stat in the PROJECTED treatment — the value reads in the projected
// tone with a "proj" badge + a scored board-progress sub-bar (boards_done/total)
// so an in-flight candidate's projected scalar / Δ is visibly NOT a settled one.
function projStat(value, key, proj) {
  const bd = proj && svg.isNum(proj.boards_done) ? proj.boards_done : null;
  const bt = proj && svg.isNum(proj.boards_total) ? proj.boards_total : null;
  const frac = (bd != null && bt != null && bt > 0) ? Math.min(1, bd / bt) : null;
  return el('div', { class: 'dn-stat dt-proj', title: 'projected — boards still streaming in' }, [
    el('span', { class: 'v dt-proj-val' }, [
      el('span', { text: value }),
      el('span', { class: 'dt-proj-badge', text: 'proj' }),
    ]),
    el('span', { class: 'k', text: key }),
    frac != null ? el('span', { class: 'dt-proj-bar', title: bd + '/' + bt + ' boards scored' }, [
      el('span', { class: 'dt-proj-bar-fill', style: 'width:' + Math.round(frac * 100) + '%;' }),
    ]) : null,
    frac != null ? el('span', { class: 'dt-proj-bar-lab', text: bd + '/' + bt }) : null,
  ].filter(Boolean));
}

function candidateDigest(s) {
  return {
    gen: s.node.id, parent: s.node.parent, decision: s.decision, championId: s.championId,
    // the visibility rating stat (int register) — a reindex that moves the
    // rating repaints the dossier; unrated folds null (pre-rating shape).
    rating: ratingTripleDigest(s.rating),
    champScalar: svg.isNum(s.championScalar) ? s.championScalar.toFixed(3) : null,
    delta: svg.isNum(s.primaryDelta) ? s.primaryDelta.toFixed(3) : null,
    // the live PROJECTED standing — ROUNDED scalar/Δ + integer board counts so a
    // no-op heartbeat is byte-identical, a board landing repaints (anti-flash).
    projected: s.projected ? [
      svg.isNum(s.projected.scalar) ? s.projected.scalar.toFixed(3) : null,
      svg.isNum(s.projected.delta) ? s.projected.delta.toFixed(3) : null,
      s.projected.boards_done == null ? '?' : s.projected.boards_done,
      s.projected.boards_total == null ? '?' : s.projected.boards_total,
    ] : null,
    mpts: s.mpts,
    // the candidate-vs-champion comparison + gate-rule explanation surfaced on
    // the lifecycle DAG — part of the digest so a change repaints (no flashing).
    champLoss: s.championLoss ? Object.keys(s.championLoss).sort().map((k) => [k, s.championLoss[k].toFixed(3)]) : null,
    candSigma: svg.isNum(s.candidateSigma) ? s.candidateSigma.toFixed(3) : null,
    champSigma: svg.isNum(s.championSigma) ? s.championSigma.toFixed(3) : null,
    deltaSigma: svg.isNum(s.deltaSigma) ? s.deltaSigma.toFixed(3) : null,
    gateExplain: s.gateExplain ? [s.gateExplain.decidingRule, s.gateExplain.decision,
      svg.isNum(s.gateExplain.deltaScalar) ? s.gateExplain.deltaScalar.toFixed(3) : null,
      svg.isNum(s.gateExplain.margin) ? s.gateExplain.margin.toFixed(3) : null, s.gateExplain.regressed] : null,
    // the RADAR silhouette model — folded in so a change to any axis (scalar,
    // pass-rate, per-judge) or its live/projected state repaints, but a no-op
    // heartbeat stays byte-identical. Delegates to svg.radarSilhouetteDigest.
    radar: s.radar ? svg.radarSilhouetteDigest({ axes: s.radar.axes, live: s.radar.live }) + (s.radar.projectedOnly ? '·proj-only' : '') : null,
    // the train→holdout generalization triplet (rounded) so a change repaints
    // and a no-op beat stays equal.
    generalization: s.generalization ? [
      svg.isNum(s.generalization.train) ? s.generalization.train.toFixed(3) : null,
      svg.isNum(s.generalization.holdout) ? s.generalization.holdout.toFixed(3) : null,
      svg.isNum(s.generalization.gap) ? s.generalization.gap.toFixed(3) : null,
      svg.isNum(s.generalization.tolerance) ? s.generalization.tolerance.toFixed(3) : null,
    ] : null,
    // the proposer PREDICTION-ACCURACY scorecard (DIAGNOSTIC) — the calibration
    // fraction + each claim's predicted/observed direction + its hit/miss/band/
    // unpredicted verdict, folded in so a claim resolving (a movement landing,
    // the fraction moving) repaints, but a no-op heartbeat stays byte-identical.
    // null (seed / no experiment / no claims) → contributes NOTHING → the dossier
    // digest is byte-identical to the pre-feature path (back-compat clean).
    scorecard: scorecardDigest(s.scorecard),
    // entries fold the continuous score + its precision/recall metrics (#18)
    // so a scored board repaints when its score/metrics move, but stays
    // byte-identical on a no-op heartbeat. A bool-only entry contributes
    // null for both (back-compat: unchanged digest vs the pre-score path).
    entries: s.entries.map((e) => [e.entry_id, svg.isNum(e.drift_loss) ? e.drift_loss.toFixed(3) : null, e.pass_fail, !!e.wall_clock_budget_exceeded, e.rung || null, e.match_id || null, !!e.cached, svg.isNum(e.score) ? e.score.toFixed(3) : null, metricsDigest(e.metrics)]),
    // per-generation mean continuous outcome (#18); null on the pre-score path.
    meanScore: svg.isNum(s.meanScore) ? s.meanScore.toFixed(3) : null,
    // Facet numbers fold at their RENDERED precision so a no-op heartbeat
    // leaves the digest byte-identical and the table's DOM nodes survive
    // (G10). A board with no facet tag contributes empty — unchanged digest
    // vs the pre-facet path. Every count the cell can print folds too: `ran`
    // reaches the DOM through facetCount, so a digest blind to it would pin
    // a stale denominator in place.
    facets: [...s.facetScores.rows, s.facetScores.overall].filter(Boolean).map((f) => [
      f.name,
      svg.isNum(f.scalar) ? f.scalar.toFixed(2) : null,
      svg.isNum(f.mean) ? f.mean.toFixed(2) : null,
      f.scored, f.ran, f.total,
    ]),
    cached: s.cached ? [s.cachedProvenance && s.cachedProvenance.sourceEpoch, s.cachedProvenance && s.cachedProvenance.sourceRun] : null,
    progression: s.progression && Array.isArray(s.progression.stages)
      ? s.progression.stages.map((st) => [st.label, st.kind, svg.isNum(st.delta) ? st.delta.toFixed(2) : null, st.verdict]) : null,
    // WHO the candidate met at the champion gate — printed on the tournament-path
    // strip, so a re-resolved final that changes the opponent must repaint. null
    // (gauntlet / never reached the final) → pre-feature digest (back-compat).
    finalOpponent: s.finalOpponent || null,
    matchups: s.mine.map((m) => [m.champion, m.challenger, m.decision, svg.isNum(m.delta_scalar) ? m.delta_scalar.toFixed(2) : null]),
    gates: s.gates.map((g, i) => g && Array.isArray(g.rules)
      ? [s.gateSpecs[i].champ, s.gateSpecs[i].chall, s.gateSpecs[i].role, g.decision, svg.isNum(g.delta_scalar) ? g.delta_scalar.toFixed(3) : null, g.rules.map((r) => [r.id, r.status, r.fired]),
        // scalar-provenance decomposition (#19) folded in so a change to which
        // transform/plugin shaped a side — or a fail-open event firing —
        // repaints the gate, but a no-op heartbeat stays byte-identical. null
        // (built-in / pre-#19) contributes nothing new (back-compat digest).
        decompDigest(g.scalar_decomposition),
        // operator-override provenance folded in (kind+action+state+reason, NO
        // timestamp) so an override appearing/changing repaints the gate while a
        // no-op beat stays byte-identical. null (none) → pre-override digest.
        overrideDigest(g.override),
        // the ABSOLUTE-scalar endpoints (champion/challenger scalar + the live
        // projected challenger + integer board counts) folded in so a board
        // landing or a settle repaints the gate head, but a no-op heartbeat
        // stays byte-identical. null (neither side resolves) → pre-feature
        // digest (back-compat clean).
        absoluteScalarsDigest(g),
        // the BRADLEY–TERRY uncertainty pre-gate (rounded θ̂/CI/P + the duel
        // counts + the next_duel pair + the ci_history P-trace, NO timestamps)
        // folded in so a duel resolving / a CI tightening / P moving repaints the
        // gate, but a no-op heartbeat stays byte-identical. null (no rating /
        // present:false) contributes nothing → pre-feature digest (back-compat).
        ratingDigest(g.rating),
        // the DIFF-COMPLEXITY parsimony line item (the two rounded per-side
        // diff-complexity costs, NO timestamp) folded in so the patch being
        // re-scored — or the term appearing (weight turned on) — repaints the
        // gate ladder, but a no-op heartbeat stays byte-identical. null (weight 0
        // / pre-feature) contributes nothing → pre-feature digest (back-compat).
        diffComplexityDigest(g)]
      : null),
    // the PER-JUDGE COMPARISON ledger rendered beside each gate — every judge's
    // champion / challenger weighted loss + Δ, plus the server's primary_driver.
    // Folded at the RENDERED precision so a no-op beat stays byte-identical; a
    // judge's side resolving (or the driver changing hands) repaints. null
    // (unserved / no judges) contributes nothing → pre-feature digest.
    judgeCmp: (s.judgeComparisons || []).map((c) => judgeComparisonDigest(c)),
    drill: s.entryParam || null,
    drillExp: s.exps && Array.isArray(s.exps.outcomes) ? s.exps.outcomes.map((o) => [o.kind, o.passed, o.judge_name, o.detail]) : null,
    // the per-judge drill folds RAW loss + WEIGHT beside the weighted value —
    // all three are rendered, so all three must gate the swap (a re-weighted
    // judge moves `weight` + `weighted_loss` while `raw_loss` holds still).
    drillJudge: s.judges && Array.isArray(s.judges.judges) ? s.judges.judges.map((j) => [
      j.judge_name,
      svg.isNum(j.weighted_loss) ? j.weighted_loss.toFixed(3) : null,
      svg.isNum(j.raw_loss) ? j.raw_loss.toFixed(3) : null,
      svg.isNum(j.weight) ? j.weight.toFixed(3) : null,
    ]) : null,
    // harmonograf deep-link state — folded in so the link appears/disappears
    // when liveness flips (server up ⇄ run ended) without a no-op-beat repaint.
    hgLive: harmonografIsLive(),
    hgSession: (s.drillHeader && s.drillHeader.adk_session_id) || null,
    // LIVE in-flight board runs for this candidate — folded into the digest so a
    // beat that advances progress repaints, but a no-op heartbeat stays equal.
    inflight: Array.isArray(s.inflight) ? s.inflight.map((r) => {
      const pr = runProgressRatio(r);
      return [r.entry_id != null ? r.entry_id : null,
        r.run_id || null, pr != null ? pr.toFixed(2) : null];
    }).sort() : null,
  };
}

// Paint ONE candidate's full lifecycle panel into `host`. `cmpId`, when set, is
// the compare target to PRESERVE while drilling into a sub-node. `isPrimary`
// gates the entry drill-down (B reads its lifecycle clean). `narrow` is the
// SPLIT-LAYOUT flag — true for BOTH panes whenever a comparison is shown — and
// drives the figure viewBox WIDTH (so A and B scale identically), independent
// of the per-side `cmpId` (which is null on B but the layout is still split).
function paintCandidate(host, ctx, epochId, s, cmpId, isPrimary, narrow, structure, reigns, bracket, liveProjected, racingSt) {
  const opts = cmpId ? { cmp: cmpId } : undefined;
  const node = s.node;
  const genId = node.id;
  const baseline = s.baseline;
  const championId = s.championId;
  const championScalar = s.championScalar;

  // PROJECTED headline — a candidate with no SETTLED scalar yet (boards still
  // streaming) shows its live PROJECTED scalar / Δ: "~<value>" + a "proj" badge
  // + the dimmed/dashed treatment, distinct from a committed number.
  const settledScalar = svg.isNum(s.scalarByGen.get(genId)) ? s.scalarByGen.get(genId) : null;
  const proj = (settledScalar == null && s.projected) ? s.projected : null;
  const scalarStat = proj
    ? projStat('~' + svg.fmt(proj.scalar, 1), 'scalar (loss)', proj)
    : stat(settledScalar != null ? svg.fmt(settledScalar, 1) : '—', 'scalar (loss)');
  const deltaStat = (proj && svg.isNum(proj.delta))
    ? projStat('~' + svg.fmtSigned(proj.delta, 1), 'Δ vs champion', proj)
    : stat(svg.isNum(s.primaryDelta) ? svg.fmtSigned(s.primaryDelta, 1) : '—', 'Δ vs champion');
  // the visibility rating stat (quiet-precision): `1512 ±34 · 7 games`; a
  // thin sample declines the point estimate — `provisional · 2 games` (the
  // ratingBlock forming-state honesty precedent); unrated reads '—'.
  const rm = ratingModel(s.rating);
  const ratingStat = stat(
    rm
      ? (rm.provisional
        ? 'provisional · ' + rm.games + (rm.games === 1 ? ' game' : ' games')
        : rm.text + (rm.games != null ? ' · ' + rm.games + (rm.games === 1 ? ' game' : ' games') : ''))
      : '—',
    'rating');
  host.appendChild(el('div', { class: 'dn-panel dn-row' }, [
    scalarStat,
    deltaStat,
    ratingStat,
    stat(node.parent || 'seed', 'parent'),
    el('div', { class: 'dn-stat' }, [verdictPill(baseline ? 'baseline' : s.decision)]),
  ]));

  // ── the REIGN RIBBON — shown ONLY for a generation that became champion.
  // A reignGantt across the epoch's rounds, with THIS generation's tenure the
  // highlighted (accent / ♛ current, ink / ♔ former) segment of the spine.
  const reignList = Array.isArray(reigns) ? reigns : [];
  if (reignList.some((r) => String(r.id) === String(genId))) {
    const maxRound = Math.max(1, ...reignList.map((r) => (svg.isNum(r.toRound) ? r.toRound : 0)));
    host.appendChild(el('div', { class: 'dn-panel dn-figpane dn-reignribbon' }, [
      el('div', { class: 'dn-reignribbon-cap dn-faint', text: genId + ' reign · its tenure across the epoch’s rounds' }),
      svg.reignGantt({
        reigns: reignList, rounds: maxRound,
        onCompetitor: (id) => { if (id && String(id) !== String(genId)) ctx.navigate('candidate', { epochId, gen: id }, opts); },
      }),
    ]));
  }

  // CACHED-CHAMPION eval-mode tag: when this candidate's per-board results were
  // REUSED (fast mode) rather than re-executed this round, surface the mode so a
  // cached champion is never read as a fresh run. (full ⇒ no tag.)
  if (s.cached) {
    const src = s.cachedProvenance && s.cachedProvenance.sourceEpoch;
    host.appendChild(el('div', { class: 'dn-cached-tag dn-faint', 'data-eval-mode': 'fast' }, [
      el('span', { class: 'dn-cached-tag-pill', text: 'fast — champion reused' }),
      src ? el('span', { class: 'dn-mono', text: ' · from ' + src }) : null,
    ].filter(Boolean)));
  }

  // ---- lifecycle DAG (patch node clickable → diff, fix #2) ----
  // FIT-TO-WIDTH: the DAG is a responsive SVG (width:100% + viewBox) painted
  // straight into the panel — NO overflow-x wrapper, so all six stages (parent
  // → patch → board → Σ → gate → terminal) are visible without sideways
  // scrolling. Density scales the figure's vertical SIZE (row step + height).
  const dt = densityTokens();
  const dagCard = el('div', { class: 'dn-panel dn-figpane' });

  // the rung-PROGRESSION strip (racing candidates): rung 0 → rung 1 → final,
  // each with its Δ-vs-champion + won/cut — relates the board runs to the
  // tournament rounds even when the per-run records carry no rung tags. Absent
  // for a gauntlet candidate (one matchup, no rungs → no strip).
  if (s.progression && Array.isArray(s.progression.stages) && s.progression.stages.length) {
    // NAME THE OPPONENT. The strip's `final` stage prints "final · Δ · promoted/
    // rejected" — the one thing it never said is WHO the candidate faced at the
    // champion gate. The racing-final match carries both sides; the opponent is
    // the other one. Absent (gauntlet / never reached the final) → no chip, so
    // the strip is byte-identical to before.
    const reachedFinal = s.progression.stages.some((st) => st.kind === 'final');
    dagCard.appendChild(el('div', { class: 'dn-rungprog-strip' }, [
      el('span', { class: 'dn-rungprog-cap dn-faint', text: 'tournament path' }),
      rungProgression({ stages: s.progression.stages, width: narrow ? 480 : 720 }),
      (reachedFinal && s.finalOpponent) ? el('span', {
        class: 'dn-rungprog-opponent dn-faint dn-mono',
        title: 'the champion this candidate faced at the gate',
        text: 'vs ' + s.finalOpponent,
      }) : null,
    ].filter(Boolean)));
  }

  dagCard.appendChild(lifecycleDag({
    genId, parentId: node.parent, baseline, promoted: node.promoted, decision: s.decision,
    // structure-aware pending terminal label (swiss → "⋯ competing", elim → "⋯
    // in bracket", racing → "⋯ racing"), so an in-flight non-racing candidate
    // does not wrongly read "racing".
    structure,
    deltaScalar: s.primaryDelta, patchPoints: s.mpts, entries: s.entries,
    // candidate-vs-champion comparison (so the circles + Σ explain the Δ the
    // gate sees) and the gate-rule explanation (which of the 3 rules decided).
    championId, championLoss: s.championLoss, championSigma: s.championSigma,
    candidateSigma: s.candidateSigma, deltaSigma: s.deltaSigma, gateExplain: s.gateExplain,
    // height is NO LONGER passed: lifecycleDag now DERIVES its viewBox height
    // from the (deduped) board-node count × a fixed row pitch, so the seed/
    // baseline (full board) and a racing challenger (deduped slice) render with
    // IDENTICAL per-row spacing and a spine centred on the fan — neither side
    // stretched/compressed, no large empty top band. Only `width` (the internal
    // viewBox width, narrower in the compare split) is still supplied. Keyed on
    // the split-layout flag (true for BOTH compare panes) — NOT the per-side
    // `cmpId` — so A and B fit-to-width at the SAME scale (no shrunken B pane).
    width: narrow ? 560 : 900,
    onEntry: (eid) => ctx.navigate('candidate', { epochId, gen: genId, entry: eid }, opts),
    onRun: (eid) => ctx.navigate('board', { epochId, entry: eid, gen: genId }),
    onPatch: baseline ? null : () => ctx.navigate('diff', { epochId, gen: genId }),
  }));
  // ONE concise caption — the verbose parent→patch→…→terminal walkthrough, the
  // 3-rule gate detail and the click/hover affordances moved into the figure's
  // "?" info hovercard (and the GATE/Σ hovercards), so the figure reads clean at
  // a glance with detail on demand (de-crowd).
  dagCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: baseline ? 'parent → patch → board → Σ → gate → terminal · click a board node → its drill-down' : 'parent → patch → board → Σ → gate → terminal · hover the “?” for how to read it' }));
  host.appendChild(section('Lifecycle · cause → effect → verdict', dagCard));

  // ---- LIVE — this candidate's in-flight board runs (current-epoch-scoped) ----
  // A candidate mid-run reads "N running" with per-board progress, NOT a static
  // page. Rendered for ANY structure (active-runs is structure-agnostic); the
  // dot-plot below covers COMPLETED boards, this covers the ones still running.
  const inflight = Array.isArray(s.inflight) ? s.inflight : [];
  if (inflight.length) {
    const liveCard = el('div', { class: 'dn-panel dn-board-inflight' });
    // a SCORED board-progress sub-bar (boards_done/boards_total) from the live
    // projected standing — distinct from each run's own time-progress bar below.
    const pj = s.projected;
    const sbd = pj && svg.isNum(pj.boards_done) ? pj.boards_done : null;
    const sbt = pj && svg.isNum(pj.boards_total) ? pj.boards_total : null;
    const sfrac = (sbd != null && sbt != null && sbt > 0) ? Math.min(1, sbd / sbt) : null;
    liveCard.appendChild(el('div', { class: 'dn-inflight-head' }, [
      el('span', { class: 'dn-inflight-pill' }, [
        el('span', { class: 'dn-inflight-pulse', 'aria-hidden': 'true' }),
        el('span', { text: 'live' }),
      ]),
      el('span', { class: 'dn-inflight-count', text: String(inflight.length) + (inflight.length === 1 ? ' board running' : ' boards running') }),
      el('span', { class: 'dn-faint', text: ' for this candidate' }),
      sfrac != null ? el('span', { class: 'dt-proj-badge', text: 'proj' }) : null,
      sfrac != null ? el('span', { class: 'dt-proj-bar', title: sbd + '/' + sbt + ' boards scored (projected)' }, [
        el('span', { class: 'dt-proj-bar-fill', style: 'width:' + Math.round(sfrac * 100) + '%;' }),
      ]) : null,
      sfrac != null ? el('span', { class: 'dt-proj-bar-lab', text: sbd + '/' + sbt + ' scored' }) : null,
    ].filter(Boolean)));
    const tbl = dataTable({
      class: 'dn-board-table dn-inflight-table',
      columns: [{ label: 'board' }, { label: 'run' }, { label: 'progress' }, { label: 'execution' }],
      rows: inflight.map((r) => {
        const eid = r.entry_id != null ? r.entry_id : '—';
        const pr = runProgressRatio(r);
        const pct = pr != null ? Math.round(pr * 100) : null;
        // a per-run harmonograf "execution ▸" link — liveness-gated (these are
        // in-flight runs, so the auto-launched server is up) and stop-propagated
        // so the cell click does not also navigate the row. Renders nothing when
        // not live / no harmonograf url (harmonografMini returns null).
        const exec = harmonografMini(r, 'execution', 'open this run’s harmonograf trace');
        if (exec) exec.addEventListener('click', (ev) => ev.stopPropagation());
        const row = {
          class: 'dn-inflight-row',
          cells: [
            { class: 'dn-mono', text: String(eid) },
            { class: 'dn-mono dn-faint', text: r.run_id ? String(r.run_id) : 'pending' },
            { el: [
              el('span', { class: 'dn-progress' }, [
                el('span', { class: 'dn-progress-fill', style: 'width:' + (pct != null ? pct : 6) + '%' + (pct == null ? ';opacity:0.4' : '') }),
              ]),
              el('span', { class: 'dn-mono dn-faint dn-progress-pct', text: pct != null ? ' ' + pct + '%' : ' running…' }),
            ] },
            { el: exec || el('span', { class: 'dn-faint', text: '—' }) },
          ],
        };
        if (eid !== '—') { row.style = 'cursor: pointer'; row.onClick = () => ctx.navigate('board', { epochId, entry: eid, gen: genId }); }
        return row;
      }),
    });
    liveCard.appendChild(tbl);
    host.appendChild(section('Live · boards running for this candidate', liveCard));
  }

  // ══ THE DOSSIER BODY — coordinated, NOT sprawling (study opt 2 layout) ══
  // The study folds the per-board read, the promote-gate ladder and the LABELED
  // radar silhouette into ONE coordinated grid beneath the full-width lifecycle
  // spine, rather than a flat stack of full-bleed sections (the sprawl the
  // operator flagged). Left column = the per-board comparison + the gate ladder;
  // right column = the silhouette, STRETCHED to fill its column (no empty band).
  // In the compare split the panes are already narrow, so the grid collapses to
  // one column (dn-dossier-grid is single-column there) and the figures stack.

  // (LEFT) ── per-board champion○ → candidate● DUMBBELL (study opt 2) ──
  // The study's per-board figure is an explicit per-row DUMBBELL: on each board
  // the CHAMPION's loss ON THAT BOARD (hollow ○) and THIS candidate's loss
  // (filled ●) ride a shared per-row value axis, joined by a connector coloured
  // improved (candidate left of champion) / regressed (right); the Δ (cand −
  // champ) + the pass/fail/timeout marker sit at the right edge, worst-first.
  // This is the paired champ→cand read — NOT the old single-series dot-plot
  // against one aggregate champion reference rule. Each per-entry record carries
  // the tournament context it ran in (match_id / rung); the SAME board can appear
  // several times (raced across rungs / rounds), so we keep the short dim context
  // tag per row + route a click to that SPECIFIC run's board drill-down.
  const scoreCard = el('div', { class: 'dn-panel' });
  // a cached champion's per-board results are reused from a prior epoch/run —
  // tag the section so it reads "cached · from <source_epoch>", never "no
  // board entries scored".
  if (s.cached) {
    const src = s.cachedProvenance && s.cachedProvenance.sourceEpoch;
    scoreCard.appendChild(el('div', { class: 'dn-cached-badge dn-faint' }, [
      el('span', { class: 'dn-cached-badge-mark', text: 'cached' }),
      el('span', { text: src ? ' · from ' + src : ' · champion results reused' }),
    ]));
  }
  if (s.entries.length) {
    // the per-board champion value comes from s.championLoss (the champion's
    // per-board drift_loss on the SAME slice, matched by entry_id; absent for the
    // seed / when the candidate IS the champion → a candidate-only row, no ○).
    const champByEntry = s.championLoss || {};
    const rows = s.entries
      .filter((e) => svg.isNum(e.drift_loss))
      .sort((a, b) => b.drift_loss - a.drift_loss)
      .map((e) => ({
        label: e.entry_id, value: e.drift_loss, id: e.entry_id,
        champ: svg.isNum(champByEntry[e.entry_id]) ? champByEntry[e.entry_id] : null,
        pass: e.pass_fail, timeout: !!e.wall_clock_budget_exceeded,
        // continuous per-entry outcome + its precision/recall decomposition (#18);
        // null/absent on a bool-only entry, where the row falls back to ✓/✗.
        score: svg.isNum(e.score) ? e.score : null, metrics: e.metrics || null,
        context: tournamentContext(e),
        entry_id: e.entry_id, run_id: e.run_id || null, gen: genId,
      }));
    // RESPONSIVE: the dumbbell fills the width of its dossier column. The
    // candidate-vs-champion comparison rides per-row (each board's own ○ → ●),
    // so the de-emphasised aggregate champion tick is just a faint reference at
    // the foot — NOT the per-row comparator (that's the dumbbell).
    scoreCard.appendChild(perBoardDumbbell({
      width: narrow ? 480 : 720, rowHeight: dt.dotRow, labelWidth: narrow ? 160 : 200, rows,
      championId,
      aggregate: svg.isNum(championScalar) ? { value: championScalar, label: `champion ${championId}` } : null,
      // click a row (board name, either dot, AND the Δ) → the board drill-down for
      // THIS exact run: the board view opens its inline transcript for the gen.
      onClick: (it) => ctx.navigate('board', { epochId, entry: it.entry_id || it.id, gen: it.gen || genId }),
    }));
    const anyPaired = rows.some((r) => svg.isNum(r.champ));
    scoreCard.appendChild(el('div', { class: 'dn-legend' }, [
      anyPaired ? el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-ink-faint);' }), `champion ${championId} ○`]) : null,
      el('span', null, [el('i', { class: 'dotact', style: 'background:var(--v2-good);' }), 'candidate ● · improved']),
      el('span', null, [el('i', { class: 'dotact', style: 'background:var(--v2-bad);' }), 'candidate ● · regressed']),
      svg.isNum(championScalar) ? el('span', null, [el('i', { class: 'spine', style: 'border-color:var(--v2-ink-faint);border-top-style:dashed;' }), `champ aggregate ${svg.fmt(championScalar, 1)}`]) : null,
      el('span', { class: 'dn-faint', text: '⏱ timeout · Δ = candidate − champion · dim tag = rung/round it ran in · click → drill-down' }),
    ].filter(Boolean)));
    // per-generation MEAN continuous outcome (#18) — a board-level score
    // summary beneath the per-board rows; higher is better. Absent on the
    // pre-score path, so the caption simply does not render.
    if (svg.isNum(s.meanScore)) {
      scoreCard.appendChild(el('div', { class: 'dn-faint dn-meanscore' }, [
        el('span', { text: 'mean score ' }),
        el('span', { class: 'dn-meanscore-val', text: svg.fmt(s.meanScore, 2) }),
        el('span', { text: ' · per-entry continuous outcome, higher is better' }),
      ]));
    }
    if (s.facetScores.rows.length) scoreCard.appendChild(facetTable(s.facetScores));
  } else if (s.radar && s.radar.projectedOnly) {
    // RACING / IN-FLIGHT with no SETTLED per-board rows yet: don't read "no
    // entries" — surface the racing affordance so the column isn't bare.
    scoreCard.appendChild(racingAffordance());
  } else {
    scoreCard.appendChild(empty('No per-entry scores for this candidate (the index may not be built).'));
  }
  const scoreSection = section('Per-board scoring · champion ○ → candidate ● · sorted', scoreCard);

  // (LEFT) ── the STACKED promote gate(s) (fix #1) — moved INTO the dossier grid
  // so the deciding rules read beside the per-board evidence + the silhouette.
  const gateSections = [];
  if (s.gates.some((g) => g && Array.isArray(g.rules))) {
    s.gateSpecs.forEach((k, i) => {
      const g = s.gates[i];
      if (!g || !Array.isArray(g.rules)) return;
      gateSections.push(section(`Promote gate · ${k.champ} → ${k.chall} (${k.role})`,
        gatePanel(g, (s.judgeComparisons || [])[i], k)));
    });
  } else if (!baseline) {
    gateSections.push(section('Promote gate', el('div', { class: 'dn-panel' }, [empty('No gate decomposition recorded for this candidate’s round.')])));
  } else {
    gateSections.push(section('Promote gate', el('div', { class: 'dn-panel' }, [empty('The seed candidate has no gate — it defines the loss floor that challengers must beat.')])));
  }

  // ── the PROPOSER PREDICTION-ACCURACY + CALIBRATION scorecard (DIAGNOSTIC) ──
  // Beneath the gate ladder in the main column: the orthogonal read of whether
  // the PROPOSER predicted what would happen (predicted vs realised movements +
  // the calibration fraction). It NEVER couples to the gate — its own caption
  // says so. Absent / no claims → null → the dossier is byte-identical to today.
  const scorecardPanel = buildPredictionScorecard(s.scorecard);
  if (scorecardPanel) {
    gateSections.push(section('Prediction accuracy · did the proposer call it? (diagnostic)', scorecardPanel));
  }

  // (RIGHT) ── RADAR SILHOUETTE (the FINAL liked study opt 2's folded-in panel) ──
  // The candidate's SHAPE vs the champion across the heterogeneous axes the gate
  // weighs (scalar-inverse, pass-rate, per-judge drift); OUTER = better, axes
  // LABELED (the operator's "missing labels / 1–9" fix — we pass MEANINGFUL
  // `axes[].label`). A live/projected candidate GHOSTS in dn-proj; a racing
  // candidate with too few settled axes shows a clearly-marked PROJECTED radar +
  // the "settled comparisons appear once boards finish" affordance instead of an
  // empty pane. This REPLACES the old scalar-component bars (folded in). Vendor-
  // clean. For racing the silhouette compares against the field-leader reference.
  let radarSection = null;
  if (s.radar && Array.isArray(s.radar.axes)) {
    const racing = String(structure) === 'racing';
    const plottable = s.radar.axes.length >= 3;
    const radarCard = el('div', { class: 'dn-panel dn-figpane dn-radarpane' });
    if (s.radar.projectedOnly && !plottable) {
      // projected/in-flight with < 3 axes — too few to draw a silhouette, but we
      // do NOT drop the panel: a clearly-marked projected placeholder + the
      // racing affordance keep the dossier coherent while boards stream.
      radarCard.appendChild(el('div', { class: 'dn-radar-projhint dt-proj' }, [
        el('span', { class: 'dt-proj-badge', text: 'projected' }),
        el('span', { class: 'dn-faint', text: ' silhouette forms as axes land · ' + s.radar.axes.length + ' of ≥3 so far' }),
      ]));
      radarCard.appendChild(racingAffordance());
    } else {
      // plottable: draw the silhouette (ghosted via live:true when projected).
      radarCard.appendChild(svg.radarSilhouette({
        axes: s.radar.axes, raw: s.radar.raw, live: s.radar.live,
        // pass the shared-contract responsive flag so the silhouette fills its
        // (full-width) dossier column rather than sitting narrow with empty space.
        responsive: true,
        // compact in the compare split; the legend rides only the wide single view.
        mini: false, legend: !cmpId,
        onAxis: null,
      }));
      radarCard.appendChild(el('p', { class: 'dn-faint dn-radar-cap', text: (s.radar.live ? 'projected silhouette (boards still streaming) · ' : '') + (racing
        ? 'candidate shape vs the field-leader reference · outer = better · hover a vertex for its value'
        : 'candidate shape vs champion · outer = better · hover a vertex for its value') }));
      if (s.radar.live) radarCard.appendChild(racingAffordance());
    }
    radarSection = section(s.radar.live ? 'Silhouette · projected shape vs champion' : 'Silhouette · candidate shape vs champion', radarCard);
  }

  // ── arrange the dossier body: the coordinated grid (study opt 2). The LEFT
  // column carries the per-board evidence + the gate ladder; the RIGHT column the
  // silhouette. In the compare split (`narrow`) the grid is single-column so each
  // half-width pane stacks its figures cleanly.
  const dossierGrid = el('div', { class: 'dn-dossier-grid' + (narrow ? ' dn-dossier-grid--narrow' : '') }, [
    el('div', { class: 'dn-dossier-col dn-dossier-col--main' }, [scoreSection, ...gateSections]),
    radarSection ? el('div', { class: 'dn-dossier-col dn-dossier-col--side' }, [radarSection]) : null,
  ].filter(Boolean));
  host.appendChild(dossierGrid);

  // ---- RACING VARIATION — racing is FIELD-relative, not pairwise ----
  // Racing cuts the whole field rung-by-rung (successive-halving), so "how good"
  // only means anything against the field. When THIS candidate's structure is
  // racing the study swaps the pairwise read for field-relative panels (field
  // standings · rung ladder), while the shared spine, gate ladder, radar, and
  // generalization stay put. Built from the REAL racing reconstruction + the
  // live projected standings — no synthesised field.
  if (String(structure) === 'racing') {
    const fieldNodes = racingFieldPanels(racingSt, epochId, genId, championId, s.scalarByGen, liveProjected, ctx, cmpId);
    if (fieldNodes) host.appendChild(fieldNodes);
  }

  if (isPrimary && s.entryParam) host.appendChild(entryDrilldown(ctx, epochId, genId, s.entryParam, s.drillRow, s.exps, s.judges, s.drillHeader));

  // ---- fix #3: ALL match-ups for this candidate ----
  // (the STACKED promote gate(s), fix #1, now ride INSIDE the dossier grid above
  // — beside the per-board evidence + the silhouette — per the study's layout.)
  host.appendChild(section('Match-ups · every round this candidate was in', allMatchupsPanel(s.mine, genId, championId, ctx, epochId)));

  // ---- GENERALIZATION · train → holdout (SHRUNK supporting panel) ----
  // The study reduced this from a hero figure to a small, width-capped slope.
  // Rendered only when this candidate's experiment carried the train/holdout
  // triplet (issue #5); absent otherwise (never a crash). The host caps its
  // width via dn-genpane so `width:100%` cannot balloon it to full width.
  if (s.generalization) {
    host.appendChild(section('Generalization · train → holdout', generalizationPanel(s.generalization)));
  }
}

// The train→holdout generalization slope as a SMALL, width-capped supporting
// panel (the study's facetGeneralize, shrunk). A 2-point slope train→holdout
// with the gap called out; caution when within tolerance, bad when it exceeds.
// viewBox kept small (240×104); the dn-genpane host caps the max width.
export function generalizationPanel(g) {
  const card = el('div', { class: 'dn-panel dn-figpane dn-genpane' });
  const train = svg.isNum(g.train) ? g.train : null;
  const holdout = svg.isNum(g.holdout) ? g.holdout : null;
  const gap = svg.isNum(g.gap) ? g.gap : (train != null && holdout != null ? holdout - train : null);
  const tol = svg.isNum(g.tolerance) ? g.tolerance : null;
  const within = (gap != null && tol != null) ? gap <= tol : null;
  const tone = within === false ? 'dn-bad' : 'dn-caution';

  // when we have the full pair, draw the slope; else a compact gap callout.
  if (train != null && holdout != null) {
    const W = 240, H = 104, xT = 78, xH = 168, top = 40, bot = 84;
    const lo = Math.min(train, holdout), hi = Math.max(train, holdout);
    const padv = (hi - lo) * 0.4 || Math.max(0.01, Math.abs(hi) * 0.1) || 0.02;
    const dlo = lo - padv, dhi = hi + padv, span = (dhi - dlo) || 1;
    const Y = (v) => bot - ((v - dlo) / span) * (bot - top);
    const fig = svgEl('svg', {
      class: 'dn-gen-svg', width: '100%', height: H, viewBox: `0 0 ${W} ${H}`,
      preserveAspectRatio: 'xMidYMid meet', role: 'img',
      'aria-label': 'train to holdout generalization slope',
    });
    const txt = (x, y, cls, anchor, t) => { const n = svgEl('text', { x, y, class: cls, 'text-anchor': anchor || 'start' }); n.textContent = t; return n; };
    fig.appendChild(txt(10, 12, 'dn-gen-title', 'start', 'train → holdout'));
    const gapLab = svgEl('text', { x: 10, y: 27, class: 'dn-gen-gap ' + tone });
    gapLab.textContent = `gap ${svg.fmtSigned(gap, 3)}` + (tol != null ? (within ? ` · ≤ tol ${svg.fmt(tol, 2)} · OK` : ` · > tol ${svg.fmt(tol, 2)}`) : '');
    fig.appendChild(gapLab);
    fig.appendChild(txt(xT, bot + 14, 'dn-gen-axlab', 'middle', 'train'));
    fig.appendChild(txt(xH, bot + 14, 'dn-gen-axlab', 'middle', 'holdout'));
    fig.appendChild(svgEl('line', { x1: xT, y1: Y(train), x2: xH, y2: Y(holdout), class: 'dn-gen-slope ' + tone }));
    fig.appendChild(svgEl('circle', { cx: xT, cy: Y(train), r: 4, class: 'dn-gen-train' }));
    fig.appendChild(svgEl('circle', { cx: xH, cy: Y(holdout), r: 4, class: 'dn-gen-holdout ' + tone }));
    fig.appendChild(txt(xT - 10, Y(train) + 3.5, 'dn-gen-val dn-gen-train-t', 'end', svg.fmt(train, 3)));
    // RIGHT-anchor the holdout value at the box's right margin (W-4) so a long
    // 3-dp value (e.g. "-123.456") grows LEFTWARD toward — but never past — the
    // edge instead of clipping the W=240 viewBox; short values still sit just
    // right of the holdout dot (mirrors the train label's leftward grow). Routed
    // through the shared svg.edgeText primitive (clamp x + flip near an edge).
    fig.appendChild(svg.edgeText({ text: svg.fmt(holdout, 3), x: W - 4, y: Y(holdout) + 3.5, anchor: 'end', viewW: W, pad: 4, cls: 'dn-gen-val ' + tone }));
    card.appendChild(fig);
  } else if (gap != null) {
    card.appendChild(el('div', { class: 'dn-gen-gaponly ' + tone, text: `generalization gap ${svg.fmtSigned(gap, 3)}` + (tol != null ? ` (tol ${svg.fmt(tol, 2)})` : '') }));
  }
  card.appendChild(el('p', { class: 'dn-faint dn-gen-cap', text: within === false
    ? 'holdout gap exceeds tolerance — possible memorization'
    : 'small gap — generalizes (no memorization)' }));
  return card;
}

// Normalize the dossier feed's `facet_scores` block for render.
// Reads `{facets: {name: {scalar, mean_score, scored_count, entry_count}},
// overall: {...}}` defensively — a missing/torn block yields `[]` rows and the
// table does not paint. Sorted by name so row order is stable across repaints
// (G10).
function facetRows(raw) {
  const block = (raw && typeof raw === 'object') ? raw : {};
  const facets = (block.facets && typeof block.facets === 'object') ? block.facets : {};
  const norm = (row, name) => ({
    name,
    scalar: svg.isNum(row.scalar) ? row.scalar : null,
    mean: svg.isNum(row.mean_score) ? row.mean_score : null,
    scored: Number.isInteger(row.scored_count) ? row.scored_count : 0,
    // The SCALAR's denominator — how many tagged entries produced a run.
    // Distinct from `scored` (the mean's denominator) and from `total` (what
    // the board tagged), so all three ride to the cell.
    ran: Number.isInteger(row.ran_count) ? row.ran_count : null,
    total: Number.isInteger(row.entry_count) ? row.entry_count : 0,
  });
  const rows = Object.keys(facets).sort().map((n) => norm(facets[n] || {}, n));
  const overall = (block.overall && typeof block.overall === 'object')
    ? norm(block.overall, 'candidate overall') : null;
  return { rows, overall };
}

// The FACET table — one row per `facet:` board tag this candidate carries,
// plus the candidate's OWN aggregate as the last row to read against.
//
// Both numbers are the candidate's own quantities recomputed over the slice at
// the epoch's frozen weights, so a facet row is directly comparable to the
// overall row beneath it — that comparison is the point of the table.
//   * `scalar`     — the SAME loss the gate's number is (lower is better):
//                    drift (every judge's weighted contribution included),
//                    the outcome miss, and the namespace terms.
//   * `mean score` — the outcome axis (higher is better).
// The two run OPPOSITE directions because one counts problems and the other
// counts quality, so the header states each direction rather than relying on
// the reader to know.
//
// Deliberately plain: no verdict colour, no bars, no ordering by value. These
// numbers carry NO noise threshold (BOARD-FORMAT.md §1.4) and a thin slice is
// mostly noise, so the table must not read as a scoreboard. `scored` is shown
// for the same reason — a scalar over one entry must not read like a scalar
// over twenty.
//
// A facet nobody scored shows an em dash for `mean score` rather than 0.00 —
// an absent measurement is not a failing one. Its `scalar` is still real: an
// unscored entry still contributes drift.
function facetTable(model) {
  const cellsFor = (f) => [
    { text: f.name },
    { text: facets.facetNum(f.scalar), class: 'dn-num' },
    { text: facets.facetNum(f.mean), class: 'dn-num' },
    { text: facets.facetCount(f.scored, f.ran, f.total), class: 'dn-num dn-faint' },
  ];
  const body = (model.rows || []).map(cellsFor);
  if (model.overall) {
    body.push({ class: 'dn-facet-overall', cells: cellsFor(model.overall) });
  }
  const table = dataTable({
    class: 'dn-facet-table',
    columns: [
      { label: 'facet' },
      { label: facets.SCALAR_LABEL, class: 'dn-num' },
      { label: facets.MEAN_SCORE_LABEL, class: 'dn-num' },
      { label: 'scored', class: 'dn-num' },
    ],
    rows: body,
  });
  const heads = facets.tableHeaderCells(table);
  facets.attachFacetHover(heads[1], 'scalar');
  facets.attachFacetHover(heads[2], 'mean_score');
  // The count column carries THREE denominators collapsed into one string,
  // so it needs its own explanation as much as the two numbers do.
  facets.attachFacetHover(heads[3], 'count');
  return el('div', { class: 'dn-facets' }, [
    facets.facetCaption('this candidate re-scored per board tag'),
    table,
  ]);
}

// A small, clearly-marked "racing — settled comparisons appear once boards
// finish" affordance. Shown when a candidate is still racing (only a projected
// scalar / partial board slice), so the dossier surfaces WHY the dumbbell / gate
// comparisons are not yet drawn rather than reading bare. Vendor-clean.
function racingAffordance() {
  return el('div', { class: 'dn-racing-affordance dt-proj' }, [
    el('span', { class: 'dn-inflight-pulse', 'aria-hidden': 'true' }),
    el('span', { class: 'dn-racing-affordance-lab' }, [
      el('span', { class: 'dt-proj-badge', text: 'racing' }),
      el('span', { class: 'dn-faint', text: ' settled comparisons (per-board dumbbell · gate ladder) appear once boards finish' }),
    ]),
  ]);
}

// ── the per-board champion○ → candidate● DUMBBELL (study opt 2, inline) ──
// An explicit per-row dumbbell, faithful to the study's dossierDotPlot: each
// board row carries, on a SHARED per-row value axis spanning every board's
// champion + candidate value, the champion's loss ON THAT BOARD as a hollow ○
// and this candidate's as a filled ●, joined by a connector coloured improved
// (candidate left of champion) / regressed (right of champion); the Δ (cand −
// champ) + the pass/fail/timeout marker ride the right edge; rows are passed
// pre-sorted (worst-first). A de-emphasised dashed champion AGGREGATE tick sits
// at the foot — context, NOT the per-row comparator. Rendered INLINE with
// svgEl(...) — NO builder is added to / modified in svg.js. Each row is a
// clickable <g> (board name, either dot, the connector, the Δ) → onClick(row).
// The shared-contract responsive flag (width:100% + viewBox) keeps it filling
// its dossier column. A board with NO champion value (seed / cand IS champion)
// renders candidate-only (just the ●), so the figure never crashes.
function perBoardDumbbell(opts) {
  const o = opts || {};
  const rows = (Array.isArray(o.rows) ? o.rows : []).filter((r) => r && svg.isNum(r.value));
  const w = svg.isNum(o.width) ? o.width : 720;
  const rh = svg.isNum(o.rowHeight) ? o.rowHeight : 22;
  const labelW = svg.isNum(o.labelWidth) ? o.labelWidth : 200;
  const top = 18;          // headroom above the first row
  const glyphW = 16;       // right-edge pass/fail/timeout glyph gutter
  const deltaW = 54;       // the Δ value column, just left of the glyph — wide
                           // enough for a two-digit, signed, 2dp Δ ("−48.00")
                           // so it never collides with the score readout.
  // CONTINUOUS-SCORE column (#18): a 0→1 mini-bar + score readout, only
  // reserved when AT LEAST ONE row carries a score; a wholly bool-only
  // dumbbell keeps the pre-score geometry (zero-width score column) so its
  // layout is byte-identical to today.
  const anyScored = rows.some((r) => r && svg.isNum(r.score));
  const scoreW = anyScored ? 92 : 0;
  const footH = 16;        // the faint aggregate-tick caption band at the foot
  const h = Math.max(rh + top + footH, top + rows.length * rh + footH);
  const svgNode = svgEl('svg', {
    class: 'dn-dumbbell', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMin meet', role: 'img',
    'aria-label': 'per-board champion to candidate dumbbell',
  });
  if (!rows.length) {
    const t = svgEl('text', { x: 4, y: 16, class: 'dn-empty-label' });
    t.textContent = 'no scored entries';
    svgNode.appendChild(t);
    return svgNode;
  }
  const x0 = labelW + 6;
  const x1 = w - glyphW - deltaW - scoreW;
  // a shared per-row value axis spanning BOTH champion + candidate across all
  // boards, so every ○/● sits on one comparable scale (the study's `X`).
  const vals = [];
  for (const r of rows) { vals.push(r.value); if (svg.isNum(r.champ)) vals.push(r.champ); }
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (lo === hi) hi += 1;
  const pad = (hi - lo) * 0.08;
  lo -= pad; hi += pad;
  const X = (v) => x0 + ((v - lo) / (hi - lo)) * (x1 - x0);

  rows.forEach((r, i) => {
    const cy = top + i * rh + rh / 2;
    const paired = svg.isNum(r.champ);
    const dx = X(r.value);
    const cx = paired ? X(r.champ) : null;
    // good when the candidate is BETTER (lower loss) than the champion on THIS
    // board; bad when worse; neutral when unpaired (seed) or exactly equal.
    const better = paired ? (r.value < r.champ) : null;
    const worse = paired ? (r.value > r.champ) : null;
    const dirCls = better ? 'dn-good' : worse ? 'dn-bad' : 'dn-flat';
    const g = svgEl('g', { class: 'dn-dumbbell-row', tabindex: o.onClick ? '0' : null });

    // board name + the dim rung/round context tag, two stacked right-anchored
    // lines in the label gutter (matches the dot-plot's dim-tag treatment).
    const hasCtx = r.context != null && String(r.context) !== '';
    const lbl = svgEl('text', { x: labelW, y: hasCtx ? cy - 2 : cy + 3, class: 'dn-dumbbell-label', 'text-anchor': 'end' });
    lbl.textContent = shortText(r.label, 22);
    g.appendChild(lbl);
    if (hasCtx) {
      const ctxt = svgEl('text', { x: labelW, y: cy + 9, class: 'dn-dumbbell-ctx', 'text-anchor': 'end' });
      ctxt.textContent = shortText(String(r.context), 22);
      g.appendChild(ctxt);
    }

    // faint full-row baseline, then the champ→candidate connector on top.
    g.appendChild(svgEl('line', { x1: x0, y1: cy, x2: x1, y2: cy, class: 'dn-dumbbell-base' }));
    if (paired) {
      g.appendChild(svgEl('line', { x1: cx, y1: cy, x2: dx, y2: cy, class: 'dn-dumbbell-conn ' + dirCls }));
      // champion marker — HOLLOW ○ (panel fill + faint stroke) on THIS board.
      const champDot = svgEl('circle', { cx, cy, r: 3.4, class: 'dn-dumbbell-champ' });
      attachHovercard(champDot, `${r.label}: champion ${svg.fmt(r.champ, 2)} (on this board)`);
      g.appendChild(champDot);
    }
    // candidate marker — FILLED ● coloured by the per-board verdict.
    const candDot = svgEl('circle', { cx: dx, cy, r: 4, class: 'dn-dumbbell-cand ' + dirCls });
    const prTip = prText(r.metrics);
    const scoreTip = svg.isNum(r.score) ? ` · score ${svg.fmt(r.score, 2)}${prTip ? ' · ' + prTip : ''}` : '';
    attachHovercard(candDot, (paired
      ? `${r.label}: candidate ${svg.fmt(r.value, 2)} vs champ ${svg.fmt(r.champ, 2)} (Δ ${svg.fmtSigned(r.value - r.champ, 2)})`
      : `${r.label}: candidate ${svg.fmt(r.value, 2)}`) + scoreTip);
    g.appendChild(candDot);

    // CONTINUOUS SCORE (#18): a 0→1 mini-bar + the score number, then the
    // compact precision/recall tag below it when the entry carries metrics.
    // Only drawn for a scored row — a bool-only row leaves this column empty
    // and relies on the ✓/✗ glyph at the edge exactly as before.
    if (scoreW > 0 && svg.isNum(r.score)) {
      const sbx = x1 + 6;                 // bar left, just past the value axis
      const sbw = 44;                     // FIXED bar width — the readout sits to
                                          // its right, both INSIDE the score column
      const sf = Math.max(0, Math.min(1, r.score));
      const barY = cy - 3;
      g.appendChild(svgEl('rect', { x: sbx, y: barY, width: sbw, height: 6, rx: 2, class: 'dn-score-track' }));
      g.appendChild(svgEl('rect', { x: sbx, y: barY, width: Math.max(1, sbw * sf), height: 6, rx: 2, class: 'dn-score-fill ' + dirCls }));
      // the readout is RIGHT-anchored at the score column's right edge so it
      // stays WITHIN the column instead of spilling into the Δ column to its
      // right (that overlap rendered the colliding "1.0048.00").
      const sv = svgEl('text', { x: x1 + scoreW - 4, y: cy + 3, class: 'dn-score-val', 'text-anchor': 'end' });
      sv.textContent = svg.fmt(r.score, 2);
      g.appendChild(sv);
      const pr = prText(r.metrics);
      if (pr) {
        const prt = svgEl('text', { x: sbx, y: cy + 11, class: 'dn-score-pr', 'text-anchor': 'start' });
        prt.textContent = pr;
        g.appendChild(prt);
      }
    }

    // per-board Δ (candidate − champion) just left of the glyph gutter.
    if (paired) {
      const dt = svgEl('text', { x: w - glyphW - 2, y: cy + 3, class: 'dn-dumbbell-delta ' + dirCls, 'text-anchor': 'end' });
      dt.textContent = svg.fmtSigned(r.value - r.champ, 2);
      g.appendChild(dt);
    }
    // the pass/fail/timeout marker at the right edge.
    const gx = w - glyphW + 6;
    if (r.timeout) {
      const tm = svgEl('text', { x: gx, y: cy + 3, class: 'dn-dumbbell-timeout', 'text-anchor': 'middle' });
      tm.textContent = '⏱';
      g.appendChild(tm);
    } else if (r.pass === 1 || r.pass === true) {
      g.appendChild(svgEl('circle', { cx: gx, cy, r: 2.6, class: 'dn-dumbbell-pass' }));
    } else if (r.pass === 0 || r.pass === false) {
      g.appendChild(svgEl('line', { x1: gx - 2.6, y1: cy - 2.6, x2: gx + 2.6, y2: cy + 2.6, class: 'dn-dumbbell-fail' }));
      g.appendChild(svgEl('line', { x1: gx - 2.6, y1: cy + 2.6, x2: gx + 2.6, y2: cy - 2.6, class: 'dn-dumbbell-fail' }));
    }

    if (o.onClick) {
      g.addEventListener('click', () => o.onClick(r));
      g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onClick(r); } });
    }
    svgNode.appendChild(g);
  });

  // de-emphasised aggregate champion tick (dashed) at the foot — context, NOT
  // the per-row comparator (the dumbbell is the comparator). Only when in range.
  if (o.aggregate && svg.isNum(o.aggregate.value) && o.aggregate.value >= lo && o.aggregate.value <= hi) {
    const ax = X(o.aggregate.value);
    const ayb = h - 4;
    svgNode.appendChild(svgEl('line', { x1: ax, y1: top - 6, x2: ax, y2: ayb - 9, class: 'dn-dumbbell-aggtick' }));
    const at = svgEl('text', { x: ax, y: ayb, class: 'dn-dumbbell-aggcap', 'text-anchor': 'middle' });
    at.textContent = (o.aggregate.label || 'champ aggregate') + ' ' + svg.fmt(o.aggregate.value, 1);
    svgNode.appendChild(at);
  }
  return svgNode;
}

// short-truncate a label to N chars with an ellipsis (local to candidate.js so
// it does not reach into svg.js's private shortLabel).
function shortText(s, n) {
  const max = svg.isNum(n) ? n : 22;
  const str = s == null ? '' : String(s);
  return str.length > max ? str.slice(0, max - 1) + '…' : str;
}

// The RACING field-relative panels — field standings (every racer ranked by
// scalar, candidate highlighted, survivors vs cut) + the rung ladder (entered /
// cut / survived per rung, candidate's rank among survivors). Built from the
// SHARED-resolver `st` (live-first → reconstructRacing → recorded, resolved once
// at render top) + the live projected standings — the SAME model the Match-ups /
// epoch / per-round views read, so the dossier never drifts from them (the old
// path called reconstructRacing here directly, missing the live envelope mid-
// race). Returns null when no racing field can be recovered.
function racingFieldPanels(st, epochId, genId, championId, scalarByGen, liveProjected, ctx, cmpId) {
  const model = st ? racingModel(st) : null;
  if (!model || !Array.isArray(model.rungs) || !model.rungs.length) return null;
  const opts = cmpId ? { cmp: cmpId } : undefined;

  // assemble the FULL field across rungs (every distinct racer), tagging each
  // with whether it ultimately survived to the gate / champion crown and the
  // rung it was cut at. Scalar from the settled trajectory, else the live
  // projected standing.
  const proj = (liveProjected && typeof liveProjected === 'object') ? liveProjected : {};
  const scalarOf = (id) => {
    if (scalarByGen && scalarByGen.has(String(id)) && svg.isNum(scalarByGen.get(String(id)))) return scalarByGen.get(String(id));
    const pr = proj[String(id)];
    return pr && svg.isNum(pr.scalar) ? pr.scalar : null;
  };
  const racers = new Map(); // id -> { id, scalar, survived, cut_rung }
  model.rungs.forEach((r, ri) => {
    const survivors = new Set((r.survivors || []).map(String));
    const cut = new Set((r.cut || []).map(String));
    for (const c of (r.competitors || []).map(String)) {
      if (!racers.has(c)) racers.set(c, { id: c, scalar: scalarOf(c), survived: true, cut_rung: null });
      // a racer cut at this rung is no longer surviving.
      if (cut.has(c)) { racers.get(c).survived = false; racers.get(c).cut_rung = ri; }
      else if (survivors.has(c)) { /* carries on */ }
    }
  });
  const list = [...racers.values()].filter((f) => f.id);
  if (!list.length) return null;
  // rank by scalar (lower = better); racers with no scalar sink to the bottom.
  list.sort((a, b) => {
    const av = svg.isNum(a.scalar) ? a.scalar : Infinity;
    const bv = svg.isNum(b.scalar) ? b.scalar : Infinity;
    return av - bv;
  });
  const fieldSize = list.length;
  const candRank = list.findIndex((f) => String(f.id) === String(genId)) + 1;
  const survivorCount = list.filter((f) => f.survived).length;

  const wrap = el('div', { class: 'dn-racing-field' });
  wrap.appendChild(el('div', { class: 'dn-racing-field-cap dn-faint', text:
    'racing variation · FIELD-relative (not pairwise) — the candidate is one racer cut rung-by-rung against the whole field' }));

  // (1) FIELD STANDINGS — the racing analogue of the pairwise scalar bullet.
  const standCard = el('div', { class: 'dn-panel' });
  standCard.appendChild(el('div', { class: 'dn-racing-standhead' }, [
    el('span', { class: 'dn-pill dn-promoted', text: candRank > 0 ? `rank ${candRank} / ${fieldSize}` : `field of ${fieldSize}` }),
    el('span', { class: 'dn-faint', text: ` · ${survivorCount} of ${fieldSize} survived the cuts` }),
  ]));
  const stbl = dataTable({
    class: 'dn-board-table dn-racing-standings',
    columns: [{ label: '#' }, { label: 'racer' }, { label: 'scalar', class: 'dn-num' }, { label: 'status' }],
    rows: list.map((f, i) => {
      const isCand = String(f.id) === String(genId);
      const isChamp = String(f.id) === String(championId);
      const row = {
        class: isCand ? 'dn-racing-cand-row' : '',
        cells: [
          { class: 'dn-mono dn-faint', text: String(i + 1) },
          { el: el('span', { class: 'dn-mono' + (isCand ? ' dn-racing-cand' : ''), text: f.id + (isChamp ? ' ♛' : '') }) },
          { class: 'dn-num dn-mono', text: svg.isNum(f.scalar) ? svg.fmt(f.scalar, 1) : '—' },
          { el: pill(f.survived ? 'promoted' : 'rejected', f.survived ? 'racing' : ('✂ rung ' + f.cut_rung)) },
        ],
      };
      if (!isCand) { row.style = 'cursor: pointer'; row.onClick = () => ctx.navigate('candidate', { epochId, gen: f.id }, opts); }
      return row;
    }),
  });
  standCard.appendChild(stbl);
  standCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'lower scalar = better · ✂ = the rung a racer was cut at · click a racer → its dossier' }));
  wrap.appendChild(section('Field standings · candidate vs the whole field', standCard));

  // (2) RUNG LADDER — entered / cut / survived per rung; the candidate's rank
  // among the survivors at each rung it reached (the field-narrowing story).
  const ladderCard = el('div', { class: 'dn-panel' });
  const ltbl = dataTable({
    class: 'dn-board-table dn-racing-ladder',
    columns: [{ label: 'rung' }, { label: 'entered', class: 'dn-num' }, { label: 'cut', class: 'dn-num' },
      { label: 'survived', class: 'dn-num' }, { label: 'candidate' }],
    rows: model.rungs.map((r, ri) => {
      const competitors = (r.competitors || []).map(String);
      const entered = competitors.length;
      const cutN = (r.cut || []).length;
      const survived = (r.survivors || []).length || (entered - cutN);
      const inThisRung = competitors.indexOf(String(genId)) >= 0;
      // the candidate's rank among the racers in this rung, by scalar.
      let candRungRank = null;
      if (inThisRung) {
        const ranked = competitors
          .map((c) => ({ id: c, scalar: scalarOf(c) }))
          .sort((a, b) => (svg.isNum(a.scalar) ? a.scalar : Infinity) - (svg.isNum(b.scalar) ? b.scalar : Infinity));
        candRungRank = ranked.findIndex((x) => String(x.id) === String(genId)) + 1;
      }
      return [
        { class: 'dn-mono', text: r.label || ('Rung ' + ri) },
        { class: 'dn-num dn-mono', text: String(entered) },
        { class: 'dn-num dn-mono ' + (cutN ? 'dn-bad-t' : ''), text: cutN ? ('✂ ' + cutN) : '0' },
        { class: 'dn-num dn-mono dn-good-t', text: String(survived) },
        { class: 'dn-mono', text: candRungRank ? ('#' + candRungRank + ' of ' + entered) : '—' },
      ];
    }),
  });
  ladderCard.appendChild(ltbl);
  ladderCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'how the candidate fared as the field narrowed rung by rung' }));
  wrap.appendChild(section('Rung ladder · how it fared as the field narrowed', ladderCard));

  return wrap;
}

// fix #3 — every matchup the candidate was in, both roles. Clicking a round
// COMPARES the two candidates side by side (S's affordance).
function allMatchupsPanel(mine, genId, championId, ctx, epochId) {
  const card = el('div', { class: 'dn-panel' });
  if (!mine.length) {
    card.appendChild(empty('This candidate did not run in any tournament round (it may be the seed and undefeated, or rounds are not yet recorded).'));
    return card;
  }
  const tbl = dataTable({
    class: 'dn-board-table',
    columns: [{ label: 'round' }, { label: 'role' }, { label: 'decision' },
      { label: 'Δ scalar', class: 'dn-num' }, { label: 'hypothesis' }],
    rows: mine.map((m) => {
      const asChamp = m.champion === genId;
      // Class B: a match-up with no recorded decision is still racing —
      // PENDING, not a default "rejected". `decisionOf` reads the matchup's own
      // stamped decision field; absent ⇒ pending.
      const dec = decisionOf(m) || 'pending';
      const other = asChamp ? m.challenger : m.champion;
      return {
        // clicking a match-up row compares the two candidates side by side (S).
        style: 'cursor: pointer',
        onClick: () => ctx.navigate('candidate', { epochId, gen: genId }, { cmp: other }),
        cells: [
          { el: el('span', { class: 'dn-mono', text: `${m.champion} → ${m.challenger}` }) },
          { el: pill(asChamp ? 'promoted' : 'rejected', asChamp ? 'champion' : 'challenger') },
          { el: pill(dec, dec) },
          deltaCell(m.delta_scalar, { base: 'dn-num dn-mono', text: svg.isNum(m.delta_scalar) ? svg.fmtSigned(m.delta_scalar, 2) : '—' }),
          { class: 'dn-faint', text: m.hypothesis_core_idea ? truncate(m.hypothesis_core_idea, 64) : '—' },
        ],
      };
    }),
  });
  card.appendChild(tbl);
  card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:10px 0 0;', text: genId === championId
    ? `as champion, ${genId} defended ${mine.length} round${mine.length === 1 ? '' : 's'} · click a round → compare the two candidates side by side`
    : `${mine.length} round${mine.length === 1 ? '' : 's'} · click a round → compare the two candidates side by side` }));
  return card;
}

// The ABSOLUTE-SCALAR endpoints the gate Δ is measured between, surfaced as a
// pair of `dn-stat` chips LEFT of the Δ chips in the gate head. The champion
// side is the SETTLED floor (`champion_scalar`); the challenger side is its
// absolute `challenger_scalar` when SETTLED, or — while boards are still
// streaming in for THIS pair — the LIVE PROJECTED scalar in the projStat
// treatment (proj badge + boards_done/total bar) so an in-flight endpoint is
// visibly NOT a settled one. Closes the "Δ without endpoints" gap with no new
// backend data: `champion_scalar`/`challenger_scalar` and `live` already ride on
// the gate object. Returns null when NEITHER side resolves (absent → the gate
// head renders byte-identical to today). All floats fmt'd 2dp.
export function absoluteScalars(gate) {
  if (!gate) return null;
  const champ = svg.isNum(gate.champion_scalar)
    ? stat(svg.fmt(gate.champion_scalar, 2), 'champion scalar') : null;
  // Prefer the LIVE projected challenger scalar (mid-flight) over the settled
  // absolute; `gate.live` is non-null ONLY while this pair is still streaming.
  const live = gate.live;
  let chall = null;
  if (live && svg.isNum(live.challenger_scalar)) {
    chall = projStat(svg.fmt(live.challenger_scalar, 2), 'challenger scalar', {
      boards_done: live.boards_done, boards_total: live.boards_total,
    });
  } else if (svg.isNum(gate.challenger_scalar)) {
    chall = stat(svg.fmt(gate.challenger_scalar, 2), 'challenger scalar');
  }
  if (!champ && !chall) return null;
  return el('div', { class: 'dn-row dn-gate-absolutes' }, [champ, chall].filter(Boolean));
}

// A content digest of the absolute-scalar endpoints: champion_scalar +
// challenger_scalar (rounded 2dp, NO timestamp) + the live projection
// (rounded scalar + INTEGER board counts). null when neither side resolves so
// it contributes NOTHING to the gate digest (back-compat: byte-identical to the
// pre-feature path). A no-op heartbeat re-emits the same numbers → equal digest
// → skipped; a board landing (boards_done grows) or a settle (champion/
// challenger scalar moving) flips it → repaint.
export function absoluteScalarsDigest(gate) {
  if (!gate) return null;
  const cs = svg.isNum(gate.champion_scalar) ? gate.champion_scalar.toFixed(2) : null;
  const ch = svg.isNum(gate.challenger_scalar) ? gate.challenger_scalar.toFixed(2) : null;
  const live = gate.live;
  const lv = live && svg.isNum(live.challenger_scalar) ? [
    live.challenger_scalar.toFixed(2),
    live.boards_done == null ? '?' : live.boards_done,
    live.boards_total == null ? '?' : live.boards_total,
  ] : null;
  if (cs == null && ch == null && lv == null) return null;
  return [cs, ch, lv];
}

// ── the BRADLEY–TERRY uncertainty PRE-GATE (the marquee) ────────────────────
//
// Before the deterministic rule ladder fires, a confidence-thresholded run
// resolves the winner by a Bradley–Terry strength estimate: each side carries a
// posterior strength θ̂ with a credible interval, and the gate promotes only when
// P(θ_child > θ_champion) clears the configured threshold. This block surfaces
// that pre-gate as the operator's FIRST read — two θ̂ whiskers + the P-bar against
// the threshold marker — so "why hasn't this promoted yet?" reads off the
// evidence, not a bare "deferred". Every field comes from `gate.rating`
// VERBATIM (build_rating_view): the keys are `present`/`credible`/`champion`/
// `challenger` (each `{theta, se, ci_lo, ci_hi}|null`)/`p_stronger`/`threshold`/
// `decision`/`ci_overlap`/`replicates_spent`/`n_duels`/`next_duel`/`ci_history`.
//
// Back-compat contract (NO Python edits): when the feature is OFF the block is
// EXACTLY `{present:false}` — render NOTHING (byte-identical to today). Below the
// credible-fit minimum (n_duels < MIN_CREDIBLE_DUELS) we render a "rating forms
// after N duels" placeholder, never a faked estimate. A `deferred` decision
// drives the replicationStrip (replicates-spent pips + the next closest-CI duel
// + a CI-convergence sparkline); a schedule-exhausted deferral (no next_duel,
// not credible) reads "inconclusive" — NEVER a faked crown.
const MIN_CREDIBLE_DUELS = 3;       // build_rating_view's credible-fit floor.

// One θ̂ whisker: the point estimate + its [ci_lo, ci_hi] credible interval drawn
// on a SHARED [lo,hi] domain so the champion and challenger whiskers are directly
// comparable. `side` is {theta, se, ci_lo, ci_hi}|null; null (unfit) → a faint
// "—" rail so the two rows still line up. Returns a row Node.
function ratingWhisker(label, side, dom, better) {
  const W = 220, H = 26, padX = 4, axW = W - 2 * padX;
  const X = (v) => padX + ((v - dom.lo) / (dom.span || 1)) * axW;
  // pin the box aspect (== the viewBox aspect) inline so the
  // `preserveAspectRatio:'none'` scale stays UNIFORM (no shear): without it the
  // flexible grid column stretches X past the fixed `height`, squashing the
  // round θ̂ <circle> + CI end-caps into horizontal ellipses. Mirrors the house
  // aspect-lock mechanism (svg.js applyResponsive) — no measurement, leak-free.
  const fig = svgEl('svg', {
    class: 'dn-bt-whisker', width: '100%', viewBox: `0 0 ${W} ${H}`,
    preserveAspectRatio: 'none', role: 'img', style: `aspect-ratio: ${W} / ${H};`,
    'aria-label': label + ' strength estimate',
  });
  const mid = H / 2;
  // the rail.
  fig.appendChild(svgEl('line', { x1: padX, y1: mid, x2: W - padX, y2: mid, class: 'dn-bt-rail' }));
  if (side && svg.isNum(side.theta)) {
    const hasCi = svg.isNum(side.ci_lo) && svg.isNum(side.ci_hi);
    if (hasCi) {
      const xlo = X(side.ci_lo), xhi = X(side.ci_hi);
      fig.appendChild(svgEl('line', { x1: xlo, y1: mid, x2: xhi, y2: mid, class: 'dn-bt-ci ' + better }));
      fig.appendChild(svgEl('line', { x1: xlo, y1: mid - 5, x2: xlo, y2: mid + 5, class: 'dn-bt-cap ' + better }));
      fig.appendChild(svgEl('line', { x1: xhi, y1: mid - 5, x2: xhi, y2: mid + 5, class: 'dn-bt-cap ' + better }));
    }
    fig.appendChild(svgEl('circle', { cx: X(side.theta), cy: mid, r: 3.5, class: 'dn-bt-theta ' + better }));
  } else {
    const t = svgEl('text', { x: W / 2, y: mid + 3.5, class: 'dn-bt-unfit', 'text-anchor': 'middle' });
    t.textContent = 'unfit';
    fig.appendChild(t);
  }
  const valTxt = (side && svg.isNum(side.theta))
    ? 'θ̂ ' + svg.fmt(side.theta, 2) + (svg.isNum(side.ci_lo) && svg.isNum(side.ci_hi)
      ? ' [' + svg.fmt(side.ci_lo, 2) + ', ' + svg.fmt(side.ci_hi, 2) + ']' : '')
    : 'not yet fit';
  const row = el('div', { class: 'dn-bt-row' }, [
    el('span', { class: 'dn-bt-rowlab', text: label }),
    el('span', { class: 'dn-bt-rowfig' }, [fig]),
    el('span', { class: 'dn-bt-rowval dn-mono dn-faint', text: valTxt }),
  ]);
  return row;
}

// The P(θ_child > θ_champion) bar against the threshold marker. `p` is the
// posterior probability the challenger is stronger; `thr` the configured
// promote_confidence_threshold. The fill earns its tone by DIRECTION (clears the
// threshold → good, below → caution while still resolving). `p` null (CIs not yet
// fit) → a faint "P forms once both intervals fit" rail. Returns a Node.
function ratingProbBar(p, thr) {
  const W = 260, H = 30, padX = 4, axW = W - 2 * padX;
  const fig = svgEl('svg', {
    class: 'dn-bt-prob', width: '100%', height: H, viewBox: `0 0 ${W} ${H}`,
    preserveAspectRatio: 'none', role: 'img',
    'aria-label': 'probability the challenger is stronger vs the promote threshold',
  });
  const top = 6, barH = 12;
  fig.appendChild(svgEl('rect', { x: padX, y: top, width: axW, height: barH, class: 'dn-bt-prob-track' }));
  const clears = svg.isNum(p) && svg.isNum(thr) ? p >= thr : null;
  if (svg.isNum(p)) {
    const w = Math.max(0, Math.min(1, p)) * axW;
    fig.appendChild(svgEl('rect', { x: padX, y: top, width: w, height: barH,
      class: 'dn-bt-prob-fill ' + (clears === false ? 'dn-caution-fill' : 'dn-good-fill') }));
  }
  if (svg.isNum(thr)) {
    const tx = padX + Math.max(0, Math.min(1, thr)) * axW;
    fig.appendChild(svgEl('line', { x1: tx, y1: top - 4, x2: tx, y2: top + barH + 4, class: 'dn-bt-prob-thr' }));
    // anchor/clamp the label inside the box: a middle-anchored label near the
    // right edge (the common 0.90-0.95 threshold sits at tx ~= 243 in a W=260,
    // preserveAspectRatio:'none' box) clips past the viewBox. Within EDGE of the
    // edge, end-anchor it just inside the right padding so it grows leftward off
    // the threshold mark; otherwise keep it centred on the mark (the common case).
    fig.appendChild(svg.edgeText({
      text: 'thr ' + svg.fmt(thr, 2),
      x: tx, y: top + barH + 14, anchor: 'middle',
      fontPx: 9, viewW: W, pad: padX, cls: 'dn-bt-prob-thrlab',
    }));
  }
  return el('div', { class: 'dn-bt-probwrap' }, [
    el('div', { class: 'dn-bt-probhead' }, [
      el('span', { class: 'dn-bt-problab', text: 'P(challenger stronger)' }),
      el('span', { class: 'dn-bt-probval dn-mono', text: svg.isNum(p) ? svg.fmt(p, 2)
        : '— · forms once both intervals fit' }),
    ]),
    fig,
  ]);
}

// The replicationStrip — shown when the rating decision is `deferred`. It reads
// the evidence the scheduler is still gathering: (1) replicates-spent pips in the
// dt-rungstep treatment, (2) the next closest-CI duel the scheduler will run to
// sharpen the estimate, and (3) a CI-convergence sparkline of P(stronger) over
// the recorded driver trace (ci_history). When the schedule is EXHAUSTED (no
// next_duel + not credible) it caps with an explicit "inconclusive" caption —
// the gate never fakes a crown out of an unresolved duel. Returns a Node, or null
// when there is nothing to strip (no replicates, no history, no next duel).
function replicationStrip(rating) {
  const spent = svg.isNum(rating.replicates_spent) ? rating.replicates_spent : 0;
  const hist = Array.isArray(rating.ci_history) ? rating.ci_history : [];
  const next = rating.next_duel && typeof rating.next_duel === 'object' ? rating.next_duel : null;
  const exhausted = !next && !rating.credible;
  const wrap = el('div', { class: 'dn-bt-replication' });
  wrap.appendChild(subhead('Replication · sharpening the estimate'));

  // (1) replicates-spent pips — dt-rungstep treatment. The pip count is the
  // replicates spent; all read "done" (already run). A zero-spent live-
  // reconstructed rating shows a faint "0 replicates spent" instead of an empty
  // strip.
  if (spent > 0) {
    const pips = el('div', { class: 'dt-rungstep', role: 'img',
      'aria-label': spent + ' replicate' + (spent === 1 ? '' : 's') + ' spent' });
    for (let i = 0; i < spent; i++) {
      pips.appendChild(el('span', { class: 'dt-rungstep-pip dt-rungstep-done', 'aria-hidden': 'true' }));
    }
    wrap.appendChild(el('div', { class: 'dn-bt-repl-row' }, [
      el('span', { class: 'dn-bt-repl-lab dn-faint', text: 'replicates spent' }),
      pips,
      el('span', { class: 'dn-bt-repl-n dn-mono dn-faint', text: String(spent) }),
    ]));
  } else {
    wrap.appendChild(el('div', { class: 'dn-bt-repl-row dn-faint', text: 'live-reconstructed · 0 replicates spent' }));
  }

  // (2) the next closest-CI duel.
  if (next && (next.left != null || next.right != null)) {
    wrap.appendChild(el('div', { class: 'dn-bt-repl-row dn-bt-nextduel' }, [
      el('span', { class: 'dn-bt-repl-lab dn-faint', text: 'next duel' }),
      el('span', { class: 'dn-mono dn-bt-duelpair', text: (next.left == null ? '?' : String(next.left))
        + ' vs ' + (next.right == null ? '?' : String(next.right)) }),
      el('span', { class: 'dn-faint', text: ' · closest-CI pair (sharpens P most)' }),
    ]));
  }

  // (3) the CI-convergence sparkline of P(stronger) across the driver trace.
  const pSeries = hist.map((h) => svg.isNum(h && h.p_stronger) ? h.p_stronger : NaN);
  if (pSeries.filter((v) => svg.isNum(v)).length >= 2) {
    wrap.appendChild(el('div', { class: 'dn-bt-repl-row dn-bt-convrow' }, [
      el('span', { class: 'dn-bt-repl-lab dn-faint', text: 'P(stronger) over duels' }),
      el('span', { class: 'dn-bt-convspark' }, [
        svg.sparkline({ values: pSeries, width: 120, height: 24,
          baseline: svg.isNum(rating.threshold) ? rating.threshold : undefined, minSpan: 0.1 }),
      ]),
    ]));
  }

  // schedule exhausted → an explicit inconclusive caption, NEVER a faked crown.
  if (exhausted) {
    wrap.appendChild(el('p', { class: 'dn-faint dn-bt-inconclusive',
      text: 'schedule exhausted — the duels did not separate the two strengths. Inconclusive: held deferred (no faked crown).' }));
  }
  return wrap;
}

// Build the Bradley–Terry uncertainty pre-gate block. Returns null when the
// feature is OFF (`rating` absent / `rating.present` falsy) so the gate panel is
// BYTE-IDENTICAL to today (back-compat). Below the credible-fit minimum it
// renders a "rating forms after N duels" placeholder instead of an estimate.
export function ratingBlock(rating) {
  if (!rating || !rating.present) return null;
  const wrap = el('div', { class: 'dn-bt-rating' });
  wrap.appendChild(subhead('Bradley–Terry uncertainty · resolve before the gate'));

  const nDuels = svg.isNum(rating.n_duels) ? rating.n_duels : 0;
  // below the credible-fit minimum: a placeholder, NOT a faked estimate.
  if (!rating.credible && nDuels < MIN_CREDIBLE_DUELS) {
    const need = MIN_CREDIBLE_DUELS - nDuels;
    wrap.appendChild(el('div', { class: 'dn-bt-forming dt-proj' }, [
      el('span', { class: 'dt-proj-badge', text: 'forming' }),
      el('span', { class: 'dn-faint', text: ' rating forms after ' + MIN_CREDIBLE_DUELS
        + ' duels · ' + nDuels + ' resolved' + (need > 0 ? ' (' + need + ' more)' : '') }),
    ]));
    // even while forming, the replication strip surfaces what's still being run.
    if (normaliseRatingDecision(rating) === 'deferred') wrap.appendChild(replicationStrip(rating));
    return wrap;
  }

  // the shared θ̂ domain spans both sides' intervals so the whiskers compare
  // directly; padded so the caps stay off the edge.
  const champ = rating.champion && typeof rating.champion === 'object' ? rating.champion : null;
  const chall = rating.challenger && typeof rating.challenger === 'object' ? rating.challenger : null;
  const pts = [];
  for (const s of [champ, chall]) {
    if (!s) continue;
    for (const k of ['theta', 'ci_lo', 'ci_hi']) if (svg.isNum(s[k])) pts.push(s[k]);
  }
  let lo = pts.length ? Math.min(...pts) : 0;
  let hi = pts.length ? Math.max(...pts) : 1;
  const pad = (hi - lo) * 0.15 || 0.5;
  lo -= pad; hi += pad;
  const dom = { lo, hi, span: (hi - lo) || 1 };

  // the challenger earns "good" iff its θ̂ exceeds the champion's; this is the
  // direction-earned tone (no new hue).
  const chalBetter = champ && chall && svg.isNum(champ.theta) && svg.isNum(chall.theta)
    ? (chall.theta >= champ.theta ? 'dn-good' : 'dn-bad') : 'dn-flat';
  wrap.appendChild(el('div', { class: 'dn-bt-whiskers' }, [
    ratingWhisker('champion', champ, dom, 'dn-flat'),
    ratingWhisker('challenger', chall, dom, chalBetter),
  ]));

  // the P(stronger) bar against the threshold.
  wrap.appendChild(ratingProbBar(
    svg.isNum(rating.p_stronger) ? rating.p_stronger : null,
    svg.isNum(rating.threshold) ? rating.threshold : null));

  // a one-line read of the rating's own verdict + the credible / overlap flags.
  const dec = normaliseRatingDecision(rating);
  const flags = [];
  if (rating.ci_overlap) flags.push('CIs overlap');
  if (!rating.credible) flags.push('not yet credible (< ' + MIN_CREDIBLE_DUELS + ' duels)');
  wrap.appendChild(el('p', { class: 'dn-faint dn-bt-readout', text:
    'rating ' + dec + ' · ' + nDuels + ' duel' + (nDuels === 1 ? '' : 's') + ' resolved'
    + (flags.length ? ' · ' + flags.join(' · ') : '') }));

  // a deferred rating drives the replication strip (the evidence still gathering).
  if (dec === 'deferred') wrap.appendChild(replicationStrip(rating));
  return wrap;
}

// Normalise the rating block's own decision to {promoted, deferred} — verbatim
// from `rating.decision` (build_rating_view emits "promoted"/"deferred"). Unknown
// / absent → "deferred" (the safe, never-faked-crown default).
function normaliseRatingDecision(rating) {
  const d = String((rating && rating.decision) || '').toLowerCase();
  return d.includes('promot') ? 'promoted' : 'deferred';
}

// A content digest of the rating block — present + credible + decision + each
// side's ROUNDED {theta, ci_lo, ci_hi} (no se: it does not drive the render) +
// ROUNDED p_stronger/threshold + ci_overlap + n_duels + replicates_spent + the
// next_duel pair + the ci_history P(stronger) trace (ROUNDED, NO timestamps).
// null when absent / not present so it contributes NOTHING to the gate digest —
// a non-BT round's digest is byte-identical to the pre-feature path. A no-op beat
// re-emits identical numbers → equal digest → skipped; a duel resolving (n_duels
// grows, a CI tightens, P moves past 2dp) flips it → repaint.
export function ratingDigest(rating) {
  if (!rating || !rating.present) return null;
  const side = (s) => (s && typeof s === 'object') ? [
    svg.isNum(s.theta) ? s.theta.toFixed(3) : null,
    svg.isNum(s.ci_lo) ? s.ci_lo.toFixed(3) : null,
    svg.isNum(s.ci_hi) ? s.ci_hi.toFixed(3) : null,
  ] : null;
  const next = rating.next_duel && typeof rating.next_duel === 'object'
    ? [rating.next_duel.left == null ? null : String(rating.next_duel.left),
       rating.next_duel.right == null ? null : String(rating.next_duel.right)] : null;
  const hist = (Array.isArray(rating.ci_history) ? rating.ci_history : []).map((h) =>
    svg.isNum(h && h.p_stronger) ? h.p_stronger.toFixed(3) : null);
  return [
    !!rating.credible, normaliseRatingDecision(rating),
    side(rating.champion), side(rating.challenger),
    svg.isNum(rating.p_stronger) ? rating.p_stronger.toFixed(3) : null,
    svg.isNum(rating.threshold) ? rating.threshold.toFixed(3) : null,
    !!rating.ci_overlap,
    svg.isNum(rating.n_duels) ? rating.n_duels : 0,
    svg.isNum(rating.replicates_spent) ? rating.replicates_spent : 0,
    next, hist,
  ];
}

// ── the DIFF-COMPLEXITY line item (a free-riding parsimony read) ─────────────
//
// When the contract carries a non-zero diff_complexity_weight, the scalar grows
// a `diff_complexity` term that penalises a bigger patch (more files / lines
// changed). That term rides on `gate.scalar_components.{champion,challenger}`
// as a per-side float — the SAME map the radar already plots — so we surface it
// as ONE extra row in the rules ladder: the two per-side diff-complexity costs +
// their Δ, in the deterministic-rule grammar (a dn-rule row).
//
// It is INFORMATIONAL, never short-circuiting: a parsimony cost is folded into
// the weighted scalar, it does not REJECT on its own. So the row reads NEUTRAL
// (flat dot) when the candidate's patch is no costlier than the champion's, and
// takes the CAUTION tone (the shipped --v2-caution decision token, NO new hue)
// only when the candidate's diff-complexity is strictly HIGHER — i.e. it is the
// term pulling AGAINST promotion. `fired` is NEVER set (this is not a
// short-circuit rule). Returns a rule-shaped dict {id,label,status,detail,fired}
// or null.
//
// Back-compat (NO Python edits): the diff_complexity key is ABSENT from the
// component maps on every default-off run (diff_complexity_weight = 0). Absent →
// null → no row → the ladder is byte-identical to today. There is intentionally
// NO read of `gate.diff_size` — that structured {added,removed,patches} block is
// NOT emitted by build_gate_breakdown on this branch (followup: wire
// diff_size_evidence() into the reader, Python, out of scope here).
const DIFF_COMPLEXITY_KEY = 'diff_complexity';
export function diffComplexityRule(gate) {
  const sc = gate && gate.scalar_components;
  if (!sc) return null;
  const champ = sc.champion && svg.isNum(sc.champion[DIFF_COMPLEXITY_KEY]) ? sc.champion[DIFF_COMPLEXITY_KEY] : null;
  const chall = sc.challenger && svg.isNum(sc.challenger[DIFF_COMPLEXITY_KEY]) ? sc.challenger[DIFF_COMPLEXITY_KEY] : null;
  // the term is absent from BOTH sides (weight 0 / pre-feature) → no row.
  if (champ == null && chall == null) return null;
  // the candidate's patch is costlier than the champion's ⇒ the parsimony term
  // is pulling against it (the "actually rejects" read) → caution; otherwise the
  // row reads neutral (a flat-dot informational line).
  const worse = champ != null && chall != null && chall > champ;
  const detail = (champ != null && chall != null)
    ? `${svg.fmt(champ, 2)} → ${svg.fmt(chall, 2)} (${svg.fmtSigned(chall - champ, 2)}; lower = simpler patch)`
    : (chall != null ? `candidate ${svg.fmt(chall, 2)} (champion term absent)` : `champion ${svg.fmt(champ, 2)} (candidate term absent)`);
  return {
    id: 'diff_complexity',
    label: prettyComponentLabel(DIFF_COMPLEXITY_KEY),
    status: worse ? 'caution' : 'neutral',
    detail,
    fired: false,
  };
}

// A content digest of the diff-complexity rule: the two rounded per-side costs +
// the caution flag (NO timestamp). null when the term is absent on both sides so
// it contributes NOTHING to the gate digest (back-compat: byte-identical to the
// pre-feature path). A no-op heartbeat re-emits the same numbers → equal digest;
// the term moving (a re-scored patch) or appearing (weight turned on) flips it.
export function diffComplexityDigest(gate) {
  const sc = gate && gate.scalar_components;
  if (!sc) return null;
  const champ = sc.champion && svg.isNum(sc.champion[DIFF_COMPLEXITY_KEY]) ? sc.champion[DIFF_COMPLEXITY_KEY].toFixed(2) : null;
  const chall = sc.challenger && svg.isNum(sc.challenger[DIFF_COMPLEXITY_KEY]) ? sc.challenger[DIFF_COMPLEXITY_KEY].toFixed(2) : null;
  if (champ == null && chall == null) return null;
  return [champ, chall];
}

// fix #1 — the stacked, non-overlapping gate panel:
// (a) decision header, (b) the rules ladder (each rule its own row).
// The old (c) champion-vs-challenger SCALAR-COMPONENTS comparison block was
// REMOVED — the FINAL liked study (single-generation.html opt 2) dropped it as
// redundant with the RADAR SILHOUETTE (which now compares candidate vs champion
// across the same scalar / pass-rate / per-judge axes). The deciding-rule detail
// the components used to carry now reads off the gate-rule ladder + the radar.
export function gatePanel(gate, comparison, spec) {
  const card = el('div', { class: 'dn-panel dn-gate' });
  // Class B: a gate with no resolved decision is still pending, not rejected.
  // The backend emits decision:"deferred" verbatim until BOTH aggregates
  // resolve — decisionOf threads it through to its caution-toned pill.
  const decision = decisionOf(gate) || 'pending';
  // operator-override provenance rides BESIDE the verdict (overrideChip) WITHOUT
  // recoloring it. Absent (gate-decided / pre-feature) → null → byte-identical.
  const ovChip = overrideChip(gate && gate.override);
  // hover detail (the operator's reason) lives in the hovercard singleton,
  // OUTSIDE the gated render — the chip itself stays a stable node.
  if (ovChip && gate && gate.override) {
    const ov = gate.override;
    const act = ov.action === 'promote' ? 'force-promoted' : 'force-rejected';
    attachHovercard(ovChip, () => hovercardBody([
      el('div', { class: 'dn-hc-title', text: 'operator override · ' + act }),
      ov.reason ? el('div', { class: 'dn-hc-row', text: ov.reason })
        : el('div', { class: 'dn-hc-row dn-faint', text: 'no reason recorded' }),
    ]));
  }
  card.appendChild(el('div', { class: 'dn-gate-head' }, [
    el('div', { class: 'dn-gate-decision' }, [verdictPill(decision), ovChip].filter(Boolean)),
    // ABSOLUTE scalars sit LEFT of the Δ chips — the settled champion floor and
    // the candidate's absolute scalar (or, mid-flight, its PROJECTED scalar in
    // the projStat treatment) so the operator reads the two ENDPOINTS the Δ is
    // taken between, not just the gap. Each side is absent-tolerant: a pre-#19 /
    // unresolved aggregate (champion_scalar/challenger_scalar = null) drops its
    // chip; absent everything → byte-identical to today (no abs block at all).
    absoluteScalars(gate),
    el('div', { class: 'dn-row dn-gate-deltas' }, [
      svg.isNum(gate.delta_scalar) ? stat(svg.fmtSigned(gate.delta_scalar, 2), 'Δ scalar (loss)') : null,
      svg.isNum(gate.delta_pass_rate) ? stat(svg.fmtSigned(gate.delta_pass_rate, 2), 'Δ pass rate') : null,
      gate.primary_driver && gate.primary_driver.judge ? stat(gate.primary_driver.judge, 'primary driver') : null,
    ].filter(Boolean)),
  ].filter(Boolean)));
  if (gate.reason) card.appendChild(el('p', { class: 'dn-gate-reason', text: gate.reason }));

  // the OVERRIDE PROVENANCE caption — 'gate said X · operator forced Y' — names
  // the divergence in words (the rules' decision vs the operator's force). Present
  // ONLY when gate.override is present; a gate-decided round → byte-identical.
  if (gate.override && gate.override.present) {
    const forcedAction = gate.override.action === 'promote' ? 'force-promoted' : 'force-rejected';
    const gateSaid = decisionOf(gate) || 'deferred';
    card.appendChild(el('p', { class: 'dn-gate-override-cap dn-faint', style: 'font-size:11px;margin:6px 0 0;',
      text: 'gate said ' + gateSaid + ' · operator ' + forcedAction
        + (gate.override.reason ? ' — ' + gate.override.reason : '') }));
  }

  // (b0) the BRADLEY–TERRY uncertainty PRE-GATE (the marquee) — the operator's
  // first read on a confidence-thresholded run: the two θ̂ whiskers + the
  // P(challenger stronger) bar against the threshold, and (when deferred) the
  // replication strip. Renders ABOVE the deterministic rule ladder because it
  // resolves the winner BEFORE the rules apply. Absent / present:false (a
  // pre-BT / disabled run) → null → the gate panel is byte-identical to today.
  const rating = ratingBlock(gate.rating);
  if (rating) card.appendChild(rating);

  const rules = Array.isArray(gate.rules) ? gate.rules : [];
  // the DIFF-COMPLEXITY line item free-rides the same ladder: an informational
  // parsimony row appended AFTER the deterministic rules (it does not
  // short-circuit). Absent (weight 0 / pre-feature) → null → the ladder is
  // byte-identical to today.
  const diffRule = diffComplexityRule(gate);
  const ladderRules = diffRule ? rules.concat([diffRule]) : rules;
  if (ladderRules.length) {
    card.appendChild(subhead('Rules · short-circuiting, in order'));
    const ladder = el('ol', { class: 'dn-rules' });
    for (const r of ladderRules) {
      const st = String(r.status || 'pending');
      ladder.appendChild(el('li', { class: 'dn-rule dn-rule-' + st }, [
        el('span', { class: 'dn-rule-dot', 'aria-hidden': 'true' }),
        el('span', { class: 'dn-rule-label', text: r.label || r.id }),
        el('span', { class: 'dn-rule-status', text: st.replace(/_/g, ' ') }),
        el('span', { class: 'dn-rule-detail dn-faint', text: r.detail || '' }),
      ]));
    }
    card.appendChild(ladder);
  }

  // (c) the SCALAR DECOMPOSITION (#19): WHICH transform / plugin produced the
  // pass term + drift component on each side, parsed from the recorded
  // provenance tokens. Renders only when a transform / plugin actually fired
  // (a plain built-in / pre-#19 round shows nothing — back-compat clean). A
  // plugin that FAILED OPEN is surfaced loudly (caution-colored banner + row).
  const decomp = scalarDecomp(gate.scalar_decomposition);
  if (decomp) card.appendChild(decomp);

  // (d) WHICH JUDGE DECIDED THIS ROUND — the per-judge champion-vs-challenger
  // ledger the gate's one-word `primary_driver` was picked from. Absent /
  // unserved → null → the panel is byte-identical to before the read existed.
  const cmp = perJudgeComparisonBlock(comparison, spec);
  if (cmp) card.appendChild(cmp);

  return card;
}

// ── WHICH JUDGE DECIDED THE ROUND (per-judge comparison) ─────────────────────
//
// `/api/round/{epoch}/{champion}/{challenger}/per-judge-comparison`
// (query/judge_view.py build_per_judge_comparison) joins the two sides' judge
// ledgers into `{judges: [{judge_name, champion_weighted_loss,
// challenger_weighted_loss, delta}], primary_driver}`. `delta` is
// challenger − champion, so NEGATIVE = the challenger drifted LESS on that
// judge (better). `primary_driver` is the judge with the largest |delta| — the
// server's call, rendered, never re-derived here.
//
// Returns null when the read is absent or names no judge, so a round without an
// indexed judge ledger renders exactly as it did before.
export function perJudgeComparisonBlock(comparison, spec) {
  if (!comparison || typeof comparison !== 'object') return null;
  const rows = (Array.isArray(comparison.judges) ? comparison.judges : [])
    .filter((j) => j && j.judge_name);
  if (!rows.length) return null;
  const champLabel = (spec && spec.champ) || comparison.champion || 'champion';
  const chalLabel = (spec && spec.chall) || comparison.challenger || 'challenger';
  const driver = comparison.primary_driver == null ? null : String(comparison.primary_driver);

  const wrap = el('div', { class: 'dn-gate-judgecmp' });
  wrap.appendChild(subhead('Which judge decided it · per-judge weighted loss'));
  // biggest mover first — the same ordering the driver is chosen by.
  const sorted = rows.slice().sort((a, b) => {
    const av = svg.isNum(a.delta) ? Math.abs(a.delta) : -1;
    const bv = svg.isNum(b.delta) ? Math.abs(b.delta) : -1;
    return bv - av;
  });
  wrap.appendChild(dataTable({
    class: 'dn-board-table dn-judgecmp-table',
    columns: [{ label: 'judge' }, { label: champLabel, class: 'dn-num' },
      { label: chalLabel, class: 'dn-num' }, { label: 'Δ', class: 'dn-num' }],
    rows: sorted.map((j) => {
      const name = String(j.judge_name);
      const isDriver = driver != null && name === driver;
      const d = svg.isNum(j.delta) ? j.delta : null;
      // lower loss is better, so a NEGATIVE Δ (challenger drifted less) is good.
      const dCls = d == null ? '' : (d < 0 ? 'dn-good-t' : d > 0 ? 'dn-bad-t' : '');
      return {
        class: isDriver ? 'dn-judgecmp-driver' : '',
        dataset: { judge: name },
        cells: [
          { el: el('span', null, [
            el('span', { class: 'dn-mono', text: name }),
            isDriver ? el('span', { class: 'dn-judgecmp-drivertag dn-faint', text: ' · primary driver' }) : null,
          ].filter(Boolean)) },
          { class: 'dn-num dn-mono', text: svg.isNum(j.champion_weighted_loss) ? svg.fmt(j.champion_weighted_loss, 2) : '—' },
          { class: 'dn-num dn-mono', text: svg.isNum(j.challenger_weighted_loss) ? svg.fmt(j.challenger_weighted_loss, 2) : '—' },
          { class: 'dn-num dn-mono ' + dCls, text: d == null ? '—' : svg.fmtSigned(d, 2) },
        ],
      };
    }),
  }));
  wrap.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;',
    text: 'weighted process-drift loss per judge · Δ = challenger − champion, so NEGATIVE is better · '
      + (driver ? 'the round turned on ' + driver + ' (largest |Δ|)' : 'no single judge dominated') }));
  return wrap;
}

// A content digest of the per-judge comparison ledger — each judge's rounded
// pair + Δ, plus the primary driver. Returns null when absent / empty so it
// contributes NOTHING to the candidate digest (back-compat: a round with no
// indexed judge ledger digests exactly as it did before this read existed).
export function judgeComparisonDigest(comparison) {
  if (!comparison || typeof comparison !== 'object') return null;
  const rows = (Array.isArray(comparison.judges) ? comparison.judges : [])
    .filter((j) => j && j.judge_name);
  if (!rows.length) return null;
  return [
    comparison.primary_driver == null ? null : String(comparison.primary_driver),
    rows.map((j) => [
      String(j.judge_name),
      svg.isNum(j.champion_weighted_loss) ? j.champion_weighted_loss.toFixed(3) : null,
      svg.isNum(j.challenger_weighted_loss) ? j.challenger_weighted_loss.toFixed(3) : null,
      svg.isNum(j.delta) ? j.delta.toFixed(3) : null,
    ]).sort(),
  ];
}

// ── the PROPOSER PREDICTION-ACCURACY + CALIBRATION scorecard (DIAGNOSTIC) ──────
//
// The promote gate answers "did the candidate win?"; this answers a DIFFERENT,
// orthogonal question — "did the PROPOSER predict what would happen?". It reads
// the hypothesis-accuracy payload (build_hypothesis_accuracy): each falsifiable
// movement the proposer claimed, joined against the realised movement, with the
// STAMPED hit/miss verdict; plus the realised movements the proposer never
// claimed (unpredicted). The headline is the calibration fraction (hits/total).
//
// It NEVER couples to the gate — a perfectly-calibrated proposer can still lose,
// and a miscalibrated one can still win. The caption says so explicitly. Every
// hover-level detail (from→to rate, signed error, the proposer's note) lives in
// the hovercard singleton, OUTSIDE the gated render.
//
// Returns null when there is nothing to show (seed / no experiment / no claims),
// so a candidate the proposer made no falsifiable claim about is byte-identical
// to the pre-feature dossier (back-compat clean).

// Map one claim to its single-glyph verdict (the dn-pred-glyph token + a tone):
//   hit  ✓ (good)   — predicted direction matched the realised movement
//   miss ✗ (bad)    — predicted, but the realised movement went the other way
//   band ◌ (flat)   — predicted, but no realised movement was paired (unresolved)
//   unp  ＋ (flat)   — a realised movement the proposer never predicted (context)
function predictionVerdict(claim) {
  if (claim && claim.unpredicted) return { kind: 'unp', glyph: '＋', tone: 'flat', label: 'unpredicted' };
  if (claim && claim.hypothesis_match === true) return { kind: 'hit', glyph: '✓', tone: 'good', label: 'hit' };
  if (claim && claim.hypothesis_match === false) return { kind: 'miss', glyph: '✗', tone: 'bad', label: 'miss' };
  // predicted but never paired against a realised movement (no outcome yet).
  return { kind: 'band', glyph: '◌', tone: 'flat', label: 'unresolved' };
}

function dirArrow(d) {
  const s = String(d == null ? '' : d).toLowerCase();
  if (s === 'up' || s === 'increase' || s === '+') return '↑';
  if (s === 'down' || s === 'decrease' || s === '-') return '↓';
  if (s === 'flat' || s === 'none' || s === 'same') return '→';
  return '·';
}

export function buildPredictionScorecard(scorecard) {
  if (!scorecard || typeof scorecard !== 'object') return null;
  const claims = Array.isArray(scorecard.claims) ? scorecard.claims : [];
  const score = (scorecard.score && typeof scorecard.score === 'object') ? scorecard.score : {};
  const predicted = claims.filter((c) => c && !c.unpredicted);
  // nothing the proposer claimed AND nothing realised-but-unclaimed → no card.
  if (!claims.length) return null;

  const card = el('div', { class: 'dn-panel dn-predcard' });

  // ── headline: the calibration fraction (hits / total predicted claims) ──
  const hits = svg.isNum(score.hits) ? score.hits : null;
  const total = svg.isNum(score.total) ? score.total : null;
  const frac = svg.isNum(score.fraction) ? score.fraction : null;
  // the proposer earns "good" by calibrating well (≥ half its claims land); the
  // tone is direction-earned, no new hue.
  const headTone = frac == null ? 'dn-flat' : (frac >= 0.5 ? 'dn-good' : 'dn-bad');
  const headValue = (hits != null && total != null)
    ? hits + '/' + total + (frac != null ? ' · ' + Math.round(frac * 100) + '%' : '')
    : '—';
  card.appendChild(el('div', { class: 'dn-predcard-head' }, [
    el('div', { class: 'dn-stat' }, [
      el('span', { class: 'v dn-predcard-frac ' + headTone, text: headValue }),
      el('span', { class: 'k', text: 'calibration · proposer claims that landed' }),
    ]),
    // the pass-rate claim rides beside the fraction as free text (it is NOT a
    // stamped match, so it never enters hits/total) — predicted vs observed.
    predRateChip(scorecard.pass_rate),
  ].filter(Boolean)));

  // ── the per-movement matrix: one row per claim, predicted → observed + glyph ──
  // predicted claims first (they carry the verdict), then unpredicted context.
  const ordered = [...predicted, ...claims.filter((c) => c && c.unpredicted)];
  const tbl = dataTable({
    class: 'dn-board-table dn-predtable',
    columns: [{ label: 'movement' }, { label: 'predicted' }, { label: 'observed' }, { label: '', class: 'dn-num' }],
    rows: ordered.map((c) => {
      const v = predictionVerdict(c);
      const pred = c.unpredicted
        ? '—'
        : dirArrow(c.predicted_direction) + (c.predicted_magnitude ? ' ' + String(c.predicted_magnitude) : '');
      const obs = dirArrow(c.observed_direction)
        + (svg.isNum(c.from_rate) && svg.isNum(c.to_rate)
          ? ' ' + svg.fmt(c.from_rate, 2) + '→' + svg.fmt(c.to_rate, 2) : '');
      const glyph = el('span', { class: 'dn-pred-glyph dn-' + v.tone, text: v.glyph,
        'aria-label': v.label });
      // hover-level detail (signed error · note · kind) lives in the hovercard
      // singleton, OUTSIDE the gated render — the glyph node stays stable.
      attachHovercard(glyph, () => hovercardBody([
        el('div', { class: 'dn-hc-title', text: (c.target || 'movement') + ' · ' + v.label }),
        el('div', { class: 'dn-hc-row', text: 'kind · ' + (c.kind || '—') }),
        c.unpredicted
          ? el('div', { class: 'dn-hc-row dn-faint', text: 'realised movement the proposer did not claim' })
          : el('div', { class: 'dn-hc-row', text: 'predicted · ' + (c.predicted_direction || '—')
            + (c.predicted_magnitude ? ' (' + c.predicted_magnitude + ')' : '') }),
        svg.isNum(c.signed_error)
          ? el('div', { class: 'dn-hc-row', text: 'signed error · ' + svg.fmtSigned(c.signed_error, 3) }) : null,
        c.note ? el('div', { class: 'dn-hc-row dn-faint', text: String(c.note) }) : null,
      ]));
      return {
        class: 'dn-predrow' + (c.unpredicted ? ' dn-predrow-unp' : ''),
        cells: [
          { class: 'dn-mono', text: c.target || '—' },
          { class: 'dn-faint', text: pred },
          { text: obs },
          { class: 'dn-num', el: glyph },
        ],
      };
    }),
  });
  card.appendChild(tbl);

  // ── the EXPLICIT diagnostic caption (the non-negotiable disclaimer) ──
  card.appendChild(el('p', { class: 'dn-faint dn-predcard-cap', style: 'font-size:11px;margin:8px 0 0;',
    text: 'diagnostic — does not affect the gate · ✓ hit · ✗ miss · ◌ unresolved · ＋ unpredicted (not scored)' }));
  return card;
}

// The pass-rate claim chip: the proposer's free-text predicted Δ vs the realised
// board-wide Δ. NOT a stamped match (no hits/total contribution) — surfaced for
// context. Returns null when the proposer made no pass-rate claim.
function predRateChip(pr) {
  if (!pr || typeof pr !== 'object') return null;
  const predicted = pr.predicted == null ? '' : String(pr.predicted);
  const observed = svg.isNum(pr.observed) ? svg.fmtSigned(pr.observed, 2) : null;
  if (!predicted && observed == null) return null;
  return el('div', { class: 'dn-stat dn-predrate' }, [
    el('span', { class: 'v', text: observed == null ? (predicted || '—') : observed }),
    el('span', { class: 'k', text: 'pass-rate Δ · ' + (predicted ? 'claimed “' + truncate(predicted, 24) + '”' : 'observed') }),
  ]);
}

// A content digest of the prediction scorecard — the rounded calibration
// fraction + hits/total + each claim's (target, kind, verdict, predicted/observed
// direction, ROUNDED from/to rate) + the pass-rate pair. NO timestamps. null when
// absent / no claims so it contributes NOTHING to the dossier digest (a candidate
// with no falsifiable claim is byte-identical to the pre-feature path). A no-op
// beat re-emits identical numbers → equal digest → skip; a movement landing (a
// from/to rate appearing, a verdict flipping, the fraction moving past 2dp) flips
// it → repaint. This is where the SSE-heartbeat flashing bug class lives.
function scorecardDigest(scorecard) {
  if (!scorecard || typeof scorecard !== 'object') return null;
  const claims = Array.isArray(scorecard.claims) ? scorecard.claims : [];
  if (!claims.length) return null;
  const score = (scorecard.score && typeof scorecard.score === 'object') ? scorecard.score : {};
  const pr = (scorecard.pass_rate && typeof scorecard.pass_rate === 'object') ? scorecard.pass_rate : {};
  return [
    svg.isNum(score.hits) ? score.hits : null,
    svg.isNum(score.total) ? score.total : null,
    svg.isNum(score.fraction) ? score.fraction.toFixed(3) : null,
    claims.map((c) => [
      c.target == null ? null : String(c.target),
      c.kind == null ? null : String(c.kind),
      predictionVerdict(c).kind,
      c.predicted_direction == null ? null : String(c.predicted_direction),
      c.observed_direction == null ? null : String(c.observed_direction),
      svg.isNum(c.from_rate) ? c.from_rate.toFixed(3) : null,
      svg.isNum(c.to_rate) ? c.to_rate.toFixed(3) : null,
    ]),
    pr.predicted == null ? null : String(pr.predicted),
    svg.isNum(pr.observed) ? pr.observed.toFixed(3) : null,
  ];
}

// Render the scalar-provenance decomposition for a gate. Returns null when no
// transform / plugin fired on either side (built-in everywhere or a pre-#19
// run with `present:false`) so a default round stays visually quiet. When a
// transform / plugin DID shape the scalar, shows per-side which one produced
// the pass term + drift component; a FAIL-OPEN plugin (one that fell back to
// the built-in / transformed default) is flagged as a first-class caution
// signal — a prominent banner plus a caution-colored row — so a silently
// degraded plugin is obvious here, not buried in a WARNING log.
function scalarDecomp(d) {
  if (!d || !d.present) return null;
  const wrap = el('div', { class: 'dn-scalar-decomp' });
  wrap.appendChild(subhead('Scalar provenance · which transform / plugin shaped this'));

  if (d.fail_open) {
    wrap.appendChild(el('div', { class: 'dn-decomp-banner', role: 'status' }, [
      el('span', { class: 'dn-decomp-banner-dot', 'aria-hidden': 'true' }),
      el('span', { text: 'A scoring plugin FAILED OPEN — it raised or returned a non-finite value and fell back to the built-in / transformed default. The scalar below is the fallback, not the plugin’s output.' }),
    ]));
  }

  const sides = el('div', { class: 'dn-decomp-sides' });
  const champ = decompSide('champion', d.champion);
  const chall = decompSide('challenger', d.challenger);
  if (champ) sides.appendChild(champ);
  if (chall) sides.appendChild(chall);
  if (!sides.childNodes.length) return null;
  wrap.appendChild(sides);
  return wrap;
}

// A content digest of the scalar-provenance decomposition: present + fail-open
// + each side/seam's (kind, source, fail_open). null when absent / not present
// (built-in / pre-#19) so it contributes NOTHING to the gate digest — a default
// round's digest is byte-identical to the pre-#19 path (back-compat). Used by
// the content-gated render so a provenance change repaints but a no-op
// heartbeat does not.
function decompDigest(d) {
  if (!d || !d.present) return null;
  const seam = (v) => v ? [v.kind || null, v.source || null, !!v.fail_open, v.fallback_reason || null] : null;
  const side = (sd) => sd ? [seam(sd.scalar), seam(sd.drift)] : null;
  return [!!d.fail_open, side(d.champion), side(d.challenger)];
}

// One side (champion / challenger) of the decomposition: a small card naming
// the pass-term and drift-component producer. Returns null when the side is
// absent. A side that is plain built-in still renders (quietly) so the two
// sides line up; the per-seam row is caution-colored iff that seam failed open.
function decompSide(label, side) {
  if (!side) return null;
  const card = el('div', { class: 'dn-decomp-side' });
  card.appendChild(el('div', { class: 'dn-decomp-sidehead', text: label }));
  card.appendChild(decompRow('pass', side.scalar));
  card.appendChild(decompRow('drift', side.drift));
  return card;
}

// One seam row: "<seam>  <source>  <kind/fallback tag>". The source is the
// transform token / dotted plugin spec / "built-in"; a built-in row reads
// faint, a fail-open row reads loud (caution).
function decompRow(seam, view) {
  view = view || { kind: 'builtin', source: 'built-in', fail_open: false };
  const isBuiltin = view.kind === 'builtin' && !view.fail_open;
  const failOpen = !!view.fail_open;
  const cls = 'dn-decomp-row'
    + (isBuiltin ? ' dn-decomp-builtin' : '')
    + (failOpen ? ' dn-decomp-failopen' : '');
  const source = view.source || 'built-in';
  const tagText = failOpen
    ? ('fell back · ' + (view.fallback_reason || 'plugin failed'))
    : (view.kind && view.kind !== 'builtin' ? view.kind : '');
  return el('div', { class: cls }, [
    el('span', { class: 'dn-decomp-seam', text: seam }),
    el('div', {}, [
      el('span', { class: 'dn-decomp-src', text: source }),
      tagText ? el('span', { class: failOpen ? 'dn-decomp-failtag' : 'dn-decomp-kindtag', text: ' · ' + tagText }) : null,
    ].filter(Boolean)),
  ]);
}

function entryDrilldown(ctx, epochId, genId, entryId, row, exps, judges, header) {
  const runId = row ? row.run_id : null;
  const card = el('div', { class: 'dn-panel dn-drill' });
  card.appendChild(el('div', { class: 'dn-row' }, [
    stat(row && svg.isNum(row.drift_loss) ? svg.fmt(row.drift_loss, 1) : '—', 'drift loss'),
    stat(row ? passLabel(row.pass_fail) : '—', 'predicate'),
    // continuous per-entry outcome (#18) — only when this entry was scored;
    // a bool-only entry shows just the pass/fail predicate above, unchanged.
    row && svg.isNum(row.score) ? stat(svg.fmt(row.score, 2), 'score') : null,
    // precision/recall decomposition (#18) when the scorer exposed it.
    row && prText(row.metrics) ? stat(prText(row.metrics), 'precision / recall') : null,
    stat(row && row.wall_clock_budget_exceeded ? 'timed out' : (row && svg.isNum(row.runtime_ms) ? `${(row.runtime_ms / 1000).toFixed(0)}s` : '—'), 'runtime'),
  ].filter(Boolean)));

  const outcomes = (exps && Array.isArray(exps.outcomes)) ? exps.outcomes : [];
  if (outcomes.length) {
    const grid = el('div', { class: 'dn-expect-grid', style: 'margin-top:12px;' });
    for (const o of outcomes) {
      const cls = o.passed === true ? 'dn-good' : o.passed === false ? 'dn-bad' : 'dn-flat';
      grid.appendChild(el('div', { class: 'dn-expect-row' }, [
        el('span', { class: 'dn-expect-dot ' + cls, title: o.passed === true ? 'passed' : o.passed === false ? 'failed' : 'no verdict' }),
        el('span', { class: 'dn-expect-kind', text: o.kind || 'expectation' }),
        o.judge_name ? el('span', { class: 'dn-faint', text: ' · ' + o.judge_name }) : null,
        el('span', { class: 'dn-expect-detail dn-faint', text: o.detail ? ' — ' + o.detail : '' }),
      ].filter(Boolean)));
    }
    card.appendChild(grid);
  } else {
    card.appendChild(el('div', { style: 'margin-top:12px;' }, [empty('No expectation recorded for this entry (no predicate / rubric).')]));
  }

  // ── per-judge drift: RAW beside WEIGHTED (the two are different questions) ──
  // The endpoint serves {judge_name, weighted_loss, raw_loss, weight}. The bars
  // plot the WEIGHTED loss (what the scalar actually paid), but a tall bar is
  // ambiguous on its own: it can mean the judge FIRED HARD (big raw_loss) or
  // merely that the judge CARRIES A BIG WEIGHT. The table beneath separates
  // them — raw × weight = weighted — so "why is this judge dominating?" is
  // answerable without opening the contract.
  const jrows = (judges && Array.isArray(judges.judges)) ? judges.judges : [];
  const jitems = jrows.filter((j) => svg.isNum(j.weighted_loss)).sort((a, b) => b.weighted_loss - a.weighted_loss).map((j) => ({ label: j.judge_name, value: j.weighted_loss }));
  if (jitems.length) {
    card.appendChild(el('p', { class: 'dn-faint', style: 'margin:14px 0 4px;font-size:11px;', text: 'per-judge weighted process-drift loss · higher = more drift' }));
    const djt = densityTokens();
    card.appendChild(svg.valueBars({ width: 420, rowHeight: Math.round(20 * djt.sizeScale), labelWidth: 180, items: jitems }));
  }
  if (jrows.length) {
    const sortedJ = jrows.slice().sort((a, b) => {
      const av = svg.isNum(a.weighted_loss) ? a.weighted_loss : -Infinity;
      const bv = svg.isNum(b.weighted_loss) ? b.weighted_loss : -Infinity;
      return bv - av;
    });
    card.appendChild(dataTable({
      class: 'dn-board-table dn-judgeraw-table',
      columns: [{ label: 'judge' }, { label: 'raw', class: 'dn-num' },
        { label: 'weight', class: 'dn-num' }, { label: 'weighted', class: 'dn-num' }],
      rows: sortedJ.map((j) => ({
        dataset: { judge: String(j.judge_name == null ? '' : j.judge_name) },
        cells: [
          { class: 'dn-mono', text: j.judge_name == null ? '—' : String(j.judge_name) },
          { class: 'dn-num dn-mono', text: svg.isNum(j.raw_loss) ? svg.fmt(j.raw_loss, 2) : '—' },
          { class: 'dn-num dn-mono dn-faint', text: svg.isNum(j.weight) ? svg.fmt(j.weight, 2) : '—' },
          { class: 'dn-num dn-mono', text: svg.isNum(j.weighted_loss) ? svg.fmt(j.weighted_loss, 2) : '—' },
        ],
      })),
    }));
    card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:6px 0 0;',
      text: 'raw = how hard the judge fired · weight = how much the contract cares · weighted = what the scalar paid (raw × weight)' }));
  }

  // fix #5 path: the transcript opens INLINE on the board view (no separate run page).
  // The harmonograf link is the per-run EXECUTION trace (this run's goldfive
  // events, keyed on its adk_session_id) — rendered only while a run is LIVE
  // (the auto-launched harmonograf server dies with the loop, so a link built
  // after it ends points at a dead port; harmonografLink returns null then).
  const hgExec = harmonografLink(header || {}, 'Open this run in harmonograf');
  card.appendChild(el('div', { style: 'margin-top:14px;' }, [
    el('a', { class: 'dn-linkbtn', href: ctx.href('board', { epochId, entry: entryId, gen: genId }), text: 'Open the transcript inline (vs champion) →' }),
    runId ? el('span', { class: 'dn-faint dn-mono', style: 'margin-left:8px;', text: runId.slice(0, 10) + '…' }) : null,
    hgExec ? el('span', { class: 'dn-faint', style: 'margin-left:8px;' }, ['· ', hgExec]) : null,
  ].filter(Boolean)));

  return section('Entry · ' + entryId, card);
}

// Derive a short tournament-context tag for a per-entry record — the rung /
// round / matchup the board run executed in. The per-entry records already
// carry `match_id` (e.g. `rung0_m2`, `racing-final`, `round1_m2`, `g0`) and a
// pre-formatted `rung` (e.g. "rung 0"); we read those rather than re-deriving
// what the backend formatted. The same scheme `reconstructRacing` reads in
// views/structure.js: `rungN_*` → rung N, `racing-final` → the champion gate.
//   • racing : "rung 0" / "rung 1"; `racing-final` → "champion-gate".
//   • gauntlet: `roundN`/`gN` → "round N".
//   • swiss  : `roundN_mM` / `swiss_rN_mM` → "round N · match M".
// Returns null when no context can be derived (so the row renders name-only).
export function tournamentContext(rec) {
  if (!rec) return null;
  const mid = rec.match_id != null ? String(rec.match_id) : '';
  // the champion gate (racing final) reads cleanest as its own label.
  if (mid === 'racing-final') return 'champion-gate';

  // prefer the backend's pre-formatted rung string when present.
  const preRung = rec.rung != null ? String(rec.rung).trim() : '';
  if (preRung) return preRung;

  // racing: `rungN[_mM]` → "rung N".
  let m = /^rung(\d+)/.exec(mid);
  if (m) return 'rung ' + m[1];

  // swiss: `roundN_mM` / `swiss_rN_mM` / `rN_mM` → "round N · match M".
  m = /(?:^|_)(?:round[_-]?|r)(\d+)_m(\d+)/i.exec(mid);
  if (m) return `round ${m[1]} · match ${m[2]}`;

  // gauntlet / single- or double-elim: `roundN` / `gN` → "round N".
  m = /^(?:round[_-]?|g)(\d+)/i.exec(mid);
  if (m) return 'round ' + m[1];

  // unknown shape: surface the raw match_id if we have one, else nothing.
  return mid || null;
}

function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}

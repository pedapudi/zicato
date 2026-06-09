// variants/T/views/candidate.js — CANDIDATE (one generation), comparison-first.
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

import { el, svgEl } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { attachHovercard } from '../hovercard.js';
import { lifecycleDag, rungProgression } from '../dag.js';
import { gatedSwap, section, subhead, empty, stat, verdictPill, normaliseDecision, decisionFor, densityTokens, prText, metricsDigest } from '../ui.js';
import { comparePicker, splitFrame } from '../compare.js';
import { candidateProgression, inflightForActiveEpoch, inflightForEntryGen, runProgressRatio, liveMatchupsForCandidate, liveBelongsToEpoch, resolveNonGauntletSt, racingModel, structureDigest } from './structure.js';
import { epochRoundModel, reignModel } from './rounds.js';
import { deriveLiveStatus } from '../livestatus.js';
import { harmonografIsLive, harmonografLink, harmonografMini } from '../../../core/harmonograf.js';

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
  const [rows, traj, bracket] = await Promise.all([
    D.generationsForEpoch(epochId), D.scoreTrajectory(epochId), D.bracket(epochId),
  ]);
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const genList = rows.length
    ? rows.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: g.promoted == null ? null : !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' ? true : (normaliseDecision(x.outcome) === 'rejected' ? false : null) }));
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

  const champ = genList.find((g) => g.promoted) || genList.find((g) => !g.parent) || null;
  const championId = champ ? champ.id : null;
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
  // became champion. Derived from the SAME epoch round model the timeline reads.
  const reigns = reignModel(epochRoundModel({
    gens: genList.map((g) => ({ id: g.id, parent: g.parent, promoted: g.promoted })),
    scalarBy: scalarByGen, bracket, structure, championId,
  }));

  // the live PROJECTED standing map ({gen: {scalar, boards_done, boards_total}})
  // from the current-epoch active tournament — so a candidate with NO settled
  // scalar yet shows its climbing PROJECTED scalar / Δ (marked "projected").
  const liveProjected = (at && liveForThisEpoch && at.projected && typeof at.projected === 'object') ? at.projected : {};

  // THE RACING FIELD MODEL — resolved through the SHARED resolveNonGauntletSt
  // (live-first → reconstructRacing → recorded) so the candidate dossier's
  // field-relative racing panels read the SAME `st` the Match-ups / epoch /
  // per-round views do. The old path called reconstructRacing directly (settled
  // only), so a LIVE racing run viewed from the dossier missed the in-flight
  // rungs the other views showed — a live-only divergence. `at` is already in
  // memory (state.activeTournament), so the resolver runs synchronously here.
  const racingSt = (String(structure) === 'racing')
    ? resolveNonGauntletSt({
        structure: 'racing', bracket, epochId,
        liveRaw: liveForThisEpoch ? at : null,
        heartbeat: state.heartbeat, activeRuns: state.activeRuns,
        params: (tournament && tournament.params) || {},
      }).st
    : null;

  // Resolve each side's full panel data (cached). Side B only when comparing.
  // The primary side (A) honours the entry drill-down param; the compare side
  // (B) reads its lifecycle clean.
  const sideA = await resolveCandidate(epochId, genId, genList, experiments, scalarByGen, championId, championScalar, allMatchups, params.entry || null, bracket, epochInflight, liveProjected);
  const sideB = cmpId ? await resolveCandidate(epochId, cmpId, genList, experiments, scalarByGen, championId, championScalar, allMatchups, null, bracket, epochInflight, liveProjected) : null;

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
async function resolveCandidate(epochId, genId, genList, experiments, scalarByGen, championId, championScalar, allMatchups, entryParam, bracket, epochInflight, liveProjected) {
  const node = genList.find((g) => g.id === genId) || { id: genId, parent: null, promoted: null };
  const baseline = !node.parent;
  const exp = experiments.find((x) => x.generation_id === genId) || null;
  // Class B: an unscored candidate (promoted == null, no resolved outcome) is
  // PENDING, never "rejected/dead branch".
  const decision = decisionFor({ promoted: node.promoted, parent: node.parent, exp });
  const mpts = exp && exp.hypothesis && Array.isArray(exp.hypothesis.mutation_points) ? exp.hypothesis.mutation_points.length
    : (exp && Array.isArray(exp.mutation_points) ? exp.mutation_points.length : null);

  const pe = await D.perEntry(epochId, genId);
  const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];
  // per-generation mean continuous outcome (#18); null on the pre-score path.
  const meanScore = pe && svg.isNum(pe.mean_score) ? pe.mean_score : null;

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
  // racing-final, each Δ + won/cut) — relates board runs to the rounds even
  // when the per-run records carry no rung tags. null for a gauntlet candidate.
  const progression = candidateProgression(bracket, genId);

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
  const gates = await Promise.all(gateSpecs.map((k) => D.gate(epochId, k.champ, k.chall)));
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
  });

  // the train→holdout GENERALIZATION triplet for THIS candidate — read off its
  // own experiment outcome record (issue #5: train_loss / holdout_loss /
  // generalization_gap, absent until the detector lands, so every read is
  // type-guarded). Rendered as a SMALL, width-capped supporting panel (the study
  // shrank it from a hero figure), never a crash when the triplet is absent.
  const generalization = buildGeneralizationModel(exp);

  return {
    node, baseline, decision, mpts, entries, meanScore, mine, gateSpecs, gates,
    primaryDelta, championId, championScalar, scalarByGen, progression,
    championLoss, championSigma, candidateSigma, deltaSigma, gateExplain,
    entryParam, exps, judges, drillRow, drillHeader, inflight, cached, cachedProvenance,
    projected, radar, generalization,
  };
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
function buildRadarModel({ primaryGate, championScalar, settledScalar, projected, entries }) {
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

  // (2) pass-rate axis — higher = better, so it maps directly (no inverse). The
  // candidate's pass-rate is read off its own per-board pass_fail; the champion's
  // is recovered from the gate's delta_pass_rate (delta = challenger − champion).
  const passable = (Array.isArray(entries) ? entries : []).filter((e) => e && (e.pass_fail === 0 || e.pass_fail === 1 || e.pass_fail === true || e.pass_fail === false));
  if (passable.length) {
    const candRate = passable.filter((e) => e.pass_fail === 1 || e.pass_fail === true).length / passable.length;
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
      lossAxis(k, sc.champion[k], sc.challenger[k], 'drift');
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
  const decision = normaliseDecision(gate) || 'pending';
  // the deciding rule: the first that fired, else the first failed, else the
  // first non-pass — rules short-circuit in declared order.
  const deciding = rules.find((r) => r && r.fired)
    || rules.find((r) => r && String(r.status) === 'fail')
    || rules.find((r) => r && String(r.status) !== 'pass' && String(r.status) !== 'not_reached')
    || null;
  const deltaScalar = svg.isNum(gate.delta_scalar) ? gate.delta_scalar : null;
  let margin = null, regressed = null;
  if (deciding && deciding.detail) {
    const det = String(deciding.detail);
    // scalar-margin detail carries "needs ≤ -0.01" (the promote margin).
    const mm = /needs\s*[≤<]=?\s*(-?\d+(?:\.\d+)?)/i.exec(det);
    if (mm) margin = parseFloat(mm[1]);
    // a monotonicity detail names the regressed predicate / namespace, e.g.
    // "regressed `no_fabricated_numbers`" or "namespace `agent.tools` regressed".
    const rb = /`([^`]+)`/.exec(det);
    if (rb) regressed = rb[1];
  }
  // the gate's own primary_driver (a judge name) is the fallback regressed
  // identifier when a monotonicity rule named no predicate in its detail.
  if (!regressed && gate.primary_driver && gate.primary_driver.judge) regressed = gate.primary_driver.judge;
  return {
    decision,
    decidingRule: deciding ? (deciding.id || null) : null,
    decidingLabel: deciding ? (deciding.label || deciding.id || null) : null,
    // The deciding rule's raw detail string, scope-agnostic — the gate
    // tooltip prefers this over hard-coded per-entry wording so an
    // aggregate-scope pass-rate detail renders verbatim.
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
    // entries fold the continuous score + its precision/recall metrics (#18)
    // so a scored board repaints when its score/metrics move, but stays
    // byte-identical on a no-op heartbeat. A bool-only entry contributes
    // null for both (back-compat: unchanged digest vs the pre-score path).
    entries: s.entries.map((e) => [e.entry_id, svg.isNum(e.drift_loss) ? e.drift_loss.toFixed(3) : null, e.pass_fail, !!e.wall_clock_budget_exceeded, e.rung || null, e.match_id || null, !!e.cached, svg.isNum(e.score) ? e.score.toFixed(3) : null, metricsDigest(e.metrics)]),
    // per-generation mean continuous outcome (#18); null on the pre-score path.
    meanScore: svg.isNum(s.meanScore) ? s.meanScore.toFixed(3) : null,
    cached: s.cached ? [s.cachedProvenance && s.cachedProvenance.sourceEpoch, s.cachedProvenance && s.cachedProvenance.sourceRun] : null,
    progression: s.progression && Array.isArray(s.progression.stages)
      ? s.progression.stages.map((st) => [st.label, st.kind, svg.isNum(st.delta) ? st.delta.toFixed(2) : null, st.verdict]) : null,
    matchups: s.mine.map((m) => [m.champion, m.challenger, m.decision, svg.isNum(m.delta_scalar) ? m.delta_scalar.toFixed(2) : null]),
    gates: s.gates.map((g, i) => g && Array.isArray(g.rules)
      ? [s.gateSpecs[i].champ, s.gateSpecs[i].chall, s.gateSpecs[i].role, g.decision, svg.isNum(g.delta_scalar) ? g.delta_scalar.toFixed(3) : null, g.rules.map((r) => [r.id, r.status, r.fired]),
        // scalar-provenance decomposition (#19) folded in so a change to which
        // transform/plugin shaped a side — or a fail-open event firing —
        // repaints the gate, but a no-op heartbeat stays byte-identical. null
        // (built-in / pre-#19) contributes nothing new (back-compat digest).
        decompDigest(g.scalar_decomposition)]
      : null),
    drill: s.entryParam || null,
    drillExp: s.exps && Array.isArray(s.exps.outcomes) ? s.exps.outcomes.map((o) => [o.kind, o.passed, o.judge_name, o.detail]) : null,
    drillJudge: s.judges && Array.isArray(s.judges.judges) ? s.judges.judges.map((j) => [j.judge_name, j.weighted_loss]) : null,
    // harmonograf deep-link state — folded in so the link appears/disappears
    // when liveness flips (server up ⇄ run ended) without a no-op-beat repaint.
    hgLive: harmonografIsLive(),
    hgSession: (s.drillHeader && s.drillHeader.adk_session_id) || null,
    // LIVE in-flight board runs for this candidate — folded into the digest so a
    // beat that advances progress repaints, but a no-op heartbeat stays equal.
    inflight: Array.isArray(s.inflight) ? s.inflight.map((r) => {
      const pr = runProgressRatio(r);
      return [r.entry_id != null ? r.entry_id : (r.board_entry_id != null ? r.board_entry_id : r.entry || null),
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
  host.appendChild(el('div', { class: 'dn-panel dn-row' }, [
    scalarStat,
    deltaStat,
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
    dagCard.appendChild(el('div', { class: 'dn-rungprog-strip' }, [
      el('span', { class: 'dn-rungprog-cap dn-faint', text: 'tournament path' }),
      rungProgression({ stages: s.progression.stages, width: narrow ? 480 : 720 }),
    ]));
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
    const tbl = el('table', { class: 'dn-board-table dn-inflight-table' });
    tbl.appendChild(el('thead', null, [el('tr', null, [
      el('th', { text: 'board' }), el('th', { text: 'run' }), el('th', { text: 'progress' }), el('th', { text: 'execution' }),
    ])]));
    const tbody = el('tbody');
    for (const r of inflight) {
      const eid = r.entry_id != null ? r.entry_id : (r.board_entry_id != null ? r.board_entry_id : (r.entry != null ? r.entry : '—'));
      const pr = runProgressRatio(r);
      const pct = pr != null ? Math.round(pr * 100) : null;
      // a per-run harmonograf "execution ▸" link — liveness-gated (these are
      // in-flight runs, so the auto-launched server is up) and stop-propagated
      // so the cell click does not also navigate the row. Renders nothing when
      // not live / no harmonograf url (harmonografMini returns null).
      const exec = harmonografMini(r, 'execution', 'open this run’s harmonograf trace');
      if (exec) exec.addEventListener('click', (ev) => ev.stopPropagation());
      const row = el('tr', { class: 'dn-inflight-row' }, [
        el('td', { class: 'dn-mono', text: String(eid) }),
        el('td', { class: 'dn-mono dn-faint', text: r.run_id ? String(r.run_id) : 'pending' }),
        el('td', null, [
          el('span', { class: 'dn-progress' }, [
            el('span', { class: 'dn-progress-fill', style: 'width:' + (pct != null ? pct : 6) + '%' + (pct == null ? ';opacity:0.4' : '') }),
          ]),
          el('span', { class: 'dn-mono dn-faint dn-progress-pct', text: pct != null ? ' ' + pct + '%' : ' running…' }),
        ]),
        el('td', null, [exec || el('span', { class: 'dn-faint', text: '—' })]),
      ]);
      if (eid !== '—') { row.style.cursor = 'pointer'; row.addEventListener('click', () => ctx.navigate('board', { epochId, entry: eid, gen: genId })); }
      tbody.appendChild(row);
    }
    tbl.appendChild(tbody);
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
      gateSections.push(section(`Promote gate · ${k.champ} → ${k.chall} (${k.role})`, gatePanel(g)));
    });
  } else if (!baseline) {
    gateSections.push(section('Promote gate', el('div', { class: 'dn-panel' }, [empty('No gate decomposition recorded for this candidate’s round.')])));
  } else {
    gateSections.push(section('Promote gate', el('div', { class: 'dn-panel' }, [empty('The seed candidate has no gate — it defines the loss floor that challengers must beat.')])));
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
function generalizationPanel(g) {
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
    fig.appendChild(txt(xH + 10, Y(holdout) + 3.5, 'dn-gen-val ' + tone, 'start', svg.fmt(holdout, 3)));
    card.appendChild(fig);
  } else if (gap != null) {
    card.appendChild(el('div', { class: 'dn-gen-gaponly ' + tone, text: `generalization gap ${svg.fmtSigned(gap, 3)}` + (tol != null ? ` (tol ${svg.fmt(tol, 2)})` : '') }));
  }
  card.appendChild(el('p', { class: 'dn-faint dn-gen-cap', text: within === false
    ? 'holdout gap exceeds tolerance — possible memorization'
    : 'small gap — generalizes (no memorization)' }));
  return card;
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
  const deltaW = 40;       // the Δ value column, just left of the glyph
  // CONTINUOUS-SCORE column (#18): a 0→1 mini-bar + score readout, only
  // reserved when AT LEAST ONE row carries a score; a wholly bool-only
  // dumbbell keeps the pre-score geometry (zero-width score column) so its
  // layout is byte-identical to today.
  const anyScored = rows.some((r) => r && svg.isNum(r.score));
  const scoreW = anyScored ? 84 : 0;
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
      const sbx = x1 + 8;                 // bar left, just past the value axis
      const sbw = scoreW - 14;            // bar width within the score column
      const sf = Math.max(0, Math.min(1, r.score));
      const barY = cy - 3;
      g.appendChild(svgEl('rect', { x: sbx, y: barY, width: sbw, height: 6, rx: 2, class: 'dn-score-track' }));
      g.appendChild(svgEl('rect', { x: sbx, y: barY, width: Math.max(1, sbw * sf), height: 6, rx: 2, class: 'dn-score-fill ' + dirCls }));
      const sv = svgEl('text', { x: sbx + sbw + 2, y: cy + 3, class: 'dn-score-val', 'text-anchor': 'start' });
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
  const stbl = el('table', { class: 'dn-board-table dn-racing-standings' });
  stbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', { text: '#' }), el('th', { text: 'racer' }), el('th', { class: 'dn-num', text: 'scalar' }), el('th', { text: 'status' }),
  ])]));
  const stbody = el('tbody');
  list.forEach((f, i) => {
    const isCand = String(f.id) === String(genId);
    const isChamp = String(f.id) === String(championId);
    const row = el('tr', { class: isCand ? 'dn-racing-cand-row' : '' }, [
      el('td', { class: 'dn-mono dn-faint', text: String(i + 1) }),
      el('td', null, [el('span', { class: 'dn-mono' + (isCand ? ' dn-racing-cand' : ''), text: f.id + (isChamp ? ' ♛' : '') })]),
      el('td', { class: 'dn-num dn-mono', text: svg.isNum(f.scalar) ? svg.fmt(f.scalar, 1) : '—' }),
      el('td', null, [el('span', { class: 'dn-pill dn-' + (f.survived ? 'promoted' : 'rejected'), text: f.survived ? 'racing' : ('✂ rung ' + f.cut_rung) })]),
    ]);
    if (!isCand) { row.style.cursor = 'pointer'; row.addEventListener('click', () => ctx.navigate('candidate', { epochId, gen: f.id }, opts)); }
    stbody.appendChild(row);
  });
  stbl.appendChild(stbody);
  standCard.appendChild(stbl);
  standCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'lower scalar = better · ✂ = the rung a racer was cut at · click a racer → its dossier' }));
  wrap.appendChild(section('Field standings · candidate vs the whole field', standCard));

  // (2) RUNG LADDER — entered / cut / survived per rung; the candidate's rank
  // among the survivors at each rung it reached (the field-narrowing story).
  const ladderCard = el('div', { class: 'dn-panel' });
  const ltbl = el('table', { class: 'dn-board-table dn-racing-ladder' });
  ltbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', { text: 'rung' }), el('th', { class: 'dn-num', text: 'entered' }), el('th', { class: 'dn-num', text: 'cut' }),
    el('th', { class: 'dn-num', text: 'survived' }), el('th', { text: 'candidate' }),
  ])]));
  const lbody = el('tbody');
  model.rungs.forEach((r, ri) => {
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
    lbody.appendChild(el('tr', null, [
      el('td', { class: 'dn-mono', text: r.label || ('Rung ' + ri) }),
      el('td', { class: 'dn-num dn-mono', text: String(entered) }),
      el('td', { class: 'dn-num dn-mono ' + (cutN ? 'dn-bad-t' : ''), text: cutN ? ('✂ ' + cutN) : '0' }),
      el('td', { class: 'dn-num dn-mono dn-good-t', text: String(survived) }),
      el('td', { class: 'dn-mono', text: candRungRank ? ('#' + candRungRank + ' of ' + entered) : '—' }),
    ]));
  });
  ltbl.appendChild(lbody);
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
  const tbl = el('table', { class: 'dn-board-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', { text: 'round' }), el('th', { text: 'role' }), el('th', { text: 'decision' }),
    el('th', { class: 'dn-num', text: 'Δ scalar' }), el('th', { text: 'hypothesis' }),
  ])]));
  const tbody = el('tbody');
  for (const m of mine) {
    const asChamp = m.champion === genId;
    // Class B: a match-up with no recorded decision is still racing — PENDING,
    // not a default "rejected". `normaliseDecision` reads the matchup's own
    // decision field; absent ⇒ pending.
    const dec = normaliseDecision(m) || 'pending';
    const other = asChamp ? m.challenger : m.champion;
    const tr = el('tr', null, [
      el('td', null, [el('span', { class: 'dn-mono', text: `${m.champion} → ${m.challenger}` })]),
      el('td', null, [el('span', { class: 'dn-pill dn-' + (asChamp ? 'promoted' : 'rejected'), text: asChamp ? 'champion' : 'challenger' })]),
      el('td', null, [el('span', { class: 'dn-pill dn-' + dec, text: dec })]),
      el('td', { class: 'dn-num dn-mono ' + (m.delta_scalar > 0 ? 'dn-bad-t' : m.delta_scalar < 0 ? 'dn-good-t' : ''), text: svg.isNum(m.delta_scalar) ? svg.fmtSigned(m.delta_scalar, 2) : '—' }),
      el('td', { class: 'dn-faint', text: m.hypothesis_core_idea ? clip(m.hypothesis_core_idea, 64) : '—' }),
    ]);
    // clicking a match-up row compares the two candidates side by side (S).
    tr.style.cursor = 'pointer';
    tr.addEventListener('click', () => ctx.navigate('candidate', { epochId, gen: genId }, { cmp: other }));
    tbody.appendChild(tr);
  }
  tbl.appendChild(tbody);
  card.appendChild(tbl);
  card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:10px 0 0;', text: genId === championId
    ? `as champion, ${genId} defended ${mine.length} round${mine.length === 1 ? '' : 's'} · click a round → compare the two candidates side by side`
    : `${mine.length} round${mine.length === 1 ? '' : 's'} · click a round → compare the two candidates side by side` }));
  return card;
}

// fix #1 — the stacked, non-overlapping gate panel:
// (a) decision header, (b) the rules ladder (each rule its own row).
// The old (c) champion-vs-challenger SCALAR-COMPONENTS comparison block was
// REMOVED — the FINAL liked study (single-generation.html opt 2) dropped it as
// redundant with the RADAR SILHOUETTE (which now compares candidate vs champion
// across the same scalar / pass-rate / per-judge axes). The deciding-rule detail
// the components used to carry now reads off the gate-rule ladder + the radar.
function gatePanel(gate) {
  const card = el('div', { class: 'dn-panel dn-gate' });
  // Class B: a gate with no resolved decision is still pending, not rejected.
  const decision = normaliseDecision(gate) || 'pending';
  card.appendChild(el('div', { class: 'dn-gate-head' }, [
    el('div', { class: 'dn-gate-decision' }, [verdictPill(decision)]),
    el('div', { class: 'dn-row dn-gate-deltas' }, [
      svg.isNum(gate.delta_scalar) ? stat(svg.fmtSigned(gate.delta_scalar, 2), 'Δ scalar (loss)') : null,
      svg.isNum(gate.delta_pass_rate) ? stat(svg.fmtSigned(gate.delta_pass_rate, 2), 'Δ pass rate') : null,
      gate.primary_driver && gate.primary_driver.judge ? stat(gate.primary_driver.judge, 'primary driver') : null,
    ].filter(Boolean)),
  ].filter(Boolean)));
  if (gate.reason) card.appendChild(el('p', { class: 'dn-gate-reason', text: gate.reason }));

  const rules = Array.isArray(gate.rules) ? gate.rules : [];
  if (rules.length) {
    card.appendChild(subhead('Rules · short-circuiting, in order'));
    const ladder = el('ol', { class: 'dn-rules' });
    for (const r of rules) {
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

  return card;
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

  const jrows = (judges && Array.isArray(judges.judges)) ? judges.judges : [];
  const jitems = jrows.filter((j) => svg.isNum(j.weighted_loss)).sort((a, b) => b.weighted_loss - a.weighted_loss).map((j) => ({ label: j.judge_name, value: j.weighted_loss }));
  if (jitems.length) {
    card.appendChild(el('p', { class: 'dn-faint', style: 'margin:14px 0 4px;font-size:11px;', text: 'per-judge weighted process-drift loss · higher = more drift' }));
    const djt = densityTokens();
    card.appendChild(svg.valueBars({ width: 420, rowHeight: Math.round(20 * djt.sizeScale), labelWidth: 180, items: jitems }));
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

function clip(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}

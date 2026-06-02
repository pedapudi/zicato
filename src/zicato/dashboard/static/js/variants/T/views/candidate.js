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

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { lifecycleDag, rungProgression } from '../dag.js';
import { gatedSwap, section, subhead, empty, stat, verdictPill, normaliseDecision, decisionFor, densityTokens } from '../ui.js';
import { comparePicker, splitFrame } from '../compare.js';
import { candidateProgression } from './structure.js';

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
  const allMatchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups : [];

  // Resolve each side's full panel data (cached). Side B only when comparing.
  // The primary side (A) honours the entry drill-down param; the compare side
  // (B) reads its lifecycle clean.
  const sideA = await resolveCandidate(epochId, genId, genList, experiments, scalarByGen, championId, championScalar, allMatchups, params.entry || null, bracket);
  const sideB = cmpId ? await resolveCandidate(epochId, cmpId, genList, experiments, scalarByGen, championId, championScalar, allMatchups, null, bracket) : null;

  const digest = JSON.stringify({
    epochId, genId, cmpId, entry: params.entry || null,
    a: candidateDigest(sideA), b: sideB ? candidateDigest(sideB) : null,
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
      a: { title: genId + (sideA.node.promoted ? ' ♛' : ''), sub: sideA.decision, build: (h) => paintCandidate(h, ctx, epochId, sideA, cmpId, true) },
      b: cmpId ? { title: cmpId + (sideB.node.promoted ? ' ♛' : ''), sub: sideB.decision, build: (h) => paintCandidate(h, ctx, epochId, sideB, null, false) } : null,
      emptyTitle: 'no comparison',
      emptyPrompt: 'Choose a candidate above to compare its lifecycle, gate, match-ups and per-board scoring against ' + genId + '.',
    }));
    return nodes;
  });
}

// Resolve one candidate's full panel data (all cached reads). `entryParam`
// only applies to the primary (A) side's drill-down.
async function resolveCandidate(epochId, genId, genList, experiments, scalarByGen, championId, championScalar, allMatchups, entryParam, bracket) {
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

  let exps = null, judges = null, drillRow = null;
  if (entryParam) {
    [exps, judges] = await Promise.all([
      D.expectations(epochId, genId, entryParam),
      D.perJudgeForRun(epochId, genId, entryParam),
    ]);
    drillRow = entries.find((e) => e.entry_id === entryParam) || null;
  }

  return {
    node, baseline, decision, mpts, entries, mine, gateSpecs, gates,
    primaryDelta, championId, championScalar, scalarByGen, progression,
    championLoss, championSigma, candidateSigma, deltaSigma, gateExplain,
    entryParam, exps, judges, drillRow,
  };
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
    deltaScalar, margin, regressed,
    reason: gate.reason || null,
  };
}

function candidateDigest(s) {
  return {
    gen: s.node.id, parent: s.node.parent, decision: s.decision, championId: s.championId,
    champScalar: svg.isNum(s.championScalar) ? s.championScalar.toFixed(3) : null,
    delta: svg.isNum(s.primaryDelta) ? s.primaryDelta.toFixed(3) : null,
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
    entries: s.entries.map((e) => [e.entry_id, svg.isNum(e.drift_loss) ? e.drift_loss.toFixed(3) : null, e.pass_fail, !!e.wall_clock_budget_exceeded, e.rung || null, e.match_id || null]),
    progression: s.progression && Array.isArray(s.progression.stages)
      ? s.progression.stages.map((st) => [st.label, st.kind, svg.isNum(st.delta) ? st.delta.toFixed(2) : null, st.verdict]) : null,
    matchups: s.mine.map((m) => [m.champion, m.challenger, m.decision, svg.isNum(m.delta_scalar) ? m.delta_scalar.toFixed(2) : null]),
    gates: s.gates.map((g, i) => g && Array.isArray(g.rules)
      ? [s.gateSpecs[i].champ, s.gateSpecs[i].chall, s.gateSpecs[i].role, g.decision, svg.isNum(g.delta_scalar) ? g.delta_scalar.toFixed(3) : null, g.rules.map((r) => [r.id, r.status, r.fired])]
      : null),
    drill: s.entryParam || null,
    drillExp: s.exps && Array.isArray(s.exps.outcomes) ? s.exps.outcomes.map((o) => [o.kind, o.passed, o.judge_name, o.detail]) : null,
    drillJudge: s.judges && Array.isArray(s.judges.judges) ? s.judges.judges.map((j) => [j.judge_name, j.weighted_loss]) : null,
  };
}

// Paint ONE candidate's full lifecycle panel into `host`. `cmpId`, when set, is
// the compare target to PRESERVE while drilling into a sub-node. `isPrimary`
// gates the entry drill-down (B reads its lifecycle clean).
function paintCandidate(host, ctx, epochId, s, cmpId, isPrimary) {
  const opts = cmpId ? { cmp: cmpId } : undefined;
  const node = s.node;
  const genId = node.id;
  const baseline = s.baseline;
  const championId = s.championId;
  const championScalar = s.championScalar;

  host.appendChild(el('div', { class: 'dn-panel dn-row' }, [
    stat(svg.isNum(s.scalarByGen.get(genId)) ? svg.fmt(s.scalarByGen.get(genId), 1) : '—', 'scalar (loss)'),
    stat(svg.isNum(s.primaryDelta) ? svg.fmtSigned(s.primaryDelta, 1) : '—', 'Δ vs champion'),
    stat(node.parent || 'seed', 'parent'),
    el('div', { class: 'dn-stat' }, [verdictPill(baseline ? 'baseline' : s.decision)]),
  ]));

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
      rungProgression({ stages: s.progression.stages, width: cmpId ? 480 : 720 }),
    ]));
  }

  dagCard.appendChild(lifecycleDag({
    genId, parentId: node.parent, baseline, promoted: node.promoted, decision: s.decision,
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
    // viewBox width, narrower in the compare split) is still supplied.
    width: cmpId ? 560 : 900,
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

  // ---- per-board scoring dot-plot ----
  // Each per-entry record carries the tournament context it ran in (match_id /
  // rung). The SAME board entry can appear several times — raced across rungs /
  // rounds — so we surface a short context tag per row to disambiguate the
  // duplicates, and route a click to that SPECIFIC run's board drill-down.
  const scoreCard = el('div', { class: 'dn-panel' });
  if (s.entries.length) {
    const items = s.entries
      .filter((e) => svg.isNum(e.drift_loss))
      .sort((a, b) => b.drift_loss - a.drift_loss)
      .map((e) => ({
        label: e.entry_id, value: e.drift_loss, id: e.entry_id,
        pass: e.pass_fail, timeout: !!e.wall_clock_budget_exceeded,
        context: tournamentContext(e),
        entry_id: e.entry_id, run_id: e.run_id || null, gen: genId,
      }));
    scoreCard.appendChild(svg.valueDotPlot({
      width: cmpId ? 480 : 560, rowHeight: dt.dotRow, labelWidth: cmpId ? 160 : 200, items,
      reference: svg.isNum(championScalar) ? { value: championScalar, label: `champion ${championId}` } : null,
      // click a row (board name AND dot) → the board drill-down for THIS exact
      // run: the board view opens its inline transcript for the selected gen.
      onClick: (it) => ctx.navigate('board', { epochId, entry: it.entry_id || it.id, gen: it.gen || genId }),
    }));
    scoreCard.appendChild(el('div', { class: 'dn-legend' }, [
      svg.isNum(championScalar) ? el('span', null, [el('i', { class: 'spine', style: 'border-color:var(--v2-ink-faint);border-top-style:dashed;' }), `champion ${championId} = ${svg.fmt(championScalar, 1)}`]) : null,
      el('span', null, [el('i', { class: 'dotact' }), 'pass']),
      el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'fail']),
      el('span', { class: 'dn-faint', text: '⏱ timeout · dim tag = rung/round it ran in · click an entry → its drill-down' }),
    ].filter(Boolean)));
  } else {
    scoreCard.appendChild(empty('No per-entry scores for this candidate (the index may not be built).'));
  }
  host.appendChild(section('Per-board scoring · sorted, vs champion', scoreCard));

  if (isPrimary && s.entryParam) host.appendChild(entryDrilldown(ctx, epochId, genId, s.entryParam, s.drillRow, s.exps, s.judges));

  // ---- fix #3: ALL match-ups for this candidate ----
  host.appendChild(section('Match-ups · every round this candidate was in', allMatchupsPanel(s.mine, genId, championId, ctx, epochId)));

  // ---- fix #1: the STACKED promote gate(s) on the candidate page ----
  if (s.gates.some((g) => g && Array.isArray(g.rules))) {
    s.gateSpecs.forEach((k, i) => {
      const g = s.gates[i];
      if (!g || !Array.isArray(g.rules)) return;
      host.appendChild(section(`Promote gate · ${k.champ} → ${k.chall} (${k.role})`, gatePanel(g, k.champ, k.chall)));
    });
  } else if (!baseline) {
    host.appendChild(section('Promote gate', el('div', { class: 'dn-panel' }, [empty('No gate decomposition recorded for this candidate’s round.')])));
  } else {
    host.appendChild(section('Promote gate', el('div', { class: 'dn-panel' }, [empty('The seed candidate has no gate — it defines the loss floor that challengers must beat.')])));
  }
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
// (a) decision header, (b) the rules ladder (each rule its own row),
// (c) a SEPARATE champion-vs-challenger scalar-components comparison block.
function gatePanel(gate, champion, challenger) {
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

  const sc = gate.scalar_components;
  if (sc && sc.champion && sc.challenger) {
    const keys = [...new Set([...Object.keys(sc.champion), ...Object.keys(sc.challenger)])].sort();
    if (keys.length) {
      card.appendChild(subhead(`Scalar components · champion ${champion} vs challenger ${challenger}`));
      const tbl = el('table', { class: 'dn-sc-table' });
      tbl.appendChild(el('thead', null, [el('tr', null, [
        el('th', { text: 'component' }), el('th', { class: 'dn-num', text: champion }),
        el('th', { class: 'dn-num', text: challenger }), el('th', { class: 'dn-num', text: 'Δ' }),
      ])]));
      const tbody = el('tbody');
      for (const k of keys) {
        const a = svg.isNum(sc.champion[k]) ? sc.champion[k] : 0;
        const b = svg.isNum(sc.challenger[k]) ? sc.challenger[k] : 0;
        const d = b - a;
        const dCls = d > 0 ? 'dn-bad-t' : d < 0 ? 'dn-good-t' : '';
        tbody.appendChild(el('tr', null, [
          el('td', { class: 'dn-mono', text: k }),
          el('td', { class: 'dn-num dn-mono', text: svg.fmt(a, 2) }),
          el('td', { class: 'dn-num dn-mono', text: svg.fmt(b, 2) }),
          el('td', { class: 'dn-num dn-mono ' + dCls, text: svg.fmtSigned(d, 2) }),
        ]));
      }
      tbl.appendChild(tbody);
      card.appendChild(tbl);
    }
  }
  return card;
}

function entryDrilldown(ctx, epochId, genId, entryId, row, exps, judges) {
  const runId = row ? row.run_id : null;
  const card = el('div', { class: 'dn-panel dn-drill' });
  card.appendChild(el('div', { class: 'dn-row' }, [
    stat(row && svg.isNum(row.drift_loss) ? svg.fmt(row.drift_loss, 1) : '—', 'drift loss'),
    stat(row ? passLabel(row.pass_fail) : '—', 'predicate'),
    stat(row && row.wall_clock_budget_exceeded ? 'timed out' : (row && svg.isNum(row.runtime_ms) ? `${(row.runtime_ms / 1000).toFixed(0)}s` : '—'), 'runtime'),
  ]));

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
  card.appendChild(el('div', { style: 'margin-top:14px;' }, [
    el('a', { class: 'dn-linkbtn', href: ctx.href('board', { epochId, entry: entryId, gen: genId }), text: 'Open the transcript inline (vs champion) →' }),
    runId ? el('span', { class: 'dn-faint dn-mono', style: 'margin-left:8px;', text: runId.slice(0, 10) + '…' }) : null,
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

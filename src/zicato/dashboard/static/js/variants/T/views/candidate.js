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
import { lifecycleDag } from '../dag.js';
import { gatedSwap, section, subhead, empty, stat, verdictPill, normaliseDecision, densityTokens } from '../ui.js';
import { comparePicker, splitFrame } from '../compare.js';

export async function render(host, ctx, params, route) {
  params = params || {};
  // the compare target rides on the route (4th arg) or, for tests, on params.
  const cmpTarget = (route && route.cmp) || params.cmp || null;
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading candidate…' }));

  const [ep, lin, traj, bracket] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory(), D.bracket()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Candidate' }), empty('No current epoch.')]);
    return;
  }
  const epochId = ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const genList = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' }));
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
  const sideA = await resolveCandidate(epochId, genId, genList, experiments, scalarByGen, championId, championScalar, allMatchups, params.entry || null);
  const sideB = cmpId ? await resolveCandidate(epochId, cmpId, genList, experiments, scalarByGen, championId, championScalar, allMatchups, null) : null;

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
async function resolveCandidate(epochId, genId, genList, experiments, scalarByGen, championId, championScalar, allMatchups, entryParam) {
  const node = genList.find((g) => g.id === genId) || { id: genId, parent: null, promoted: false };
  const baseline = !node.parent;
  const exp = experiments.find((x) => x.generation_id === genId) || null;
  const decision = baseline ? 'baseline' : (node.promoted ? 'promoted' : (exp ? normaliseDecision(exp.outcome) || 'rejected' : 'rejected'));
  const mpts = exp && exp.hypothesis && Array.isArray(exp.hypothesis.mutation_points) ? exp.hypothesis.mutation_points.length
    : (exp && Array.isArray(exp.mutation_points) ? exp.mutation_points.length : null);

  const pe = await D.perEntry(epochId, genId);
  const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];

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
    primaryDelta, championId, championScalar, scalarByGen,
    entryParam, exps, judges, drillRow,
  };
}

function candidateDigest(s) {
  return {
    gen: s.node.id, parent: s.node.parent, decision: s.decision, championId: s.championId,
    champScalar: svg.isNum(s.championScalar) ? s.championScalar.toFixed(3) : null,
    delta: svg.isNum(s.primaryDelta) ? s.primaryDelta.toFixed(3) : null,
    mpts: s.mpts,
    entries: s.entries.map((e) => [e.entry_id, svg.isNum(e.drift_loss) ? e.drift_loss.toFixed(3) : null, e.pass_fail, !!e.wall_clock_budget_exceeded]),
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
  dagCard.appendChild(lifecycleDag({
    genId, parentId: node.parent, baseline, promoted: node.promoted, decision: s.decision,
    deltaScalar: s.primaryDelta, patchPoints: s.mpts, entries: s.entries,
    width: cmpId ? 560 : 900, height: Math.max(Math.round(300 * dt.sizeScale), Math.round(120 * dt.sizeScale) + s.entries.length * dt.dagRowStep),
    onEntry: (eid) => ctx.navigate('candidate', { epochId, gen: genId, entry: eid }, opts),
    onPatch: baseline ? null : () => ctx.navigate('diff', { epochId, gen: genId }),
  }));
  dagCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: baseline ? 'parent → patch → board (one node per entry, colour = pass/fail/timeout) → Σ → gate → terminal · click a board node → its drill-down' : 'parent → patch → board → Σ → gate → terminal · click the PATCH node → this candidate’s side-by-side diff · click a board node → its drill-down' }));
  host.appendChild(section('Lifecycle · cause → effect → verdict', dagCard));

  // ---- per-board scoring dot-plot ----
  const scoreCard = el('div', { class: 'dn-panel' });
  if (s.entries.length) {
    const items = s.entries
      .filter((e) => svg.isNum(e.drift_loss))
      .sort((a, b) => b.drift_loss - a.drift_loss)
      .map((e) => ({ label: e.entry_id, value: e.drift_loss, id: e.entry_id, pass: e.pass_fail, timeout: !!e.wall_clock_budget_exceeded }));
    scoreCard.appendChild(svg.valueDotPlot({
      width: cmpId ? 480 : 560, rowHeight: dt.dotRow, labelWidth: cmpId ? 160 : 200, items,
      reference: svg.isNum(championScalar) ? { value: championScalar, label: `champion ${championId}` } : null,
      onClick: (it) => ctx.navigate('candidate', { epochId, gen: genId, entry: it.id }, opts),
    }));
    scoreCard.appendChild(el('div', { class: 'dn-legend' }, [
      svg.isNum(championScalar) ? el('span', null, [el('i', { class: 'spine', style: 'border-color:var(--v2-ink-faint);border-top-style:dashed;' }), `champion ${championId} = ${svg.fmt(championScalar, 1)}`]) : null,
      el('span', null, [el('i', { class: 'dotact' }), 'pass']),
      el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'fail']),
      el('span', { class: 'dn-faint', text: '⏱ timeout · click an entry → its drill-down' }),
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
    const dec = m.decision || 'rejected';
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
  const decision = normaliseDecision(gate) || gate.decision || 'rejected';
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

function clip(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}

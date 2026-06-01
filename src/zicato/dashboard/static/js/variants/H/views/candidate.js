// variants/E/views/candidate.js — CANDIDATE (one generation).
//
// Atlas's candidate screen pairs D's per-board dot-plot scoring (with
// drill-down) alongside a compact C-style lifecycle DAG for that candidate:
//
//   * the compact lifecycle DAG (dag.js) — parent → patch → board fan →
//     Σ → gate → terminal, the cause→effect→verdict flow at a glance;
//   * the per-board scoring dot-plot — each entry's ABSOLUTE drift loss,
//     sorted worst-first, with a reference line at the champion's scalar;
//     click an entry (or a board node in the DAG) to drill in.
//   * the entry drill-down (when an entry is selected via the URL) — its
//     expectation outcomes + per-judge weighted losses, and a link into
//     the full run transcript.
//
// The selected entry lives in the URL (the route param), so the drill-down
// rebuilds ONLY when the selection changes (a route change), never on a
// heartbeat — the gatedSwap digest carries the entry param.
//
// Data: /api/epoch, /api/lineage, /api/score-trajectory,
// /api/generation/{e}/{g}/per-entry, /api/round/{e}/{champ}/{chall}/gate,
// /api/run/{e}/{g}/{entry}/{expectations,per-judge}.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { lifecycleDag } from '../dag.js';
import { tufteSankey, buildCandidateFlow } from '../diagram/sankey.js';
import { gatedSwap, section, empty, stat, verdictPill, normaliseDecision } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'd-empty', text: 'Reading candidate…' }));
  const entryParam = params && params.entry;

  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'd-h1', text: 'Candidate' }), empty('No current epoch.')]);
    return;
  }
  const epochId = ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const genList = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' }));
  const allIds = genList.map((g) => g.id);
  const genId = (params && params.gen && allIds.includes(params.gen)) ? params.gen : (allIds[allIds.length - 1] || (params && params.gen) || null);
  const node = genList.find((g) => g.id === genId) || (genId ? { id: genId, parent: null, promoted: false } : null);

  if (!node) {
    gatedSwap(host, 'no-cand', () => [el('h1', { class: 'd-h1', text: 'Candidate' }), empty('No candidate selected.')]);
    return;
  }

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  // champion reference = the promoted generation's scalar.
  const champ = genList.find((g) => g.promoted) || genList.find((g) => !g.parent) || null;
  const championId = champ ? champ.id : null;
  const championScalar = championId ? scalarByGen.get(championId) : null;

  const exp = experiments.find((x) => x.generation_id === genId) || null;
  const baseline = !node.parent;
  const decision = baseline ? 'baseline' : (node.promoted ? 'promoted' : (exp ? normaliseDecision(exp.outcome) || 'rejected' : 'rejected'));
  const mpts = exp && exp.hypothesis && Array.isArray(exp.hypothesis.mutation_points) ? exp.hypothesis.mutation_points.length
    : (exp && Array.isArray(exp.mutation_points) ? exp.mutation_points.length : null);

  const pe = await D.perEntry(epochId, genId);
  const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];

  // Gate (for the DAG's delta + verdict), only for a non-baseline candidate.
  let gate = null;
  if (!baseline && node.parent) gate = await D.gate(epochId, node.parent, genId);
  const deltaScalar = gate && svg.isNum(gate.delta_scalar) ? gate.delta_scalar : null;

  // Entry drill-down data (only when an entry is selected).
  let exps = null, judges = null, drillRow = null;
  if (entryParam) {
    [exps, judges] = await Promise.all([
      D.expectations(epochId, genId, entryParam),
      D.perJudgeForRun(epochId, genId, entryParam),
    ]);
    drillRow = entries.find((e) => e.entry_id === entryParam) || null;
  }

  const digest = JSON.stringify({
    genId, parent: node.parent, decision, championId,
    champScalar: svg.isNum(championScalar) ? championScalar.toFixed(3) : null,
    delta: svg.isNum(deltaScalar) ? deltaScalar.toFixed(3) : null,
    mpts,
    entries: entries.map((e) => [e.entry_id, svg.isNum(e.drift_loss) ? e.drift_loss.toFixed(3) : null, e.pass_fail, !!e.wall_clock_budget_exceeded]),
    drill: entryParam || null,
    drillExp: exps && Array.isArray(exps.outcomes) ? exps.outcomes.map((o) => [o.kind, o.passed, o.judge_name, o.detail]) : null,
    drillJudge: judges && Array.isArray(judges.judges) ? judges.judges.map((j) => [j.judge_name, j.weighted_loss]) : null,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'e-pagehead' }, [
      el('h1', { class: 'd-h1' }, [(node.promoted ? '♛ ' : '') + 'Candidate ' + genId]),
      el('p', { class: 'd-lede', text: baseline ? 'The seed candidate (no parent) — it defines the loss floor for the epoch.' : `Born from ${node.parent} by a patch; faced the board; met the champion at the gate.` }),
    ]));

    // headline + verdict
    nodes.push(el('div', { class: 'd-panel d-row' }, [
      stat(svg.isNum(scalarByGen.get(genId)) ? svg.fmt(scalarByGen.get(genId), 1) : '—', 'scalar (loss)'),
      stat(svg.isNum(deltaScalar) ? svg.fmtSigned(deltaScalar, 1) : '—', 'Δ vs champion'),
      stat(node.parent || 'seed', 'parent'),
      el('div', { class: 'd-stat' }, [verdictPill(baseline ? 'baseline' : decision)]),
    ]));

    // ---- compact lifecycle DAG (fit-to-width, NO pan/zoom viewport) ----
    const dagCard = el('div', { class: 'd-panel d-fit' });
    dagCard.appendChild(lifecycleDag({
      genId, parentId: node.parent, baseline, promoted: node.promoted, decision,
      deltaScalar, patchPoints: mpts, entries,
      width: 900, height: Math.max(300, 120 + entries.length * 34),
      onEntry: (eid) => ctx.navigate('candidate', { gen: genId, entry: eid }),
    }));
    dagCard.appendChild(el('p', { class: 'd-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'parent → patch → board (one node per entry, colour = pass/fail/timeout) → Σ → gate → terminal · click a board node → its drill-down' }));
    nodes.push(section('Lifecycle · cause → effect → verdict', dagCard));

    // ---- Tufte causal-flow Sankey (candidate → per-board loss → Σ) ----
    const sankeyCard = el('div', { class: 'd-panel d-fit' });
    if (entries.length) {
      sankeyCard.appendChild(tufteSankey({
        width: 900, flow: buildCandidateFlow(genId, entries),
        onEntry: (eid) => ctx.navigate('candidate', { gen: genId, entry: eid }),
      }));
      sankeyCard.appendChild(el('div', { class: 'd-legend' }, [
        el('span', null, [el('i', { class: 'good' }), 'board entry passed']),
        el('span', null, [el('i', { class: 'bad' }), 'failed / timed out']),
        el('span', { class: 'd-faint', text: 'flow width ∝ that entry’s share of the aggregate loss · fit-to-width, no pan/zoom · click an entry → its drill-down' }),
      ]));
    } else {
      sankeyCard.appendChild(empty('No per-board loss flow yet (the index may not be built).'));
    }
    nodes.push(section('Causal flow · candidate → per-board loss → aggregate', sankeyCard));

    // ---- per-board scoring dot-plot ----
    const scoreCard = el('div', { class: 'd-panel' });
    if (entries.length) {
      const items = entries
        .filter((e) => svg.isNum(e.drift_loss))
        .sort((a, b) => b.drift_loss - a.drift_loss)
        .map((e) => ({ label: e.entry_id, value: e.drift_loss, id: e.entry_id, pass: e.pass_fail, timeout: !!e.wall_clock_budget_exceeded }));
      scoreCard.appendChild(svg.valueDotPlot({
        width: 560, rowHeight: 22, labelWidth: 200, items,
        reference: svg.isNum(championScalar) ? { value: championScalar, label: `champion ${championId}` } : null,
        onClick: (it) => ctx.navigate('candidate', { gen: genId, entry: it.id }),
      }));
      scoreCard.appendChild(el('div', { class: 'd-legend' }, [
        svg.isNum(championScalar) ? el('span', null, [el('i', { class: 'spine', style: 'border-color:var(--v2-ink-faint);border-top-style:dashed;' }), `champion ${championId} = ${svg.fmt(championScalar, 1)}`]) : null,
        el('span', null, [el('i', { class: 'dotact' }), 'pass']),
        el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'fail']),
        el('span', { class: 'd-faint', text: '⏱ timeout · click an entry → its drill-down' }),
      ].filter(Boolean)));
    } else {
      scoreCard.appendChild(empty('No per-entry scores for this candidate (the index may not be built).'));
    }
    nodes.push(section('Per-board scoring · sorted, vs champion', scoreCard));

    // ---- entry drill-down (selection in the URL) ----
    if (entryParam) nodes.push(entryDrilldown(ctx, genId, entryParam, drillRow, exps, judges));
    return nodes;
  });
}

function entryDrilldown(ctx, genId, entryId, row, exps, judges) {
  const runId = row ? row.run_id : null;
  const card = el('div', { class: 'd-panel e-drill' });
  card.appendChild(el('div', { class: 'd-row' }, [
    stat(row && svg.isNum(row.drift_loss) ? svg.fmt(row.drift_loss, 1) : '—', 'drift loss'),
    stat(row ? passLabel(row.pass_fail) : '—', 'predicate'),
    stat(row && row.wall_clock_budget_exceeded ? 'timed out' : (row && svg.isNum(row.runtime_ms) ? `${(row.runtime_ms / 1000).toFixed(0)}s` : '—'), 'runtime'),
  ]));

  // expectation outcomes
  const outcomes = (exps && Array.isArray(exps.outcomes)) ? exps.outcomes : [];
  if (outcomes.length) {
    const grid = el('div', { class: 'd-expect-grid', style: 'margin-top:12px;' });
    for (const o of outcomes) {
      const cls = o.passed === true ? 'd-good' : o.passed === false ? 'd-bad' : 'd-flat';
      grid.appendChild(el('div', { class: 'd-expect-row' }, [
        el('span', { class: 'd-expect-dot ' + cls, title: o.passed === true ? 'passed' : o.passed === false ? 'failed' : 'no verdict' }),
        el('span', { class: 'd-expect-kind', text: o.kind || 'expectation' }),
        o.judge_name ? el('span', { class: 'd-faint', text: ' · ' + o.judge_name }) : null,
        el('span', { class: 'd-expect-detail d-faint', text: o.detail ? ' — ' + o.detail : '' }),
      ].filter(Boolean)));
    }
    card.appendChild(grid);
  } else {
    card.appendChild(el('div', { style: 'margin-top:12px;' }, [empty('No expectation recorded for this entry (no predicate / rubric).')]));
  }

  // per-judge weighted losses
  const jrows = (judges && Array.isArray(judges.judges)) ? judges.judges : [];
  const jitems = jrows.filter((j) => svg.isNum(j.weighted_loss)).sort((a, b) => b.weighted_loss - a.weighted_loss).map((j) => ({ label: j.judge_name, value: j.weighted_loss }));
  if (jitems.length) {
    card.appendChild(el('p', { class: 'd-faint', style: 'margin:14px 0 4px;font-size:11px;', text: 'per-judge weighted process-drift loss · higher = more drift' }));
    card.appendChild(svg.valueBars({ width: 420, rowHeight: 20, labelWidth: 180, items: jitems }));
  }

  // a link into the full run transcript (the run view) — a properly themed
  // button-like link (the E bug fix: never an unstyled anchor).
  card.appendChild(el('div', { class: 'e-drill-actions', style: 'margin-top:14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;' }, [
    el('a', { class: 'h-link-btn', href: ctx.href('run', { gen: genId, entry: entryId }) }, [
      'Open the full run transcript ', el('span', { 'aria-hidden': 'true', text: '→' }),
    ]),
    runId ? el('span', { class: 'd-faint d-mono', text: runId.slice(0, 10) + '…' }) : null,
  ].filter(Boolean)));

  return section('Entry · ' + entryId, card);
}

function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}

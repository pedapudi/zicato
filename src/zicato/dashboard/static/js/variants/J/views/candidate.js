// variants/J/views/candidate.js — CANDIDATE (one generation).
//
// Console's candidate screen pairs the compact lifecycle DAG with the
// per-board scoring dot-plot (the D dot-plot, theme-aware so it reads in all
// three themes). The selected entry lives in the URL so the drill-down
// rebuilds ONLY on a route change, never on a heartbeat.
//
// Data: /api/epoch, /api/lineage, /api/score-trajectory,
// /api/generation/{e}/{g}/per-entry, /api/round/{e}/{champ}/{chall}/gate,
// /api/run/{e}/{g}/{entry}/{expectations,per-judge}.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { lifecycleDag } from '../dag.js';
import { gatedSwap, section, empty, stat, verdictPill, normaliseDecision } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dj-empty', text: 'Reading candidate…' }));
  const entryParam = params && params.entry;

  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dj-h1', text: 'Candidate' }), empty('No current epoch.')]);
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
    gatedSwap(host, 'no-cand', () => [el('h1', { class: 'dj-h1', text: 'Candidate' }), empty('No candidate selected.')]);
    return;
  }

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

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

  let gate = null;
  if (!baseline && node.parent) gate = await D.gate(epochId, node.parent, genId);
  const deltaScalar = gate && svg.isNum(gate.delta_scalar) ? gate.delta_scalar : null;

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
    nodes.push(el('div', { class: 'dj-pagehead' }, [
      el('h1', { class: 'dj-h1' }, [(node.promoted ? '♛ ' : '') + 'Candidate ' + genId]),
      el('p', { class: 'dj-lede', text: baseline ? 'The seed candidate (no parent) — it defines the loss floor for the epoch.' : `Born from ${node.parent} by a patch; faced the board; met the champion at the gate.` }),
    ]));

    nodes.push(el('div', { class: 'dj-panel dj-row' }, [
      stat(svg.isNum(scalarByGen.get(genId)) ? svg.fmt(scalarByGen.get(genId), 1) : '—', 'scalar (loss)'),
      stat(svg.isNum(deltaScalar) ? svg.fmtSigned(deltaScalar, 1) : '—', 'Δ vs champion'),
      stat(node.parent || 'seed', 'parent'),
      el('div', { class: 'dj-stat' }, [verdictPill(baseline ? 'baseline' : decision)]),
    ]));

    const dagCard = el('div', { class: 'dj-panel', style: 'overflow-x:auto;' });
    dagCard.appendChild(lifecycleDag({
      genId, parentId: node.parent, baseline, promoted: node.promoted, decision,
      deltaScalar, patchPoints: mpts, entries,
      width: 900, height: Math.max(300, 120 + entries.length * 34),
      onEntry: (eid) => ctx.navigate('candidate', { gen: genId, entry: eid }),
    }));
    dagCard.appendChild(el('p', { class: 'dj-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'parent → patch → board (one node per entry, colour = pass/fail/timeout) → Σ → gate → terminal · click a board node → its drill-down' }));
    nodes.push(section('Lifecycle · cause → effect → verdict', dagCard));

    const scoreCard = el('div', { class: 'dj-panel' });
    if (entries.length) {
      const items = entries
        .filter((e) => svg.isNum(e.drift_loss))
        .sort((a, b) => b.drift_loss - a.drift_loss)
        .map((e) => ({ label: e.entry_id, value: e.drift_loss, id: e.entry_id, pass: e.pass_fail, timeout: !!e.wall_clock_budget_exceeded }));
      scoreCard.appendChild(svg.valueDotPlot({
        width: 560, rowHeight: 21, labelWidth: 200, items,
        reference: svg.isNum(championScalar) ? { value: championScalar, label: `champion ${championId}` } : null,
        onClick: (it) => ctx.navigate('candidate', { gen: genId, entry: it.id }),
      }));
      scoreCard.appendChild(el('div', { class: 'dj-legend' }, [
        svg.isNum(championScalar) ? el('span', null, [el('i', { class: 'spine', style: 'border-color:var(--v2-ink-faint);border-top-style:dashed;' }), `champion ${championId} = ${svg.fmt(championScalar, 1)}`]) : null,
        el('span', null, [el('i', { class: 'dotact' }), 'pass']),
        el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'fail']),
        el('span', { class: 'dj-faint', text: '⏱ timeout · click an entry → its drill-down' }),
      ].filter(Boolean)));
    } else {
      scoreCard.appendChild(empty('No per-entry scores for this candidate (the index may not be built).'));
    }
    nodes.push(section('Per-board scoring · sorted, vs champion', scoreCard));

    if (entryParam) nodes.push(entryDrilldown(ctx, genId, entryParam, drillRow, exps, judges));
    return nodes;
  });
}

function entryDrilldown(ctx, genId, entryId, row, exps, judges) {
  const runId = row ? row.run_id : null;
  const card = el('div', { class: 'dj-panel dj-drill' });
  card.appendChild(el('div', { class: 'dj-row' }, [
    stat(row && svg.isNum(row.drift_loss) ? svg.fmt(row.drift_loss, 1) : '—', 'drift loss'),
    stat(row ? passLabel(row.pass_fail) : '—', 'predicate'),
    stat(row && row.wall_clock_budget_exceeded ? 'timed out' : (row && svg.isNum(row.runtime_ms) ? `${(row.runtime_ms / 1000).toFixed(0)}s` : '—'), 'runtime'),
  ]));

  const outcomes = (exps && Array.isArray(exps.outcomes)) ? exps.outcomes : [];
  if (outcomes.length) {
    const grid = el('div', { class: 'dj-expect-grid', style: 'margin-top:12px;' });
    for (const o of outcomes) {
      const cls = o.passed === true ? 'dj-good' : o.passed === false ? 'dj-bad' : 'dj-flat';
      grid.appendChild(el('div', { class: 'dj-expect-row' }, [
        el('span', { class: 'dj-expect-dot ' + cls, title: o.passed === true ? 'passed' : o.passed === false ? 'failed' : 'no verdict' }),
        el('span', { class: 'dj-expect-kind', text: o.kind || 'expectation' }),
        o.judge_name ? el('span', { class: 'dj-faint', text: ' · ' + o.judge_name }) : null,
        el('span', { class: 'dj-expect-detail dj-faint', text: o.detail ? ' — ' + o.detail : '' }),
      ].filter(Boolean)));
    }
    card.appendChild(grid);
  } else {
    card.appendChild(el('div', { style: 'margin-top:12px;' }, [empty('No expectation recorded for this entry (no predicate / rubric).')]));
  }

  const jrows = (judges && Array.isArray(judges.judges)) ? judges.judges : [];
  const jitems = jrows.filter((j) => svg.isNum(j.weighted_loss)).sort((a, b) => b.weighted_loss - a.weighted_loss).map((j) => ({ label: j.judge_name, value: j.weighted_loss }));
  if (jitems.length) {
    card.appendChild(el('p', { class: 'dj-faint', style: 'margin:14px 0 4px;font-size:11px;', text: 'per-judge weighted process-drift loss · higher = more drift' }));
    card.appendChild(svg.valueBars({ width: 420, rowHeight: 20, labelWidth: 180, items: jitems }));
  }

  // The themed "open full transcript" link (E bug: must be a styled control).
  card.appendChild(el('div', { style: 'margin-top:14px;' }, [
    el('a', { class: 'dj-linkbtn', href: ctx.href('run', { gen: genId, entry: entryId }), text: 'Open the full run transcript →' }),
    runId ? el('span', { class: 'dj-faint dj-mono', style: 'margin-left:8px;', text: runId.slice(0, 10) + '…' }) : null,
  ].filter(Boolean)));

  return section('Entry · ' + entryId, card);
}

function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}

// variants/I/views/candidate.js — CANDIDATE (one generation).
//
// Ledger's candidate screen reads one generation's life as a paper page:
//   * the hypothesis bet as a PULL-QUOTE (the editorial voice);
//   * the fit-to-width Tufte SANKEY — candidate → per-board loss → aggregate
//     scalar (the re-skinned causal flow, NO pan/zoom viewport), with the
//     board nodes clickable to drill in;
//   * the compact lifecycle DAG (parent → patch → board fan → Σ → gate);
//   * the per-board scoring dot-plot (absolute drift loss, sorted, vs the
//     champion reference), clickable to drill in;
//   * on rejection, the GATE'S reason rendered as a pull-quote;
//   * the entry drill-down (when an entry is selected via the URL) — its
//     expectation outcomes + per-judge weighted losses + a themed button
//     into the full run transcript (the E-bug fix: a styled link, never a
//     bare anchor).
//
// The selected entry lives in the URL, so the drill-down rebuilds ONLY on
// a route change (the digest carries the entry param), never on a heartbeat.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { lifecycleDag } from '../dag.js';
import { buildTufteSankey } from '../diagram/sankey.js';
import { gatedSwap, section, empty, stat, verdictPill, normaliseDecision, pageHead, figure, pullQuote } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'd-empty', text: 'Reading candidate…' }));
  const entryParam = params && params.entry;

  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [pageHead('Candidate', 'Candidate', ''), empty('No current epoch.')]);
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
    gatedSwap(host, 'no-cand', () => [pageHead('Candidate', 'Candidate', ''), empty('No candidate selected.')]);
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
  const hyp = exp && exp.hypothesis ? exp.hypothesis : null;
  const hypIdea = hyp ? (hyp.core_idea || hyp.idea || hyp.summary || null) : (exp && exp.hypothesis_core_idea) || null;
  const mpts = hyp && Array.isArray(hyp.mutation_points) ? hyp.mutation_points.length
    : (exp && Array.isArray(exp.mutation_points) ? exp.mutation_points.length : null);

  const pe = await D.perEntry(epochId, genId);
  const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];

  let gate = null;
  if (!baseline && node.parent) gate = await D.gate(epochId, node.parent, genId);
  const deltaScalar = gate && svg.isNum(gate.delta_scalar) ? gate.delta_scalar : null;
  const gateReason = gate && gate.reason ? gate.reason : null;

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
    reason: gateReason, hyp: hypIdea, mpts,
    entries: entries.map((e) => [e.entry_id, svg.isNum(e.drift_loss) ? e.drift_loss.toFixed(3) : null, e.pass_fail, !!e.wall_clock_budget_exceeded]),
    drill: entryParam || null,
    drillExp: exps && Array.isArray(exps.outcomes) ? exps.outcomes.map((o) => [o.kind, o.passed, o.judge_name, o.detail]) : null,
    drillJudge: judges && Array.isArray(judges.judges) ? judges.judges.map((j) => [j.judge_name, j.weighted_loss]) : null,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(pageHead('Candidate · ' + (baseline ? 'seed' : decision),
      (node.promoted ? '♛ ' : '') + 'Candidate ' + genId,
      baseline ? 'The seed candidate (no parent) — it defines the loss floor for the epoch.'
        : `Born from ${node.parent} by a patch; faced the board; met the champion at the gate.`));

    if (hypIdea) {
      nodes.push(pullQuote(hypIdea, { attribution: 'the proposer’s bet for ' + genId, class: 'i-pq-hyp' }));
    }

    nodes.push(el('div', { class: 'd-panel d-row' }, [
      stat(svg.isNum(scalarByGen.get(genId)) ? svg.fmt(scalarByGen.get(genId), 1) : '—', 'scalar (loss)'),
      stat(svg.isNum(deltaScalar) ? svg.fmtSigned(deltaScalar, 1) : '—', 'Δ vs champion'),
      stat(node.parent || 'seed', 'parent'),
      el('div', { class: 'd-stat' }, [verdictPill(baseline ? 'baseline' : decision)]),
    ]));

    // ---- fit-to-width Tufte Sankey (causal flow) ----
    if (entries.length) {
      const rows = entries.map((e) => ({
        entryId: e.entry_id, driftLoss: e.drift_loss, passFail: e.pass_fail,
        budgetExceeded: !!e.wall_clock_budget_exceeded, runId: e.run_id,
      }));
      const sankey = buildTufteSankey({
        genId, rows,
        onBoard: (r) => ctx.navigate('candidate', { gen: genId, entry: r.entryId }),
      });
      nodes.push(section('Causal flow · candidate → per-board loss → aggregate scalar',
        el('div', { class: 'd-panel' }, [
          figure(sankey, 'Ribbon width is each board’s contribution to the aggregate scalar. Fit-to-width, no zoom. Click a board node to drill into it.', { label: 'Figure 1.' }),
        ])));
    }

    // ---- compact lifecycle DAG ----
    const dagInner = el('div', { class: 'i-scroll-x' });
    dagInner.appendChild(lifecycleDag({
      genId, parentId: node.parent, baseline, promoted: node.promoted, decision,
      deltaScalar, patchPoints: mpts, entries,
      width: 900, height: Math.max(300, 120 + entries.length * 34),
      onEntry: (eid) => ctx.navigate('candidate', { gen: genId, entry: eid }),
    }));
    nodes.push(section('Lifecycle · cause → effect → verdict',
      el('div', { class: 'd-panel' }, [
        figure(dagInner, 'Parent → patch → board fan (node colour reads pass / fail / timeout) → Σ → gate → terminal. Click a board node to drill in.', { label: 'Figure 2.' }),
      ])));

    // ---- per-board scoring dot-plot ----
    const scoreCard = el('div', { class: 'd-panel' });
    if (entries.length) {
      const items = entries
        .filter((e) => svg.isNum(e.drift_loss))
        .sort((a, b) => b.drift_loss - a.drift_loss)
        .map((e) => ({ label: e.entry_id, value: e.drift_loss, id: e.entry_id, pass: e.pass_fail, timeout: !!e.wall_clock_budget_exceeded }));
      const dot = svg.valueDotPlot({
        width: 560, rowHeight: 22, labelWidth: 200, items,
        reference: svg.isNum(championScalar) ? { value: championScalar, label: `champion ${championId}` } : null,
        onClick: (it) => ctx.navigate('candidate', { gen: genId, entry: it.id }),
      });
      scoreCard.appendChild(figure(dot,
        (svg.isNum(championScalar) ? `The dashed reference is champion ${championId} = ${svg.fmt(championScalar, 1)}. ` : '')
        + 'Each entry’s absolute drift loss, sorted worst-first; the trailing glyph reads pass / fail / timeout. Click an entry to drill in.',
        { label: 'Figure 3.' }));
    } else {
      scoreCard.appendChild(empty('No per-entry scores for this candidate (the index may not be built).'));
    }
    nodes.push(section('Per-board scoring · sorted, vs champion', scoreCard));

    // ---- the gate's reason as a pull-quote (rejection / promotion) ----
    if (!baseline && gateReason) {
      nodes.push(pullQuote(gateReason, {
        attribution: 'the promote gate · ' + decision, class: 'i-pq-' + decision,
      }));
    }

    // ---- entry drill-down ----
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

  const jrows = (judges && Array.isArray(judges.judges)) ? judges.judges : [];
  const jitems = jrows.filter((j) => svg.isNum(j.weighted_loss)).sort((a, b) => b.weighted_loss - a.weighted_loss).map((j) => ({ label: j.judge_name, value: j.weighted_loss }));
  if (jitems.length) {
    card.appendChild(el('p', { class: 'i-figcap', style: 'margin:14px 0 4px;', text: 'Per-judge weighted process-drift loss; higher is more drift.' }));
    card.appendChild(svg.valueBars({ width: 420, rowHeight: 20, labelWidth: 180, items: jitems }));
  }

  // The E-bug fix: a themed button, never a bare unstyled anchor.
  card.appendChild(el('div', { class: 'i-drill-actions' }, [
    el('a', { class: 'i-btn', href: ctx.href('run', { gen: genId, entry: entryId }), text: 'Open the full run transcript →' }),
    runId ? el('span', { class: 'd-faint d-mono', style: 'margin-left:10px;', text: runId.slice(0, 10) + '…' }) : null,
  ].filter(Boolean)));

  return section('Entry · ' + entryId, card);
}

function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}

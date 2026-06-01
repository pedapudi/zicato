// variants/L/views/candidate.js — CANDIDATE (one generation).
//
// The lifecycle screen: a Tufte causal-flow SANKEY (candidate → per-board
// loss → aggregate scalar; label/value never overlap — fix #5), the
// per-board scoring DOT-PLOT (each entry's absolute loss vs the champion;
// click → the per-board cross-candidate view, keyed by entry id), the entry
// drill-down, and — for a non-baseline candidate — the PROMOTE GATE laid out
// as clean STACKED sections that never overlap (fix #1):
//   (a) decision pill + Δscalar / Δpass-rate,
//   (b) the rules ladder — each rule its OWN row (label · status · detail),
//   (c) a SEPARATE champion-vs-challenger scalar-components comparison block.
//
// Data: /api/epoch, /api/lineage, /api/score-trajectory,
// /api/generation/{e}/{g}/per-entry, /api/round/{e}/{champ}/{chall}/gate,
// /api/run/{e}/{g}/{entry}/{expectations,per-judge}.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, verdictPill, normaliseDecision, linkButton } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'vl-empty', text: 'Reading candidate…' }));
  const entryParam = params && params.entry;

  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'vl-h1', text: 'Candidate' }), empty('No current epoch.')]);
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
    gatedSwap(host, 'no-cand', () => [el('h1', { class: 'vl-h1', text: 'Candidate' }), empty('No candidate selected.')]);
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

  const pe = await D.perEntry(epochId, genId);
  const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];

  let gate = null;
  if (!baseline && node.parent) gate = await D.gate(epochId, node.parent, genId);
  const deltaScalar = gate && svg.isNum(gate.delta_scalar) ? gate.delta_scalar : null;

  let exps = null; let judges = null; let drillRow = null;
  if (entryParam) {
    [exps, judges] = await Promise.all([D.expectations(epochId, genId, entryParam), D.perJudgeForRun(epochId, genId, entryParam)]);
    drillRow = entries.find((e) => e.entry_id === entryParam) || null;
  }

  const digest = JSON.stringify({
    genId, parent: node.parent, decision, championId,
    champScalar: svg.isNum(championScalar) ? championScalar.toFixed(3) : null,
    delta: svg.isNum(deltaScalar) ? deltaScalar.toFixed(3) : null,
    gate: gate ? {
      d: gate.decision, dp: gate.delta_pass_rate,
      rules: Array.isArray(gate.rules) ? gate.rules.map((r) => [r.id, r.status, r.fired, r.detail]) : null,
      comp: gate.scalar_components || null,
    } : null,
    entries: entries.map((e) => [e.entry_id, svg.isNum(e.drift_loss) ? e.drift_loss.toFixed(3) : null, e.pass_fail, !!e.wall_clock_budget_exceeded]),
    drill: entryParam || null,
    drillExp: exps && Array.isArray(exps.outcomes) ? exps.outcomes.map((o) => [o.kind, o.passed, o.judge_name, o.detail]) : null,
    drillJudge: judges && Array.isArray(judges.judges) ? judges.judges.map((j) => [j.judge_name, j.weighted_loss]) : null,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'vl-pagehead' }, [
      el('h1', { class: 'vl-h1', text: (node.promoted ? '♛ ' : '') + 'Candidate ' + genId }),
      el('p', { class: 'vl-lede', text: baseline ? 'The seed candidate (no parent) — it defines the loss floor for the epoch.' : `Born from ${node.parent} by a patch; faced the board; met the champion at the gate.` }),
    ]));

    nodes.push(el('div', { class: 'vl-panel vl-row' }, [
      stat(svg.isNum(scalarByGen.get(genId)) ? svg.fmt(scalarByGen.get(genId), 1) : '—', 'scalar (loss)'),
      stat(svg.isNum(deltaScalar) ? svg.fmtSigned(deltaScalar, 1) : '—', 'Δ vs champion'),
      stat(node.parent || 'seed', 'parent'),
      el('div', { class: 'vl-stat' }, [verdictPill(baseline ? 'baseline' : decision)]),
    ]));

    // ---- lifecycle causal-flow sankey -------------------------------
    const boards = entries.filter((e) => svg.isNum(e.drift_loss)).map((e) => ({
      id: e.entry_id, label: e.entry_id, value: e.drift_loss, ref: e.entry_id,
      cls: e.wall_clock_budget_exceeded ? 'vl-bad' : (e.pass_fail === 1 ? 'vl-good' : ''),
    }));
    const agg = boards.reduce((a, b) => a + b.value, 0);
    const flowCard = el('div', { class: 'vl-panel' });
    if (boards.length) {
      flowCard.appendChild(svg.sankey({
        width: 760, candidate: { label: genId, sub: 'patch on mutation sites' }, boards,
        aggregate: { label: 'scalar', sub: svg.fmt(agg, 1) + ' loss' },
        onBoard: (entryId) => ctx.navigate('run', { gen: genId, entry: entryId }),
      }));
      flowCard.appendChild(el('p', { class: 'vl-faint vl-fignote', text: 'candidate → per-board loss → aggregate scalar · each board node’s value is right-aligned, clear of its label · click a board → its run' }));
    } else {
      flowCard.appendChild(empty('No per-board flow yet for this candidate.'));
    }
    nodes.push(section('Lifecycle · cause → effect → verdict', flowCard));

    // ---- promote gate (clean stacked sections) ----------------------
    if (!baseline && gate) nodes.push(section('Promote gate · the decisive moment', gatePanel(gate, championId, genId)));

    // ---- per-board scoring dot-plot ---------------------------------
    const scoreCard = el('div', { class: 'vl-panel' });
    if (entries.length) {
      const items = entries
        .filter((e) => svg.isNum(e.drift_loss))
        .sort((a, b) => b.drift_loss - a.drift_loss)
        .map((e) => ({ label: e.entry_id, value: e.drift_loss, id: e.entry_id, pass: e.pass_fail, timeout: !!e.wall_clock_budget_exceeded }));
      scoreCard.appendChild(svg.valueDotPlot({
        width: 560, rowHeight: 22, labelWidth: 200, items,
        reference: svg.isNum(championScalar) ? { value: championScalar, label: `champion ${championId}` } : null,
        onClick: (it) => ctx.navigate('board', { entry: it.id }),
      }));
      scoreCard.appendChild(el('p', { class: 'vl-faint vl-fignote', text: 'lower loss is better · ✓ pass · ✕ fail · ⏱ timeout · click an entry → its cross-candidate view' }));
    } else {
      scoreCard.appendChild(empty('No per-entry scores for this candidate (the index may not be built).'));
    }
    nodes.push(section('Per-board scoring · sorted, vs champion', scoreCard));

    if (entryParam) nodes.push(entryDrilldown(ctx, genId, entryParam, drillRow, exps, judges));
    return nodes;
  });
}

// The promote gate — three STACKED, non-overlapping sub-blocks, each
// fit-to-width. (a) decision + deltas; (b) rules ladder, one row each;
// (c) a SEPARATE champion-vs-challenger scalar-components block.
function gatePanel(gate, championId, challengerId) {
  const card = el('div', { class: 'vl-panel vl-gate' });

  // (a) decision + deltas
  const dec = String(gate.decision || 'rejected').toLowerCase();
  card.appendChild(el('div', { class: 'vl-gate-head' }, [
    el('span', { class: 'vl-pill vl-' + dec, text: dec }),
    el('div', { class: 'vl-gate-deltas' }, [
      deltaTile('Δ scalar', svg.isNum(gate.delta_scalar) ? svg.fmtSigned(gate.delta_scalar, 2) : '—', gate.delta_scalar),
      deltaTile('Δ pass-rate', svg.isNum(gate.delta_pass_rate) ? svg.fmtSigned(gate.delta_pass_rate, 2) : '—', svg.isNum(gate.delta_pass_rate) ? -gate.delta_pass_rate : null),
    ]),
    gate.reason ? el('p', { class: 'vl-gate-reason vl-faint', text: gate.reason }) : null,
  ].filter(Boolean)));

  // (b) rules ladder — one row each (label · status · detail), never overlapping
  const rules = Array.isArray(gate.rules) ? gate.rules : [];
  const ladder = el('div', { class: 'vl-gate-ladder' });
  if (rules.length) {
    for (const r of rules) {
      const status = String(r.status || (r.fired ? 'fail' : 'pending')).toLowerCase();
      ladder.appendChild(el('div', { class: 'vl-gate-rule' }, [
        el('span', { class: 'vl-gate-rule-label', text: r.label || r.id }),
        el('span', { class: 'vl-gate-rule-status vl-st-' + status.replace(/[^a-z_]/g, ''), text: status.replace(/_/g, ' ') }),
        el('span', { class: 'vl-gate-rule-detail vl-faint', text: r.detail || '' }),
      ]));
    }
  } else {
    ladder.appendChild(empty('No rule ladder recorded for this round.'));
  }
  card.appendChild(el('div', { class: 'vl-gate-block' }, [el('div', { class: 'vl-gate-subhead', text: 'Rules · short-circuiting, in order' }), ladder]));

  // (c) SEPARATE champion vs challenger scalar-components block
  const comp = gate.scalar_components || null;
  if (comp && (comp.champion || comp.challenger)) {
    const keys = [...new Set([...Object.keys(comp.champion || {}), ...Object.keys(comp.challenger || {})])].sort();
    const tbl = el('table', { class: 'vl-comp-table' });
    tbl.appendChild(el('thead', null, [el('tr', null, [
      el('th', { text: 'component' }),
      el('th', { class: 'vl-num', text: 'champion ' + (championId || '') }),
      el('th', { class: 'vl-num', text: 'challenger ' + (challengerId || '') }),
      el('th', { class: 'vl-num', text: 'Δ' }),
    ])]));
    const tb = el('tbody');
    for (const k of keys) {
      const cv = comp.champion ? comp.champion[k] : null;
      const xv = comp.challenger ? comp.challenger[k] : null;
      const d = (svg.isNum(cv) && svg.isNum(xv)) ? xv - cv : null;
      const dCls = d == null || d === 0 ? '' : (d > 0 ? 'vl-bad' : 'vl-good');
      tb.appendChild(el('tr', null, [
        el('td', { class: 'vl-mono', text: k }),
        el('td', { class: 'vl-num', text: svg.isNum(cv) ? svg.fmt(cv, 2) : '—' }),
        el('td', { class: 'vl-num', text: svg.isNum(xv) ? svg.fmt(xv, 2) : '—' }),
        el('td', { class: 'vl-num ' + dCls, text: d == null ? '—' : svg.fmtSigned(d, 2) }),
      ]));
    }
    tbl.appendChild(tb);
    const block = el('div', { class: 'vl-gate-block' }, [
      el('div', { class: 'vl-gate-subhead', text: 'Scalar components · champion vs challenger' }),
      tbl,
    ]);
    if (gate.primary_driver && gate.primary_driver.judge) {
      block.appendChild(el('p', { class: 'vl-faint vl-fignote', text: `primary driver: ${gate.primary_driver.judge} (${svg.fmtSigned(gate.primary_driver.delta, 1)})` }));
    }
    card.appendChild(block);
  }
  return card;
}

function deltaTile(k, v, polarity) {
  const tone = polarity == null || polarity === 0 ? '' : (polarity > 0 ? ' vl-bad-t' : ' vl-good-t');
  return el('div', { class: 'vl-gate-delta' }, [
    el('span', { class: 'vl-gate-delta-v' + tone, text: v }),
    el('span', { class: 'vl-gate-delta-k', text: k }),
  ]);
}

function entryDrilldown(ctx, genId, entryId, row, exps, judges) {
  const runId = row ? row.run_id : null;
  const card = el('div', { class: 'vl-panel vl-drill' });
  card.appendChild(el('div', { class: 'vl-row' }, [
    stat(row && svg.isNum(row.drift_loss) ? svg.fmt(row.drift_loss, 1) : '—', 'drift loss'),
    stat(row ? passLabel(row.pass_fail) : '—', 'predicate'),
    stat(row && row.wall_clock_budget_exceeded ? 'timed out' : (row && svg.isNum(row.runtime_ms) ? `${(row.runtime_ms / 1000).toFixed(0)}s` : '—'), 'runtime'),
  ]));
  const outcomes = (exps && Array.isArray(exps.outcomes)) ? exps.outcomes : [];
  if (outcomes.length) {
    const grid = el('div', { class: 'vl-expect-grid', style: 'margin-top:12px;' });
    for (const o of outcomes) {
      const cls = o.passed === true ? 'vl-good' : o.passed === false ? 'vl-bad' : 'vl-flat';
      grid.appendChild(el('div', { class: 'vl-expect-row' }, [
        el('span', { class: 'vl-expect-dot ' + cls, title: o.passed === true ? 'passed' : o.passed === false ? 'failed' : 'no verdict' }),
        el('span', { class: 'vl-expect-kind', text: o.kind || 'expectation' }),
        o.judge_name ? el('span', { class: 'vl-faint', text: ' · ' + o.judge_name }) : null,
        el('span', { class: 'vl-expect-detail vl-faint', text: o.detail ? ' — ' + o.detail : '' }),
      ].filter(Boolean)));
    }
    card.appendChild(grid);
  } else {
    card.appendChild(el('div', { style: 'margin-top:12px;' }, [empty('No expectation recorded for this entry (no predicate / rubric).')]));
  }
  const jrows = (judges && Array.isArray(judges.judges)) ? judges.judges : [];
  const jitems = jrows.filter((j) => svg.isNum(j.weighted_loss)).sort((a, b) => b.weighted_loss - a.weighted_loss).map((j) => ({ label: j.judge_name, value: j.weighted_loss }));
  if (jitems.length) {
    card.appendChild(el('p', { class: 'vl-faint', style: 'margin:14px 0 4px;font-size:11px;', text: 'per-judge weighted process-drift loss · higher = more drift' }));
    card.appendChild(svg.valueBars({ width: 420, rowHeight: 20, labelWidth: 180, items: jitems }));
  }
  card.appendChild(el('div', { class: 'vl-drill-actions', style: 'margin-top:14px;' }, [
    linkButton('Open the full run transcript →', ctx.href('run', { gen: genId, entry: entryId }), () => ctx.navigate('run', { gen: genId, entry: entryId })),
    runId ? el('span', { class: 'vl-faint vl-mono', style: 'margin-left:8px;', text: runId.slice(0, 10) + '…' }) : null,
  ].filter(Boolean)));
  return section('Entry · ' + entryId, card);
}

function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}

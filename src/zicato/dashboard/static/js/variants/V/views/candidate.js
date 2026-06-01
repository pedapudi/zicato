// variants/V/views/candidate.js — CANDIDATE (one generation), "Reel" + compare.
//
// This is the detail a REEL STATION opens: one round's challenger, showing its
// match-up + promote gate + lifecycle. It carries forward EVERY round-5 fix from
// P's candidate screen:
//   * fix #1 — the STACKED, non-overlapping PROMOTE GATE lives on THIS page.
//   * fix #2 — the lifecycle "patch" node is clickable → the SIDE-BY-SIDE diff.
//   * fix #3 — ALL match-ups the candidate was in (champion==gen||challenger==gen).
// AND it folds in S "Lens"'s first-class side-by-side COMPARE (round-6 #1): a
// "compare with…" picker SPLITS the SAME detail pane into two candidates side by
// side (lifecycle / gate / match-ups / per-board scoring), via the `cmp` route
// param (URL-encoded, deep-linkable). Clicking a match-up row compares the two.
//
// Data: /api/epoch, /api/lineage, /api/score-trajectory,
// /api/generation/{e}/{g}/per-entry, /api/tournaments,
// /api/round/{e}/{champ}/{chall}/gate,
// /api/run/{e}/{g}/{entry}/{expectations,per-judge}.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { lifecycleDag } from '../dag.js';
import { gatedSwap, section, subhead, empty, stat, verdictPill, normaliseDecision } from '../ui.js';
import { comparePicker, splitFrame } from '../compare.js';

export async function render(host, ctx, params, route) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading candidate…' }));
  const entryParam = params && params.entry;
  const cmpReq = route && route.cmp ? route.cmp : null;

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
  const genId = (params && params.gen && allIds.includes(params.gen)) ? params.gen : (allIds[allIds.length - 1] || (params && params.gen) || null);

  if (!genId) {
    gatedSwap(host, 'no-cand', () => [el('h1', { class: 'dn-h1', text: 'Candidate' }), empty('No candidate selected.')]);
    return;
  }

  const cmpId = (cmpReq && allIds.includes(cmpReq) && cmpReq !== genId) ? cmpReq : null;

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  const champ = genList.find((g) => g.promoted) || genList.find((g) => !g.parent) || null;
  const championId = champ ? champ.id : null;
  const allMatchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups : [];

  // resolve both sides' data (cached). Side B only when a compare target is set.
  const sideA = await resolveCandidate(epochId, genId, genList, championId, scalarByGen, experiments, allMatchups, entryParam);
  const sideB = cmpId ? await resolveCandidate(epochId, cmpId, genList, championId, scalarByGen, experiments, allMatchups, null) : null;

  const digest = JSON.stringify({ epochId, genId, cmpId, entry: entryParam || null, a: sideDigest(sideA), b: sideB ? sideDigest(sideB) : null });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1' }, [(sideA.node.promoted ? '♛ ' : '') + 'Candidate ' + genId + (cmpId ? `  vs  ${cmpId}` : '')]),
      el('p', { class: 'dn-lede', text: cmpId
        ? `Two candidates side by side — ${genId} against ${cmpId}: lifecycle, promote gate, match-ups and per-board scoring.`
        : 'This round’s challenger — its lifecycle, promote gate, every match-up and per-board scoring. Use “compare with…” to split this pane and read two candidates side by side.' }),
    ]));

    // the compare affordance — sets the cmp route param (URL-encoded).
    nodes.push(el('div', { class: 'vs-cmp-bar' }, [
      comparePicker({
        label: 'compare with…',
        options: genList.map((g) => ({ id: g.id, label: g.id + (g.promoted ? ' ♛' : '') })),
        current: genId, value: cmpId,
        onChange: (v) => ctx.navigate('candidate', { epochId, gen: genId }, { cmp: v }),
      }),
      cmpId ? el('button', { class: 'vs-cmp-clear', type: 'button', text: 'clear comparison' }) : null,
    ].filter(Boolean)));
    if (cmpId) {
      const clear = nodes[nodes.length - 1].querySelector('.vs-cmp-clear');
      if (clear) clear.addEventListener('click', () => ctx.navigate('candidate', { epochId, gen: genId }));
    }

    nodes.push(splitFrame({
      a: { title: genId + (sideA.node.promoted ? ' ♛' : ''), sub: sideA.decision, build: (h) => paintCandidate(h, ctx, epochId, sideA, entryParam, cmpId) },
      b: cmpId ? { title: cmpId + (sideB.node.promoted ? ' ♛' : ''), sub: sideB.decision, build: (h) => paintCandidate(h, ctx, epochId, sideB, null, null) } : null,
      emptyTitle: 'compare',
      emptyPrompt: 'Choose a candidate above to compare its lifecycle, gate, match-ups and per-board scoring against ' + genId + '.',
    }));
    return nodes;
  });
}

// Resolve one candidate's full bundle (cached drill-downs).
async function resolveCandidate(epochId, genId, genList, championId, scalarByGen, experiments, allMatchups, entryParam) {
  const node = genList.find((g) => g.id === genId) || { id: genId, parent: null, promoted: false };
  const exp = experiments.find((x) => x.generation_id === genId) || null;
  const baseline = !node.parent;
  const decision = baseline ? 'baseline' : (node.promoted ? 'promoted' : (exp ? normaliseDecision(exp.outcome) || 'rejected' : 'rejected'));
  const mpts = exp && exp.hypothesis && Array.isArray(exp.hypothesis.mutation_points) ? exp.hypothesis.mutation_points.length
    : (exp && Array.isArray(exp.mutation_points) ? exp.mutation_points.length : null);

  const pe = await D.perEntry(epochId, genId);
  const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];

  // fix #3 — EVERY matchup the candidate was in (as champion OR challenger).
  const mine = allMatchups.filter((m) => m.champion === genId || m.challenger === genId);

  // fix #1 — the gate(s): as a challenger its round (parent→gen); as a champion each defended round.
  const gateKeys = [];
  if (!baseline && node.parent) gateKeys.push({ champ: node.parent, chall: genId, role: 'as challenger' });
  for (const m of mine) if (m.champion === genId && m.challenger) gateKeys.push({ champ: genId, chall: m.challenger, role: 'defended' });
  const seenK = new Set();
  const gateSpecs = gateKeys.filter((k) => { const id = k.champ + '>' + k.chall; if (seenK.has(id)) return false; seenK.add(id); return true; });
  const gates = await Promise.all(gateSpecs.map((k) => D.gate(epochId, k.champ, k.chall)));
  const primaryGate = gates.find((g, i) => g && gateSpecs[i].role === 'as challenger') || null;
  const primaryDelta = primaryGate && svg.isNum(primaryGate.delta_scalar) ? primaryGate.delta_scalar : null;

  let exps = null, judges = null, drillRow = null;
  if (entryParam) {
    [exps, judges] = await Promise.all([D.expectations(epochId, genId, entryParam), D.perJudgeForRun(epochId, genId, entryParam)]);
    drillRow = entries.find((e) => e.entry_id === entryParam) || null;
  }

  return {
    node, baseline, decision, mpts, entries, mine, gateSpecs, gates,
    primaryDelta, championId, scalar: scalarByGen.get(genId), championScalar: championId ? scalarByGen.get(championId) : null,
    entryParam, exps, judges, drillRow,
  };
}

function sideDigest(s) {
  return {
    gen: s.node.id, parent: s.node.parent, decision: s.decision, mpts: s.mpts,
    scalar: svg.isNum(s.scalar) ? s.scalar.toFixed(3) : null,
    delta: svg.isNum(s.primaryDelta) ? s.primaryDelta.toFixed(3) : null,
    entries: s.entries.map((e) => [e.entry_id, svg.isNum(e.drift_loss) ? e.drift_loss.toFixed(3) : null, e.pass_fail, !!e.wall_clock_budget_exceeded]),
    matchups: s.mine.map((m) => [m.champion, m.challenger, m.decision, svg.isNum(m.delta_scalar) ? m.delta_scalar.toFixed(2) : null]),
    gates: s.gates.map((g, i) => g && Array.isArray(g.rules)
      ? [s.gateSpecs[i].champ, s.gateSpecs[i].chall, s.gateSpecs[i].role, g.decision, g.rules.map((r) => [r.id, r.status, r.fired])] : null),
    drill: s.entryParam || null,
  };
}

// Paint one candidate's full lifecycle panel into `host`. `cmpId`, when set,
// keeps match-up clicks routed back to the comparison.
function paintCandidate(host, ctx, epochId, s, entryParam, cmpId) {
  const opts = cmpId ? { cmp: cmpId } : undefined;
  const node = s.node; const genId = node.id;
  host.appendChild(el('div', { class: 'dn-panel dn-row' }, [
    stat(svg.isNum(s.scalar) ? svg.fmt(s.scalar, 1) : '—', 'scalar (loss)'),
    stat(svg.isNum(s.primaryDelta) ? svg.fmtSigned(s.primaryDelta, 1) : '—', 'Δ vs champion'),
    stat(node.parent || 'seed', 'parent'),
    el('div', { class: 'dn-stat' }, [verdictPill(s.baseline ? 'baseline' : s.decision)]),
  ]));

  // ---- lifecycle DAG (patch node clickable → diff, fix #2) ----
  const dagCard = el('div', { class: 'dn-panel', style: 'overflow-x:auto;' });
  dagCard.appendChild(lifecycleDag({
    genId, parentId: node.parent, baseline: s.baseline, promoted: node.promoted, decision: s.decision,
    deltaScalar: s.primaryDelta, patchPoints: s.mpts, entries: s.entries,
    width: 880, height: Math.max(280, 120 + s.entries.length * 34),
    onEntry: (eid) => ctx.navigate('candidate', { epochId, gen: genId, entry: eid }, opts),
    onPatch: s.baseline ? null : () => ctx.navigate('diff', { epochId, gen: genId }),
  }));
  dagCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: s.baseline ? 'parent → patch → board (one node per entry) → Σ → gate → terminal · click a board node → its drill-down' : 'parent → patch → board → Σ → gate → terminal · click the PATCH node → this candidate’s side-by-side diff · click a board node → its drill-down' }));
  host.appendChild(section('Lifecycle · cause → effect → verdict', dagCard));

  // ---- per-board scoring dot-plot ----
  const scoreCard = el('div', { class: 'dn-panel' });
  if (s.entries.length) {
    const items = s.entries
      .filter((e) => svg.isNum(e.drift_loss))
      .sort((a, b) => b.drift_loss - a.drift_loss)
      .map((e) => ({ label: e.entry_id, value: e.drift_loss, id: e.entry_id, pass: e.pass_fail, timeout: !!e.wall_clock_budget_exceeded }));
    scoreCard.appendChild(svg.valueDotPlot({
      width: 540, rowHeight: 21, labelWidth: 190, items,
      reference: svg.isNum(s.championScalar) ? { value: s.championScalar, label: `champion ${s.championId}` } : null,
      onClick: (it) => ctx.navigate('candidate', { epochId, gen: genId, entry: it.id }, opts),
    }));
    scoreCard.appendChild(el('div', { class: 'dn-legend' }, [
      svg.isNum(s.championScalar) ? el('span', null, [el('i', { class: 'spine', style: 'border-color:var(--v2-ink-faint);border-top-style:dashed;' }), `champion ${s.championId} = ${svg.fmt(s.championScalar, 1)}`]) : null,
      el('span', null, [el('i', { class: 'dotact' }), 'pass']),
      el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'fail']),
      el('span', { class: 'dn-faint', text: '⏱ timeout · click an entry → its drill-down' }),
    ].filter(Boolean)));
  } else {
    scoreCard.appendChild(empty('No per-entry scores for this candidate (the index may not be built).'));
  }
  host.appendChild(section('Per-board scoring · sorted, vs champion', scoreCard));

  if (entryParam) host.appendChild(entryDrilldown(ctx, epochId, genId, entryParam, s.drillRow, s.exps, s.judges, opts));

  // ---- fix #3: ALL match-ups for this candidate (click → compare) ----
  host.appendChild(section('Match-ups · every round this candidate was in', allMatchupsPanel(s.mine, genId, s.championId, ctx, epochId)));

  // ---- fix #1: the STACKED promote gate(s) ----
  if (s.gates.some((g) => g && Array.isArray(g.rules))) {
    s.gateSpecs.forEach((k, i) => {
      const g = s.gates[i];
      if (!g || !Array.isArray(g.rules)) return;
      host.appendChild(section(`Promote gate · ${k.champ} → ${k.chall} (${k.role})`, gatePanel(g, k.champ, k.chall)));
    });
  } else if (!s.baseline) {
    host.appendChild(section('Promote gate', el('div', { class: 'dn-panel' }, [empty('No gate decomposition recorded for this candidate’s round.')])));
  }
}

// fix #3 — every matchup the candidate was in, both roles. Clicking a round
// COMPARES the two candidates side by side (S's compare-first affordance).
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
    const other = asChamp ? m.challenger : m.champion;
    const dec = m.decision || 'rejected';
    const tr = el('tr', null, [
      el('td', null, [el('span', { class: 'dn-mono', text: `${m.champion} → ${m.challenger}` })]),
      el('td', null, [el('span', { class: 'dn-pill dn-' + (asChamp ? 'promoted' : 'rejected'), text: asChamp ? 'champion' : 'challenger' })]),
      el('td', null, [el('span', { class: 'dn-pill dn-' + dec, text: dec })]),
      el('td', { class: 'dn-num dn-mono ' + (m.delta_scalar > 0 ? 'dn-bad-t' : m.delta_scalar < 0 ? 'dn-good-t' : ''), text: svg.isNum(m.delta_scalar) ? svg.fmtSigned(m.delta_scalar, 2) : '—' }),
      el('td', { class: 'dn-faint', text: m.hypothesis_core_idea ? clip(m.hypothesis_core_idea, 64) : '—' }),
    ]);
    tr.style.cursor = 'pointer';
    tr.addEventListener('click', () => ctx.navigate('candidate', { epochId, gen: genId }, { cmp: other }));
    tbody.appendChild(tr);
  }
  tbl.appendChild(tbody);
  card.appendChild(tbl);
  card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:10px 0 0;', text: `${mine.length} round${mine.length === 1 ? '' : 's'} · click a round → compare the two candidates side by side` }));
  return card;
}

// fix #1 — the stacked, non-overlapping gate panel.
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

function entryDrilldown(ctx, epochId, genId, entryId, row, exps, judges, opts) {
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
    card.appendChild(svg.valueBars({ width: 420, rowHeight: 20, labelWidth: 180, items: jitems }));
  }

  // the transcript opens INLINE on the board view (no separate run page).
  card.appendChild(el('div', { style: 'margin-top:14px;' }, [
    el('a', { class: 'dn-linkbtn', href: ctx.href('board', { epochId, entry: entryId, gen: genId }, opts), text: 'Open the transcript inline (vs champion) →' }),
    runId ? el('span', { class: 'dn-faint dn-mono', style: 'margin-left:8px;', text: runId.slice(0, 10) + '…' }) : null,
  ].filter(Boolean)));

  return section('Entry · ' + entryId, card);
}

function clip(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
function passLabel(pf) { if (pf === 1 || pf === true) return 'pass'; if (pf === 0 || pf === false) return 'fail'; return 'none'; }

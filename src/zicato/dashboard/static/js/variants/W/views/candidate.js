// variants/W/views/candidate.js — CANDIDATE (one generation), comparison-first.
//
// Arena's candidate screen pairs Console III's lifecycle DAG + per-board scoring
// + ALL match-ups + the stacked promote gate with S's first-class side-by-side
// COMPARISON: a "compare with…" picker SPLITS the detail into two candidates
// (A | B). Each side gets its own digest-gated host so one side changing never
// rebuilds the other.
//
// Carried-forward fixes, on every candidate panel:
//   * the STACKED, non-overlapping PROMOTE GATE lives on THIS page (decision
//     header · rules ladder, each rule its own row · scalar-components block);
//   * the lifecycle "patch" node is clickable → this candidate's SIDE-BY-SIDE
//     diff (the `diff` view);
//   * ALL match-ups the candidate was in (champion==gen || challenger==gen),
//     not just one — clicking a match-up row compares the two candidates.
//
// Data: /api/epoch, /api/lineage, /api/score-trajectory,
// /api/generation/{e}/{g}/per-entry, /api/tournaments,
// /api/round/{e}/{champ}/{chall}/gate,
// /api/run/{e}/{g}/{entry}/{expectations,per-judge}.

import { el } from '../../../core/dom.js';
import * as D from '../../P/data.js';
import * as svg from '../../P/svg.js';
import * as model from '../model.js';
import { lifecycleDag } from '../../P/dag.js';
import { gatedSwap, section, subhead, empty, stat, verdictPill, normaliseDecision } from '../ui.js';
import { comparePicker, splitFrame } from '../../S/compare.js';

export async function render(host, ctx, route) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading candidate…' }));
  const params = (route && route.params) || route || {};
  const cmpParam = route && route.cmp ? route.cmp : null;

  const [ep, { gens, championId, scalarByGen, experiments }] = await Promise.all([D.epoch(), model.generations()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Candidate' }), empty('No current epoch.')]);
    return;
  }
  const epochId = params.epochId || ep.epoch_id;
  const allIds = gens.map((g) => g.id);
  const genId = (params.gen && allIds.includes(params.gen)) ? params.gen : (allIds[allIds.length - 1] || params.gen || null);
  if (!genId) {
    gatedSwap(host, 'no-cand', () => [el('h1', { class: 'dn-h1', text: 'Candidate' }), empty('No candidate selected.')]);
    return;
  }
  const cmpId = (cmpParam && allIds.includes(cmpParam) && cmpParam !== genId) ? cmpParam : null;
  const entryParam = params.entry || null;

  const sideA = await resolveCandidate(epochId, genId, gens, championId, scalarByGen, experiments, entryParam);
  const sideB = cmpId ? await resolveCandidate(epochId, cmpId, gens, championId, scalarByGen, experiments, null) : null;

  const digest = JSON.stringify({
    epochId, genId, cmpId, entry: entryParam,
    a: candidateDigest(sideA), b: sideB ? candidateDigest(sideB) : null,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1' }, [(sideA.node.promoted ? '♛ ' : '') + 'Candidate ' + genId + (cmpId ? `  vs  ${cmpId}` : '')]),
      el('p', { class: 'dn-lede', text: cmpId
        ? 'Two candidates side by side — lifecycle, promote gate, match-ups, and per-board scoring, A against B.'
        : 'One generation’s life. Use “compare with…” to split this pane and read two candidates side by side.' }),
    ]));

    // the compare affordance — sets the cmp route param (URL-encoded).
    nodes.push(el('div', { class: 'vs-cmp-bar' }, [
      comparePicker({
        label: 'compare with…',
        options: gens.map((g) => ({ id: g.id, label: g.id + (g.promoted ? ' ♛' : '') })),
        current: genId, value: cmpId,
        onChange: (v) => ctx.navigate('candidate', { epochId, gen: genId }, { cmp: v }),
      }),
      cmpId ? el('button', { class: 'vs-cmp-clear', type: 'button', text: 'clear comparison',
        onclick: () => ctx.navigate('candidate', { epochId, gen: genId }) }) : null,
    ].filter(Boolean)));

    nodes.push(splitFrame({
      a: { title: genId + (sideA.node.promoted ? ' ♛' : ''), sub: sideA.decision, build: (h) => paintCandidate(h, ctx, epochId, sideA, cmpId) },
      b: cmpId ? { title: cmpId + (sideB.node.promoted ? ' ♛' : ''), sub: sideB.decision, build: (h) => paintCandidate(h, ctx, epochId, sideB, null) } : null,
      emptyTitle: 'no comparison',
      emptyPrompt: 'Choose a candidate above to compare its lifecycle, gate, match-ups and per-board scoring against ' + genId + '.',
    }));
    return nodes;
  });
}

async function resolveCandidate(epochId, genId, gens, championId, scalarByGen, experiments, entryParam) {
  const node = gens.find((g) => g.id === genId) || { id: genId, parent: null, promoted: false };
  const baseline = !node.parent;
  const decision = model.decisionFor(node, experiments);

  const pe = await D.perEntry(epochId, genId);
  const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];

  // every matchup this candidate is in (as champion OR challenger).
  const matchups = await model.matchupsFor(genId);

  // gates: as a challenger (parent → gen); as a champion (each defended round).
  const gateKeys = [];
  if (!baseline && node.parent) gateKeys.push({ champ: node.parent, chall: genId, role: 'as challenger' });
  for (const m of matchups) {
    if (m.champion === genId && m.challenger) gateKeys.push({ champ: genId, chall: m.challenger, role: 'defended' });
  }
  const seen = new Set();
  const gateSpecs = gateKeys.filter((k) => { const id = k.champ + '>' + k.chall; if (seen.has(id)) return false; seen.add(id); return true; });
  const gates = await Promise.all(gateSpecs.map((k) => D.gate(epochId, k.champ, k.chall)));

  let drill = null;
  if (entryParam) {
    const [exps, judges] = await Promise.all([
      D.expectations(epochId, genId, entryParam), D.perJudgeForRun(epochId, genId, entryParam),
    ]);
    drill = { entry: entryParam, row: entries.find((e) => e.entry_id === entryParam) || null, exps, judges };
  }

  const mpts = entries.length; // patch-point count is surfaced via the diff view
  return { node, baseline, decision, entries, matchups, gateSpecs, gates, drill, championId, scalarByGen, mpts };
}

function candidateDigest(s) {
  return {
    gen: s.node.id, parent: s.node.parent, decision: s.decision,
    scalar: s.scalarByGen.has(s.node.id) ? s.scalarByGen.get(s.node.id).toFixed(3) : null,
    entries: s.entries.map((e) => [e.entry_id, svg.isNum(e.drift_loss) ? e.drift_loss.toFixed(3) : null, e.pass_fail, !!e.wall_clock_budget_exceeded]),
    matchups: s.matchups.map((m) => [m.champion, m.challenger, m.decision, svg.isNum(m.delta_scalar) ? m.delta_scalar.toFixed(2) : null]),
    gates: s.gates.map((g, i) => g && Array.isArray(g.rules)
      ? [s.gateSpecs[i].champ, s.gateSpecs[i].chall, s.gateSpecs[i].role, g.decision, g.rules.map((r) => [r.id, r.status, r.fired])]
      : null),
    drill: s.drill ? [s.drill.entry, s.drill.exps && Array.isArray(s.drill.exps.outcomes) ? s.drill.exps.outcomes.map((o) => [o.kind, o.passed]) : null] : null,
  };
}

function paintCandidate(host, ctx, epochId, s, cmpId) {
  const opts = cmpId ? { cmp: cmpId } : undefined;
  const node = s.node;
  const genId = node.id;
  const championId = s.championId;
  const championScalar = championId ? s.scalarByGen.get(championId) : null;
  const primaryGate = s.gates.find((g, i) => g && s.gateSpecs[i].role === 'as challenger') || null;
  const primaryDelta = primaryGate && svg.isNum(primaryGate.delta_scalar) ? primaryGate.delta_scalar : null;

  host.appendChild(el('div', { class: 'dn-panel dn-row' }, [
    stat(svg.isNum(s.scalarByGen.get(genId)) ? svg.fmt(s.scalarByGen.get(genId), 1) : '—', 'scalar (loss)'),
    stat(svg.isNum(primaryDelta) ? svg.fmtSigned(primaryDelta, 1) : '—', 'Δ vs champion'),
    stat(node.parent || 'seed', 'parent'),
    el('div', { class: 'dn-stat' }, [verdictPill(s.baseline ? 'baseline' : s.decision)]),
  ]));

  // lifecycle DAG — the PATCH node routes to this candidate's patch diff.
  const dagCard = el('div', { class: 'dn-panel', style: 'overflow-x:auto;' });
  dagCard.appendChild(lifecycleDag({
    genId, parentId: node.parent, baseline: s.baseline, promoted: node.promoted, decision: s.decision,
    deltaScalar: primaryDelta, patchPoints: s.entries.length ? null : null, entries: s.entries,
    width: 560, height: Math.max(260, 120 + s.entries.length * 30),
    onEntry: (eid) => ctx.navigate('candidate', { epochId, gen: genId, entry: eid }, opts),
    onPatch: s.baseline ? null : () => ctx.navigate('diff', { epochId, gen: genId }),
  }));
  if (!s.baseline) {
    dagCard.appendChild(el('a', {
      class: 'dn-linkbtn', style: 'margin-top:8px;',
      href: ctx.href('diff', { epochId, gen: genId }),
      text: 'Open this candidate’s side-by-side patch diff →',
    }));
  }
  host.appendChild(section('Lifecycle · cause → effect → verdict', dagCard));

  // per-board scoring dot-plot
  const scoreCard = el('div', { class: 'dn-panel' });
  if (s.entries.length) {
    const items = s.entries.filter((e) => svg.isNum(e.drift_loss)).sort((a, b) => b.drift_loss - a.drift_loss)
      .map((e) => ({ label: e.entry_id, value: e.drift_loss, id: e.entry_id, pass: e.pass_fail, timeout: !!e.wall_clock_budget_exceeded }));
    scoreCard.appendChild(svg.valueDotPlot({
      width: 480, rowHeight: 21, labelWidth: 180, items,
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

  if (s.drill) host.appendChild(entryDrilldown(ctx, epochId, genId, s.drill));

  // ALL match-ups for this candidate.
  host.appendChild(section('Match-ups · every round this candidate was in', matchupsPanel(s.matchups, genId, championId, ctx, epochId)));

  // the STACKED promote gate(s) on the candidate page.
  if (s.gates.some((g) => g && Array.isArray(g.rules))) {
    s.gateSpecs.forEach((k, i) => {
      const g = s.gates[i];
      if (!g || !Array.isArray(g.rules)) return;
      host.appendChild(section(`Promote gate · ${k.champ} → ${k.chall} (${k.role})`, gatePanel(g, k.champ, k.chall)));
    });
  } else if (!s.baseline) {
    host.appendChild(section('Promote gate', el('div', { class: 'dn-panel' }, [empty('No gate decomposition recorded for this candidate’s round.')])));
  } else {
    host.appendChild(section('Promote gate', el('div', { class: 'dn-panel' }, [empty('The seed candidate has no gate — it defines the loss floor that challengers must beat.')])));
  }
}

function matchupsPanel(matchups, genId, championId, ctx, epochId) {
  const card = el('div', { class: 'dn-panel' });
  if (!matchups.length) {
    card.appendChild(empty('This candidate did not run in any tournament round (it may be the seed and undefeated, or rounds are not yet recorded).'));
    return card;
  }
  const tbl = el('table', { class: 'dn-board-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', { text: 'round' }), el('th', { text: 'role' }), el('th', { text: 'decision' }),
    el('th', { class: 'dn-num', text: 'Δ scalar' }), el('th', { text: 'hypothesis' }),
  ])]));
  const tbody = el('tbody');
  for (const m of matchups) {
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
    tr.style.cursor = 'pointer';
    // clicking a match-up row compares the two candidates side by side.
    tr.addEventListener('click', () => ctx.navigate('candidate', { epochId, gen: genId }, { cmp: other }));
    tbody.appendChild(tr);
  }
  tbl.appendChild(tbody);
  card.appendChild(tbl);
  card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:10px 0 0;', text: genId === championId
    ? `as champion, ${genId} defended ${matchups.length} round${matchups.length === 1 ? '' : 's'} · click a round → compare the two candidates side by side`
    : `${matchups.length} round${matchups.length === 1 ? '' : 's'} · click a round → compare the two candidates side by side` }));
  return card;
}

// The stacked, non-overlapping promote gate.
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

function entryDrilldown(ctx, epochId, genId, drill) {
  const { entry, row, exps, judges } = drill;
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
        el('span', { class: 'dn-expect-dot ' + cls }),
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
  // the transcript opens INLINE on the board view (side by side, no separate run page).
  card.appendChild(el('div', { style: 'margin-top:14px;' }, [
    el('a', { class: 'dn-linkbtn', href: ctx.href('board', { epochId, entry }, { runs: [genId] }), text: 'Open the transcript inline (vs champion) →' }),
    runId ? el('span', { class: 'dn-faint dn-mono', style: 'margin-left:8px;', text: runId.slice(0, 10) + '…' }) : null,
  ].filter(Boolean)));
  return section('Entry · ' + entry, card);
}

function clip(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}

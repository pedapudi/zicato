// variants/R/views/candidate.js — the CANDIDATE detail pane (one generation).
//
// Reached by selecting a generation in the third Miller column. It carries the
// full candidate lifecycle, refined per the convergence-III fixes:
//   * the lifecycle DAG (parent → patch → board fan → Σ → gate → terminal);
//   * the per-board scoring dot-plot (champion reference rule);
//   * the PROMOTE GATE — stacked, non-overlapping rows + a separate
//     scalar-components block (FIX #1: N lacked the gate on the candidate);
//   * ALL match-ups the candidate was in — champion==gen OR challenger==gen
//     (FIX #3: O showed only one);
//   * a patch → per-candidate SIDE-BY-SIDE diff drilldown (FIX #2: clickable
//     PATCH node + an explicit "patch sites" rail).
//
// The selected entry / patch site / matchups facet lives in the URL, so the
// drilldown rebuilds ONLY on a path change — never on a heartbeat.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { lifecycleDag, verdictClass } from '../dag.js';
import { gatedSwap, section, subhead, empty, stat, verdictPill, normaliseDecision } from '../ui.js';

export async function render(host, ctx, path) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dr-empty', text: 'Reading candidate…' }));
  const entryParam = path && path.entry;
  const patchSite = path && path.mutationId;
  const facet = path && path.facet;

  const [ep, lin, traj, bracket, muts] = await Promise.all([
    D.epoch(), D.lineage(), D.scoreTrajectory(), D.bracket(), null,
  ]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dr-h1', text: 'Candidate' }), empty('No current epoch.')]);
    return;
  }
  const epochId = ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const genList = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' }));
  const allIds = genList.map((g) => g.id);
  const genId = (path && path.gen && allIds.includes(path.gen)) ? path.gen : (path && path.gen) || allIds[allIds.length - 1] || null;
  const node = genList.find((g) => g.id === genId) || (genId ? { id: genId, parent: null, promoted: false } : null);
  if (!node) {
    gatedSwap(host, 'no-cand', () => [el('h1', { class: 'dr-h1', text: 'Candidate' }), empty('No candidate selected.')]);
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

  // The promote gate vs the candidate's parent (FIX #1).
  let gate = null;
  if (!baseline && node.parent) gate = await D.gate(epochId, node.parent, genId);
  const deltaScalar = gate && svg.isNum(gate.delta_scalar) ? gate.delta_scalar : null;

  // ALL match-ups the candidate was in (FIX #3): champion==gen || challenger==gen.
  const matchupsAll = (bracket && Array.isArray(bracket.matchups))
    ? bracket.matchups.filter((m) => m.champion === genId || m.challenger === genId)
    : experiments.filter((x) => x.parent_generation_id && (x.parent_generation_id === genId || x.generation_id === genId))
      .map((x) => ({ champion: x.parent_generation_id, challenger: x.generation_id, decision: normaliseDecision(x.outcome) }));
  const mgrids = await Promise.all(matchupsAll.map((m) => (m.champion && m.challenger) ? D.matchupGrid(epochId, m.champion, m.challenger) : Promise.resolve(null)));

  // Per-entry drill.
  let exps = null, judges = null, drillRow = null;
  if (entryParam) {
    [exps, judges] = await Promise.all([D.expectations(epochId, genId, entryParam), D.perJudgeForRun(epochId, genId, entryParam)]);
    drillRow = entries.find((e) => e.entry_id === entryParam) || null;
  }

  // Patch → per-candidate side-by-side diff (FIX #2).
  let mutSurface = null; let patchList = []; let diffPaneData = null;
  if (!baseline) {
    mutSurface = await D.mutations(epochId);
    const pp = await D.patches(epochId, genId);
    patchList = (pp && Array.isArray(pp.patches)) ? pp.patches : [];
    if (patchSite) {
      const detail = await D.mutationDetail(epochId, patchSite);
      let baselineStr = baselineContent(detail);
      if (baselineStr == null && mutSurface && Array.isArray(mutSurface.mutations)) {
        baselineStr = baselineContent(mutSurface.mutations.find((m) => m.mutation_id === patchSite));
      }
      const match = patchList.find((p) => p.mutation_id === patchSite || p.id === patchSite) || null;
      let nextStr = match && match.new_content != null ? String(match.new_content) : null;
      let usedFallback = false;
      if ((baselineStr == null || nextStr == null)) {
        const df = await D.diff(epochId, genId);
        const files = (df && Array.isArray(df.files)) ? df.files : [];
        const site = mutSurface && Array.isArray(mutSurface.mutations) ? mutSurface.mutations.find((m) => m.mutation_id === patchSite) : null;
        const fileHint = site ? site.file : null;
        const f = (fileHint && files.find((x) => x.path === fileHint)) || files[0] || null;
        if (f) {
          if (baselineStr == null && f.old_content != null) { baselineStr = String(f.old_content); usedFallback = true; }
          if (nextStr == null && f.new_content != null) { nextStr = String(f.new_content); usedFallback = true; }
        }
      }
      const site = mutSurface && Array.isArray(mutSurface.mutations) ? mutSurface.mutations.find((m) => m.mutation_id === patchSite) : null;
      diffPaneData = { site, baseline: baselineStr, next: nextStr, op: match ? match.op : null, rationale: match ? match.rationale : null, usedFallback };
    }
  }

  const digest = JSON.stringify({
    genId, parent: node.parent, decision, championId,
    champScalar: svg.isNum(championScalar) ? championScalar.toFixed(3) : null,
    delta: svg.isNum(deltaScalar) ? deltaScalar.toFixed(3) : null, mpts,
    entries: entries.map((e) => [e.entry_id, svg.isNum(e.drift_loss) ? e.drift_loss.toFixed(3) : null, e.pass_fail, !!e.wall_clock_budget_exceeded]),
    drill: entryParam || null,
    drillExp: exps && Array.isArray(exps.outcomes) ? exps.outcomes.map((o) => [o.kind, o.passed, o.judge_name, o.detail]) : null,
    drillJudge: judges && Array.isArray(judges.judges) ? judges.judges.map((j) => [j.judge_name, j.weighted_loss]) : null,
    gate: gate && Array.isArray(gate.rules) ? [gate.decision, gate.rules.map((r) => [r.id, r.status])] : null,
    matchups: matchupsAll.map((m, i) => [m.champion, m.challenger, m.decision,
      mgrids[i] && Array.isArray(mgrids[i].entry_grid) ? mgrids[i].entry_grid.map((r) => [r.entry_id, r.parent_drift_loss, r.child_drift_loss, r.verdict]) : null]),
    facet: facet || null,
    patchSite: patchSite || null,
    patchList: patchList.map((p) => [p.mutation_id || p.id, p.op]),
    diff: diffPaneData ? [diffPaneData.baseline == null ? -1 : diffPaneData.baseline.length, diffPaneData.next == null ? -1 : diffPaneData.next.length] : null,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dr-pagehead' }, [
      el('h1', { class: 'dr-h1', text: (node.promoted ? '♛ ' : '') + 'Candidate ' + genId }),
      el('p', { class: 'dr-lede', text: baseline ? 'The seed candidate (no parent) — it defines the loss floor for the epoch.' : `Born from ${node.parent} by a patch; faced the board; met the champion at the gate.` }),
    ]));

    nodes.push(el('div', { class: 'dr-panel dr-row' }, [
      stat(svg.isNum(scalarByGen.get(genId)) ? svg.fmt(scalarByGen.get(genId), 1) : '—', 'scalar (loss)'),
      stat(svg.isNum(deltaScalar) ? svg.fmtSigned(deltaScalar, 1) : '—', 'Δ vs champion'),
      stat(node.parent || 'seed', 'parent'),
      el('div', { class: 'dr-stat' }, [verdictPill(baseline ? 'baseline' : decision)]),
    ]));

    // ---- lifecycle DAG (PATCH node clickable → diff; board nodes → entry) ----
    const dagCard = el('div', { class: 'dr-panel', style: 'overflow-x:auto;' });
    dagCard.appendChild(lifecycleDag({
      genId, parentId: node.parent, baseline, promoted: node.promoted, decision,
      deltaScalar, patchPoints: mpts, entries,
      width: 900, height: Math.max(300, 120 + entries.length * 34),
      onEntry: (eid) => ctx.navigate({ section: 'generations', gen: genId, entry: eid }),
      onPatch: () => {
        const first = patchList[0];
        ctx.navigate({ section: 'generations', gen: genId, mutationId: (first && (first.mutation_id || first.id)) || (patchSite || '') });
      },
    }));
    dagCard.appendChild(el('p', { class: 'dr-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'parent → patch → board (one node per entry) → Σ → gate → terminal · click PATCH → this candidate’s side-by-side diff · click a board node → its drill-down' }));
    nodes.push(section('Lifecycle · cause → effect → verdict', dagCard));

    // ---- per-board scoring dot-plot ----
    const scoreCard = el('div', { class: 'dr-panel' });
    if (entries.length) {
      const items = entries.filter((e) => svg.isNum(e.drift_loss)).sort((a, b) => b.drift_loss - a.drift_loss)
        .map((e) => ({ label: e.entry_id, value: e.drift_loss, id: e.entry_id, pass: e.pass_fail, timeout: !!e.wall_clock_budget_exceeded }));
      scoreCard.appendChild(svg.valueDotPlot({
        width: 560, rowHeight: 21, labelWidth: 200, items,
        reference: svg.isNum(championScalar) ? { value: championScalar, label: `champion ${championId}` } : null,
        onClick: (it) => ctx.navigate({ section: 'generations', gen: genId, entry: it.id }),
      }));
      scoreCard.appendChild(el('p', { class: 'dr-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'absolute per-board loss, worst-first · dashed rule = champion · click an entry → its drill-down' }));
    } else {
      scoreCard.appendChild(empty('No per-entry scores for this candidate (the index may not be built).'));
    }
    nodes.push(section('Per-board scoring · sorted, vs champion', scoreCard));

    // ---- the PROMOTE GATE (FIX #1) ----
    if (!baseline) {
      nodes.push(section('Promote gate · ' + (node.parent || 'champion') + ' → ' + genId, gatePanel(gate, node.parent, genId)));
    }

    // ---- ALL match-ups (FIX #3) ----
    nodes.push(section('All match-ups · every round this candidate was in', allMatchups(matchupsAll, mgrids, genId, ctx)));

    // ---- patch sites rail + per-candidate diff (FIX #2) ----
    if (!baseline) {
      nodes.push(section('Patch · this candidate’s mutation sites', patchRail(patchList, mutSurface, patchSite, genId, ctx)));
      nodes.push(section('Patch diff · champion baseline | challenger new', diffPane(diffPaneData, patchSite, genId)));
    }

    // ---- per-entry drill ----
    if (entryParam) nodes.push(entryDrilldown(ctx, genId, entryParam, drillRow, exps, judges));
    return nodes;
  });
}

function gatePanel(gate, parentId, genId) {
  const card = el('div', { class: 'dr-panel dr-gate' });
  if (!gate || (!Array.isArray(gate.rules) && gate.decision == null)) {
    card.appendChild(empty('No promote-gate decomposition recorded for this round.'));
    return card;
  }
  const decision = normaliseDecision(gate) || gate.decision || 'rejected';
  card.appendChild(el('div', { class: 'dr-gate-head' }, [
    el('div', { class: 'dr-gate-decision' }, [verdictPill(decision)]),
    el('div', { class: 'dr-row dr-gate-deltas' }, [
      svg.isNum(gate.delta_scalar) ? stat(svg.fmtSigned(gate.delta_scalar, 2), 'Δ scalar (loss)') : null,
      svg.isNum(gate.delta_pass_rate) ? stat(svg.fmtSigned(gate.delta_pass_rate, 2), 'Δ pass rate') : null,
      gate.primary_driver && gate.primary_driver.judge ? stat(gate.primary_driver.judge, 'primary driver') : null,
    ].filter(Boolean)),
  ]));
  if (gate.reason) card.appendChild(el('p', { class: 'dr-gate-reason', text: gate.reason }));
  const rules = Array.isArray(gate.rules) ? gate.rules : [];
  if (rules.length) {
    card.appendChild(subhead('Rules · short-circuiting, in order'));
    const ladder = el('ol', { class: 'dr-rules' });
    for (const r of rules) {
      const st = String(r.status || 'pending');
      ladder.appendChild(el('li', { class: 'dr-rule dr-rule-' + st }, [
        el('span', { class: 'dr-rule-dot', 'aria-hidden': 'true' }),
        el('span', { class: 'dr-rule-label', text: r.label || r.id }),
        el('span', { class: 'dr-rule-status', text: st.replace(/_/g, ' ') }),
        el('span', { class: 'dr-rule-detail dr-faint', text: r.detail || '' }),
      ]));
    }
    card.appendChild(ladder);
  }
  const sc = gate.scalar_components;
  if (sc && sc.champion && sc.challenger) {
    const keys = [...new Set([...Object.keys(sc.champion), ...Object.keys(sc.challenger)])].sort();
    if (keys.length) {
      card.appendChild(subhead(`Scalar components · champion ${parentId} vs challenger ${genId}`));
      const tbl = el('table', { class: 'dr-sc-table' });
      tbl.appendChild(el('thead', null, [el('tr', null, [
        el('th', { text: 'component' }), el('th', { class: 'dr-num', text: parentId }),
        el('th', { class: 'dr-num', text: genId }), el('th', { class: 'dr-num', text: 'Δ' }),
      ])]));
      const tbody = el('tbody');
      for (const k of keys) {
        const a = svg.isNum(sc.champion[k]) ? sc.champion[k] : 0;
        const b = svg.isNum(sc.challenger[k]) ? sc.challenger[k] : 0;
        const d = b - a;
        const dCls = d > 0 ? 'dr-bad-t' : d < 0 ? 'dr-good-t' : '';
        tbody.appendChild(el('tr', null, [
          el('td', { class: 'dr-mono', text: k }),
          el('td', { class: 'dr-num dr-mono', text: svg.fmt(a, 2) }),
          el('td', { class: 'dr-num dr-mono', text: svg.fmt(b, 2) }),
          el('td', { class: 'dr-num dr-mono ' + dCls, text: svg.fmtSigned(d, 2) }),
        ]));
      }
      tbl.appendChild(tbody);
      card.appendChild(tbl);
    }
  }
  return card;
}

function allMatchups(matchups, grids, genId, ctx) {
  const card = el('div', { class: 'dr-panel' });
  if (!matchups.length) { card.appendChild(empty('This candidate has no recorded match-ups.')); return card; }
  card.appendChild(el('p', { class: 'dr-faint', style: 'font-size:11px;margin:0 0 8px;', text: `${matchups.length} round${matchups.length === 1 ? '' : 's'} · this candidate as champion or challenger · click a duel line → that run` }));
  const wrap = el('div', { class: 'dr-pslope-grid' });
  matchups.forEach((m, i) => {
    const grid = grids[i];
    const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
    const series = rows.filter((r) => svg.isNum(r.parent_drift_loss) || svg.isNum(r.child_drift_loss))
      .map((r) => ({ label: r.entry_id, id: r.entry_id, a: svg.isNum(r.parent_drift_loss) ? r.parent_drift_loss : NaN, b: svg.isNum(r.child_drift_loss) ? r.child_drift_loss : NaN, verdict: r.verdict }));
    const dec = m.decision || 'rejected';
    const role = m.challenger === genId ? 'as challenger' : 'as champion';
    const cell = el('div', { class: 'dr-pslope-cell' }, [
      el('div', { class: 'dr-pslope-title' }, [
        el('span', { class: 'dr-mono', text: `${m.champion} → ${m.challenger}` }),
        el('span', { class: 'dr-faint', style: 'font-size:10px;', text: role }),
        el('span', { class: `dr-pill dr-${dec}`, text: dec }),
      ]),
    ]);
    if (series.length) {
      cell.appendChild(svg.pairedSlopegraph({
        width: 460, height: Math.max(200, 50 + series.length * 26),
        left: { title: `champion ${m.champion}` }, right: { title: `challenger ${m.challenger}` },
        labelGap: 140, goodDirection: 'down', series,
        onClick: (s) => ctx.navigate({ section: 'boards', entry: s.id, runGen: m.challenger }),
      }));
    } else {
      cell.appendChild(empty('No paired per-board grid for this round (loss files missing).'));
    }
    wrap.appendChild(cell);
  });
  card.appendChild(wrap);
  return card;
}

function patchRail(patchList, mutSurface, patchSite, genId, ctx) {
  const card = el('div', { class: 'dr-panel' });
  if (!patchList.length) { card.appendChild(empty('No patch ops recorded for this candidate.')); return card; }
  const siteMeta = new Map();
  if (mutSurface && Array.isArray(mutSurface.mutations)) for (const m of mutSurface.mutations) siteMeta.set(m.mutation_id, m);
  const rail = el('div', { class: 'dr-patch-rail' });
  for (const p of patchList) {
    const id = p.mutation_id || p.id;
    const meta = siteMeta.get(id);
    const active = patchSite === id;
    const chip = el('button', { class: 'dr-patch-chip' + (active ? ' dr-active' : ''), type: 'button' }, [
      el('span', { class: 'dr-patch-chip-id dr-mono', text: id }),
      el('span', { class: 'dr-patch-chip-sub dr-faint', text: (meta ? (meta.role || meta.file || '') : '') + (p.op ? ' · ' + p.op : '') }),
    ]);
    chip.addEventListener('click', () => ctx.navigate({ section: 'generations', gen: genId, mutationId: id }));
    rail.appendChild(chip);
  }
  card.appendChild(rail);
  card.appendChild(el('p', { class: 'dr-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'each chip = one mutation site this candidate patched · select → its baseline-vs-challenger diff below' }));
  return card;
}

function diffPane(data, patchSite, genId) {
  const card = el('div', { class: 'dr-panel dr-diffpane' });
  if (!patchSite || !data) {
    card.appendChild(empty('Select a patch site above (or click the PATCH node) to see this candidate’s baseline-vs-challenger diff.'));
    return card;
  }
  const site = data.site;
  card.appendChild(el('div', { class: 'dr-diff-meta' }, [
    el('span', { class: 'dr-diff-status dr-op-' + (data.op || 'edit'), text: data.op || 'edit' }),
    el('span', { class: 'dr-diff-path dr-mono', text: (site && (siteSub(site) || site.role || site.mutation_id)) || patchSite }),
    el('span', { class: 'dr-faint', text: ' · ' + genId }),
    data.usedFallback ? el('span', { class: 'dr-chip dr-chip-open', text: 'full-file fallback' }) : null,
  ].filter(Boolean)));
  if (data.rationale) card.appendChild(el('p', { class: 'dr-soft', text: data.rationale }));
  if (data.baseline == null && data.next == null) {
    card.appendChild(empty('No baseline or patch content recorded for this site.'));
    return card;
  }
  card.appendChild(svg.sideBySideDiff({
    baseline: data.baseline != null ? data.baseline : '',
    challenger: data.next != null ? data.next : '',
    leftLabel: 'champion baseline · v0',
    rightLabel: 'challenger new · ' + genId,
  }));
  return card;
}

function entryDrilldown(ctx, genId, entryId, row, exps, judges) {
  const card = el('div', { class: 'dr-panel dr-drill' });
  card.appendChild(el('div', { class: 'dr-row' }, [
    stat(row && svg.isNum(row.drift_loss) ? svg.fmt(row.drift_loss, 1) : '—', 'drift loss'),
    stat(row ? passLabel(row.pass_fail) : '—', 'predicate'),
    stat(row && row.wall_clock_budget_exceeded ? 'timed out' : (row && svg.isNum(row.runtime_ms) ? `${(row.runtime_ms / 1000).toFixed(0)}s` : '—'), 'runtime'),
  ]));
  const outcomes = (exps && Array.isArray(exps.outcomes)) ? exps.outcomes : [];
  if (outcomes.length) {
    const grid = el('div', { class: 'dr-expect-grid', style: 'margin-top:12px;' });
    for (const o of outcomes) {
      const cls = o.passed === true ? 'dr-good' : o.passed === false ? 'dr-bad' : 'dr-flat';
      grid.appendChild(el('div', { class: 'dr-expect-row' }, [
        el('span', { class: 'dr-expect-dot ' + cls }),
        el('span', { class: 'dr-expect-kind', text: o.kind || 'expectation' }),
        o.judge_name ? el('span', { class: 'dr-faint', text: ' · ' + o.judge_name }) : null,
        el('span', { class: 'dr-expect-detail dr-faint', text: o.detail ? ' — ' + o.detail : '' }),
      ].filter(Boolean)));
    }
    card.appendChild(grid);
  } else {
    card.appendChild(el('div', { style: 'margin-top:12px;' }, [empty('No expectation recorded for this entry.')]));
  }
  const jrows = (judges && Array.isArray(judges.judges)) ? judges.judges : [];
  const jitems = jrows.filter((j) => svg.isNum(j.weighted_loss)).sort((a, b) => b.weighted_loss - a.weighted_loss).map((j) => ({ label: j.judge_name, value: j.weighted_loss }));
  if (jitems.length) {
    card.appendChild(el('p', { class: 'dr-faint', style: 'margin:14px 0 4px;font-size:11px;', text: 'per-judge weighted process-drift loss · higher = more drift' }));
    card.appendChild(svg.valueBars({ width: 420, rowHeight: 20, labelWidth: 180, items: jitems }));
  }
  card.appendChild(el('div', { style: 'margin-top:14px;' }, [
    el('button', { class: 'dr-linkbtn', type: 'button', onClick: () => ctx.navigate({ section: 'boards', entry: entryId, runGen: genId }), text: 'See this run side-by-side on the board →' }),
  ]));
  return section('Entry · ' + entryId, card);
}

function baselineContent(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const b = payload.baseline;
  if (b && typeof b === 'object' && typeof b.content === 'string') return b.content;
  if (typeof payload.baseline_content === 'string') return payload.baseline_content;
  return null;
}
function siteSub(m) {
  const file = m.file || '';
  const span = (svg.isNum(m.line_start)) ? `:${m.line_start}${svg.isNum(m.line_end) && m.line_end !== m.line_start ? '-' + m.line_end : ''}` : '';
  const k = m.kind ? ` (${m.kind})` : '';
  return (file + span + k).trim();
}
function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}

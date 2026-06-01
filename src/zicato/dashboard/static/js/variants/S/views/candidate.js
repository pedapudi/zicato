// variants/S/views/candidate.js — CANDIDATE, comparison-first.
//
// The signature S screen. One candidate's lifecycle reads on the left; a
// "compare with…" picker SPLITS the detail into TWO candidates side by side —
// lifecycle · promote gate · all match-ups · per-board scoring, A | B. Each
// side gets its own digest-gated host so one side changing never rebuilds the
// other.
//
// Mandatory fixes carried on every candidate panel:
//   #1 the STACKED promote gate (rules each on their own row + a separate
//      champion-vs-challenger scalar-components block) — N lacked it.
//   #2 the lifecycle PATCH node is clickable → this candidate's SIDE-BY-SIDE
//      patch diff (its own patches + the site baseline.content), inline.
//   #3 ALL match-ups the candidate was in (champion OR challenger), not one.
//
// Data: /api/epoch, /api/lineage, /api/score-trajectory, /api/tournaments,
// /api/generation/{e}/{g}/per-entry, /api/round/{e}/{champ}/{chall}/gate,
// /api/run/{e}/{g}/{entry}/{expectations,per-judge}, /api/matchup-grid/…,
// /api/mutations/{e}/{id} (.baseline.content), /api/files/{e}/{g}/patches.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import * as model from '../model.js';
import { lifecycleDag } from '../dag.js';
import { gatedSwap, section, subhead, empty, stat, verdictPill, normaliseDecision } from '../ui.js';
import { comparePicker, splitFrame } from '../compare.js';

export async function render(host, ctx, route) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading candidate…' }));
  const p = route.params || {};

  const [ep, { gens, championId, scalarByGen, experiments }] = await Promise.all([D.epoch(), model.generations()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Candidate' }), empty('No current epoch.')]);
    return;
  }
  const epochId = p.epochId || ep.epoch_id;
  const allIds = gens.map((g) => g.id);
  const genId = (p.gen && allIds.includes(p.gen)) ? p.gen : (allIds[allIds.length - 1] || p.gen || null);
  if (!genId) {
    gatedSwap(host, 'no-cand', () => [el('h1', { class: 'dn-h1', text: 'Candidate' }), empty('No candidate selected.')]);
    return;
  }
  const cmpId = (route.cmp && allIds.includes(route.cmp) && route.cmp !== genId) ? route.cmp : null;

  // Resolve BOTH sides' data (cached). Side B only when cmpId is set.
  const sideA = await resolveCandidate(epochId, genId, gens, championId, scalarByGen, experiments, p);
  const sideB = cmpId ? await resolveCandidate(epochId, cmpId, gens, championId, scalarByGen, experiments, {}) : null;

  const digest = JSON.stringify({
    epochId, genId, cmpId, sub: p.sub || null, entry: p.entry || null,
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
      a: { title: genId + (sideA.node.promoted ? ' ♛' : ''), sub: sideA.decision, build: (h) => paintCandidate(h, ctx, epochId, sideA, p, cmpId) },
      b: cmpId ? { title: cmpId + (sideB.node.promoted ? ' ♛' : ''), sub: sideB.decision, build: (h) => paintCandidate(h, ctx, epochId, sideB, {}, null) } : null,
      emptyTitle: 'no comparison',
      emptyPrompt: 'Choose a candidate above to compare its lifecycle, gate, match-ups and per-board scoring against ' + genId + '.',
    }));
    return nodes;
  });
}

// Resolve one candidate's full panel data (all cached reads).
async function resolveCandidate(epochId, genId, gens, championId, scalarByGen, experiments, p) {
  const node = gens.find((g) => g.id === genId) || { id: genId, parent: null, promoted: false };
  const baseline = !node.parent;
  const decision = model.decisionFor(node, experiments);

  const pe = await D.perEntry(epochId, genId);
  const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];

  let gate = null;
  if (!baseline && node.parent) gate = await D.gate(epochId, node.parent, genId);

  // ALL match-ups this candidate is in (fix #3).
  const matchups = await model.matchupsFor(genId);

  // patch diff (fix #2) — resolved when the patch sub-node is selected for the
  // PRIMARY candidate.
  let patch = null;
  if (p && p.sub === 'patch' && !baseline) {
    patch = await resolvePatch(epochId, genId);
  }

  // entry drill (per-board scoring → one entry)
  let drill = null;
  if (p && p.sub === 'entry' && p.entry) {
    const [exps, judges] = await Promise.all([
      D.expectations(epochId, genId, p.entry), D.perJudgeForRun(epochId, genId, p.entry),
    ]);
    drill = { entry: p.entry, row: entries.find((e) => e.entry_id === p.entry) || null, exps, judges };
  }

  return { node, baseline, decision, entries, gate, matchups, patch, drill, championId, scalarByGen };
}

// This candidate's OWN side-by-side patch diff: its patches + each site's
// baseline.content (the STRING — never the baseline object).
async function resolvePatch(epochId, genId) {
  const [pp, muts] = await Promise.all([D.patches(epochId, genId), D.mutations(epochId)]);
  const patches = (pp && Array.isArray(pp.patches)) ? pp.patches : [];
  const sites = (muts && Array.isArray(muts.mutations)) ? muts.mutations : [];
  const siteById = new Map(sites.map((s) => [s.mutation_id, s]));
  const blocks = [];
  for (const patch of patches) {
    const detail = await D.mutationDetail(epochId, patch.mutation_id);
    const baseline = baselineContent(detail) || baselineContent(siteById.get(patch.mutation_id));
    blocks.push({
      mutationId: patch.mutation_id,
      site: siteById.get(patch.mutation_id) || null,
      baseline: baseline != null ? String(baseline) : '',
      next: patch.new_content != null ? String(patch.new_content) : '',
      op: patch.op || 'edit', rationale: patch.rationale || '',
    });
  }
  return { blocks };
}

function baselineContent(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const b = payload.baseline;
  if (b && typeof b === 'object' && typeof b.content === 'string') return b.content;
  if (typeof payload.baseline_content === 'string') return payload.baseline_content;
  return null;
}

function candidateDigest(s) {
  return {
    gen: s.node.id, parent: s.node.parent, decision: s.decision,
    scalar: s.scalarByGen.has(s.node.id) ? s.scalarByGen.get(s.node.id).toFixed(3) : null,
    delta: s.gate && svg.isNum(s.gate.delta_scalar) ? s.gate.delta_scalar.toFixed(3) : null,
    entries: s.entries.map((e) => [e.entry_id, svg.isNum(e.drift_loss) ? e.drift_loss.toFixed(3) : null, e.pass_fail, !!e.wall_clock_budget_exceeded]),
    matchups: s.matchups.map((m) => [m.champion, m.challenger, m.decision]),
    patch: s.patch ? s.patch.blocks.map((b) => [b.mutationId, b.baseline.length, b.next.length, b.op]) : null,
    drill: s.drill ? [s.drill.entry, s.drill.exps && Array.isArray(s.drill.exps.outcomes) ? s.drill.exps.outcomes.map((o) => [o.kind, o.passed]) : null] : null,
    gateRules: s.gate && Array.isArray(s.gate.rules) ? s.gate.rules.map((r) => [r.id, r.status, r.fired]) : null,
  };
}

// Paint one candidate's full lifecycle panel into `host`. `cmpId`, when set,
// is the comparison target to PRESERVE when the user drills into a sub-node.
function paintCandidate(host, ctx, epochId, s, p, cmpId) {
  const opts = cmpId ? { cmp: cmpId } : undefined;
  const node = s.node;
  const genId = node.id;
  const championId = s.championId;
  const championScalar = championId ? s.scalarByGen.get(championId) : null;
  const deltaScalar = s.gate && svg.isNum(s.gate.delta_scalar) ? s.gate.delta_scalar : null;

  // headline stats
  host.appendChild(el('div', { class: 'dn-panel dn-row' }, [
    stat(svg.isNum(s.scalarByGen.get(genId)) ? svg.fmt(s.scalarByGen.get(genId), 1) : '—', 'scalar (loss)'),
    stat(svg.isNum(deltaScalar) ? svg.fmtSigned(deltaScalar, 1) : '—', 'Δ vs champion'),
    stat(node.parent || 'seed', 'parent'),
    el('div', { class: 'dn-stat' }, [verdictPill(s.baseline ? 'baseline' : s.decision)]),
  ]));

  // lifecycle DAG — the PATCH node routes to this candidate's patch diff (fix #2).
  const dagCard = el('div', { class: 'dn-panel', style: 'overflow-x:auto;' });
  dagCard.appendChild(lifecycleDag({
    genId, parentId: node.parent, baseline: s.baseline, promoted: node.promoted, decision: s.decision,
    deltaScalar, patchPoints: s.patch ? s.patch.blocks.length : null, entries: s.entries,
    width: 560, height: Math.max(260, 120 + s.entries.length * 30),
    onEntry: (eid) => ctx.navigate('candidate', { epochId, gen: genId, sub: 'entry', entry: eid }, opts),
    onPatch: s.baseline ? null : () => ctx.navigate('candidate', { epochId, gen: genId, sub: 'patch' }, opts),
  }));
  const patchLink = s.baseline ? null : el('a', {
    class: 'dn-linkbtn', style: 'margin-top:8px;',
    href: ctx.href('candidate', { epochId, gen: genId, sub: 'patch' }, opts),
    text: 'Open this candidate’s patch diff →',
    onclick: (ev) => { ev.preventDefault(); ctx.navigate('candidate', { epochId, gen: genId, sub: 'patch' }, opts); },
  });
  if (patchLink) dagCard.appendChild(patchLink);
  host.appendChild(section('Lifecycle · cause → effect → verdict', dagCard));

  // PATCH DIFF — inline, side-by-side, when the patch node is selected (fix #2).
  if (s.patch) {
    host.appendChild(section('Patch · ' + genId + ' over ' + (node.parent || 'seed') + ' — side by side', patchPanel(s.patch, ctx, epochId, genId)));
  }

  // PROMOTE GATE — stacked, no overlap (fix #1).
  if (s.gate && Array.isArray(s.gate.rules) && s.gate.rules.length) {
    host.appendChild(section('Promote gate · ' + (node.parent || '?') + ' → ' + genId, gatePanel(s.gate, { champion: node.parent, challenger: genId })));
  } else if (s.baseline) {
    host.appendChild(section('Promote gate', el('div', { class: 'dn-panel' }, [empty('The seed candidate has no gate — it defines the loss floor that challengers must beat.')])));
  }

  // ALL MATCH-UPS for this candidate (fix #3).
  host.appendChild(section('Match-ups · every round this candidate was in', matchupsPanel(s.matchups, genId, ctx, epochId)));

  // per-board scoring dot-plot
  const scoreCard = el('div', { class: 'dn-panel' });
  if (s.entries.length) {
    const items = s.entries.filter((e) => svg.isNum(e.drift_loss)).sort((a, b) => b.drift_loss - a.drift_loss)
      .map((e) => ({ label: e.entry_id, value: e.drift_loss, id: e.entry_id, pass: e.pass_fail, timeout: !!e.wall_clock_budget_exceeded }));
    scoreCard.appendChild(svg.valueDotPlot({
      width: 480, rowHeight: 21, labelWidth: 180, items,
      reference: svg.isNum(championScalar) ? { value: championScalar, label: `champion ${championId}` } : null,
      onClick: (it) => ctx.navigate('candidate', { epochId, gen: genId, sub: 'entry', entry: it.id }),
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

  // entry drill-down
  if (s.drill) host.appendChild(entryDrilldown(ctx, epochId, genId, s.drill));
}

function patchPanel(patch, ctx, epochId, genId) {
  const card = el('div', { class: 'dn-panel dn-mut-detail' });
  if (!patch.blocks.length) {
    card.appendChild(empty('No patch recorded for this candidate (it may be the seed, or the index is not built).'));
    return card;
  }
  for (const b of patch.blocks) {
    const block = el('div', { class: 'dn-patch-block' });
    block.appendChild(el('div', { class: 'dn-patch-head' }, [
      el('span', { class: 'dn-mono', text: (b.site && (b.site.role || b.site.file)) || b.mutationId }),
      el('span', { class: 'dn-patch-op dn-mono', text: b.op }),
    ]));
    if (b.rationale) block.appendChild(el('p', { class: 'dn-patch-why' }, [el('span', { class: 'dn-patch-why-lead', text: 'Why. ' }), b.rationale]));
    if (b.baseline === '' && b.next === '') {
      block.appendChild(empty('No baseline or patch content recorded for this site.'));
    } else {
      block.appendChild(svg.sideBySideDiff({
        baseline: b.baseline, challenger: b.next,
        leftLabel: 'champion baseline', rightLabel: 'challenger new · ' + genId,
      }));
    }
    card.appendChild(block);
  }
  return card;
}

function matchupsPanel(matchups, genId, ctx, epochId) {
  const card = el('div', { class: 'dn-panel' });
  if (!matchups.length) {
    card.appendChild(empty('This candidate was in no recorded match-up.'));
    return card;
  }
  const tbl = el('table', { class: 'dn-board-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', { text: 'role' }), el('th', { text: 'champion' }), el('th', { text: 'challenger' }),
    el('th', { class: 'dn-num', text: 'Δ scalar' }), el('th', { text: 'decision' }),
  ])]));
  const tbody = el('tbody');
  for (const m of matchups) {
    const role = m.champion === genId ? 'champion' : 'challenger';
    const dec = m.decision || '—';
    const tr = el('tr', { class: m.champion === genId ? 'dn-board-champ' : '' }, [
      el('td', { text: role }),
      el('td', { class: 'dn-mono', text: m.champion }),
      el('td', { class: 'dn-mono', text: m.challenger }),
      el('td', { class: 'dn-num dn-mono', text: svg.isNum(m.delta_scalar) ? svg.fmtSigned(m.delta_scalar, 1) : '—' }),
      el('td', null, [el('span', { class: 'dn-pill dn-' + dec, text: dec })]),
    ]);
    // clicking a match-up row compares the two candidates side by side.
    tr.style.cursor = 'pointer';
    const other = m.champion === genId ? m.challenger : m.champion;
    tr.addEventListener('click', () => ctx.navigate('candidate', { epochId, gen: genId }, { cmp: other }));
    tbody.appendChild(tr);
  }
  tbl.appendChild(tbody);
  card.appendChild(tbl);
  card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'every round this candidate fought — champion AND challenger · click a row → compare the two candidates side by side' }));
  return card;
}

// The stacked, non-overlapping promote gate (fix #1).
function gatePanel(gate, m) {
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
      card.appendChild(subhead(`Scalar components · champion ${m.champion} vs challenger ${m.challenger}`));
      const tbl = el('table', { class: 'dn-sc-table' });
      tbl.appendChild(el('thead', null, [el('tr', null, [
        el('th', { text: 'component' }), el('th', { class: 'dn-num', text: m.champion }),
        el('th', { class: 'dn-num', text: m.challenger }), el('th', { class: 'dn-num', text: 'Δ' }),
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
    card.appendChild(el('div', { style: 'margin-top:12px;' }, [empty('No expectation recorded for this entry.')]));
  }
  // to read the transcript head-to-head, route to the board view's inline diff.
  card.appendChild(el('div', { style: 'margin-top:14px;' }, [
    el('a', { class: 'dn-linkbtn', href: ctx.href('board', { epochId, entry }, { runs: [genId] }),
      text: 'See this board across candidates · side-by-side transcripts →',
      onclick: (ev) => { ev.preventDefault(); ctx.navigate('board', { epochId, entry }, { runs: [genId] }); } }),
  ]));
  return section('Entry · ' + entry, card);
}

function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}

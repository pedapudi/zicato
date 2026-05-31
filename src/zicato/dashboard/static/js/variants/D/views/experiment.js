// variants/D/views/experiment.js — CODE CHANGE → DRIFT → VERDICT, led by visuals.
//
// One generation's experiment, told visual-first:
//   1. PREDICTED-vs-ACTUAL small multiples — for each quantity the
//      proposer bet on (scalar, pass-rate, drift), a bullet showing the
//      predicted target vs the actual outcome. Did the bet pay off?
//   2. DRIFT-BY-KIND slopegraph — champion → challenger per-kind drift
//      counts, non-colliding.
//   3. PER-ENTRY deltas as a sorted dot plot (improved entries teal,
//      regressed rose), click → that run.
//   4. The GATE as a compact visual (challenger vs champion scalar on a
//      margin track).
//   5. The patch DIFF, secondary and collapsible — never the lead.
//
// Data: /api/epoch (experiment record incl. hypothesis/outcome/patches),
// /api/matchup-grid/{e}/{champ}/{chall}, /api/drift-movements/{g},
// /api/files/{e}/{g}/diff. A v0 seed shows its baseline board results.

import { el, clearChildren } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { section, crumb, empty, loading, stat, verdictPill, normaliseDecision } from '../ui.js';

export async function render(host, ctx, params) {
  clearChildren(host);
  const genId = params && params.gen;
  host.appendChild(crumb([
    { label: 'environment', view: 'environment' },
    { label: 'epoch', view: 'epoch' },
    { label: genId ? `experiment ${genId}` : 'experiment' },
  ]));
  const head = el('div'); host.appendChild(head);
  const body = el('div'); host.appendChild(body);
  body.appendChild(loading('Reading experiment…'));

  const ep = await D.epoch();
  if (!ep || ep.epoch_id == null) { clearChildren(body); body.appendChild(empty('No epoch loaded.')); return; }
  const epochId = ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const exp = experiments.find((x) => x.generation_id === genId) || experiments[experiments.length - 1];
  clearChildren(body);
  if (!exp) { head.appendChild(el('h1', { class: 'd-h1', text: 'Experiment' })); body.appendChild(empty('No experiments recorded.')); return; }

  const gid = exp.generation_id;
  const parentId = exp.parent_generation_id || null;
  const hyp = exp.hypothesis || {};
  const outcome = exp.outcome || {};
  const decision = normaliseDecision(outcome);
  const isSeed = !parentId;

  head.appendChild(el('h1', { class: 'd-h1' }, [
    `Experiment ${gid}`, ' ', verdictPill(isSeed ? 'baseline' : decision),
  ]));
  head.appendChild(el('p', { class: 'd-lede',
    text: isSeed
      ? 'The v0 seed — its absolute baseline board results, no parent to compare against.'
      : `Derived from ${parentId} by a patch. The cause → the drift → the gate's verdict.` }));
  if (hyp.core_idea) {
    head.appendChild(el('p', { class: 'd-soft', style: 'font-size:13px;max-width:64ch;line-height:1.5;margin:4px 0 0;',
      text: '“' + hyp.core_idea + '”' }));
  }

  // Fetch the comparison substrate.
  const [grid, movements, diff] = await Promise.all([
    isSeed ? Promise.resolve(null) : D.matchupGrid(epochId, parentId, gid),
    isSeed ? Promise.resolve(null) : D.driftMovements(gid),
    D.diff(epochId, gid),
  ]);

  // ---- 1. predicted vs actual small multiples ----
  if (!isSeed) {
    const pvaGrid = el('div', { class: 'd-sm-grid' });
    const cards = [
      { label: 'scalar Δ', predicted: hyp.expected_scalar_delta, actual: outcome.scalar_score_delta },
      { label: 'pass-rate Δ', predicted: hyp.expected_pass_rate_delta, actual: outcome.pass_rate_delta, good: 'up' },
      { label: 'drift-loss Δ', predicted: hyp.expected_drift_loss_delta, actual: outcome.drift_loss_delta },
    ];
    let anyPva = false;
    for (const c of cards) {
      if (!svg.isNum(c.predicted) && !svg.isNum(c.actual)) continue;
      anyPva = true;
      // The label lives on the smallMultiple caption; the mark itself
      // stays unlabelled so the caption is not duplicated.
      const mark = svg.predictedActual({
        width: 168, height: 30, predicted: c.predicted, actual: c.actual,
        goodDirection: c.good || 'down',
      });
      const sub = svg.isNum(c.actual) ? svg.fmtSigned(c.actual) : '—';
      pvaGrid.appendChild(svg.smallMultiple(c.label, mark, sub));
    }
    const pvaCard = el('div', { class: 'd-panel' });
    if (anyPva) {
      pvaCard.appendChild(pvaGrid);
      pvaCard.appendChild(el('div', { class: 'd-legend' }, [
        el('span', null, [el('i', { class: 'dotpred' }), 'predicted (the bet)']),
        el('span', null, [el('i', { class: 'dotact' }), 'actual (the outcome)']),
        el('span', { class: 'd-faint', text: 'dashed = prediction error' }),
      ]));
    } else {
      pvaCard.appendChild(empty('No quantified predictions recorded for this experiment.'));
    }
    body.appendChild(section('Predicted vs actual', pvaCard));
  }

  // ---- 2. drift-by-kind slopegraph ----
  if (!isSeed) {
    const dmCard = el('div', { class: 'd-panel' });
    const moves = (movements && Array.isArray(movements.movements)) ? movements.movements : [];
    if (moves.length) {
      const series = moves.map((m) => ({
        label: m.kind, id: m.kind, a: m.champion_count, b: m.challenger_count,
      }));
      dmCard.appendChild(svg.slopegraph({
        width: 520, height: Math.max(160, 40 + series.length * 22),
        left: { title: 'champion' }, right: { title: 'challenger' },
        labelGap: 150, goodDirection: 'down', series,
      }));
      dmCard.appendChild(el('div', { class: 'd-legend' }, [
        el('span', null, [el('i', { class: 'good' }), 'fewer drift events (improved)']),
        el('span', null, [el('i', { class: 'bad' }), 'more drift events (worsened)']),
      ]));
    } else {
      dmCard.appendChild(empty(movements && movements.note ? movements.note : 'No drift-kind movements recorded.'));
    }
    body.appendChild(section('Drift by kind · champion → challenger', dmCard));
  }

  // ---- 3. per-entry deltas (sorted dot plot) / seed baseline ----
  const entryCard = el('div', { class: 'd-panel' });
  if (isSeed) {
    // Baseline board results: each entry's absolute drift loss.
    const pe = await D.perEntry(epochId, gid);
    const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];
    if (entries.length) {
      const items = entries
        .filter((e) => svg.isNum(e.drift_loss))
        .sort((a, b) => b.drift_loss - a.drift_loss)
        .map((e) => ({ label: e.entry_id, value: e.drift_loss, id: e.entry_id }));
      entryCard.appendChild(svg.dotPlot({
        width: 560, rowHeight: 20, labelWidth: 170, goodDirection: 'down',
        valueFmt: (v) => svg.fmt(v), items,
        onClick: (it) => ctx.navigate('run', { entry: it.id }),
      }));
      entryCard.appendChild(el('p', { class: 'd-faint', style: 'font-size:11px;margin:8px 0 0;',
        text: 'absolute drift loss per board entry · lower is better · click → run' }));
    } else {
      entryCard.appendChild(empty('No baseline board results yet.'));
    }
    body.appendChild(section('Baseline board results', entryCard));
  } else {
    const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
    const items = rows
      .filter((r) => svg.isNum(r.delta))
      .sort((a, b) => a.delta - b.delta)
      .map((r) => ({ label: r.entry_id, value: r.delta, id: r.entry_id }));
    if (items.length) {
      entryCard.appendChild(svg.dotPlot({
        width: 560, rowHeight: 20, labelWidth: 170, goodDirection: 'down', items,
        onClick: (it) => ctx.navigate('run', { entry: it.id }),
      }));
      entryCard.appendChild(el('p', { class: 'd-faint', style: 'font-size:11px;margin:8px 0 0;',
        text: 'Δ drift loss vs champion · left/teal = entry improved · right/rose = regressed · click → run' }));
    } else {
      entryCard.appendChild(empty('No per-entry comparison available.'));
    }
    body.appendChild(section('Per-entry deltas', entryCard));
  }

  // ---- 4. the gate (compact visual) ----
  if (!isSeed && grid && grid.scalar) {
    const sc = grid.scalar;
    const gateCard = el('div', { class: 'd-panel' });
    const pScalar = sc.parent; const cScalar = sc.child; const delta = sc.delta;
    // Track domain: span both scalars with a little padding.
    const lo = Math.min(svg.isNum(pScalar) ? pScalar : 0, svg.isNum(cScalar) ? cScalar : 0);
    const hi = Math.max(svg.isNum(pScalar) ? pScalar : 1, svg.isNum(cScalar) ? cScalar : 1);
    const span = (hi - lo) || 1;
    const pos = (v) => `${(((v - lo) / span) * 100)}%`;
    const track = el('div', { class: 'd-gate-track' });
    if (svg.isNum(pScalar)) {
      const m = el('div', { class: 'd-gate-marker', style: `left:${pos(pScalar)};` });
      m.title = `champion ${svg.fmt(pScalar)}`;
      track.appendChild(m);
    }
    if (svg.isNum(cScalar)) {
      const improved = svg.isNum(delta) ? delta < 0 : false;
      const m = el('div', { class: 'd-gate-marker ' + (improved ? 'd-good' : 'd-bad'), style: `left:${pos(cScalar)};` });
      m.title = `challenger ${svg.fmt(cScalar)}`;
      track.appendChild(m);
    }
    gateCard.appendChild(el('div', { class: 'd-gate' }, [
      stat(svg.fmt(pScalar), 'champion'),
      track,
      stat(svg.fmt(cScalar), 'challenger'),
    ]));
    gateCard.appendChild(el('div', { class: 'd-row', style: 'margin-top:8px;' }, [
      stat(svg.fmtSigned(delta), 'Δ scalar'),
      el('div', null, [verdictPill(decision)]),
      outcome.rejection_reason
        ? el('div', { class: 'd-soft', style: 'font-size:12px;max-width:48ch;', text: outcome.rejection_reason })
        : null,
    ]));
    // per-component breakdown as a dot plot when present.
    if (sc.components && Object.keys(sc.components).length) {
      const items = Object.entries(sc.components)
        .map(([k, v]) => ({ label: k, value: v, id: k }))
        .sort((a, b) => a.value - b.value);
      gateCard.appendChild(el('div', { style: 'margin-top:12px;' }, [
        el('div', { class: 'd-faint', style: 'font-size:11px;margin-bottom:4px;', text: 'scalar components · Δ champion → challenger' }),
        svg.dotPlot({ width: 460, rowHeight: 18, labelWidth: 150, goodDirection: 'down', items }),
      ]));
    }
    body.appendChild(section('The gate', gateCard));
  }

  // ---- 5. the patch diff (secondary, collapsible) ----
  const diffDetails = el('details', { class: 'd-brief' });
  diffDetails.appendChild(el('summary', null, [
    el('span', { class: 'chev', text: '▸' }), 'Patch & code diff',
    el('span', { class: 'd-faint', style: 'font-weight:400;font-size:11px;',
      text: diff && Array.isArray(diff.files) ? `· ${diff.files.length} files` : '· —' }),
  ]));
  const diffBody = el('div', { class: 'd-brief-body', style: 'max-width:none;' });
  // rationale lines from the experiment's patches
  const patches = exp.patches && typeof exp.patches === 'object' ? Object.values(exp.patches) : [];
  if (patches.length) {
    const ul = el('ul');
    for (const p of patches) {
      ul.appendChild(el('li', null, [
        el('code', { text: p.mutation_id || p.id || 'patch' }), ' — ',
        p.rationale || p.op || '(no rationale)',
      ]));
    }
    diffBody.appendChild(el('div', { style: 'margin-bottom:10px;' }, [
      el('div', { class: 'd-faint', style: 'font-size:11px;margin-bottom:4px;', text: 'mutation rationale' }), ul,
    ]));
  }
  if (diff && Array.isArray(diff.files) && diff.files.length) {
    for (const f of diff.files) {
      const fd = el('details', { class: 'd-diff-file' });
      fd.appendChild(el('summary', null, [
        el('span', { class: `d-diff-status ${f.status}`, text: f.status }),
        el('span', { class: 'd-diff-path', text: f.path }),
      ]));
      fd.appendChild(el('div', { class: 'd-diff-body d-diff' }, [
        el('div', null, [
          el('div', { class: 'd-diff-colhead', text: 'before' }),
          el('div', { class: 'd-diff-col old', text: f.old_binary ? '(binary)' : (f.old_content || '') }),
        ]),
        el('div', null, [
          el('div', { class: 'd-diff-colhead', text: 'after' }),
          el('div', { class: 'd-diff-col new', text: f.new_binary ? '(binary)' : (f.new_content || '') }),
        ]),
      ]));
      diffBody.appendChild(fd);
    }
  } else {
    diffBody.appendChild(el('p', { class: 'd-faint', text: isSeed ? 'The seed has no parent diff — every file is the baseline.' : 'No file changes recorded.' }));
  }
  diffDetails.appendChild(diffBody);
  body.appendChild(section('The cause (code change)', diffDetails));
}

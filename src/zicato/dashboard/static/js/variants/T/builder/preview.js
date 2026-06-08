// variants/T/builder/preview.js — the live-PREVIEW renderer, extracted so it is
// reusable beyond the builder view.
//
// The builder view (views/builder.js) drives this from its live `/builder/op`
// envelope (the structure schematic + a cost meter + a board/holdout strip + a
// contract-impact pill + validation warnings, all computed server-side). The
// SAME renderer is reused READ-ONLY by Settings → Contract, bound to the frozen
// current contract (`/api/epoch`) — the frozen contract is the same shape, and
// the cost / validation are recomputed CLIENT-SIDE (builder/model.js's pure
// estimateCost / validateContract) from the contract alone, so the read-only
// surface needs no `/builder/op` round-trip.
//
// One node array per render; the caller owns the host + the digest gate (so a
// no-op heartbeat re-dispatch never rebuilds the DOM — render discipline).

import { el } from '../../../core/dom.js';
import * as svg from '../svg.js';
import { schematicModel } from './model.js';

// Build the preview node array from a normalized model:
//   {
//     structure, params,            // the tournament structure + its params
//     cost,                          // { board_runs_per_round, breakdown:[…] }
//     warnings,                      // [{ code, message, severity }]
//     boardCount, trainCount, holdoutCount,
//     diff,                          // OPTIONAL — the contract-impact diff
//                                    //   ({ changed_components, rolls_epoch });
//                                    //   omit for a frozen, read-only contract.
//     readonly,                      // true ⇒ no impact pill, a "current" note
//     heading,                       // the figure heading text
//   }
// The caller appends these to its own host through a digest gate.
export function previewNodes(model) {
  const m = model || {};
  const structure = m.structure || 'gauntlet';
  const params = m.params || {};
  const cost = m.cost || {};
  const warnings = Array.isArray(m.warnings) ? m.warnings : [];
  const boardCount = m.boardCount || 0;
  const trainN = m.trainCount || 0;
  const holdN = m.holdoutCount || 0;
  const readonly = !!m.readonly;
  const nodes = [];

  nodes.push(el('h2', { class: 'dn-h2', text: m.heading || 'Live preview' }));

  // The contract-impact pill. The builder shows whether applying the DRAFT
  // would roll the epoch (from the live diff); a READ-ONLY contract preview
  // shows a quiet "current contract" note instead (there is nothing to apply).
  if (readonly) {
    nodes.push(el('div', { class: 'dn-bld-impact dn-bld-impact-current', title: 'the frozen contract this epoch is running on' }, [
      el('span', { class: 'dn-bld-impact-glyph', 'aria-hidden': 'true', text: '◷' }),
      el('span', { text: 'current contract · read-only' }),
    ]));
  } else {
    const diff = m.diff || {};
    const changed = diff.changed_components || [];
    nodes.push(el('div', {
      class: 'dn-bld-impact' + (diff.rolls_epoch ? ' dn-bld-impact-roll' : ''),
      title: diff.rolls_epoch ? 'applying this draft will roll the epoch' : 'no contract change',
    }, [
      el('span', { class: 'dn-bld-impact-glyph', 'aria-hidden': 'true', text: diff.rolls_epoch ? '⟳' : '=' }),
      el('span', { text: diff.rolls_epoch ? ('rolls epoch · changed: ' + (changed.length ? changed.join(', ') : 'contract')) : 'no contract change' }),
    ]));
  }

  // The per-structure schematic — REUSED from svg.js so the preview and the
  // runtime view speak one visual language.
  const sm = schematicModel(structure, params, boardCount);
  let fig;
  if (structure === 'racing') fig = svg.survivalFunnel(sm);
  else if (structure === 'swiss') fig = svg.swissLadder(sm);
  else if (structure === 'single_elim' || structure === 'double_elim') fig = svg.elimFlow(sm);
  else fig = svg.valueDotPlot(sm); // gauntlet → the Δ dot-plot
  nodes.push(el('div', { class: 'dn-bld-figure' }, [fig]));

  // The cost meter.
  nodes.push(costMeter(cost));

  // The board / holdout strip.
  nodes.push(el('div', {
    class: 'dn-bld-previewstrip', role: 'img',
    'aria-label': `board ${boardCount} · train ${trainN} · holdout ${holdN}`,
  }, [
    el('span', { class: 'dn-bld-split-train', style: `flex:${Math.max(1, trainN)}`, text: `train ${trainN}` }),
    el('span', { class: 'dn-bld-split-hold', style: `flex:${Math.max(0.001, holdN)}`, text: `holdout ${holdN}` }),
  ]));

  // Validation warnings.
  if (warnings.length) {
    nodes.push(el('ul', { class: 'dn-bld-warnings' }, warnings.map((w) => el('li', {
      class: 'dn-bld-warn dn-bld-warn-' + (w.severity || 'warning'),
    }, [
      el('span', { class: 'dn-bld-warn-glyph', 'aria-hidden': 'true', text: w.severity === 'info' ? 'ⓘ' : '⚠' }),
      el('span', { class: 'dn-bld-warn-msg', text: w.message || w.code || '' }),
    ]))));
  } else {
    nodes.push(el('p', { class: 'dn-faint', text: 'No validation warnings.' }));
  }
  return nodes;
}

export function costMeter(cost) {
  const c = cost || {};
  const runs = c.board_runs_per_round != null ? c.board_runs_per_round : 0;
  const breakdown = Array.isArray(c.breakdown) ? c.breakdown : [];
  const wrap = el('div', { class: 'dn-bld-cost' });
  wrap.appendChild(el('div', { class: 'dn-bld-cost-head' }, [
    el('span', { class: 'dn-bld-cost-num', text: String(runs) }),
    el('span', { class: 'dn-bld-cost-lab', text: 'board-runs / round' }),
  ]));
  const total = Math.max(1, runs);
  wrap.appendChild(el('div', { class: 'dn-bld-cost-bars' }, breakdown.map((line) => el('div', {
    class: 'dn-bld-cost-bar', title: `${line.label}: ${line.runs} · ${line.detail || ''}`,
  }, [
    el('span', { class: 'dn-bld-cost-barfill', style: `width:${Math.round((line.runs / total) * 100)}%` }),
    el('span', { class: 'dn-bld-cost-barlab', text: `${line.label} · ${line.runs}` }),
  ]))));
  return wrap;
}

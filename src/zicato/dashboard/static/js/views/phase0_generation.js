// views/phase0_generation.js — L2 (generation-level) view.
//
// Renders the per-generation shell: hypothesis (proposed-before /
// outcome-after split), patches list, per-entry breakdown, per-judge
// breakdown (stub until #179 lands), and a compare-to-v? picker. Pulls
// from the already-populated ``state.epochDef.experiments`` payload so
// no additional fetch is needed for the minimal phase-0 render.

import { $, el, clearChildren } from '../core/dom.js';
import { state } from '../core/state.js';

function findExperiment(generationId) {
  const def = state.epochDef;
  if (!def || !Array.isArray(def.experiments)) return null;
  for (const exp of def.experiments) {
    if (exp && exp.generation_id === generationId) return exp;
  }
  return null;
}

function renderHypothesis(exp) {
  const node = $('phase0-gen-hypothesis');
  if (!node) return;
  clearChildren(node);
  if (!exp) {
    node.appendChild(el('p', { class: 'empty' }, ['No hypothesis recorded.']));
    return;
  }
  const hyp = exp.hypothesis || {};
  const out = exp.outcome || {};
  const wrap = el('div', { class: 'phase0-hypothesis' });

  wrap.appendChild(el('h3', { class: 'phase0-h3' }, ['Proposed (before)']));
  if (hyp.core_idea) {
    wrap.appendChild(el('p', { class: 'phase0-hyp-core' }, [hyp.core_idea]));
  } else {
    wrap.appendChild(el('p', { class: 'empty' }, ['No core idea recorded.']));
  }
  if (hyp.why) wrap.appendChild(el('p', null, [el('strong', null, ['why. ']), hyp.why]));
  if (hyp.risks) wrap.appendChild(el('p', null, [el('strong', null, ['risks. ']), hyp.risks]));

  wrap.appendChild(el('h3', { class: 'phase0-h3' }, ['Outcome (after)']));
  if (out.summary) {
    wrap.appendChild(el('p', null, [out.summary]));
  } else if (out.tournament_decision || out.decision) {
    wrap.appendChild(el('p', null, [
      'decision: ',
      el('span', { class: 'mono' }, [String(out.tournament_decision || out.decision)]),
    ]));
  } else {
    wrap.appendChild(el('p', { class: 'empty' }, ['No outcome recorded.']));
  }
  node.appendChild(wrap);
}

function renderPatches(exp) {
  const node = $('phase0-gen-patches');
  if (!node) return;
  clearChildren(node);
  const patches = exp && exp.patches;
  // ``patches`` may be a mapping (mutation_id -> patch dict) or a list.
  let entries = [];
  if (patches && typeof patches === 'object' && !Array.isArray(patches)) {
    for (const k of Object.keys(patches)) entries.push({ id: k, patch: patches[k] });
  } else if (Array.isArray(patches)) {
    entries = patches.map((p, i) => ({ id: (p && p.mutation_id) || ('p' + i), patch: p }));
  }
  if (entries.length === 0) {
    node.appendChild(el('p', { class: 'empty' }, ['No patches recorded.']));
    return;
  }
  const tbl = el('table', { class: 'phase0-mini-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['mutation']),
    el('th', null, ['op']),
    el('th', null, ['rationale']),
  ])]));
  const tbody = el('tbody');
  for (const e of entries) {
    const p = e.patch || {};
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'mono' }, [e.id]),
      el('td', { class: 'mono' }, [String(p.op || p.kind || '—')]),
      el('td', null, [String(p.rationale || p.message || '—')]),
    ]));
  }
  tbl.appendChild(tbody);
  node.appendChild(tbl);
}

function renderEntries() {
  const node = $('phase0-gen-entries');
  if (!node) return;
  clearChildren(node);
  // Per-entry breakdown lives in the matchup detail — for phase 0 we
  // just hint at the data dimension; the full table lands once #180's
  // tournament_id FK is wired so each entry can be cross-referenced
  // against its tournament row.
  node.appendChild(el('p', { class: 'panel-subheader' },
    ['Per-entry breakdown lands once #180 wires the tournament_id FK.']));
}

function renderJudges() {
  const node = $('phase0-gen-judges');
  if (!node) return;
  clearChildren(node);
  node.appendChild(el('p', { class: 'empty phase0-stub-msg' },
    ['(per-judge breakdown — populated once #179 lands)']));
}

function renderCompare() {
  const node = $('phase0-gen-compare');
  if (!node) return;
  clearChildren(node);
  // The compare-to-v? picker is a phase-2 affordance; phase 0 only
  // surfaces the slot so the layout is honest.
  node.appendChild(el('p', { class: 'panel-subheader' },
    ['Compare-to-v? picker lands in phase 2.']));
}

export function renderPhase0Generation(params) {
  const exp = findExperiment(params && params.generationId);
  renderHypothesis(exp);
  renderPatches(exp);
  renderEntries();
  renderJudges();
  renderCompare();
}

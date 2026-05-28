// views/phase0_generation.js — L2 (generation-level) view.
//
// Renders the per-generation shell: hypothesis (proposed-before /
// outcome-after split), patches list, per-entry breakdown (via
// /api/generation/{epoch}/{gen}/per-entry — the tournament_id FK
// query), per-judge breakdown (via
// /api/generation/{epoch}/{gen}/per-judge), and a compare-to-v? picker
// (phase-2 affordance). Pulls hypothesis + patches from the already-
// populated ``state.epochDef.experiments`` payload; the new tables are
// lazy-fetched per generation key.

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';

const _perJudgeCache = new Map(); // "epoch/gen" -> payload
const _perEntryCache = new Map(); // "epoch/gen" -> payload
const _loadingJudges = new Set();
const _loadingEntries = new Set();

export function resetGenerationCaches() {
  _perJudgeCache.clear();
  _perEntryCache.clear();
  _loadingJudges.clear();
  _loadingEntries.clear();
}

export function perJudgePayload(epochId, generationId) {
  return _perJudgeCache.get(epochId + '/' + generationId) || null;
}

export function perEntryPayload(epochId, generationId) {
  return _perEntryCache.get(epochId + '/' + generationId) || null;
}

async function ensurePerJudge(epochId, generationId, repaint) {
  if (!epochId || !generationId) return null;
  const key = epochId + '/' + generationId;
  if (_perJudgeCache.has(key)) return _perJudgeCache.get(key);
  if (_loadingJudges.has(key)) return null;
  _loadingJudges.add(key);
  try {
    const data = await fetchJson('/api/generation/'
      + encodeURIComponent(epochId) + '/' + encodeURIComponent(generationId)
      + '/per-judge');
    if (data && typeof data === 'object') _perJudgeCache.set(key, data);
  } catch {
    _perJudgeCache.set(key, { epoch_id: epochId, generation_id: generationId, judges: [] });
  } finally {
    _loadingJudges.delete(key);
    if (typeof repaint === 'function') repaint();
  }
  return _perJudgeCache.get(key);
}

async function ensurePerEntry(epochId, generationId, repaint) {
  if (!epochId || !generationId) return null;
  const key = epochId + '/' + generationId;
  if (_perEntryCache.has(key)) return _perEntryCache.get(key);
  if (_loadingEntries.has(key)) return null;
  _loadingEntries.add(key);
  try {
    const data = await fetchJson('/api/generation/'
      + encodeURIComponent(epochId) + '/' + encodeURIComponent(generationId)
      + '/per-entry');
    if (data && typeof data === 'object') _perEntryCache.set(key, data);
  } catch {
    _perEntryCache.set(key, {
      epoch_id: epochId, generation_id: generationId, tournament_id: null, entries: [],
    });
  } finally {
    _loadingEntries.delete(key);
    if (typeof repaint === 'function') repaint();
  }
  return _perEntryCache.get(key);
}

function _fmtNum(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  return v.toFixed(3);
}

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

function renderEntries(epochId, generationId) {
  const node = $('phase0-gen-entries');
  if (!node) return;
  clearChildren(node);
  if (!epochId || !generationId) {
    node.appendChild(el('p', { class: 'empty' }, ['No generation selected.']));
    return;
  }
  const data = _perEntryCache.get(epochId + '/' + generationId);
  if (!data) {
    node.appendChild(el('p', { class: 'empty' }, ['loading per-entry breakdown…']));
    return;
  }
  const entries = Array.isArray(data.entries) ? data.entries : [];
  if (entries.length === 0) {
    node.appendChild(el('p', { class: 'empty' },
      [data.note ? '(no per-entry data: ' + data.note + ')' : 'No per-entry data recorded.']));
    return;
  }
  if (data.tournament_id) {
    node.appendChild(el('p', { class: 'panel-subheader mono' },
      ['tournament_id · ', data.tournament_id]));
  }
  const tbl = el('table', { class: 'phase0-mini-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['entry']),
    el('th', null, ['drift loss']),
    el('th', null, ['pass/fail']),
    el('th', null, ['runtime ms']),
    el('th', null, ['budget exceeded']),
  ])]));
  const tbody = el('tbody');
  for (const e of entries) {
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'mono' }, [String(e.entry_id || '—')]),
      el('td', { class: 'mono' }, [_fmtNum(e.drift_loss)]),
      el('td', { class: 'mono' }, [String(e.pass_fail == null ? '—' : e.pass_fail)]),
      el('td', { class: 'mono' }, [String(e.runtime_ms == null ? '—' : e.runtime_ms)]),
      el('td', { class: 'mono' }, [
        e.wall_clock_budget_exceeded == null ? '—'
          : (e.wall_clock_budget_exceeded ? 'yes' : 'no'),
      ]),
    ]));
  }
  tbl.appendChild(tbody);
  node.appendChild(tbl);
}

function renderJudges(epochId, generationId) {
  const node = $('phase0-gen-judges');
  if (!node) return;
  clearChildren(node);
  if (!epochId || !generationId) {
    node.appendChild(el('p', { class: 'empty' }, ['No generation selected.']));
    return;
  }
  const data = _perJudgeCache.get(epochId + '/' + generationId);
  if (!data) {
    node.appendChild(el('p', { class: 'empty' }, ['loading per-judge breakdown…']));
    return;
  }
  const judges = Array.isArray(data.judges) ? data.judges : [];
  if (judges.length === 0) {
    const msg = data.note ? '(no per-judge data: ' + data.note + ')'
      : '(no per-judge data recorded for this generation)';
    node.appendChild(el('p', { class: 'empty' }, [msg]));
    return;
  }
  const tbl = el('table', { class: 'phase0-mini-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['judge']),
    el('th', null, ['weighted loss']),
    el('th', null, ['raw loss']),
    el('th', null, ['weight']),
    el('th', null, ['runs']),
  ])]));
  const tbody = el('tbody');
  for (const j of judges) {
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'mono' }, [String(j.judge_name || '—')]),
      el('td', { class: 'mono' }, [_fmtNum(j.weighted_loss)]),
      el('td', { class: 'mono' }, [_fmtNum(j.raw_loss)]),
      el('td', { class: 'mono' }, [_fmtNum(j.weight)]),
      el('td', { class: 'mono' }, [String(j.run_count == null ? '—' : j.run_count)]),
    ]));
  }
  tbl.appendChild(tbody);
  node.appendChild(tbl);
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

export function renderPhase0Generation(params, repaint) {
  const epochId = (params && params.epochId)
    || (state.epochDef && state.epochDef.epoch_id) || null;
  const generationId = (params && params.generationId) || null;
  const exp = findExperiment(generationId);
  ensurePerJudge(epochId, generationId, repaint);
  ensurePerEntry(epochId, generationId, repaint);
  renderHypothesis(exp);
  renderPatches(exp);
  renderEntries(epochId, generationId);
  renderJudges(epochId, generationId);
  renderCompare();
}

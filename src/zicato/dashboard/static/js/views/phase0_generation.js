// views/phase0_generation.js — L2 (generation-level) view.
//
// Renders the per-generation shell:
//   - Verdict strip (Δscalar tile + decision pill) -> compare slot
//   - Hypothesis + Patches in respective slots
//   - Per-entry table with pass/fail pills
//   - Per-judge table

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';
import { renderCard } from '../components/card.js';
import { renderPill, renderInlinePill } from '../components/pill.js';
import { renderMetricTile } from '../components/tile.js';

const _perJudgeCache = new Map();
const _perEntryCache = new Map();
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

function _renderHypothesis(exp) {
  const node = $('phase0-gen-hypothesis');
  if (!node) return;
  clearChildren(node);
  let body;
  if (!exp) {
    body = el('p', { class: 'empty' }, ['No hypothesis recorded.']);
  } else {
    const hyp = exp.hypothesis || {};
    const out = exp.outcome || {};
    const wrap = el('div', { class: 'hyp-block' });
    const before = el('div', { class: 'hyp-section' });
    before.appendChild(el('h4', null, ['Proposed (before)']));
    if (hyp.core_idea) {
      before.appendChild(el('p', { class: 'hyp-core' }, [hyp.core_idea]));
    } else {
      before.appendChild(el('p', { class: 'empty' }, ['No core idea recorded.']));
    }
    if (hyp.why) {
      before.appendChild(el('p', null, [el('strong', null, ['why. ']), hyp.why]));
    }
    if (hyp.risks) {
      before.appendChild(el('p', null, [el('strong', null, ['risks. ']), hyp.risks]));
    }
    wrap.appendChild(before);
    const after = el('div', { class: 'hyp-section' });
    after.appendChild(el('h4', null, ['Outcome (after)']));
    if (out.summary) {
      after.appendChild(el('p', null, [out.summary]));
    } else if (out.tournament_decision || out.decision) {
      after.appendChild(el('p', null, [
        'decision: ',
        el('span', { class: 'mono' },
          [String(out.tournament_decision || out.decision)]),
      ]));
    } else {
      after.appendChild(el('p', { class: 'empty' }, ['No outcome recorded.']));
    }
    wrap.appendChild(after);
    body = wrap;
  }
  node.appendChild(renderCard({
    title: 'Hypothesis',
    subtitle: 'Proposed before; outcome reconciled after.',
    body,
  }));
}

function _renderPatches(exp) {
  const node = $('phase0-gen-patches');
  if (!node) return;
  clearChildren(node);
  const patches = exp && exp.patches;
  let entries = [];
  if (patches && typeof patches === 'object' && !Array.isArray(patches)) {
    for (const k of Object.keys(patches)) entries.push({ id: k, patch: patches[k] });
  } else if (Array.isArray(patches)) {
    entries = patches.map((p, i) => ({ id: (p && p.mutation_id) || ('p' + i), patch: p }));
  }
  let body;
  if (entries.length === 0) {
    body = el('p', { class: 'empty' }, ['No patches recorded.']);
  } else {
    const tbl = el('table', { class: 'ds-table patches-list' });
    tbl.appendChild(el('thead', null, [el('tr', null, [
      el('th', null, ['mutation']),
      el('th', null, ['op']),
      el('th', null, ['rationale']),
    ])]));
    const tbody = el('tbody');
    for (const e of entries) {
      const p = e.patch || {};
      const op = String(p.op || p.kind || '—');
      let opVariant = 'neutral';
      if (op.includes('add')) opVariant = 'success';
      else if (op.includes('remove') || op.includes('delete')) opVariant = 'error';
      else if (op.includes('change') || op.includes('modify')) opVariant = 'warning';
      tbody.appendChild(el('tr', null, [
        el('td', { class: 'mono' }, [e.id]),
        el('td', null, [renderInlinePill(op, opVariant)]),
        el('td', null, [String(p.rationale || p.message || '—')]),
      ]));
    }
    tbl.appendChild(tbody);
    body = tbl;
  }
  node.appendChild(renderCard({
    title: 'Patches',
    body,
  }));
}

function _renderEntries(epochId, generationId) {
  const node = $('phase0-gen-entries');
  if (!node) return;
  clearChildren(node);
  let body;
  if (!epochId || !generationId) {
    body = el('p', { class: 'empty' }, ['No generation selected.']);
  } else {
    const data = _perEntryCache.get(epochId + '/' + generationId);
    if (!data) {
      body = el('p', { class: 'empty' }, ['loading per-entry breakdown…']);
    } else {
      const entries = Array.isArray(data.entries) ? data.entries : [];
      if (entries.length === 0) {
        body = el('p', { class: 'empty' },
          [data.note ? '(no per-entry data: ' + data.note + ')' : 'No per-entry data recorded.']);
      } else {
        const wrap = el('div');
        if (data.tournament_id) {
          wrap.appendChild(el('p', {
            style: 'font-size:var(--font-size-11); color:var(--color-text-muted); margin:0 0 var(--space-2); font-family:var(--font-mono);',
          }, ['tournament · ', data.tournament_id]));
        }
        const tbl = el('table', { class: 'ds-table' });
        tbl.appendChild(el('thead', null, [el('tr', null, [
          el('th', null, ['entry']),
          el('th', null, ['drift loss']),
          el('th', null, ['pass/fail']),
          el('th', null, ['runtime ms']),
          el('th', null, ['budget']),
        ])]));
        const tbody = el('tbody');
        for (const e of entries) {
          const pf = e.pass_fail;
          let pfPill;
          if (pf === true || pf === 1 || String(pf).toLowerCase() === 'pass') {
            pfPill = renderInlinePill('pass', 'pass');
          } else if (pf === false || pf === 0 || String(pf).toLowerCase() === 'fail') {
            pfPill = renderInlinePill('fail', 'fail');
          } else if (pf == null) {
            pfPill = el('span', { class: 'mono' }, ['—']);
          } else {
            pfPill = el('span', { class: 'mono' }, [String(pf)]);
          }
          const exceeded = e.wall_clock_budget_exceeded;
          let budgetCell;
          if (exceeded == null) budgetCell = el('span', { class: 'mono' }, ['—']);
          else if (exceeded) budgetCell = renderInlinePill('over', 'warning');
          else budgetCell = renderInlinePill('ok', 'success');
          tbody.appendChild(el('tr', null, [
            el('td', { class: 'mono' }, [String(e.entry_id || '—')]),
            el('td', { class: 'mono' }, [_fmtNum(e.drift_loss)]),
            el('td', null, [pfPill]),
            el('td', { class: 'mono' }, [String(e.runtime_ms == null ? '—' : e.runtime_ms)]),
            el('td', null, [budgetCell]),
          ]));
        }
        tbl.appendChild(tbody);
        wrap.appendChild(tbl);
        body = wrap;
      }
    }
  }
  node.appendChild(renderCard({
    title: 'Per-entry breakdown',
    body,
  }));
}

function _renderJudges(epochId, generationId) {
  const node = $('phase0-gen-judges');
  if (!node) return;
  clearChildren(node);
  let body;
  if (!epochId || !generationId) {
    body = el('p', { class: 'empty' }, ['No generation selected.']);
  } else {
    const data = _perJudgeCache.get(epochId + '/' + generationId);
    if (!data) {
      body = el('p', { class: 'empty' }, ['loading per-judge breakdown…']);
    } else {
      const judges = Array.isArray(data.judges) ? data.judges : [];
      if (judges.length === 0) {
        const msg = data.note ? '(no per-judge data: ' + data.note + ')'
          : '(no per-judge data recorded for this generation)';
        body = el('p', { class: 'empty' }, [msg]);
      } else {
        const tbl = el('table', { class: 'ds-table' });
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
        body = tbl;
      }
    }
  }
  node.appendChild(renderCard({
    title: 'Per-judge breakdown',
    body,
  }));
}

function _renderCompare(exp) {
  const node = $('phase0-gen-compare');
  if (!node) return;
  clearChildren(node);
  // The compare slot now carries the verdict tile strip at the top of L2.
  let body;
  if (!exp) {
    body = el('p', { class: 'empty' }, ['No experiment recorded.']);
  } else {
    const out = exp.outcome || {};
    const decision = (out.tournament_decision || out.decision || '—').toString().toLowerCase();
    let pillVariant = 'neutral';
    if (decision === 'promoted') pillVariant = 'promoted';
    else if (decision === 'rejected') pillVariant = 'rejected';
    else if (decision === 'deferred') pillVariant = 'deferred';

    const ds = out.scalar_score_delta;
    const sentiment = (typeof ds === 'number' && isFinite(ds))
      ? (ds < 0 ? 'good' : (ds > 0 ? 'bad' : 'flat')) : 'flat';

    const strip = el('div', { class: 'tile-strip' });
    strip.appendChild(renderMetricTile({
      label: 'decision', value: decision, size: 'sm',
    }));
    strip.appendChild(renderMetricTile({
      label: 'Δ scalar',
      value: typeof ds === 'number' && isFinite(ds)
        ? ((ds > 0 ? '+' : '') + ds.toFixed(3)) : '—',
      sentiment,
    }));
    if (typeof out.scalar_score === 'number') {
      strip.appendChild(renderMetricTile({
        label: 'scalar', value: out.scalar_score.toFixed(3),
      }));
    }
    const decorTile = el('div', { class: 'tile' }, [
      el('div', { class: 'tile-label' }, ['status']),
      el('div', { style: 'margin-top:var(--space-2);' },
        [renderPill(decision, pillVariant)]),
    ]);
    strip.appendChild(decorTile);
    body = strip;
  }
  node.appendChild(renderCard({
    title: 'Verdict',
    variant: 'flush',
    body,
  }));
}

export function renderPhase0Generation(params, repaint) {
  const epochId = (params && params.epochId)
    || (state.epochDef && state.epochDef.epoch_id) || null;
  const generationId = (params && params.generationId) || null;
  const exp = findExperiment(generationId);
  ensurePerJudge(epochId, generationId, repaint);
  ensurePerEntry(epochId, generationId, repaint);
  _renderCompare(exp);
  _renderHypothesis(exp);
  _renderPatches(exp);
  _renderEntries(epochId, generationId);
  _renderJudges(epochId, generationId);
}

// views/phase0_epoch.js — L1 (epoch-level) view.
//
// Renders the epoch shell using the design-system components:
//   - GOAL as a callout card
//   - CONTRACT DIFF — status of frozen components
//   - GENERATION SPINE — the centerpiece visual element
//   - PER-ENTRY × GENERATION heatmap
//   - PER-JUDGE × GENERATION heatmap
//   - JOURNAL / Recent experiments
//
// Each section renders INTO its pre-existing slot so tests reading
// those slots find the expected text.

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';
import { phase0Href } from './phase0_router.js';
import { renderCard, renderCalloutCard } from '../components/card.js';
import { renderPill } from '../components/pill.js';
import { renderSpine } from '../components/spine.js';
import { renderHeatmapTable } from '../components/heatmap.js';
import { renderMetricTile } from '../components/tile.js';
import { renderHypothesisOutcomeCompact } from '../core/hypothesis_block.js';

const _contractDiffCache = new Map();
const _loadingDiff = new Set();
const _perJudgeTrendCache = new Map();
const _loadingTrend = new Set();
const _perEntryTrendCache = new Map();
const _loadingEntryTrend = new Set();

export function resetPerJudgeTrendCache() {
  _perJudgeTrendCache.clear();
  _loadingTrend.clear();
}
export function perJudgeTrendPayload(epochId) {
  return _perJudgeTrendCache.get(epochId) || null;
}

async function ensurePerJudgeTrend(epochId, repaint) {
  if (!epochId) return null;
  if (_perJudgeTrendCache.has(epochId)) return _perJudgeTrendCache.get(epochId);
  if (_loadingTrend.has(epochId)) return null;
  _loadingTrend.add(epochId);
  try {
    const data = await fetchJson('/api/epoch/' + encodeURIComponent(epochId) + '/per-judge-trend');
    if (data && typeof data === 'object') {
      _perJudgeTrendCache.set(epochId, data);
    }
  } catch {
    _perJudgeTrendCache.set(epochId, {
      epoch_id: epochId, generations: [], judges: [],
    });
  } finally {
    _loadingTrend.delete(epochId);
    if (typeof repaint === 'function') repaint();
  }
  return _perJudgeTrendCache.get(epochId);
}

export function resetContractDiffCache() {
  _contractDiffCache.clear();
  _loadingDiff.clear();
}
export function contractDiffPayload(epochId) {
  return _contractDiffCache.get(epochId) || null;
}

async function ensureContractDiff(epochId, repaint) {
  if (!epochId) return null;
  if (_contractDiffCache.has(epochId)) return _contractDiffCache.get(epochId);
  if (_loadingDiff.has(epochId)) return null;
  _loadingDiff.add(epochId);
  try {
    const data = await fetchJson('/api/contract-diff/' + encodeURIComponent(epochId));
    if (data && typeof data === 'object') {
      _contractDiffCache.set(epochId, data);
    }
  } catch {
    _contractDiffCache.set(epochId, {
      epoch_id: epochId, predecessor_epoch_id: null,
      components: [], any_changed: false,
    });
  } finally {
    _loadingDiff.delete(epochId);
    if (typeof repaint === 'function') repaint();
  }
  return _contractDiffCache.get(epochId);
}

async function ensurePerEntryTrend(epochId, generationIds, repaint) {
  if (!epochId || !Array.isArray(generationIds) || generationIds.length === 0) return null;
  if (_perEntryTrendCache.has(epochId)) return _perEntryTrendCache.get(epochId);
  if (_loadingEntryTrend.has(epochId)) return null;
  _loadingEntryTrend.add(epochId);
  try {
    const entryMap = new Map();
    for (const gid of generationIds) {
      try {
        const data = await fetchJson('/api/generation/'
          + encodeURIComponent(epochId) + '/' + encodeURIComponent(gid) + '/per-entry');
        const entries = (data && Array.isArray(data.entries)) ? data.entries : [];
        for (const e of entries) {
          if (!e || !e.entry_id) continue;
          if (!entryMap.has(e.entry_id)) entryMap.set(e.entry_id, {});
          if (typeof e.drift_loss === 'number') {
            entryMap.get(e.entry_id)[gid] = e.drift_loss;
          }
        }
      } catch { /* skip */ }
    }
    _perEntryTrendCache.set(epochId, {
      epoch_id: epochId,
      generations: generationIds.slice(),
      entries: Array.from(entryMap.keys()).sort().map((eid) => ({
        entry_id: eid, by_gen: entryMap.get(eid),
      })),
    });
  } finally {
    _loadingEntryTrend.delete(epochId);
    if (typeof repaint === 'function') repaint();
  }
  return _perEntryTrendCache.get(epochId);
}

export function resetPerEntryTrendCache() {
  _perEntryTrendCache.clear();
  _loadingEntryTrend.clear();
}
export function perEntryTrendPayload(epochId) {
  return _perEntryTrendCache.get(epochId) || null;
}

// -- Goal slot --------------------------------------------------------
function _renderGoal() {
  const node = $('phase0-epoch-goal');
  if (!node) return;
  clearChildren(node);
  const def = state.epochDef;
  const goal = (def && typeof def.goal === 'string') ? def.goal.trim() : '';
  if (goal) {
    node.appendChild(renderCalloutCard({
      title: 'Goal',
      accent: 'accent',
      body: el('p', { class: 'goal-callout-text' }, [goal]),
    }));
    return;
  }
  node.appendChild(renderCalloutCard({
    title: 'Goal',
    accent: 'warning',
    body: el('div', null, [
      el('p', { class: 'empty', style: 'padding-top:0' }, ['(no goal recorded)']),
      el('p', {
        style: 'font-size:var(--font-size-12); color:var(--color-text-muted); margin:0;',
      }, [
        'Set via ',
        el('code', { class: 'mono' }, ['zicato epoch set-goal --epoch <id> --goal "..."']),
      ]),
    ]),
  }));
}

// -- Contract diff slot -----------------------------------------------
function _renderContractDiff(epochId) {
  const node = $('phase0-epoch-contract-diff');
  if (!node) return;
  clearChildren(node);
  let body;
  if (!epochId) {
    body = el('p', { class: 'empty' }, ['Select an epoch from the workspace view.']);
  } else {
    const data = _contractDiffCache.get(epochId);
    if (!data) {
      body = el('p', { class: 'empty' }, ['loading contract diff…']);
    } else if (!data.predecessor_epoch_id) {
      body = el('p', { class: 'empty' },
        ['First epoch in the workspace — no predecessor to diff.']);
    } else {
      const comps = Array.isArray(data.components) ? data.components : [];
      const def = state.epochDef;
      const experiments = (def && Array.isArray(def.experiments)) ? def.experiments : [];
      const promoted = experiments.filter((e) => {
        const dec = (e.outcome && (e.outcome.tournament_decision || e.outcome.decision)) || '';
        return String(dec).toLowerCase() === 'promoted';
      }).length;
      const isClosed = !!(def && def.closed);

      const wrap = el('div', null, [
        el('div', { class: 'tile-strip',
          style: 'margin-bottom:var(--space-3);' }, [
          renderMetricTile({
            label: 'state', value: isClosed ? 'closed' : 'open', size: 'sm',
          }),
          renderMetricTile({
            label: 'generations', value: experiments.length, size: 'sm',
          }),
          renderMetricTile({
            label: 'promoted', value: promoted, size: 'sm',
          }),
        ]),
      ]);
      if (comps.length > 0) {
        const tbl = el('table', { class: 'ds-table' });
        tbl.appendChild(el('thead', null, [el('tr', null, [
          el('th', null, ['component']),
          el('th', null, ['previous']),
          el('th', null, ['current']),
          el('th', null, ['changed']),
        ])]));
        const tbody = el('tbody');
        for (const c of comps) {
          const pill = c.changed
            ? renderPill('changed', 'warning')
            : renderPill('same', 'neutral');
          tbody.appendChild(el('tr', null, [
            el('td', { class: 'mono' }, [c.name]),
            el('td', { class: 'mono' }, [c.previous_hash ? c.previous_hash.slice(0, 8) : '—']),
            el('td', { class: 'mono' }, [c.current_hash ? c.current_hash.slice(0, 8) : '—']),
            el('td', null, [pill]),
          ]));
        }
        tbl.appendChild(tbody);
        wrap.appendChild(tbl);
      }
      body = wrap;
    }
  }
  node.appendChild(renderCard({
    title: 'Contract diff',
    subtitle: 'Frozen components — board / brief / scoring / harness — vs predecessor.',
    body,
  }));
}

// -- Spine slot — the centerpiece -------------------------------------
function _decisionOf(exp) {
  if (!exp || !exp.outcome) return null;
  return (exp.outcome.tournament_decision || exp.outcome.decision || '').toString().toLowerCase();
}
function _scalarOf(exp) {
  if (!exp || !exp.outcome) return null;
  const v = exp.outcome.scalar_score;
  return (typeof v === 'number' && isFinite(v)) ? v : null;
}

function _buildSpineNodes(epochId) {
  const def = state.epochDef;
  if (!def || !Array.isArray(def.experiments)) return [];
  const exps = def.experiments;
  const hb = state.heartbeat || {};
  const liveGen = (hb.epoch_id === (def.epoch_id || epochId) && hb.generation_id)
    ? hb.generation_id : null;

  const nodes = [];
  for (const exp of exps) {
    const id = exp.generation_id || '?';
    const dec = _decisionOf(exp);
    const promoted = dec === 'promoted';
    const scalar = _scalarOf(exp);
    nodes.push({
      id, scalar, promoted, decision: dec, live: false,
      href: phase0Href('generation', { epochId: epochId, generationId: id }),
    });
  }
  if (liveGen && !nodes.find((n) => n.id === liveGen)) {
    nodes.push({
      id: liveGen, scalar: null, promoted: false, live: true,
      href: phase0Href('generation', { epochId: epochId, generationId: liveGen }),
    });
  } else if (liveGen) {
    const idx = nodes.findIndex((n) => n.id === liveGen);
    if (idx >= 0) nodes[idx].live = true;
  }
  return nodes;
}

function _renderSpine(epochId) {
  const node = $('phase0-epoch-spine');
  if (!node) return;
  clearChildren(node);
  const spineNodes = _buildSpineNodes(epochId);
  const body = spineNodes.length === 0
    ? el('p', { class: 'empty' }, ['No generations yet.'])
    : renderSpine({ nodes: spineNodes });
  node.appendChild(renderCard({
    title: 'Generation spine',
    subtitle: 'Champion lineage left-to-right; rejected generations footnoted below.',
    accent: 'accent',
    body,
  }));
}

// -- Entry × generation heatmap slot ----------------------------------
function _renderEntryHeatmap(epochId, generationIds) {
  const node = $('phase0-epoch-heatmap-entries');
  if (!node) return;
  clearChildren(node);
  let body;
  if (!epochId || generationIds.length === 0) {
    body = el('p', { class: 'empty' }, ['No generations yet.']);
  } else {
    const data = _perEntryTrendCache.get(epochId);
    if (!data) {
      body = el('p', { class: 'empty' }, ['loading per-entry heatmap…']);
    } else {
      const entries = Array.isArray(data.entries) ? data.entries : [];
      if (entries.length === 0) {
        body = el('p', { class: 'empty' }, ['No per-entry data yet.']);
      } else {
        body = renderHeatmapTable({
          rows: entries.map((e) => e.entry_id),
          cols: data.generations,
          valueAt: (row, col) => {
            const found = entries.find((e) => e.entry_id === row);
            if (!found) return null;
            const v = found.by_gen && found.by_gen[col];
            return (typeof v === 'number' && isFinite(v)) ? v : null;
          },
          scale: 'sequential',
          rowLabel: 'entry',
          ariaLabel: 'per-entry × generation loss heatmap',
        });
      }
    }
  }
  node.appendChild(renderCard({
    title: 'Per-entry × generation',
    subtitle: 'Drift loss per board entry across the spine.',
    body,
  }));
}

// -- Judge × generation heatmap slot ----------------------------------
function _renderJudgeHeatmap(epochId) {
  const node = $('phase0-epoch-heatmap-judges');
  if (!node) return;
  clearChildren(node);
  let body;
  if (!epochId) body = el('p', { class: 'empty' }, ['Select an epoch.']);
  else {
    const data = _perJudgeTrendCache.get(epochId);
    if (!data) {
      body = el('p', { class: 'empty' }, ['loading per-judge heatmap…']);
    } else {
      const generations = Array.isArray(data.generations) ? data.generations : [];
      const judges = Array.isArray(data.judges) ? data.judges : [];
      if (generations.length === 0 || judges.length === 0) {
        const msg = data.note ? '(no per-judge data: ' + data.note + ')'
          : '(no per-judge data recorded for this epoch yet)';
        body = el('p', { class: 'empty' }, [msg]);
      } else {
        body = renderHeatmapTable({
          rows: judges.map((j) => j.judge_name || '—'),
          cols: generations,
          valueAt: (row, col) => {
            const found = judges.find((j) => (j.judge_name || '—') === row);
            if (!found) return null;
            const v = found.by_generation && found.by_generation[col];
            return (typeof v === 'number' && isFinite(v)) ? v : null;
          },
          scale: 'sequential',
          rowLabel: 'judge',
          ariaLabel: 'per-judge × generation weighted-loss heatmap',
        });
      }
    }
  }
  node.appendChild(renderCard({
    title: 'Per-judge × generation',
    subtitle: 'Weighted loss per judge across the spine.',
    body,
  }));
}

// Compact "Recent experiments" list — Phase 1.5's hypothesis-block
// helper rendered inside visual-design's card wrapper. L1 shows several
// experiments on one page, so the helper runs in compact mode; the
// full version (L2) renders one experiment per page from the same
// helper.
function _renderRecentExperiments(epochId) {
  const node = $('phase0-epoch-experiments');
  if (!node) return;
  clearChildren(node);
  const def = state.epochDef;
  const experiments = (def && Array.isArray(def.experiments)) ? def.experiments : [];
  let body;
  if (experiments.length === 0) {
    body = el('p', { class: 'empty' }, ['No experiments recorded yet.']);
  } else {
    body = el('div');
    body.appendChild(el('p', { class: 'panel-subheader' }, [
      'Most recent first — proposed-before / outcome-after split, '
      + 'matching the per-generation view. Open a row for the full block.',
    ]));
    // Newest first, cap at six so L1 stays a digest. Use slice + reverse
    // (not sort) so a stable order with ties is preserved.
    const recent = experiments.slice(-6).reverse();
    const list = el('div', { class: 'phase0-exp-list' });
    for (const exp of recent) {
      const genId = exp && exp.generation_id ? exp.generation_id : '?';
      const row = el('div', { class: 'phase0-exp-row' });
      const header = el('div', { class: 'phase0-exp-row-h' }, [
        el('span', { class: 'phase0-exp-gen mono' }, ['gen · ', genId]),
      ]);
      if (epochId) {
        header.appendChild(el('a', {
          class: 'phase0-exp-link',
          href: phase0Href('generation', { epochId, generationId: genId }),
        }, ['open generation →']));
      }
      row.appendChild(header);
      row.appendChild(renderHypothesisOutcomeCompact(
        exp && exp.hypothesis, exp && exp.outcome, { compact: true },
      ));
      list.appendChild(row);
    }
    body.appendChild(list);
  }
  node.appendChild(renderCard({
    title: 'Recent experiments',
    body,
  }));
}

function _renderJournal() {
  const node = $('phase0-epoch-journal');
  if (!node) return;
  clearChildren(node);
  const def = state.epochDef;
  const journal = def && typeof def.journal === 'string' ? def.journal : '';
  if (!journal.trim()) {
    node.appendChild(el('p', { class: 'empty' }, ['No journal preview.']));
    return;
  }
  node.appendChild(renderCard({
    title: 'Journal preview',
    body: el('pre', { class: 'phase0-journal-preview mono' },
      [journal.slice(0, 1200)]),
  }));
}

export function renderPhase0Epoch(params, repaint) {
  const epochId = (params && params.epochId)
    || (state.heartbeat && state.heartbeat.epoch_id)
    || (state.epochDef && state.epochDef.epoch_id)
    || null;

  const def = state.epochDef;
  const gids = (def && Array.isArray(def.experiments))
    ? def.experiments.map((e) => e.generation_id).filter(Boolean)
    : [];

  ensureContractDiff(epochId, repaint);
  ensurePerJudgeTrend(epochId, repaint);
  if (gids.length > 0) ensurePerEntryTrend(epochId, gids, repaint);

  _renderGoal();
  _renderContractDiff(epochId);
  _renderSpine(epochId);
  _renderEntryHeatmap(epochId, gids);
  _renderJudgeHeatmap(epochId);
  _renderRecentExperiments(epochId);
  _renderJournal();
}

// variants/G/views/epoch.js — L1 epoch.
//
// The objective up top; a real home for the (possibly long) proposer
// brief in a drawer; the lineage as D's NON-COLLIDING bumps chart
// (champion spine lane + challenger lane); and the entries × generation
// drift-loss as D's quiet HEATMAP (A's dense table replaced). Click a
// generation column → drill into the candidate.
//
// Digest-gated: the per-entry trend is fetched once and cached; a
// heartbeat tick produces an identical digest and the view is a no-op.

import { el } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { state } from '../../../core/state.js';
import { panel, readouts, empty, loading, drawer, fmt, signed } from '../components/ui.js';
import { renderMarkdown } from '../components/markdown.js';
import { heatmap, bumps } from '../svg.js';
import { lineageModel, bumpsNodes, decisionOf } from '../model.js';
import { href, navigate } from '../router.js';

const entryTrend = new Map();   // epochId -> { generations, entries }
const loadingTrend = new Set();
let _lastDigest = null;

export function resetEpochCache() { entryTrend.clear(); loadingTrend.clear(); _lastDigest = null; }

async function ensureEntryTrend(epochId, gids, repaint) {
  if (!epochId || !gids.length || entryTrend.has(epochId) || loadingTrend.has(epochId)) return;
  loadingTrend.add(epochId);
  const map = new Map();
  for (const gid of gids) {
    try {
      const data = await fetchJson('/api/generation/' + encodeURIComponent(epochId)
        + '/' + encodeURIComponent(gid) + '/per-entry');
      const entries = (data && Array.isArray(data.entries)) ? data.entries : [];
      for (const e of entries) {
        if (!e || !e.entry_id) continue;
        if (!map.has(e.entry_id)) map.set(e.entry_id, {});
        if (typeof e.drift_loss === 'number') map.get(e.entry_id)[gid] = e.drift_loss;
      }
    } catch { /* skip gen */ }
  }
  entryTrend.set(epochId, {
    generations: gids.slice(),
    entries: [...map.keys()].sort().map((id) => ({ entry_id: id, by_gen: map.get(id) })),
  });
  loadingTrend.delete(epochId);
  if (repaint) repaint();
}

export function epochDigest(params) {
  const def = state.epochDef;
  const epochId = (def && def.epoch_id) || (params && params.epochId) || null;
  const exps = def && Array.isArray(def.experiments) ? def.experiments : null;
  const trend = epochId ? entryTrend.get(epochId) : null;
  return JSON.stringify({
    epochId,
    loaded: !!def,
    goal: def && def.goal,
    brief: def ? (def.brief ? def.brief.length : 0) : null,
    exps: exps ? exps.map((e) => [e.generation_id, decisionOf(e),
      e.outcome && typeof e.outcome.scalar_score === 'number' ? e.outcome.scalar_score.toFixed(3) : null]) : null,
    trend: trend ? trend.entries.map((e) => [e.entry_id, Object.keys(e.by_gen).length]) : null,
  });
}

function objectiveBlock(def) {
  const goal = (def && typeof def.goal === 'string') ? def.goal.trim() : '';
  return el('div', { class: 'g-objective' }, [
    el('div', { class: 'g-objective-label' }, ['Mission objective']),
    goal
      ? el('div', { class: 'g-objective-text' }, [goal])
      : el('div', { class: 'g-objective-empty' }, ['No objective recorded — set via zicato epoch set-goal.']),
  ]);
}

function briefDrawer(def) {
  const brief = (def && typeof def.brief === 'string') ? def.brief : '';
  return drawer({
    title: 'Proposer brief',
    sub: 'the operator’s brief to the proposer',
    openByDefault: false,
    body: brief.trim()
      ? renderMarkdown(brief)
      : el('div', { class: 'g-empty' }, ['No brief.md / rubric.md found for this epoch. The proposer ran with the goal and contract only; this panel is the brief’s home when one is authored.']),
  });
}

function buildHeatmap(epochId) {
  const data = entryTrend.get(epochId);
  if (!data) return loading('Building per-entry drift grid');
  const entries = data.entries || [];
  const gens = data.generations || [];
  if (!entries.length) return empty('No per-entry drift data recorded yet.');
  return heatmap({
    rows: entries.map((e) => ({ id: e.entry_id, label: e.entry_id })),
    cols: gens.map((g) => ({ id: g, label: g })),
    value: (rowId, colId) => {
      const e = entries.find((x) => x.entry_id === rowId);
      const v = e && e.by_gen[colId];
      return (typeof v === 'number' && isFinite(v)) ? v : null;
    },
    cellW: 54, cellH: 22, labelWidth: 190,
    onClick: (rowId, colId) => navigate('experiment', { epochId, genId: colId }),
  });
}

export function renderEpoch(root, params, repaint) {
  const def = state.epochDef;
  const epochId = (def && def.epoch_id) || params.epochId
    || (state.heartbeat && state.heartbeat.epoch_id) || null;

  if (def) {
    const gids = Array.isArray(def.experiments)
      ? def.experiments.map((e) => e.generation_id).filter(Boolean) : [];
    ensureEntryTrend(epochId, gids, repaint);
  }

  const digest = epochDigest(params);
  if (digest === _lastDigest && root.firstChild) return;
  _lastDigest = digest;
  root.textContent = '';

  root.appendChild(el('div', { class: 'g-pagehead' }, [
    el('h1', null, ['Epoch']),
    el('span', { class: 'g-pagehead-sub g-mono' }, [epochId || '—']),
  ]));

  if (def == null) { root.appendChild(loading('Loading epoch contract')); return; }

  root.appendChild(el('div', { class: 'g-section' }, [objectiveBlock(def)]));

  const promoted = (def.experiments || []).filter((e) => decisionOf(e) === 'promoted').length;
  const gids = Array.isArray(def.experiments) ? def.experiments.map((e) => e.generation_id).filter(Boolean) : [];
  const spineScalar = def.delta_scalar_summary && def.delta_scalar_summary.champion_spine;
  root.appendChild(el('div', { class: 'g-section' }, [
    readouts([
      { label: 'generations', value: gids.length },
      { label: 'promoted', value: promoted, tone: 'improve' },
      { label: 'spine Δscalar', value: signed(spineScalar), tone: typeof spineScalar === 'number' && spineScalar < 0 ? 'improve' : null, foot: 'meta-loop progress' },
      { label: 'state', value: def.closed ? 'CLOSED' : 'OPEN', tone: def.closed ? null : 'live' },
    ]),
  ]));

  root.appendChild(el('div', { class: 'g-section' }, [briefDrawer(def)]));

  // lineage as D's non-colliding bumps chart (replaces A's gauntlet SVG).
  const nodes = bumpsNodes(state, epochId);
  const { spine, challengers } = lineageModel(state, epochId);
  root.appendChild(el('div', { class: 'g-section' }, [
    panel({
      title: 'Lineage',
      sub: 'champion spine in its own lane · challengers branch into theirs · click a node → candidate',
      actions: el('a', { class: 'g-btn', href: href('tournament', { epochId }) }, ['match-ups →']),
      body: nodes.length === 0 ? empty('No generations yet.') : el('div', { class: 'g-bumps-wrap' }, [
        bumps({ nodes, width: 720, height: 180, onClick: (n) => navigate('experiment', { epochId, genId: n.id }) }),
      ]),
    }),
  ]));
  void spine; void challengers;

  // entries × generation drift heatmap (replaces A's dense heat table).
  root.appendChild(panel({
    title: 'Board entry × generation drift',
    sub: 'cell shade = drift loss (lighter low → darker high) · hover for the exact value · click a cell to drill into that candidate',
    body: el('div', { class: 'g-heatmap-wrap' }, [buildHeatmap(epochId)]),
  }));
}

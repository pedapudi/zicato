// variants/A/views/epoch.js — L1 epoch CONTROL PANEL.
//
// The objective up top, prominent. A real home for the proposer brief
// (which can be LONG) in a dedicated, well-designed drawer — never a
// truncated line. The gauntlet as a bold live bracket with clean lanes
// (champion vs challengers; no colliding lines). Board entries ×
// generations as a themed drift heatmap (hover -> detail, click ->
// drill into the experiment).
//
// Data: /api/epoch (goal, brief, board, experiments, scoring),
// /api/generation/{e}/{g}/per-entry (heatmap), /api/active-tournament.

import { el } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { state } from '../../../core/state.js';
import { panel, readouts, empty, loading, heatmap, makeTip, chip } from '../components/instruments.js';
import { gauntlet, gauntletLegend } from '../components/gauntlet.js';
import { renderMarkdown } from '../components/markdown.js';
import { href, navigate } from '../router.js';

const entryTrend = new Map();      // epochId -> { generations, entries }
const loadingTrend = new Set();
let briefOpen = false;
let _tip = null;

export function resetEpochCache() { entryTrend.clear(); loadingTrend.clear(); }

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

function fmt(v, d = 3) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(d) : '—'; }
function decisionOf(exp) {
  const o = exp && exp.outcome;
  if (!o || typeof o !== 'object') return null;
  const d = String(o.tournament_decision || o.decision || '').toLowerCase();
  if (d.includes('promot')) return 'promoted';
  if (d.includes('reject')) return 'rejected';
  return d || null;
}

// -- objective + brief -------------------------------------------------
function objectiveBlock(def) {
  const goal = (def && typeof def.goal === 'string') ? def.goal.trim() : '';
  return el('div', { class: 'mcA-objective' }, [
    el('div', { class: 'mcA-objective-label' }, ['Mission objective']),
    goal
      ? el('div', { class: 'mcA-objective-text' }, [goal])
      : el('div', { class: 'mcA-objective-empty' },
          ['No objective recorded — set via zicato epoch set-goal.']),
  ]);
}

function briefDrawer(def) {
  const brief = (def && typeof def.brief === 'string') ? def.brief : '';
  const card = el('div', { class: 'mcA-brief-card' });
  const head = el('div', { class: 'mcA-brief-head' }, [
    el('div', null, [
      el('span', { class: 'mcA-panel-title' }, ['Proposer brief']),
      el('span', { class: 'mcA-panel-sub' }, ['  the operator’s brief to the proposer']),
    ]),
    el('span', { class: 'mcA-brief-toggle' }, [briefOpen ? 'collapse ∧' : 'expand ∨']),
  ]);
  const body = el('div', { class: 'mcA-brief-body' });
  if (!briefOpen) body.setAttribute('hidden', 'true');
  if (brief.trim()) {
    body.appendChild(renderMarkdown(brief));
  } else {
    body.appendChild(el('div', { class: 'mcA-brief-empty' }, [
      'No brief.md / rubric.md found for this epoch. The proposer ran '
      + 'with the goal and contract only; this panel is the brief’s '
      + 'home when one is authored.',
    ]));
  }
  head.addEventListener('click', () => {
    briefOpen = !briefOpen;
    if (briefOpen) body.removeAttribute('hidden'); else body.setAttribute('hidden', 'true');
    head.querySelector ? null : null;
    // update toggle label without rebuilding
    const t = head.childNodes[head.childNodes.length - 1];
    if (t) t.textContent = briefOpen ? 'collapse ∧' : 'expand ∨';
  });
  card.appendChild(head);
  card.appendChild(body);
  return card;
}

// -- gauntlet ----------------------------------------------------------
function buildGauntlet(def, epochId) {
  const exps = Array.isArray(def.experiments) ? def.experiments : [];
  const hb = state.heartbeat || {};
  const liveGen = (hb.epoch_id === epochId && hb.generation_id) ? hb.generation_id : null;

  // spine = baseline (v0) + every promoted generation, in order.
  const spine = [];
  const challengers = [];
  let lastSpineId = null;
  for (const exp of exps) {
    const id = exp.generation_id || '?';
    const dec = decisionOf(exp);
    const isBaseline = !exp.parent_generation_id && exp.outcome == null;
    const scalar = exp.outcome && typeof exp.outcome.scalar_score === 'number'
      ? exp.outcome.scalar_score : null;
    if (isBaseline || dec === 'promoted') {
      spine.push({ id, scalar });
      lastSpineId = id;
    } else if (dec === 'rejected') {
      challengers.push({
        id, parentId: exp.parent_generation_id || lastSpineId,
        decision: 'rejected',
        delta: exp.outcome ? exp.outcome.scalar_score_delta : null,
      });
    }
  }
  if (liveGen && !spine.find((n) => n.id === liveGen) && !challengers.find((c) => c.id === liveGen)) {
    spine.push({ id: liveGen, scalar: null, live: true });
  }
  return { spine, challengers };
}

// -- heatmap -----------------------------------------------------------
function buildHeatmap(epochId) {
  const data = entryTrend.get(epochId);
  if (!data) return loading('Building per-entry drift grid');
  const entries = data.entries || [];
  const gens = data.generations || [];
  if (!entries.length) return empty('No per-entry drift data recorded yet.');

  // normalize against the global max loss so cell color is comparable.
  let maxLoss = 0;
  for (const e of entries) for (const g of gens) {
    const v = e.by_gen[g];
    if (typeof v === 'number' && isFinite(v)) maxLoss = Math.max(maxLoss, v);
  }
  maxLoss = maxLoss || 1;

  const rows = entries.map((e) => ({
    label: e.entry_id,
    cells: gens.map((g) => {
      const v = e.by_gen[g];
      const has = typeof v === 'number' && isFinite(v);
      return {
        value: has ? v : null,
        norm: has ? v / maxLoss : 0,
        showValue: false,
        tipTitle: e.entry_id + ' @ ' + g,
        tipRows: ['drift loss  ' + (has ? v.toFixed(3) : '—')],
        gen: g,
        entry: e.entry_id,
      };
    }),
  }));

  return heatmap({
    rows, cols: gens, tip: _tip,
    onCell: (ri, ci, cell) => {
      if (cell && cell.gen) navigate('experiment', { epochId, genId: cell.gen });
    },
  });
}

export function renderEpoch(root, params, repaint) {
  const epochId = params.epochId
    || (state.epochDef && state.epochDef.epoch_id)
    || (state.heartbeat && state.heartbeat.epoch_id)
    || null;
  const def = state.epochDef;

  root.textContent = '';
  if (!_tip) _tip = makeTip(document.body);

  root.appendChild(el('div', { class: 'mcA-pagehead' }, [
    el('h1', null, ['Epoch']),
    el('span', { class: 'mcA-pagehead-sub mono' }, [epochId || '—']),
  ]));

  if (def == null) {
    root.appendChild(loading('Loading epoch contract'));
    return;
  }

  // The /api/epoch contract arrives via the global environment fold, but
  // if def.epoch_id differs from the requested epochId we still render
  // the current one (the only epoch with a live contract).
  const gids = Array.isArray(def.experiments)
    ? def.experiments.map((e) => e.generation_id).filter(Boolean) : [];
  ensureEntryTrend(def.epoch_id || epochId, gids, repaint);

  // objective
  root.appendChild(el('div', { style: 'margin-bottom:16px' }, [objectiveBlock(def)]));

  // vitals readout
  const promoted = (def.experiments || []).filter((e) => decisionOf(e) === 'promoted').length;
  const spineScalar = def.delta_scalar_summary && def.delta_scalar_summary.champion_spine;
  root.appendChild(el('div', { style: 'margin-bottom:16px' }, [
    readouts([
      { label: 'generations', value: gids.length },
      { label: 'promoted', value: promoted, tone: 'go' },
      { label: 'spine Δscalar', value: fmt(spineScalar), tone: typeof spineScalar === 'number' && spineScalar < 0 ? 'go' : null, foot: 'meta-loop progress' },
      { label: 'state', value: def.closed ? 'CLOSED' : 'OPEN', tone: def.closed ? null : 'live' },
    ]),
  ]));

  // brief drawer (dedicated home)
  root.appendChild(el('div', { style: 'margin-bottom:16px' }, [briefDrawer(def)]));

  // gauntlet
  const { spine, challengers } = buildGauntlet(def, def.epoch_id || epochId);
  const gauntletBody = spine.length === 0
    ? empty('No generations yet.')
    : el('div', null, [
        el('div', { class: 'mcA-gauntlet' }, [
          gauntlet({ spine, challengers, onSelect: (id) => navigate('experiment', { epochId: def.epoch_id || epochId, genId: id }) }),
        ]),
        gauntletLegend(),
      ]);
  root.appendChild(el('div', { style: 'margin-bottom:16px' }, [
    panel({
      title: 'Gauntlet',
      sub: 'champion spine · challengers in their own lanes · click a node → telemetry',
      accent: 'accent',
      actions: el('a', { class: 'mcA-btn', href: href('tournament', { epochId: def.epoch_id || epochId }) }, ['full lineage →']),
      body: gauntletBody,
    }),
  ]));

  // drift heatmap
  root.appendChild(panel({
    title: 'Board entry × generation drift',
    sub: 'cell color = drift loss (green low → red high) · hover for detail · click to drill',
    body: buildHeatmap(def.epoch_id || epochId),
  }));
}

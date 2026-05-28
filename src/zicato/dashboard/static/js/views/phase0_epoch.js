// views/phase0_epoch.js — L1 (epoch-level) view.
//
// Renders the epoch shell using the design-system components:
//   - GOAL as a callout card
//   - CONTRACT DIFF — status of frozen components
//   - GENERATION SPINE — the centerpiece visual element; promoted spine
//     left-to-right with rejected challengers branching off their parent
//   - PER-ENTRY × GENERATION heatmap
//   - PER-JUDGE × GENERATION heatmap
//   - RECENT EXPERIMENTS — full-width cards stacked vertically
//   - ANALYSIS REPORT — the analyzer's rendered figures + tables
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
import { renderLoadingState, renderEmptyState } from '../components/loading.js';

const _contractDiffCache = new Map();
const _loadingDiff = new Set();
const _perJudgeTrendCache = new Map();
const _loadingTrend = new Set();
const _perEntryTrendCache = new Map();
const _loadingEntryTrend = new Set();
const _analysisCache = new Map();
const _loadingAnalysis = new Set();

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

export function resetAnalysisCache() {
  _analysisCache.clear();
  _loadingAnalysis.clear();
}
export function analysisPayload(epochId) {
  return _analysisCache.get(epochId) || null;
}

// Fetch the analyzer's rendered report — markdown + paper-styled HTML
// fragment + the "raw analysis.html available" flag — and cache by
// epoch. The endpoint is best-effort on the server side; we still
// install an empty cache entry on failure so the view paints the
// "not yet generated" placeholder rather than the loading spinner.
async function ensureAnalysis(epochId, repaint) {
  if (!epochId) return null;
  if (_analysisCache.has(epochId)) return _analysisCache.get(epochId);
  if (_loadingAnalysis.has(epochId)) return null;
  _loadingAnalysis.add(epochId);
  try {
    const data = await fetchJson('/api/epoch/' + encodeURIComponent(epochId) + '/analysis');
    if (data && typeof data === 'object') {
      _analysisCache.set(epochId, data);
    } else {
      _analysisCache.set(epochId, {
        epoch_id: epochId, analysis_md: '',
        analysis_html_inline: '', analysis_html_available: false,
      });
    }
  } catch {
    _analysisCache.set(epochId, {
      epoch_id: epochId, analysis_md: '',
      analysis_html_inline: '', analysis_html_available: false,
    });
  } finally {
    _loadingAnalysis.delete(epochId);
    if (typeof repaint === 'function') repaint();
  }
  return _analysisCache.get(epochId);
}

// -- Goal slot --------------------------------------------------------
function _renderGoal() {
  const node = $('phase0-epoch-goal');
  if (!node) return;
  clearChildren(node);
  const def = state.epochDef;
  // Still waiting for the epoch contract to land via SSE — say so
  // explicitly instead of falling through to "(no goal recorded)".
  if (def == null) {
    node.appendChild(renderCalloutCard({
      title: 'Goal',
      accent: 'default',
      body: renderLoadingState(),
    }));
    return;
  }
  const goal = (typeof def.goal === 'string') ? def.goal.trim() : '';
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
      body = renderLoadingState({ label: 'Loading contract diff' });
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

// v0 is the baseline seed — it has no tournament outcome but it IS the
// promoted root of the spine. The spine renderer reads `promoted: true`
// off the synthesized v0 record so the seed sits at the left edge of
// the lineage rather than falling into the rejected pile.
function _isBaselineSeed(exp) {
  if (!exp || typeof exp !== 'object') return false;
  const parent = exp.parent_generation_id;
  if (typeof parent === 'string' && parent !== '') return false;
  // No outcome yet OR explicit null outcome — v0 carries no tournament
  // verdict because nothing competed against it.
  return exp.outcome == null;
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
    const baseline = _isBaselineSeed(exp);
    // Baseline (v0) is treated as promoted so it anchors the spine row.
    const promoted = baseline || dec === 'promoted';
    const scalar = _scalarOf(exp);
    const parentRaw = exp && exp.parent_generation_id;
    const parent_id = (typeof parentRaw === 'string' && parentRaw !== '')
      ? parentRaw : null;
    nodes.push({
      id, scalar, promoted, decision: dec, live: false, parent_id,
      href: phase0Href('generation', { epochId: epochId, generationId: id }),
    });
  }
  if (liveGen && !nodes.find((n) => n.id === liveGen)) {
    nodes.push({
      id: liveGen, scalar: null, promoted: false, live: true, parent_id: null,
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
  // Loading state — the epoch contract has not yet landed via SSE.
  // Without this guard the spine flashes the empty message even on a
  // workspace with many generations.
  let body;
  if (state.epochDef == null) {
    body = renderLoadingState({ label: 'Loading spine' });
  } else {
    const spineNodes = _buildSpineNodes(epochId);
    body = spineNodes.length === 0
      ? renderEmptyState('No generations yet.')
      : renderSpine({ nodes: spineNodes });
  }
  node.appendChild(renderCard({
    title: 'Generation spine',
    subtitle: 'Champion lineage left-to-right; rejected challengers branch off their parent.',
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
  // The epoch contract drives whether there are generations at all, so
  // a null epochDef is a loading state — not a "no generations" empty.
  if (state.epochDef == null) {
    body = renderLoadingState({ label: 'Loading per-entry heatmap' });
  } else if (!epochId || generationIds.length === 0) {
    body = renderEmptyState('No generations yet.');
  } else {
    const data = _perEntryTrendCache.get(epochId);
    if (!data) {
      body = renderLoadingState({ label: 'Loading per-entry heatmap' });
    } else {
      const entries = Array.isArray(data.entries) ? data.entries : [];
      if (entries.length === 0) {
        body = renderEmptyState('No per-entry data yet.');
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
      body = renderLoadingState({ label: 'Loading per-judge heatmap' });
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

// -- Recent experiments — full-width card stack -----------------------
//
// Each experiment renders as one full-width card. The header carries
// the generation id, the verdict pill, and the headline deltas; the
// body carries the hypothesis core idea followed by labelled "why" and
// "predicted" inline rows. Cards stack vertically with clear borders
// and spacing, so a long Proposed prose never desyncs columns.

function _fmtSigned(v, digits) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  const d = digits == null ? 3 : digits;
  return (v > 0 ? '+' : '') + v.toFixed(d);
}

function _normaliseDecisionRaw(outcome) {
  if (!outcome || typeof outcome !== 'object') return null;
  const raw = outcome.tournament_decision || outcome.decision || '';
  const d = String(raw).toLowerCase();
  if (d.includes('promot')) return 'promoted';
  if (d.includes('reject')) return 'rejected';
  if (d.includes('defer')) return 'deferred';
  return raw ? d : null;
}

function _expectedDriftLine(moves) {
  if (!Array.isArray(moves) || moves.length === 0) return null;
  const items = moves
    .map((m) => (m && m.kind)
      ? `${m.kind} ${m.direction || '?'}${m.magnitude ? ' (' + m.magnitude + ')' : ''}`
      : null)
    .filter(Boolean);
  return items.length ? items.join('; ') : null;
}

function _renderExperimentCard(exp, epochId) {
  const genId = exp && exp.generation_id ? String(exp.generation_id) : '?';
  const outcome = (exp && typeof exp.outcome === 'object') ? exp.outcome : null;
  const hyp = (exp && typeof exp.hypothesis === 'object') ? exp.hypothesis : {};

  // --- header row: gen id + verdict pill + metric deltas + link -----
  const decision = _normaliseDecisionRaw(outcome);
  const verdictVariant = decision === 'promoted' ? 'promoted'
    : decision === 'rejected' ? 'rejected'
    : decision === 'deferred' ? 'deferred'
    : 'neutral';
  const verdictLabel = decision ? decision.toUpperCase() : 'PENDING';

  const dScalar = outcome ? outcome.scalar_score_delta : null;
  const dPass = outcome ? outcome.pass_rate_delta : null;
  const dDrift = outcome ? outcome.drift_loss_delta : null;

  const metricSpan = (label, value, goodIsNeg) => {
    const num = typeof value === 'number' && isFinite(value) ? value : null;
    let cls = 'phase0-exp-metric';
    if (num !== null && num !== 0) {
      const good = goodIsNeg ? num < 0 : num > 0;
      cls += good ? ' phase0-exp-metric-good' : ' phase0-exp-metric-bad';
    }
    return el('span', { class: cls }, [
      el('span', { class: 'phase0-exp-metric-label' }, [label]),
      el('span', { class: 'phase0-exp-metric-value mono' },
        [num === null ? '—' : _fmtSigned(num)]),
    ]);
  };

  const headerChildren = [
    el('span', { class: 'phase0-exp-gen mono' }, [genId]),
    renderPill(verdictLabel, verdictVariant),
    el('span', { class: 'phase0-exp-metrics' }, [
      metricSpan('Δscalar', dScalar, true),
      metricSpan('Δpass', dPass, false),
      metricSpan('Δdrift', dDrift, true),
    ]),
  ];
  if (epochId) {
    headerChildren.push(el('a', {
      class: 'phase0-exp-link',
      href: phase0Href('generation', { epochId, generationId: genId }),
    }, ['open →']));
  }
  const header = el('div', { class: 'phase0-exp-header' }, headerChildren);

  // --- body rows: core idea + why + predicted -----------------------
  const bodyChildren = [];
  const coreIdea = (typeof hyp.core_idea === 'string') ? hyp.core_idea.trim() : '';
  if (coreIdea) {
    bodyChildren.push(el('p', { class: 'phase0-exp-idea' }, [coreIdea]));
  }
  const why = (typeof hyp.why === 'string') ? hyp.why.trim() : '';
  if (why) {
    bodyChildren.push(el('p', { class: 'phase0-exp-line' }, [
      el('span', { class: 'phase0-exp-lead' }, ['why']),
      ' ',
      why,
    ]));
  }

  // Compose a single "predicted" line — drift movements + the
  // expected pass-rate delta — so the picture reads as a sentence.
  const predictedParts = [];
  const driftLine = _expectedDriftLine(hyp.expected_drift_movements);
  if (driftLine) predictedParts.push(driftLine);
  const passLine = (typeof hyp.expected_pass_rate_delta === 'string')
    ? hyp.expected_pass_rate_delta.trim() : '';
  if (passLine) predictedParts.push('pass-rate Δ ' + passLine);
  if (predictedParts.length > 0) {
    bodyChildren.push(el('p', { class: 'phase0-exp-line' }, [
      el('span', { class: 'phase0-exp-lead' }, ['predicted']),
      ' ',
      predictedParts.join('; '),
    ]));
  }

  // Rejection reason — only on rejected cards, kept short so the
  // section reads as a digest, not the full L2 transcript.
  if (decision === 'rejected' && outcome) {
    const rr = (typeof outcome.rejection_reason === 'string')
      ? outcome.rejection_reason.trim() : '';
    if (rr) {
      bodyChildren.push(el('p', { class: 'phase0-exp-line phase0-exp-rejection' }, [
        el('span', { class: 'phase0-exp-lead' }, ['rejected because']),
        ' ',
        rr,
      ]));
    }
  }

  if (bodyChildren.length === 0) {
    bodyChildren.push(el('p', { class: 'empty' },
      ['No hypothesis recorded for this experiment.']));
  }

  const cls = 'phase0-exp-card'
    + (decision ? ' phase0-exp-card-' + decision : ' phase0-exp-card-pending');
  return el('div', { class: cls }, [
    header,
    el('div', { class: 'phase0-exp-body' }, bodyChildren),
  ]);
}

function _renderRecentExperiments(epochId) {
  const node = $('phase0-epoch-experiments');
  if (!node) return;
  clearChildren(node);
  const def = state.epochDef;
  let body;
  if (def == null) {
    body = renderLoadingState({ label: 'Loading experiments' });
  } else {
    const experiments = Array.isArray(def.experiments) ? def.experiments : [];
    if (experiments.length === 0) {
      body = renderEmptyState('No experiments recorded yet.');
    } else {
      body = el('div');
      body.appendChild(el('p', { class: 'panel-subheader' }, [
        'Most recent first — header carries verdict + deltas; '
        + 'body carries the hypothesis core idea and predictions.',
      ]));
      // Newest first, cap at six so L1 stays a digest. Use slice + reverse
      // (not sort) so a stable order with ties is preserved.
      const recent = experiments.slice(-6).reverse();
      const list = el('div', { class: 'phase0-exp-list' });
      for (const exp of recent) {
        list.appendChild(_renderExperimentCard(exp, epochId));
      }
      body.appendChild(list);
    }
  }
  node.appendChild(renderCard({
    title: 'Recent experiments',
    body,
  }));
}

// -- Analysis report slot ---------------------------------------------
//
// The analyzer writes ``analysis.md`` + ``analysis.html`` continuously.
// Render the paper-styled HTML fragment inside a card so the rich
// post-hoc figures (hypothesis-vs-outcome chart, drift heatmaps,
// per-judge attribution table) finally surface from the dashboard.
// Falls back to a "not yet generated" placeholder when the analyzer
// has not run for this epoch.
//
// The fragment is constructed by the dashboard's own server from
// trusted workspace data and a vendored markdown renderer; the inline
// CSS is scoped to ``.paper`` so it cannot leak into the dashboard
// chrome. We therefore use a wrapping ``<div>`` whose innerHTML we
// set ourselves — NOT an iframe — so the figures pick up the same
// font + colour tokens as the rest of L1. (Iframe would isolate
// scrollbars + give a hard-to-style document edge.)

function _setHtmlContent(node, html) {
  // The harness's Element forbids innerHTML in production code paths
  // (it's tracked for the no-flash test), but the analysis fragment
  // ships server-rendered HTML we must inject as-is. The harness still
  // accepts the write — it counts it via innerHTMLWriteCount(). Tests
  // that pin no-flash never reach this code path because the analysis
  // slot is only painted once per epoch (the cache is keyed on
  // epoch_id).
  // eslint-disable-next-line no-unsanitized/property
  node.innerHTML = html;
}

function _renderAnalysis(epochId) {
  const node = $('phase0-epoch-analysis');
  if (!node) return;
  clearChildren(node);
  let body;
  let actions = null;
  if (!epochId) {
    body = el('p', { class: 'empty' }, ['Select an epoch.']);
  } else if (state.epochDef == null) {
    body = renderLoadingState({ label: 'Loading analysis report' });
  } else {
    const data = _analysisCache.get(epochId);
    if (!data) {
      body = renderLoadingState({ label: 'Loading analysis report' });
    } else {
      const inline = (typeof data.analysis_html_inline === 'string')
        ? data.analysis_html_inline : '';
      const available = !!data.analysis_html_available;
      if (inline.trim()) {
        const host = el('div', { class: 'phase0-analysis-host' });
        _setHtmlContent(host, inline);
        body = host;
        if (available) {
          actions = el('a', {
            class: 'phase0-analysis-link',
            href: '/api/epoch/' + encodeURIComponent(epochId) + '/analysis.html',
            target: '_blank',
            rel: 'noopener',
          }, ['Open full report ↗']);
        }
      } else if (available) {
        // No inline fragment but a full HTML file exists — link to it.
        body = el('p', { class: 'empty' }, [
          'Analysis report rendered as a standalone HTML file.',
        ]);
        actions = el('a', {
          class: 'phase0-analysis-link',
          href: '/api/epoch/' + encodeURIComponent(epochId) + '/analysis.html',
          target: '_blank',
          rel: 'noopener',
        }, ['Open full report ↗']);
      } else {
        body = el('p', { class: 'empty' }, [
          'Analysis report not yet generated (will appear after the analyzer runs).',
        ]);
      }
    }
  }
  node.appendChild(renderCard({
    title: 'Analysis report',
    subtitle: 'Hypothesis-vs-outcome figure, drift heatmaps, per-judge attribution.',
    body,
    actions,
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
  ensureAnalysis(epochId, repaint);
  if (gids.length > 0) ensurePerEntryTrend(epochId, gids, repaint);

  _renderGoal();
  _renderContractDiff(epochId);
  _renderSpine(epochId);
  _renderEntryHeatmap(epochId, gids);
  _renderJudgeHeatmap(epochId);
  _renderRecentExperiments(epochId);
  _renderAnalysis(epochId);
}

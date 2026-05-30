// views/phase0_epoch.js — L1 (epoch-level) view, redesigned.
//
// The L1 view answers, top to bottom: "is this loop meaningful, and what
// has it done this epoch?" The redesign threads that question through:
//
//   - LOOP-HEALTH BANNER — fetched from /api/health-report; surfaces
//     degenerate scoring / dead entries / flat drift at the very top.
//   - EPOCH STORY HEADER — a compact at-a-glance line: goal · frozen
//     contract rollup · #generations · trajectory direction · current
//     champion · health rollup.
//   - LINEAGE RIBBON — replaces the flat generation spine. The promoted
//     lineage traces the optimization curve (lower loss higher on screen),
//     rejected challengers branch off their parent, the live node pulses
//     at the right edge. Clicking a node drills to L2.
//   - CONTRACT DIFF — reframed to "what changed to roll this epoch":
//     which frozen components (board / brief / scoring / harness) moved.
//   - HEATMAPS — the per-entry×gen and per-judge×gen heatmaps fold into
//     ONE card with an entries/judges tab toggle.
//   - RECENT EXPERIMENTS — full-width cards; verdict iconography speaks
//     through the shared verdictGlyph.
//   - ANALYSIS REPORT — the analyzer's rendered figures + tables.
//
// The health banner + story header render INTO the goal slot
// (#phase0-epoch-goal) since index.html is out of scope; the tabbed
// heatmap renders into the entries slot and the (now redundant) judges
// slot is cleared. Each section renders into its pre-existing slot so
// tests reading those slots find the expected text. Re-render safe;
// degrades gracefully on missing data.

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';
import { fmtScalar } from '../core/format.js';
import { phase0Href } from './phase0_router.js';
import { renderCard, renderCalloutCard } from '../components/card.js';
import { renderPill } from '../components/pill.js';
import { lineageRibbon } from '../components/lineage_ribbon.js';
import { healthBanner } from '../components/health_banner.js';
import { verdictGlyph } from '../components/verdict_glyph.js';
import { renderHeatmapTable } from '../components/heatmap.js';
import { renderMetricTile } from '../components/tile.js';
import { renderLoadingState, renderEmptyState } from '../components/loading.js';
import { harmonografLink } from '../core/harmonograf.js';

const _contractDiffCache = new Map();
const _loadingDiff = new Set();
const _perJudgeTrendCache = new Map();
const _loadingTrend = new Set();
const _perEntryTrendCache = new Map();
const _loadingEntryTrend = new Set();
const _analysisCache = new Map();
const _loadingAnalysis = new Set();

// The active heatmap tab — survives re-render so a repaint does not
// snap the user back to "entries". Module-level (one L1 at a time).
let _heatmapTab = 'entries';
export function resetHeatmapTab() { _heatmapTab = 'entries'; }

// -- Loop-health report -----------------------------------------------
const _healthCache = { report: undefined };
const _loadingHealth = { busy: false };

export function resetHealthReportCache() {
  _healthCache.report = undefined;
  _loadingHealth.busy = false;
}
export function healthReportPayload() {
  return _healthCache.report === undefined ? null : _healthCache.report;
}

// Fetch GET /api/health-report once per render cycle and cache it. The
// report shape — { epoch_id, healthy, findings: [{ code, severity,
// summary, detail }], checked_at? } — maps straight onto healthBanner.
// On failure we fall back to whatever the SSE snapshot folded into
// state.healthReport, then to null (banner degrades to "not yet
// evaluated").
async function ensureHealthReport(repaint) {
  if (_healthCache.report !== undefined) return _healthCache.report;
  if (_loadingHealth.busy) return null;
  _loadingHealth.busy = true;
  try {
    const data = await fetchJson('/api/health-report');
    _healthCache.report = (data && typeof data === 'object') ? data : null;
  } catch {
    _healthCache.report = (state.healthReport && typeof state.healthReport === 'object')
      ? state.healthReport : null;
  } finally {
    _loadingHealth.busy = false;
    if (typeof repaint === 'function') repaint();
  }
  return _healthCache.report;
}

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

// =====================================================================
// Decision / scalar helpers (shared by the ribbon, the story header and
// the experiment cards).
// =====================================================================
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
// promoted root of the lineage. We treat a parentless, outcome-less node
// as the promoted root so the seed anchors the ribbon's spine rather
// than falling into the rejected branches.
function _isBaselineSeed(exp) {
  if (!exp || typeof exp !== 'object') return false;
  const parent = exp.parent_generation_id;
  if (typeof parent === 'string' && parent !== '') return false;
  return exp.outcome == null;
}

// =====================================================================
// Loop-health banner — top of L1, rendered into the goal slot.
// =====================================================================
function _renderHealthBanner(node) {
  const report = healthReportPayload();
  // healthBanner degrades to "health not yet evaluated" on a null
  // report, so a still-loading fetch reads as the neutral muted strip.
  node.appendChild(healthBanner({ report }));
}

// A one-word rollup of the loop's health for the story header.
function _healthRollup() {
  const report = healthReportPayload();
  if (!report || typeof report !== 'object') return { word: 'unknown', kind: 'muted' };
  const findings = Array.isArray(report.findings) ? report.findings.filter(Boolean) : [];
  let rank = 0;
  for (const f of findings) {
    const s = String((f && f.severity) || '').toLowerCase();
    const r = s === 'critical' ? 3 : s === 'warning' ? 2 : s === 'info' ? 1 : 0;
    if (r > rank) rank = r;
  }
  if (rank >= 3) return { word: 'critical', kind: 'rejected' };
  if (rank >= 2) return { word: 'degraded', kind: 'warning' };
  return report.healthy === false
    ? { word: 'degraded', kind: 'warning' }
    : { word: 'healthy', kind: 'promoted' };
}

// =====================================================================
// Epoch story header — the compact at-a-glance line.
// =====================================================================

// Trajectory: did the best (lowest) scalar improve over the epoch? We
// compare the first scored generation's scalar to the best scalar seen.
function _trajectory(experiments) {
  const scored = experiments
    .map((e) => _scalarOf(e))
    .filter((s) => typeof s === 'number' && isFinite(s));
  if (scored.length === 0) return { dir: 'flat', glyph: '·', label: 'no signal yet' };
  const first = scored[0];
  const best = Math.min(...scored);
  if (best < first - 1e-9) return { dir: 'down', glyph: '↘', label: 'improving' };
  if (best > first + 1e-9) return { dir: 'up', glyph: '↗', label: 'regressing' };
  return { dir: 'flat', glyph: '→', label: 'flat' };
}

// The current champion — the promoted node with the best (lowest)
// scalar, falling back to the last promoted node, then the baseline.
function _currentChampion(experiments) {
  const promoted = experiments.filter((e) => {
    const d = _decisionOf(e);
    return d === 'promoted' || _isBaselineSeed(e);
  });
  if (promoted.length === 0) return null;
  let champ = null;
  let bestScalar = Infinity;
  for (const e of promoted) {
    const s = _scalarOf(e);
    if (typeof s === 'number' && isFinite(s) && s < bestScalar) {
      bestScalar = s; champ = e;
    }
  }
  if (!champ) champ = promoted[promoted.length - 1];
  return {
    id: champ.generation_id || '?',
    scalar: _scalarOf(champ),
  };
}

function _storyChip(label, valueNode, opts) {
  const o = opts || {};
  const cls = 'epoch-story-chip'
    + (o.kind ? ' epoch-story-chip-' + o.kind : '');
  return el('div', { class: cls }, [
    el('span', { class: 'epoch-story-chip-label' }, [label]),
    el('span', { class: 'epoch-story-chip-value' },
      [typeof valueNode === 'string' ? valueNode : valueNode]),
  ]);
}

function _renderStoryHeader(node, epochId) {
  const def = state.epochDef;
  if (def == null) {
    node.appendChild(renderCalloutCard({
      title: 'Epoch', accent: 'default', body: renderLoadingState(),
    }));
    return;
  }
  const experiments = Array.isArray(def.experiments) ? def.experiments : [];
  const goal = (typeof def.goal === 'string') ? def.goal.trim() : '';
  const isClosed = !!def.closed;

  // Contract rollup — how many frozen components changed vs predecessor.
  const diff = epochId ? _contractDiffCache.get(epochId) : null;
  let contractWord = 'pending';
  let contractKind = 'muted';
  if (diff) {
    if (!diff.predecessor_epoch_id) {
      contractWord = 'first epoch';
      contractKind = 'muted';
    } else {
      const comps = Array.isArray(diff.components) ? diff.components : [];
      const changed = comps.filter((c) => c && c.changed).map((c) => c.name);
      if (changed.length === 0) {
        contractWord = 'unchanged';
        contractKind = 'muted';
      } else {
        contractWord = changed.join(' · ');
        contractKind = 'warning';
      }
    }
  }

  const traj = _trajectory(experiments);
  const champ = _currentChampion(experiments);
  const health = _healthRollup();

  // -- the goal line (kept prominent; replaces the old goal callout) --
  const goalNode = goal
    ? el('p', { class: 'epoch-story-goal' }, [goal])
    : el('p', { class: 'epoch-story-goal epoch-story-goal-empty' }, [
        '(no goal recorded) — set via ',
        el('code', { class: 'mono' }, ['zicato epoch set-goal --epoch <id> --goal "..."']),
      ]);

  // -- the chip strip -------------------------------------------------
  const chips = el('div', { class: 'epoch-story-chips' }, [
    _storyChip('state', isClosed ? 'closed' : 'open',
      { kind: isClosed ? 'muted' : 'promoted' }),
    _storyChip('generations', String(experiments.length)),
    _storyChip('contract', contractWord, { kind: contractKind }),
    _storyChip('trajectory',
      el('span', { class: 'epoch-story-traj epoch-story-traj-' + traj.dir }, [
        el('span', { class: 'epoch-story-traj-glyph', 'aria-hidden': 'true' }, [traj.glyph]),
        ' ' + traj.label,
      ]),
      { kind: traj.dir === 'down' ? 'promoted' : traj.dir === 'up' ? 'rejected' : 'muted' }),
    _storyChip('champion',
      champ
        ? el('span', { class: 'epoch-story-champ' }, [
            el('span', { class: 'mono' }, [String(champ.id)]),
            champ.scalar != null
              ? el('span', { class: 'epoch-story-champ-scalar mono' },
                  [' · ' + fmtScalar(champ.scalar)])
              : null,
          ].filter(Boolean))
        : '—',
      { kind: champ ? 'promoted' : 'muted' }),
    _storyChip('health', health.word, { kind: health.kind }),
  ]);

  node.appendChild(renderCalloutCard({
    title: 'Epoch story',
    accent: goal ? 'accent' : 'warning',
    body: el('div', { class: 'epoch-story' }, [goalNode, chips]),
  }));
}

// -- Goal slot — now hosts the health banner + the story header -------
function _renderEpochHead(epochId) {
  const node = $('phase0-epoch-goal');
  if (!node) return;
  clearChildren(node);
  const wrap = el('div', { class: 'epoch-head' });
  _renderHealthBanner(wrap);
  _renderStoryHeader(wrap, epochId);
  node.appendChild(wrap);
}

// -- Contract diff slot — "what changed to roll this epoch" -----------
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
      const changed = comps.filter((c) => c && c.changed);
      const unchanged = comps.filter((c) => c && !c.changed);

      const wrap = el('div', { class: 'contract-diff' });
      // Headline: what rolled this epoch.
      if (changed.length === 0) {
        wrap.appendChild(el('p', { class: 'contract-diff-headline contract-diff-headline-same' }, [
          'Nothing changed — the frozen contract carries over from ',
          el('span', { class: 'mono' }, [String(data.predecessor_epoch_id)]),
          '.',
        ]));
      } else {
        const lead = el('p', { class: 'contract-diff-headline' }, [
          'Rolled by ',
          el('strong', null, [String(changed.length)]),
          changed.length === 1 ? ' component change vs ' : ' component changes vs ',
          el('span', { class: 'mono' }, [String(data.predecessor_epoch_id)]),
          ':',
        ]);
        wrap.appendChild(lead);
        const chipRow = el('div', { class: 'contract-diff-chips' });
        for (const c of changed) {
          chipRow.appendChild(el('span', { class: 'contract-diff-chip' }, [
            el('span', { class: 'mono' }, [String(c.name)]),
            el('span', { class: 'contract-diff-hash mono' }, [
              (c.previous_hash ? c.previous_hash.slice(0, 7) : '—')
              + ' → '
              + (c.current_hash ? c.current_hash.slice(0, 7) : '—'),
            ]),
          ]));
        }
        wrap.appendChild(chipRow);
      }
      // The carry-over components, named compactly so the reader can see
      // what stayed frozen.
      if (unchanged.length > 0) {
        wrap.appendChild(el('p', { class: 'contract-diff-carry' }, [
          el('span', { class: 'contract-diff-carry-label' }, ['unchanged: ']),
          unchanged.map((c) => String(c.name)).join(' · '),
        ]));
      }
      body = wrap;
    }
  }
  node.appendChild(renderCard({
    title: 'Contract diff',
    subtitle: 'What changed to roll this epoch — board / brief / scoring / harness vs predecessor.',
    body,
  }));
}

// =====================================================================
// Lineage ribbon slot — replaces the flat generation spine.
// =====================================================================

// Build the ribbon node list from this epoch's experiments + the live
// heartbeat. Shape per node: { id, parentId, scalar, verdict, live,
// label }. verdict ∈ 'promoted' | 'rejected' | (else open).
function _buildRibbonNodes(epochId) {
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
    const verdict = (baseline || dec === 'promoted') ? 'promoted'
      : dec === 'rejected' ? 'rejected'
      : 'open';
    const parentRaw = exp && exp.parent_generation_id;
    const parentId = (typeof parentRaw === 'string' && parentRaw !== '')
      ? parentRaw : null;
    nodes.push({
      id, parentId, scalar: _scalarOf(exp), verdict, live: false, label: id,
    });
  }
  if (liveGen && !nodes.find((n) => n.id === liveGen)) {
    nodes.push({
      id: liveGen, parentId: null, scalar: null, verdict: 'open', live: true, label: liveGen,
    });
  } else if (liveGen) {
    const idx = nodes.findIndex((n) => n.id === liveGen);
    if (idx >= 0) nodes[idx].live = true;
  }
  return nodes;
}

function _renderRibbon(epochId) {
  const node = $('phase0-epoch-spine');
  if (!node) return;
  clearChildren(node);
  let body;
  if (state.epochDef == null) {
    body = renderLoadingState({ label: 'Loading lineage' });
  } else {
    const nodes = _buildRibbonNodes(epochId);
    body = nodes.length === 0
      ? renderEmptyState('No generations yet.')
      : lineageRibbon({
          nodes,
          zoom: 'generations',
          onSelect: (id) => {
            if (!epochId || !id) return;
            const target = phase0Href('generation', { epochId, generationId: id });
            if (typeof window !== 'undefined' && window.location
                && window.location.hash !== target) {
              window.location.hash = target;
            }
          },
        });
  }
  // When there is a live run on THIS epoch, surface a harmonograf
  // deep-link as a card action — the ribbon doubles as the L1 live
  // callout. Degrades to the bare base URL when no run has surfaced an
  // adk_session_id yet, and renders nothing when no harmonograf_url is
  // configured.
  const hb = state.heartbeat || {};
  const liveHere = hb.epoch_id === epochId && !!hb.generation_id;
  let actions = null;
  if (liveHere) {
    const activeRunList = Array.isArray(state.activeRuns) ? state.activeRuns : [];
    const firstRun = activeRunList.length > 0 ? activeRunList[0] : null;
    actions = firstRun
      ? harmonografLink(firstRun, 'Open in harmonograf')
      : harmonografLink({}, 'Open in harmonograf');
  }
  node.appendChild(renderCard({
    title: 'Lineage ribbon',
    subtitle: 'Champion lineage traces the loss curve (lower = higher); '
      + 'rejected challengers branch off their parent; the live node pulses at the right.',
    accent: 'accent',
    body,
    actions,
  }));
}

// =====================================================================
// Tabbed heatmap slot — entries / judges folded into one card.
// =====================================================================

function _entriesHeatmapBody(epochId, generationIds) {
  if (state.epochDef == null) {
    return renderLoadingState({ label: 'Loading per-entry heatmap' });
  }
  if (!epochId || generationIds.length === 0) {
    return renderEmptyState('No generations yet.');
  }
  const data = _perEntryTrendCache.get(epochId);
  if (!data) return renderLoadingState({ label: 'Loading per-entry heatmap' });
  const entries = Array.isArray(data.entries) ? data.entries : [];
  if (entries.length === 0) return renderEmptyState('No per-entry data yet.');
  return renderHeatmapTable({
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

function _judgesHeatmapBody(epochId) {
  if (!epochId) return el('p', { class: 'empty' }, ['Select an epoch.']);
  const data = _perJudgeTrendCache.get(epochId);
  if (!data) return renderLoadingState({ label: 'Loading per-judge heatmap' });
  const generations = Array.isArray(data.generations) ? data.generations : [];
  const judges = Array.isArray(data.judges) ? data.judges : [];
  if (generations.length === 0 || judges.length === 0) {
    const msg = data.note ? '(no per-judge data: ' + data.note + ')'
      : '(no per-judge data recorded for this epoch yet)';
    return el('p', { class: 'empty' }, [msg]);
  }
  return renderHeatmapTable({
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

function _renderHeatmaps(epochId, generationIds, repaint) {
  const node = $('phase0-epoch-heatmap-entries');
  if (!node) return;
  clearChildren(node);

  // -- tab strip ------------------------------------------------------
  const tabs = el('div', { class: 'heatmap-tabs', role: 'tablist' });
  const makeTab = (key, label, sub) => {
    const active = _heatmapTab === key;
    const btn = el('button', {
      type: 'button',
      class: 'heatmap-tab' + (active ? ' heatmap-tab-active' : ''),
      role: 'tab',
      'aria-selected': active ? 'true' : 'false',
      'data-heatmap-tab': key,
    }, [
      el('span', { class: 'heatmap-tab-label' }, [label]),
      el('span', { class: 'heatmap-tab-sub' }, [sub]),
    ]);
    btn.addEventListener('click', () => {
      if (_heatmapTab === key) return;
      _heatmapTab = key;
      if (typeof repaint === 'function') repaint();
      else _renderHeatmaps(epochId, generationIds, repaint);
    });
    return btn;
  };
  tabs.appendChild(makeTab('entries', 'entries', 'drift loss per board entry'));
  tabs.appendChild(makeTab('judges', 'judges', 'weighted loss per judge'));

  const panel = el('div', {
    class: 'heatmap-panel',
    role: 'tabpanel',
    'data-heatmap-panel': _heatmapTab,
  }, [
    _heatmapTab === 'judges'
      ? _judgesHeatmapBody(epochId)
      : _entriesHeatmapBody(epochId, generationIds),
  ]);

  node.appendChild(renderCard({
    title: 'Loss heatmaps',
    subtitle: 'Per-entry and per-judge drift across the lineage — toggle between views.',
    body: el('div', { class: 'heatmap-card' }, [tabs, panel]),
  }));
}

// The redundant judges slot is folded into the tabbed card above; clear
// it so the static placeholder does not linger under the combined card.
function _clearJudgeSlot() {
  const node = $('phase0-epoch-heatmap-judges');
  if (node) clearChildren(node);
}

// =====================================================================
// Recent experiments — full-width card stack.
// =====================================================================

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

  // --- header row: gen id + verdict glyph + metric deltas + link -----
  const decision = _normaliseDecisionRaw(outcome);
  // The shared verdict glyph is the single source of verdict iconography
  // dashboard-wide; 'pending' covers a not-yet-decided experiment.
  const glyph = verdictGlyph(decision || 'pending');

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
    glyph,
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

// =====================================================================
// Analysis report slot.
// =====================================================================

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

  ensureHealthReport(repaint);
  ensureContractDiff(epochId, repaint);
  ensurePerJudgeTrend(epochId, repaint);
  ensureAnalysis(epochId, repaint);
  if (gids.length > 0) ensurePerEntryTrend(epochId, gids, repaint);

  _renderEpochHead(epochId);
  _renderContractDiff(epochId);
  _renderRibbon(epochId);
  _renderHeatmaps(epochId, gids, repaint);
  _clearJudgeSlot();
  _renderRecentExperiments(epochId);
  _renderAnalysis(epochId);
}

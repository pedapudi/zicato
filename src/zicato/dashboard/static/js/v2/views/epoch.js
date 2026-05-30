// js/v2/views/epoch.js — the v2 Epoch view.
//
// DASHBOARD-V2 §4.3 — the Epoch answers *"what are we trying to learn &
// what have we learned?"*. It is the Notebook's lab-bench page: a frozen
// statement of intent (goal + contract), a dense experiment ledger (one
// row per generation, the whole epoch's reasoning scannable at a glance),
// the comparative drift/loss landscape (entry × gen / judge × gen
// heatmap), and the epoch's publication — the ACM report, embedded inline
// AND reachable as the full-page standalone (verbatim; §6).
//
// Density over cards (§3): the ledger is a real dataTable with semantic
// delta coloring (green improve / red regress, glyph-redundant), and the
// landscape is a real heatmap, not a stack of hero numbers. Every ledger
// row is a door (§2.5) — it drills to #/v2/experiment/{genId}. Comparison
// is the default unit of meaning (§2.2): every delta is vs the parent.
//
// Honest states (§2.4): every async section (contract diff, per-judge
// trend, per-entry trend, analysis) renders through `stateBlock` — a
// not-yet-built index says so plainly with the actionable hint, never a
// bare blank.
//
// The view is a pure renderer keyed on `route.epochId` (defaulting to the
// current epoch from state). It self-registers with the shell. Async
// fetches populate module caches and call back to re-render in place.

import { $, el, clearChildren, swapIfChanged } from '../../core/dom.js';
import { fetchJson } from '../../core/api.js';
import { state } from '../../core/state.js';
import { fmtScalar } from '../../core/format.js';
import { registerView } from '../shell.js';
import { v2Router, v2Href } from '../router.js';
import { dataTable, deltaCell } from '../components/dataTable.js';
import { stateBlock } from '../components/stateBlock.js';
import { verdictGlyph } from '../../components/verdict_glyph.js';
import { renderHeatmapTable } from '../../components/heatmap.js';

// =====================================================================
// Async caches. Each keyed by epoch id; a value of `undefined` means
// "never fetched", an installed entry means "fetched (maybe empty)", and
// a membership in the matching `_loading` set means "in flight". This is
// what lets the renderer distinguish not-yet / running / empty honestly.
// =====================================================================
const _epochCache = new Map();      // epochId → /api/epoch payload
const _loadingEpoch = new Set();
const _contractDiffCache = new Map();
const _loadingDiff = new Set();
const _perJudgeTrendCache = new Map();
const _loadingTrend = new Set();
const _perEntryTrendCache = new Map();
const _loadingEntryTrend = new Set();
const _analysisCache = new Map();
const _loadingAnalysis = new Set();

// The active landscape facet — entries | judges. Survives a re-render so
// a repaint does not snap the operator back to "entries".
let _landscapeFacet = 'entries';

export function resetEpochView() {
  _epochCache.clear(); _loadingEpoch.clear();
  _contractDiffCache.clear(); _loadingDiff.clear();
  _perJudgeTrendCache.clear(); _loadingTrend.clear();
  _perEntryTrendCache.clear(); _loadingEntryTrend.clear();
  _analysisCache.clear(); _loadingAnalysis.clear();
  _landscapeFacet = 'entries';
}

// -- the canonical repaint: re-run the registered renderer for the
// current route so a settled fetch flows into the DOM in place.
function _repaint() {
  const host = $('v2-view');
  if (host) renderEpoch(host, v2Router.current());
}

// =====================================================================
// Fetch helpers — each installs an empty payload on failure so the
// section paints an honest empty/broken state, never a stuck spinner.
// =====================================================================
async function ensureEpoch(epochId) {
  // The current epoch is also folded into state.epochDef by the env
  // poll; prefer it when the requested epoch matches so the first paint
  // is never blank. A specific (non-current) epoch always fetches.
  if (epochId && _epochCache.has(epochId)) return _epochCache.get(epochId);
  const def = state.epochDef;
  if (def && (!epochId || def.epoch_id === epochId)) {
    _epochCache.set(epochId || def.epoch_id, def);
    return def;
  }
  if (!epochId) return null;
  if (_loadingEpoch.has(epochId)) return null;
  _loadingEpoch.add(epochId);
  try {
    const data = await fetchJson('/api/epoch');
    if (data && typeof data === 'object') {
      _epochCache.set(data.epoch_id || epochId, data);
      if (epochId !== (data.epoch_id || epochId)) _epochCache.set(epochId, data);
    }
  } catch {
    _epochCache.set(epochId, { epoch_id: epochId, experiments: [], __broken: true });
  } finally {
    _loadingEpoch.delete(epochId);
    _repaint();
  }
  return _epochCache.get(epochId);
}

async function ensureContractDiff(epochId) {
  if (!epochId || _contractDiffCache.has(epochId)) return _contractDiffCache.get(epochId);
  if (_loadingDiff.has(epochId)) return null;
  _loadingDiff.add(epochId);
  try {
    const data = await fetchJson('/api/contract-diff/' + encodeURIComponent(epochId));
    _contractDiffCache.set(epochId, (data && typeof data === 'object') ? data : null);
  } catch {
    _contractDiffCache.set(epochId, {
      epoch_id: epochId, predecessor_epoch_id: null, components: [], any_changed: false,
    });
  } finally {
    _loadingDiff.delete(epochId);
    _repaint();
  }
  return _contractDiffCache.get(epochId);
}

async function ensurePerJudgeTrend(epochId) {
  if (!epochId || _perJudgeTrendCache.has(epochId)) return _perJudgeTrendCache.get(epochId);
  if (_loadingTrend.has(epochId)) return null;
  _loadingTrend.add(epochId);
  try {
    const data = await fetchJson('/api/epoch/' + encodeURIComponent(epochId) + '/per-judge-trend');
    _perJudgeTrendCache.set(epochId, (data && typeof data === 'object')
      ? data : { epoch_id: epochId, generations: [], judges: [] });
  } catch {
    _perJudgeTrendCache.set(epochId, { epoch_id: epochId, generations: [], judges: [] });
  } finally {
    _loadingTrend.delete(epochId);
    _repaint();
  }
  return _perJudgeTrendCache.get(epochId);
}

async function ensurePerEntryTrend(epochId, generationIds) {
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
          if (typeof e.drift_loss === 'number' && isFinite(e.drift_loss)) {
            entryMap.get(e.entry_id)[gid] = e.drift_loss;
          }
        }
      } catch { /* one gen failing must not sink the whole landscape */ }
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
    _repaint();
  }
  return _perEntryTrendCache.get(epochId);
}

async function ensureAnalysis(epochId) {
  if (!epochId || _analysisCache.has(epochId)) return _analysisCache.get(epochId);
  if (_loadingAnalysis.has(epochId)) return null;
  _loadingAnalysis.add(epochId);
  try {
    const data = await fetchJson('/api/epoch/' + encodeURIComponent(epochId) + '/analysis');
    _analysisCache.set(epochId, (data && typeof data === 'object') ? data : {
      epoch_id: epochId, analysis_html_inline: '', analysis_html_available: false,
    });
  } catch {
    _analysisCache.set(epochId, {
      epoch_id: epochId, analysis_html_inline: '', analysis_html_available: false,
    });
  } finally {
    _loadingAnalysis.delete(epochId);
    _repaint();
  }
  return _analysisCache.get(epochId);
}

// =====================================================================
// Outcome readers — tolerant of the experiment.json shape variants.
// =====================================================================
function _decisionOf(exp) {
  const o = exp && exp.outcome;
  if (!o || typeof o !== 'object') return null;
  const raw = String(o.tournament_decision || o.decision || o.verdict || '').toLowerCase();
  if (raw.includes('promot') || raw === 'accepted') return 'promoted';
  if (raw.includes('reject')) return 'rejected';
  if (raw.includes('defer')) return 'deferred';
  return raw || null;
}
function _isBaselineSeed(exp) {
  if (!exp || typeof exp !== 'object') return false;
  const parent = exp.parent_generation_id;
  if (typeof parent === 'string' && parent !== '') return false;
  return exp.outcome == null;
}
function _num(v) { return (typeof v === 'number' && isFinite(v)) ? v : null; }
function _outcomeNum(exp, key) {
  const o = exp && exp.outcome;
  return (o && typeof o === 'object') ? _num(o[key]) : null;
}
function _hypothesisLine(exp) {
  const h = exp && exp.hypothesis;
  if (h && typeof h === 'object') {
    const idea = (typeof h.core_idea === 'string' && h.core_idea.trim())
      ? h.core_idea.trim()
      : (typeof h.summary === 'string' ? h.summary.trim() : '');
    if (idea) return idea;
  }
  if (typeof exp.hypothesis === 'string' && exp.hypothesis.trim()) return exp.hypothesis.trim();
  return '';
}

// The fired gate rule for the ledger. A structured `fired_gate_rule` /
// `gate_rule` wins; otherwise we infer a terse rule label from a rejected
// outcome's rejection_reason. Promoted / open / baseline rows show '—'.
const _RULE_PATTERNS = [
  [/scalar|margin/i, 'scalar_margin'],
  [/pass[\s_-]*rate|monotonic.*pass|pass.*monoton/i, 'pass_rate_monotonicity'],
  [/namespace/i, 'namespace_monotonicity'],
  [/budget|wall[\s_-]*clock|timeout/i, 'budget'],
];
function _firedRule(exp) {
  const o = exp && exp.outcome;
  if (o && typeof o === 'object') {
    for (const k of ['fired_gate_rule', 'gate_rule', 'fired_rule']) {
      if (typeof o[k] === 'string' && o[k].trim()) return o[k].trim();
    }
    const reason = typeof o.rejection_reason === 'string' ? o.rejection_reason : '';
    if (_decisionOf(exp) === 'rejected' && reason) {
      for (const [re, name] of _RULE_PATTERNS) if (re.test(reason)) return name;
      return 'rejected';
    }
  }
  return null;
}

// =====================================================================
// Section: goal + frozen contract.
// =====================================================================
function _section(title, sub, bodyNode) {
  const head = el('div', { class: 'v2-epoch-sec-head' }, [
    el('h2', { class: 'v2-epoch-sec-title' }, [title]),
    sub ? el('p', { class: 'v2-epoch-sec-sub' }, [sub]) : null,
  ].filter(Boolean));
  return el('section', { class: 'v2-epoch-sec' }, [head, bodyNode]);
}

function _renderGoalContract(epochId, def) {
  const goal = (def && typeof def.goal === 'string') ? def.goal.trim() : '';
  const closed = !!(def && def.closed);
  const goalNode = goal
    ? el('p', { class: 'v2-epoch-goal' }, [goal])
    : el('p', { class: 'v2-epoch-goal v2-epoch-goal-empty' }, [
        '(no goal recorded) — set it via ',
        el('code', { class: 'v2-mono' }, ['zicato epoch set-goal --epoch <id> --goal "..."']),
      ]);

  // The frozen contract: what changed to roll this epoch.
  const diff = epochId ? _contractDiffCache.get(epochId) : undefined;
  let contractNode;
  if (diff === undefined) {
    contractNode = stateBlock('running', { label: 'Reading contract diff' });
  } else if (diff == null) {
    contractNode = stateBlock('broken', { reason: 'contract diff unavailable' });
  } else if (!diff.predecessor_epoch_id) {
    contractNode = el('p', { class: 'v2-epoch-contract-line' }, [
      el('span', { class: 'v2-epoch-contract-tag' }, ['first epoch']),
      ' no predecessor to diff — the contract is the seed.',
    ]);
  } else {
    const comps = Array.isArray(diff.components) ? diff.components : [];
    const changed = comps.filter((c) => c && c.changed);
    const unchanged = comps.filter((c) => c && !c.changed);
    const kids = [];
    if (changed.length === 0) {
      kids.push(el('p', { class: 'v2-epoch-contract-line' }, [
        'Unchanged — the frozen contract carries over from ',
        el('span', { class: 'v2-mono' }, [String(diff.predecessor_epoch_id)]), '.',
      ]));
    } else {
      kids.push(el('p', { class: 'v2-epoch-contract-line' }, [
        'Rolled by ', el('strong', null, [String(changed.length)]),
        changed.length === 1 ? ' change vs ' : ' changes vs ',
        el('span', { class: 'v2-mono' }, [String(diff.predecessor_epoch_id)]), ':',
      ]));
      const chips = el('div', { class: 'v2-epoch-contract-chips' });
      for (const c of changed) {
        chips.appendChild(el('span', { class: 'v2-epoch-contract-chip v2-epoch-contract-chip-changed' }, [
          el('span', { class: 'v2-mono' }, [String(c.name)]),
          el('span', { class: 'v2-epoch-contract-hash v2-mono' }, [
            (c.previous_hash ? String(c.previous_hash).slice(0, 7) : '—') + ' → '
            + (c.current_hash ? String(c.current_hash).slice(0, 7) : '—'),
          ]),
        ]));
      }
      kids.push(chips);
    }
    if (unchanged.length > 0) {
      kids.push(el('p', { class: 'v2-epoch-contract-carry' }, [
        el('span', { class: 'v2-epoch-contract-carry-label' }, ['frozen: ']),
        unchanged.map((c) => String(c.name)).join(' · '),
      ]));
    }
    contractNode = el('div', { class: 'v2-epoch-contract' }, kids);
  }

  const body = el('div', { class: 'v2-epoch-intent' }, [
    el('div', { class: 'v2-epoch-intent-meta' }, [
      el('span', {
        class: 'v2-epoch-state v2-epoch-state-' + (closed ? 'closed' : 'open'),
      }, [closed ? 'closed' : 'open']),
      el('span', { class: 'v2-epoch-id v2-mono' }, [String(epochId || '—')]),
    ]),
    goalNode,
    contractNode,
  ]);
  return _section('Goal & frozen contract',
    'What this epoch is trying to learn, and what changed to roll it.', body);
}

// =====================================================================
// Section: the experiment ledger — the dense answer to "what have we
// learned". One row per generation, deltas vs parent, every row a door.
// =====================================================================
function _ledgerRows(experiments) {
  // Newest first so the latest reasoning reads at the top; the table is
  // also user-sortable per column.
  return experiments.slice().reverse().map((exp) => {
    const decision = _decisionOf(exp);
    const verdict = (_isBaselineSeed(exp) || decision === 'promoted') ? 'promoted'
      : decision === 'rejected' ? 'rejected'
      : decision === 'deferred' ? 'deferred' : 'open';
    return {
      gen: exp.generation_id ? String(exp.generation_id) : '?',
      parent: exp.parent_generation_id ? String(exp.parent_generation_id) : null,
      verdict,
      hypothesis: _hypothesisLine(exp),
      dScalar: _outcomeNum(exp, 'scalar_score_delta'),
      dDrift: _outcomeNum(exp, 'drift_loss_delta'),
      dPass: _outcomeNum(exp, 'pass_rate_delta'),
      firedRule: _firedRule(exp),
    };
  });
}

function _renderLedger(epochId, experiments) {
  let body;
  if (experiments.length === 0) {
    body = stateBlock('empty', {
      label: 'No experiments yet',
      detail: 'Generations appear here as the proposer forms and runs each hypothesis.',
    });
  } else {
    const rows = _ledgerRows(experiments);
    body = dataTable({
      ariaLabel: 'experiment ledger — one row per generation',
      sort: { key: 'gen', dir: 'desc' },
      rowKey: (r) => r.gen,
      rows,
      columns: [
        { key: 'gen', header: 'gen', mono: true, width: '5.5em',
          render: (r) => el('span', { class: 'v2-mono v2-epoch-led-gen' }, [r.gen]) },
        { key: 'verdict', header: '', sortable: true, width: '2.2em',
          sortValue: (r) => r.verdict,
          render: (r) => verdictGlyph(r.verdict === 'open' ? 'open' : r.verdict,
            { withLabel: false }) },
        { key: 'hypothesis', header: 'hypothesis', align: 'left',
          render: (r) => r.hypothesis
            ? el('span', { class: 'v2-epoch-led-hyp', title: r.hypothesis }, [r.hypothesis])
            : el('span', { class: 'v2-epoch-led-hyp v2-epoch-led-hyp-empty' }, ['(no hypothesis recorded)']) },
        { key: 'dScalar', header: 'Δscalar', semantic: 'delta', improveWhenNegative: true,
          digits: 3, width: '7em' },
        { key: 'dDrift', header: 'Δdrift', semantic: 'delta', improveWhenNegative: true,
          digits: 3, width: '7em' },
        // Pass-rate UP is the improvement, so the delta direction inverts.
        { key: 'dPass', header: 'Δpass', semantic: 'delta', improveWhenNegative: false,
          digits: 2, width: '6.5em' },
        { key: 'firedRule', header: 'fired rule', mono: true, align: 'left', width: '11em',
          sortValue: (r) => r.firedRule || '',
          render: (r) => r.firedRule
            ? el('span', { class: 'v2-mono v2-epoch-led-rule' }, [r.firedRule])
            : el('span', { class: 'v2-epoch-led-rule v2-epoch-led-rule-none' }, ['—']) },
      ],
      onRowClick: (r) => { if (r.gen && r.gen !== '?') v2Router.go('experiment', r.gen); },
    });
  }
  return _section('Experiment ledger',
    'One row per generation — verdict, hypothesis, and every delta vs the parent. '
    + 'Click a row to drill into the experiment.', body);
}

// =====================================================================
// Section: the drift/loss landscape — entry × gen / judge × gen heatmap.
// =====================================================================
function _entriesHeatmap(epochId, generationIds) {
  const data = epochId ? _perEntryTrendCache.get(epochId) : undefined;
  if (generationIds.length === 0) {
    return stateBlock('empty', { label: 'No generations yet' });
  }
  if (data === undefined) return stateBlock('running', { label: 'Reading per-entry loss' });
  const entries = Array.isArray(data.entries) ? data.entries : [];
  if (entries.length === 0) {
    return stateBlock('empty', {
      label: 'No per-entry loss recorded yet',
      detail: 'Loss lands here as each board entry runs.',
    });
  }
  return renderHeatmapTable({
    rows: entries.map((e) => e.entry_id),
    cols: data.generations,
    valueAt: (row, col) => {
      const found = entries.find((e) => e.entry_id === row);
      const v = found && found.by_gen && found.by_gen[col];
      return (typeof v === 'number' && isFinite(v)) ? v : null;
    },
    scale: 'sequential',
    rowLabel: 'entry',
    rowHref: (eid) => v2Href('run', eid),
    colHref: (gid) => v2Href('experiment', gid),
    ariaLabel: 'per-entry × generation drift-loss heatmap',
  });
}

function _judgesHeatmap(epochId) {
  const data = epochId ? _perJudgeTrendCache.get(epochId) : undefined;
  if (data === undefined) return stateBlock('running', { label: 'Reading per-judge loss' });
  const generations = Array.isArray(data.generations) ? data.generations : [];
  const judges = Array.isArray(data.judges) ? data.judges : [];
  if (generations.length === 0 || judges.length === 0) {
    return stateBlock('empty', {
      label: 'No per-judge loss recorded yet',
      detail: data.note ? String(data.note)
        : 'Per-judge weighted loss lands here once judges score this epoch.',
    });
  }
  return renderHeatmapTable({
    rows: judges.map((j) => j.judge_name || '—'),
    cols: generations,
    valueAt: (row, col) => {
      const found = judges.find((j) => (j.judge_name || '—') === row);
      const v = found && found.by_generation && found.by_generation[col];
      return (typeof v === 'number' && isFinite(v)) ? v : null;
    },
    scale: 'sequential',
    rowLabel: 'judge',
    colHref: (gid) => v2Href('experiment', gid),
    ariaLabel: 'per-judge × generation weighted-loss heatmap',
  });
}

function _renderLandscape(epochId, generationIds) {
  const facets = [
    ['entries', 'entries', 'drift loss per board entry'],
    ['judges', 'judges', 'weighted loss per judge'],
  ];
  const toggle = el('div', { class: 'v2-epoch-facets', role: 'tablist',
    'aria-label': 'landscape facet' });
  for (const [key, label, sub] of facets) {
    const active = _landscapeFacet === key;
    const btn = el('button', {
      type: 'button',
      class: 'v2-epoch-facet' + (active ? ' v2-epoch-facet-active' : ''),
      role: 'tab', 'aria-selected': active ? 'true' : 'false',
      'data-facet': key,
    }, [
      el('span', { class: 'v2-epoch-facet-label' }, [label]),
      el('span', { class: 'v2-epoch-facet-sub' }, [sub]),
    ]);
    btn.addEventListener('click', () => {
      if (_landscapeFacet === key) return;
      _landscapeFacet = key;
      _repaint();
    });
    toggle.appendChild(btn);
  }

  const panel = el('div', {
    class: 'v2-epoch-landscape-panel', role: 'tabpanel', 'data-facet': _landscapeFacet,
  }, [
    _landscapeFacet === 'judges'
      ? _judgesHeatmap(epochId)
      : _entriesHeatmap(epochId, generationIds),
  ]);

  return _section('Drift / loss landscape',
    'The comparative substrate — entry × generation and judge × generation. '
    + 'Lower is better; click a column to drill into that generation.',
    el('div', { class: 'v2-epoch-landscape' }, [toggle, panel]));
}

// =====================================================================
// Section: the ACM report — inline fragment + standalone links.
// =====================================================================
function _setHtml(node, html) {
  // The analysis fragment is server-rendered, self-contained HTML the
  // report owns; we inject it verbatim (§6 — retained as-is). The render
  // spine forbids innerHTML for deltas, but this slot is painted once per
  // epoch (cache-keyed on epoch_id) so it never participates in no-flash.
  // eslint-disable-next-line no-unsanitized/property
  node.innerHTML = html;
}

function _renderReportSection(epochId) {
  const data = epochId ? _analysisCache.get(epochId) : undefined;
  let body;
  let actions = null;
  const rawHref = epochId ? '/api/epoch/' + encodeURIComponent(epochId) + '/analysis.html' : null;

  if (!epochId) {
    body = stateBlock('empty', { label: 'No epoch selected' });
  } else if (data === undefined) {
    body = stateBlock('running', { label: 'Loading analysis report' });
  } else {
    const inline = (typeof data.analysis_html_inline === 'string') ? data.analysis_html_inline : '';
    const available = !!data.analysis_html_available;
    if (inline.trim()) {
      const host = el('div', { class: 'v2-epoch-report-host' });
      _setHtml(host, inline);
      body = host;
    } else if (available) {
      body = stateBlock('empty', {
        label: 'Report rendered as a standalone document',
        detail: 'Open the full report to read it.',
      });
    } else {
      // Honest not-yet with the actionable hint (§4, §8) — never a blank.
      body = stateBlock('not_yet', {
        label: 'Analysis report not built yet',
        detail: 'It is generated by the analyzer after the epoch is analyzed — run '
          + 'zicato epoch analyze --epoch ' + epochId + ' to build it.',
      });
    }
    if (available || inline.trim()) {
      actions = el('div', { class: 'v2-epoch-report-actions' }, [
        el('a', {
          class: 'v2-epoch-report-link', href: v2Href('report', epochId),
        }, ['Full report ↗']),
        rawHref ? el('a', {
          class: 'v2-epoch-report-link v2-epoch-report-link-raw',
          href: rawHref, target: '_blank', rel: 'noopener',
        }, ['Open standalone in new tab ↗']) : null,
      ].filter(Boolean));
    }
  }

  const head = el('div', { class: 'v2-epoch-sec-head v2-epoch-report-head' }, [
    el('div', null, [
      el('h2', { class: 'v2-epoch-sec-title' }, ['Analysis report']),
      el('p', { class: 'v2-epoch-sec-sub' }, [
        "The epoch's publication — hypothesis-vs-outcome figures, drift heatmaps, "
        + 'per-judge attribution. Embedded inline; the standalone document is retained verbatim.',
      ]),
    ]),
    actions,
  ].filter(Boolean));
  return el('section', { class: 'v2-epoch-sec v2-epoch-report-sec' }, [head, body]);
}

// =====================================================================
// The view.
// =====================================================================
export function renderEpoch(host, route) {
  if (!host) return;
  const epochId = (route && route.params && route.params.epochId)
    || (route && route.epochId)
    || (state.epochDef && state.epochDef.epoch_id)
    || (state.heartbeat && state.heartbeat.epoch_id)
    || (state.epoch && state.epoch.id !== '—' ? state.epoch.id : null)
    || null;

  // Kick the async fetches (idempotent — each guards its own cache).
  ensureEpoch(epochId);
  ensureContractDiff(epochId);
  ensurePerJudgeTrend(epochId);
  ensureAnalysis(epochId);

  const def = (epochId && _epochCache.get(epochId)) || state.epochDef || null;
  const experiments = (def && Array.isArray(def.experiments)) ? def.experiments : [];
  const gids = experiments.map((e) => e && e.generation_id).filter(Boolean).map(String);
  if (gids.length > 0) ensurePerEntryTrend(epochId, gids);

  // Coarse content key: rebuild the whole view only when an input that
  // changes its shape moves. Within a render the sub-sections are rebuilt
  // wholesale (this is a post-hoc view, not a streaming one) but the
  // wrapper node keeps identity so the surrounding shell does not flash.
  const ck = JSON.stringify({
    epochId,
    facet: _landscapeFacet,
    nExp: experiments.length,
    diff: epochId ? !!_contractDiffCache.get(epochId) : false,
    diffSeen: epochId ? _contractDiffCache.has(epochId) : false,
    trend: epochId ? (_perJudgeTrendCache.get(epochId) || {}).judges?.length ?? -1 : -1,
    entries: epochId ? (_perEntryTrendCache.get(epochId) || {}).entries?.length ?? -1 : -1,
    analysisSeen: epochId ? _analysisCache.has(epochId) : false,
    // include a digest of ledger deltas so a settled outcome repaints
    ledger: experiments.map((e) => [
      e.generation_id, _decisionOf(e),
      _outcomeNum(e, 'scalar_score_delta'),
      _outcomeNum(e, 'drift_loss_delta'),
      _outcomeNum(e, 'pass_rate_delta'),
    ]),
  });

  swapIfChanged(host, ck, () => {
    const wrap = el('div', { class: 'v2-epoch' });
    wrap.appendChild(el('h1', { class: 'v2-view-title v2-epoch-title' }, [
      'Epoch ', el('span', { class: 'v2-mono' }, [String(epochId || '—')]),
    ]));
    if (def == null && epochId == null) {
      wrap.appendChild(stateBlock('empty', {
        label: 'No epoch yet',
        detail: 'Start an evolve run, or open an epoch from the overview.',
      }));
      return wrap;
    }
    if (def == null) {
      wrap.appendChild(stateBlock('running', { label: 'Loading epoch' }));
      return wrap;
    }
    if (def.__broken) {
      wrap.appendChild(stateBlock('broken', { reason: 'epoch could not be read' }));
      return wrap;
    }
    wrap.appendChild(_renderGoalContract(epochId, def));
    wrap.appendChild(_renderLedger(epochId, experiments));
    wrap.appendChild(_renderLandscape(epochId, gids));
    wrap.appendChild(_renderReportSection(epochId));
    return wrap;
  });
}

// Self-register with the shell. Unused import guard: deltaCell is part of
// the dataTable contract surface; the ledger uses the `semantic: 'delta'`
// shorthand rather than calling it directly.
void deltaCell;
void clearChildren;
registerView('epoch', renderEpoch);

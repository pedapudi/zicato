// views/phase0_generation.js — L2 (generation-level) view.
//
// Holistic inverted-pyramid layout (Task #200):
//
//   1. HERO    — Decision pill + 3 delta tiles + rejection/promotion line.
//                This slot owns the "what happened" answer; no other card
//                duplicates the verdict.
//   2. HYP/ALIGN — Hypothesis on the left; Alignment-vs-Outcome on the
//                right (predicted vs actual per dimension, plus risks).
//   3. PATCHES — Single-patch rows render inline; 2+ patches keep the
//                compact table.
//   4. ENTRIES — Per-entry table with a "vs <champion>" delta column;
//                rows are clickable links into L4.
//   5. JUDGES  — Single-judge rows render inline; 2+ judges keep the
//                compact table.
//
// The old bottom "Verdict" card and the old inline 2-column Outcome
// card are GONE — the hero subsumes both.

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';
import { renderCard } from '../components/card.js';
import { renderPill, renderInlinePill } from '../components/pill.js';
import { renderMetricTile } from '../components/tile.js';
import { renderLoadingState, renderEmptyState } from '../components/loading.js';
import { phase0Href } from './phase0_router.js';

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

function _fmtSigned(v, digits) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  const d = digits == null ? 3 : digits;
  return (v > 0 ? '+' : '') + v.toFixed(d);
}

function _isStr(v) { return typeof v === 'string' && v.trim() !== ''; }
function _isNum(v) { return typeof v === 'number' && isFinite(v); }

// Format an ISO timestamp like "2026-05-20T01:25:49+00:00" as a human
// "2026-05-20 01:25" without the noise. Falls back to the raw string if
// parsing fails so the user still sees *something*.
function _fmtHumanTimestamp(iso) {
  if (!_isStr(iso)) return null;
  const d = new Date(iso);
  if (!isFinite(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    + ` ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function findExperiment(generationId) {
  const def = state.epochDef;
  if (!def || !Array.isArray(def.experiments)) return null;
  for (const exp of def.experiments) {
    if (exp && exp.generation_id === generationId) return exp;
  }
  return null;
}

function _normaliseDecision(outcome) {
  if (!outcome || typeof outcome !== 'object') return null;
  const raw = outcome.tournament_decision || outcome.decision || '';
  const d = String(raw).toLowerCase();
  if (d.includes('promot')) return 'promoted';
  if (d.includes('reject')) return 'rejected';
  if (d.includes('defer')) return 'deferred';
  return raw ? d : null;
}

// Parse the prose "rejection_reason" field, which on disk reads like:
//   "challenger regressed: loss rose by 10.122619
//    (champion 47.580429 -> challenger 57.703048);
//    a promotion needs the loss to drop by at least 0.010000"
// We surface a short headline ("challenger regressed — loss 47.58 → 57.70")
// plus the promotion-margin caveat as a fine-print second line.
function _parseRejectionSummary(reason) {
  if (!_isStr(reason)) return null;
  const m = reason.match(
    /champion\s+([0-9.+-]+)\s*->\s*challenger\s+([0-9.+-]+)/i,
  );
  if (!m) return { headline: reason, caveat: null };
  const champ = parseFloat(m[1]);
  const chal = parseFloat(m[2]);
  const headline = `challenger regressed — loss ${_isNum(champ) ? champ.toFixed(2) : m[1]}`
    + ` → ${_isNum(chal) ? chal.toFixed(2) : m[2]}`;
  const marginMatch = reason.match(/drop by at least\s+([0-9.+-]+)/i);
  const caveat = marginMatch
    ? `(promotion needs Δloss ≤ −${parseFloat(marginMatch[1]).toFixed(2)})`
    : null;
  return { headline, caveat };
}

// Build the promotion-summary line from the outcome alone (no
// rejection_reason; promoted gens leave that field blank). We synthesise
// loss before / after from scalar_score_delta when scalar_score is also
// present — otherwise just report the delta.
function _promotionSummary(outcome) {
  if (!outcome || typeof outcome !== 'object') return null;
  const d = outcome.scalar_score_delta;
  if (!_isNum(d)) return null;
  const after = _isNum(outcome.scalar_score) ? outcome.scalar_score : null;
  // The "loss" is conceptually the scalar; before = after - delta.
  if (after != null) {
    const before = after - d;
    return `promoted — loss dropped from ${before.toFixed(2)}`
      + ` to ${after.toFixed(2)} (better by ${Math.abs(d).toFixed(2)})`;
  }
  return `promoted — loss dropped by ${Math.abs(d).toFixed(2)}`;
}

// ---------------------------------------------------------------------------
// HERO — pill, deltas, summary line.
// ---------------------------------------------------------------------------

function _renderHero(exp, epochId, generationId) {
  // The hero slot reuses the existing phase0-gen-compare DOM id so we
  // do not need an index.html change. The earlier "Verdict" card lived
  // here too — it is replaced wholesale.
  const node = $('phase0-gen-compare');
  if (!node) return;
  clearChildren(node);

  let body;
  if (state.epochDef == null) {
    body = renderLoadingState({ label: 'Loading verdict' });
    node.appendChild(renderCard({ title: 'Generation', body }));
    return;
  }
  if (!exp) {
    body = renderEmptyState('No experiment recorded.');
    node.appendChild(renderCard({ title: 'Generation', body }));
    return;
  }

  const out = exp.outcome || {};
  const decision = _normaliseDecision(out) || 'pending';
  const pillVariant = decision === 'promoted' ? 'promoted'
    : decision === 'rejected' ? 'rejected'
    : decision === 'deferred' ? 'deferred' : 'pending';

  // --- header row: generation id + decision pill + lineage line -----
  const titleRow = el('div', { class: 'gen-hero-title-row' }, [
    el('h3', { class: 'gen-hero-title' }, [
      'Generation ',
      String(generationId || '—'),
    ]),
    renderPill(decision.toUpperCase(), pillVariant),
  ]);

  const parentId = exp.parent_generation_id || null;
  const lineageBits = [];
  if (parentId) {
    lineageBits.push('Challenger to ');
    lineageBits.push(el('a', {
      class: 'gen-hero-parent',
      href: phase0Href('generation', { epochId, generationId: parentId }),
    }, [String(parentId)]));
  } else {
    lineageBits.push('Seed generation');
  }
  const stamp = _fmtHumanTimestamp(out.ran_at);
  if (stamp) {
    lineageBits.push(' · evaluated ');
    lineageBits.push(el('span', { class: 'mono' }, [stamp]));
  }
  const lineage = el('p', { class: 'gen-hero-lineage' }, lineageBits);

  // --- delta tile strip ---------------------------------------------
  // Sentiment mapping:
  //   Δscalar / Δdrift: lower is better → positive = bad (red ↑), negative = good (green ↓)
  //   Δpass:            higher is better → positive = good (green ↑), negative = bad (red ↓)
  // Below: we pass a pre-formatted directional delta string ("↑ worse" /
  // "↓ better") so the tile reads at a glance. The tile component
  // colours by sentiment; the arrow direction matches the raw sign.
  const sentimentLowerBetter = (v) => (!_isNum(v) || v === 0) ? 'flat'
    : (v < 0 ? 'good' : 'bad');
  const sentimentHigherBetter = (v) => (!_isNum(v) || v === 0) ? 'flat'
    : (v > 0 ? 'good' : 'bad');

  // Direction (arrow) is bound to the metric's sign; the delta text is
  // the operator-readable verdict word so the tile reads as e.g.
  // "↑ worse" / "↓ better" with no redundant +/- prefix duplicating the
  // headline number above.
  const direction = (v) => {
    if (!_isNum(v) || v === 0) return 'flat';
    return v > 0 ? 'up' : 'down';
  };
  const verdictText = (sent) => sent === 'good' ? 'better'
    : (sent === 'bad' ? 'worse' : 'flat');

  const tileStrip = el('div', { class: 'tile-strip gen-hero-tiles' }, [
    renderMetricTile({
      label: 'Δ scalar', size: 'lg',
      value: _fmtSigned(out.scalar_score_delta),
      direction: direction(out.scalar_score_delta),
      delta: verdictText(sentimentLowerBetter(out.scalar_score_delta)),
      sentiment: sentimentLowerBetter(out.scalar_score_delta),
    }),
    renderMetricTile({
      label: 'Δ drift', size: 'lg',
      value: _fmtSigned(out.drift_loss_delta),
      direction: direction(out.drift_loss_delta),
      delta: verdictText(sentimentLowerBetter(out.drift_loss_delta)),
      sentiment: sentimentLowerBetter(out.drift_loss_delta),
    }),
    renderMetricTile({
      label: 'Δ pass', size: 'lg',
      value: _fmtSigned(out.pass_rate_delta),
      direction: direction(out.pass_rate_delta),
      delta: verdictText(sentimentHigherBetter(out.pass_rate_delta)),
      sentiment: sentimentHigherBetter(out.pass_rate_delta),
    }),
  ]);

  // --- summary line --------------------------------------------------
  let summaryLine = null;
  if (decision === 'rejected') {
    const parsed = _parseRejectionSummary(out.rejection_reason);
    if (parsed) {
      const lines = [el('p', { class: 'gen-hero-summary' }, [
        el('strong', null, ['Rejection: ']),
        parsed.headline,
      ])];
      if (parsed.caveat) {
        lines.push(el('p', { class: 'gen-hero-caveat' }, [parsed.caveat]));
      }
      summaryLine = el('div', null, lines);
    }
  } else if (decision === 'promoted') {
    const text = _promotionSummary(out);
    if (text) {
      summaryLine = el('p', { class: 'gen-hero-summary' }, [
        el('strong', null, ['Result: ']), text,
      ]);
    }
  } else if (decision === 'deferred') {
    summaryLine = el('p', { class: 'gen-hero-summary' }, [
      el('strong', null, ['Result: ']),
      'No decisive winner — kept for analysis.',
    ]);
  }

  const heroBody = el('div', { class: 'gen-hero-body' }, [
    titleRow, lineage, tileStrip, summaryLine,
  ]);
  // Variant accent matches the decision so the page reads at a glance.
  const accent = decision === 'promoted' ? 'promoted'
    : decision === 'rejected' ? 'rejected'
    : decision === 'deferred' ? 'warning' : 'default';
  node.appendChild(renderCard({
    body: heroBody, accent, variant: 'flush', class: 'gen-hero-card',
  }));
}

// ---------------------------------------------------------------------------
// HYPOTHESIS — left column (pure prose) + right column (alignment).
// ---------------------------------------------------------------------------

// Format an "expected_drift_movements" entry as a one-line predicted
// summary: "off_topic decrease (medium)".
function _fmtDriftPrediction(m) {
  if (!m || !m.kind) return '';
  const dir = m.direction || '?';
  const mag = m.magnitude ? ` (${m.magnitude})` : '';
  return `${m.kind} ${dir}${mag}`;
}

// Render the LEFT column — the operator's pre-run hypothesis prose. We
// intentionally drop the "Modulating." line: the same info surfaces in
// the Patches card below (mutation_id == modulating site).
function _renderHypothesisColumn(hyp) {
  const wrap = el('div', { class: 'gen-hyp-col' });
  wrap.appendChild(el('h4', { class: 'gen-hyp-block-h' }, ['Hypothesis']));
  let any = false;
  if (_isStr(hyp.core_idea)) {
    wrap.appendChild(el('p', { class: 'gen-hyp-core' }, [hyp.core_idea]));
    any = true;
  }
  if (_isStr(hyp.why)) {
    wrap.appendChild(el('p', { class: 'gen-hyp-line' }, [
      el('strong', null, ['Why. ']), hyp.why,
    ]));
    any = true;
  }
  const predicted = [];
  if (_isStr(hyp.expected_pass_rate_delta)) {
    predicted.push(`pass-rate Δ ${hyp.expected_pass_rate_delta}`);
  }
  if (Array.isArray(hyp.expected_drift_movements)) {
    for (const m of hyp.expected_drift_movements) {
      const line = _fmtDriftPrediction(m);
      if (line) predicted.push(`drift: ${line}`);
    }
  }
  if (predicted.length) {
    const ul = el('ul', { class: 'gen-hyp-predicted' });
    for (const p of predicted) ul.appendChild(el('li', null, [p]));
    wrap.appendChild(el('div', null, [
      el('p', { class: 'gen-hyp-line gen-hyp-lead' }, [
        el('strong', null, ['Predicted']),
      ]),
      ul,
    ]));
    any = true;
  }
  if (!any) {
    wrap.appendChild(el('p', { class: 'empty' },
      ['No structured rationale recorded.']));
  }
  return wrap;
}

// Render the RIGHT column — Alignment vs Outcome. For each predicted
// dimension, render the predicted band/direction and the actual value,
// with an aligned (✓) / missed (✗) glyph.
function _renderAlignmentColumn(hyp, outcome) {
  const wrap = el('div', { class: 'gen-align-col' });
  wrap.appendChild(el('h4', { class: 'gen-hyp-block-h' },
    ['Alignment vs Outcome']));
  const out = outcome && typeof outcome === 'object' ? outcome : null;
  const rows = [];

  // --- pass-rate -----------------------------------------------------
  if (_isStr(hyp.expected_pass_rate_delta) && out && _isNum(out.pass_rate_delta)) {
    const actual = out.pass_rate_delta;
    // Parse the band — "+0.05 to +0.15", "+0.10", "-0.05 to +0.05" all work.
    const nums = (hyp.expected_pass_rate_delta.match(/-?\d+(\.\d+)?/g) || [])
      .map(parseFloat);
    let aligned = null;
    if (nums.length >= 2) {
      const lo = Math.min(nums[0], nums[1]);
      const hi = Math.max(nums[0], nums[1]);
      aligned = actual >= lo && actual <= hi;
    } else if (nums.length === 1) {
      // Single value: same sign counts as aligned.
      aligned = (nums[0] > 0 && actual > 0)
        || (nums[0] < 0 && actual < 0) || (nums[0] === 0 && actual === 0);
    }
    rows.push(_alignRow({
      label: 'Pass-rate',
      predicted: hyp.expected_pass_rate_delta,
      actual: _fmtSigned(actual),
      aligned,
    }));
  } else if (_isStr(hyp.expected_pass_rate_delta)) {
    rows.push(_alignRow({
      label: 'Pass-rate',
      predicted: hyp.expected_pass_rate_delta,
      actual: '—', aligned: null,
    }));
  }

  // --- drift movements per kind --------------------------------------
  // We do not have per-kind actuals in the outcome (only aggregate
  // drift_loss_delta), so the alignment heuristic is: predicted
  // "decrease" should pair with actual drift_loss_delta <= 0; predicted
  // "increase" should pair with delta >= 0. This is a directional check
  // that surfaces the meta-question ("did the proposer's hunch hold?").
  const moves = Array.isArray(hyp.expected_drift_movements)
    ? hyp.expected_drift_movements : [];
  for (const m of moves) {
    if (!m || !m.kind) continue;
    const predDir = String(m.direction || '').toLowerCase();
    const actual = out && _isNum(out.drift_loss_delta) ? out.drift_loss_delta : null;
    let aligned = null;
    if (actual != null) {
      if (predDir === 'decrease') aligned = actual <= 0;
      else if (predDir === 'increase') aligned = actual >= 0;
    }
    rows.push(_alignRow({
      label: `Drift: ${m.kind}`,
      predicted: _fmtDriftPrediction(m),
      actual: actual != null
        ? `drift-loss Δ ${_fmtSigned(actual)}`
        : '—',
      aligned,
    }));
  }

  if (rows.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' },
      ['No predictions to compare against the outcome.']));
  } else {
    const stack = el('div', { class: 'gen-align-rows' });
    for (const r of rows) stack.appendChild(r);
    wrap.appendChild(stack);
  }

  // Risks block at the bottom — operator-stated, flagged at proposal.
  if (_isStr(hyp.risks)) {
    wrap.appendChild(el('div', { class: 'gen-align-risks' }, [
      el('h5', { class: 'gen-hyp-sub-h' }, ['Risks (operator-stated)']),
      el('p', null, [hyp.risks]),
    ]));
  }
  return wrap;
}

function _alignRow(opts) {
  const aligned = opts.aligned;
  const glyph = aligned === true ? '✓'
    : aligned === false ? '✗' : '·';
  const glyphClass = aligned === true ? 'good'
    : aligned === false ? 'bad' : 'flat';
  const note = aligned === true ? 'aligned'
    : aligned === false ? 'direction missed' : 'no comparable actual';
  return el('div', { class: 'gen-align-row' }, [
    el('div', { class: 'gen-align-label' }, [opts.label]),
    el('div', { class: 'gen-align-grid' }, [
      el('span', { class: 'gen-align-kv-key' }, ['predicted']),
      el('span', { class: 'gen-align-kv-val mono' }, [opts.predicted]),
      el('span', { class: 'gen-align-kv-key' }, ['actual']),
      el('span', { class: 'gen-align-kv-val mono' }, [opts.actual]),
    ]),
    el('div', { class: 'gen-align-verdict ' + glyphClass }, [
      el('span', { class: 'gen-align-glyph' }, [glyph]),
      ' ', note,
    ]),
  ]);
}

function _renderHypothesis(exp) {
  const node = $('phase0-gen-hypothesis');
  if (!node) return;
  clearChildren(node);
  let body;
  if (state.epochDef == null) {
    body = renderLoadingState({ label: 'Loading hypothesis' });
  } else if (!exp) {
    body = renderEmptyState('No hypothesis recorded.');
  } else {
    const hyp = (exp.hypothesis && typeof exp.hypothesis === 'object')
      ? exp.hypothesis : {};
    body = el('div', { class: 'gen-hyp-block' }, [
      _renderHypothesisColumn(hyp),
      _renderAlignmentColumn(hyp, exp.outcome),
    ]);
  }
  node.appendChild(renderCard({
    title: 'Hypothesis · Alignment',
    subtitle: 'Proposed before; reconciled against the outcome.',
    body,
  }));
}

// ---------------------------------------------------------------------------
// PATCHES — inline for one, table for many.
// ---------------------------------------------------------------------------

function _opVariant(op) {
  const s = String(op || '').toLowerCase();
  if (s.includes('add')) return 'success';
  if (s.includes('remove') || s.includes('delete')) return 'error';
  if (s.includes('change') || s.includes('modify') || s.includes('replace')) return 'warning';
  return 'neutral';
}

function _renderPatches(exp) {
  const node = $('phase0-gen-patches');
  if (!node) return;
  clearChildren(node);
  let body;
  if (state.epochDef == null) {
    body = renderLoadingState({ label: 'Loading patches' });
    node.appendChild(renderCard({ title: 'Patches', body }));
    return;
  }
  const patches = exp && exp.patches;
  let entries = [];
  if (patches && typeof patches === 'object' && !Array.isArray(patches)) {
    for (const k of Object.keys(patches)) entries.push({ id: k, patch: patches[k] });
  } else if (Array.isArray(patches)) {
    entries = patches.map((p, i) => ({ id: (p && p.mutation_id) || ('p' + i), patch: p }));
  }
  let titleSuffix = '';
  if (entries.length === 0) {
    body = renderEmptyState('No patches recorded.');
  } else if (entries.length === 1) {
    titleSuffix = ' (1)';
    const e = entries[0];
    const p = e.patch || {};
    const op = String(p.op || p.kind || '—');
    body = el('div', { class: 'gen-patch-inline' }, [
      el('div', { class: 'gen-patch-inline-head' }, [
        el('code', { class: 'mono code-pill' }, [e.id]),
        ' ',
        renderInlinePill(op.toUpperCase(), _opVariant(op)),
      ]),
      el('p', { class: 'gen-patch-inline-rationale' }, [
        String(p.rationale || p.message || '(no rationale recorded)'),
      ]),
    ]);
  } else {
    titleSuffix = ' (' + entries.length + ')';
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
      tbody.appendChild(el('tr', null, [
        el('td', { class: 'mono' }, [e.id]),
        el('td', null, [renderInlinePill(op, _opVariant(op))]),
        el('td', null, [String(p.rationale || p.message || '—')]),
      ]));
    }
    tbl.appendChild(tbody);
    body = tbl;
  }
  node.appendChild(renderCard({
    title: 'Patches' + titleSuffix,
    body,
  }));
}

// ---------------------------------------------------------------------------
// PER-ENTRY — table with "vs <champion>" delta and clickable rows.
// ---------------------------------------------------------------------------

function _entryDriftDelta(childDrift, parentDrift) {
  if (!_isNum(childDrift) || !_isNum(parentDrift)) return null;
  return childDrift - parentDrift;
}

function _renderEntries(epochId, generationId, parentEntries) {
  const node = $('phase0-gen-entries');
  if (!node) return;
  clearChildren(node);
  let body;
  let titleSuffix = '';
  if (!epochId || !generationId) {
    body = el('p', { class: 'empty' }, ['No generation selected.']);
  } else {
    const data = _perEntryCache.get(epochId + '/' + generationId);
    if (!data) {
      body = renderLoadingState({ label: 'Loading per-entry breakdown' });
    } else {
      const entries = Array.isArray(data.entries) ? data.entries : [];
      if (entries.length === 0) {
        body = renderEmptyState(
          data.note ? '(no per-entry data: ' + data.note + ')' : 'No per-entry data recorded.',
        );
      } else {
        titleSuffix = ' (' + entries.length + ')';
        // Build a champion-side index keyed by entry_id so the "vs <champ>"
        // delta is a constant-time lookup per row.
        const parentMap = new Map();
        if (parentEntries && Array.isArray(parentEntries.entries)) {
          for (const pe of parentEntries.entries) {
            if (pe && pe.entry_id != null) parentMap.set(pe.entry_id, pe);
          }
        }
        const parentLabel = (state.epochDef && state.epochDef.experiments
          && findExperiment(generationId)
          && findExperiment(generationId).parent_generation_id) || '—';
        const tbl = el('table', { class: 'ds-table gen-entries-table' });
        tbl.appendChild(el('thead', null, [el('tr', null, [
          el('th', null, ['entry']),
          el('th', null, ['drift loss']),
          el('th', null, ['vs ' + String(parentLabel)]),
          el('th', null, ['pass/fail']),
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
          else budgetCell = renderInlinePill('OK', 'success');

          const peer = parentMap.get(e.entry_id);
          const delta = peer ? _entryDriftDelta(e.drift_loss, peer.drift_loss) : null;
          let deltaCell;
          if (delta == null) {
            deltaCell = el('span', { class: 'mono dim' }, ['—']);
          } else {
            const sentiment = delta < 0 ? 'good' : (delta > 0 ? 'bad' : 'flat');
            deltaCell = el('span', {
              class: 'mono gen-entry-delta gen-entry-delta-' + sentiment,
            }, [_fmtSigned(delta)]);
          }

          // The whole row is clickable — anchored on the row's run id so
          // the L4 page resolves the right transcript. We hand the L4
          // path to phase0Href so the URL grammar stays canonical.
          const href = phase0Href('run', {
            epochId,
            generationId,
            entryId: e.entry_id,
          });
          const row = el('tr', { class: 'gen-entries-row' }, [
            el('td', { class: 'mono' }, [
              el('a', { href, class: 'gen-entries-link' }, [String(e.entry_id || '—')]),
            ]),
            el('td', { class: 'mono' }, [_fmtNum(e.drift_loss)]),
            el('td', null, [deltaCell]),
            el('td', null, [pfPill]),
            el('td', null, [budgetCell]),
          ]);
          tbody.appendChild(row);
        }
        tbl.appendChild(tbody);
        const wrap = el('div', null, [
          tbl,
          el('p', { class: 'gen-entries-hint' },
            ['Click an entry id to inspect its run transcript.']),
        ]);
        body = wrap;
      }
    }
  }
  node.appendChild(renderCard({
    title: 'Per-entry' + titleSuffix,
    body,
  }));
}

// ---------------------------------------------------------------------------
// PER-JUDGE — inline for one, table for many.
// ---------------------------------------------------------------------------

function _renderJudges(epochId, generationId) {
  const node = $('phase0-gen-judges');
  if (!node) return;
  clearChildren(node);
  let body;
  let titleSuffix = '';
  if (!epochId || !generationId) {
    body = el('p', { class: 'empty' }, ['No generation selected.']);
  } else {
    const data = _perJudgeCache.get(epochId + '/' + generationId);
    if (!data) {
      body = renderLoadingState({ label: 'Loading per-judge breakdown' });
    } else {
      const judges = Array.isArray(data.judges) ? data.judges : [];
      if (judges.length === 0) {
        const msg = data.note ? '(no per-judge data: ' + data.note + ')'
          : '(no per-judge data recorded for this generation)';
        body = renderEmptyState(msg);
      } else if (judges.length === 1) {
        titleSuffix = ' (1)';
        const j = judges[0];
        body = el('div', { class: 'gen-judge-inline mono' }, [
          el('span', { class: 'gen-judge-name' }, [String(j.judge_name || '—')]),
          ' · weighted ', _fmtNum(j.weighted_loss),
          ' · raw ', _fmtNum(j.raw_loss),
          ' · weight ', _fmtNum(j.weight),
          j.run_count != null ? ' · ' + String(j.run_count) + ' runs' : '',
        ]);
      } else {
        titleSuffix = ' (' + judges.length + ')';
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
    title: 'Per-judge' + titleSuffix,
    body,
  }));
}

// ---------------------------------------------------------------------------
// Public entrypoint.
// ---------------------------------------------------------------------------

export function renderPhase0Generation(params, repaint) {
  const epochId = (params && params.epochId)
    || (state.epochDef && state.epochDef.epoch_id) || null;
  const generationId = (params && params.generationId) || null;
  const exp = findExperiment(generationId);
  ensurePerJudge(epochId, generationId, repaint);
  ensurePerEntry(epochId, generationId, repaint);
  // For the "vs champion" entry column we also need the parent's per-entry
  // payload — fetched into the same cache so the next render paints with
  // a non-null parentEntries lookup.
  const parentId = exp ? exp.parent_generation_id : null;
  if (parentId) ensurePerEntry(epochId, parentId, repaint);
  const parentEntries = parentId
    ? _perEntryCache.get(epochId + '/' + parentId)
    : null;
  _renderHero(exp, epochId, generationId);
  _renderHypothesis(exp);
  _renderPatches(exp);
  _renderEntries(epochId, generationId, parentEntries);
  _renderJudges(epochId, generationId);
}

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
//
// COMPARE MODE (Task #205): every section above grows a sibling on the
// right whenever the compare picker selects another generation in the
// same epoch. The picker lives in the hero slot above the hero strip,
// follows L4's stable-<select> pattern (heartbeat-tick safe), and stays
// per (epoch, focused-gen) — switching gens drops the picker back to
// "off" rather than carrying a stale compare target.

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

// Compare-picker state — per (epoch, focused-gen) so a route change
// drops the picker back to "off" instead of carrying a stale compare
// target. Stable <select> reference mirrors the L4 pattern: an SSE
// heartbeat tick rebuilds the surrounding cards but leaves the picker
// (and any open native dropdown) alone.
const _compareGenByFocused = new Map();
let _comparePicker = null;
let _comparePickerSig = null;
let _comparePickerHandler = null;

// Per-card digest — the SSE heartbeat ticks every second; the picker
// must NOT close. Each card has its own digest covering only the
// inputs it actually reads. A heartbeat tick that touches no card
// input rebuilds nothing.
let _lastHeroDigest = null;
let _lastHypothesisDigest = null;
let _lastPatchesDigest = null;
let _lastEntriesDigest = null;
let _lastJudgesDigest = null;
// Force-render override — picker-change handlers flip this so the next
// render bypasses every gate.
let _forceNextGenRender = false;

export function resetGenerationCaches() {
  _perJudgeCache.clear();
  _perEntryCache.clear();
  _loadingJudges.clear();
  _loadingEntries.clear();
  _compareGenByFocused.clear();
  _comparePicker = null;
  _comparePickerSig = null;
  _comparePickerHandler = null;
  _lastHeroDigest = null;
  _lastHypothesisDigest = null;
  _lastPatchesDigest = null;
  _lastEntriesDigest = null;
  _lastJudgesDigest = null;
  _forceNextGenRender = false;
}

// Reset every per-card digest — used by tests that want to assert a
// no-op render is gated without clearing the data caches.
export function resetGenerationRenderDigest() {
  _lastHeroDigest = null;
  _lastHypothesisDigest = null;
  _lastPatchesDigest = null;
  _lastEntriesDigest = null;
  _lastJudgesDigest = null;
  _forceNextGenRender = false;
}

export function compareGenFor(epochId, focusedGen) {
  return _compareGenByFocused.get(epochId + '/' + focusedGen) || null;
}

export function setCompareGenFor(epochId, focusedGen, generationId) {
  const key = epochId + '/' + focusedGen;
  if (!generationId) _compareGenByFocused.delete(key);
  else _compareGenByFocused.set(key, generationId);
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
    _forceNextGenRender = true;
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
    _forceNextGenRender = true;
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
// COMPARE PICKER — stable <select> reference (mirrors L4 pattern).
// ---------------------------------------------------------------------------

// Build (or update in place) the sibling-generation picker. Lists every
// generation in the focused epoch except the focused one; emitting an
// empty string value = "no compare". Returns the <select> element —
// the SAME element across renders unless its option list changed.
function _buildComparePicker(epochId, focusedGen, selectedCompare, onChange) {
  // Source the option list from state.epochDef.experiments first (the
  // canonical L2 data shape) and fall back to state.lineage.generations
  // if the def has not landed yet. Either way the focused gen is
  // filtered out.
  const def = state.epochDef || {};
  const exps = Array.isArray(def.experiments) ? def.experiments : [];
  const fromDef = exps
    .filter((e) => e && e.generation_id && e.generation_id !== focusedGen)
    .map((e) => e.generation_id);
  const lineage = state.lineage || {};
  const lineageGens = Array.isArray(lineage.generations) ? lineage.generations : [];
  const fromLineage = lineageGens
    .filter((g) => g && g.epoch_id === epochId && g.generation_id !== focusedGen)
    .map((g) => g.generation_id);
  // Merge + dedupe; sort so the option list has a stable order.
  const merged = Array.from(new Set([...fromDef, ...fromLineage]));
  merged.sort();
  const sig = JSON.stringify({ epochId, focusedGen, options: merged });

  if (_comparePicker == null || _comparePickerSig !== sig) {
    const select = el('select', { class: 'mono gen-compare-picker' });
    select.appendChild(el('option', { value: '' }, ['compare to … (off)']));
    for (const gid of merged) {
      select.appendChild(el('option', { value: gid }, [gid]));
    }
    // Bind the change handler ONCE; later renders swap the mutable ref.
    select.addEventListener('change', (ev) => {
      const v = (ev && ev.target && ev.target.value) ? String(ev.target.value) : '';
      const handler = _comparePickerHandler;
      if (typeof handler === 'function') handler(v || null);
    });
    _comparePicker = select;
    _comparePickerSig = sig;
  }

  const wantValue = selectedCompare || '';
  if (_comparePicker.value !== wantValue) {
    const opts = _comparePicker.children;
    for (const opt of opts) {
      const isMatch = (opt.getAttribute && opt.getAttribute('value')) === wantValue;
      if (isMatch) opt.setAttribute('selected', 'selected');
      else if (opt.removeAttribute) opt.removeAttribute('selected');
    }
    _comparePicker.value = wantValue;
  }

  _comparePickerHandler = onChange;
  return _comparePicker;
}

export function resetComparePicker() {
  _comparePicker = null;
  _comparePickerSig = null;
  _comparePickerHandler = null;
}

// ---------------------------------------------------------------------------
// HERO — pill, deltas, summary line.
// ---------------------------------------------------------------------------

// Render one compact hero strip — used both for the single-mode card
// body and for each side of a compare-mode pair.
function _heroStripBody(exp, epochId, generationId, opts) {
  const o = opts || {};
  const out = exp.outcome || {};
  const decision = _normaliseDecision(out) || 'pending';
  const pillVariant = decision === 'promoted' ? 'promoted'
    : decision === 'rejected' ? 'rejected'
    : decision === 'deferred' ? 'deferred' : 'pending';

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

  // Sentiment mapping:
  //   Δscalar / Δdrift: lower is better → positive = bad
  //   Δpass:            higher is better → positive = good
  const sentimentLowerBetter = (v) => (!_isNum(v) || v === 0) ? 'flat'
    : (v < 0 ? 'good' : 'bad');
  const sentimentHigherBetter = (v) => (!_isNum(v) || v === 0) ? 'flat'
    : (v > 0 ? 'good' : 'bad');
  const direction = (v) => {
    if (!_isNum(v) || v === 0) return 'flat';
    return v > 0 ? 'up' : 'down';
  };
  const verdictText = (sent) => sent === 'good' ? 'better'
    : (sent === 'bad' ? 'worse' : 'flat');

  // In compare mode the tiles render at a smaller size so two strips
  // sit side-by-side without a horizontal scrollbar.
  const tileSize = o.compact ? 'md' : 'lg';
  const tileStrip = el('div', { class: 'tile-strip gen-hero-tiles' }, [
    renderMetricTile({
      label: 'Δ scalar', size: tileSize,
      value: _fmtSigned(out.scalar_score_delta),
      direction: direction(out.scalar_score_delta),
      delta: verdictText(sentimentLowerBetter(out.scalar_score_delta)),
      sentiment: sentimentLowerBetter(out.scalar_score_delta),
    }),
    renderMetricTile({
      label: 'Δ drift', size: tileSize,
      value: _fmtSigned(out.drift_loss_delta),
      direction: direction(out.drift_loss_delta),
      delta: verdictText(sentimentLowerBetter(out.drift_loss_delta)),
      sentiment: sentimentLowerBetter(out.drift_loss_delta),
    }),
    renderMetricTile({
      label: 'Δ pass', size: tileSize,
      value: _fmtSigned(out.pass_rate_delta),
      direction: direction(out.pass_rate_delta),
      delta: verdictText(sentimentHigherBetter(out.pass_rate_delta)),
      sentiment: sentimentHigherBetter(out.pass_rate_delta),
    }),
  ]);

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

  return {
    body: el('div', { class: 'gen-hero-body' }, [
      titleRow, lineage, tileStrip, summaryLine,
    ]),
    decision,
  };
}

// Render the small "vs" delta column between the two hero strips. Each
// row reports the directional difference focused-vs-compared so the
// operator can read the trade-off at a glance.
function _heroVsColumn(focusedExp, comparedExp) {
  const fOut = focusedExp.outcome || {};
  const cOut = comparedExp.outcome || {};
  const rows = [];
  const mkRow = (label, focused, compared, lowerBetter) => {
    if (!_isNum(focused) || !_isNum(compared)) return;
    const diff = focused - compared;
    const sentiment = lowerBetter
      ? (diff < 0 ? 'good' : (diff > 0 ? 'bad' : 'flat'))
      : (diff > 0 ? 'good' : (diff < 0 ? 'bad' : 'flat'));
    rows.push(el('div', { class: 'gen-hero-vs-row' }, [
      el('div', { class: 'gen-hero-vs-label' }, [label]),
      el('div', { class: 'gen-hero-vs-value mono gen-hero-vs-' + sentiment },
        [_fmtSigned(diff)]),
    ]));
  };
  mkRow('Δ scalar', fOut.scalar_score_delta, cOut.scalar_score_delta, true);
  mkRow('Δ drift',  fOut.drift_loss_delta,   cOut.drift_loss_delta,   true);
  mkRow('Δ pass',   fOut.pass_rate_delta,    cOut.pass_rate_delta,    false);
  return el('div', { class: 'gen-hero-vs-col' }, [
    el('div', { class: 'gen-hero-vs-label gen-hero-vs-header' }, ['vs']),
    ...rows,
  ]);
}

function _renderHero(exp, epochId, generationId, comparedExp, comparedGenId,
                    onPickerChange) {
  const node = $('phase0-gen-compare');
  if (!node) return;
  clearChildren(node);

  let body;
  let accent = 'default';
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

  // Picker header — stays above the strip(s) so it stays clickable in
  // both modes.
  const picker = _buildComparePicker(epochId, generationId,
    comparedGenId, onPickerChange);
  const pickerWrap = el('div', { class: 'gen-compare-picker-wrap' }, [
    el('span', { class: 'gen-compare-picker-label' }, ['compare']),
    picker,
  ]);

  if (!comparedGenId) {
    // Single mode — one strip, like before.
    const single = _heroStripBody(exp, epochId, generationId);
    body = el('div', null, [pickerWrap, single.body]);
    accent = single.decision === 'promoted' ? 'promoted'
      : single.decision === 'rejected' ? 'rejected'
      : single.decision === 'deferred' ? 'warning' : 'default';
  } else if (!comparedExp) {
    // Compared gen id known but its experiment record has not landed —
    // render the focused side and an empty-state stub on the right.
    const left = _heroStripBody(exp, epochId, generationId, { compact: true });
    const right = el('div', { class: 'gen-hero-body' }, [
      el('p', { class: 'empty' },
        [`No experiment record for compared generation ${comparedGenId}.`]),
    ]);
    body = el('div', null, [
      pickerWrap,
      el('div', { class: 'gen-compare-pair' }, [
        el('div', { class: 'gen-compare-side focused' }, [left.body]),
        el('div', { class: 'gen-compare-side compared' }, [right]),
      ]),
    ]);
    accent = left.decision === 'promoted' ? 'promoted'
      : left.decision === 'rejected' ? 'rejected'
      : left.decision === 'deferred' ? 'warning' : 'default';
  } else {
    const left = _heroStripBody(exp, epochId, generationId, { compact: true });
    const right = _heroStripBody(comparedExp, epochId, comparedGenId,
      { compact: true });
    const vs = _heroVsColumn(exp, comparedExp);
    body = el('div', null, [
      pickerWrap,
      el('div', { class: 'gen-compare-pair gen-compare-pair-with-vs' }, [
        el('div', { class: 'gen-compare-side focused' }, [left.body]),
        vs,
        el('div', { class: 'gen-compare-side compared' }, [right.body]),
      ]),
    ]);
    accent = left.decision === 'promoted' ? 'promoted'
      : left.decision === 'rejected' ? 'rejected'
      : left.decision === 'deferred' ? 'warning' : 'default';
  }
  node.appendChild(renderCard({
    body, accent, variant: 'flush', class: 'gen-hero-card',
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

  if (_isStr(hyp.expected_pass_rate_delta) && out && _isNum(out.pass_rate_delta)) {
    const actual = out.pass_rate_delta;
    const nums = (hyp.expected_pass_rate_delta.match(/-?\d+(\.\d+)?/g) || [])
      .map(parseFloat);
    let aligned = null;
    if (nums.length >= 2) {
      const lo = Math.min(nums[0], nums[1]);
      const hi = Math.max(nums[0], nums[1]);
      aligned = actual >= lo && actual <= hi;
    } else if (nums.length === 1) {
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

// Build the single-mode hypothesis block (two inner columns —
// hypothesis prose left, alignment-vs-outcome right). Reused as the
// per-side body in compare mode.
function _hypothesisBlock(exp) {
  const hyp = (exp.hypothesis && typeof exp.hypothesis === 'object')
    ? exp.hypothesis : {};
  return el('div', { class: 'gen-hyp-block' }, [
    _renderHypothesisColumn(hyp),
    _renderAlignmentColumn(hyp, exp.outcome),
  ]);
}

function _renderHypothesis(exp, comparedExp, comparedGenId, generationId) {
  const node = $('phase0-gen-hypothesis');
  if (!node) return;
  clearChildren(node);
  let body;
  let subtitle = 'Proposed before; reconciled against the outcome.';
  if (state.epochDef == null) {
    body = renderLoadingState({ label: 'Loading hypothesis' });
  } else if (!exp) {
    body = renderEmptyState('No hypothesis recorded.');
  } else if (!comparedGenId) {
    body = _hypothesisBlock(exp);
  } else {
    // Compare mode — two side-by-side hypothesis blocks. The compared
    // side may still be loading; show a placeholder until its
    // experiment record lands.
    const leftHeader = el('div', { class: 'gen-compare-side-label' },
      ['v · ' + String(generationId)]);
    const left = el('div', { class: 'gen-compare-side focused' }, [
      leftHeader,
      _hypothesisBlock(exp),
    ]);
    const rightHeader = el('div', { class: 'gen-compare-side-label' },
      ['v · ' + String(comparedGenId)]);
    const rightBody = comparedExp ? _hypothesisBlock(comparedExp)
      : el('p', { class: 'empty' },
        [`No hypothesis record for compared generation ${comparedGenId}.`]);
    const right = el('div', { class: 'gen-compare-side compared' }, [
      rightHeader,
      rightBody,
    ]);
    body = el('div', { class: 'gen-compare-pair gen-compare-pair-2col' },
      [left, right]);
    subtitle = 'Side-by-side hypothesis & alignment.';
  }
  node.appendChild(renderCard({
    title: 'Hypothesis · Alignment',
    subtitle,
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

// Build the patch list body for one experiment — extracted so compare
// mode can render the same content per side.
function _patchListBody(exp) {
  const patches = exp && exp.patches;
  let entries = [];
  if (patches && typeof patches === 'object' && !Array.isArray(patches)) {
    for (const k of Object.keys(patches)) entries.push({ id: k, patch: patches[k] });
  } else if (Array.isArray(patches)) {
    entries = patches.map((p, i) => ({ id: (p && p.mutation_id) || ('p' + i), patch: p }));
  }
  if (entries.length === 0) return { body: renderEmptyState('No patches recorded.'), count: 0 };
  if (entries.length === 1) {
    const e = entries[0];
    const p = e.patch || {};
    const op = String(p.op || p.kind || '—');
    return {
      body: el('div', { class: 'gen-patch-inline' }, [
        el('div', { class: 'gen-patch-inline-head' }, [
          el('code', { class: 'mono code-pill' }, [e.id]),
          ' ',
          renderInlinePill(op.toUpperCase(), _opVariant(op)),
        ]),
        el('p', { class: 'gen-patch-inline-rationale' }, [
          String(p.rationale || p.message || '(no rationale recorded)'),
        ]),
      ]),
      count: 1,
    };
  }
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
  return { body: tbl, count: entries.length };
}

function _renderPatches(exp, comparedExp, comparedGenId, generationId) {
  const node = $('phase0-gen-patches');
  if (!node) return;
  clearChildren(node);
  let body;
  let titleSuffix = '';
  if (state.epochDef == null) {
    body = renderLoadingState({ label: 'Loading patches' });
    node.appendChild(renderCard({ title: 'Patches', body }));
    return;
  }
  if (!comparedGenId) {
    const single = _patchListBody(exp || {});
    body = single.body;
    if (single.count > 0) titleSuffix = ' (' + single.count + ')';
  } else {
    const left = _patchListBody(exp || {});
    const rightBody = comparedExp
      ? _patchListBody(comparedExp).body
      : el('p', { class: 'empty' },
        [`No patches record for compared generation ${comparedGenId}.`]);
    body = el('div', { class: 'gen-compare-pair gen-compare-pair-2col' }, [
      el('div', { class: 'gen-compare-side focused' }, [
        el('div', { class: 'gen-compare-side-label' },
          ['v · ' + String(generationId)]),
        left.body,
      ]),
      el('div', { class: 'gen-compare-side compared' }, [
        el('div', { class: 'gen-compare-side-label' },
          ['v · ' + String(comparedGenId)]),
        rightBody,
      ]),
    ]);
  }
  node.appendChild(renderCard({
    title: 'Patches' + titleSuffix,
    body,
  }));
}

// ---------------------------------------------------------------------------
// PER-ENTRY — table with "vs <champion>" delta and clickable rows.
// In compare mode the "vs" column instead reads "vs <compared-gen>".
// ---------------------------------------------------------------------------

function _entryDriftDelta(childDrift, parentDrift) {
  if (!_isNum(childDrift) || !_isNum(parentDrift)) return null;
  return childDrift - parentDrift;
}

function _passFailCell(pf) {
  if (pf === true || pf === 1 || String(pf).toLowerCase() === 'pass') {
    return renderInlinePill('pass', 'pass');
  }
  if (pf === false || pf === 0 || String(pf).toLowerCase() === 'fail') {
    return renderInlinePill('fail', 'fail');
  }
  if (pf == null) return el('span', { class: 'mono' }, ['—']);
  return el('span', { class: 'mono' }, [String(pf)]);
}

function _budgetCell(exceeded) {
  if (exceeded == null) return el('span', { class: 'mono' }, ['—']);
  if (exceeded) return renderInlinePill('over', 'warning');
  return renderInlinePill('OK', 'success');
}

function _deltaCell(delta) {
  if (delta == null) return el('span', { class: 'mono dim' }, ['—']);
  const sentiment = delta < 0 ? 'good' : (delta > 0 ? 'bad' : 'flat');
  return el('span', {
    class: 'mono gen-entry-delta gen-entry-delta-' + sentiment,
  }, [_fmtSigned(delta)]);
}

function _renderEntries(epochId, generationId, parentEntries,
                       comparedGenId, comparedEntries) {
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
        // In compare mode the right-hand peer is the picker target, NOT
        // the lineage parent. The header reads "vs <compared-gen>".
        const peerLabel = comparedGenId || (
          (state.epochDef && state.epochDef.experiments
            && findExperiment(generationId)
            && findExperiment(generationId).parent_generation_id) || '—');
        const peerData = comparedGenId
          ? (comparedEntries && Array.isArray(comparedEntries.entries)
            ? comparedEntries.entries : null)
          : (parentEntries && Array.isArray(parentEntries.entries)
            ? parentEntries.entries : null);
        const peerMap = new Map();
        if (peerData) {
          for (const pe of peerData) {
            if (pe && pe.entry_id != null) peerMap.set(pe.entry_id, pe);
          }
        }

        const tbl = el('table', { class: 'ds-table gen-entries-table' });
        if (!comparedGenId) {
          tbl.appendChild(el('thead', null, [el('tr', null, [
            el('th', null, ['entry']),
            el('th', null, ['drift loss']),
            el('th', null, ['vs ' + String(peerLabel)]),
            el('th', null, ['pass/fail']),
            el('th', null, ['budget']),
          ])]));
        } else {
          // Compare mode — unified table with focused + compared columns
          // per metric. The legacy "vs <champion>" column becomes
          // "vs <compared>" so the headline is unambiguous.
          tbl.appendChild(el('thead', null, [el('tr', null, [
            el('th', null, ['entry']),
            el('th', null, ['focused drift']),
            el('th', null, ['compared drift']),
            el('th', null, ['vs ' + String(peerLabel)]),
            el('th', null, ['focused pass/fail']),
            el('th', null, ['compared pass/fail']),
          ])]));
        }
        const tbody = el('tbody');
        for (const e of entries) {
          const peer = peerMap.get(e.entry_id);
          const delta = peer ? _entryDriftDelta(e.drift_loss, peer.drift_loss) : null;
          const href = phase0Href('run', {
            epochId,
            generationId,
            entryId: e.entry_id,
          });
          if (!comparedGenId) {
            tbody.appendChild(el('tr', { class: 'gen-entries-row' }, [
              el('td', { class: 'mono' }, [
                el('a', { href, class: 'gen-entries-link' }, [String(e.entry_id || '—')]),
              ]),
              el('td', { class: 'mono' }, [_fmtNum(e.drift_loss)]),
              el('td', null, [_deltaCell(delta)]),
              el('td', null, [_passFailCell(e.pass_fail)]),
              el('td', null, [_budgetCell(e.wall_clock_budget_exceeded)]),
            ]));
          } else {
            const peerDrift = peer ? peer.drift_loss : null;
            const peerPf = peer ? peer.pass_fail : null;
            tbody.appendChild(el('tr', { class: 'gen-entries-row' }, [
              el('td', { class: 'mono' }, [
                el('a', { href, class: 'gen-entries-link' }, [String(e.entry_id || '—')]),
              ]),
              el('td', { class: 'mono' }, [_fmtNum(e.drift_loss)]),
              el('td', { class: 'mono' },
                [peer ? _fmtNum(peerDrift) : '—']),
              el('td', null, [_deltaCell(delta)]),
              el('td', null, [_passFailCell(e.pass_fail)]),
              el('td', null, [peer ? _passFailCell(peerPf)
                : el('span', { class: 'mono dim' }, ['—'])]),
            ]));
          }
        }
        tbl.appendChild(tbody);
        body = el('div', null, [
          tbl,
          el('p', { class: 'gen-entries-hint' },
            ['Click an entry id to inspect its run transcript.']),
        ]);
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

function _perJudgeBody(data) {
  if (!data) return renderLoadingState({ label: 'Loading per-judge breakdown' });
  const judges = Array.isArray(data.judges) ? data.judges : [];
  if (judges.length === 0) {
    const msg = data.note ? '(no per-judge data: ' + data.note + ')'
      : '(no per-judge data recorded for this generation)';
    return { body: renderEmptyState(msg), count: 0 };
  }
  if (judges.length === 1) {
    const j = judges[0];
    return {
      body: el('div', { class: 'gen-judge-inline mono' }, [
        el('span', { class: 'gen-judge-name' }, [String(j.judge_name || '—')]),
        ' · weighted ', _fmtNum(j.weighted_loss),
        ' · raw ', _fmtNum(j.raw_loss),
        ' · weight ', _fmtNum(j.weight),
        j.run_count != null ? ' · ' + String(j.run_count) + ' runs' : '',
      ]),
      count: 1,
    };
  }
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
  return { body: tbl, count: judges.length };
}

function _renderJudges(epochId, generationId, comparedGenId) {
  const node = $('phase0-gen-judges');
  if (!node) return;
  clearChildren(node);
  let body;
  let titleSuffix = '';
  if (!epochId || !generationId) {
    body = el('p', { class: 'empty' }, ['No generation selected.']);
  } else {
    const data = _perJudgeCache.get(epochId + '/' + generationId);
    if (!comparedGenId) {
      // Single mode — preserve the legacy "inline if 1, table if 2+"
      // shape exactly.
      const wrapped = _perJudgeBody(data);
      if (wrapped instanceof Object && 'body' in wrapped) {
        body = wrapped.body;
        if (wrapped.count > 0) titleSuffix = ' (' + wrapped.count + ')';
      } else {
        body = wrapped;
      }
    } else {
      // Compare mode — two columns, each a per-judge breakdown.
      const left = _perJudgeBody(data);
      const compared = _perJudgeCache.get(epochId + '/' + comparedGenId);
      const right = _perJudgeBody(compared);
      const leftBody = (left && left.body) ? left.body : left;
      const rightBody = (right && right.body) ? right.body : right;
      body = el('div', { class: 'gen-compare-pair gen-compare-pair-2col' }, [
        el('div', { class: 'gen-compare-side focused' }, [
          el('div', { class: 'gen-compare-side-label' },
            ['v · ' + String(generationId)]),
          leftBody,
        ]),
        el('div', { class: 'gen-compare-side compared' }, [
          el('div', { class: 'gen-compare-side-label' },
            ['v · ' + String(comparedGenId)]),
          rightBody,
        ]),
      ]);
    }
  }
  node.appendChild(renderCard({
    title: 'Per-judge' + titleSuffix,
    body,
  }));
}

// ---------------------------------------------------------------------------
// Digest helpers — see L4 for the rationale. Each card lists ONLY the
// inputs it actually reads so a heartbeat tick does not invalidate it.
// ---------------------------------------------------------------------------

function _summariseExp(exp) {
  if (!exp) return null;
  const out = exp.outcome || {};
  return {
    g: exp.generation_id || null,
    p: exp.parent_generation_id || null,
    d: out.tournament_decision || out.decision || null,
    s: _isNum(out.scalar_score_delta) ? out.scalar_score_delta : null,
    dr: _isNum(out.drift_loss_delta) ? out.drift_loss_delta : null,
    pr: _isNum(out.pass_rate_delta) ? out.pass_rate_delta : null,
  };
}

function _summarisePerEntry(payload) {
  if (!payload) return null;
  const entries = Array.isArray(payload.entries) ? payload.entries : [];
  return entries.length;
}

function _summarisePerJudge(payload) {
  if (!payload) return null;
  const judges = Array.isArray(payload.judges) ? payload.judges : [];
  return judges.length;
}

export function generationViewDigest(params) {
  const epochId = (params && params.epochId)
    || (state.epochDef && state.epochDef.epoch_id) || null;
  const generationId = (params && params.generationId) || null;
  const compared = compareGenFor(epochId, generationId);
  const exp = findExperiment(generationId);
  const comparedExp = compared ? findExperiment(compared) : null;
  const fEntry = epochId && generationId
    ? _perEntryCache.get(epochId + '/' + generationId) : null;
  const cEntry = epochId && compared
    ? _perEntryCache.get(epochId + '/' + compared) : null;
  const parentId = exp && exp.parent_generation_id || null;
  const pEntry = epochId && parentId
    ? _perEntryCache.get(epochId + '/' + parentId) : null;
  const fJudge = epochId && generationId
    ? _perJudgeCache.get(epochId + '/' + generationId) : null;
  const cJudge = epochId && compared
    ? _perJudgeCache.get(epochId + '/' + compared) : null;

  // Number of generations in the focused epoch — the picker reads that
  // (via state.lineage / state.epochDef) for its option list, so a
  // change to the list must invalidate the hero digest.
  const def = state.epochDef || {};
  const exps = Array.isArray(def.experiments) ? def.experiments : [];
  const inDefCount = exps.filter((e) => e && e.generation_id).length;
  const lineageGens = (state.lineage && Array.isArray(state.lineage.generations))
    ? state.lineage.generations : [];
  const inLineageCount = lineageGens
    .filter((g) => g && g.epoch_id === epochId).length;

  return {
    hero: {
      epochId, generationId, compared,
      f: _summariseExp(exp),
      c: _summariseExp(comparedExp),
      optsCount: inDefCount + ':' + inLineageCount,
    },
    hypothesis: {
      epochId, generationId, compared,
      f: _summariseExp(exp),
      c: _summariseExp(comparedExp),
    },
    patches: {
      epochId, generationId, compared,
      f: _summariseExp(exp),
      c: _summariseExp(comparedExp),
    },
    entries: {
      epochId, generationId, compared,
      f: _summarisePerEntry(fEntry),
      c: _summarisePerEntry(cEntry),
      p: _summarisePerEntry(pEntry),
    },
    judges: {
      epochId, generationId, compared,
      f: _summarisePerJudge(fJudge),
      c: _summarisePerJudge(cJudge),
    },
  };
}

// ---------------------------------------------------------------------------
// Public entrypoint.
// ---------------------------------------------------------------------------

export function renderPhase0Generation(params, repaint) {
  const epochId = (params && params.epochId)
    || (state.epochDef && state.epochDef.epoch_id) || null;
  const generationId = (params && params.generationId) || null;
  // URL-seeded compare target: ``#/gen/<epoch>/<gen>/<compareGen>``
  // primes the picker on first paint when no in-memory state has been
  // set for this (epoch, focused) pair yet. The in-memory state still
  // wins once the user touches the picker — so once a compare target
  // is cleared, it stays cleared.
  const urlSeed = (params && params.compareGenerationId) || null;
  if (urlSeed && epochId && generationId
      && !_compareGenByFocused.has(epochId + '/' + generationId)
      && urlSeed !== generationId) {
    _compareGenByFocused.set(epochId + '/' + generationId, urlSeed);
  }
  const exp = findExperiment(generationId);
  const comparedGenId = compareGenFor(epochId, generationId);
  const comparedExp = comparedGenId ? findExperiment(comparedGenId) : null;

  ensurePerJudge(epochId, generationId, repaint);
  ensurePerEntry(epochId, generationId, repaint);
  // The vs-champion column needs the parent's per-entry payload too.
  const parentId = exp ? exp.parent_generation_id : null;
  if (parentId) ensurePerEntry(epochId, parentId, repaint);
  // Compare mode — fetch the compared gen's per-entry and per-judge
  // payloads so both sides paint with non-null data.
  if (comparedGenId) {
    ensurePerEntry(epochId, comparedGenId, repaint);
    ensurePerJudge(epochId, comparedGenId, repaint);
  }
  const parentEntries = parentId
    ? _perEntryCache.get(epochId + '/' + parentId)
    : null;
  const comparedEntries = comparedGenId
    ? _perEntryCache.get(epochId + '/' + comparedGenId)
    : null;

  const onPickerChange = (next) => {
    setCompareGenFor(epochId, generationId, next);
    // Reflect the picker in the URL so a side-by-side view is
    // shareable. The router parses ``#/gen/<epoch>/<gen>/<cmp>``; we
    // rewrite the third segment in place (or drop it for "off").
    try {
      if (typeof window !== 'undefined' && window.location && epochId
          && generationId) {
        const base = '#/gen/' + encodeURIComponent(epochId)
          + '/' + encodeURIComponent(generationId);
        const target = next
          ? base + '/' + encodeURIComponent(next)
          : base;
        if (window.location.hash !== target) {
          window.location.hash = target;
        }
      }
    } catch {
      // window.location is locked-down or absent (test harness); fall
      // through and rely on the in-memory state alone.
    }
    _forceNextGenRender = true;
    if (typeof repaint === 'function') repaint();
    else renderPhase0Generation(params, repaint);
  };

  // Per-card digest gates — heartbeat ticks should NOT rebuild the
  // hero card (which hosts the compare picker), so the digest covers
  // only payload-shaped inputs.
  const digests = generationViewDigest(params);
  const force = _forceNextGenRender;
  _forceNextGenRender = false;

  const heroDigest = JSON.stringify(digests.hero);
  if (force || heroDigest !== _lastHeroDigest) {
    _lastHeroDigest = heroDigest;
    _renderHero(exp, epochId, generationId, comparedExp, comparedGenId,
      onPickerChange);
  }
  const hypDigest = JSON.stringify(digests.hypothesis);
  if (force || hypDigest !== _lastHypothesisDigest) {
    _lastHypothesisDigest = hypDigest;
    _renderHypothesis(exp, comparedExp, comparedGenId, generationId);
  }
  const patchDigest = JSON.stringify(digests.patches);
  if (force || patchDigest !== _lastPatchesDigest) {
    _lastPatchesDigest = patchDigest;
    _renderPatches(exp, comparedExp, comparedGenId, generationId);
  }
  const entryDigest = JSON.stringify(digests.entries);
  if (force || entryDigest !== _lastEntriesDigest) {
    _lastEntriesDigest = entryDigest;
    _renderEntries(epochId, generationId, parentEntries,
      comparedGenId, comparedEntries);
  }
  const judgeDigest = JSON.stringify(digests.judges);
  if (force || judgeDigest !== _lastJudgesDigest) {
    _lastJudgesDigest = judgeDigest;
    _renderJudges(epochId, generationId, comparedGenId);
  }
}

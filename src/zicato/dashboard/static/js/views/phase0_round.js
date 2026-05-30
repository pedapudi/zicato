// views/phase0_round.js — L3 (round/tournament) view, rebuilt as the
// DECISION VIEW: the legible "why was this challenger promoted or
// rejected?".
//
// Layout maps onto the four existing L3 slots:
//   * phase0-round-vs       — header (champion → challenger + verdict
//                             glyph), the GATE LADDER (the why), the
//                             SCALAR BAND (±margin threshold) and the
//                             SCALAR WATERFALL (which component moved).
//   * phase0-round-entries  — per-entry A/B as a DIVERGING BAR (each
//                             entry's challenger−champion drift delta;
//                             pass→fail flips are flagged).
//   * phase0-round-judges   — the PRIMARY-DRIVER call-out.
//   * phase0-round-decision — PROMOTE / REJECT controls (honest:
//                             "recorded (manual enactment)").
//
// Data sources:
//   * NEW  /api/round/{e}/{champ}/{chal}/gate            — gate ladder +
//          decision + scalar_components + primary_driver.
//   * /api/matchup-grid/{e}/{champ}/{chal}               — per-entry A/B.
//   * /api/round/{e}/{champ}/{chal}/per-judge-comparison — judges + driver.
//
// Every endpoint may 404 on a cold deep-link; each fetch degrades to a
// loading/empty state and NEVER throws. Renders are re-render-safe — the
// caches mean a repaint paints the same data without refetching.

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson, postControl } from '../core/api.js';
import { state } from '../core/state.js';
import { renderCard, renderCalloutCard } from '../components/card.js';
import { renderLoadingState, renderEmptyState } from '../components/loading.js';
import { harmonografGenLink } from '../core/harmonograf.js';

import { gateLadder } from '../components/gate_ladder.js';
import { divergingBar } from '../components/diverging_bar.js';
import { scalarWaterfall } from '../components/scalar_waterfall.js';
import { scalarBand } from '../components/scalar_band.js';
import { verdictGlyph } from '../components/verdict_glyph.js';

const _gateCache = new Map();
const _gridCache = new Map();
const _judgeCmpCache = new Map();
const _loadingGate = new Set();
const _loadingGrid = new Set();
const _loadingJudges = new Set();

function _key(epochId, champId, chalId) {
  return epochId + '/' + champId + '->' + chalId;
}

export function resetRoundCaches() {
  _gateCache.clear();
  _gridCache.clear();
  _judgeCmpCache.clear();
  _loadingGate.clear();
  _loadingGrid.clear();
  _loadingJudges.clear();
}

// Test/inspection accessors.
export function roundGatePayload(epochId, champId, chalId) {
  return _gateCache.get(_key(epochId, champId, chalId)) || null;
}
export function roundGridPayload(epochId, champId, chalId) {
  return _gridCache.get(_key(epochId, champId, chalId)) || null;
}
export function roundJudgesPayload(epochId, champId, chalId) {
  return _judgeCmpCache.get(_key(epochId, champId, chalId)) || null;
}

// --- fetchers (each degrades, never throws) --------------------------

async function ensureGate(epochId, champId, chalId, repaint) {
  if (!epochId || !champId || !chalId) return null;
  const key = _key(epochId, champId, chalId);
  if (_gateCache.has(key)) return _gateCache.get(key);
  if (_loadingGate.has(key)) return null;
  _loadingGate.add(key);
  try {
    const data = await fetchJson('/api/round/'
      + encodeURIComponent(epochId) + '/'
      + encodeURIComponent(champId) + '/' + encodeURIComponent(chalId) + '/gate');
    if (data && typeof data === 'object') _gateCache.set(key, data);
    else _gateCache.set(key, _emptyGate());
  } catch {
    // Cold deep-link / not-yet-shipped endpoint — degrade gracefully.
    _gateCache.set(key, _emptyGate());
  } finally {
    _loadingGate.delete(key);
    if (typeof repaint === 'function') repaint();
  }
  return _gateCache.get(key);
}

function _emptyGate() {
  return {
    decision: null, reason: '', delta_scalar: null, delta_pass_rate: null,
    rules: [], scalar_components: null, primary_driver: null, _empty: true,
  };
}

async function ensureGrid(epochId, champId, chalId, repaint) {
  if (!epochId || !chalId) return null;
  const key = _key(epochId, champId, chalId);
  if (_gridCache.has(key)) return _gridCache.get(key);
  if (_loadingGrid.has(key)) return null;
  _loadingGrid.add(key);
  try {
    const data = await fetchJson('/api/matchup-grid/'
      + encodeURIComponent(epochId) + '/'
      + encodeURIComponent(champId) + '/' + encodeURIComponent(chalId));
    _gridCache.set(key, (data && typeof data === 'object') ? data : { entry_grid: [] });
  } catch {
    _gridCache.set(key, { entry_grid: [], scalar: null, _empty: true });
  } finally {
    _loadingGrid.delete(key);
    if (typeof repaint === 'function') repaint();
  }
  return _gridCache.get(key);
}

async function ensureJudgesComparison(epochId, champId, chalId, repaint) {
  if (!epochId || !champId || !chalId) return null;
  const key = _key(epochId, champId, chalId);
  if (_judgeCmpCache.has(key)) return _judgeCmpCache.get(key);
  if (_loadingJudges.has(key)) return null;
  _loadingJudges.add(key);
  try {
    const data = await fetchJson('/api/round/'
      + encodeURIComponent(epochId) + '/'
      + encodeURIComponent(champId) + '/' + encodeURIComponent(chalId)
      + '/per-judge-comparison');
    _judgeCmpCache.set(key, (data && typeof data === 'object')
      ? data
      : { judges: [], primary_driver: null });
  } catch {
    _judgeCmpCache.set(key, {
      epoch_id: epochId, champion: champId, challenger: chalId,
      judges: [], primary_driver: null,
    });
  } finally {
    _loadingJudges.delete(key);
    if (typeof repaint === 'function') repaint();
  }
  return _judgeCmpCache.get(key);
}

// --- helpers ---------------------------------------------------------

function _fmtNum(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  return v.toFixed(3);
}

function _normalizeDecision(d) {
  return d == null ? null : String(d).toLowerCase();
}

// The promote margin: prefer an explicit margin baked into a gate rule's
// detail, else the scoring margin in app state, else null (band omitted).
function _resolveMargin(gate) {
  const rules = gate && Array.isArray(gate.rules) ? gate.rules : [];
  for (const r of rules) {
    if (r && (r.id === 'scalar_margin' || /margin/i.test(r.label || ''))) {
      if (typeof r.margin === 'number' && isFinite(r.margin)) return r.margin;
      const m = String(r.detail || '').match(/(\d+(?:\.\d+)?)/);
      if (m) {
        const v = parseFloat(m[1]);
        if (isFinite(v)) return v;
      }
    }
  }
  const sm = state.scoring && state.scoring.margin;
  return (typeof sm === 'number' && isFinite(sm)) ? sm : null;
}

function _isPass(v) {
  return v === true || v === 1 || String(v).toLowerCase() === 'pass';
}

// --- HEADER + GATE LADDER + BAND + WATERFALL (vs slot) ---------------

function _renderHeaderAndGate(epochId, champId, chalId) {
  const node = $('phase0-round-vs');
  if (!node) return;
  clearChildren(node);

  if (!epochId || !champId || !chalId) {
    node.appendChild(renderCard({
      title: 'Decision',
      body: el('p', { class: 'empty' }, ['No matchup selected.']),
    }));
    return;
  }

  const gate = _gateCache.get(_key(epochId, champId, chalId));
  if (!gate) {
    node.appendChild(renderCard({
      title: 'Decision',
      body: renderLoadingState({ label: 'Loading decision' }),
    }));
    return;
  }

  const decision = _normalizeDecision(gate.decision);
  const champLink = harmonografGenLink(champId);
  const chalLink = harmonografGenLink(chalId);

  // 1. Header: champion → challenger + big verdict glyph + verdict word.
  const header = el('div', { class: 'decision-header' }, [
    el('div', { class: 'decision-header-matchup' }, [
      el('span', { class: 'decision-side decision-side-champion mono' },
        [champId, champLink ? ' ' : null, champLink].filter(Boolean)),
      el('span', { class: 'decision-arrow', 'aria-hidden': 'true' }, ['→']),
      el('span', { class: 'decision-side decision-side-challenger mono' },
        [chalId, chalLink ? ' ' : null, chalLink].filter(Boolean)),
    ]),
    el('div', { class: 'decision-verdict' }, [
      verdictGlyph(decision || 'neutral', { withLabel: true }),
    ]),
  ]);

  const sections = [header];

  // 2. Gate ladder — the legible why.
  const rules = Array.isArray(gate.rules) ? gate.rules : [];
  sections.push(el('div', { class: 'decision-section' }, [
    el('h4', { class: 'decision-section-title' }, ['Gate']),
    rules.length
      ? gateLadder({ rules })
      : renderEmptyState('Gate rules not available for this round.'),
  ]));

  // 3. Scalar band — where the challenger landed vs the ±margin window.
  const sc = gate.scalar_components || null;
  const champScalar = sc && typeof sc.champion === 'number' ? sc.champion : null;
  const chalScalar = sc && typeof sc.challenger === 'number' ? sc.challenger : null;
  const margin = _resolveMargin(gate);
  let band = null;
  if (champScalar != null && chalScalar != null) {
    band = scalarBand({
      champion: champScalar,
      challenger: chalScalar,
      margin: margin != null ? margin : 0,
    });
  }
  if (band) {
    sections.push(el('div', { class: 'decision-section' }, [
      el('h4', { class: 'decision-section-title' }, ['Scalar vs margin']),
      band,
    ]));
  }

  // 4. Scalar waterfall — which component moved the loss.
  const wfComponents = _waterfallComponents(epochId, champId, chalId, sc);
  if (wfComponents.length) {
    sections.push(el('div', { class: 'decision-section' }, [
      el('h4', { class: 'decision-section-title' }, ['What moved the loss']),
      scalarWaterfall({ components: wfComponents, label: 'challenger − champion, per component' }),
    ]));
  }

  node.appendChild(renderCard({
    title: 'Decision',
    accent: decision === 'promoted' ? 'success'
      : (decision === 'rejected' ? 'error' : 'default'),
    body: el('div', { class: 'decision-view' }, sections),
  }));
}

// The waterfall components are the per-component challenger−champion
// change. The gate's scalar_components carries the two scalar totals; the
// matchup-grid's `scalar.components` already carries the per-component
// deltas — prefer that. Falls back to nothing when neither is present.
function _waterfallComponents(epochId, champId, chalId, gateScalar) {
  const grid = _gridCache.get(_key(epochId, champId, chalId));
  const comps = grid && grid.scalar && grid.scalar.components;
  if (comps && typeof comps === 'object') {
    return Object.keys(comps)
      .filter((name) => typeof comps[name] === 'number' && isFinite(comps[name]))
      .map((name) => ({ name, delta: comps[name] }));
  }
  // Fall back to a single aggregate bar from the gate's scalar totals.
  if (gateScalar && typeof gateScalar.champion === 'number'
      && typeof gateScalar.challenger === 'number') {
    return [{ name: 'scalar', delta: gateScalar.challenger - gateScalar.champion }];
  }
  return [];
}

// --- PER-ENTRY A/B as a DIVERGING BAR (entries slot) -----------------

function _renderEntries(epochId, champId, chalId) {
  const node = $('phase0-round-entries');
  if (!node) return;
  clearChildren(node);

  let body;
  if (!epochId || !chalId) {
    body = el('p', { class: 'empty' }, ['No matchup selected.']);
  } else {
    const grid = _gridCache.get(_key(epochId, champId, chalId));
    if (!grid) {
      body = renderLoadingState({ label: 'Loading per-entry comparison' });
    } else {
      const entryGrid = Array.isArray(grid.entry_grid) ? grid.entry_grid : [];
      if (entryGrid.length === 0) {
        body = renderEmptyState('No per-entry data recorded for this round.');
      } else {
        const rows = [];
        for (const e of entryGrid) {
          if (!e) continue;
          const delta = (typeof e.delta === 'number' && isFinite(e.delta))
            ? e.delta
            : ((typeof e.child_drift_loss === 'number' && typeof e.parent_drift_loss === 'number')
                ? e.child_drift_loss - e.parent_drift_loss : null);
          if (delta == null) continue;
          // Flag a pass→fail flip (a monotonicity regression).
          const champPass = _isPass(e.parent_pass);
          const chalPass = _isPass(e.child_pass);
          const flipped = (e.parent_pass != null && e.child_pass != null)
            && champPass && !chalPass;
          rows.push({
            label: e.entry_id || '—',
            delta,
            annotation: flipped
              ? { glyph: '⚠', title: 'pass→fail (monotonicity)' }
              : undefined,
          });
        }
        if (rows.length === 0) {
          body = renderEmptyState('No comparable per-entry drift deltas.');
        } else {
          // Lower drift loss is better → a negative challenger−champion
          // delta is the good direction.
          body = divergingBar({ rows, goodWhenNegative: true });
        }
      }
    }
  }
  node.appendChild(renderCard({
    title: 'Per-entry A/B (challenger − champion drift)',
    body,
  }));
}

// --- PRIMARY DRIVER call-out (judges slot) ---------------------------

function _driverFromGate(gate) {
  // Gate's primary_driver is {judge, delta} | null.
  const pd = gate && gate.primary_driver;
  if (pd && typeof pd === 'object' && pd.judge) {
    return { judge: pd.judge, delta: typeof pd.delta === 'number' ? pd.delta : null };
  }
  return null;
}

function _driverFromJudges(judgeData) {
  // Per-judge-comparison's primary_driver is a judge_name string.
  const name = judgeData && judgeData.primary_driver;
  if (typeof name !== 'string' || !name) return null;
  const judges = Array.isArray(judgeData.judges) ? judgeData.judges : [];
  const match = judges.find((j) => j && j.judge_name === name);
  return { judge: name, delta: match && typeof match.delta === 'number' ? match.delta : null };
}

function _renderDriver(epochId, champId, chalId) {
  const node = $('phase0-round-judges');
  if (!node) return;
  clearChildren(node);

  if (!epochId || !champId || !chalId) {
    node.appendChild(renderCard({
      title: 'Primary driver',
      body: el('p', { class: 'empty' }, ['No matchup selected.']),
    }));
    return;
  }

  const gate = _gateCache.get(_key(epochId, champId, chalId));
  const judgeData = _judgeCmpCache.get(_key(epochId, champId, chalId));
  if (!gate && !judgeData) {
    node.appendChild(renderCard({
      title: 'Primary driver',
      body: renderLoadingState({ label: 'Loading primary driver' }),
    }));
    return;
  }

  const driver = _driverFromGate(gate) || _driverFromJudges(judgeData);
  if (!driver) {
    node.appendChild(renderCard({
      title: 'Primary driver',
      body: renderEmptyState('No primary driver recorded for this round.'),
    }));
    return;
  }

  const deltaText = driver.delta != null
    ? (driver.delta > 0 ? '+' : '') + driver.delta.toFixed(3)
    : '—';
  const deltaCls = 'decision-driver-delta mono'
    + (driver.delta != null ? (driver.delta < 0 ? ' is-good' : (driver.delta > 0 ? ' is-bad' : '')) : '');

  node.appendChild(renderCard({
    title: 'Primary driver',
    body: el('p', { class: 'decision-driver' }, [
      'Decision driven by judge ',
      el('strong', { class: 'mono' }, [driver.judge]),
      ' (Δ ',
      el('span', { class: deltaCls }, [deltaText]),
      ').',
    ]),
  }));
}

// --- PROMOTE / REJECT controls (decision slot) -----------------------

function _renderControls(epochId, champId, chalId) {
  const node = $('phase0-round-decision');
  if (!node) return;
  clearChildren(node);

  const challengerId = chalId;
  if (!challengerId) {
    node.appendChild(renderCard({
      title: 'Controls',
      body: el('p', { class: 'empty' }, ['No challenger selected.']),
    }));
    return;
  }

  const readOnly = !!(state.health && state.health.read_only);

  const status = el('p', {
    class: 'decision-controls-status',
    style: 'margin:var(--space-3) 0 0; font-size:var(--font-size-12); color:var(--color-text-muted);',
  }, ['']);

  function makeButton(action, label, variant) {
    const btn = el('button', {
      class: 'btn btn-' + variant + ' decision-control decision-control-' + action,
      type: 'button',
      'data-action': action,
    }, [label]);
    if (readOnly) {
      btn.setAttribute('disabled', 'disabled');
      btn.setAttribute('aria-disabled', 'true');
    } else {
      btn.addEventListener('click', async () => {
        // Confirm before POST.
        const ok = (typeof globalThis.confirm === 'function')
          ? globalThis.confirm(action + ' challenger ' + challengerId + '?')
          : true;
        if (!ok) return;
        btn.setAttribute('disabled', 'disabled');
        status.textContent = 'sending ' + action + '…';
        try {
          const res = await postControl(action + '/' + encodeURIComponent(challengerId), {
            epoch_id: epochId, champion: champId, challenger: challengerId,
          });
          if (res && res.ok) {
            status.textContent = action + ' recorded (manual enactment).';
          } else {
            status.textContent = action + ' rejected (HTTP '
              + (res ? res.status : '?') + ').';
            btn.removeAttribute('disabled');
          }
        } catch {
          status.textContent = action + ' failed (network).';
          btn.removeAttribute('disabled');
        }
      });
    }
    return btn;
  }

  const buttons = el('div', { class: 'decision-controls-row' }, [
    makeButton('promote', 'Promote', 'success'),
    makeButton('reject', 'Reject', 'error'),
  ]);

  const note = el('p', {
    class: 'decision-controls-note',
    style: 'margin:var(--space-2) 0 0; font-size:var(--font-size-11); color:var(--color-text-muted);',
  }, [readOnly
    ? 'read-only workspace — controls disabled.'
    : 'recorded (manual enactment) — the orchestrator does not yet consume control files.']);

  node.appendChild(renderCalloutCard({
    title: 'Controls',
    accent: readOnly ? 'warning' : 'accent',
    body: el('div', { class: 'decision-controls' }, [buttons, note, status]),
  }));
}

// --- entry point -----------------------------------------------------

export function renderPhase0Round(params, repaint) {
  const epochId = (params && params.epochId)
    || (state.epochDef && state.epochDef.epoch_id) || null;
  const champId = (params && params.championId) || null;
  const chalId = (params && params.challengerId) || null;

  ensureGate(epochId, champId, chalId, repaint);
  ensureGrid(epochId, champId, chalId, repaint);
  ensureJudgesComparison(epochId, champId, chalId, repaint);

  _renderHeaderAndGate(epochId, champId, chalId);
  _renderEntries(epochId, champId, chalId);
  _renderDriver(epochId, champId, chalId);
  _renderControls(epochId, champId, chalId);
}

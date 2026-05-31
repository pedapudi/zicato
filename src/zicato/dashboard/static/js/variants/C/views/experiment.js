// variants/C/views/experiment.js — Screen 3: THE causal flow.
//
// The signature screen. A Sankey diagram that literally shows
// cause → effect → verdict as flow:
//
//   PATCH (mutation points)  →  DRIFT KINDS that moved  →  GATE outcome
//
//   * left   — one node per mutation point the patch touched (the cause).
//   * middle — one node per drift kind that moved between champion and
//              challenger; ribbon WIDTH = magnitude of the movement.
//   * right  — the gate verdict (PROMOTE / REJECT), with the headline
//              deltas.
//
// Hovering a node lights the flow it participates in; clicking a
// mutation node opens the DIFF in the drawer (the diff is accessible but
// the FLOW leads). For a v0 baseline (no tournament) we show the seed's
// board results as nodes instead.
//
// Data: state.epochDef.experiments (gate outcome + hypothesis),
//       /api/drift-movements/:gen (the effect), /api/files/.../patches
//       and /api/files/.../diff (the cause detail).

import { el, svgEl, clearChildren } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { createSurface, createSurfaceControls } from '../diagram/surface.js';
import { ribbonPath, verdictClass, verdictLabel } from '../diagram/primitives.js';
import { layoutSankey } from '../diagram/sankey.js';
import { experimentById, decisionOf, mutationPointsOf, isBaselineSeed } from '../model.js';
import { openDrawer } from '../chrome.js';
import { fmtDelta, fmtScalar } from '../../../core/format.js';

// Per-view caches keyed by gen id, so an SSE refresh does not re-fetch.
const _driftCache = new Map();
const _driftLoading = new Set();
const _patchCache = new Map();
const _patchLoading = new Set();
const _diffCache = new Map();

export function resetExperimentCaches() {
  _driftCache.clear(); _driftLoading.clear();
  _patchCache.clear(); _patchLoading.clear();
  _diffCache.clear();
}

export function renderExperiment(ctx) {
  const { stage, state, params, chrome, repaint } = ctx;
  const epochId = params.epochId;
  const genId = params.genId;
  clearChildren(stage);

  const exp = experimentById(state, genId);

  stage.appendChild(el('div', { class: 'cz-screen-head' }, [
    el('div', { class: 'cz-epoch-eyebrow' }, [
      'EXPERIMENT', el('span', { class: 'cz-mono' }, [genId || '—']),
      epochId ? el('span', { class: 'cz-tag cz-tag-open' }, [epochId]) : null,
    ]),
    el('h1', { class: 'cz-screen-title' }, ['Causal flow']),
    el('p', { class: 'cz-screen-sub' }, [
      'The patch is the cause; the drift kinds that moved are the effect; the gate is the verdict. '
      + 'Ribbon width is the magnitude of the movement. Click a mutation node to read its diff.',
    ]),
  ]));

  if (!exp && !genId) {
    stage.appendChild(el('div', { class: 'cz-empty' }, ['No experiment selected. Click a node on the Environment map or Epoch gauntlet.']));
    return;
  }

  // Kick off lazy fetches.
  ensureDrift(genId, repaint);
  ensurePatches(epochId, genId, repaint);

  const hyp = (exp && exp.hypothesis) || {};
  const outcome = (exp && exp.outcome) || null;
  const baseline = exp && isBaselineSeed(exp);

  // Hypothesis banner (the operator's stated cause→effect prediction).
  if (hyp.core_idea) {
    stage.appendChild(el('div', { class: 'cz-hyp-banner' }, [
      el('span', { class: 'cz-hyp-lead' }, ['Hypothesis']),
      el('span', { class: 'cz-hyp-text' }, [String(hyp.core_idea)]),
    ]));
  }

  if (baseline) {
    stage.appendChild(renderBaselineFlow(exp, epochId, genId, chrome));
    return;
  }

  stage.appendChild(renderSankey(state, exp, epochId, genId, chrome));

  // The verdict strip below the flow — the gate's reasons in words.
  stage.appendChild(renderVerdictStrip(exp, outcome));
}

// -- the Sankey -------------------------------------------------------
function renderSankey(state, exp, epochId, genId, chrome) {
  const mpts = mutationPointsOf(exp);
  const drift = _driftCache.get(genId);
  const movements = (drift && Array.isArray(drift.movements)) ? drift.movements : [];
  const dec = decisionOf(exp) || 'running';

  // PATCH column.
  const patchNodes = (mpts.length ? mpts : ['(patch)']).map((p, i) => ({
    id: 'patch:' + i, label: shortPath(p), sub: 'mutation point', cls: 'cz-flow-patch', ref: { kind: 'patch', path: p },
    value: 1,
  }));

  // DRIFT column — only kinds that actually moved; value = |delta|.
  let driftNodes = movements
    .filter((m) => m && (m.delta || 0) !== 0)
    .map((m) => ({
      id: 'drift:' + m.kind,
      label: prettyKind(m.kind),
      sub: `${m.direction} ${signed(m.delta)}`,
      cls: m.direction === 'improved' ? 'cz-flow-improved' : (m.direction === 'worsened' ? 'cz-flow-worsened' : 'cz-flow-flat'),
      value: Math.abs(m.delta) || 1,
      ref: { kind: 'drift', mv: m },
    }));
  if (driftNodes.length === 0) {
    driftNodes = [{ id: 'drift:none', label: drift ? 'no drift moved' : 'loading…', sub: '', cls: 'cz-flow-flat', value: 1 }];
  }

  // GATE column — the verdict.
  const gateNode = {
    id: 'gate', label: verdictLabel(dec), sub: 'gate verdict', cls: verdictClass(dec),
    value: Math.max(driftNodes.reduce((a, n) => a + n.value, 0), 1),
    ref: { kind: 'gate' },
  };

  // LINKS: every patch → every moved drift kind (cause→effect), then
  // every drift kind → gate (effect→verdict). Magnitude carried on the
  // drift side; patch links share equally so the cause column reads.
  const links = [];
  const totalDrift = driftNodes.reduce((a, n) => a + n.value, 0) || 1;
  for (const p of patchNodes) {
    for (const d of driftNodes) {
      links.push({ source: p.id, target: d.id, value: (d.value / patchNodes.length), cls: d.cls });
    }
  }
  for (const d of driftNodes) {
    links.push({ source: d.id, target: gateNode.id, value: d.value, cls: d.cls });
  }
  void totalDrift;

  const layout = layoutSankey({
    patch: patchNodes, drift: driftNodes, gate: [gateNode], links,
    colW: 168, colGap: 190, nodeW: 150, colHeight: 430,
  });

  const surface = createSurface({
    width: Math.max(layout.box.w, 960), height: Math.max(layout.box.h, 360),
    ariaLabel: 'Causal flow: patch to drift to gate',
  });
  const vp = surface.viewport;

  // Stage column headers.
  const headerY = 22;
  const headers = [
    { x: layout.nodes.find((n) => n.stage === 'patch')?.x ?? 0, t: 'PATCH · the cause' },
    { x: layout.nodes.find((n) => n.stage === 'drift')?.x ?? 0, t: 'DRIFT · the effect' },
    { x: layout.nodes.find((n) => n.stage === 'gate')?.x ?? 0, t: 'GATE · the verdict' },
  ];
  for (const h of headers) {
    vp.appendChild(svgEl('text', { x: h.x, y: headerY, class: 'cz-sankey-col-head' }, [h.t]));
  }

  // Ribbons.
  const ribbonLayer = svgEl('g', { class: 'cz-ribbon-layer' });
  for (const l of layout.links) {
    const d = ribbonPath(l.sx, l.sy, l.tx, l.ty, l.hwS, l.hwT);
    const path = svgEl('path', {
      d, class: 'cz-ribbon ' + (l.cls || ''),
      'data-source': l.source, 'data-target': l.target,
    });
    ribbonLayer.appendChild(path);
  }
  vp.appendChild(ribbonLayer);

  // Nodes.
  const nodeLayer = svgEl('g', { class: 'cz-node-layer' });
  for (const n of layout.nodes) {
    const grp = svgEl('g', {
      class: 'cz-sankey-node ' + (n.cls || ''),
      'data-id': n.id,
      tabindex: '0',
      role: n.ref && n.ref.kind === 'patch' ? 'button' : 'group',
      'aria-label': `${n.label} ${n.sub}`,
    }, [
      svgEl('rect', { x: n.x, y: n.y, width: n.w, height: n.h, rx: 6, class: 'cz-sankey-rect' }),
      svgEl('text', { x: n.x + 10, y: n.y + Math.min(n.h / 2, 18), class: 'cz-sankey-label' }, [clip(n.label, 22)]),
      n.sub ? svgEl('text', { x: n.x + 10, y: n.y + Math.min(n.h / 2, 18) + 14, class: 'cz-sankey-sub' }, [clip(n.sub, 24)]) : null,
    ]);
    grp.addEventListener('mouseenter', () => lightFlow(ribbonLayer, nodeLayer, n.id));
    grp.addEventListener('mouseleave', () => unlightFlow(ribbonLayer, nodeLayer));
    if (n.ref && n.ref.kind === 'patch') {
      grp.classList.add('cz-clickable');
      grp.addEventListener('click', () => openDiff(chrome, epochId, genId, n.ref.path));
      grp.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') openDiff(chrome, epochId, genId, n.ref.path); });
    }
    nodeLayer.appendChild(grp);
  }
  vp.appendChild(nodeLayer);

  surface.fit(layout.box);

  return el('div', { class: 'cz-canvas-wrap' }, [
    createSurfaceControls(surface, el),
    surface.svg,
  ]);
}

function lightFlow(ribbonLayer, nodeLayer, nodeId) {
  ribbonLayer.classList.add('cz-dimmed');
  for (const r of ribbonLayer.children) {
    if (r.getAttribute('data-source') === nodeId || r.getAttribute('data-target') === nodeId) {
      r.classList.add('cz-lit');
    }
  }
}
function unlightFlow(ribbonLayer) {
  ribbonLayer.classList.remove('cz-dimmed');
  for (const r of ribbonLayer.children) r.classList.remove('cz-lit');
}

// -- verdict strip ----------------------------------------------------
function renderVerdictStrip(exp, outcome) {
  const dec = decisionOf(exp) || 'running';
  const tiles = [];
  const tile = (label, v, goodNeg) => {
    const num = typeof v === 'number' && isFinite(v) ? v : null;
    let cls = 'cz-tile';
    if (num != null && num !== 0) cls += (goodNeg ? num < 0 : num > 0) ? ' cz-tile-good' : ' cz-tile-bad';
    return el('div', { class: cls }, [
      el('div', { class: 'cz-tile-label' }, [label]),
      el('div', { class: 'cz-tile-value cz-mono' }, [num == null ? '—' : fmtDelta(num)]),
    ]);
  };
  if (outcome) {
    tiles.push(tile('Δ scalar', outcome.scalar_score_delta, true));
    tiles.push(tile('Δ drift', outcome.drift_loss_delta, true));
    tiles.push(tile('Δ pass-rate', outcome.pass_rate_delta, false));
  }
  const reason = outcome && (outcome.rejection_reason || outcome.decision_reason);
  return el('div', { class: 'cz-verdict-strip ' + verdictClass(dec) }, [
    el('div', { class: 'cz-verdict-pill ' + verdictClass(dec) }, [verdictLabel(dec)]),
    el('div', { class: 'cz-tile-strip' }, tiles),
    reason ? el('div', { class: 'cz-verdict-reason' }, [String(reason)]) : null,
  ]);
}

// -- baseline (v0) — show board results as nodes ----------------------
function renderBaselineFlow(exp, epochId, genId, chrome) {
  void chrome;
  const wrap = el('div', { class: 'cz-baseline-flow' });
  wrap.appendChild(el('div', { class: 'cz-verdict-strip cz-v-baseline' }, [
    el('div', { class: 'cz-verdict-pill cz-v-baseline' }, ['BASELINE']),
    el('div', { class: 'cz-verdict-reason' }, [
      'The seed generation — no patch, no tournament. It defines the floor the first challenger must beat.',
    ]),
  ]));
  const entries = (exp.outcome && Array.isArray(exp.outcome.per_entry)) ? exp.outcome.per_entry
    : (Array.isArray(exp.per_entry) ? exp.per_entry : []);
  if (entries.length) {
    const grid = el('div', { class: 'cz-board-cluster' });
    for (const e of entries) {
      grid.appendChild(el('div', { class: 'cz-board-node' }, [
        el('div', { class: 'cz-board-node-head' }, [
          el('span', { class: 'cz-board-id cz-mono' }, [e.entry_id || '?']),
          el('span', { class: 'cz-board-weight' }, [e.drift_loss != null ? fmtScalar(e.drift_loss) : '—']),
        ]),
        el('div', { class: 'cz-board-kind' }, [e.pass_fail ? 'pass' : 'fail']),
      ]));
    }
    wrap.appendChild(grid);
  } else {
    wrap.appendChild(el('p', { class: 'cz-section-sub' }, [
      'Baseline board results appear here once the seed has been scored.',
    ]));
  }
  void epochId; void genId;
  return wrap;
}

// -- diff drawer ------------------------------------------------------
async function openDiff(chrome, epochId, genId, path) {
  const body = el('div', { class: 'cz-diff' });
  body.appendChild(el('div', { class: 'cz-diff-head' }, [
    el('span', { class: 'cz-diff-target cz-mono' }, [path || '(patch)']),
  ]));
  const slot = el('pre', { class: 'cz-diff-body cz-mono' }, ['Loading diff…']);
  body.appendChild(slot);
  openDrawer(chrome, 'Patch diff', body);

  const key = epochId + '/' + genId;
  if (_diffCache.has(key)) { paintDiff(slot, _diffCache.get(key), path); return; }
  try {
    const data = await fetchJson('/api/files/' + encodeURIComponent(epochId) + '/' + encodeURIComponent(genId) + '/diff');
    _diffCache.set(key, data);
    paintDiff(slot, data, path);
  } catch {
    slot.textContent = 'No diff available for this generation.';
  }
}

function paintDiff(slot, data, path) {
  while (slot.firstChild) slot.removeChild(slot.firstChild);
  let text = '';
  if (data) {
    if (typeof data.diff === 'string') text = data.diff;
    else if (Array.isArray(data.files)) {
      const hit = data.files.find((f) => f && (f.path === path || (f.path || '').includes(path)));
      text = (hit && (hit.diff || hit.unified)) || data.files.map((f) => (f.diff || f.unified || '')).join('\n');
    } else if (typeof data.unified === 'string') text = data.unified;
  }
  if (!text) { slot.textContent = 'No textual diff for this patch.'; return; }
  // Colourise +/- lines.
  for (const line of text.split('\n')) {
    let cls = 'cz-diff-line';
    if (line.startsWith('+') && !line.startsWith('+++')) cls += ' cz-diff-add';
    else if (line.startsWith('-') && !line.startsWith('---')) cls += ' cz-diff-del';
    else if (line.startsWith('@@')) cls += ' cz-diff-hunk';
    slot.appendChild(el('div', { class: cls }, [line || ' ']));
  }
}

// -- fetch helpers ----------------------------------------------------
async function ensureDrift(genId, repaint) {
  if (!genId || _driftCache.has(genId) || _driftLoading.has(genId)) return;
  _driftLoading.add(genId);
  try {
    const d = await fetchJson('/api/drift-movements/' + encodeURIComponent(genId));
    _driftCache.set(genId, d || { movements: [] });
  } catch {
    _driftCache.set(genId, { movements: [] });
  } finally {
    _driftLoading.delete(genId);
    if (typeof repaint === 'function') repaint();
  }
}

async function ensurePatches(epochId, genId, repaint) {
  if (!epochId || !genId) return;
  const key = epochId + '/' + genId;
  if (_patchCache.has(key) || _patchLoading.has(key)) return;
  _patchLoading.add(key);
  try {
    const d = await fetchJson('/api/files/' + encodeURIComponent(epochId) + '/' + encodeURIComponent(genId) + '/patches');
    _patchCache.set(key, d || { patches: [] });
  } catch {
    _patchCache.set(key, { patches: [] });
  } finally {
    _patchLoading.delete(key);
    if (typeof repaint === 'function') repaint();
  }
}

// -- small format helpers ---------------------------------------------
function prettyKind(k) {
  return String(k || '').replace(/_/g, ' ').toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}
function signed(v) { return (typeof v === 'number' && v > 0 ? '+' : '') + v; }
function shortPath(p) {
  const s = String(p || '');
  const parts = s.split(/[/.]/).filter(Boolean);
  return parts.length > 2 ? parts.slice(-2).join('.') : s;
}
function clip(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

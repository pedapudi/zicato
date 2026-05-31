// variants/C/views/lifecycle.js — Screen: the CANDIDATE LIFECYCLE (themes 1+2).
//
// One candidate's life as a left-to-right DAG:
//
//   PARENT ─▶ PATCH ─▶ [ board fan: one node per entry ] ─▶ AGGREGATE ─▶ GATE ─▶ TERMINAL
//   (lineage)  (the      (theme 2 — node size/colour = loss,      (Σ loss)  (verdict)  (crown / dead branch)
//              cause)     edge weight = contribution)
//
// The board fan (theme 2) is the *set* of entries the candidate faces,
// drawn as a column of nodes between the patch and the aggregate. Each
// node's radius + colour encodes its drift loss; the edge into the
// aggregate is weighted by that entry's contribution. Animated flow runs
// along the spine.
//
// Below the lifecycle, the LINEAGE DAG shows the family: v0 root → v1/v2
// children, the champion crowned. Click any lineage node to switch the
// lifecycle to that candidate.
//
// Data: /api/lineage (parent/children), the experiment's hypothesis +
// mutation points (state.epochDef.experiments), /api/generation/.../per-entry
// (the board fan + loss), /api/round/.../gate (the verdict climax).

import { el, svgEl, clearChildren } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { createSurface, createSurfaceControls } from '../diagram/surface.js';
import { verdictClass, verdictLabel, flowPath, edgeEl, layoutDag, normalize } from '../diagram/primitives.js';
import {
  experimentById, decisionOf, mutationPointsOf, isBaselineSeed,
  lineageNode, childrenOf, candidatesOf, perEntryRows, championOf,
} from '../model.js';
import { href } from '../router.js';
import { fmtScalar, fmtDelta } from '../../../core/format.js';

const _entryCache = new Map();   // key epoch/gen -> rows
const _entryLoading = new Set();
const _gateCache = new Map();    // key epoch/champ/chall -> gate
const _gateLoading = new Set();

export function resetLifecycleCaches() {
  _entryCache.clear(); _entryLoading.clear();
  _gateCache.clear(); _gateLoading.clear();
}

export function renderLifecycle(ctx) {
  const { stage, state, params, repaint, onNavigate } = ctx;
  const epochId = params.epochId || (state.epochDef && state.epochDef.epoch_id) || null;
  const genId = params.genId;
  clearChildren(stage);

  stage.appendChild(el('div', { class: 'cz-screen-head' }, [
    el('div', { class: 'cz-epoch-eyebrow' }, [
      'LIFECYCLE', el('span', { class: 'cz-mono' }, [genId || '—']),
      epochId ? el('span', { class: 'cz-tag cz-tag-open' }, [epochId]) : null,
    ]),
    el('h1', { class: 'cz-screen-title' }, ['Candidate lifecycle']),
    el('p', { class: 'cz-screen-sub' }, [
      'One candidate’s whole life as a flow: born from a parent, shaped by a patch, '
      + 'sent across the board (each entry a node, sized by its loss), aggregated, met at the gate, '
      + 'and recorded as a crowned champion or a dead branch.',
    ]),
  ]));

  const node = lineageNode(state, genId);
  if (!genId || !node) {
    stage.appendChild(el('div', { class: 'cz-empty' }, [
      genId ? `No lineage record for "${genId}".` : 'No candidate selected. Pick a node from the lineage DAG below or the Environment map.',
    ]));
    // Still show the lineage DAG so the screen is navigable.
    if (candidatesOf(state, epochId).length) stage.appendChild(buildLineageSection(state, epochId, genId, onNavigate));
    return;
  }

  ensureEntries(epochId, genId, repaint);
  const exp = experimentById(state, genId);
  const baseline = isBaselineSeed(exp) || (!node.parent);
  if (node.parent && !baseline) ensureGate(epochId, node.parent, genId, repaint);

  stage.appendChild(buildLifecycleSpine(state, epochId, genId, node, exp, baseline));
  stage.appendChild(buildLineageSection(state, epochId, genId, onNavigate));
}

// -- the lifecycle spine DAG ------------------------------------------
function buildLifecycleSpine(state, epochId, genId, node, exp, baseline) {
  const rows = _entryCache.get(key(epochId, genId)) || null;
  const loaded = rows != null;
  const entries = rows || [];
  const dec = baseline ? 'baseline' : (decisionOf(exp) || 'running');
  const mpts = mutationPointsOf(exp);
  const champId = node.parent || championOf(state, epochId);
  const gate = _gateCache.get(gkey(epochId, champId, genId)) || null;

  // Column x positions.
  const X = { parent: 70, patch: 250, board: 470, agg: 700, gate: 880, term: 1040 };
  const midY = 230;
  const surfaceW = 1140;
  const fanTop = 70;
  const fanBot = 400;
  const surface = createSurface({ width: surfaceW, height: 470, ariaLabel: 'Candidate lifecycle flow' });
  const vp = surface.viewport;

  // Column headers.
  const heads = [
    [X.parent, 'PARENT'], [X.patch, 'PATCH'], [X.board, 'BOARD'],
    [X.agg, 'AGGREGATE'], [X.gate, 'GATE'], [X.term, baseline ? 'SEED' : 'TERMINAL'],
  ];
  for (const [x, t] of heads) vp.appendChild(svgEl('text', { x, y: 28, class: 'cz-sankey-col-head' }, [t]));

  const edgeLayer = svgEl('g', { class: 'cz-edge-layer' });
  const nodeLayer = svgEl('g', { class: 'cz-node-layer' });

  // PARENT node (lineage origin).
  const parentLabel = node.parent || '∅ seed';
  rectNode(nodeLayer, X.parent, midY, 130, 50, parentLabel, baseline ? 'no parent' : 'champion', baseline ? 'cz-v-baseline' : 'cz-v-promoted');

  // PATCH node (the cause — mutation points touched).
  const patchSub = mpts.length ? mpts.length + ' mutation point' + (mpts.length === 1 ? '' : 's') : (baseline ? 'no patch (seed)' : 'patch');
  rectNode(nodeLayer, X.patch, midY, 140, 50, baseline ? 'seed snapshot' : 'PATCH', patchSub, baseline ? 'cz-v-baseline' : 'cz-flow-patch');
  edgeLayer.appendChild(edgeEl(flowPath(X.parent + 65, midY, X.patch - 70, midY), { cls: baseline ? 'cz-edge-promoted' : 'cz-edge-promoted', animated: true, width: 2.4 }));

  // BOARD fan (theme 2): one node per entry, radius+colour = loss.
  const losses = entries.map((e) => e.driftLoss);
  const norm = normalize(losses.filter((v) => v != null));
  const total = entries.reduce((a, e) => a + (e.driftLoss || 0), 0) || 1;
  const n = Math.max(1, entries.length);
  const step = entries.length > 1 ? (fanBot - fanTop) / (entries.length - 1) : 0;
  if (!loaded) {
    rectNode(nodeLayer, X.board, midY, 150, 44, 'loading board…', '', 'cz-v-neutral');
  } else if (entries.length === 0) {
    rectNode(nodeLayer, X.board, midY, 150, 44, 'no board entries', 'scored yet', 'cz-v-neutral');
  } else {
    entries.forEach((e, i) => {
      const y = entries.length > 1 ? fanTop + i * step : midY;
      const t = e.driftLoss == null ? 0.5 : norm(e.driftLoss);
      const r = 14 + t * 16; // bigger = more loss
      const lossCls = e.passFail === 1 ? 'cz-v-promoted' : (e.budgetExceeded ? 'cz-v-deferred' : 'cz-v-rejected');
      // patch → board entry
      edgeLayer.appendChild(edgeEl(flowPath(X.patch + 70, midY, X.board - r, y), { cls: 'cz-edge-rejected', width: 1.3 }));
      // board entry → aggregate, weighted by contribution
      const contrib = (e.driftLoss || 0) / total;
      const w = Math.max(1, contrib * 14);
      edgeLayer.appendChild(edgeEl(flowPath(X.board + r, y, X.agg - 60, midY), { cls: lossCls === 'cz-v-promoted' ? 'cz-edge-promoted' : 'cz-edge-rejected', width: w }));
      const g = svgEl('g', { class: 'cz-node cz-lc-board ' + lossCls, 'data-cz': 'lc-board-node', 'data-key': e.entryId, tabindex: '0', 'aria-label': `${e.entryId} loss ${e.driftLoss}` }, [
        svgEl('circle', { cx: X.board, cy: y, r, class: 'cz-node-disc' }),
        svgEl('text', { x: X.board, y: y - r - 6, class: 'cz-lc-board-label' }, [clip(e.entryId, 22)]),
        svgEl('text', { x: X.board, y: y + 4, class: 'cz-lc-board-loss cz-mono' }, [e.driftLoss == null ? '—' : fmtScalar(e.driftLoss)]),
      ]);
      nodeLayer.appendChild(g);
    });
  }

  // AGGREGATE node.
  const aggLoss = loaded && entries.length ? fmtScalar(total) : '—';
  rectNode(nodeLayer, X.agg, midY, 130, 56, 'Σ loss', aggLoss, 'cz-v-neutral');

  // GATE node (the verdict climax).
  const gateSub = gate ? (gate.decision || dec) : (baseline ? 'no gate (seed)' : 'gate');
  edgeLayer.appendChild(edgeEl(flowPath(X.agg + 65, midY, X.gate - 70, midY), { cls: verdictClass(dec) === 'cz-v-promoted' ? 'cz-edge-promoted' : 'cz-edge-rejected', animated: true, width: 2.4 }));
  rectNode(nodeLayer, X.gate, midY, 140, 56, baseline ? 'BASELINE' : 'GATE', gateSub, verdictClass(dec));

  // TERMINAL — crown (promoted) or dead branch (rejected).
  const promoted = dec === 'promoted' || (baseline && node.promoted === true);
  const termLabel = baseline ? 'seed' : (promoted ? '♛ promoted' : '✕ dead branch');
  const termCls = baseline ? 'cz-v-baseline' : (promoted ? 'cz-v-promoted' : 'cz-v-rejected');
  edgeLayer.appendChild(edgeEl(flowPath(X.gate + 70, midY, X.term - 60, midY), { cls: promoted ? 'cz-edge-promoted' : 'cz-edge-rejected', width: 2.4 }));
  rectNode(nodeLayer, X.term, midY, 130, 56, termLabel, baseline ? 'defines the floor' : (promoted ? 'new champion' : 'champion stands'), termCls);

  vp.appendChild(edgeLayer);
  vp.appendChild(nodeLayer);
  surface.fit({ x: 0, y: 0, w: surfaceW, h: 470 });

  const wrap = el('div', {}, [
    el('div', { class: 'cz-canvas-wrap' }, [createSurfaceControls(surface, el), surface.svg]),
  ]);
  // A verdict strip echoing the gate beneath the flow.
  if (!baseline) wrap.appendChild(buildGateStrip(dec, gate, exp));
  return wrap;
}

function buildGateStrip(dec, gate, exp) {
  const reason = (gate && gate.reason)
    || (exp && exp.outcome && (exp.outcome.rejection_reason || exp.outcome.decision_reason))
    || null;
  const delta = gate && typeof gate.delta_scalar === 'number' ? gate.delta_scalar : null;
  return el('div', { class: 'cz-verdict-strip ' + verdictClass(dec), 'data-cz': 'lc-verdict' }, [
    el('div', { class: 'cz-verdict-pill ' + verdictClass(dec) }, [verdictLabel(dec)]),
    delta != null ? el('div', { class: 'cz-tile' }, [
      el('div', { class: 'cz-tile-label' }, ['Δ scalar']),
      el('div', { class: 'cz-tile-value cz-mono' }, [fmtDelta(delta)]),
    ]) : null,
    reason ? el('div', { class: 'cz-verdict-reason' }, [String(reason)]) : null,
  ]);
}

// -- the lineage DAG (family tree) ------------------------------------
function buildLineageSection(state, epochId, currentGen, onNavigate) {
  const cands = candidatesOf(state, epochId);
  const wrap = el('div', {}, [
    el('h2', { class: 'cz-section-title' }, ['Lineage']),
    el('p', { class: 'cz-section-sub' }, [
      'The family tree: the seed root, its children, the champion crowned. Click a node to follow that candidate’s lifecycle.',
    ]),
  ]);
  if (cands.length === 0) {
    wrap.appendChild(el('div', { class: 'cz-empty' }, ['No generations in this epoch yet.']));
    return wrap;
  }
  const dag = layoutDag(cands.map((c) => ({ id: c.id, parent: c.parent })));
  const COL = 200; const ROWH = 90; const PAD = 70;
  const width = Math.max(PAD * 2 + (dag.maxCol + 1) * COL, 720);
  const height = PAD * 2 + Math.max(1, dag.maxRow) * ROWH;
  const surface = createSurface({ width, height: Math.max(height, 220), ariaLabel: 'Lineage DAG' });
  const vp = surface.viewport;
  const edgeLayer = svgEl('g', { class: 'cz-edge-layer' });
  const nodeLayer = svgEl('g', { class: 'cz-node-layer' });

  const posOf = new Map();
  for (const c of cands) {
    const p = dag.pos.get(c.id);
    const x = PAD + (p ? p.col : 0) * COL;
    const y = PAD + (p ? p.row : 0) * ROWH;
    posOf.set(c.id, { x, y });
  }
  for (const c of cands) {
    if (!c.parent) continue;
    const a = posOf.get(c.parent); const b = posOf.get(c.id);
    if (!a || !b) continue;
    const promoted = c.promoted === true;
    edgeLayer.appendChild(edgeEl(flowPath(a.x + 50, a.y, b.x - 50, b.y), {
      cls: promoted ? 'cz-edge-promoted' : 'cz-edge-rejected', width: promoted ? 2.4 : 1.5,
    }));
  }
  vp.appendChild(edgeLayer);

  for (const c of cands) {
    const { x, y } = posOf.get(c.id);
    const verdict = c.promoted === true ? 'promoted' : (c.promoted === false ? 'rejected' : 'running');
    const crown = c.promoted === true;
    const isCur = c.id === currentGen;
    const w = 100; const h = 48;
    const a = svgEl('a', {
      class: 'cz-node cz-node-gen ' + verdictClass(verdict) + (isCur ? ' cz-lc-current' : ''),
      href: href('lifecycle', { epochId, genId: c.id }),
      'data-cz': 'lc-lineage-node', 'data-key': c.id,
      'aria-label': `${c.id} — ${verdict}${crown ? ' champion' : ''}`,
    }, [
      svgEl('rect', { x: x - w / 2, y: y - h / 2, width: w, height: h, rx: 9, class: 'cz-node-box' }),
      crown ? svgEl('text', { x, y: y - h / 2 - 6, class: 'cz-lc-crown' }, ['♛']) : null,
      svgEl('text', { x, y: y - 4, class: 'cz-node-gid' }, [c.id]),
      svgEl('text', { x, y: y + 13, class: 'cz-node-verdict' }, [crown ? 'champion' : verdictLabel(verdict).toLowerCase()]),
    ]);
    nodeLayer.appendChild(a);
  }
  vp.appendChild(nodeLayer);
  surface.fit({ x: 0, y: 0, w: width, h: height });
  wrap.appendChild(el('div', { class: 'cz-canvas-wrap cz-canvas-short' }, [createSurfaceControls(surface, el), surface.svg]));
  void onNavigate;
  return wrap;
}

// -- helpers ----------------------------------------------------------
function rectNode(layer, cx, cy, w, h, label, sub, cls) {
  const g = svgEl('g', { class: 'cz-node cz-lc-step ' + (cls || ''), 'data-cz': 'lc-step' }, [
    svgEl('rect', { x: cx - w / 2, y: cy - h / 2, width: w, height: h, rx: 9, class: 'cz-node-box' }),
    svgEl('text', { x: cx, y: cy - (sub ? 6 : 0), class: 'cz-node-gid' }, [clip(label, 18)]),
    sub ? svgEl('text', { x: cx, y: cy + 13, class: 'cz-node-verdict' }, [clip(sub, 22)]) : null,
  ]);
  layer.appendChild(g);
  return g;
}

function key(e, g) { return e + '/' + g; }
function gkey(e, c, h) { return e + '/' + c + '/' + h; }
function clip(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

async function ensureEntries(epochId, genId, repaint) {
  if (!epochId || !genId) return;
  const k = key(epochId, genId);
  if (_entryCache.has(k) || _entryLoading.has(k)) return;
  _entryLoading.add(k);
  try {
    const d = await fetchJson('/api/generation/' + encodeURIComponent(epochId) + '/' + encodeURIComponent(genId) + '/per-entry');
    _entryCache.set(k, perEntryRows(d));
  } catch {
    _entryCache.set(k, []);
  } finally {
    _entryLoading.delete(k);
    if (typeof repaint === 'function') repaint();
  }
}

async function ensureGate(epochId, champId, challId, repaint) {
  if (!epochId || !champId || !challId) return;
  const k = gkey(epochId, champId, challId);
  if (_gateCache.has(k) || _gateLoading.has(k)) return;
  _gateLoading.add(k);
  try {
    const d = await fetchJson('/api/round/' + encodeURIComponent(epochId) + '/' + encodeURIComponent(champId) + '/' + encodeURIComponent(challId) + '/gate');
    _gateCache.set(k, d || {});
  } catch {
    _gateCache.set(k, {});
  } finally {
    _gateLoading.delete(k);
    if (typeof repaint === 'function') repaint();
  }
}

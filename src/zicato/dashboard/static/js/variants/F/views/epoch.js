// variants/F/views/epoch.js — Screen 2: the epoch.
//
// Three things, in priority order:
//   1. OBJECTIVE — prominent. The epoch's goal as the screen's headline.
//   2. PROPOSER BRIEF — the long/complex operator's brief to the
//      proposer, given a dedicated DRAWER (it can be pages long, so it
//      gets its own surface rather than crowding the graph).
//   3. The GAUNTLET as a clean lineage GRAPH — champion through-line
//      left→right, rejected challengers branching DOWN off their parent
//      with proper layered layout (no colliding lines), plus the board
//      entries as a node cluster.
//
// Data: state.epochDef (goal, brief, board, experiments) + lineage.

import { el, svgEl, clearChildren } from '../../../core/dom.js';
import { createSurface, createSurfaceControls } from '../diagram/surface.js';
import { verdictClass, verdictLabel, flowPath, edgeEl } from '../diagram/primitives.js';
import { experimentsOf, decisionOf, isBaselineSeed, scalarOf, liveGenId, goalForEpoch } from '../model.js';
import { href } from '../router.js';
import { openDrawer } from '../chrome.js';
import { fmtScalar } from '../../../core/format.js';

const SPINE_Y = 90;
const COL_W = 150;
const PAD_X = 80;
const BRANCH_DROP = 92;

let _lastDigest = null;

export function renderEpoch(ctx) {
  const { stage, state, chrome } = ctx;
  const epochId = ctx.params.epochId || (state.epochDef && state.epochDef.epoch_id) || null;

  const goal = goalForEpoch(state, epochId) || (state.epochDef && state.epochDef.goal) || null;
  const brief = state.epochDef && typeof state.epochDef.brief === 'string' ? state.epochDef.brief : '';

  // Digest gate: epoch id, contract loaded?, objective, per-experiment
  // (id, parent, decision, scalar), the live tip, board ids. No timestamps.
  const exps = experimentsOf(state);
  const digest = JSON.stringify({
    epochId, goal,
    loaded: !!(state.epochDef && state.epochDef.epoch_id === epochId),
    closed: !!(state.epochDef && state.epochDef.closed),
    live: liveGenId(state),
    exps: exps.map((e) => [e.generation_id, e.parent_generation_id || null, decisionOf(e), scalarOf(e)]),
    board: (state.epochDef && Array.isArray(state.epochDef.board))
      ? state.epochDef.board.map((b) => b.entry_id || b.id) : [],
    hasBrief: !!(brief && brief.trim()),
  });
  if (digest === _lastDigest && stage.firstChild) return;
  _lastDigest = digest;

  clearChildren(stage);

  // -- OBJECTIVE: the headline ----------------------------------------
  const head = el('div', { class: 'cz-epoch-head' }, [
    el('div', { class: 'cz-epoch-eyebrow' }, [
      'EPOCH', el('span', { class: 'cz-mono' }, [epochId || '—']),
      state.epochDef && state.epochDef.closed ? el('span', { class: 'cz-tag cz-tag-closed' }, ['closed']) : el('span', { class: 'cz-tag cz-tag-open' }, ['open']),
    ]),
    el('h1', { class: 'cz-objective' }, [goal || 'No objective recorded for this epoch.']),
    el('div', { class: 'cz-epoch-actions' }, [
      briefButton(chrome, epochId, brief),
      el('a', { class: 'cz-btn cz-btn-ghost', href: href('tournament', { epochId }) }, ['Open gauntlet →']),
    ]),
  ]);
  stage.appendChild(head);

  if (!state.epochDef || state.epochDef.epoch_id !== epochId) {
    // Contract not loaded for this epoch yet.
    stage.appendChild(el('div', { class: 'cz-empty' }, [
      epochId ? 'Loading epoch contract…' : 'No epoch selected. Pick a lane on the Environment map.',
    ]));
    return;
  }

  // -- the GAUNTLET graph ---------------------------------------------
  stage.appendChild(el('h2', { class: 'cz-section-title' }, ['Lineage gauntlet']));
  stage.appendChild(el('p', { class: 'cz-section-sub' }, [
    'Champion through-line runs left to right; rejected challengers branch down off the parent they failed against. Click any node for its causal flow.',
  ]));
  stage.appendChild(buildGauntletGraph(state, epochId));

  // -- BOARD entries as a node cluster --------------------------------
  stage.appendChild(el('h2', { class: 'cz-section-title' }, ['Board']));
  stage.appendChild(buildBoardCluster(state));
}

function briefButton(chrome, epochId, brief) {
  const has = brief && brief.trim().length > 0;
  return el('button', {
    type: 'button',
    class: 'cz-btn cz-btn-primary',
    'data-cz': 'brief-btn',
    onclick: () => openBriefDrawer(chrome, epochId, brief),
  }, [has ? '📋 Proposer brief' : '📋 Proposer brief (none)']);
}

function openBriefDrawer(chrome, epochId, brief) {
  const body = el('div', { class: 'cz-brief' });
  body.appendChild(el('p', { class: 'cz-brief-meta' }, [
    'The operator\'s brief to the proposer for epoch ', el('span', { class: 'cz-mono' }, [epochId || '—']),
    '. Frozen for the epoch; drives every challenger proposed within it.',
  ]));
  if (brief && brief.trim()) {
    // Render as readable prose blocks — split on blank lines, headings
    // (## ...) get emphasised. Plain text, no HTML injection.
    const blocks = brief.replace(/\r\n/g, '\n').split(/\n{2,}/);
    for (const b of blocks) {
      const t = b.trim();
      if (!t) continue;
      const headingMatch = t.match(/^(#{1,4})\s+(.*)$/);
      if (headingMatch) {
        body.appendChild(el('h3', { class: 'cz-brief-h' }, [headingMatch[2]]));
        const rest = t.split('\n').slice(1).join('\n').trim();
        if (rest) body.appendChild(el('p', { class: 'cz-brief-p' }, [rest]));
      } else {
        body.appendChild(el('p', { class: 'cz-brief-p' }, [t]));
      }
    }
  } else {
    body.appendChild(el('p', { class: 'cz-empty cz-empty-inline' }, [
      'No proposer brief recorded for this epoch. Set one with ',
      el('code', { class: 'cz-mono' }, ['zicato epoch ... --brief brief.md']),
      ' or push a replacement from the control surface.',
    ]));
  }
  openDrawer(chrome, 'Proposer brief', body);
}

function buildGauntletGraph(state, epochId) {
  const exps = experimentsOf(state);
  const live = liveGenId(state);

  // Index experiments and build the champion spine + branches.
  const byId = new Map();
  for (const e of exps) if (e && e.generation_id) byId.set(e.generation_id, e);

  // The spine = promoted (or baseline) generations in order; each
  // rejected challenger hangs off the champion that was reigning when it
  // ran (its parent_generation_id).
  const nodes = exps.map((e) => {
    const id = e.generation_id;
    const dec = decisionOf(e);
    const baseline = isBaselineSeed(e);
    const onSpine = baseline || dec === 'promoted';
    return { id, exp: e, dec, baseline, onSpine, parent: e.parent_generation_id || null, scalar: scalarOf(e) };
  });
  // Inject a live (in-flight) node if not present. An in-flight
  // challenger renders as the RUNNING tip on the spine — it is the
  // current contender, not a settled branch.
  let liveTipNode = null;
  if (live && !nodes.find((n) => n.id === live)) {
    liveTipNode = { id: live, exp: null, dec: null, baseline: false, onSpine: true, parent: null, scalar: null, live: true, isTip: true };
    nodes.push(liveTipNode);
  } else if (live) {
    const n = nodes.find((x) => x.id === live);
    if (n) n.live = true;
  }

  if (nodes.length === 0) {
    return el('div', { class: 'cz-empty' }, ['No generations yet in this epoch.']);
  }

  // The tip (if any) is always last on the spine.
  const spine = nodes.filter((n) => n.onSpine && !n.isTip);
  if (liveTipNode) spine.push(liveTipNode);
  const branches = nodes.filter((n) => !n.onSpine);

  // Spine x positions, left to right by order in `spine`.
  const xOf = new Map();
  spine.forEach((n, i) => xOf.set(n.id, PAD_X + i * COL_W));

  // Group branches by the spine node they hang off.
  const branchesByParent = new Map();
  for (const b of branches) {
    const key = b.parent && xOf.has(b.parent) ? b.parent : (spine.length ? spine[spine.length - 1].id : null);
    if (!branchesByParent.has(key)) branchesByParent.set(key, []);
    branchesByParent.get(key).push(b);
  }

  const width = Math.max(PAD_X + spine.length * COL_W + COL_W, 960);
  const maxBranchDepth = Math.max(0, ...[...branchesByParent.values()].map((a) => a.length));
  const height = SPINE_Y + 60 + maxBranchDepth * BRANCH_DROP;
  const surface = createSurface({ width, height: Math.max(height, 220), ariaLabel: 'Epoch lineage gauntlet' });
  const vp = surface.viewport;

  const edgeLayer = svgEl('g', { class: 'cz-edge-layer' });
  const nodeLayer = svgEl('g', { class: 'cz-node-layer' });

  // Spine edges (promoted through-line); the edge INTO a live tip is the
  // running flow (animated), not a settled promotion.
  for (let i = 1; i < spine.length; i++) {
    const a = xOf.get(spine[i - 1].id);
    const b = xOf.get(spine[i].id);
    const tip = spine[i].live;
    edgeLayer.appendChild(edgeEl(flowPath(a + 22, SPINE_Y, b - 22, SPINE_Y), {
      cls: tip ? 'cz-edge-running' : 'cz-edge-promoted', animated: !!tip, width: tip ? 2.5 : 3,
    }));
  }

  // Branch edges (rejected challengers drop down).
  for (const [parentId, list] of branchesByParent) {
    const px = parentId != null ? xOf.get(parentId) : null;
    if (px == null) continue;
    list.forEach((b, j) => {
      const by = SPINE_Y + (j + 1) * BRANCH_DROP;
      const bx = px + COL_W * 0.6;
      xOf.set(b.id, bx);
      const cls = b.live ? 'cz-edge-running' : 'cz-edge-rejected';
      edgeLayer.appendChild(edgeEl(flowPath(px, SPINE_Y + 18, bx, by - 18), { cls, animated: !!b.live, width: 1.6 }));
      // store y for node placement
      b._y = by;
    });
  }

  vp.appendChild(edgeLayer);

  // Spine nodes (including the live tip).
  for (const n of spine) {
    nodeLayer.appendChild(genNode(n, xOf.get(n.id), SPINE_Y, epochId, true));
  }
  // Branch nodes.
  for (const list of branchesByParent.values()) {
    for (const b of list) {
      nodeLayer.appendChild(genNode(b, xOf.get(b.id), b._y || SPINE_Y + BRANCH_DROP, epochId, false));
    }
  }

  vp.appendChild(nodeLayer);
  surface.fit({ x: 0, y: 0, w: width, h: height });

  return el('div', { class: 'cz-canvas-wrap cz-canvas-short' }, [
    createSurfaceControls(surface, el),
    surface.svg,
  ]);
}

function genNode(n, x, y, epochId, onSpine) {
  const verdict = n.live ? 'running' : (n.baseline ? 'baseline' : (n.dec || 'running'));
  const w = 96; const h = 44;
  const grp = svgEl('a', {
    class: 'cz-node cz-node-gen ' + verdictClass(verdict) + (onSpine ? ' cz-node-spine' : ''),
    href: href('experiment', { epochId, genId: n.id }),
    'data-key': n.id,
    'data-cz': 'gen-node',
    'data-spine': onSpine ? '1' : '0',
    'aria-label': `${n.id} — ${verdictLabel(verdict)}`,
  }, [
    svgEl('rect', { x: x - w / 2, y: y - h / 2, width: w, height: h, rx: 9, class: 'cz-node-box' }),
    n.live ? svgEl('rect', { x: x - w / 2 - 4, y: y - h / 2 - 4, width: w + 8, height: h + 8, rx: 11, class: 'cz-node-pulse-box' }) : null,
    svgEl('text', { x, y: y - 3, class: 'cz-node-gid' }, [n.id]),
    svgEl('text', { x, y: y + 13, class: 'cz-node-verdict' }, [
      n.scalar != null ? fmtScalar(n.scalar) : verdictLabel(verdict).toLowerCase(),
    ]),
  ]);
  return grp;
}

function buildBoardCluster(state) {
  const def = state.epochDef || {};
  const board = Array.isArray(def.board) ? def.board : [];
  if (board.length === 0) {
    return el('div', { class: 'cz-empty' }, ['No board entries recorded for this epoch.']);
  }
  const grid = el('div', { class: 'cz-board-cluster' });
  for (const entry of board) {
    const id = entry.entry_id || entry.id || '?';
    const weight = entry.weight != null ? entry.weight : 1;
    const preview = entry.input_preview || entry.goal || '';
    const kind = entry.kind || (entry.turns ? 'multi-turn' : 'single');
    grid.appendChild(el('div', { class: 'cz-board-node', 'data-key': id, title: preview }, [
      el('div', { class: 'cz-board-node-head' }, [
        el('span', { class: 'cz-board-id cz-mono' }, [id]),
        el('span', { class: 'cz-board-weight' }, ['×' + weight]),
      ]),
      el('div', { class: 'cz-board-kind' }, [kind]),
      preview ? el('div', { class: 'cz-board-preview' }, [clip(preview, 120)]) : null,
    ]));
  }
  return grid;
}

function clip(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

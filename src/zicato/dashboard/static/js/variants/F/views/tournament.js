// variants/F/views/tournament.js — Screen 4: the gauntlet bracket.
//
// The king-of-the-hill gauntlet as a crisp flow graph: the champion
// spine left→right, each round's challenger branching off, the verdict
// (PROMOTE / DISCARD) and score delta on every matchup. Non-colliding
// by construction (champion on one row, challengers on their own rows).
//
// This shares the lineage-graph language with the Epoch screen but is
// rendered standalone here, and it also surfaces the LIVE active
// tournament (in-flight matchup) at the tip when one is running.

import { el, svgEl, clearChildren } from '../../../core/dom.js';
import { createSurface, createSurfaceControls } from '../diagram/surface.js';
import { verdictClass, verdictLabel, flowPath, edgeEl } from '../diagram/primitives.js';
import { experimentsOf, decisionOf, isBaselineSeed, liveGenId } from '../model.js';
import { href } from '../router.js';
import { fmtDelta } from '../../../core/format.js';

const SPINE_Y = 100;
const COL_W = 168;
const PAD_X = 90;
const DROP = 96;

let _lastDigest = null;

export function renderTournament(ctx) {
  const { stage, state, params } = ctx;
  const epochId = params.epochId || (state.epochDef && state.epochDef.epoch_id) || null;

  // Digest gate: experiments (id/parent/decision/delta), live tip, and the
  // active matchup's per-entry STATUS (not its churning progress %). No
  // timestamps — a heartbeat that only re-stamps a clock is a no-op.
  const exps = experimentsOf(state);
  const at = state.activeTournament;
  const atEntries = at && Array.isArray(at.entries) ? at.entries
    : (at && Array.isArray(at.board) ? at.board : []);
  const digest = JSON.stringify({
    epochId,
    live: liveGenId(state),
    exps: exps.map((e) => [e.generation_id, e.parent_generation_id || null, decisionOf(e),
      e.outcome ? e.outcome.scalar_score_delta : null]),
    matchup: at ? [at.champion_id || at.champion, at.challenger_id || at.challenger,
      atEntries.map((e) => [e.entry_id || e.id, (e.status || e.phase || (e.done ? 'done' : 'queued'))])] : null,
  });
  if (digest === _lastDigest && stage.firstChild) return;
  _lastDigest = digest;

  clearChildren(stage);

  stage.appendChild(el('div', { class: 'cz-screen-head' }, [
    el('div', { class: 'cz-epoch-eyebrow' }, ['TOURNAMENT', el('span', { class: 'cz-mono' }, [epochId || '—'])]),
    el('h1', { class: 'cz-screen-title' }, ['The gauntlet']),
    el('p', { class: 'cz-screen-sub' }, [
      'King-of-the-hill: one champion per epoch, one challenger per round. '
      + 'The spine is the champion through-line; discarded challengers branch off the champion they failed against.',
    ]),
  ]));

  if (exps.length === 0) {
    stage.appendChild(el('div', { class: 'cz-empty' }, ['No rounds recorded yet for this epoch.']));
    return;
  }

  stage.appendChild(buildBracket(state, epochId));
  stage.appendChild(buildLiveMatchup(state, epochId));
}

function buildBracket(state, epochId) {
  const exps = experimentsOf(state);
  const live = liveGenId(state);
  const nodes = exps.map((e) => {
    const dec = decisionOf(e);
    const baseline = isBaselineSeed(e);
    return {
      id: e.generation_id, exp: e, dec, baseline,
      onSpine: baseline || dec === 'promoted',
      parent: e.parent_generation_id || null,
      delta: e.outcome ? e.outcome.scalar_score_delta : null,
    };
  });

  const spine = nodes.filter((n) => n.onSpine);
  const branches = nodes.filter((n) => !n.onSpine);
  const xOf = new Map();
  spine.forEach((n, i) => xOf.set(n.id, PAD_X + i * COL_W));
  let tipX = PAD_X + spine.length * COL_W;

  const branchesByParent = new Map();
  for (const b of branches) {
    const key = b.parent && xOf.has(b.parent) ? b.parent : (spine.length ? spine[spine.length - 1].id : null);
    if (!branchesByParent.has(key)) branchesByParent.set(key, []);
    branchesByParent.get(key).push(b);
  }
  const maxDepth = Math.max(0, ...[...branchesByParent.values()].map((a) => a.length));
  const liveTip = live && !xOf.has(live) && !branches.find((b) => b.id === live);

  const width = Math.max(tipX + COL_W, 960);
  const height = SPINE_Y + 80 + maxDepth * DROP;
  const surface = createSurface({ width, height: Math.max(height, 240), ariaLabel: 'Tournament gauntlet bracket' });
  const vp = surface.viewport;
  const edgeLayer = svgEl('g', { class: 'cz-edge-layer' });
  const nodeLayer = svgEl('g', { class: 'cz-node-layer' });

  for (let i = 1; i < spine.length; i++) {
    const a = xOf.get(spine[i - 1].id);
    const b = xOf.get(spine[i].id);
    edgeLayer.appendChild(edgeEl(flowPath(a + 26, SPINE_Y, b - 26, SPINE_Y), { cls: 'cz-edge-promoted', width: 3 }));
  }
  if (liveTip && spine.length) {
    const a = xOf.get(spine[spine.length - 1].id);
    edgeLayer.appendChild(edgeEl(flowPath(a + 26, SPINE_Y, tipX - 26, SPINE_Y), { cls: 'cz-edge-running', animated: true, width: 2.5 }));
    xOf.set(live, tipX);
  }
  for (const [parentId, list] of branchesByParent) {
    const px = xOf.get(parentId);
    if (px == null) continue;
    list.forEach((b, j) => {
      const by = SPINE_Y + (j + 1) * DROP;
      const bx = px + COL_W * 0.55;
      xOf.set(b.id, bx);
      b._y = by;
      edgeLayer.appendChild(edgeEl(flowPath(px, SPINE_Y + 20, bx, by - 20), { cls: 'cz-edge-rejected', width: 1.6 }));
    });
  }
  vp.appendChild(edgeLayer);

  for (const n of spine) vp_node(nodeLayer, n, xOf.get(n.id), SPINE_Y, epochId, true);
  if (liveTip) {
    vp_node(nodeLayer, { id: live, dec: null, live: true, delta: null }, tipX, SPINE_Y, epochId, true);
  }
  for (const list of branchesByParent.values()) {
    for (const b of list) vp_node(nodeLayer, b, xOf.get(b.id), b._y || SPINE_Y + DROP, epochId, false);
  }
  vp.appendChild(nodeLayer);
  surface.fit({ x: 0, y: 0, w: width, h: height });

  return el('div', { class: 'cz-canvas-wrap' }, [createSurfaceControls(surface, el), surface.svg]);
}

function vp_node(layer, n, x, y, epochId, onSpine) {
  const verdict = n.live ? 'running' : (n.baseline ? 'baseline' : (n.dec || 'running'));
  const w = 112; const h = 52;
  const node = svgEl('a', {
    class: 'cz-node cz-node-match ' + verdictClass(verdict) + (onSpine ? ' cz-node-spine' : ''),
    href: href('experiment', { epochId, genId: n.id }),
    'data-key': n.id,
    'aria-label': `${n.id} — ${verdictLabel(verdict)}`,
  }, [
    svgEl('rect', { x: x - w / 2, y: y - h / 2, width: w, height: h, rx: 9, class: 'cz-node-box' }),
    n.live ? svgEl('rect', { x: x - w / 2 - 4, y: y - h / 2 - 4, width: w + 8, height: h + 8, rx: 11, class: 'cz-node-pulse-box' }) : null,
    svgEl('text', { x, y: y - 8, class: 'cz-node-gid' }, [n.id]),
    svgEl('text', { x, y: y + 8, class: 'cz-node-verdict' }, [n.live ? 'in flight' : verdictLabel(verdict)]),
    (n.delta != null) ? svgEl('text', { x, y: y + 21, class: 'cz-node-delta cz-mono' }, [fmtDelta(n.delta)]) : null,
  ]);
  layer.appendChild(node);
}

// The live in-flight matchup, read from active_tournament (state.activeTournament).
function buildLiveMatchup(state, epochId) {
  const at = state.activeTournament;
  if (!at) return el('div', { class: 'cz-section-sub', style: 'margin-top:var(--v2-space-4,16px)' }, [
    'No tournament running right now.',
  ]);
  const entries = Array.isArray(at.entries) ? at.entries : (Array.isArray(at.board) ? at.board : []);
  const wrap = el('div', { class: 'cz-live-matchup' });
  wrap.appendChild(el('h2', { class: 'cz-section-title' }, [
    'Active matchup ',
    el('span', { class: 'cz-live-dot' }),
  ]));
  const champ = at.champion_id || at.parent || at.champion || '—';
  const chall = at.challenger_id || at.child || at.challenger || '—';
  wrap.appendChild(el('div', { class: 'cz-vs' }, [
    el('span', { class: 'cz-vs-champ cz-mono' }, [String(champ)]),
    el('span', { class: 'cz-vs-x' }, ['vs']),
    el('span', { class: 'cz-vs-chall cz-mono' }, [String(chall)]),
    epochId ? el('a', { class: 'cz-btn cz-btn-ghost', href: href('experiment', { epochId, genId: String(chall) }) }, ['flow →']) : null,
  ]));
  if (entries.length) {
    const grid = el('div', { class: 'cz-board-cluster' });
    for (const e of entries) {
      const status = (e.status || e.phase || (e.done ? 'done' : 'queued')).toString();
      grid.appendChild(el('div', { class: 'cz-board-node cz-board-node-live', 'data-status': status }, [
        el('div', { class: 'cz-board-node-head' }, [
          el('span', { class: 'cz-board-id cz-mono' }, [e.entry_id || e.id || '?']),
        ]),
        el('div', { class: 'cz-board-kind' }, [status]),
      ]));
    }
    wrap.appendChild(grid);
  }
  return wrap;
}

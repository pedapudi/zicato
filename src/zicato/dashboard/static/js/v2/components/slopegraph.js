// js/v2/components/slopegraph.js — the tournament & promotion as a Tufte
// slopegraph / bumps chart. DASHBOARD-V2 §3 (the corrected visual
// language): the tournament IS a slopegraph, graphical and interactive —
// NOT a table. This is the headline visual of v2.
//
//   * x = ROUND (left → right). y = SCALAR (loss). LOWER loss sits HIGHER
//     on screen, so the promoted lineage literally traces the descent.
//   * The CHAMPION is the bold through-line connecting the reigning
//     generation across rounds.
//   * Each CHALLENGER is a slope from the champion's value to the
//     challenger's value at that round:
//       - PROMOTE → the slope is GREEN and the challenger JOINS the
//         champion through-line (it becomes the next champion node). One
//         glance: the line bends to the challenger's lower loss.
//       - REJECT → the slope is RED and ends in a DETACHED, FADED node
//         that does NOT join the line (it falls away). The champion line
//         continues flat at the champion's value into the next round.
//       - RUNNING → AMBER, DASHED, pulsing (CSS, prefers-reduced-motion
//         gated) — the in-flight matchup.
//   * Tufte endpoint labels (gen id + scalar) at the slope ends; a
//     verdict glyph at each matchup.
//   * Interactive: hover a slope/node → tooltip (verdict · Δscalar ·
//     fired gate rule); click a matchup slope → onMatchup(challengerId);
//     click a node → onGeneration(id).
//
// Honest at 0 / 1 / few rounds (no empty/NaN SVG). Re-render-safe: the
// factory returns a fresh detached node every call; the view mounts it
// behind a digest gate so SSE heartbeats do not flash it.
//
// Color comes from --v2-* tokens ONLY (works across all three themes) —
// never a hard-coded hex.
//
//   slopegraph({ rounds, live, onMatchup, onGeneration })
//     rounds — ordered [{
//       round,            // 0-based round index (x position)
//       champion:   { id, scalar },   // the reigning generation this round
//       challenger: { id, scalar },   // the contender
//       decision,         // 'promoted' | 'rejected' | 'running'
//       deltaScalar,      // challenger.scalar - champion.scalar (signed)
//       firedRule,        // the gate rule that fired (rejects), or null
//     }]
//     live    — optional bool; when true the LAST round is treated as the
//               in-flight matchup even if its decision is not 'running'
//               (defensive — the caller normally sets decision:'running').
//     onMatchup    — (challengerId) => void   (click a slope)
//     onGeneration — (generationId) => void   (click a node)

import { el, svgEl } from '../../core/dom.js';
import { fmtScalar, fmtDelta } from '../../core/format.js';

// ---------------------------------------------------------------------------
// Geometry — every coordinate derives from these so one tweak rebalances
// the whole picture. Exported (frozen) so a regression cannot silently
// slide the slopes out of place.
// ---------------------------------------------------------------------------
const COL_W = 132;       // px between adjacent round columns
const PAD_X = 96;        // horizontal padding (room for the endpoint labels)
const PAD_TOP = 34;      // top of the scalar band
const PAD_BOTTOM = 40;   // bottom padding (round-axis labels)
const PLOT_H = 168;      // height of the scalar band itself
const DOT_R = 7;         // champion / challenger node radius
const HEIGHT = PAD_TOP + PLOT_H + PAD_BOTTOM;

export const SLOPEGRAPH_GEOMETRY = Object.freeze({
  COL_W, PAD_X, PAD_TOP, PAD_BOTTOM, PLOT_H, DOT_R, HEIGHT,
});

const DECISIONS = new Set(['promoted', 'rejected', 'running']);

function normDecision(raw) {
  const d = String(raw == null ? '' : raw).toLowerCase();
  if (d.startsWith('prom') || d === 'accepted') return 'promoted';
  if (d.startsWith('rej')) return 'rejected';
  if (d.startsWith('run') || d === 'in_flight' || d === 'live') return 'running';
  return DECISIONS.has(d) ? d : 'running';
}

function finite(v) {
  return (typeof v === 'number' && isFinite(v)) ? v : null;
}

const VERDICT_GLYPH = { promoted: '✓', rejected: '✗', running: '◇' };

// ---------------------------------------------------------------------------
// Layout — pure geometry, no DOM. Exported so the promote-joins /
// reject-falls-away invariants are unit-testable without a DOM.
//
// Returns { mode, ... }:
//   mode 'empty' → no rounds at all (a labeled empty state).
//   mode 'plot'  → { columns, matchups, championPath, championPathLive,
//                    width, height, scalarDomain }
//
// columns:  one x per round (round-axis labels).
// matchups: one per round — { round, x, decision, champion:{id,scalar,y},
//           challenger:{id,scalar,y,joins}, deltaScalar, firedRule,
//           slopePath }. `challenger.joins` is true ONLY on a promote
//           (the node sits ON the through-line for the next column);
//           false on reject/running (it falls away / is in flight).
//
// The champion through-line walks left→right: at round r the line sits at
// the champion's y; on a PROMOTE it descends to the challenger's y for
// round r+1 (the challenger joined); on a REJECT it stays at the
// champion's y. The final hop into a RUNNING matchup is split into
// championPathLive so the live segment can render dashed/amber.
// ---------------------------------------------------------------------------
export function computeSlopegraphLayout(rounds, opts) {
  const o = opts || {};
  const list = Array.isArray(rounds) ? rounds.filter((r) => r && typeof r === 'object') : [];
  if (list.length === 0) return { mode: 'empty' };

  // Normalize each round into champion/challenger scalars + decision. The
  // last round is forced running when `live` is set (defensive).
  const norm = list.map((r, i) => {
    const champ = r.champion || {};
    const chal = r.challenger || {};
    let decision = normDecision(r.decision);
    if (o.live && i === list.length - 1 && decision !== 'running') decision = 'running';
    return {
      round: Number.isFinite(r.round) ? r.round : i,
      index: i,
      decision,
      championId: champ.id != null ? String(champ.id) : '?',
      challengerId: chal.id != null ? String(chal.id) : '?',
      championScalar: finite(champ.scalar),
      challengerScalar: finite(chal.scalar),
      deltaScalar: finite(r.deltaScalar),
      firedRule: (typeof r.firedRule === 'string' && r.firedRule.trim()) ? r.firedRule.trim() : null,
    };
  });

  // Scalar domain across every finite champion/challenger value.
  const scalars = [];
  for (const n of norm) {
    if (n.championScalar != null) scalars.push(n.championScalar);
    if (n.challengerScalar != null) scalars.push(n.challengerScalar);
  }
  const sMin = scalars.length ? Math.min(...scalars) : 0;
  const sMax = scalars.length ? Math.max(...scalars) : 1;
  // A degenerate (flat or empty) domain still draws — every node lands on
  // the mid-line rather than producing a NaN coordinate.
  const degenerate = !(sMax > sMin);
  const sRange = degenerate ? 1 : (sMax - sMin);
  const midY = PAD_TOP + PLOT_H / 2;
  const yFor = (s) => {
    if (s == null) return midY;
    if (degenerate) return midY;
    // Lower loss → higher on screen (smaller y).
    return PAD_TOP + ((s - sMin) / sRange) * PLOT_H;
  };

  const xFor = (i) => PAD_X + i * COL_W;

  const columns = [];
  const matchups = [];
  for (const n of norm) {
    const x = xFor(n.index);
    columns.push({ round: n.round, index: n.index, x, decision: n.decision });
    const cy = yFor(n.championScalar);
    const gy = yFor(n.challengerScalar);
    const joins = n.decision === 'promoted';
    // The slope from the champion value down/over to the challenger value.
    // We draw it as a short diagonal within the column so the "slope" Tufte
    // reads as a tilt of the matchup, anchored on the through-line on the
    // left and the challenger node on the right.
    const x0 = x;
    const x1 = x + COL_W * 0.62;
    const slopePath = `M ${x0} ${cy} L ${x1} ${gy}`;
    matchups.push({
      round: n.round,
      index: n.index,
      x,
      slopeX0: x0,
      slopeX1: x1,
      decision: n.decision,
      deltaScalar: n.deltaScalar,
      firedRule: n.firedRule,
      champion: { id: n.championId, scalar: n.championScalar, y: cy },
      challenger: { id: n.challengerId, scalar: n.challengerScalar, y: gy, joins },
      slopePath,
    });
  }

  // The champion through-line. Start at the first champion node; at each
  // round, if the challenger was promoted, descend to its value for the
  // next round; otherwise hold the champion value. The final hop into a
  // running matchup is split into the live segment.
  let championPath = '';
  let championPathLive = '';
  for (let i = 0; i < matchups.length; i += 1) {
    const m = matchups[i];
    const next = matchups[i + 1];
    // Horizontal hold across the column up to the matchup's slope origin is
    // implicit (the champion node sits at x). The segment that carries the
    // line into the NEXT column is what we draw.
    if (!next) {
      // Trailing segment for the last matchup: from champion node toward the
      // winning endpoint when promoted, to visualize the line continuing.
      if (m.decision === 'promoted') {
        const seg = `M ${m.x} ${m.champion.y} L ${m.slopeX1} ${m.challenger.y}`;
        championPath += (championPath ? ' ' : '') + seg;
      }
      continue;
    }
    // The y the line carries into round i+1: the promoted challenger's y,
    // else the held champion y.
    const carryY = m.decision === 'promoted' ? m.challenger.y : m.champion.y;
    const seg = `M ${m.x} ${m.champion.y} L ${next.x} ${carryY}`;
    if (next.decision === 'running') championPathLive += (championPathLive ? ' ' : '') + seg;
    else championPath += (championPath ? ' ' : '') + seg;
  }

  const width = PAD_X * 2 + (matchups.length - 1) * COL_W + COL_W * 0.62;
  return {
    mode: 'plot',
    columns,
    matchups,
    championPath,
    championPathLive,
    width: Math.max(width, PAD_X * 2 + COL_W),
    height: HEIGHT,
    scalarDomain: { min: sMin, max: sMax, degenerate },
  };
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------
export function slopegraph(opts) {
  const o = opts || {};
  const onMatchup = typeof o.onMatchup === 'function' ? o.onMatchup : null;
  const onGeneration = typeof o.onGeneration === 'function' ? o.onGeneration : null;
  const layout = computeSlopegraphLayout(o.rounds, { live: o.live });

  if (layout.mode === 'empty') {
    return el('div', {
      class: 'v2-slope v2-slope-empty', 'data-mode': 'empty',
    }, [
      el('span', { class: 'v2-state-glyph', 'aria-hidden': 'true' }, ['◌']),
      el('span', {}, [
        'No tournament yet — the first matchup appears once a challenger runs against the champion.',
      ]),
    ]);
  }

  const { width, height } = layout;

  // The shared hover tooltip — one node, repositioned + retargeted on
  // hover. Absolutely positioned within the stage.
  const tip = el('div', {
    class: 'v2-slope-tip', role: 'tooltip', 'aria-hidden': 'true', 'data-show': 'false',
  });

  const stage = el('div', {
    class: 'v2-slope-stage',
    style: `position: relative; width: ${width}px; height: ${height}px;`,
  });

  const svg = svgEl('svg', {
    class: 'v2-slope-svg',
    width, height,
    viewBox: `0 0 ${width} ${height}`,
    'aria-hidden': 'true',
  });

  // Champion through-line — drawn first so nodes sit on top. The settled
  // (solid) line and the live (dashed/amber) hop render as separate paths.
  if (layout.championPath) {
    svg.appendChild(svgEl('path', { class: 'v2-slope-champ-path', d: layout.championPath }));
  }
  if (layout.championPathLive) {
    svg.appendChild(svgEl('path', {
      class: 'v2-slope-champ-path v2-slope-champ-path-live', d: layout.championPathLive,
    }));
  }

  // Each matchup slope.
  for (const m of layout.matchups) {
    svg.appendChild(svgEl('path', {
      class: `v2-slope-edge v2-slope-edge-${m.decision}`,
      'data-decision': m.decision,
      d: m.slopePath,
    }));
  }
  stage.appendChild(svg);

  // Hover-target overlay for each slope (a thicker invisible hit path so
  // the thin slope is easy to hover/click). Built as buttons over the SVG.
  for (const m of layout.matchups) {
    const hit = makeSlopeHit(m, { onMatchup, tip, stage });
    stage.appendChild(hit);
  }

  // Champion + challenger nodes (with Tufte endpoint labels). A promoted
  // challenger node carries the "joins the line" class; a rejected one the
  // "falls away" class; a running one pulses.
  for (let i = 0; i < layout.matchups.length; i += 1) {
    const m = layout.matchups[i];
    // Champion node — only render the first occurrence per distinct
    // (x, id) so a held champion is not double-drawn; here one per column
    // is fine (each round has its own champion column position).
    stage.appendChild(makeNode({
      role: 'champion',
      decision: m.decision,
      id: m.champion.id,
      scalar: m.champion.scalar,
      x: m.x,
      y: m.champion.y,
      side: i === 0 ? 'left' : 'left',
      onGeneration, tip, stage,
    }));
    // Challenger node.
    stage.appendChild(makeNode({
      role: 'challenger',
      decision: m.decision,
      id: m.challenger.id,
      scalar: m.challenger.scalar,
      x: m.slopeX1,
      y: m.challenger.y,
      joins: m.challenger.joins,
      deltaScalar: m.deltaScalar,
      firedRule: m.firedRule,
      side: 'right',
      onGeneration, tip, stage,
    }));
    // Round-axis label.
    stage.appendChild(el('span', {
      class: 'v2-slope-round-label v2-num',
      style: `position: absolute; left: ${m.x}px; top: ${height - PAD_BOTTOM + 14}px; transform: translateX(-50%);`,
    }, [`round ${m.round + 1}`]));
  }

  stage.appendChild(tip);

  return el('div', {
    class: 'v2-slope', 'data-mode': 'plot',
    role: 'group', 'aria-label': 'Tournament slopegraph — champion vs challenger across rounds',
  }, [
    el('div', { class: 'v2-slope-scroll' }, [stage]),
    legend(),
  ]);
}

// A legend pinning the color/glyph semantics (redundant to color — a11y).
function legend() {
  const item = (cls, glyph, label) => el('span', { class: `v2-slope-legend-item ${cls}` }, [
    el('span', { class: 'v2-slope-legend-glyph', 'aria-hidden': 'true' }, [glyph]),
    el('span', { class: 'v2-slope-legend-label' }, [label]),
  ]);
  return el('div', { class: 'v2-slope-legend', 'aria-hidden': 'false' }, [
    item('v2-slope-legend-promoted', '✓', 'promoted — joins the line'),
    item('v2-slope-legend-rejected', '✗', 'rejected — falls away'),
    item('v2-slope-legend-running', '◇', 'running'),
    el('span', { class: 'v2-slope-legend-axis' }, ['y = loss · lower is higher']),
  ]);
}

// The invisible-but-thick hover/click target laid over a slope. Carries
// the verdict tooltip and fires onMatchup(challengerId) on click.
function makeSlopeHit(m, ctx) {
  const { onMatchup, tip } = ctx;
  const midX = (m.slopeX0 + m.slopeX1) / 2;
  const midY = (m.champion.y + m.challenger.y) / 2;

  const hit = el('button', {
    type: 'button',
    class: `v2-slope-hit v2-slope-hit-${m.decision}`,
    'data-decision': m.decision,
    'data-challenger': m.challenger.id,
    // Position a slim hit zone around the slope's midpoint.
    style: `position: absolute; left: ${m.slopeX0}px; top: ${Math.min(m.champion.y, m.challenger.y) - 8}px;`
      + ` width: ${Math.max(m.slopeX1 - m.slopeX0, 8)}px;`
      + ` height: ${Math.abs(m.challenger.y - m.champion.y) + 16}px;`,
    'aria-label': matchupAria(m),
  });

  const showTip = () => positionTip(tip, ctx.stage, midX, midY, matchupTipNodes(m));
  const hideTip = () => hideTooltip(tip);
  hit.addEventListener('mouseenter', showTip);
  hit.addEventListener('focus', showTip);
  hit.addEventListener('mouseleave', hideTip);
  hit.addEventListener('blur', hideTip);
  if (onMatchup) {
    const fire = (ev) => {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (m.challenger.id && m.challenger.id !== '?') onMatchup(m.challenger.id);
    };
    hit.addEventListener('click', fire);
    hit.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') fire(ev);
    });
  }
  return hit;
}

// A champion/challenger node: a button carrying the verdict glyph + a
// Tufte endpoint label (gen id + scalar). Clicking drills to the
// generation; hovering shows the same matchup tooltip.
function makeNode(spec) {
  const {
    role, decision, id, scalar, x, y, joins, side, onGeneration, tip,
  } = spec;
  const scalarStr = fmtScalar(scalar);
  const isChallenger = role === 'challenger';
  const glyph = isChallenger ? (VERDICT_GLYPH[decision] || VERDICT_GLYPH.running) : '●';

  const dot = el('span', {
    class: 'v2-slope-dot', 'aria-hidden': 'true',
    style: `width: ${DOT_R * 2}px; height: ${DOT_R * 2}px;`,
  }, [glyph]);

  const labelChildren = [
    el('span', { class: 'v2-slope-node-id v2-num' }, [String(id)]),
    el('span', { class: 'v2-slope-node-scalar v2-num' }, [scalarStr]),
  ];
  const label = el('span', { class: `v2-slope-node-label v2-slope-node-label-${side}` }, labelChildren);

  const cls = [
    'v2-slope-node',
    `v2-slope-node-${role}`,
    `v2-slope-node-${decision}`,
  ];
  if (isChallenger) cls.push(joins ? 'v2-slope-node-joins' : 'v2-slope-node-falls');

  const props = {
    type: 'button',
    class: cls.join(' '),
    'data-role': role,
    'data-decision': decision,
    'data-gen': String(id),
    'data-live': decision === 'running' && isChallenger ? 'true' : null,
    style: `position: absolute; left: ${x - DOT_R}px; top: ${y - DOT_R}px;`,
    'aria-label': `${role} generation ${id}`
      + (scalarStr !== '—' ? `, scalar ${scalarStr}` : '')
      + (isChallenger ? `, ${decision}` : ''),
  };

  const node = el('button', props, side === 'left'
    ? [label, dot]
    : [dot, label]);

  if (tip && isChallenger) {
    // The challenger node shares the matchup tooltip (verdict · Δ · rule).
    const m = spec;
    const showTip = () => positionTip(tip, spec.stage, x, y, [
      tipVerdictRow(decision),
      tipDeltaRow(m.deltaScalar),
      m.firedRule ? tipRuleRow(m.firedRule) : null,
    ].filter(Boolean));
    node.addEventListener('mouseenter', showTip);
    node.addEventListener('focus', showTip);
    node.addEventListener('mouseleave', () => hideTooltip(tip));
    node.addEventListener('blur', () => hideTooltip(tip));
  }

  if (onGeneration) {
    const fire = (ev) => {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (id && id !== '?') onGeneration(String(id));
    };
    node.addEventListener('click', fire);
    node.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') fire(ev);
    });
  }
  return node;
}

// ---------------------------------------------------------------------------
// Tooltip helpers
// ---------------------------------------------------------------------------
function matchupAria(m) {
  const parts = [`round ${m.round + 1}`, m.decision,
    `champion ${m.champion.id} → challenger ${m.challenger.id}`];
  if (m.deltaScalar != null) parts.push(`Δscalar ${fmtDelta(m.deltaScalar)}`);
  if (m.firedRule) parts.push(`fired rule ${m.firedRule}`);
  return parts.join(', ');
}

function tipVerdictRow(decision) {
  return el('div', { class: `v2-slope-tip-verdict v2-slope-tip-${decision}` }, [
    el('span', { class: 'v2-slope-tip-glyph', 'aria-hidden': 'true' }, [VERDICT_GLYPH[decision] || '◇']),
    el('span', null, [decision]),
  ]);
}
function tipDeltaRow(delta) {
  const d = finite(delta);
  const sig = d == null ? 'neutral' : (d < 0 ? 'improve' : (d > 0 ? 'regress' : 'neutral'));
  return el('div', { class: 'v2-slope-tip-row' }, [
    el('span', { class: 'v2-slope-tip-key' }, ['Δscalar']),
    el('span', { class: 'v2-slope-tip-val v2-num', 'data-signal': sig }, [fmtDelta(d)]),
  ]);
}
function tipRuleRow(rule) {
  return el('div', { class: 'v2-slope-tip-row' }, [
    el('span', { class: 'v2-slope-tip-key' }, ['fired rule']),
    el('span', { class: 'v2-slope-tip-val v2-mono' }, [rule]),
  ]);
}
function matchupTipNodes(m) {
  return [
    tipVerdictRow(m.decision),
    el('div', { class: 'v2-slope-tip-row' }, [
      el('span', { class: 'v2-slope-tip-key' }, ['matchup']),
      el('span', { class: 'v2-slope-tip-val v2-num' }, [`${m.champion.id} → ${m.challenger.id}`]),
    ]),
    tipDeltaRow(m.deltaScalar),
    m.firedRule ? tipRuleRow(m.firedRule) : null,
  ].filter(Boolean);
}

function positionTip(tip, stage, x, y, children) {
  // Repopulate + show. The tooltip is absolutely positioned within the
  // stage; we anchor it above-left of the point and let CSS handle the
  // arrow/offset.
  while (tip.firstChild) tip.removeChild(tip.firstChild);
  for (const c of children) tip.appendChild(c);
  tip.setAttribute('style', `position: absolute; left: ${x}px; top: ${Math.max(y - 12, 4)}px;`);
  tip.setAttribute('data-show', 'true');
  tip.setAttribute('aria-hidden', 'false');
}
function hideTooltip(tip) {
  tip.setAttribute('data-show', 'false');
  tip.setAttribute('aria-hidden', 'true');
}

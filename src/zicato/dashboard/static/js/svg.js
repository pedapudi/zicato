// js/svg.js — dependency-free SVG data-viz primitives (Console).
//
// Self-contained for Variant N ("Console II"). Mark CSS classes are `dn-*` and
//     themed in one place, swapped by the [data-n-theme] attribute.

import { svgEl, el } from './core/dom.js';
import { attachHovercard } from './hovercard.js';

export const NS = 'http://www.w3.org/2000/svg';

// ── CROWN GLYPHS — the SINGLE source of truth (CONSOLE-IV §9) ─────────
//
// The rule, defined ONCE so it cannot drift across files again:
//   CROWN.current — the CURRENT champion (the crowned survivor of the gate;
//                   the last id in champion_lineage). Solid crown.
//   CROWN.former  — a FORMER champion (the displaced incumbent) OR a transient
//                   round-leader before the gate decides. Hollow crown.
// A just-crowned gate winner IS the current champion, so gate labels use
// CROWN.current too (the historical `♚` mix is retired). Every file that emits
// a crown imports from here.
export const CROWN = { current: '♛', former: '♔' };

// Wire a mark with the styled, theme-aware HOVERCARD instead of a native,
// off-brand <title> tooltip (positioned card on hover/focus; keyboard- and
// reduced-motion-aware; a transient overlay, NOT part of the digest-gated
function hov(node, tip) { attachHovercard(node, tip); return node; }

// Wire a node as a pointer/keyboard activatable control (click + Enter/Space).
// Returns the node. No-op when `fn` is falsy.
function clickable(node, fn) {
  if (!fn) return node;
  node.style.cursor = 'pointer';
  node.addEventListener('click', () => fn());
  node.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); fn(); } });
  return node;
}

// ---- numeric helpers ------------------------------------------------

export function isNum(v) { return typeof v === 'number' && isFinite(v); }

export function finiteValues(arr) {
  return (Array.isArray(arr) ? arr : []).filter(isNum);
}

export function extent(values) {
  const v = finiteValues(values);
  if (v.length === 0) return [0, 1];
  let lo = v[0]; let hi = v[0];
  for (const x of v) { if (x < lo) lo = x; if (x > hi) hi = x; }
  if (lo === hi) { lo -= 0.5; hi += 0.5; }
  return [lo, hi];
}

// A linear scale from [d0,d1] to [r0,r1].
export function scale(domain, range) {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0 || 1;
  return (x) => r0 + ((x - d0) / span) * (r1 - r0);
}

export function fmt(v, digits) {
  if (!isNum(v)) return '—';
  const d = digits == null ? 3 : digits;
  return v.toFixed(d);
}
export function fmtSigned(v, digits) {
  if (!isNum(v)) return '—';
  const d = digits == null ? 3 : digits;
  return (v > 0 ? '+' : '') + v.toFixed(d);
}

export function title(text) {
  const t = svgEl('title', null);
  t.textContent = text == null ? '' : String(text);
  return t;
}

function shortLabel(s, n) {
  const max = isNum(n) ? n : 12;
  const str = s == null ? '' : String(s);
  return str.length > max ? str.slice(0, max - 1) + '…' : str;
}

// ── responsive (aspect-locked, full-width hero) sizing ───────────────
//
// The SHARED contract every structure builder honours, mirroring the reference
// `sparkline({responsive:true})` (the cross-epoch fix). Default OFF: a fixed
// call site (mini cards, drills) is untouched. When opt-in:
//   * `width:100%`, the fixed pixel `height` is DROPPED;
//   * a CSS `aspect-ratio` (== the viewBox aspect w/h) is pinned inline so a
//     `preserveAspectRatio:'none'` scale is UNIFORM (no shear) and the figure
//     tracks the screen width up to the matched max (the *-hero CSS cap);
//   * the hero class is appended (carrying the cap + a meet→none switch where a
//     builder otherwise meets its box).
// No measurement, no ResizeObserver, no post-mount mutation — leak-free and
// digest-gate compatible. Returns the (mutated) svgAttrs for chaining.
function applyResponsive(svgAttrs, opts, w, h, heroClass) {
  const o = opts || {};
  if (!o.responsive && !o.fitWidth) return svgAttrs;
  delete svgAttrs.height;
  svgAttrs.width = '100%';
  // a uniform scale needs preserveAspectRatio:'none' against a box whose aspect
  // EQUALS the viewBox aspect (pinned below); 'meet'/'xMid…' would letterbox.
  svgAttrs.preserveAspectRatio = 'none';
  svgAttrs.class = (svgAttrs.class ? svgAttrs.class + ' ' : '') + heroClass;
  const aspect = `aspect-ratio:${w} / ${h};`;
  svgAttrs.style = svgAttrs.style ? svgAttrs.style + aspect : aspect;
  return svgAttrs;
}

// The lane set for a racing rung's FULL field (every survivor still racing this
// rung), per the shared contract: the UNION of the rung's `live_progress` keys,
// its `competitors`, and its `survivors`/`cut` ids — EXCLUDING the champion /
// benchmark (which defends at the gate, never a rung lane). Stable, de-duped,
// competitor-order-first so the figure is deterministic. A rung with survivors
// v5 + v7 yields BOTH (not just the first matchup's competitors). `exclude` is a
// Set of ids to drop (the champion / benchmark id).
function rungFieldLanes(rung, exclude) {
  const ex = exclude instanceof Set ? exclude : new Set(exclude ? [String(exclude)] : []);
  const seen = new Set();
  const out = [];
  const add = (id) => {
    if (id == null) return;
    const s = String(id);
    if (!s || ex.has(s) || seen.has(s)) return;
    seen.add(s); out.push(s);
  };
  // competitor order first (deterministic), then any survivor/cut/live id not
  // already listed (a rung whose live_progress carries a lane the published
  // competitors elided — the multi-survivor case — still shows every lane).
  for (const c of (Array.isArray(rung && rung.competitors) ? rung.competitors : [])) add(c);
  for (const s of (Array.isArray(rung && rung.survivors) ? rung.survivors : [])) add(s);
  for (const c of (Array.isArray(rung && rung.cut) ? rung.cut : [])) add(c);
  const prog = (rung && rung.live_progress && typeof rung.live_progress === 'object') ? rung.live_progress : null;
  if (prog) for (const k of Object.keys(prog)) add(k);
  return out;
}

// ── orthogonal-pipe routers (the double-elim drop-bus language) ──────
//
// A rounded-corner orthogonal "pipe": a vertical drop from (x0,y0) to a
// horizontal bus at busY, a run across to xt, then a short vertical into
// (xt,yt). Corners are arc-rounded (radius rr) so the route reads as a calm
// pipe, not a kinked wire. Direction-aware (target left OR right of source).
// Reproduces the study's `elbow()` (double-elim.html opt 7).
export function elbowPath(x0, y0, xt, yt, busY, rr) {
  rr = rr || 6;
  const dir = xt >= x0 ? 1 : -1;
  const downA = busY > y0 ? 1 : -1;
  const downB = yt > busY ? 1 : -1;
  const r1 = Math.min(rr, Math.abs(busY - y0) / 2);
  const r2 = Math.min(rr, Math.abs(yt - busY) / 2, Math.abs(xt - x0) / 2);
  return `M${x0.toFixed(1)} ${y0.toFixed(1)}`
    + ` L${x0.toFixed(1)} ${(busY - downA * r1).toFixed(1)}`
    + ` Q${x0.toFixed(1)} ${busY.toFixed(1)} ${(x0 + dir * r1).toFixed(1)} ${busY.toFixed(1)}`
    + ` L${(xt - dir * r2).toFixed(1)} ${busY.toFixed(1)}`
    + ` Q${xt.toFixed(1)} ${busY.toFixed(1)} ${xt.toFixed(1)} ${(busY + downB * r2).toFixed(1)}`
    + ` L${xt.toFixed(1)} ${yt.toFixed(1)}`;
}

// A WB→LB demotion route that runs in a DEDICATED CHANNEL below the whole lane
// stack — never on (or across) any competitor's lane row. The old elbow anchored
// its horizontal bus a half-row beneath the SOURCE lane, so the run cut straight
// across the rows (boxes / labels / dots) of every lane physically between the
// WB column and the LB re-entry column; with two losers demoted from one node the
// two buses straddled the intervening lanes and crossed each other. Here each
// demotion edge owns a distinct horizontal channel lane (chY) in a reserved
// gutter under the stack, and a per-edge horizontal NUDGE (dx) so two edges that
// share a source column (the two-loser case) drop on parallel, non-overlapping
// verticals rather than one shared x. The endpoints stay EXACTLY on the source
// dot (x0,y0) and the LB-entry node (xt,yt) so the visual connection is intact.
//
//   (x0,y0) ─┐                                   ┌─ (xt,yt)
//            │  short stub, jog to the dx lane    │   rise, jog back to xt
//            └──┐                              ┌──┘
//   chY  ───────┴──────────── run across ─────┴───────────   (below all lanes)
//
// dx is signed toward the run direction so the jog opens into the channel, never
// back over the source node. rr rounds every corner into the calm-pipe language.
export function channelDropPath(x0, y0, xt, yt, chY, dx, rr) {
  rr = rr || 5;
  const dir = xt >= x0 ? 1 : -1;
  const nx0 = x0 + dir * dx;        // nudged source vertical lane
  const nxt = xt - dir * dx;        // nudged target vertical lane
  // a short vertical stub off the dot before the jog, so the route leaves the
  // node cleanly (and a same-column pair separates immediately, not at the dot).
  const stub = Math.min(6, Math.abs(chY - y0) * 0.25);
  const yA = y0 + stub;             // depth of the source stub before the jog
  const yB = yt + stub;             // height of the target stub before the rise
  const rRun = (a, b) => Math.min(rr, Math.abs(a - b) / 2);
  const rJog = Math.max(1, Math.min(rr, (Math.abs(dx) || rr * 2) / 2, (stub || rr * 2) / 2));
  const rRunA = rRun(chY, yA);
  const rRunB = rRun(chY, yB);
  return `M${x0.toFixed(1)} ${y0.toFixed(1)}`
    // source: short stub down, jog out to the nudged lane
    + ` L${x0.toFixed(1)} ${(yA - rJog).toFixed(1)}`
    + ` Q${x0.toFixed(1)} ${yA.toFixed(1)} ${(x0 + dir * rJog).toFixed(1)} ${yA.toFixed(1)}`
    + ` L${(nx0 - dir * rJog).toFixed(1)} ${yA.toFixed(1)}`
    + ` Q${nx0.toFixed(1)} ${yA.toFixed(1)} ${nx0.toFixed(1)} ${(yA + rJog).toFixed(1)}`
    // drop into the channel, arc onto the run
    + ` L${nx0.toFixed(1)} ${(chY - rRunA).toFixed(1)}`
    + ` Q${nx0.toFixed(1)} ${chY.toFixed(1)} ${(nx0 + dir * rRunA).toFixed(1)} ${chY.toFixed(1)}`
    // run across the channel, arc off the run, rise toward the target stub
    + ` L${(nxt - dir * rRunB).toFixed(1)} ${chY.toFixed(1)}`
    + ` Q${nxt.toFixed(1)} ${chY.toFixed(1)} ${nxt.toFixed(1)} ${(chY - rRunB).toFixed(1)}`
    + ` L${nxt.toFixed(1)} ${(yB + rJog).toFixed(1)}`
    // target: jog back to the LB column, rise into the node
    + ` Q${nxt.toFixed(1)} ${yB.toFixed(1)} ${(nxt + dir * rJog).toFixed(1)} ${yB.toFixed(1)}`
    + ` L${(xt - dir * rJog).toFixed(1)} ${yB.toFixed(1)}`
    + ` Q${xt.toFixed(1)} ${yB.toFixed(1)} ${xt.toFixed(1)} ${(yB - rJog).toFixed(1)}`
    + ` L${xt.toFixed(1)} ${yt.toFixed(1)}`;
}

// a live racing lane's progress label: "k/N boards" when the rung's board total
// is known, else "k running" when only the in-flight count is known.
function laneProgressText(lane) {
  if (!lane) return '';
  if (isNum(lane.total) && lane.total > 0) return `${lane.done || 0}/${lane.total} boards`;
  if (lane.inflight) return `${lane.inflight} running`;
  if (lane.done) return `${lane.done} boards`;
  return 'racing';
}

// ---- sparkline ------------------------------------------------------

// A dependency-free sparkline. Opt-in robustness flags (default OFF so existing
// dense call sites are untouched):
//   o.padY    — pad the [min,max] y-domain by this FRACTION of its span (e.g.
//               0.18) so the stroke breathes inside the frame.
//   o.minSpan — enforce a MINIMUM absolute y-range: a near-flat series (every
//               value ~equal) gets a CENTRED domain of at least this width, so
//               it reads as gentle vertical variation rather than a pin-flat
//               line — while a TRULY flat series stays calmly centred (its tiny
//               real deltas show as tiny wiggles, never a fabricated slope).
//   o.markers — draw a dot per finite point (atop the line). Useful for
//               FEW-points series, where a single segment reads as a skewed
//               slash; a single point renders as a centred dot, not a line.
//   o.responsive — OPT-IN aspect-locked sizing for a FULL-WIDTH hero panel (the
//               cross-epoch trajectory). The dense, fixed-size call sites (fleet
//               cards, board-status gap, dag sparks) are UNTOUCHED — they keep
//               their small fixed pixel height. The default 'none' aspect ratio
//               stretches a small viewBox NON-uniformly to a wide pane, which
//               flattens every slope into the skewed streak. With `responsive`
//               we DROP the fixed pixel height, keep `width:100%`, and pin the
//               element's CSS `aspect-ratio` to the viewBox aspect (w / h) so the
//               box aspect EQUALS the viewBox aspect — then the 'none' scale is
//               UNIFORM (no distortion) and the height tracks the width. The
//               .dn-spark-hero CSS caps it with a matched max-width / max-height
//               on ultra-wide screens (so the cap never re-introduces a stretch).
//               No measurement, no ResizeObserver, no post-mount mutation — so it
//               is leak-free and fully compatible with the digest-gated swap.
export function sparkline(opts) {
  const o = opts || {};
  const w = o.width || 120;
  const h = o.height || 28;
  const pad = 2;
  const raw = Array.isArray(o.values) ? o.values : [];
  const fin = finiteValues(raw);
  const svgAttrs = {
    // fit-to-width: width:100% so the trend sparkline scales to its pane.
    class: 'dn-spark', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'none', role: 'img',
  };
  if (o.responsive) {
    // aspect-locked hero: drop the fixed pixel height and let the CSS
    // aspect-ratio (== the viewBox aspect) drive it, so the 'none' scale is
    // uniform and the trajectory keeps its real slopes at any pane width.
    delete svgAttrs.height;
    svgAttrs.class = 'dn-spark dn-spark-hero';
    svgAttrs.style = `aspect-ratio:${w} / ${h};`;
  }
  const svg = svgEl('svg', svgAttrs);
  if (fin.length === 0) {
    svg.appendChild(svgEl('line', { x1: pad, y1: h / 2, x2: w - pad, y2: h / 2, class: 'dn-spark-empty' }));
    return svg;
  }
  // y-domain with OPT-IN min-span + fractional padding. extent() already opens a
  // ±0.5 window when lo===hi; minSpan/padY refine it so a near-flat series sits
  // calmly CENTRED (mid ± span/2) instead of collapsed onto one frame edge.
  let [lo, hi] = extent(fin);
  if (isNum(o.minSpan) && o.minSpan > 0 && hi - lo < o.minSpan) {
    const mid = (lo + hi) / 2;
    lo = mid - o.minSpan / 2;
    hi = mid + o.minSpan / 2;
  }
  if (isNum(o.padY) && o.padY > 0) {
    const padAmt = (hi - lo) * o.padY;
    lo -= padAmt;
    hi += padAmt;
  }
  // a single finite point has no x-spread: render it as a centred dot, never a
  // line to nowhere (handled below by skipping the path; the endDot/marker
  // draws the dot at the vertical mid of the centred y-domain).
  const singlePoint = fin.length === 1;
  const x = scale([0, Math.max(1, raw.length - 1)], [pad, w - pad]);
  const y = scale([lo, hi], [h - pad, pad]);
  if (o.band) {
    svg.appendChild(svgEl('rect', { x: pad, y: pad, width: w - 2 * pad, height: h - 2 * pad, class: 'dn-spark-band' }));
  }
  if (isNum(o.baseline)) {
    const by = y(o.baseline);
    svg.appendChild(svgEl('line', { x1: pad, x2: w - pad, y1: by, y2: by, class: 'dn-spark-baseline' }));
  }
  if (!singlePoint) {
    let d = '';
    let penDown = false;
    raw.forEach((v, i) => {
      if (!isNum(v)) { penDown = false; return; }
      d += `${penDown ? 'L' : 'M'}${x(i).toFixed(2)},${y(v).toFixed(2)} `;
      penDown = true;
    });
    svg.appendChild(svgEl('path', { d: d.trim(), class: 'dn-spark-line', fill: 'none' }));
  }
  // the last finite index — the end-dot (good/bad coloured) is drawn here, so
  // the opt-in per-point markers skip it to avoid a doubled dot.
  let endI = -1;
  for (let i = raw.length - 1; i >= 0; i--) { if (isNum(raw[i])) { endI = i; break; } }
  if (o.markers) {
    raw.forEach((v, i) => {
      if (!isNum(v) || i === endI) return;
      svg.appendChild(hov(svgEl('circle', { cx: x(i), cy: y(v), r: 1.8, class: 'dn-spark-mark' }), fmt(v)));
    });
  }
  if (o.endDot !== false) {
    const lastI = endI;
    if (lastI >= 0) {
      const dir = o.goodDirection || 'down';
      let firstI = -1;
      for (let i = 0; i < raw.length; i++) { if (isNum(raw[i])) { firstI = i; break; } }
      const improved = firstI >= 0 && lastI !== firstI
        ? (dir === 'down' ? raw[lastI] < raw[firstI] : raw[lastI] > raw[firstI])
        : null;
      const cls = improved === null ? 'dn-spark-dot'
        : improved ? 'dn-spark-dot dn-good' : 'dn-spark-dot dn-bad';
      // a lone centred point reads a hair larger so it's an intentional dot.
      const r = singlePoint ? 2.8 : 2.2;
      svg.appendChild(hov(svgEl('circle', { cx: x(lastI), cy: y(raw[lastI]), r, class: cls }), fmt(raw[lastI])));
    }
  }
  return svg;
}

// ---- bumps chart (lineage as ranked lanes) --------------------------
// Champion lineage on its own spine lane; rejected challengers branch off.
// opts: { width, height, nodes:[{id, x, promoted, scalar, parent}], onClick }
export function bumps(opts) {
  const o = opts || {};
  const nodes = (Array.isArray(o.nodes) ? o.nodes : []).filter((n) => n);
  const w = o.width || 640;
  const h = o.height || 170;
  const padX = 44; const spineY = h * 0.40; const challY = h * 0.80;
  const svg = svgEl('svg', { class: 'dn-bumps', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (nodes.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no generations yet';
    svg.appendChild(t);
    return svg;
  }
  const maxX = Math.max(1, ...nodes.map((n) => n.x || 0));
  const X = scale([0, maxX], [padX, w - padX]);

  svg.appendChild(svgEl('line', { x1: padX, x2: w - padX, y1: spineY, y2: spineY, class: 'dn-lane-guide dn-spine-guide' }));
  svg.appendChild(svgEl('line', { x1: padX, x2: w - padX, y1: challY, y2: challY, class: 'dn-lane-guide' }));
  const lblS = svgEl('text', { x: 6, y: spineY - 8, class: 'dn-lane-label' }); lblS.textContent = 'champion';
  const lblC = svgEl('text', { x: 6, y: challY - 8, class: 'dn-lane-label' }); lblC.textContent = 'challenger';
  svg.appendChild(lblS); svg.appendChild(lblC);

  const laneY = (n) => (n.promoted ? spineY : challY);
  const byId = new Map(nodes.map((n) => [n.id, n]));

  // De-collide the screen-x of nodes WITHIN each lane (the F bug). Two
  // challengers branching off the same parent share a generation index and
  // would land on the same x; push them apart along the lane.
  const cx = new Map();
  for (const lanePromoted of [true, false]) {
    const lane = nodes.filter((n) => !!n.promoted === lanePromoted)
      .map((n) => ({ id: n.id, x: X(n.x || 0) }))
      .sort((a, b) => a.x - b.x);
    const minGap = 34;
    for (let i = 1; i < lane.length; i++) {
      if (lane[i].x - lane[i - 1].x < minGap) lane[i].x = lane[i - 1].x + minGap;
    }
    // clamp the trailing overflow back inside the frame
    const right = w - padX;
    if (lane.length && lane[lane.length - 1].x > right) {
      lane[lane.length - 1].x = right;
      for (let i = lane.length - 2; i >= 0; i--) {
        if (lane[i + 1].x - lane[i].x < minGap) lane[i].x = lane[i + 1].x - minGap;
      }
    }
    for (const n of lane) cx.set(n.id, n.x);
  }
  const nodeX = (n) => (cx.has(n.id) ? cx.get(n.id) : X(n.x || 0));

  const promoted = nodes.filter((n) => n.promoted).sort((a, b) => nodeX(a) - nodeX(b));
  for (let i = 1; i < promoted.length; i++) {
    svg.appendChild(svgEl('line', { x1: nodeX(promoted[i - 1]), y1: spineY, x2: nodeX(promoted[i]), y2: spineY, class: 'dn-spine-line' }));
  }
  for (const n of nodes) {
    if (n.promoted) continue;
    const p = n.parent ? byId.get(n.parent) : null;
    const px = p ? nodeX(p) : nodeX(n) - 40;
    const py = p ? laneY(p) : spineY;
    const nx = nodeX(n);
    const path = `M${px},${py} C${(px + nx) / 2},${py} ${(px + nx) / 2},${challY} ${nx},${challY}`;
    svg.appendChild(svgEl('path', { d: path, class: 'dn-branch', fill: 'none' }));
  }
  for (const n of nodes) {
    const cy = laneY(n);
    const px = nodeX(n);
    const cls = 'dn-bump-node ' + (n.promoted ? 'dn-promoted' : 'dn-rejected');
    const c = hov(svgEl('circle', { cx: px, cy, r: n.promoted ? 4.5 : 3.5, class: cls, tabindex: o.onClick ? '0' : null }),
      `${n.id}${isNum(n.scalar) ? ' · ' + fmt(n.scalar) : ''} · ${n.promoted ? 'promoted' : 'rejected'}`);
    clickable(c, o.onClick && (() => o.onClick(n)));
    svg.appendChild(c);
    const t = svgEl('text', { x: px, y: cy + 16, class: 'dn-bump-label', 'text-anchor': 'middle' });
    t.textContent = shortLabel(n.id);
    svg.appendChild(t);
  }
  return svg;
}

// One-dimensional collision resolver — exported for the test suite.
export function decollide(items, y, minGap, top, bottom) {
  const idx = items.map((it, i) => ({ i, pos: isNum(it.v) ? y(it.v) : (top + bottom) / 2 }));
  idx.sort((p, q) => p.pos - q.pos);
  for (let k = 1; k < idx.length; k++) {
    if (idx[k].pos - idx[k - 1].pos < minGap) idx[k].pos = idx[k - 1].pos + minGap;
  }
  if (idx.length && idx[idx.length - 1].pos > bottom) {
    idx[idx.length - 1].pos = bottom;
    for (let k = idx.length - 2; k >= 0; k--) {
      if (idx[k + 1].pos - idx[k].pos < minGap) idx[k].pos = idx[k + 1].pos - minGap;
    }
  }
  if (idx.length && idx[0].pos < top) idx[0].pos = top;
  const out = new Array(items.length);
  for (const p of idx) out[p.i] = p.pos;
  return out;
}

// Spread coincident node positions a hair apart — exported for the tests.
export function jitterColumn(ys, step) {
  const out = ys.slice();
  const groups = new Map();
  ys.forEach((v, i) => {
    if (!isNum(v)) return;
    const key = Math.round(v * 2) / 2;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(i);
  });
  for (const idxs of groups.values()) {
    if (idxs.length < 2) continue;
    const n = idxs.length;
    const mid = (n - 1) / 2;
    idxs.forEach((idx, k) => { out[idx] = ys[idx] + (k - mid) * step; });
  }
  return out;
}

// ---- theme-aware heatmap (rows × cols coloured by value) ------------
export function heatmap(opts) {
  const o = opts || {};
  const rows = Array.isArray(o.rows) ? o.rows : [];
  const cols = Array.isArray(o.cols) ? o.cols : [];
  const cw = o.cellW || 24;
  const ch = o.cellH || 15;
  const labelW = o.labelWidth || 128;
  const headH = o.headHeight || 44;
  const w = labelW + cols.length * cw + 6;
  const h = headH + rows.length * ch + 6;
  // FIT-TO-WIDTH: width:100% + a viewBox so the matrix scales DOWN to its pane
  // (no fixed pixel width that overflows, no horizontal-scroll wrapper). The
  // intrinsic cell size (cw/ch) is density-scaled by the caller.
  const svg = svgEl('svg', { class: 'dn-heatmap', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img' });
  if (rows.length === 0 || cols.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'dn-empty-label' });
    t.textContent = 'no profiles yet';
    svg.appendChild(t);
    return svg;
  }
  const vals = [];
  for (const r of rows) for (const c of cols) { const v = o.value(r.id, c.id); if (isNum(v)) vals.push(v); }
  const [lo, hi] = extent(vals);
  const span = hi - lo || 1;

  cols.forEach((c, j) => {
    const cx = labelW + j * cw + cw / 2;
    const t = svgEl('text', { x: cx, y: headH - 6, class: 'dn-hm-col', transform: `rotate(-45 ${cx} ${headH - 6})`, 'text-anchor': 'start' });
    t.textContent = shortLabel(c.label);
    svg.appendChild(t);
  });
  rows.forEach((r, i) => {
    const ry = headH + i * ch;
    const lbl = svgEl('text', { x: labelW - 6, y: ry + ch - 4, class: 'dn-hm-row', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(r.label);
    svg.appendChild(lbl);
    cols.forEach((c, j) => {
      const v = o.value(r.id, c.id);
      const cx = labelW + j * cw;
      const t = isNum(v) ? (v - lo) / span : null;
      // Theme-aware, HIGHER-CONTRAST cell scale. Two contrast axes, both driven
      // by the same per-theme CSS tokens (so it stays correct across all 16
      // themes, light and dark):
      // from an EMPTY cell (the flat --v2-cell-empty token at full opacity).
      const cls = t == null ? 'dn-hm-cell dn-hm-empty' : 'dn-hm-cell';
      const tc = t == null ? null : Math.max(0, Math.min(1, t));
      const e = tc == null ? null : Math.pow(tc, 0.8);
      const mixPct = e == null ? null : (8 + 92 * e).toFixed(2);
      const op = e == null ? null : (0.30 + 0.70 * e).toFixed(3);
      const attrs = { x: cx + 1, y: ry + 1, width: cw - 2, height: ch - 2, rx: 1.5, class: cls };
      if (op != null) attrs['fill-opacity'] = op;
      const cell = hov(svgEl('rect', attrs), `${r.label} × ${c.label}: ${isNum(v) ? fmt(v) : '—'}`);
      if (mixPct != null) {
        // theme-correct cool→hot gradient via CSS custom props (no hardcoded hex)
        cell.style.setProperty('fill', `color-mix(in srgb, var(--v2-hm-hot) ${mixPct}%, var(--v2-hm-cool))`);
        cell.setAttribute('data-hm-mix', mixPct);
      }
      clickable(cell, o.onClick && (() => o.onClick(r.id, c.id)));
      svg.appendChild(cell);
    });
  });
  return svg;
}

// ---- value dot-plot with a reference line (clickable; onClick → full item) --
export function valueDotPlot(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d);
  const w = o.width || 460;
  const rh = o.rowHeight || 19;
  const labelW = o.labelWidth || 170;
  const glyphW = 16;
  const h = Math.max(rh, items.length * rh + 8);
  // FIT-TO-WIDTH: width:100% + viewBox so the dot-plot scales to its pane (and
  // the narrower compare-split column) without overflowing.
  const svg = svgEl('svg', { class: 'dn-valdot', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMin meet', role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'dn-empty-label' });
    t.textContent = 'no scored entries';
    svg.appendChild(t);
    return svg;
  }
  const ref = o.reference && isNum(o.reference.value) ? o.reference.value : null;
  const vals = items.map((d) => d.value).filter(isNum);
  if (ref != null) vals.push(ref);
  let [lo, hi] = extent(vals);
  lo = Math.min(lo, 0);
  if (lo === hi) { hi += 1; }
  const x = scale([lo, hi], [labelW + 4, w - 4 - glyphW]);

  if (ref != null) {
    const rx = x(ref);
    svg.appendChild(hov(svgEl('line', { x1: rx, x2: rx, y1: 2, y2: h - 2, class: 'dn-ref-rule' }),
      `${(o.reference.label || 'reference')}: ${fmt(ref)}`));
  }
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 4;
    const hasCtx = d.context != null && String(d.context) !== '';
    const g = svgEl('g', { class: 'dn-dotrow', tabindex: o.onClick ? '0' : null });
    // With a context tag the name lifts onto its own baseline and the dim tag
    // sits just beneath it (two stacked right-anchored lines inside the gutter).
    const nameY = hasCtx ? cy - 2 : cy + 3;
    const lbl = svgEl('text', { x: labelW, y: nameY, class: 'dn-dot-label', 'text-anchor': 'end' });
    lbl.textContent = d.label != null ? shortLabel(String(d.label), 22) : '';
    g.appendChild(lbl);
    if (hasCtx) {
      // theme-aware (uses the faint ink token), no extra stylesheet rule.
      const ctx = svgEl('text', {
        x: labelW, y: cy + 9, class: 'dn-dot-ctx', 'text-anchor': 'end',
        fill: 'var(--v2-ink-faint)', 'font-size': '9px', 'font-family': 'var(--v2-mono)',
      });
      ctx.textContent = shortLabel(String(d.context), 22);
      g.appendChild(ctx);
    }
    if (isNum(d.value)) {
      const dx = x(d.value);
      g.appendChild(svgEl('line', { x1: x(lo), x2: dx, y1: cy, y2: cy, class: 'dn-dot-connector' }));
      const good = ref != null ? d.value < ref : false;
      const worse = ref != null ? d.value > ref : false;
      const cls = 'dn-dot ' + (good ? 'dn-good' : worse ? 'dn-bad' : '');
      g.appendChild(hov(svgEl('circle', { cx: dx, cy, r: 3.2, class: cls }),
        `${d.label}: ${fmt(d.value)}${ref != null ? ` (vs champ ${fmt(ref)})` : ''}`));
      // outcomeGlyph() returns a fixed 1:1-aspect <svg> sized `gsz`; position it
      // at the row's right edge via the nested-svg x/y attrs (NOT by passing the
      // chart x-coordinate as the size — that blew each glyph up to ~chart width).
      const gsz = glyphW - 4;
      const gl = outcomeGlyph(d, gsz);
      gl.setAttribute('x', w - glyphW + 2);
      gl.setAttribute('y', cy - gsz / 2);
      g.appendChild(gl);
    } else {
      const t = svgEl('text', { x: x(lo) + 6, y: cy + 3, class: 'dn-dot-missing' });
      t.textContent = 'no run';
      g.appendChild(t);
    }
    clickable(g, o.onClick && (() => o.onClick(d)));
    svg.appendChild(g);
  });
  return svg;
}

// ---- sparkbar (micro loss bars + a verdict marker) ------------------

export function sparkbar(opts) {
  const o = opts || {};
  const bars = (Array.isArray(o.bars) ? o.bars : []).filter((b) => b);
  const w = o.width || 120;
  const h = o.height || 30;
  const pad = 2;
  const footH = 2;
  // FIT-TO-WIDTH inside its trellis cell: width:100% + viewBox (height is the
  // density-scaled intrinsic dimension).
  // The BARS layer stretches non-uniformly to fill the cell width (bars are
  // rectangles — stretching them is fine). The verdict GLYPH must stay a true
  // triangle, so it rides in a SEPARATE fixed-aspect overlay (see below), NOT
  // inside this `preserveAspectRatio:'none'` viewBox.
  const svg = svgEl('svg', { class: 'dn-sparkbar', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'none', role: 'img' });
  if (bars.length === 0) {
    svg.appendChild(svgEl('line', { x1: pad, y1: h - footH, x2: w - pad, y2: h - footH, class: 'dn-spark-empty' }));
    return svg;
  }
  const dom = o.domain && o.domain.length === 2 && isNum(o.domain[0]) && isNum(o.domain[1])
    ? o.domain : extent(bars.map((b) => b.value));
  const [lo, hi] = dom[0] === dom[1] ? [dom[0], dom[0] + 1] : dom;
  const base = Math.min(lo, 0);
  const yTop = scale([base, hi], [h - footH, pad + 5]);
  const n = bars.length;
  const slot = (w - 2 * pad) / n;
  const bw = Math.max(1.5, Math.min(slot * 0.7, 10));
  const y0 = yTop(base);
  bars.forEach((b, i) => {
    const cx = pad + slot * (i + 0.5);
    if (isNum(b.value)) {
      const y = yTop(b.value);
      const cls = 'dn-sparkbar-bar' + (b.timeout ? ' dn-timeout' : '') + (b.fail ? ' dn-fail' : '');
      svg.appendChild(hov(svgEl('rect', { x: cx - bw / 2, y: Math.min(y, y0), width: bw, height: Math.max(1, Math.abs(y0 - y)), class: cls }),
        `${b.label}: ${fmt(b.value)}${b.timeout ? ' · timed out' : ''}${b.fail ? ' · failed' : ''}`));
    } else {
      svg.appendChild(hov(svgEl('line', { x1: cx, y1: y0 - 1, x2: cx, y2: y0 - 4, class: 'dn-sparkbar-missing' }), `${b.label}: no run`));
    }
  });
  svg.appendChild(svgEl('line', { x1: pad, y1: y0, x2: w - pad, y2: y0, class: 'dn-sparkbar-foot' }));
  if (o.verdict !== 'promoted' && o.verdict !== 'rejected') return svg;

  // The verdict triangle as a FIXED-ASPECT (1:1 viewBox) overlay so it renders
  // as a true triangle — never sheared by the bars' non-uniform width stretch.
  // The bars SVG + the glyph SVG share an HTML positioning wrapper; the glyph
  // pins to the top-right corner (where it sat inside the old stretched viewBox).
  const good = o.verdict === 'promoted';
  const r = 3.2;
  const tri = good ? `5,${5 - r} ${5 - r},${5 + r} ${5 + r},${5 + r}` : `5,${5 + r} ${5 - r},${5 - r} ${5 + r},${5 - r}`;
  const gsz = 12;
  const glyph = svgEl('svg', { class: 'dn-sparkbar-verdict', width: gsz, height: gsz, viewBox: '0 0 10 10', preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  glyph.appendChild(svgEl('polygon', { points: tri, class: 'dn-verdict-glyph ' + (good ? 'dn-good' : 'dn-bad') }));
  hov(glyph, o.verdict);
  return el('div', { class: 'dn-sparkbar-wrap' }, [svg, glyph]);
}

// A row of pass/fail/timeout glyphs — PROPORTIONAL (true circles, no oval
// distortion). The round status marks must NOT inherit the trellis cell's
// non-uniform width stretch, so each glyph is a FIXED 1:1-aspect SVG laid out
// in an HTML flex row (one equal-flex cell per candidate, glyphs aligned under
// their bars). The row still spans the full cell width; only the inner glyphs
// keep their aspect, so a ✓/✕/⏱/○ renders round, never elliptical.
export function genDots(opts) {
  const o = opts || {};
  const cells = Array.isArray(o.cells) ? o.cells : [];
  const h = o.height || 14;
  const row = el('div', { class: 'dn-genrow', role: 'img' });
  // a fixed mark side so the 1:1 viewBox never stretches with the cell width;
  // capped by the row height so dense rows stay compact.
  const mark = Math.max(8, Math.min(h, 14));
  for (const c of cells) {
    const slot = el('span', { class: 'dn-genrow-slot' });
    slot.appendChild(outcomeGlyph(c, mark));
    row.appendChild(slot);
  }
  if (!cells.length) row.appendChild(el('span', { class: 'dn-genrow-slot' }));
  return row;
}

// One fixed-aspect (1:1 viewBox) glyph SVG, so the mark renders as a TRUE
// circle / square-cornered cross regardless of the parent's width stretch.
function outcomeGlyph(d, side) {
  const s = side || 14;
  const svg = svgEl('svg', { class: 'dn-glyph', width: s, height: s, viewBox: '0 0 10 10', preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  const cx = 5, cy = 5;
  if (d && d.ran === false) { svg.appendChild(svgEl('circle', { cx, cy, r: 2.2, class: 'dn-glyph-none' })); return hov(svg, 'no run'); }
  if (d && d.timeout) { svg.appendChild(svgEl('text', { x: cx, y: cy + 3.2, class: 'dn-glyph-timeout', 'text-anchor': 'middle' }, ['⏱'])); return hov(svg, 'budget exceeded (timeout)'); }
  if (d && (d.pass === true || d.pass === 1)) { svg.appendChild(svgEl('circle', { cx, cy, r: 2.6, class: 'dn-glyph-pass' })); return hov(svg, 'passed'); }
  if (d && (d.pass === false || d.pass === 0)) {
    svg.appendChild(svgEl('line', { x1: cx - 2.6, y1: cy - 2.6, x2: cx + 2.6, y2: cy + 2.6, class: 'dn-glyph-fail' }));
    svg.appendChild(svgEl('line', { x1: cx - 2.6, y1: cy + 2.6, x2: cx + 2.6, y2: cy - 2.6, class: 'dn-glyph-fail' }));
    return hov(svg, 'failed');
  }
  svg.appendChild(svgEl('circle', { cx, cy, r: 2.2, class: 'dn-glyph-none' }));
  return hov(svg, 'no predicate');
}

// ---- horizontal value bars (per-judge losses) ----------------------

export function valueBars(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d && isNum(d.value));
  const w = o.width || 360;
  const rh = o.rowHeight || 18;
  const labelW = o.labelWidth || 150;
  const h = Math.max(rh, items.length * rh + 6);
  const svg = svgEl('svg', { class: 'dn-vbars', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMin meet', role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 14, class: 'dn-empty-label' });
    t.textContent = 'no values';
    svg.appendChild(t);
    return svg;
  }
  const hi = Math.max(1e-9, ...items.map((d) => Math.abs(d.value)));
  const x0 = labelW + 4;
  const x = scale([0, hi], [x0, w - 36]);
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 3;
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'dn-dot-label', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(d.label), 20);
    svg.appendChild(lbl);
    const bx = x(Math.abs(d.value));
    svg.appendChild(hov(svgEl('rect', { x: x0, y: cy - 4, width: Math.max(1, bx - x0), height: 8, rx: 1, class: 'dn-vbar' }), `${d.label}: ${fmt(d.value)}`));
    const vt = svgEl('text', { x: bx + 4, y: cy + 3, class: 'dn-vbar-val' });
    vt.textContent = fmt(d.value, 1);
    svg.appendChild(vt);
  });
  return svg;
}

// ---- paired per-board slopegraph (NON-COLLIDING) --------------------

export function pairedSlopegraph(opts) {
  const o = opts || {};
  const series = (Array.isArray(o.series) ? o.series : []).filter((s) => s && (isNum(s.a) || isNum(s.b)));
  const w = o.width || 520;
  const h = o.height || 300;
  const padTop = 28; const padBottom = 18;
  const colGap = o.labelGap || 150;
  const leftX = colGap;
  const rightX = w - colGap;
  const goodDown = (o.goodDirection || 'down') === 'down';

  const svg = svgEl('svg', { class: 'dn-pslope', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMin meet', role: 'img' });
  if (series.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no paired board duels yet';
    svg.appendChild(t);
    return svg;
  }
  const allVals = [];
  for (const s of series) { if (isNum(s.a)) allVals.push(s.a); if (isNum(s.b)) allVals.push(s.b); }
  const [lo, hi] = extent(allVals);
  const y = scale([lo, hi], [h - padBottom, padTop]);

  const hL = svgEl('text', { x: leftX, y: 15, class: 'dn-slope-col', 'text-anchor': 'end' });
  hL.textContent = (o.left && o.left.title) || 'champion';
  const hR = svgEl('text', { x: rightX, y: 15, class: 'dn-slope-col', 'text-anchor': 'start' });
  hR.textContent = (o.right && o.right.title) || 'challenger';
  svg.appendChild(hL); svg.appendChild(hR);
  svg.appendChild(svgEl('line', { x1: leftX, x2: leftX, y1: y(hi), y2: y(lo), class: 'dn-slope-axis' }));
  svg.appendChild(svgEl('line', { x1: rightX, x2: rightX, y1: y(hi), y2: y(lo), class: 'dn-slope-axis' }));

  const minGap = 14;
  const leftY = series.map((s) => (isNum(s.a) ? y(s.a) : (isNum(s.b) ? y(s.b) : (padTop + h - padBottom) / 2)));
  const rightY = series.map((s) => (isNum(s.b) ? y(s.b) : (isNum(s.a) ? y(s.a) : (padTop + h - padBottom) / 2)));
  const leftNode = jitterColumn(leftY, 3.2);
  const rightNode = jitterColumn(rightY, 3.2);
  const leftLabels = decollide(series.map((s) => ({ v: isNum(s.a) ? s.a : s.b })), y, minGap, padTop, h - padBottom);
  const rightLabels = decollide(series.map((s) => ({ v: isNum(s.b) ? s.b : s.a })), y, minGap, padTop, h - padBottom);

  series.forEach((s, i) => {
    const ay = isNum(s.a) ? leftNode[i] : null;
    const by = isNum(s.b) ? rightNode[i] : null;
    const verdict = s.verdict || (isNum(s.a) && isNum(s.b)
      ? (s.b === s.a ? 'flat' : (goodDown ? (s.b < s.a ? 'improved' : 'regressed') : (s.b > s.a ? 'improved' : 'regressed')))
      : 'flat');
    const dirCls = verdict === 'improved' ? 'dn-good' : verdict === 'regressed' ? 'dn-bad' : 'dn-flat';
    const g = svgEl('g', { class: 'dn-pslope-series' });
    if (ay != null && by != null) {
      const line = svgEl('line', { x1: leftX, y1: ay, x2: rightX, y2: by, class: 'dn-pslope-line ' + dirCls });
      hov(line, `${s.label}: ${fmt(s.a)} → ${fmt(s.b)} (${fmtSigned(s.b - s.a)}; ${verdict})`);
      g.appendChild(line);
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'dn-pslope-node ' + dirCls }));
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'dn-pslope-node ' + dirCls }));
    } else if (ay != null) {
      g.appendChild(hov(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'dn-pslope-node dn-flat' }), `${s.label}: champion only ${fmt(s.a)}`));
    } else if (by != null) {
      g.appendChild(hov(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'dn-pslope-node dn-flat' }), `${s.label}: challenger only ${fmt(s.b)}`));
    }
    const ll = leftLabels[i];
    if (isNum(s.a)) {
      if (Math.abs(ll - y(s.a)) > 1.5) g.appendChild(svgEl('line', { x1: leftX - 4, y1: ll, x2: leftX, y2: y(s.a), class: 'dn-leader' }));
      const tx = svgEl('text', { x: leftX - 8, y: ll + 3, class: 'dn-pslope-label', 'text-anchor': 'end' });
      tx.textContent = `${shortLabel(s.label, 14)}  ${fmt(s.a, 1)}`;
      g.appendChild(tx);
    }
    const rl = rightLabels[i];
    if (isNum(s.b)) {
      if (Math.abs(rl - y(s.b)) > 1.5) g.appendChild(svgEl('line', { x1: rightX, y1: y(s.b), x2: rightX + 4, y2: rl, class: 'dn-leader' }));
      const tx = svgEl('text', { x: rightX + 8, y: rl + 3, class: 'dn-pslope-label', 'text-anchor': 'start' });
      tx.textContent = `${fmt(s.b, 1)}  ${shortLabel(s.label, 14)}`;
      g.appendChild(tx);
    }
    clickable(g, o.onClick && (() => o.onClick(s)));
    svg.appendChild(g);
  });
  return svg;
}

// ---- racing SURVIVAL FUNNEL (the at-a-glance epoch hero) -------------
//
// The successive-halving field rendered as a FLOW that narrows at each cut:
//     (absent ⇒ inferred from championId + live).
export function survivalFunnel(opts) {
  const o = opts || {};
  const rungs = (Array.isArray(o.rungs) ? o.rungs : []).filter((r) => r);
  const live = !!o.live;
  const stageW = 150;
  const stageGap = 20;
  const gateW = 132;
  // three stacked header baselines (bench / head / sub) so nothing collides.
  const benchY = 12;
  const headY = 30;
  const subY = 42;
  const top = 56;           // the flow lane begins below all three header rows
  const laneH = 132;        // the vertical band the surviving flow occupies
  const deadH = 18;         // per-eliminated-branch row height below the lane
  // the widest stack of dead-end branches across stages bounds the figure height.
  const maxDead = Math.max(0, ...rungs.map((r) => (Array.isArray(r.cut) ? r.cut.length : 0)));
  // the BENCHMARK (v0, the gate defender) is never a rung lane — but the crowned
  // champion (e.g. a surviving rung competitor) IS, so exclude the benchmark
  // ONLY, never the championId (the model already drops v0 from rung
  // competitors; this guards a caller that still lists it).
  const benchExcl = (o.benchmarkId != null) ? String(o.benchmarkId) : null;
  const w = rungs.length * stageW + Math.max(0, rungs.length - 1) * stageGap + stageGap + gateW + 8;
  const h = top + laneH + maxDead * deadH + 26;
  const svg = svgEl('svg', applyResponsive({
    class: 'dn-funnel', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
  }, o, w, h, 'dn-funnel-hero'));
  if (rungs.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no rungs yet';
    svg.appendChild(t);
    return svg;
  }
  // CHAMPION / v0 BENCHMARK caption — make explicit that the field is raced vs
  // the reigning champion (v0), that every Δ is vs v0, and that v0 defends at
  // the gate. v0 is the benchmark, not one of the rung competitors.
  const benchId = o.benchmarkId != null ? String(o.benchmarkId)
    : (o.championId != null ? String(o.championId) : null);
  if (benchId) {
    const bt = hov(svgEl('text', { x: 2, y: benchY, class: 'dn-funnel-bench' }),
      `champion v0 = ${benchId} · the field is raced vs this benchmark; every Δ is vs v0 · v0 defends at the champion-gate`);
    bt.textContent = `▸ vs champion v0 = ${shortLabel(benchId, 18)} · every Δ is vs v0`;
    svg.appendChild(bt);
  }
  const midY = top + laneH / 2;
  // the entering field of stage 0 sets the maximum flow width (100% lane). Drive
  // it from the FULL field (every lane racing rung 0), not just the first
  // matchup's competitors, so a wide entering rung reads at full width.
  const field0 = Math.max(1, rungFieldLanes(rungs[0], benchExcl).length || 1);
  // a stage's flow band half-height ∝ its entering field size.
  const bandHalf = (n) => Math.max(6, (laneH / 2) * (Math.max(0, n) / field0));
  const stageX = (j) => j * (stageW + stageGap) + 2;

  // ── the flowing band: one trapezoid per stage, narrowing at each cut ──
  // entering width = |competitors|; leaving width = |survivors| (the field
  // carried to the next stage). A pending (live, undecided) stage keeps a
  rungs.forEach((rung, j) => {
    // the FULL entering field of this rung (every lane racing it), per the shared
    // contract: live_progress keys ∪ competitors ∪ survivors/cut, minus the
    // champion/benchmark — so a multi-survivor rung shows ALL lanes, not just the
    // first matchup's competitors.
    const comps = rungFieldLanes(rung, benchExcl);
    const cut = new Set(Array.isArray(rung.cut) ? rung.cut.map(String) : []);
    const surv = Array.isArray(rung.survivors) ? rung.survivors.map(String) : [];
    const pending = !!rung.pending || (cut.size === 0 && surv.length === 0);
    const enterN = comps.length;
    const leaveN = pending ? enterN : surv.length;
    const x0 = stageX(j);
    const x1 = x0 + stageW;
    const hIn = bandHalf(enterN);
    const hOut = bandHalf(leaveN);
    const cls = 'dn-funnel-band' + (pending ? ' dn-funnel-pending' : '');
    // a trapezoid: left edge full enter-height, right edge narrowed to leave-height.
    svg.appendChild(hov(svgEl('polygon', {
      points: `${x0},${midY - hIn} ${x1},${midY - hOut} ${x1},${midY + hOut} ${x0},${midY + hIn}`,
      class: cls,
    }), `${rung.label || 'rung ' + j}: ${enterN} in → ${pending ? '…' : leaveN + ' survive'}${isNum(rung.board_fraction) ? ` · ${(rung.board_fraction * 100).toFixed(0)}% board` : ''}`));
    // stage label + board fraction above the band — on the dedicated header /
    // sub baselines (headY / subY), CENTRED on this stage's column x, so they
    // never overlap each other, an adjacent column, or the benchmark line.
    const head = svgEl('text', { x: x0 + stageW / 2, y: headY, class: 'dn-funnel-head', 'text-anchor': 'middle' });
    head.textContent = shortLabel(rung.label || `Rung ${j}`, 16);
    svg.appendChild(head);
    const sub = svgEl('text', { x: x0 + stageW / 2, y: subY, class: 'dn-funnel-sub', 'text-anchor': 'middle' });
    sub.textContent = `${enterN} field` + (isNum(rung.board_fraction) ? ` · ${(rung.board_fraction * 100).toFixed(0)}/100 board` : '');
    svg.appendChild(sub);

    // ── the surviving runners ride INSIDE the band (↑), clickable ──
    // a LIVE racing rung carries a per-lane `live_progress` map; an active
    // (not queued) lane reads "racing · k/N boards" + a partial Δ-vs-champion
    // and grows a thin in-flight progress bar as boards land.
    const prog = (rung.live_progress && typeof rung.live_progress === 'object') ? rung.live_progress : null;
    // a pending (live) rung shows the FULL field racing (every lane); a settled
    // rung shows the survivors riding the band (the cut peel off below).
    const survRunners = pending ? comps.slice() : surv;
    survRunners.forEach((sid, i) => {
      const cy = survRunners.length === 1 ? midY
        : midY - hOut + 8 + (i * (Math.max(1, 2 * hOut - 16)) / Math.max(1, survRunners.length - 1));
      const lane = prog ? prog[String(sid)] : null;
      funnelRunner(svg, o, sid, rung, j, x0 + 8, cy, pending ? 'racing' : 'survives', lane, stageW - 16);
    });

    // ── eliminated competitors peel off as labelled dead-end branches (✕) ──
    if (!pending) {
      [...cut].forEach((cid, i) => {
        const sid = String(cid);
        const branchY = top + laneH + 6 + i * deadH;
        const elbowX = x0 + stageW * 0.5;
        // anchor each branch ON the band's lower edge at the elbow x so it peels
        // off the funnel with no gap. The lower edge runs from (x0, midY+hIn) to
        // (x1, midY+hOut); at fraction f along the stage its y is interpolated.
        const f = (elbowX - x0) / stageW;
        const edgeYAtElbow = midY + hIn + (hOut - hIn) * f;
        const labelX = elbowX + 12;
        // a dead-end branch that drops from the band's lower edge and then a SHORT
        // stub that stops just LEFT of the label — the connector must lead INTO
        // the cut name, never run through it (it used to extend the full stage
        // width at the label's own baseline, slashing across the text).
        svg.appendChild(svgEl('path', {
          d: `M${elbowX},${edgeYAtElbow} V${branchY} H${labelX - 4}`,
          class: 'dn-funnel-deadedge', fill: 'none',
        }));
        funnelRunner(svg, o, sid, rung, j, labelX, branchY, 'cut');
      });
    }
  });

  // ── the terminal CHAMPION-GATE ──
  const finalSurv = (() => {
    for (let i = rungs.length - 1; i >= 0; i--) {
      const s = Array.isArray(rungs[i].survivors) ? rungs[i].survivors.map(String) : [];
      if (s.length) return s;
    }
    return [];
  })();
  const champId = o.championId ? String(o.championId) : null;
  const gateState = o.gateState || (live ? 'deciding' : (champId ? 'crowned' : 'pending'));
  const crowned = gateState === 'crowned' && !!champId;
  const seatId = champId || (finalSurv.length === 1 ? finalSurv[0] : null);
  const gx = rungs.length * stageW + Math.max(0, rungs.length - 1) * stageGap + stageGap + 2;
  // the converging flow from the last stage's surviving band into the gate.
  const lastLeave = (() => {
    const r = rungs[rungs.length - 1];
    const c = rungFieldLanes(r, benchExcl);
    const s = Array.isArray(r.survivors) ? r.survivors.map(String) : [];
    const pend = !!r.pending || (s.length === 0 && (!Array.isArray(r.cut) || r.cut.length === 0));
    return pend ? c.length : s.length;
  })();
  const flowH = bandHalf(lastLeave);
  const lastX = stageX(rungs.length - 1) + stageW;
  svg.appendChild(svgEl('polygon', {
    points: `${lastX},${midY - flowH} ${gx},${midY - 11} ${gx},${midY + 11} ${lastX},${midY + flowH}`,
    class: 'dn-funnel-band dn-funnel-gateflow' + (crowned ? ' dn-good' : ''),
  }));
  const gHead = svgEl('text', { x: gx + gateW / 2, y: headY, class: 'dn-funnel-head', 'text-anchor': 'middle' });
  gHead.textContent = 'champion-gate';
  svg.appendChild(gHead);
  const gSub = svgEl('text', { x: gx + gateW / 2, y: subY, class: 'dn-funnel-sub', 'text-anchor': 'middle' });
  gSub.textContent = benchId ? 'full board · vs champion v0' : 'full board · vs champion';
  svg.appendChild(gSub);

  const clickId = champId || seatId;
  const gateG = svgEl('g', { class: 'dn-funnel-gate', tabindex: (clickId && o.onCompetitor) ? '0' : null });
  gateG.appendChild(svgEl('rect', {
    x: gx, y: midY - 16, width: gateW, height: 32, rx: 5,
    class: 'dn-funnel-gatebox' + (crowned ? ' dn-good' : ''),
  }));
  const dStr = isNum(o.gateDelta) ? ` · Δ ${fmtSigned(o.gateDelta, 2)}` : '';
  let label;
  let tip;
  if (crowned) {
    label = CROWN.current + ' ' + shortLabel(champId, 12);
    tip = `${champId} cleared the full-board gate → crowned champion${dStr}`;
  } else if (gateState === 'stands') {
    label = 'champion stands';
    tip = `the survivor lost the full-board gate — champion stands${dStr}`;
  } else if (gateState === 'deciding') {
    label = 'deciding…';
    tip = champId ? `${champId} leads — the gate has not committed${dStr}` : 'the final gate is deciding';
  } else {
    label = 'tbd';
    tip = 'awaiting the final survivor';
  }
  const gt = hov(svgEl('text', { x: gx + gateW / 2, y: midY + 4, class: 'dn-funnel-gatelab' + (crowned ? ' dn-good' : ''), 'text-anchor': 'middle' }), tip);
  gt.textContent = label;
  gateG.appendChild(gt);
  clickable(gateG, (clickId && o.onCompetitor) && (() => o.onCompetitor(clickId)));
  svg.appendChild(gateG);
  return svg;
}

// One funnel competitor label (a survivor riding the band, or a peeled-off
// eliminated dead-end). Hover → its per-rung Δ + cut/survive verdict; click →
// its candidate. `verdict` ∈ {survives, cut, racing}. A LIVE racing lane passes
// its `lane` ({inflight, done, total, partialDelta}) so the runner reads
// "racing · k/N boards" + a partial Δ and grows an in-flight progress bar
// (`barW` is the band-bounded bar width); `lane` is null for settled/non-live.
function funnelRunner(svg, o, sid, rung, j, x, cy, verdict, lane, barW) {
  // partial Δ-vs-champion (live) falls back to the committed rung Δ.
  const partial = lane && isNum(lane.partialDelta) ? lane.partialDelta : null;
  const delta = (rung.deltas && isNum(rung.deltas[sid])) ? rung.deltas[sid] : partial;
  const glyph = verdict === 'cut' ? ' ✕' : verdict === 'survives' ? ' ↑' : '';
  // a LIVE racing lane with a server-side PROJECTED standing reads as projected:
  // a ~prefix on the scalar + a "proj" suffix + the dashed/dimmed dn-proj
  // treatment + a SCORED board-progress sub-bar, distinct from a settled lane.
  const projected = !!(lane && lane.projected && verdict === 'racing');
  // a live racing lane appends its "k/N boards" progress to the label.
  const laneSuffix = (verdict === 'racing' && lane) ? ' · ' + laneProgressText(lane) : '';
  const projSuffix = projected && isNum(lane.projected_scalar)
    ? ' · ~' + fmt(lane.projected_scalar, 1) + ' proj' : '';
  const cls = 'dn-funnel-name'
    + (verdict === 'cut' ? ' dn-out dn-bad' : verdict === 'survives' ? ' dn-good' : ' dn-racing')
    + (projected ? ' dn-proj' : '');
  const tip = `${sid} · ${rung.label || 'rung ' + j}`
    + (isNum(rung.board_fraction) ? ` · ${(rung.board_fraction * 100).toFixed(0)}% board` : '')
    + (projected && isNum(lane.projected_scalar) ? ` · projected scalar ~${fmt(lane.projected_scalar, 2)} (boards still streaming)` : '')
    + (delta != null ? ` · Δ ${fmtSigned(delta, 2)} vs champion` : '')
    + (laneSuffix ? ` · ${laneProgressText(lane)}` : '')
    + ` · ${projected ? 'projected' : verdict}`;
  const g = svgEl('g', { class: 'dn-funnel-runner', tabindex: o.onCompetitor ? '0' : null });
  const t = hov(svgEl('text', { x, y: cy + 3, class: cls }), tip);
  t.textContent = shortLabel(sid, lane ? 8 : 13) + glyph + laneSuffix + projSuffix;
  g.appendChild(t);
  // a thin SCORED board-progress sub-bar under a live lane (boards done / total).
  // A projected lane draws it in the projected (dashed/amber) treatment; a plain
  // live lane keeps the accent in-flight bar.
  if (lane && (lane.inflight || lane.done || projected)) {
    const bw = Math.max(20, barW || 80);
    // prefer the scored boards_done/boards_total when present (the projected
    // standing's own progress); else the live activeRuns done/total tally.
    const sd = isNum(lane.boards_done) ? lane.boards_done : lane.done;
    const stot = isNum(lane.boards_total) ? lane.boards_total : lane.total;
    const frac = (isNum(stot) && stot > 0)
      ? Math.min(1, (sd || 0) / stot)
      : (lane.inflight ? 0.5 : 0);
    if (projected) {
      g.appendChild(svgEl('rect', { x, y: cy + 5, width: bw, height: 2.4, rx: 1, class: 'dn-proj-bar-bg' }));
      g.appendChild(svgEl('rect', { x, y: cy + 5, width: Math.max(1, bw * frac), height: 2.4, rx: 1, class: 'dn-proj-bar' }));
    } else {
      g.appendChild(svgEl('rect', { x, y: cy + 5, width: bw, height: 2, rx: 1, class: 'dn-funnel-bar-bg' }));
      g.appendChild(svgEl('rect', { x, y: cy + 5, width: Math.max(1, bw * frac), height: 2, rx: 1,
        class: 'dn-funnel-bar' + (lane.inflight ? ' dn-funnel-bar-live' : '') }));
    }
  }
  clickable(g, o.onCompetitor && (() => o.onCompetitor(sid)));
  svg.appendChild(g);
}

// ---- swiss STANDINGS LADDER (DATA-DRIVEN, live + completed) ----------
//
// The swiss analogue of the racing survivalFunnel: a column per round (its
//   gateState ∈ 'crowned'|'stands'|'deciding'|'pending' (else inferred).
export function swissLadder(opts) {
  const o = opts || {};
  const rounds = (Array.isArray(o.rounds) ? o.rounds : []).filter((r) => r);
  const standings = (Array.isArray(o.standings) ? o.standings : []).filter((s) => s);
  const live = !!o.live;
  const colW = 150;
  const colGap = 22;
  const standW = 150;
  const gateW = 124;
  const pairH = 30;
  const headH = 32;
  const top = 8;
  const benchId = o.benchmarkId != null ? String(o.benchmarkId)
    : (o.championId != null ? String(o.championId) : null);
  const benchH = benchId ? 16 : 0;
  const maxPairs = Math.max(1, ...rounds.map((r) => (Array.isArray(r.pairings) ? r.pairings.length : 0)), 1);
  const maxRows = Math.max(maxPairs, standings.length, 1);
  const ladderW = rounds.length * colW + Math.max(0, rounds.length - 1) * colGap;
  // a standings column + a champion-gate column ride after the round columns.
  const w = Math.max(colW, ladderW + colGap + standW + colGap + gateW) + 8;
  const h = top + benchH + headH + maxRows * pairH + 8;
  const svg = svgEl('svg', applyResponsive({
    class: 'dn-swissladder', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
  }, o, w, h, 'dn-swissladder-hero'));
  if (benchId) {
    const bt = hov(svgEl('text', { x: 4, y: top + 10, class: 'dn-swissladder-bench' }),
      `incumbent champion = ${benchId} · the swiss winner must beat the incumbent at the champion-gate to be promoted`);
    bt.textContent = `▸ incumbent champion = ${shortLabel(benchId, 18)} · defends at the gate`;
    svg.appendChild(bt);
  }
  if (rounds.length === 0 && standings.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no swiss rounds yet';
    svg.appendChild(t);
    return svg;
  }
  const headTop = top + benchH;
  const colX = (j) => j * (colW + colGap) + 2;
  const rowY = (i) => headTop + headH + i * pairH + pairH / 2;

  // ── round columns: each round's pairings (a vs b → winner) ──
  rounds.forEach((rnd, j) => {
    const x = colX(j);
    const queued = !!rnd.queued;
    const head = svgEl('text', { x: x + colW / 2, y: headTop + 12, class: 'dn-swissladder-head' + (queued ? ' dn-swissladder-queued' : ''), 'text-anchor': 'middle' });
    head.textContent = shortLabel(rnd.label || `Round ${j + 1}`, 16) + (queued ? ' · queued' : '');
    svg.appendChild(head);
    const pairings = Array.isArray(rnd.pairings) ? rnd.pairings : [];
    pairings.forEach((p, i) => {
      const cy = rowY(i);
      const decided = !!p.winner && !p.pending;
      const inflight = !!p.inflight || (isNum(p.total) && isNum(p.done) && p.done < p.total && !decided);
      const g = svgEl('g', { class: 'dn-swissladder-pair' + (queued ? ' dn-swissladder-lane-queued' : ''), tabindex: o.onCompetitor ? '0' : null });
      const a = p.a == null ? 'bye' : String(p.a);
      const b = p.bye ? 'bye' : (p.b == null ? '—' : String(p.b));
      const aWon = decided && p.winner === p.a;
      const bWon = decided && p.winner === p.b;
      const progText = decided ? (p.winner === p.a ? ` · ${shortLabel(a, 8)} ↑` : ` · ${shortLabel(String(p.winner), 8)} ↑`)
        : queued ? ' · queued'
        : inflight ? ' · running' + (isNum(p.total) && p.total > 0 ? ` ${p.done || 0}/${p.total}` : (p.inflight ? ` · ${p.inflight} board${p.inflight === 1 ? '' : 's'}` : ''))
        : ' · pairing';
      const cls = 'dn-swissladder-pairlab' + (queued ? ' dn-swissladder-queued' : (inflight ? ' dn-racing' : ''));
      const t = hov(svgEl('text', { x: x + 6, y: cy + 3, class: cls }),
        `${a} vs ${b}${decided ? ' → ' + p.winner : ''}${isNum(p.delta) ? ` · Δ ${fmtSigned(p.delta, 2)}` : ''}`);
      const aCls = aWon ? ' ↑' : '';
      t.textContent = shortLabel(a, 6) + ' v ' + shortLabel(b, 6) + (decided ? '' : progText.replace(' · pairing', ''));
      g.appendChild(t);
      if (decided) {
        const sub = svgEl('text', { x: x + 6, y: cy + 13, class: 'dn-swissladder-win dn-good' });
        sub.textContent = shortLabel(String(p.winner), 10) + ' ↑';
        g.appendChild(sub);
      } else if (inflight) {
        const barW = colW - 12;
        const frac = (isNum(p.total) && p.total > 0) ? Math.min(1, (p.done || 0) / p.total) : 0.5;
        g.appendChild(svgEl('rect', { x: x + 6, y: cy + 7, width: barW, height: 2, rx: 1, class: 'dn-swissladder-bar-bg' }));
        g.appendChild(svgEl('rect', { x: x + 6, y: cy + 7, width: Math.max(1, barW * frac), height: 2, rx: 1, class: 'dn-swissladder-bar dn-swissladder-bar-live' }));
      }
      { const open = p.winner || p.a || p.b;
        clickable(g, (o.onCompetitor && open) && (() => o.onCompetitor(String(open)))); }
      svg.appendChild(g);
    });
  });

  // ── the accumulating Copeland-point standings column ──
  const sx = ladderW + colGap + 2;
  const sHead = svgEl('text', { x: sx + standW / 2, y: headTop + 12, class: 'dn-swissladder-head', 'text-anchor': 'middle' });
  sHead.textContent = 'standings';
  svg.appendChild(sHead);
  const leaderId = standings.length ? String(standings[0].id) : null;
  // distinguish the NEW champion (♛, accent) from the displaced incumbent
  // (♔ "former", dim). A bare round-leader gets ♔ only while no champion is
  // crowned yet (live).
  const ladChampId = o.championId ? String(o.championId) : null;
  const ladBenchId = o.benchmarkId != null ? String(o.benchmarkId) : null;
  const ladFormerId = (ladChampId && ladBenchId && ladBenchId !== ladChampId) ? ladBenchId : null;
  standings.forEach((s, i) => {
    const cy = rowY(i);
    const sid = String(s.id);
    const isChamp = sid === ladChampId;
    const isFormer = sid === ladFormerId;
    const isLeader = sid === leaderId && !ladChampId;
    const emph = isChamp || isLeader;
    // PROJECTED — an in-flight competitor's mean-scalar is projected (Copeland
    // points are NOT — a half-finished duel has crowned no winner). Mark the
    // row "projected" (dashed/~) but never re-rank it on the projection.
    const proj = !!(s.in_flight && isNum(s.projected_scalar));
    const g = svgEl('g', { class: 'dn-swissladder-stand' + (proj ? ' dn-proj' : ''), tabindex: o.onCompetitor ? '0' : null });
    const lab = hov(svgEl('text', { x: sx + 6, y: cy + 3, class: 'dn-swissladder-standlab' + (emph ? ' dn-good' : (isFormer ? ' dn-faint' : '')) + (proj ? ' dn-proj' : '') }),
      `${sid} · ${isNum(s.points) ? fmt(s.points, 1) : '?'} pts · ${s.wins || 0}W ${s.draws || 0}D ${s.losses || 0}L${isFormer ? ' · former champion' : ''}${proj ? ` · projected scalar ~${fmt(s.projected_scalar, 2)} (boards streaming; points not projected)` : ''}`);
    lab.textContent = `${i + 1}. ${shortLabel(sid, 9)}` + (isChamp ? ' ' + CROWN.current : (isFormer || isLeader ? ' ' + CROWN.former : '')) + (proj ? ' ~proj' : '');
    g.appendChild(lab);
    const pts = svgEl('text', { x: sx + standW - 6, y: cy + 3, 'text-anchor': 'end', class: 'dn-swissladder-pts' + (emph ? ' dn-good' : '') });
    pts.textContent = isNum(s.points) ? fmt(s.points, s.points % 1 ? 1 : 0) : '—';
    g.appendChild(pts);
    // a SCORED board-progress sub-bar for a projected row (boards_done/total).
    if (proj && isNum(s.boards_total) && s.boards_total > 0) {
      const barW = standW - 12;
      const frac = Math.min(1, (s.boards_done || 0) / s.boards_total);
      g.appendChild(svgEl('rect', { x: sx + 6, y: cy + 7, width: barW, height: 2.4, rx: 1, class: 'dn-proj-bar-bg' }));
      g.appendChild(svgEl('rect', { x: sx + 6, y: cy + 7, width: Math.max(1, barW * frac), height: 2.4, rx: 1, class: 'dn-proj-bar' }));
    }
    clickable(g, o.onCompetitor && (() => o.onCompetitor(sid)));
    svg.appendChild(g);
  });

  // ── the champion-gate column (the leader vs the incumbent) ──
  const champId = o.championId ? String(o.championId) : null;
  const gateState = o.gateState || (live ? 'deciding' : (champId ? 'crowned' : 'pending'));
  const crowned = gateState === 'crowned' && !!champId;
  const gx = sx + standW + colGap;
  const gateHead = svgEl('text', { x: gx + gateW / 2, y: headTop + 12, class: 'dn-swissladder-head', 'text-anchor': 'middle' });
  gateHead.textContent = 'champion-gate';
  svg.appendChild(gateHead);
  const cy = rowY(0);
  if (leaderId) {
    const x1 = sx + standW;
    const mx = (x1 + gx) / 2;
    svg.appendChild(svgEl('path', { d: `M${x1},${cy} H${mx} V${cy} H${gx}`, class: 'dn-swissladder-edge' + (crowned ? ' dn-swissladder-edge-champ' : ''), fill: 'none' }));
  }
  const clickId = champId || leaderId;
  const gateG = svgEl('g', { class: 'dn-swissladder-gate', tabindex: (clickId && o.onCompetitor) ? '0' : null });
  gateG.appendChild(svgEl('rect', { x: gx, y: cy - pairH / 2, width: gateW, height: pairH, rx: 4, class: 'dn-swissladder-gatebox' + (crowned ? ' dn-good' : '') }));
  const dStr = isNum(o.gateDelta) ? ` · Δ ${fmtSigned(o.gateDelta, 2)}` : '';
  let label;
  let tip;
  if (crowned) { label = CROWN.current + ' ' + shortLabel(champId, 11); tip = `${champId} won the swiss + cleared the gate → new champion${dStr}`; }
  else if (gateState === 'stands') { label = 'champion stands'; tip = `the swiss winner did not beat the incumbent — champion stands${dStr}`; }
  else if (gateState === 'deciding') { label = 'deciding…'; tip = leaderId ? `${leaderId} leads — gate not yet committed` : 'the gate is deciding'; }
  else { label = 'tbd'; tip = 'awaiting the swiss leader'; }
  const gt = hov(svgEl('text', { x: gx + 6, y: cy + 3, class: 'dn-swissladder-gatelab' + (crowned ? ' dn-good' : '') }), tip);
  gt.textContent = label;
  gateG.appendChild(gt);
  clickable(gateG, (clickId && o.onCompetitor) && (() => o.onCompetitor(String(clickId))));
  svg.appendChild(gateG);
  return svg;
}

// ---- elim FLOW (Tufte slopegraph / bipartite — generations across rounds) --
//
// The COMPANION to the bracket tree on the generations-overview page (Task 3):
// the elimination analogue of the racing survival funnel. The tree shows
// who-played-whom; this shows each generation's SURVIVAL TRAJECTORY through the
// rounds.
//
//   * ROUNDS are columns (R0 · R1 · … · champion-gate).
//   * ONE LANE per generation (a horizontal row).
//   * a generation's line CONTINUES to the next column when it WON (advanced),
//     drawn with --v2-good; it TERMINATES with ✕ (--v2-bad) when ELIMINATED.
//   * the champion's line reaches the gate marked with CROWN.current; the
//     displaced incumbent (benchmark) reads CROWN.former.
//   * a still-pending (live) leg is drawn dashed (the pending convention).
//
// Derived PURELY from elimModel(st)'s winners rounds + competitors (single
// source — no new data path). `opts`:
//   { winners:[{label, matches:[{competitors, winner, decision, pending, bye}]}],
//     championId, benchmarkId, gateState, live, onCompetitor(id) }
export function elimFlow(opts) {
  const o = opts || {};
  // COLUMN ORDER must be TEMPORAL (by round_index), not the caller's band
  // concatenation order. The double-elim caller passes winners.concat(losers),
  // which lists the GRAND FINAL (a winners' band) BEFORE the losers' bracket
  // rounds — so the losers' columns rendered to the RIGHT of the gate-bound
  // final, the winners→losers DROP edges pointed backwards, and a dropped lane's
  // dots were left orphaned. Sorting by round_index restores WB → LB → GF order
  // so every advancement / drop edge runs left-to-right into its real target.
  const rounds = (Array.isArray(o.winners) ? o.winners : [])
    .filter((r) => r && Array.isArray(r.matches))
    .map((r, i) => ({ r, i }))
    .sort((a, b) => {
      const ra = isNum(a.r.round_index) ? a.r.round_index : a.i;
      const rb = isNum(b.r.round_index) ? b.r.round_index : b.i;
      return ra - rb || a.i - b.i;
    })
    .map((x) => x.r);
  const live = !!o.live;
  const champId = o.championId != null ? String(o.championId) : null;
  const benchId = o.benchmarkId != null ? String(o.benchmarkId) : null;

  // ── derive each generation's per-round state from the winners rounds ──
  // For each round we record, per competitor that PLAYED in it: advanced (won),
  // eliminated (lost a decided match), or pending (the match is still in flight).
  // R = rounds.length columns + 1 gate column.
  const nCols = rounds.length;
  // gen id → { firstCol, lastCol, eliminatedAt, advancedThrough:Set, pendingAt:Set }
  const genState = new Map();
  const ensure = (id) => {
    const k = String(id);
    if (!genState.has(k)) genState.set(k, { id: k, played: new Set(), advanced: new Set(), lostAt: new Set(), eliminatedAt: null, pendingAt: new Set(),
      // double-elim: which band columns a lane plays in, keyed by side, + the
      // exact column it RE-ENTERS the losers' bracket (the first LB column it
      // plays) so a WB→LB drop can route into that node's TOP.
      sideOf: new Map(), lbEntryCol: null });
    return genState.get(k);
  };
  // the per-round MATCHES (a two-lane convergence each): two competitors meet, the
  // winner's lane continues, the loser's terminates. Captured here so the figure
  // can draw the bracket-as-flow convergence node + carry the pairing onto HOVER.
  const matchesByCol = rounds.map(() => []);
  // double-elim: each column's bracket side (WB / LB), inferred from its matches'
  // bracket_slot. A column with any LB-slotted match is an LB (losers') column.
  const colSide = rounds.map(() => 'WB');
  let anyLB = false;
  rounds.forEach((r, ci) => {
    for (const m of (Array.isArray(r.matches) ? r.matches : [])) {
      const comps = (Array.isArray(m.competitors) ? m.competitors : []).map(String).filter((c) => c && c !== 'tbd');
      const winner = m.winner ? String(m.winner) : null;
      const pending = !!m.pending || (!winner && !m.bye && !m.decision);
      const slot = String(m.bracket_slot || '');
      const isLB = slot.startsWith('LB');
      if (isLB) { colSide[ci] = 'LB'; anyLB = true; }
      // a real two-lane convergence (not a bye / placeholder) is recorded for the
      // match-node layer; a winner+loser pair, with the live state per leg.
      if (comps.length >= 2 && !m.bye) {
        const loser = winner ? comps.find((c) => c !== winner) || null : null;
        matchesByCol[ci].push({ comps, winner, loser, pending, delta: isNum(m.delta_scalar) ? m.delta_scalar : null,
          slot: m.bracket_slot || m.match_id || '', isLB,
          // the per-side live PROJECTED standing on an in-flight match.
          projected: (m.projected && typeof m.projected === 'object') ? m.projected : null });
      }
      for (const c of comps) {
        const g = ensure(c);
        g.played.add(ci);
        g.sideOf.set(ci, isLB ? 'LB' : 'WB');
        if (isLB && g.lbEntryCol == null) g.lbEntryCol = ci;
        if (pending) { g.pendingAt.add(ci); continue; }
        if (m.bye) { g.advanced.add(ci); continue; }
        if (winner && c === winner) g.advanced.add(ci);
        else if (winner) g.lostAt.add(ci);   // a decided loss in THIS column
      }
    }
  });
  const isDouble = anyLB;
  // ELIMINATION vs DROP (double-elim correctness): a generation is ELIMINATED at
  // a column only when it lost there AND never plays again in a LATER column. An
  // earlier loss that is followed by a later appearance is a winners→losers DROP
  // (the "second life"), not a termination — so it must keep its lane, connect to
  // its losers'-bracket entry by a drop edge, and NOT draw a phantom ✕ in the WB.
  // A single-elim loss has no later column, so it stays a true elimination.
  for (const g of genState.values()) {
    const lost = [...g.lostAt].sort((a, b) => a - b);
    const lastPlayed = g.played.size ? Math.max(...g.played) : -1;
    for (const ci of lost) {
      if (ci >= lastPlayed) { g.eliminatedAt = ci; break; }  // no later column → eliminated here
    }
  }
  // per-generation live PROJECTED standing (from an in-flight match's
  // `projected` map): the latest column's projected row wins. Drives the
  // lane's "projected" treatment (dashed/~prefix) + scored sub-bar.
  const projByGen = new Map();
  matchesByCol.forEach((matches) => {
    for (const m of matches) {
      if (!m.projected || !m.pending) continue;
      for (const c of m.comps) {
        const p = m.projected[c];
        if (p && isNum(p.scalar)) projByGen.set(String(c), p);
      }
    }
  });
  const gens = [...genState.values()];
  // order lanes: survivors / champion first (by deepest round reached), then the
  // earlier-eliminated; the champion lane floats to the top.
  const reach = (g) => (g.eliminatedAt == null ? nCols + 1 : g.eliminatedAt);
  gens.sort((a, b) => reach(b) - reach(a)
    || (a.id === champId ? -1 : b.id === champId ? 1 : 0)
    || a.id.localeCompare(b.id));
  // lane index per generation id — so a match can draw a convergence between the
  // two competitors' lanes (winner above/below the loser, whichever order).
  const laneOf = new Map();
  gens.forEach((g, li) => laneOf.set(g.id, li));

  // ── WB→LB DEMOTION EDGES (pre-pass) ──
  // A dropped lane (lost a WB column, plays again in a later LB column) threads
  // from its WB-loss dot into its LB re-entry node. These connectors used to dip
  // a half-row beneath the SOURCE lane and run across there — straight through the
  // rows of every lane physically between the two columns, and, with two losers
  // demoted from one node, across each other. We instead route ALL of them through
  // a reserved CHANNEL below the whole stack, each on its own horizontal lane.
  // Collected here (before geometry) so the channel count sizes the figure.
  const demotions = [];
  if (isDouble) for (const g of genState.values()) {
    const cols = [...g.played].sort((a, b) => a - b);
    for (const ci of cols) {
      // a DROP is a non-terminal WB loss (lost here, but it is NOT the lane's true
      // elimination column → it plays on). A lane that is LATER eliminated in the
      // LB still made this WB→LB drop, so it must be collected too.
      const dropped = g.lostAt.has(ci) && g.eliminatedAt !== ci;
      if (!dropped) continue;
      const nextCi = cols.find((c) => c > ci);
      if (nextCi == null || colSide[nextCi] !== 'LB') continue;  // only WB→LB drops use the channel
      demotions.push({ id: g.id, fromCol: ci, toCol: nextCi, lane: laneOf.get(g.id) });
    }
  }
  // assign each demotion a distinct channel lane. Order by (source column, lane)
  // so an upper/earlier drop takes the shallower channel and the runs nest rather
  // than cross; a per-edge horizontal nudge keeps two drops that share a source
  // column (the two-loser case) on parallel, non-overlapping verticals.
  demotions.sort((a, b) => a.fromCol - b.fromCol || a.toCol - b.toCol || a.lane - b.lane);
  const chSlot = new Map();   // id+'|'+fromCol+'|'+toCol → channel index
  demotions.forEach((d, k) => chSlot.set(`${d.id}|${d.fromCol}|${d.toCol}`, k));
  const nCh = demotions.length;

  // ── geometry: columns × lanes, fit-to-width ──
  const colW = 116;
  const padL = 16;
  const padR = 116;          // gutter for the lane labels + gate marks
  const top = 30;
  const laneH = 22;
  // the reserved demotion CHANNEL gutter under the lane stack: one lane per drop.
  const chGap = 7;           // vertical spacing between channel lanes
  const chPad = 12;          // clearance between the last lane row and the channel
  const channelTop = top + Math.max(1, gens.length) * laneH + chPad;
  const channelY = (k) => channelTop + k * chGap;
  const w = padL + Math.max(1, nCols) * colW + padR + 8;
  const h = (nCh > 0 ? channelY(nCh - 1) + 12 : top + Math.max(1, gens.length) * laneH + 18);
  const svg = svgEl('svg', applyResponsive({
    class: 'dn-elimflow', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
  }, o, w, h, 'dn-elimflow-hero'));
  if (!nCols || !gens.length) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no bracket rounds yet';
    svg.appendChild(t);
    return svg;
  }
  const colX = (ci) => padL + ci * colW + 8;       // a round column's node x
  const gateX = padL + nCols * colW + 8;           // the champion-gate column x
  const laneY = (li) => top + li * laneH + laneH / 2;

  // ── double-elim: WB / LB tinted bands behind the columns ──
  // The winners' (●● two-lives) and losers' (●○ one-life) columns are tinted
  // into two clearly-distinguished bands so the bracket side reads at a glance
  // (reproducing the study opt-7 banding language). The bands run the full lane
  // height behind each column group; a side glyph rides each band's first column.
  if (isDouble) {
    // contiguous runs of same-side columns → one band rect each.
    let s = 0;
    while (s < nCols) {
      let e = s;
      while (e + 1 < nCols && colSide[e + 1] === colSide[s]) e++;
      const x0 = colX(s) - colW / 2 + 6;
      const x1 = colX(e) + colW / 2 - 6;
      const bandCls = 'dn-elimflow-band ' + (colSide[s] === 'LB' ? 'dn-elimflow-band-lb' : 'dn-elimflow-band-wb');
      svg.appendChild(svgEl('rect', { x: x0, y: top - 6, width: Math.max(2, x1 - x0), height: gens.length * laneH + 12, rx: 6, class: bandCls }));
      const glyph = svgEl('text', { x: colX(s), y: top - 22, class: 'dn-elimflow-bandlab ' + (colSide[s] === 'LB' ? 'dn-elimflow-band-lb' : 'dn-elimflow-band-wb'), 'text-anchor': 'middle' });
      glyph.textContent = colSide[s] === 'LB' ? "●○ losers'" : "●● winners'";
      svg.appendChild(glyph);
      s = e + 1;
    }
  }

  // round-axis headers (R0 · R1 · … · champion-gate).
  rounds.forEach((r, ci) => {
    const hx = colX(ci);
    const head = svgEl('text', { x: hx, y: top - 12, class: 'dn-elimflow-col', 'text-anchor': 'middle' });
    head.textContent = shortLabel(r.label || `R${ci}`, 12);
    svg.appendChild(head);
  });
  const gateHead = svgEl('text', { x: gateX, y: top - 12, class: 'dn-elimflow-col', 'text-anchor': 'middle' });
  gateHead.textContent = 'champion-gate';
  svg.appendChild(gateHead);

  // ── the MATCH CONVERGENCES (bracket-as-flow): at each round column the two
  // competitors' lanes meet at a match node — a short bracket joining the two
  // lane-ys to the node x. The winner's lane continues (good); the loser's
  // terminates (✕, drawn on its lane below). The pairing + Δ live on HOVER. ──
  matchesByCol.forEach((matches, ci) => {
    const x = colX(ci);
    for (const m of matches) {
      const lys = m.comps.map((c) => laneOf.has(c) ? laneY(laneOf.get(c)) : null).filter((v) => v != null);
      if (lys.length < 2) continue;
      const yTop = Math.min(...lys);
      const yBot = Math.max(...lys);
      const ymid = (yTop + yBot) / 2;
      // a projected (in-flight, with a server-side projected scalar) match draws
      // the convergence in the projected (dashed/amber) treatment.
      const projMatch = !!(m.pending && m.projected
        && m.comps.some((c) => m.projected[c] && isNum(m.projected[c].scalar)));
      // a small convergence elbow: the two lanes pinch toward the node at x.
      const cls = 'dn-elimflow-conv' + (m.pending ? ' dn-elimflow-conv-pending' : '') + (projMatch ? ' dn-proj' : '');
      svg.appendChild(svgEl('path', {
        d: `M${x - 8},${yTop} Q${x},${yTop} ${x},${ymid} Q${x},${yBot} ${x - 8},${yBot}`,
        class: cls, fill: 'none',
      }));
      const projTip = projMatch
        ? ' · projected: ' + m.comps.filter((c) => m.projected[c] && isNum(m.projected[c].scalar))
            .map((c) => `${shortLabel(c, 8)} ~${fmt(m.projected[c].scalar, 2)}`).join(', ')
        : '';
      const tip = `${m.slot ? m.slot + ': ' : ''}${m.comps.join(' vs ')}`
        + (m.winner ? ` → ${m.winner} ↑` : m.pending ? (projMatch ? ' · projected (boards streaming)' : ' · racing') : '')
        + (m.delta != null ? ` · Δ ${fmtSigned(m.delta, 2)}` : '') + projTip;
      const node = svgEl('circle', { cx: x, cy: ymid, r: m.pending ? 2.6 : 3,
        class: 'dn-elimflow-convnode' + (m.pending ? ' dn-elimflow-pending' : m.winner ? ' dn-elimflow-good' : '') + (projMatch ? ' dn-proj' : '') });
      svg.appendChild(hov(node, tip));
    }
  });

  // ── one lane per generation: dots at each round it played, a segment to the
  // next column when it advanced, a ✕ where it was cut, the crown at the gate ──
  gens.forEach((g, li) => {
    const y = laneY(li);
    const isChamp = champId != null && g.id === champId;
    const isFormer = benchId != null && g.id === benchId && !isChamp;
    const lane = svgEl('g', { class: 'dn-elimflow-lane', tabindex: o.onCompetitor ? '0' : null });

    // the lane's played columns, sorted.
    const cols = [...g.played].sort((a, b) => a - b);
    for (const ci of cols) {
      const x = colX(ci);
      const advanced = g.advanced.has(ci);
      const pending = g.pendingAt.has(ci);
      const eliminated = g.eliminatedAt === ci;
      // a DROP: lost this column but plays again later (winners→losers second
      // life) — not a terminal cut, not pending; its dot reads as a loss and a
      // drop edge carries the lane into its next (losers'-bracket) column.
      const dropped = g.lostAt.has(ci) && !eliminated;
      // the node dot at this round.
      const dotCls = 'dn-elimflow-dot ' + (eliminated || dropped ? 'dn-elimflow-bad' : advanced ? 'dn-elimflow-good' : 'dn-elimflow-pending');
      lane.appendChild(hov(svgEl('circle', { cx: x, cy: y, r: 2.8, class: dotCls }),
        `${g.id} · ${rounds[ci] ? (rounds[ci].label || 'R' + ci) : 'R' + ci} · ${eliminated ? 'eliminated' : dropped ? 'lost → losers’ bracket' : advanced ? 'advanced' : 'racing'}`));
      // a segment to the NEXT column the lane plays (a later round, or the gate)
      // whenever the lane CONTINUES: it advanced, it is racing, OR it dropped to
      // the losers' bracket. Without the drop case the dropped lane's WB dot was
      // orphaned from its LB entry, so the bracket "couldn't tell what connects".
      if (advanced || pending || dropped) {
        const nextCi = cols.find((c) => c > ci);
        // a lane reaches the GATE from the last column only when it WON / is still
        // racing there (advanced or pending) — never on a drop (a dropped lane
        // always has a later played column, so it never falls through to here).
        const toX = (nextCi != null) ? colX(nextCi)
          : ((advanced || pending) && ci === nCols - 1 ? gateX : null);
        // a DROP into an LB column (double-elim) routes as a rounded orthogonal
        // PIPE through the RESERVED CHANNEL below the whole lane stack — it leaves
        // the WB-loss dot, drops to its OWN channel lane, runs across there (never
        // over another lane's row), then rises into the TOP of the LB re-entry
        // node. Each demotion owns a distinct channel lane + a per-edge horizontal
        // nudge, so the two-loser case renders as two parallel, non-crossing pipes.
        // Every other continuation stays a straight lane segment.
        const dropToLB = dropped && isDouble && nextCi != null && colSide[nextCi] === 'LB';
        if (toX != null && dropToLB) {
          const k = chSlot.get(`${g.id}|${ci}|${nextCi}`);
          const chY = channelY(k != null ? k : 0);
          // a per-edge nudge fans the source verticals apart (two drops sharing a
          // WB column never overlap); kept small so the run stays near its column.
          const dx = 6 + ((k != null ? k : 0) % 4) * 4;
          lane.appendChild(svgEl('path', {
            d: channelDropPath(x, y, toX, y, chY, dx, 5),
            class: 'dn-elimflow-seg dn-elimflow-seg-drop dn-elimflow-bad', fill: 'none',
          }));
        } else if (toX != null) {
          const segCls = 'dn-elimflow-seg ' + (dropped ? 'dn-elimflow-seg-drop dn-elimflow-bad'
            : pending ? 'dn-elimflow-seg-pending' : 'dn-elimflow-good');
          lane.appendChild(svgEl('line', { x1: x, y1: y, x2: toX, y2: y, class: segCls }));
        }
      }
    }

    // the terminating ✕ at the elimination column.
    if (g.eliminatedAt != null) {
      const x = colX(g.eliminatedAt) + 8;
      const xm = svgEl('text', { x, y: y + 3.2, class: 'dn-elimflow-cut dn-elimflow-bad', 'text-anchor': 'start' });
      xm.textContent = '✕';
      lane.appendChild(xm);
    } else if (isChamp || isFormer || g.advanced.size) {
      // a survivor reaching the gate column: the champion gets CROWN.current, the
      // displaced incumbent CROWN.former; any other survivor a neutral arrival.
      const gx = gateX;
      const crowned = isChamp && (o.gateState === 'crowned' || (!o.gateState && !live));
      const mark = isChamp ? CROWN.current : isFormer ? CROWN.former : '→';
      const cls = 'dn-elimflow-gate' + (crowned ? ' dn-elimflow-good' : isFormer ? ' dn-elimflow-former' : '');
      const gm = hov(svgEl('text', { x: gx + 6, y: y + 3.2, class: cls, 'text-anchor': 'start' }),
        isChamp ? `${g.id} · champion ${CROWN.current}` : isFormer ? `${g.id} · former champion (displaced incumbent)` : `${g.id} · reached the gate`);
      gm.textContent = mark;
      lane.appendChild(gm);
    }

    // the lane label at the right gutter. A lane with a live PROJECTED standing
    // (in-flight, boards streaming) reads "~proj" in the projected treatment.
    const proj = g.eliminatedAt == null && projByGen.has(g.id) ? projByGen.get(g.id) : null;
    const lblCls = 'dn-elimflow-name' + (isChamp ? ' dn-elimflow-good' : isFormer ? ' dn-elimflow-former' : g.eliminatedAt != null ? ' dn-elimflow-bad' : '') + (proj ? ' dn-proj' : '');
    const lbl = hov(svgEl('text', { x: w - 6, y: y + 3.2, class: lblCls, 'text-anchor': 'end' }),
      proj ? `${g.id} · projected scalar ~${fmt(proj.scalar, 2)} (boards still streaming)` : g.id);
    lbl.textContent = shortLabel(g.id, 11) + (isChamp ? ' ' + CROWN.current : isFormer ? ' ' + CROWN.former : '') + (proj ? ' ~proj' : '');
    lane.appendChild(lbl);
    // a SCORED board-progress sub-bar under a projected lane (boards_done/total).
    if (proj && isNum(proj.boards_total) && proj.boards_total > 0) {
      const barW = 40;
      const bx = w - 6 - barW;
      const frac = Math.min(1, (proj.boards_done || 0) / proj.boards_total);
      lane.appendChild(svgEl('rect', { x: bx, y: y + 6, width: barW, height: 2.2, rx: 1, class: 'dn-proj-bar-bg' }));
      lane.appendChild(svgEl('rect', { x: bx, y: y + 6, width: Math.max(1, barW * frac), height: 2.2, rx: 1, class: 'dn-proj-bar' }));
    }

    clickable(lane, o.onCompetitor && (() => o.onCompetitor(g.id)));
    svg.appendChild(lane);
  });
  return svg;
}

// ---- COMPACT SWISS OVERVIEW (epoch-card hero) ----------------------
// (1) a STANDINGS BUMP CHART — one line per competitor, x = round, y = rank
//     (1 at top); lines cross as the leader emerges (champion line bold).
// (2) a RANKED COPELAND-POINT BAR — final standings, leader ♔, gate verdict.
//   { series:[{id,champion,ranks}], bars:[{id,points,wins,draws,losses,leader,
//     champion}], labels, championId, benchmarkId, gateState, gateDelta, live,
//     onCompetitor(id) }
export function swissOverview(opts) {
  const o = opts || {};
  const series = (Array.isArray(o.series) ? o.series : []).filter((s) => s && Array.isArray(s.ranks));
  const bars = (Array.isArray(o.bars) ? o.bars : []).filter((b) => b);
  const labels = Array.isArray(o.labels) ? o.labels : [];
  const live = !!o.live;
  const w = 640;
  const nR = Math.max(1, labels.length);
  const nC = Math.max(1, series.length, bars.length);
  // bump panel geometry. bumpTop leaves a clear band under the section title
  // (baseline y=14) so the round-axis labels (drawn at bumpTop-8) never collide
  // with it.
  const bumpTop = 42;
  const rowH = 22;
  const bumpH = bumpTop + nC * rowH + 10;
  const padL = 96;          // left gutter for the round-0 competitor labels
  const padR = 120;         // right gutter for the final-rank labels
  const barTop = bumpH + 30;
  const barH = 18;
  const barGap = 8;
  const barBandH = bars.length * (barH + barGap);
  const h = barTop + barBandH + 14;
  const svg = svgEl('svg', {
    class: 'dn-swissover', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
  });
  if (!series.length && !bars.length) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no swiss rounds yet';
    svg.appendChild(t);
    return svg;
  }

  // panel (1): the standings BUMP CHART
  const ttl = svgEl('text', { x: 2, y: 14, class: 'dn-swissover-title' });
  ttl.textContent = 'standings by round' + (live ? ' · LIVE' : '');
  svg.appendChild(ttl);
  const X = scale([0, Math.max(1, nR - 1)], [padL, w - padR]);
  const Y = scale([1, Math.max(2, nC)], [bumpTop, bumpTop + (nC - 1) * rowH]);
  labels.forEach((lab, j) => {
    const x = X(j);
    const tk = svgEl('text', { x, y: bumpTop - 8, class: 'dn-swissover-round', 'text-anchor': j === 0 ? 'start' : (j === labels.length - 1 ? 'end' : 'middle') });
    // compact axis ticks — "Swiss round 2" → "R2", "Champion gate" → "Gate" —
    // so the labels never truncate to an ambiguous "Swiss r…".
    const ls = String(lab);
    const rm = ls.match(/(\d+)/);
    tk.textContent = /gate/i.test(ls) ? 'Gate' : (/round/i.test(ls) && rm ? 'R' + rm[1] : shortLabel(ls, 8));
    svg.appendChild(tk);
    svg.appendChild(svgEl('line', { x1: x, x2: x, y1: bumpTop - 4, y2: bumpTop + (nC - 1) * rowH + 4, class: 'dn-swissover-grid' }));
  });
  // one polyline per competitor; champion emphasised.
  series.forEach((s) => {
    const pts = [];
    s.ranks.forEach((r, j) => { if (isNum(r)) pts.push([X(j), Y(r)]); });
    if (!pts.length) return;
    // The CURRENT champion's line is emphasised (bold + ♛); the displaced
    // incumbent reads dim with a "former" mark so the two never look alike.
    const champ = !!s.crown;
    const former = !!s.former;
    const cls = 'dn-swissover-line' + (champ ? ' dn-swissover-line-champ' : (former ? ' dn-swissover-line-former' : ''));
    const d = pts.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
    const path = clickable(hov(svgEl('path', { d, class: cls, fill: 'none', tabindex: o.onCompetitor ? '0' : null }),
      `${s.id}${champ ? ' · new champion' : (former ? ' · former champion' : '')} · finishes rank ${s.ranks[s.ranks.length - 1] || '?'}`),
      o.onCompetitor && (() => o.onCompetitor(s.id)));
    svg.appendChild(path);
    // end-dots (start + final rank) + left name label + right rank label.
    const [x0, y0] = pts[0];
    const [xn, yn] = pts[pts.length - 1];
    const dotCls = 'dn-swissover-dot' + (champ ? ' dn-swissover-dot-champ' : '');
    const r = champ ? 3.4 : 2.6;
    svg.appendChild(svgEl('circle', { cx: x0, cy: y0, r, class: dotCls }));
    svg.appendChild(svgEl('circle', { cx: xn, cy: yn, r, class: dotCls }));
    const lL = svgEl('text', { x: x0 - 6, y: y0 + 3, class: 'dn-swissover-name' + (champ ? ' dn-swissover-name-champ' : (former ? ' dn-swissover-name-former' : '')), 'text-anchor': 'end' });
    lL.textContent = shortLabel(s.id, 11) + (champ ? ' ' + CROWN.current : (former ? ' ' + CROWN.former : ''));
    svg.appendChild(lL);
    const lR = svgEl('text', { x: xn + 6, y: yn + 3, class: 'dn-swissover-rank', 'text-anchor': 'start' });
    lR.textContent = '#' + (s.ranks[s.ranks.length - 1] || '?');
    svg.appendChild(lR);
  });

  // ── panel (2): the RANKED COPELAND-POINT BAR ──
  const bt = svgEl('text', { x: 2, y: barTop - 10, class: 'dn-swissover-title' });
  bt.textContent = 'Copeland points · final standings';
  svg.appendChild(bt);
  const maxPts = Math.max(1, ...bars.map((b) => b.points || 0));
  const champId = o.championId ? String(o.championId) : null;
  const gateState = o.gateState || (live ? 'deciding' : 'pending');
  const barX0 = padL;
  const barMaxW = w - padR - barX0;
  bars.forEach((b, i) => {
    const y = barTop + i * (barH + barGap);
    const bw = Math.max(2, barMaxW * ((b.points || 0) / maxPts));
    const champ = !!b.crown;
    const former = !!b.former;
    // a transient round-leader ♔ shows only BEFORE the gate decides; once a
    // champion is crowned the ♛ takes over (no double crown).
    const lead = !champ && !former && b.leader && live;
    const g = svgEl('g', { class: 'dn-swissover-barrow', tabindex: o.onCompetitor ? '0' : null });
    const lab = svgEl('text', { x: barX0 - 6, y: y + barH / 2 + 3, class: 'dn-swissover-barname' + (champ ? ' dn-swissover-name-champ' : (former ? ' dn-swissover-name-former' : '')), 'text-anchor': 'end' });
    lab.textContent = (i + 1) + '. ' + shortLabel(b.id, 9) + (champ ? ' ' + CROWN.current : (former || lead ? ' ' + CROWN.former : ''));
    g.appendChild(lab);
    g.appendChild(svgEl('rect', { x: barX0, y, width: barMaxW, height: barH, rx: 3, class: 'dn-swissover-bar-bg' }));
    g.appendChild(hov(svgEl('rect', { x: barX0, y, width: bw, height: barH, rx: 3, class: 'dn-swissover-bar' + (champ || lead ? ' dn-swissover-bar-lead' : '') }),
      `${b.id} · ${fmt(b.points, b.points % 1 ? 1 : 0)} pts · ${b.wins}W ${b.draws}D ${b.losses}L`));
    const pv = svgEl('text', { x: barX0 + bw + 5, y: y + barH / 2 + 3, class: 'dn-swissover-barval' });
    pv.textContent = fmt(b.points, b.points % 1 ? 1 : 0) + ' pts';
    g.appendChild(pv);
    clickable(g, o.onCompetitor && (() => o.onCompetitor(b.id)));
    svg.appendChild(g);
  });
  // the champion-gate verdict, anchored at the bottom-right.
  const crowned = gateState === 'crowned' && !!champId;
  const vy = barTop + barBandH + 4;
  let verdict;
  if (crowned) verdict = `${CROWN.current} ${shortLabel(champId, 12)} promoted`;
  else if (gateState === 'stands') verdict = 'champion stands';
  else if (gateState === 'deciding') verdict = 'gate deciding…';
  else verdict = '';
  if (verdict) {
    const vt = svgEl('text', { x: w - padR, y: vy, class: 'dn-swissover-verdict' + (crowned ? ' dn-good' : ''), 'text-anchor': 'end' });
    vt.textContent = verdict + (isNum(o.gateDelta) ? ` · Δ ${fmtSigned(o.gateDelta, 2)}` : '');
    svg.appendChild(vt);
  }
  return svg;
}

// ── the GAUNTLET DUEL FLOW — the field as Δ-vs-champion lanes ────────
//
// The gauntlet structure-flow that REPLACES the boxed champion banner + the
// per-challenger match cards: the round's field as a column of lanes, each a
// challenger DUELLING the reigning champion. A horizontal REFERENCE RULE at Δ=0
// is the champion (the crowned gate node on the right); each challenger sits a
// dot at its Δ-vs-champion — BELOW the rule (good, lower loss) when it improved,
// ABOVE (bad) when it regressed — with a status glyph (↑ promoted / ✕ cut / ○
// pending). The promoted challenger's lane reaches the crowned gate (♛). The
// per-challenger hypothesis + the exact Δ live ON HOVER.
//
//   opts: {
//     championId, championScalar,
//     challengers: [{ id, delta, verdict:'promoted'|'rejected'|'pending',
//                     hypothesis, driver }],
//     onCompetitor(id).
//   }
export function duelFlow(opts) {
  const o = opts || {};
  const challengers = (Array.isArray(o.challengers) ? o.challengers : []).filter((c) => c && c.id != null);
  const champId = o.championId != null ? String(o.championId) : null;
  const w = o.width || 720;
  const padTop = 34;
  const padBottom = 22;
  const laneGap = 26;
  const nameW = 60;                              // left gutter for challenger labels
  const gateW = 124;
  const plotLeft = nameW + 18;                   // start of the measured band
  const fieldRight = w - gateW - 28;             // end of the improvement zone (before the gate)
  // The Δ=0 rule sits inside the band with a regression zone to its LEFT and a
  // (larger) improvement zone to its RIGHT running toward the gate.
  const refX = Math.round(plotLeft + 0.34 * (fieldRight - plotLeft));
  const leftSpan = refX - plotLeft;              // |Δ| range for regressions (left)
  const rightSpan = fieldRight - refX;           // |Δ| range for improvements (right)
  const h = padTop + Math.max(1, challengers.length) * laneGap + padBottom;
  const svg = svgEl('svg', {
    class: 'dn-duelflow', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
    'aria-label': 'The field duelling the champion',
  });
  // the champion REFERENCE rule (Δ=0) — a VERTICAL spine the field is measured
  // against: a lane reaches RIGHT toward the gate when it improved, LEFT when it
  // regressed; the bar length encodes |Δ|.
  svg.appendChild(hov(svgEl('line', { x1: refX, x2: refX, y1: padTop - 10, y2: h - padBottom + 4, class: 'dn-duelflow-ref' }),
    champId ? `champion ${champId}${isNum(o.championScalar) ? ' · loss ' + fmt(o.championScalar, 1) : ''} · Δ=0 reference` : 'champion · Δ=0 reference'));
  svg.appendChild(svgEl('text', { x: refX, y: padTop - 16, class: 'dn-duelflow-axis', 'text-anchor': 'middle' }, ['champion · Δ=0']));
  svg.appendChild(svgEl('text', { x: plotLeft, y: padTop - 16, class: 'dn-duelflow-dir dn-bad', 'text-anchor': 'start' }, ['← worse']));
  svg.appendChild(svgEl('text', { x: fieldRight, y: padTop - 16, class: 'dn-duelflow-dir dn-good', 'text-anchor': 'end' }, ['better → (gate)']));

  if (!challengers.length) {
    const t = svgEl('text', { x: (refX + fieldRight) / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no challenger has entered the ring';
    svg.appendChild(t);
  }
  // |Δ| → a SIGNED horizontal offset from the rule: improvements (Δ<0) ride RIGHT
  // toward the gate, regressions (Δ>0) ride LEFT, magnitude scaled to the largest
  // |Δ| in the field so the lanes are comparable. A pending / no-Δ lane sits on
  // the rule.
  const deltas = challengers.map((c) => c.delta).filter(isNum).map(Math.abs);
  const maxAbs = Math.max(1e-9, ...deltas);
  // Reserve a margin at each band edge so the OUTBOARD Δ label never collides
  // with the name gutter (left) or the gate (right) even at the max |Δ|.
  const labelPad = 32;
  const offsetOf = (d) => {
    if (!isNum(d) || d === 0) return 0;
    const frac = Math.min(1, Math.abs(d) / maxAbs);
    return d < 0 ? frac * Math.max(12, rightSpan - labelPad) : -(frac * Math.max(12, leftSpan - labelPad));
  };

  let promotedX = null, promotedY = null;
  challengers.forEach((c, i) => {
    const cy = padTop + i * laneGap + laneGap / 2;
    const verdict = c.verdict || 'pending';
    const won = verdict === 'promoted';
    const cut = verdict === 'rejected';
    const good = isNum(c.delta) ? c.delta < 0 : won;
    const bad = isNum(c.delta) ? c.delta > 0 : cut;
    const dx = refX + offsetOf(c.delta);
    const cls = 'dn-duelflow-dot ' + (good ? 'dn-good' : bad ? 'dn-bad' : 'dn-duelflow-pending');
    const glyph = won ? ' ↑' : cut ? ' ✕' : ' ○';
    const g = svgEl('g', { class: 'dn-duelflow-lane', tabindex: o.onCompetitor ? '0' : null,
      'aria-label': `${c.id} vs champion${isNum(c.delta) ? ', Δ ' + fmtSigned(c.delta, 1) : ''}, ${verdict}` });
    // the lane bar from the rule out to the dot — its direction IS the sign of Δ.
    g.appendChild(svgEl('line', { x1: refX, x2: dx, y1: cy, y2: cy, class: 'dn-duelflow-laneline ' + (good ? 'dn-good' : bad ? 'dn-bad' : '') }));
    const tip = `${c.id} vs ${champId || 'champion'}`
      + (isNum(c.delta) ? ` · Δ ${fmtSigned(c.delta, 2)} (${good ? 'improved' : bad ? 'regressed' : 'flat'})` : '')
      + ` · ${verdict}`
      + (c.hypothesis ? ` · hypothesis: ${c.hypothesis}` : '')
      + (c.driver ? ` · decisive driver: ${c.driver}` : '');
    g.appendChild(hov(svgEl('circle', { cx: dx, cy, r: won ? 4.4 : 3.4, class: cls }), tip));
    // the challenger label + status glyph in the left gutter.
    const lbl = svgEl('text', { x: nameW, y: cy + 3, class: 'dn-duelflow-name ' + (good ? 'dn-good' : bad ? 'dn-bad' : ''), 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(c.id), 9) + glyph;
    g.appendChild(lbl);
    // the Δ value, just OUTBOARD of the dot (away from the rule) so it never
    // collides with the spine.
    if (isNum(c.delta) && c.delta !== 0) {
      const rightward = c.delta < 0;
      const dt = svgEl('text', { x: dx + (rightward ? 7 : -7), y: cy + 3,
        class: 'dn-duelflow-delta ' + (good ? 'dn-good' : bad ? 'dn-bad' : ''),
        'text-anchor': rightward ? 'start' : 'end' });
      dt.textContent = fmtSigned(c.delta, Math.abs(c.delta) < 0.1 ? 2 : 1);
      g.appendChild(dt);
    }
    clickable(g, o.onCompetitor && (() => o.onCompetitor(String(c.id))));
    svg.appendChild(g);
    if (won) { promotedX = dx; promotedY = cy; }
  });

  // ── the crowned CHAMPION GATE on the right ──
  const gx = fieldRight + 18;
  const gateCy = promotedY != null ? promotedY : padTop + (Math.max(1, challengers.length) * laneGap) / 2;
  const promotedAny = challengers.some((c) => (c.verdict || '') === 'promoted');
  const gateG = svgEl('g', { class: 'dn-duelflow-gate', tabindex: (champId && o.onCompetitor) ? '0' : null });
  gateG.appendChild(svgEl('rect', { x: gx, y: gateCy - 14, width: gateW, height: 28, rx: 5,
    class: 'dn-duelflow-gatebox' + (promotedAny ? ' dn-good' : '') }));
  // the converging flow from the promoted lane's dot into the gate.
  if (promotedY != null) {
    svg.appendChild(svgEl('path', { d: `M${promotedX != null ? promotedX : fieldRight},${promotedY} H${gx}`, class: 'dn-duelflow-gateflow dn-good', fill: 'none' }));
  }
  const gt = hov(svgEl('text', { x: gx + gateW / 2, y: gateCy + 4, class: 'dn-duelflow-gatelab' + (promotedAny ? ' dn-good' : ''), 'text-anchor': 'middle' }),
    champId ? `champion-gate · ${promotedAny ? 'a challenger was promoted' : champId + ' defends the title'}` : 'champion-gate');
  gt.textContent = (champId ? CROWN.current + ' ' + shortLabel(champId, 11) : 'champion-gate');
  gateG.appendChild(gt);
  clickable(gateG, (champId && o.onCompetitor) && (() => o.onCompetitor(champId)));
  svg.appendChild(gateG);
  return svg;
}

// ── racing SCALAR TRACK (every gen on a shared scalar number-line) ───
//
// The FINAL liked racing study figure (racing.html opt 1). Every generation is
// a MARKER on one shared scalar number-line — lower loss sits LEFT (better). The
// marker SIZE encodes INVERSE LOSS (bigger = better) via the study's area-honest
// radius `r = 4 + sqrt(1 - normLoss) * 9`, so the surviving leader looms largest
// and the cut candidates shrink away — cut-closeness becomes literal distance.
// The champion v0 is a dashed accent benchmark line; the cut threshold (the worst
// surviving scalar) is a dashed caution tick; labels stagger into tiers so near
// markers never overlap.
//
// FOUR lifecycle states (mirroring funnelRunner):
//   queued      — no scalar yet: a hollow dim marker parked at the axis left.
//   in-flight   — a live rung lane (live:true + per-gen `live_progress`): the
//                 marker is dashed/caution + a "k/N boards" progress sub-bar.
//   projected   — an in-flight lane WITH a server-side projected scalar: the
//                 marker sits at its projected x in the dashed/amber dn-proj
//                 treatment + a "~scalar proj" label + a scored progress sub-bar.
//   settled     — solid (survivor) / hollow-outline (cut) marker, final verdict.
//
// CONVERGENCE: a settled marker renders byte-identically whether it arrived via
// the live path (projected→settled) or a completed record — no live-only chrome
// survives once `live_progress` is absent and a scalar is committed.
//
// opts: {
//   rungs: [{ label, match_id, board_fraction, competitors:[id],
//             survivors:[id], cut:[id], deltas:{id: Δ-vs-champ},
//             scalars:{id: scalar}, live_progress:{id: lane}, pending }],
//   championId | benchmarkId, championScalar,   // the v0 benchmark line
//   live, mini|compact, onCompetitor(id), focusRung (default: last)
// }
// A rung lane `live_progress[id]` is the SAME shape funnelRunner reads:
//   { inflight, done, total, boards_done, boards_total, partialDelta,
//     projected, projected_scalar }.
// Each competitor's plotted scalar is taken from `rung.scalars[id]` when present,
// else recovered from the Δ-vs-champ + championScalar (scalar = champ + Δ), else
// (live, no scalar) the lane's projected_scalar.
export function racingScalarTrack(opts) {
  const o = opts || {};
  const rungs = (Array.isArray(o.rungs) ? o.rungs : []).filter((r) => r);
  const mini = !!(o.mini || o.compact);
  const benchId = o.benchmarkId != null ? String(o.benchmarkId)
    : (o.championId != null ? String(o.championId) : null);
  const champScalar = isNum(o.championScalar) ? o.championScalar : null;

  // the focus rung — the one whose field is plotted on the track. Default: the
  // last (deepest) rung that has any competitors, so the hero shows the live edge.
  let focus = isNum(o.focusRung) ? o.focusRung : -1;
  if (focus < 0 || focus >= rungs.length) {
    focus = 0;
    for (let i = rungs.length - 1; i >= 0; i--) {
      if (Array.isArray(rungs[i].competitors) && rungs[i].competitors.length) { focus = i; break; }
    }
  }
  const rung = rungs[focus] || { competitors: [], survivors: [], cut: [] };

  // recover each competitor's plotted scalar from the model.
  const scalarOf = (id, lane) => {
    if (rung.scalars && isNum(rung.scalars[id])) return rung.scalars[id];
    if (champScalar != null && rung.deltas && isNum(rung.deltas[id])) return champScalar + rung.deltas[id];
    if (lane && isNum(lane.projected_scalar)) return lane.projected_scalar;
    return null;
  };

  // the FULL field of THIS rung: every lane racing it (all survivors), per the
  // shared contract — the union of live_progress keys ∪ competitors ∪
  // survivors/cut, minus the champion/benchmark (which defends at the gate). A
  // rung with survivors v5 + v7 plots BOTH markers, not just the first matchup.
  const comps = rungFieldLanes(rung, benchId);
  const survSet = new Set((Array.isArray(rung.survivors) ? rung.survivors : []).map(String));
  const cutSet = new Set((Array.isArray(rung.cut) ? rung.cut : []).map(String));
  const prog = (rung.live_progress && typeof rung.live_progress === 'object') ? rung.live_progress : null;

  const W = mini ? 360 : 560;
  const padL = mini ? 40 : 70;
  const padR = 20;
  const top = mini ? 24 : 40;

  // marker model — scalar, radius (inverse loss), verdict, live lane.
  const vals = [];
  if (benchId != null && champScalar != null) vals.push(champScalar);
  const marks = comps.map((id, idx) => {
    const lane = prog ? prog[id] : null;
    const v = scalarOf(id, lane);
    if (isNum(v)) vals.push(v);
    const survived = survSet.has(id);
    const cut = cutSet.has(id);
    const racing = !!lane && !survived && !cut;
    const projected = !!(lane && lane.projected && racing);
    return { id, v, lane, survived, cut, racing, projected, idx };
  });
  const [lo, hi] = (() => {
    const e = extent(vals.length ? vals : [0, 1]);
    return [e[0] - 0.02, e[1] + 0.02];
  })();
  const X = scale([lo, hi], [padL, W - padR]);
  const radOf = (v) => {
    if (!isNum(v)) return 4;
    const norm = Math.max(0, Math.min(1, (v - lo) / (hi - lo || 1)));   // 0 best … 1 worst
    return (mini ? 3 : 4) + Math.sqrt(1 - norm) * (mini ? 6 : 9);       // area-honest inverse loss
  };

  // NO-SCALAR SPREAD: a lane with no recoverable scalar (early in-flight, no
  // committed/delta/projected_scalar yet) must NOT pile at x=padL — it is SPREAD
  // across the axis by its lane index so an entering rung reads as a field, not a
  // stack. Once a projected/committed scalar arrives the marker positions by it.
  const noScalar = marks.filter((m) => !isNum(m.v));
  const spreadX = (() => {
    const n = noScalar.length;
    if (n <= 0) return () => padL;
    if (n === 1) return () => (padL + (W - padR)) / 2;
    // even fractions across the inboard span (a small margin off each end).
    const x0 = padL + (W - padR - padL) * 0.08;
    const x1 = padL + (W - padR - padL) * 0.92;
    const pos = new Map();
    noScalar.forEach((m, k) => pos.set(m, x0 + (x1 - x0) * (k / (n - 1))));
    return (m) => (pos.has(m) ? pos.get(m) : (padL + (W - padR)) / 2);
  })();
  // stagger labels into tiers so near-x markers don't collide (greedy by x).
  marks.forEach((m) => { m.x = isNum(m.v) ? X(m.v) : spreadX(m); m.r = radOf(m.v); });
  const minDX = mini ? 22 : 30;
  const order = [...marks].sort((a, b) => a.x - b.x);
  const tierLastX = [];
  order.forEach((m) => {
    let t = 0;
    while (tierLastX[t] != null && m.x - tierLastX[t] < minDX) t++;
    m.tier = t; tierLastX[t] = m.x;
  });
  const maxTier = Math.max(0, ...marks.map((m) => m.tier || 0));
  const tierH = mini ? 9 : 11;
  const axisY = top + 8 + maxTier * tierH;
  const H = axisY + (mini ? 26 : 40);

  const svg = svgEl('svg', applyResponsive({
    class: 'dn-scalartrack', width: '100%', height: H,
    viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
    'aria-label': 'racing scalar track — lower is better, bigger marker = better',
  }, o, W, H, 'dn-scalartrack-hero'));
  if (!comps.length) {
    const t = svgEl('text', { x: W / 2, y: H / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no challengers on the track yet';
    svg.appendChild(t);
    return svg;
  }

  // the axis line.
  svg.appendChild(svgEl('line', { x1: padL, y1: axisY, x2: W - padR, y2: axisY, class: 'dn-scalartrack-axis' }));
  if (!mini) {
    const cap = svgEl('text', { x: padL, y: top - 18, class: 'dn-scalartrack-cap' });
    cap.textContent = `${shortLabel(rung.label || `Rung ${focus}`, 14)} — scalar, lower is better`;
    svg.appendChild(cap);
  }
  // axis end ticks.
  if (!mini) {
    const t0 = svgEl('text', { x: padL, y: axisY + 18, class: 'dn-scalartrack-tick' });
    t0.textContent = fmt(lo, 3);
    const t1 = svgEl('text', { x: W - padR, y: axisY + 18, class: 'dn-scalartrack-tick', 'text-anchor': 'end' });
    t1.textContent = fmt(hi, 3);
    svg.appendChild(t0); svg.appendChild(t1);
  }

  // the cut threshold tick (the worst surviving scalar) — only when settled.
  const survScalars = marks.filter((m) => m.survived && isNum(m.v)).map((m) => m.v);
  if (survScalars.length && !rung.pending) {
    const thr = Math.max(...survScalars);
    const tx = X(thr);
    svg.appendChild(svgEl('line', { x1: tx, y1: top - 4, x2: tx, y2: axisY + 6, class: 'dn-scalartrack-cut' }));
    if (!mini) {
      const ct = svgEl('text', { x: tx, y: top - 8, class: 'dn-scalartrack-cutlab', 'text-anchor': 'middle' });
      ct.textContent = 'cut';
      svg.appendChild(ct);
    }
  }

  // the champion v0 benchmark line (dashed accent).
  if (benchId != null && champScalar != null) {
    const cx = X(champScalar);
    svg.appendChild(hov(svgEl('line', { x1: cx, y1: top - 4, x2: cx, y2: axisY + (mini ? 6 : 16), class: 'dn-scalartrack-bench' }),
      `champion v0 = ${benchId} · scalar ${fmt(champScalar, 3)} · the benchmark every Δ is measured against`));
    const bt = svgEl('text', { x: cx, y: top - (mini ? 4 : 8), class: 'dn-scalartrack-benchlab', 'text-anchor': 'middle' });
    bt.textContent = mini ? 'v0' : 'champ ' + fmt(champScalar, 3);
    svg.appendChild(bt);
  }

  // the markers — radius = inverse loss; survivors filled, cuts hollow, live
  // dashed; staggered labels with a connector tick when raised.
  marks.forEach((m) => {
    const verdictCls = m.cut ? 'dn-bad' : m.survived ? 'dn-good' : m.projected ? 'dn-proj' : m.racing ? 'dn-racing' : '';
    const g = svgEl('g', { class: 'dn-scalartrack-mark', tabindex: o.onCompetitor ? '0' : null });
    const filled = m.survived;
    const dashed = m.racing && !m.projected;
    const markCls = 'dn-scalartrack-dot ' + verdictCls
      + (filled ? ' dn-scalartrack-filled' : '')
      + (m.projected ? ' dn-proj' : '')
      + (dashed ? ' dn-scalartrack-live' : '')
      + (isNum(m.v) ? '' : ' dn-scalartrack-queued');
    const tip = `${m.id} · ${shortLabel(rung.label || 'rung ' + focus, 12)}`
      + (isNum(m.v) ? ` · scalar ${fmt(m.v, 3)}` : ' · queued')
      + (rung.deltas && isNum(rung.deltas[m.id]) ? ` · Δ ${fmtSigned(rung.deltas[m.id], 2)} vs champion` : '')
      + (m.lane ? ' · ' + laneProgressText(m.lane) : '')
      + (m.projected && isNum(m.lane.projected_scalar) ? ` · projected ~${fmt(m.lane.projected_scalar, 2)} (boards streaming)` : '')
      + ` · ${m.cut ? 'cut' : m.survived ? 'survives' : m.projected ? 'projected' : m.racing ? 'racing' : 'queued'}`;
    g.appendChild(hov(svgEl('circle', { cx: m.x, cy: axisY, r: m.r, class: markCls }), tip));
    // the staggered id label, lifted off the axis by tier.
    const ly = axisY - m.r - 4 - (m.tier || 0) * tierH;
    if ((m.tier || 0) > 0) g.appendChild(svgEl('line', { x1: m.x, y1: axisY - m.r - 1, x2: m.x, y2: ly + 2, class: 'dn-scalartrack-tier ' + verdictCls }));
    const lab = svgEl('text', { x: m.x, y: ly, class: 'dn-scalartrack-name ' + verdictCls + (m.projected ? ' dn-proj' : ''), 'text-anchor': 'middle' });
    const projSuffix = m.projected && isNum(m.lane.projected_scalar) ? ' ~' + fmt(m.lane.projected_scalar, 1) + ' proj' : '';
    lab.textContent = shortLabel(m.id, mini ? 6 : 9) + projSuffix;
    g.appendChild(lab);
    // a live/projected progress sub-bar UNDER the axis (boards done / total).
    if (m.lane && (m.lane.inflight || m.lane.done || m.projected)) {
      const bw = mini ? 26 : 40;
      const bx = m.x - bw / 2;
      const by = axisY + (mini ? 6 : 10);
      const sd = isNum(m.lane.boards_done) ? m.lane.boards_done : m.lane.done;
      const stot = isNum(m.lane.boards_total) ? m.lane.boards_total : m.lane.total;
      const frac = (isNum(stot) && stot > 0) ? Math.min(1, (sd || 0) / stot) : (m.lane.inflight ? 0.5 : 0);
      if (m.projected) {
        g.appendChild(svgEl('rect', { x: bx, y: by, width: bw, height: 2.4, rx: 1, class: 'dn-proj-bar-bg' }));
        g.appendChild(svgEl('rect', { x: bx, y: by, width: Math.max(1, bw * frac), height: 2.4, rx: 1, class: 'dn-proj-bar' }));
      } else {
        g.appendChild(svgEl('rect', { x: bx, y: by, width: bw, height: 2, rx: 1, class: 'dn-scalartrack-bar-bg' }));
        g.appendChild(svgEl('rect', { x: bx, y: by, width: Math.max(1, bw * frac), height: 2, rx: 1, class: 'dn-scalartrack-bar dn-scalartrack-live' }));
      }
    }
    clickable(g, o.onCompetitor && (() => o.onCompetitor(m.id)));
    svg.appendChild(g);
  });
  return svg;
}

// A stable digest of the racingScalarTrack model — changes ONLY when the visible
// content does (so the digest-gated swap never re-renders on a no-op heartbeat).
export function racingScalarTrackDigest(opts) {
  const o = opts || {};
  const rungs = Array.isArray(o.rungs) ? o.rungs : [];
  return JSON.stringify({
    b: o.benchmarkId != null ? String(o.benchmarkId) : (o.championId != null ? String(o.championId) : null),
    cs: isNum(o.championScalar) ? o.championScalar.toFixed(3) : null,
    f: isNum(o.focusRung) ? o.focusRung : null,
    r: rungs.map((r) => [r.match_id, r.label,
      (r.competitors || []).map(String).join('/'),
      (r.survivors || []).map(String).join('/'),
      (r.cut || []).map(String).join('/'),
      r.scalars ? Object.keys(r.scalars).sort().map((k) => k + ':' + (isNum(r.scalars[k]) ? r.scalars[k].toFixed(3) : '?')).join(',') : '',
      r.deltas ? Object.keys(r.deltas).sort().map((k) => k + ':' + (isNum(r.deltas[k]) ? r.deltas[k].toFixed(3) : '?')).join(',') : '',
      r.live_progress ? Object.keys(r.live_progress).sort().map((k) => {
        const p = r.live_progress[k];
        return k + ':' + (p.done || 0) + '/' + (p.total == null ? '?' : p.total) + ':' + (p.inflight || 0)
          + (p.projected ? ':j' + (isNum(p.projected_scalar) ? p.projected_scalar.toFixed(3) : '?')
            + '/' + (p.boards_done == null ? '?' : p.boards_done) + '/' + (p.boards_total == null ? '?' : p.boards_total) : '');
      }).join(',') : '',
      !!r.pending]),
  });
}

// ── GAUNTLET FIELD BARS (the wave of challengers vs the champion standard) ──
//
// The FINAL liked gauntlet study figure (gauntlet.html opt 5, reinterpreted for
// the multi-challenger gauntlet wave). One wave of challenger MARKERS measured
// against the FIXED champion standard on a shared scalar axis (lower = better).
// Each challenger is a bar from the champion-standard line out to its own scalar,
// coloured by outcome (cleared = good, failed = bad, tied = flat), survivor-marked
// (↑); the champion standard is a solid accent line and the PROMOTE GATE (champ −
// margin) is a dashed accent threshold. A projected (in-flight) challenger is
// ghosted in the dn-proj treatment + a scored progress sub-bar.
//
// FOUR lifecycle states (mirroring funnelRunner):
//   queued    — no scalar yet: a hollow dim marker parked at the champion line.
//   in-flight — live:true + per-challenger `lane` ({inflight,done,total}): a
//               caution marker + a "k/N boards" progress sub-bar.
//   projected — an in-flight challenger WITH a projected scalar: ghosted dn-proj
//               at its projected x + "~scalar proj" + a scored sub-bar.
//   settled   — solid bar to its final scalar, outcome colour + survivor glyph.
//
// CONVERGENCE: a settled challenger renders byte-identically via the live or the
// completed path — no live-only chrome persists once a scalar is committed.
//
// opts: {
//   championId, championScalar,            // the fixed standard (the reference)
//   promoteMargin,                         // the gate = championScalar - margin
//   challengers: [{ id, scalar, delta,     // scalar OR delta-vs-champ (champ+Δ)
//                   outcome:'cleared'|'failed'|'tied', survivor,
//                   lane:{inflight,done,total,boards_done,boards_total,
//                         projected,projected_scalar} }],
//   live, mini|compact, onCompetitor(id)
// }
export function gauntletFieldBars(opts) {
  const o = opts || {};
  const field = (Array.isArray(o.challengers) ? o.challengers : []).filter((c) => c && c.id != null);
  const mini = !!(o.mini || o.compact);
  const champId = o.championId != null ? String(o.championId) : null;
  const champScalar = isNum(o.championScalar) ? o.championScalar : null;
  const margin = isNum(o.promoteMargin) ? o.promoteMargin : null;

  const scalarOf = (c) => {
    if (isNum(c.scalar)) return c.scalar;
    if (champScalar != null && isNum(c.delta)) return champScalar + c.delta;
    if (c.lane && isNum(c.lane.projected_scalar)) return c.lane.projected_scalar;
    return null;
  };
  const outcomeOf = (c, v) => {
    if (c.outcome) return String(c.outcome);
    if (champScalar == null || !isNum(v)) return 'pending';
    if (Math.abs(v - champScalar) < 1e-9) return 'tied';
    return v < champScalar ? 'cleared' : 'failed';
  };

  const W = mini ? 360 : 600;
  const padL = mini ? 56 : 110;
  const padR = mini ? 16 : 30;
  const top = mini ? 26 : 34;
  const rowH = mini ? 18 : 22;
  const H = top + Math.max(1, field.length) * rowH + (mini ? 10 : 26);

  // scalar domain: champion standard + the gate + every challenger scalar.
  const vals = [];
  if (champScalar != null) vals.push(champScalar);
  if (champScalar != null && margin != null) vals.push(champScalar - margin);
  const rows = field.map((c) => {
    const v = scalarOf(c);
    if (isNum(v)) vals.push(v);
    const lane = c.lane && typeof c.lane === 'object' ? c.lane : null;
    const racing = !!(lane && (lane.inflight || lane.done)) && c.outcome == null;
    const projected = !!(lane && lane.projected && racing);
    return { c, v, lane, racing, projected, outcome: outcomeOf(c, v), survivor: !!c.survivor };
  });
  const [lo, hi] = (() => {
    const e = extent(vals.length ? vals : [0, 1]);
    return [e[0] - 0.02, e[1] + 0.02];
  })();
  const X = scale([lo, hi], [padL, W - padR]);

  const svg = svgEl('svg', applyResponsive({
    class: 'dn-fieldbars', width: '100%', height: H,
    viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
    'aria-label': 'gauntlet field vs the champion standard — lower is better',
  }, o, W, H, 'dn-fieldbars-hero'));
  if (!field.length) {
    const t = svgEl('text', { x: W / 2, y: H / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no challengers have entered the gauntlet';
    svg.appendChild(t);
    return svg;
  }
  const bandTop = top - 2;
  const bandBot = H - (mini ? 8 : 22);

  // the champion STANDARD line (solid accent) — the reference all bars start from.
  if (champScalar != null) {
    const cx = X(champScalar);
    svg.appendChild(hov(svgEl('line', { x1: cx, y1: bandTop, x2: cx, y2: bandBot, class: 'dn-fieldbars-standard' }),
      champId ? `champion ${champId} · standard ${fmt(champScalar, 3)}` : `champion standard ${fmt(champScalar, 3)}`));
    const ct = svgEl('text', { x: cx, y: bandTop - 4, class: 'dn-fieldbars-axis', 'text-anchor': 'middle' });
    ct.textContent = mini ? 'champ' : (champId ? shortLabel(champId, 10) + ' standard' : 'champ standard');
    svg.appendChild(ct);
  }
  // the PROMOTE GATE threshold (champ − margin) — a dashed accent line.
  if (champScalar != null && margin != null) {
    const gx = X(champScalar - margin);
    svg.appendChild(hov(svgEl('line', { x1: gx, y1: bandTop, x2: gx, y2: bandBot, class: 'dn-fieldbars-gate' }),
      `promote gate · champion − margin ${fmt(margin, 3)} = ${fmt(champScalar - margin, 3)} (clear this to be crowned)`));
    if (!mini) {
      const gt = svgEl('text', { x: gx, y: bandBot + 12, class: 'dn-fieldbars-gatelab', 'text-anchor': 'middle' });
      gt.textContent = 'gate −' + fmt(margin, 2);
      svg.appendChild(gt);
    }
  }

  rows.forEach((row, i) => {
    const cy = top + i * rowH + rowH / 2;
    const c = row.c;
    const id = String(c.id);
    const cls = row.outcome === 'cleared' ? 'dn-good' : row.outcome === 'failed' ? 'dn-bad' : row.outcome === 'tied' ? 'dn-flat' : 'dn-racing';
    const g = svgEl('g', { class: 'dn-fieldbars-row', tabindex: o.onCompetitor ? '0' : null });
    const x0 = champScalar != null ? X(champScalar) : padL;
    const dx = isNum(row.v) ? X(row.v) : x0;
    // the bar from the champion standard out to the challenger scalar.
    const barCls = 'dn-fieldbars-bar ' + cls + (row.projected ? ' dn-proj' : '') + (row.racing && !row.projected ? ' dn-fieldbars-live' : '');
    if (isNum(row.v) && Math.abs(dx - x0) >= 0.5) {
      svg.appendChild(svgEl('rect', { x: Math.min(x0, dx), y: cy - 3, width: Math.max(1, Math.abs(dx - x0)), height: 6, rx: 1.5, class: barCls }));
    }
    // the challenger marker (a dot at its scalar; bigger when a survivor).
    const dotCls = 'dn-fieldbars-dot ' + cls + (row.projected ? ' dn-proj' : '')
      + (row.racing && !row.projected ? ' dn-fieldbars-livedot' : '')
      + (isNum(row.v) ? '' : ' dn-fieldbars-queued');
    const tip = `${id} vs ${champId || 'champion'}`
      + (isNum(row.v) ? ` · scalar ${fmt(row.v, 3)}` : ' · queued')
      + (champScalar != null && isNum(row.v) ? ` · Δ ${fmtSigned(row.v - champScalar, 2)}` : '')
      + (row.lane ? ' · ' + laneProgressText(row.lane) : '')
      + (row.projected && isNum(row.lane.projected_scalar) ? ` · projected ~${fmt(row.lane.projected_scalar, 2)} (boards streaming)` : '')
      + ` · ${row.outcome}`;
    g.appendChild(hov(svgEl('circle', { cx: dx, cy, r: row.survivor ? 4.4 : 3.3, class: dotCls }), tip));
    // the challenger label + survivor glyph in the left gutter.
    const glyph = row.survivor ? ' ↑' : row.outcome === 'failed' ? ' ✕' : row.outcome === 'tied' ? ' =' : '';
    const lbl = svgEl('text', { x: padL - 6, y: cy + 3, class: 'dn-fieldbars-name ' + cls + (row.projected ? ' dn-proj' : ''), 'text-anchor': 'end' });
    lbl.textContent = shortLabel(id, mini ? 7 : 11) + glyph;
    g.appendChild(lbl);
    // the scalar value just past the marker (settled / projected).
    if (!mini && isNum(row.v)) {
      const vt = svgEl('text', { x: dx + (row.survivor ? 8 : 7), y: cy + 3, class: 'dn-fieldbars-val ' + cls + (row.projected ? ' dn-proj' : ''), 'text-anchor': 'start' });
      vt.textContent = (row.projected ? '~' : '') + fmt(row.v, 3);
      g.appendChild(vt);
    }
    // a live/projected scored progress sub-bar under the marker.
    if (row.lane && (row.lane.inflight || row.lane.done || row.projected)) {
      const bw = mini ? 24 : 40;
      const bx = dx - bw / 2;
      const by = cy + 6;
      const sd = isNum(row.lane.boards_done) ? row.lane.boards_done : row.lane.done;
      const stot = isNum(row.lane.boards_total) ? row.lane.boards_total : row.lane.total;
      const frac = (isNum(stot) && stot > 0) ? Math.min(1, (sd || 0) / stot) : (row.lane.inflight ? 0.5 : 0);
      if (row.projected) {
        g.appendChild(svgEl('rect', { x: bx, y: by, width: bw, height: 2.4, rx: 1, class: 'dn-proj-bar-bg' }));
        g.appendChild(svgEl('rect', { x: bx, y: by, width: Math.max(1, bw * frac), height: 2.4, rx: 1, class: 'dn-proj-bar' }));
      } else {
        g.appendChild(svgEl('rect', { x: bx, y: by, width: bw, height: 2, rx: 1, class: 'dn-fieldbars-bar-bg' }));
        g.appendChild(svgEl('rect', { x: bx, y: by, width: Math.max(1, bw * frac), height: 2, rx: 1, class: 'dn-fieldbars-livebar' }));
      }
    }
    clickable(g, o.onCompetitor && (() => o.onCompetitor(id)));
    svg.appendChild(g);
  });
  return svg;
}

// A stable digest of the gauntletFieldBars model.
export function gauntletFieldBarsDigest(opts) {
  const o = opts || {};
  const field = Array.isArray(o.challengers) ? o.challengers : [];
  return JSON.stringify({
    c: o.championId != null ? String(o.championId) : null,
    cs: isNum(o.championScalar) ? o.championScalar.toFixed(3) : null,
    m: isNum(o.promoteMargin) ? o.promoteMargin.toFixed(3) : null,
    f: field.map((c) => {
      const lane = c && c.lane;
      return [String(c.id),
        isNum(c.scalar) ? c.scalar.toFixed(3) : (isNum(c.delta) ? 'd' + c.delta.toFixed(3) : '?'),
        c.outcome || '', !!c.survivor,
        lane ? (lane.done || 0) + '/' + (lane.total == null ? '?' : lane.total) + ':' + (lane.inflight || 0)
          + (lane.projected ? ':j' + (isNum(lane.projected_scalar) ? lane.projected_scalar.toFixed(3) : '?')
            + '/' + (lane.boards_done == null ? '?' : lane.boards_done) + '/' + (lane.boards_total == null ? '?' : lane.boards_total) : '') : ''];
    }),
  });
}

// ── ELIM RADIAL (concentric bracket — rounds as rings to a center gate) ──
//
// The FINAL liked single-elim study figure (single-elim.html opt 6), plus an
// OPT-IN double-elim mode (double-elim.html opt 8). Rounds are CONCENTRIC RINGS
// narrowing toward the champion seat at the CENTER; each competitor is a spoke
// from the outer ring inward. The per-round segments a gen SURVIVED render
// --good; the segment where it was ELIMINATED turns --bad capped with a red ✕;
// the survivor stays good to the gate ring then dashes accent into the center.
// In double-elim mode the winners' bracket owns the UPPER arc (accent, ●●) and
// the losers' bracket the LOWER arc (caution, ●○), split by a dashed equator; a
// WB→LB drop is a RIM-HUGGING transfer arc (never a chord across the center).
//
// FOUR lifecycle states (mirroring funnelRunner): a still-pending (live) spoke
// segment dashes (queued/in-flight); a spoke with a projected standing reads
// dn-proj; a settled spoke is solid with its final survive/eliminate verdict.
//
// CONVERGENCE: a settled spoke renders byte-identically via the live or the
// completed path.
//
// opts: {
//   rounds: [{ label, matches:[{ competitors:[id], winner, decision, pending,
//                                bracket_slot, projected:{id:{scalar,...}} }] }],
//   championId, benchmarkId, gateState, live, double (bool: double-elim mode),
//   mini|compact, onCompetitor(id)
// }
// The model is the SAME elimModel shape elimFlow consumes; this is a polar
// alternative renderer (so a caller can swap radial ↔ flow on the same data).
export function elimRadial(opts) {
  const o = opts || {};
  const rounds = (Array.isArray(o.rounds) ? o.rounds : []).filter((r) => r && Array.isArray(r.matches));
  const mini = !!(o.mini || o.compact);
  const live = !!o.live;
  const champId = o.championId != null ? String(o.championId) : null;
  const benchId = o.benchmarkId != null ? String(o.benchmarkId) : null;
  const isDouble = !!o.double;

  // ── derive each gen's per-round state (advanced / eliminated / pending) and
  // its bracket side, from the rounds — the same way elimFlow does. ──
  const colsSorted = rounds
    .map((r, i) => ({ r, i, ri: isNum(r.round_index) ? r.round_index : i }))
    .sort((a, b) => a.ri - b.ri || a.i - b.i)
    .map((x) => x.r);
  const nCols = colsSorted.length;
  const genState = new Map();
  const ensure = (id) => {
    const k = String(id);
    if (!genState.has(k)) genState.set(k, { id: k, played: new Set(), advanced: new Set(), lostAt: new Set(), pendingAt: new Set(), eliminatedAt: null, side: 'WB', proj: null });
    return genState.get(k);
  };
  const projByGen = new Map();
  colsSorted.forEach((r, ci) => {
    for (const m of (Array.isArray(r.matches) ? r.matches : [])) {
      const comps = (Array.isArray(m.competitors) ? m.competitors : []).map(String).filter((c) => c && c !== 'tbd');
      const winner = m.winner ? String(m.winner) : null;
      const pending = !!m.pending || (!winner && !m.bye && !m.decision);
      const isLB = String(m.bracket_slot || '').startsWith('LB');
      const projMatch = (m.projected && typeof m.projected === 'object') ? m.projected : null;
      for (const c of comps) {
        const g = ensure(c);
        g.played.add(ci);
        if (isLB) g.side = 'LB';
        if (projMatch && pending && projMatch[c] && isNum(projMatch[c].scalar)) projByGen.set(c, projMatch[c]);
        if (pending) { g.pendingAt.add(ci); continue; }
        if (m.bye) { g.advanced.add(ci); continue; }
        if (winner && c === winner) g.advanced.add(ci);
        else if (winner) g.lostAt.add(ci);
      }
    }
  });
  for (const g of genState.values()) {
    const lost = [...g.lostAt].sort((a, b) => a - b);
    const lastPlayed = g.played.size ? Math.max(...g.played) : -1;
    for (const ci of lost) { if (ci >= lastPlayed) { g.eliminatedAt = ci; break; } }
    if (projByGen.has(g.id)) g.proj = projByGen.get(g.id);
  }
  const gens = [...genState.values()];

  const sz = mini ? 200 : 340;
  const W = sz; const H = sz;
  const cx = W / 2; const cy = H / 2;
  const labelPad = mini ? 22 : 40;
  const R = Math.min(cx, cy) - labelPad;
  const rings = Math.max(2, nCols + 1);             // one ring per round + the center gate ring
  const rr = (k) => R * (1 - k / rings) + (mini ? 5 : 8);

  const svg = svgEl('svg', applyResponsive({
    class: 'dn-elimradial', width: '100%', height: H,
    viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': 'radial bracket — rounds as rings narrowing to the champion gate',
  }, o, W, H, 'dn-elimradial-hero'));
  if (!nCols || !gens.length) {
    const t = svgEl('text', { x: W / 2, y: H / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no bracket rounds yet';
    svg.appendChild(t);
    return svg;
  }

  // the concentric ring guides.
  for (let k = 0; k < rings; k++) {
    svg.appendChild(svgEl('circle', { cx, cy, r: rr(k), class: 'dn-elimradial-ring' + (k === 0 ? ' dn-elimradial-ring-outer' : ''), fill: 'none' }));
  }

  // angular placement. Single-elim: spokes spread full circle. Double-elim: WB
  // on the UPPER arc, LB on the LOWER arc, split by a dashed equator.
  const ang = (frac) => (-90 + frac * 360) * Math.PI / 180;
  const pol = (r, a) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  if (isDouble) {
    // the WB↔LB equator (the horizontal diameter a drop must cross).
    svg.appendChild(svgEl('line', { x1: cx - R - (mini ? 4 : 10), y1: cy, x2: cx + R + (mini ? 4 : 10), y2: cy, class: 'dn-elimradial-equator' }));
  }

  // order spokes: survivors / champion first (deepest reach), then earlier-out.
  const reach = (g) => (g.eliminatedAt == null ? nCols + 1 : g.eliminatedAt);
  gens.sort((a, b) => reach(b) - reach(a) || (a.id === champId ? -1 : b.id === champId ? 1 : 0) || a.id.localeCompare(b.id));

  // assign each spoke an angle. Double-elim: split by side onto two half-arcs.
  const angleOf = new Map();
  if (isDouble) {
    const wb = gens.filter((g) => g.side !== 'LB');
    const lb = gens.filter((g) => g.side === 'LB');
    // WB upper arc: 290°→430° (upper-left→top→upper-right); LB lower arc: 110°→250°.
    const place = (list, a0, a1) => list.forEach((g, i) => {
      const f = list.length > 1 ? (i + 0.5) / list.length : 0.5;
      angleOf.set(g.id, ((a0 + (a1 - a0) * f) - 90) * Math.PI / 180);
    });
    place(wb, 290, 430);
    place(lb, 110, 250);
  } else {
    gens.forEach((g, i) => angleOf.set(g.id, ang((i + 0.0) / gens.length)));
  }

  // each spoke: per-round survival segments + node dots + the loss ✕ / gate dash.
  gens.forEach((g) => {
    const a = angleOf.get(g.id);
    const isChamp = champId != null && g.id === champId;
    const isFormer = benchId != null && g.id === benchId && !isChamp;
    const eliminated = g.eliminatedAt != null;
    const proj = !eliminated && g.proj && isNum(g.proj.scalar) ? g.proj : null;
    // # of survived (advanced) rings = won segments before elimination/gate.
    const surv = eliminated ? g.eliminatedAt : Math.max(g.advanced.size, g.played.size ? Math.max(...g.played) + 1 : 0);
    const lane = svgEl('g', { class: 'dn-elimradial-spoke' + (proj ? ' dn-proj' : ''), tabindex: o.onCompetitor ? '0' : null });

    // green (survived) segments rr(k) → rr(k+1).
    for (let k = 0; k < surv; k++) {
      const [sx, sy] = pol(rr(k), a); const [ex, ey] = pol(rr(k + 1), a);
      lane.appendChild(svgEl('line', { x1: sx, y1: sy, x2: ex, y2: ey, class: 'dn-elimradial-seg dn-good' }));
    }
    // node dots at each survived ring (each cleared round reads as a beat).
    for (let k = 0; k <= surv && k < rings; k++) {
      const [dx, dy] = pol(rr(k), a);
      lane.appendChild(svgEl('circle', { cx: dx, cy: dy, r: mini ? 1.6 : 2, class: 'dn-elimradial-node dn-good' }));
    }
    const pendingSpoke = !eliminated && (g.pendingAt.size > 0) && !isChamp;
    if (eliminated) {
      // the LOSS segment (--bad) capped with a red ✕.
      const [sx, sy] = pol(rr(surv), a); const [ex, ey] = pol(rr(Math.min(surv + 1, rings)), a);
      lane.appendChild(svgEl('line', { x1: sx, y1: sy, x2: ex, y2: ey, class: 'dn-elimradial-seg dn-bad' }));
      lane.appendChild(svgEl('circle', { cx: sx, cy: sy, r: mini ? 1.8 : 2.2, class: 'dn-elimradial-node dn-bad' }));
      const xm = svgEl('text', { x: ex, y: ey + 3.2, class: 'dn-elimradial-cut dn-bad', 'text-anchor': 'middle' });
      xm.textContent = '✕';
      lane.appendChild(xm);
    } else if (isChamp) {
      // the survivor reaches the gate ring (good) then dashes accent into center.
      const [gx, gy] = pol(rr(surv), a);
      lane.appendChild(svgEl('line', { x1: gx, y1: gy, x2: cx, y2: cy, class: 'dn-elimradial-seg dn-elimradial-gateline' }));
    } else if (pendingSpoke) {
      // a still-racing spoke: a dashed (in-flight) segment toward the next ring.
      const [sx, sy] = pol(rr(surv), a); const [ex, ey] = pol(rr(Math.min(surv + 1, rings)), a);
      lane.appendChild(svgEl('line', { x1: sx, y1: sy, x2: ex, y2: ey, class: 'dn-elimradial-seg dn-elimradial-pending' + (proj ? ' dn-proj' : '') }));
    }

    // the outer spoke label, anchored & nudged by quadrant so it never clips.
    const [lx, ly] = pol(rr(0) + (mini ? 4 : 7), a);
    const ca = Math.cos(a); const sa = Math.sin(a);
    const anchor = ca < -0.3 ? 'end' : (ca > 0.3 ? 'start' : 'middle');
    const ldy = sa < -0.3 ? -2 : (sa > 0.3 ? 9 : 3);
    const lblCls = 'dn-elimradial-name ' + (eliminated ? 'dn-bad' : isChamp ? 'dn-good' : isFormer ? 'dn-elimradial-former' : 'dn-good') + (proj ? ' dn-proj' : '');
    const tip = `${g.id}`
      + (isChamp ? ` · champion ${CROWN.current}` : isFormer ? ' · former champion' : eliminated ? ` · eliminated at ${colsSorted[g.eliminatedAt] ? (colsSorted[g.eliminatedAt].label || 'R' + g.eliminatedAt) : 'R' + g.eliminatedAt}` : pendingSpoke ? ' · racing' : ' · advanced')
      + (proj ? ` · projected scalar ~${fmt(proj.scalar, 2)} (boards streaming)` : '')
      + (isDouble ? ` · ${g.side === 'LB' ? "losers' bracket ●○" : "winners' bracket ●●"}` : '');
    const lbl = hov(svgEl('text', { x: lx, y: ly + ldy, class: lblCls, 'text-anchor': anchor }), tip);
    lbl.textContent = shortLabel(g.id, mini ? 5 : 8) + (isChamp ? ' ' + CROWN.current : isFormer ? ' ' + CROWN.former : '') + (proj ? ' ~' : '');
    lane.appendChild(lbl);
    clickable(lane, o.onCompetitor && (() => o.onCompetitor(g.id)));
    svg.appendChild(lane);
  });

  // double-elim: rim-hugging WB→LB transfer arcs (a dropped lane's second life).
  if (isDouble) {
    const transferR = R + (mini ? 3 : 8);
    let li = 0;
    const drops = gens.filter((g) => g.side === 'LB' && g.lostAt.size);
    drops.forEach((g) => {
      const a = angleOf.get(g.id);
      if (a == null) return;
      // a short rim arc just outside the play area, hugging the rim (not a chord).
      const aDeg = (a * 180 / Math.PI) + 90;
      const fromDeg = aDeg - 18;
      const stagger = transferR + li * (mini ? 2 : 4); li++;
      const [fx, fy] = pol(stagger, (fromDeg - 90) * Math.PI / 180);
      const [tx, ty] = pol(stagger, a);
      const large = 0;
      svg.appendChild(svgEl('path', {
        d: `M${fx.toFixed(1)} ${fy.toFixed(1)} A${stagger.toFixed(1)} ${stagger.toFixed(1)} 0 ${large} 1 ${tx.toFixed(1)} ${ty.toFixed(1)}`,
        class: 'dn-elimradial-transfer', fill: 'none',
      }));
    });
  }

  // the CENTER champion gate.
  const gateState = o.gateState || (live ? 'deciding' : (champId ? 'crowned' : 'pending'));
  const crowned = gateState === 'crowned' && !!champId;
  const seatR = mini ? 11 : 14;
  const gateG = svgEl('g', { class: 'dn-elimradial-gate', tabindex: (champId && o.onCompetitor) ? '0' : null });
  gateG.appendChild(svgEl('circle', { cx, cy, r: seatR, class: 'dn-elimradial-seat' + (crowned ? ' dn-good' : '') }));
  const gt = hov(svgEl('text', { x: cx, y: cy + (mini ? 3.6 : 4.5), class: 'dn-elimradial-seatlab' + (crowned ? ' dn-good' : ''), 'text-anchor': 'middle' }),
    crowned ? `${champId} · crowned champion ${CROWN.current}`
      : gateState === 'stands' ? 'champion stands'
        : gateState === 'deciding' ? 'gate deciding…' : 'champion gate');
  gt.textContent = crowned ? CROWN.current : gateState === 'deciding' ? '…' : CROWN.former;
  gateG.appendChild(gt);
  clickable(gateG, (champId && o.onCompetitor) && (() => o.onCompetitor(champId)));
  svg.appendChild(gateG);
  return svg;
}

// A stable digest of the elimRadial model.
export function elimRadialDigest(opts) {
  const o = opts || {};
  const rounds = Array.isArray(o.rounds) ? o.rounds : [];
  return JSON.stringify({
    c: o.championId != null ? String(o.championId) : null,
    b: o.benchmarkId != null ? String(o.benchmarkId) : null,
    g: o.gateState || null, d: !!o.double,
    r: rounds.map((r) => [r.round_index, r.label,
      (Array.isArray(r.matches) ? r.matches : []).map((m) => [m.match_id, (m.competitors || []).map(String).join('/'), m.winner, m.decision, m.bracket_slot, m.bye, !!m.pending,
        m.projected ? Object.keys(m.projected).sort().map((k) => k + ':' + (isNum(m.projected[k].scalar) ? m.projected[k].scalar.toFixed(3) : '?')).join(',') : '']),
    ]),
  });
}

// ── RADAR SILHOUETTE (challenger vs champion across the gate's axes) ──
//
// The FINAL liked single-generation study figure (single-generation.html opt 2's
// radar panel). A polar silhouette comparing the CHALLENGER (filled accent
// polygon) to the CHAMPION (dashed faint polygon) across the axes the gate weighs
// — scalar (inverse), pass-rate, and each per-judge drift — with OUTER = better.
// Each vertex carries a hover tooltip with the underlying value per axis; a
// candidate vertex reads --good when it dominates the champion on that axis,
// --bad when it pulls in.
//
// Lifecycle: a candidate still on boards passes `live:true` + projected axes; the
// candidate polygon then reads in the dn-proj (dashed/amber) treatment. Absent
// live data → settled (solid). CONVERGENCE: a settled silhouette renders
// byte-identically via the live or completed path.
//
// opts: {
//   axes: [{ label, chal, champ }],   // normalised radii 0..1, OUTER = better
//   raw:  [{ chal, champ, unit, better }],   // optional underlying values (tips)
//   live, mini|compact, legend (default !mini), onAxis(label)
// }
export function radarSilhouette(opts) {
  const o = opts || {};
  const axes = (Array.isArray(o.axes) ? o.axes : []).filter((a) => a && isNum(a.chal) && isNum(a.champ));
  const raw = Array.isArray(o.raw) ? o.raw : [];
  const mini = !!(o.mini || o.compact);
  const live = !!o.live;
  const legend = o.legend != null ? !!o.legend : !mini;
  const n = axes.length;

  const W = mini ? 200 : (legend ? 560 : 360);
  const H = mini ? 200 : 360;
  const cx = mini ? W / 2 : (legend ? 210 : W / 2);
  const cy = H / 2 - (mini ? 0 : 6);
  const R = mini ? 76 : 128;

  const svg = svgEl('svg', applyResponsive({
    class: 'dn-radar', width: '100%', height: H,
    viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': 'radar — challenger vs champion across the gate axes; outer = better',
  }, o, W, H, 'dn-radar-hero'));
  if (n < 3) {
    const t = svgEl('text', { x: W / 2, y: H / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'not enough axes to plot';
    svg.appendChild(t);
    return svg;
  }
  const angle = (i) => -Math.PI / 2 + i * 2 * Math.PI / n;
  const P = (i, r) => {
    const rr = Math.max(0.05, Math.min(1, r));
    return [cx + Math.cos(angle(i)) * R * rr, cy + Math.sin(angle(i)) * R * rr];
  };

  // concentric grid rings.
  for (const ring of [0.25, 0.5, 0.75, 1]) {
    const pts = axes.map((_, i) => P(i, ring).join(',')).join(' ');
    svg.appendChild(svgEl('polygon', { points: pts, class: 'dn-radar-ring', fill: 'none' }));
  }
  // spokes + axis LABELS — render each axis's TEXT (opts.axes[].label) at the
  // tip (the contract: the label, NOT an index 1..n), with the full label on
  // hover. Long labels are TRUNCATED to a length that scales with the per-axis
  // angular budget, and labels near the top/bottom (where the radius runs out
  // before the next spoke) are ROTATED to follow the spoke so they don't overlap
  // their neighbours. The mini radar suppresses tip labels (too small) but its
  // vertices still carry the label on hover.
  // budget: with more axes each gets less room, so truncate harder.
  const labelMax = n <= 6 ? 16 : n <= 8 ? 12 : n <= 12 ? 9 : 7;
  axes.forEach((a, i) => {
    const [ex, ey] = P(i, 1);
    svg.appendChild(svgEl('line', { x1: cx, y1: cy, x2: ex, y2: ey, class: 'dn-radar-spoke' }));
    if (mini) return;
    const [lx, ly] = P(i, 1.14);
    const dx = lx - cx;
    const dy = ly - cy;
    const near = Math.abs(dx) < R * 0.34;                // near the vertical (top/bottom)
    const anchor = Math.abs(dx) < 6 ? 'middle' : dx < 0 ? 'end' : 'start';
    const full = String(a.label == null ? '' : a.label);
    // a near-vertical, LONG label rotates to follow the spoke (so a long axis
    // name at top/bottom does not run across its neighbours); others stay
    // horizontal and rely on truncation + the quadrant anchor.
    const rotate = near && full.length > labelMax;
    const t = svgEl('text', {
      x: lx, y: ly + 3, class: 'dn-radar-axislab',
      'text-anchor': rotate ? (dy < 0 ? 'start' : 'end') : anchor,
    });
    if (rotate) {
      // rotate around the tip so the text runs outward along the spoke direction.
      const deg = Math.atan2(dy, dx) * 180 / Math.PI + (dy < 0 ? 90 : -90);
      t.setAttribute('transform', `rotate(${deg.toFixed(1)} ${lx.toFixed(1)} ${(ly + 3).toFixed(1)})`);
    }
    t.textContent = shortLabel(full, labelMax);
    hov(t, full);
    svg.appendChild(t);
  });

  // the champion polygon (dashed, faint).
  const champPts = axes.map((a, i) => P(i, a.champ).join(',')).join(' ');
  svg.appendChild(svgEl('polygon', { points: champPts, class: 'dn-radar-champ', fill: 'none' }));
  axes.forEach((a, i) => { const [x, y] = P(i, a.champ); svg.appendChild(svgEl('circle', { cx: x, cy: y, r: mini ? 1.8 : 2.5, class: 'dn-radar-champ-dot', fill: 'none' })); });

  // the BRADLEY–TERRY credible-interval BAND on an axis vertex (the scalar axis
  // carries `chalBand:{lo,hi}` in radius space) — a short radial whisker from the
  // inner to the outer credible radius along the spoke, so the candidate vertex
  // reads as an interval, not a false point. Drawn UNDER the candidate polygon so
  // the vertex dot still sits on top. Absent on every axis → byte-identical.
  axes.forEach((a, i) => {
    if (!a.chalBand || !isNum(a.chalBand.lo) || !isNum(a.chalBand.hi)) return;
    const [ix, iy] = P(i, a.chalBand.lo);
    const [ox, oy] = P(i, a.chalBand.hi);
    svg.appendChild(svgEl('line', { x1: ix, y1: iy, x2: ox, y2: oy, class: 'dn-radar-ciband' + (live ? ' dn-proj' : '') }));
    // small ticks at each credible endpoint, perpendicular to the spoke.
    const ang = angle(i);
    const tx = Math.cos(ang + Math.PI / 2), ty = Math.sin(ang + Math.PI / 2);
    const tk = mini ? 2.5 : 3.5;
    svg.appendChild(svgEl('line', { x1: ix - tx * tk, y1: iy - ty * tk, x2: ix + tx * tk, y2: iy + ty * tk, class: 'dn-radar-citick' + (live ? ' dn-proj' : '') }));
    svg.appendChild(svgEl('line', { x1: ox - tx * tk, y1: oy - ty * tk, x2: ox + tx * tk, y2: oy + ty * tk, class: 'dn-radar-citick' + (live ? ' dn-proj' : '') }));
  });

  // the candidate polygon (filled accent; dn-proj when live/projected).
  const candPts = axes.map((a, i) => P(i, a.chal).join(',')).join(' ');
  svg.appendChild(svgEl('polygon', { points: candPts, class: 'dn-radar-cand' + (live ? ' dn-proj' : ''), 'aria-hidden': 'true' }));
  // vertex dots coloured by per-axis dominance + a generous hover hit-target.
  const fmtV = (v, u) => (u === 'rate' ? (v * 100).toFixed(1) + '%' : fmt(v, 3));
  axes.forEach((a, i) => {
    const [x, y] = P(i, a.chal);
    const better = a.chal >= a.champ;
    const dot = svgEl('circle', { cx: x, cy: y, r: mini ? 2.6 : 3.5, class: 'dn-radar-cand-dot ' + (better ? 'dn-good' : 'dn-bad') + (live ? ' dn-proj' : '') });
    const r = raw[i];
    const tip = r && isNum(r.chal) && isNum(r.champ)
      ? `${a.label} · cand ${fmtV(r.chal, r.unit)} vs champ ${fmtV(r.champ, r.unit)} (${r.better || 'lower'} = better)`
      : `${a.label} · ${better ? 'dominates' : 'pulls in'} (cand ${fmt(a.chal, 2)} vs champ ${fmt(a.champ, 2)})`;
    svg.appendChild(hov(dot, tip));
    // a larger transparent hit-circle so the hover target stays generous.
    const hit = svgEl('circle', { cx: x, cy: y, r: mini ? 8 : 11, class: 'dn-radar-hot', fill: 'transparent', tabindex: o.onAxis ? '0' : null });
    hov(hit, tip);
    clickable(hit, o.onAxis && (() => o.onAxis(String(a.label))));
    svg.appendChild(hit);
  });

  // the optional legend block.
  if (legend) {
    const lx = W - 118; const ly = 64;
    svg.appendChild(svgEl('rect', { x: lx - 10, y: ly - 18, width: 116, height: 88, rx: 6, class: 'dn-radar-legendbox' }));
    const cap = svgEl('text', { x: lx, y: ly, class: 'dn-radar-legendcap' }); cap.textContent = 'outer = better';
    svg.appendChild(cap);
    svg.appendChild(svgEl('line', { x1: lx, y1: ly + 16, x2: lx + 22, y2: ly + 16, class: 'dn-radar-cand-key' }));
    const ck = svgEl('text', { x: lx + 28, y: ly + 19, class: 'dn-radar-legendlab' }); ck.textContent = 'candidate';
    svg.appendChild(ck);
    svg.appendChild(svgEl('line', { x1: lx, y1: ly + 34, x2: lx + 22, y2: ly + 34, class: 'dn-radar-champ-key' }));
    const hk = svgEl('text', { x: lx + 28, y: ly + 37, class: 'dn-radar-legendlab' }); hk.textContent = 'champion';
    svg.appendChild(hk);
    const gk = svgEl('text', { x: lx, y: ly + 56, class: 'dn-radar-legendlab dn-good' }); gk.textContent = '● gain';
    svg.appendChild(gk);
    const bk = svgEl('text', { x: lx + 52, y: ly + 56, class: 'dn-radar-legendlab dn-bad' }); bk.textContent = '● loss';
    svg.appendChild(bk);
  }
  return svg;
}

// A stable digest of the radarSilhouette model.
export function radarSilhouetteDigest(opts) {
  const o = opts || {};
  const axes = Array.isArray(o.axes) ? o.axes : [];
  return JSON.stringify({
    l: !!o.live,
    a: axes.map((a) => [String(a.label), isNum(a.chal) ? a.chal.toFixed(3) : '?', isNum(a.champ) ? a.champ.toFixed(3) : '?',
      // the BT credible-interval band (rounded radii, no timestamps) so a CI
      // tightening repaints the radar but a no-op beat stays byte-identical.
      // absent on every non-rating axis → no contribution (back-compat digest).
      a.chalBand && isNum(a.chalBand.lo) && isNum(a.chalBand.hi)
        ? [a.chalBand.lo.toFixed(3), a.chalBand.hi.toFixed(3)] : null]),
  });
}

// The elim epoch overview + Match-ups both render the BRACKET-AS-FLOW
// (`elimFlow`) — the seat/box bracket tree (`elimBracket`) is retired.

// ---- Tufte Sankey (fit-to-width) — the causal patch→drift→gate flow -
export function layoutSankey(spec) {
  const colW = spec.nodeW || 150;
  const top = spec.top || 30;
  const colHeight = spec.colHeight || 360;
  const minNodeH = spec.minNodeH || 22;
  const gap = spec.nodeGap || 12;
  const totalW = spec.width || 720;

  const stages = ['patch', 'drift', 'gate'];
  const cols = { patch: spec.patch || [], drift: spec.drift || [], gate: spec.gate || [] };
  const links = spec.links || [];

  // Fit to width: three columns + two gaps fill the container exactly.
  const colGap = Math.max(40, (totalW - 3 * colW) / 2);

  const throughput = (nodeId) => {
    let t = 0;
    for (const l of links) if (l.source === nodeId || l.target === nodeId) t += Math.abs(l.value || 0);
    return t;
  };

  const positioned = new Map();
  const nodesOut = [];
  stages.forEach((stage, si) => {
    const list = cols[stage];
    const x = si * (colW + colGap);
    const raw = list.map((n) => Math.max(0.0001, n.value != null ? Math.abs(n.value) : throughput(n.id)));
    const total = raw.reduce((a, b) => a + b, 0) || 1;
    const avail = colHeight - gap * Math.max(0, list.length - 1);
    const heights = raw.map((r) => Math.max(minNodeH, (r / total) * avail));
    const blockH = heights.reduce((a, b) => a + b, 0) + gap * Math.max(0, list.length - 1);
    let y = top + Math.max(0, (colHeight - blockH) / 2);
    list.forEach((n, i) => {
      const h = heights[i];
      const node = { id: n.id, stage, x, y, h, w: colW, label: n.label != null ? n.label : n.id, sub: n.sub || '', cls: n.cls || '', value: n.value, ref: n.ref || null, _outCursor: 0, _inCursor: 0 };
      positioned.set(n.id, node);
      nodesOut.push(node);
      y += h + gap;
    });
  });

  const linksOut = [];
  const outSum = new Map();
  const inSum = new Map();
  for (const l of links) {
    outSum.set(l.source, (outSum.get(l.source) || 0) + Math.abs(l.value || 0));
    inSum.set(l.target, (inSum.get(l.target) || 0) + Math.abs(l.value || 0));
  }
  for (const l of links) {
    const s = positioned.get(l.source);
    const t = positioned.get(l.target);
    if (!s || !t) continue;
    const v = Math.abs(l.value || 0) || 0.0001;
    const sBand = s.h * (v / (outSum.get(l.source) || v));
    const tBand = t.h * (v / (inSum.get(l.target) || v));
    const sx = s.x + s.w;
    const tx = t.x;
    const sy = s.y + s._outCursor + sBand / 2;
    const ty = t.y + t._inCursor + tBand / 2;
    s._outCursor += sBand;
    t._inCursor += tBand;
    linksOut.push({ id: l.id || `${l.source}__${l.target}`, source: l.source, target: l.target, sx, sy, tx, ty, hwS: Math.max(0.6, sBand / 2), hwT: Math.max(0.6, tBand / 2), value: l.value, cls: l.cls || '' });
  }
  const box = { x: 0, y: 0, w: totalW, h: colHeight + top * 2 };
  return { nodes: nodesOut, links: linksOut, box };
}

// Render a fit-to-width Tufte Sankey to an <svg>. Reads the layout above.
// opts: same as layoutSankey + { onNode }.
export function sankey(opts) {
  const o = opts || {};
  const { nodes, links, box } = layoutSankey(o);
  const svg = svgEl('svg', {
    class: 'dn-sankey', width: '100%', height: box.h,
    viewBox: `0 0 ${box.w} ${box.h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
  });
  if (nodes.length === 0) {
    const t = svgEl('text', { x: box.w / 2, y: box.h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no causal flow yet';
    svg.appendChild(t);
    return svg;
  }
  const stageHead = { patch: 'PATCH', drift: 'PER-BOARD DRIFT', gate: 'GATE' };
  const byStage = {};
  for (const n of nodes) (byStage[n.stage] = byStage[n.stage] || []).push(n);
  for (const stage of Object.keys(byStage)) {
    const x = byStage[stage][0].x + byStage[stage][0].w / 2;
    const t = svgEl('text', { x, y: 14, class: 'dn-sankey-head', 'text-anchor': 'middle' });
    t.textContent = stageHead[stage] || stage;
    svg.appendChild(t);
  }
  // ribbons (drawn first, behind nodes) — thin filled paths.
  const linkLayer = svgEl('g', { class: 'dn-sankey-links' });
  for (const l of links) {
    const mx = (l.sx + l.tx) / 2;
    const d = `M ${l.sx} ${l.sy - l.hwS} `
      + `C ${mx} ${l.sy - l.hwS}, ${mx} ${l.ty - l.hwT}, ${l.tx} ${l.ty - l.hwT} `
      + `L ${l.tx} ${l.ty + l.hwT} `
      + `C ${mx} ${l.ty + l.hwT}, ${mx} ${l.sy + l.hwS}, ${l.sx} ${l.sy + l.hwS} Z`;
    linkLayer.appendChild(hov(svgEl('path', { d, class: 'dn-sankey-ribbon ' + (l.cls || ''), fill: 'currentColor' }), `${l.source} → ${l.target}: ${fmt(l.value, 1)}`));
  }
  svg.appendChild(linkLayer);
  // nodes — thin bars + direct in-place labels. FIX #5: the per-board node's
  // LABEL and its loss VALUE must never overlap. The label sits on the top
  // baseline (truncated short so it cannot run under the value); the loss value
  // "picky_stakeholder_emu…" and its "642" can never collide.
  const nodeLayer = svgEl('g', { class: 'dn-sankey-nodes' });
  for (const n of nodes) {
    const g = svgEl('g', { class: 'dn-sankey-node ' + (n.cls || ''), tabindex: o.onNode ? '0' : null });
    g.appendChild(hov(svgEl('rect', { x: n.x, y: n.y, width: 6, height: n.h, rx: 1, class: 'dn-sankey-bar' }), `${n.label}${isNum(n.value) ? ' · ' + fmt(n.value, 1) : ''}`));
    const anchor = n.stage === 'gate' ? 'end' : 'start';
    const lx = n.stage === 'gate' ? n.x - 6 : n.x + 12;
    const ty = n.y + n.h / 2;
    // Drift (middle) nodes carry a numeric loss value; reserve room for it by
    // truncating the label harder, and right-align the value to the node's far
    // edge so the two strings sit on the same baseline without overlapping.
    const hasValue = n.stage === 'drift' && isNum(n.value);
    const lbl = svgEl('text', { x: lx, y: ty - 1, class: 'dn-sankey-label', 'text-anchor': anchor });
    lbl.textContent = shortLabel(String(n.label), hasValue ? 16 : 22);
    g.appendChild(lbl);
    if (hasValue) {
      const vx = n.x + n.w; // far (right) edge of this column's node band
      const val = svgEl('text', { x: vx, y: ty - 1, class: 'dn-sankey-value', 'text-anchor': 'end' });
      val.textContent = fmt(n.value, 0);
      g.appendChild(val);
    }
    if (n.sub) {
      const sub = svgEl('text', { x: lx, y: ty + 11, class: 'dn-sankey-sub', 'text-anchor': anchor });
      sub.textContent = shortLabel(String(n.sub), 24);
      g.appendChild(sub);
    }
    clickable(g, o.onNode && (() => o.onNode(n)));
    nodeLayer.appendChild(g);
  }
  svg.appendChild(nodeLayer);
  return svg;
}

// ---- small-multiple wrapper -----------------------------------------

export function smallMultiple(caption, mark, sub) {
  return el('figure', { class: 'dn-sm' }, [
    el('figcaption', { class: 'dn-sm-cap' }, [
      el('span', { class: 'dn-sm-title', text: caption == null ? '' : String(caption) }),
      sub ? el('span', { class: 'dn-sm-sub', text: String(sub) }) : null,
    ]),
    mark,
  ]);
}

// ---- SIDE-BY-SIDE line diff (champion baseline | challenger new) ----
// opts: { baseline: string, challenger: string, leftLabel, rightLabel }
export function sideBySideDiff(opts) {
  const o = opts || {};
  const leftText = o.baseline == null ? '' : String(o.baseline);
  const rightText = o.challenger == null ? '' : String(o.challenger);
  const a = leftText.replace(/\r\n/g, '\n').split('\n');
  const b = rightText.replace(/\r\n/g, '\n').split('\n');
  const rows = lcsDiff(a, b);

  const wrap = el('div', { class: 'dn-sxs' });
  const head = el('div', { class: 'dn-sxs-head' }, [
    el('span', { class: 'dn-sxs-col-h dn-sxs-old', text: o.leftLabel || 'champion baseline' }),
    el('span', { class: 'dn-sxs-col-h dn-sxs-new', text: o.rightLabel || 'challenger new' }),
  ]);
  wrap.appendChild(head);

  const body = el('div', { class: 'dn-sxs-body', role: 'list' });
  let ln = 0; let rn = 0;
  for (const r of rows) {
    const cls = r.type === 'same' ? '' : (r.type === 'del' ? ' dn-sxs-changed' : (r.type === 'add' ? ' dn-sxs-changed' : ' dn-sxs-changed'));
    const lhsText = r.left != null ? r.left : '';
    const rhsText = r.right != null ? r.right : '';
    const lGutter = r.left != null ? String(++ln) : '';
    const rGutter = r.right != null ? String(++rn) : '';
    body.appendChild(el('div', { class: 'dn-sxs-row' + cls, role: 'listitem' }, [
      el('span', { class: 'dn-sxs-gutter', 'aria-hidden': 'true', text: lGutter }),
      el('span', { class: 'dn-sxs-cell dn-sxs-old' + (r.type === 'del' || r.type === 'mod' ? ' dn-sxs-del' : ''), text: r.left == null ? '' : (lhsText === '' ? '​' : lhsText) }),
      el('span', { class: 'dn-sxs-gutter', 'aria-hidden': 'true', text: rGutter }),
      el('span', { class: 'dn-sxs-cell dn-sxs-new' + (r.type === 'add' || r.type === 'mod' ? ' dn-sxs-add' : ''), text: r.right == null ? '' : (rhsText === '' ? '​' : rhsText) }),
    ]));
  }
  wrap.appendChild(body);
  return wrap;
}

// A compact LCS line-diff → aligned rows: {type:'same'|'mod'|'del'|'add',
// left, right}. 'mod' pairs a deleted line with an added line on the same row.
function lcsDiff(a, b) {
  const n = a.length; const m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const rows = [];
  let i = 0; let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { rows.push({ type: 'same', left: a[i], right: b[j] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { rows.push({ type: 'del', left: a[i], right: null }); i++; }
    else { rows.push({ type: 'add', left: null, right: b[j] }); j++; }
  }
  while (i < n) { rows.push({ type: 'del', left: a[i], right: null }); i++; }
  while (j < m) { rows.push({ type: 'add', left: null, right: b[j] }); j++; }
  // Coalesce an adjacent del+add into a single 'mod' row so a one-line edit
  // reads as a side-by-side replacement rather than two stacked rows.
  const out = [];
  for (let k = 0; k < rows.length; k++) {
    const cur = rows[k]; const nxt = rows[k + 1];
    if (cur.type === 'del' && nxt && nxt.type === 'add') {
      out.push({ type: 'mod', left: cur.left, right: nxt.right }); k++;
    } else out.push(cur);
  }
  return out;
}

// ---- proposing-step tracker -----------------------------------------
//
// The candidate-generation step rendered as the field FORMS: one row per
// challenger slot. Each row makes the proposal phase LEGIBLE rather than a
// black box — for a rejected slot it shows the SPECIFIC reason (the
// `expected_metric_movements` validation message, an empty/parse failure, a
// post-apply error) inline + the retry count, with every attempt's reason on
// hover; for an applied slot it shows the hypothesis; for an in-flight slot
// it shows a "proposing…" pending state. `onCompetitor(gid)` (optional) makes
// an applied row a drill-in affordance.
export function proposingTracker(opts) {
  const o = opts || {};
  const list = (Array.isArray(o.fieldStatus) ? o.fieldStatus : []).filter((f) => f && f.generation_id);
  const applied = list.filter((f) => f.status === 'applied').length;
  const proposing = list.filter((f) => f.status === 'proposing').length;
  const rejected = list.filter((f) => f.status === 'rejected').length;
  const proposed = list.length;
  // "all rejected" is only a verdict once the field has FULLY settled — a slot
  // still proposing must not flip the headline to the alarming all-bad state.
  const allRejected = proposed > 0 && applied === 0 && proposing === 0;
  const onCompetitor = typeof o.onCompetitor === 'function' ? o.onCompetitor : null;

  // headline counts — never an empty/idle read for a field that minted rows.
  let head;
  if (proposed === 0) {
    head = 'minting the field…';
  } else {
    head = `${proposed} proposed · ${applied} applied`;
    if (rejected > 0) head += ` · ${rejected} rejected`;
    if (proposing > 0) head += ` · ${proposing} proposing…`;
    else if (allRejected) head += ' — all rejected';
  }

  const rows = list.map((f) => {
    const st = f.status === 'applied' ? 'applied' : (f.status === 'proposing' ? 'proposing' : 'rejected');
    const ok = st === 'applied';
    const pending = st === 'proposing';
    const glyph = el('span', {
      class: 'dn-prop-glyph ' + (ok ? 'dn-prop-ok' : pending ? 'dn-prop-pending' : 'dn-prop-bad'),
      'aria-hidden': 'true', text: ok ? '✓' : pending ? '⋯' : '✗',
    });
    const gid = el('span', { class: 'dn-prop-gen', text: shortLabel(String(f.generation_id), 16) });
    const verdict = el('span', {
      class: 'dn-prop-verdict ' + (ok ? 'dn-prop-ok' : pending ? 'dn-prop-pending' : 'dn-prop-bad'),
      text: ok ? 'applied' : pending ? 'proposing…' : 'rejected',
    });

    // The retry badge — only when more than one attempt was made (a retried
    // slot is worth flagging; a clean first-try slot stays uncluttered).
    const attempts = (typeof f.attempts === 'number' && f.attempts > 0) ? f.attempts : null;
    const topRow = [glyph, gid, verdict];
    if (!pending && attempts != null && attempts > 1) {
      topRow.push(el('span', {
        class: 'dn-prop-attempts dn-faint',
        text: `${attempts} attempts`,
        title: `${attempts} proposer attempts before this outcome`,
      }));
    }

    // The DETAIL line — the part that makes the phase legible. For a rejected
    // slot it is the SPECIFIC final reason inline (faint mono), so a
    // file_findability-style validation rejection is plainly visible without a
    // hover. For an applied slot it is the hypothesis. Proposing slots show a
    // muted pending note.
    let detailText = '';
    let detailClass = 'dn-prop-detail dn-faint';
    if (pending) {
      detailText = 'proposing…';
    } else if (ok) {
      detailText = f.hypothesis ? String(f.hypothesis) : 'applied cleanly';
    } else {
      detailText = f.reason ? String(f.reason) : 'no reason recorded';
      detailClass = 'dn-prop-detail dn-prop-reason';
    }
    const detail = el('div', { class: detailClass, text: detailText });

    const rowClass = 'dn-prop-row ' + (ok ? 'dn-prop-row-ok' : pending ? 'dn-prop-row-pending' : 'dn-prop-row-bad');
    const top = el('div', { class: 'dn-prop-rowtop' }, topRow);
    const row = el('div', { class: rowClass, role: 'listitem' }, [top, detail]);

    // Hovercard carries the FULL per-attempt list when a slot was retried, so
    // every attempt's reason is recoverable even though the inline line shows
    // only the final one.
    const reasons = Array.isArray(f.attempt_reasons) ? f.attempt_reasons : [];
    let tip;
    if (pending) {
      tip = `${f.generation_id}: proposing…`;
    } else if (ok) {
      tip = `${f.generation_id} applied cleanly` + (f.hypothesis ? `\n${f.hypothesis}` : '');
    } else if (reasons.length > 1) {
      tip = `${f.generation_id} rejected after ${reasons.length} attempts:\n`
        + reasons.map((r, i) => `  attempt ${i + 1}: ${r}`).join('\n');
    } else {
      tip = `${f.generation_id} rejected: ${f.reason || (reasons[0] || 'no reason recorded')}`;
    }
    hov(row, tip);

    if (ok && onCompetitor) {
      row.classList.add('dn-prop-clickable');
      row.tabIndex = 0;
      row.addEventListener('click', () => onCompetitor(String(f.generation_id)));
      row.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onCompetitor(String(f.generation_id)); }
      });
    }
    return row;
  });

  const headNode = el('div', {
    class: 'dn-prop-head' + (allRejected ? ' dn-prop-head-allbad' : ''),
    text: head,
  });
  const listNode = el('div', { class: 'dn-prop-list', role: 'list' }, rows);
  return el('div', { class: 'dn-prop-tracker', role: 'group', 'aria-label': 'Proposed field' }, [
    el('div', { class: 'dn-prop-caption dn-faint', text: 'proposed field' }),
    headNode,
    listNode,
  ]);
}

// A stable digest of the proposing-step field so the live hero can
// digest-gate the tracker swap (a no-op heartbeat writes ZERO DOM). Folds in
// the attempt count + final reason so a slot transitioning proposing → retry
// → settled re-stamps, but a steady no-op tick does not.
export function proposingDigest(fieldStatus) {
  const list = Array.isArray(fieldStatus) ? fieldStatus : [];
  return 'prop|' + list.map((f) => {
    if (!f) return '';
    const att = (typeof f.attempts === 'number') ? f.attempts : '';
    const reason = f.reason ? String(f.reason).slice(0, 48) : '';
    return `${f.generation_id}:${f.status}:${att}:${reason}`;
  }).join(',');
}

// ── the CHAMPION-SPINE ROUND TIMELINE — the epoch overview hero ──────
//
// The epoch is N evolve ROUNDS along a horizontal CHAMPION SPINE: one node per
// round's incoming champion (v0 → promoted → …), each annotated with its loss so
// the DESCENDING LOSS-FLOOR reads as the headline "is it improving?" signal.
//
// Each round is an EPISODE card below its spine node:
//   incoming champion + a fan of that round's MINTED challengers + a COMPACT
//   per-round STRUCTURE FIGURE (the caller passes it — swissOverview / survival
//   funnel / elimFlow / a single duel) + the GATE OUTCOME (promoted onto the
//   spine, or held). Clicking the episode (or its spine node) drills into that
//   round's tournament.
//
// SUBSUMES the gauntlet reel: a single round (every run so far, --rounds 1)
// degrades to ONE episode ≈ today's overview; N rounds → the full spine.
//
//   opts: {
//     rounds: [{ round_index, champion:{id,scalar}, challengers:[{id,scalar,promoted}],
//                structure, gateOutcome:{kind,gen} }],
//     selected:   the selected round_index (or null),
//     figureFor(round) → a DOM node (the per-round structure figure) | null,
//     onRound(round_index), onCompetitor(genId),
//   }
export function roundTimeline(opts) {
  const o = opts || {};
  const rounds = Array.isArray(o.rounds) ? o.rounds : [];
  const wrap = el('div', { class: 'dn-roundtl', role: 'group', 'aria-label': 'Champion-spine round timeline' });
  if (!rounds.length) {
    wrap.appendChild(el('p', { class: 'dn-empty', text: 'No rounds have run in this epoch yet — the timeline fills as the evolve loop mints fields.' }));
    return wrap;
  }
  const single = rounds.length === 1;

  // ── the fit-to-width SPINE (champion node per round) ────────────────
  // A FIXED viewBox; the champion node of round 0 is the seed, each subsequent
  // node the round's incoming (carried) champion. The loss annotation under each
  // node lets the descending floor read at a glance.
  const VBW = 1000;
  const VBH = 96;
  const spineY = 54;
  const x0 = 64;
  const xMax = VBW - 56;
  const stationCount = Math.max(1, rounds.length);
  const step = stationCount > 1 ? (xMax - x0) / (stationCount - 1) : 0;
  const xAt = (i) => (stationCount > 1 ? x0 + i * step : (x0 + xMax) / 2);
  const svg = svgEl('svg', {
    class: 'dn-roundtl-spine', viewBox: `0 0 ${VBW} ${VBH}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': `Champion spine across ${rounds.length} round${single ? '' : 's'}`,
  });
  if (stationCount > 1) {
    svg.appendChild(svgEl('line', {
      x1: xAt(0), y1: spineY, x2: xAt(stationCount - 1), y2: spineY, class: 'dn-roundtl-spineline',
    }));
  }
  svg.appendChild(svgEl('text', { x: x0, y: 18, class: 'dn-roundtl-axis' }, ['champion spine · loss floor · rounds →']));

  rounds.forEach((r, i) => {
    const cx = xAt(i);
    const champId = r.champion && r.champion.id != null ? String(r.champion.id) : 'seed';
    const promoted = r.gateOutcome && r.gateOutcome.kind === 'promoted';
    const selected = o.selected != null && String(o.selected) === String(r.round_index);
    const g = svgEl('g', {
      class: 'dn-roundtl-node' + (promoted ? ' dn-roundtl-promote' : '')
        + (r.inflight ? ' dn-roundtl-live' : '') + (selected ? ' dn-roundtl-sel' : ''),
      tabindex: o.onRound ? '0' : null, role: o.onRound ? 'button' : null,
      'data-round': String(r.round_index),
      'aria-label': `Round ${r.round_index}: champion ${champId}`
        + (isNum(r.champion && r.champion.scalar) ? `, loss ${fmt(r.champion.scalar, 1)}` : '')
        + (r.inflight ? ', in-flight — proposing the field'
          : promoted ? `, promoted ${r.gateOutcome.gen}` : ', champion held'),
    }, [
      svgEl('circle', { cx, cy: spineY, r: 8, class: 'dn-roundtl-disc' }),
      svgEl('text', { x: cx, y: spineY + 3.5, class: 'dn-roundtl-glyph', 'text-anchor': 'middle' }, [CROWN.current]),
      svgEl('text', { x: cx, y: spineY - 16, class: 'dn-roundtl-champid', 'text-anchor': 'middle' }, [shortLabel(champId, 10)]),
      svgEl('text', { x: cx, y: spineY + 26, class: 'dn-roundtl-loss', 'text-anchor': 'middle' },
        [isNum(r.champion && r.champion.scalar) ? fmt(r.champion.scalar, 1) : '·']),
      svgEl('text', { x: cx, y: spineY + 38, class: 'dn-roundtl-rord', 'text-anchor': 'middle' }, ['r' + r.round_index]),
    ]);
    clickable(g, o.onRound && (() => o.onRound(r.round_index)));
    svg.appendChild(g);
  });
  // The spine plots a champion trajectory across rounds — meaningless for a
  // single round (one node floating in a wide empty viewBox reads as broken).
  // A single-round epoch shows just its episode card below.
  if (!single) wrap.appendChild(el('div', { class: 'dn-roundtl-spineframe' }, [svg]));

  // ── one EPISODE card per round ──────────────────────────────────────
  const episodes = el('div', { class: 'dn-roundtl-episodes' + (single ? ' dn-roundtl-single' : '') });
  rounds.forEach((r) => {
    const champId = r.champion && r.champion.id != null ? String(r.champion.id) : 'seed';
    const promoted = r.gateOutcome && r.gateOutcome.kind === 'promoted';
    const selected = o.selected != null && String(o.selected) === String(r.round_index);
    const card = el('div', {
      class: 'dn-roundtl-ep' + (selected ? ' dn-roundtl-ep-sel' : ''),
      'data-round': String(r.round_index), role: 'group',
      'aria-label': `Round ${r.round_index} episode`,
    });
    // episode header: round ordinal + the incoming champion + a drill link. An
    // IN-FLIGHT round (still proposing/applying its field, no settled gate yet)
    // wears a LIVE badge so it reads as the round forming NOW (issue #16).
    const head = el('div', { class: 'dn-roundtl-ephead' + (r.inflight ? ' dn-roundtl-ephead-live' : '') }, [
      el('span', { class: 'dn-roundtl-eptag', text: 'round ' + r.round_index }),
      r.inflight ? el('span', { class: 'dn-roundtl-eplive', 'aria-label': 'in-flight round', text: 'LIVE' }) : null,
      el('span', { class: 'dn-roundtl-epchamp' }, [
        el('span', { class: 'dn-roundtl-epcrown', 'aria-hidden': 'true', text: CROWN.current }),
        el('span', { class: 'dn-mono', text: champId }),
        isNum(r.champion && r.champion.scalar)
          ? el('span', { class: 'dn-faint', text: ' · loss ' + fmt(r.champion.scalar, 1) }) : null,
      ].filter(Boolean)),
    ]);
    if (o.onRound) {
      const link = el('button', { class: 'dn-linkbtn dn-roundtl-epdrill', type: 'button', text: 'open round →' });
      link.addEventListener('click', () => o.onRound(r.round_index));
      head.appendChild(link);
    }
    card.appendChild(head);

    // the fan of MINTED challengers (chips) — each opens its candidate.
    const fan = el('div', { class: 'dn-roundtl-fan' });
    if (r.challengers.length) {
      for (const c of r.challengers) {
        // an in-flight round's chip carries its PROPOSING-STEP status (a
        // proposing slot is dimmed/pending, a rejected slot dimmed, an applied
        // slot reads normal) so the field reads as it forms (issue #16).
        const st = c.status || null;
        const statusCls = st === 'proposing' ? ' dn-roundtl-chip-proposing'
          : st === 'rejected' ? ' dn-roundtl-chip-rejected' : '';
        const chip = el('button', {
          class: 'dn-roundtl-chip' + (c.promoted ? ' dn-roundtl-chip-win' : '') + statusCls,
          type: 'button',
          'aria-label': `Challenger ${c.id}` + (isNum(c.scalar) ? `, loss ${fmt(c.scalar, 1)}` : '')
            + (c.promoted ? ' — promoted' : st === 'proposing' ? ' — proposing' : st === 'rejected' ? ' — rejected' : ''),
        }, [
          el('span', { class: 'dn-mono', text: shortLabel(String(c.id), 12) }),
          c.promoted ? el('span', { class: 'dn-roundtl-chipcrown', 'aria-hidden': 'true', text: CROWN.current }) : null,
          st === 'proposing' ? el('span', { class: 'dn-faint dn-roundtl-chipstatus', 'aria-hidden': 'true', text: '⋯' }) : null,
          st === 'rejected' ? el('span', { class: 'dn-faint dn-roundtl-chipstatus', 'aria-hidden': 'true', text: '✗' }) : null,
          isNum(c.scalar) ? el('span', { class: 'dn-faint dn-roundtl-chiploss', text: fmt(c.scalar, 1) }) : null,
        ].filter(Boolean));
        if (o.onCompetitor) chip.addEventListener('click', () => o.onCompetitor(String(c.id)));
        fan.appendChild(chip);
      }
    } else {
      fan.appendChild(el('span', { class: 'dn-faint', text: r.inflight ? 'minting the field…' : 'no challengers minted this round' }));
    }
    card.appendChild(el('div', { class: 'dn-roundtl-fanrow' }, [
      el('span', { class: 'dn-faint dn-roundtl-fanlab', text: 'field' }),
      fan,
    ]));

    // the compact per-round structure figure (caller-built; null → omitted).
    const fig = o.figureFor ? o.figureFor(r) : null;
    if (fig) card.appendChild(el('div', { class: 'dn-roundtl-fig dn-figpane' }, [fig]));

    // the GATE OUTCOME — promoted (merges onto the spine), held, or (in-flight)
    // PROPOSING: the field is still minting, so the gate has not decided yet.
    // The in-flight gate line carries the LIVE "N proposed · M applied" tally so
    // the banner counts increment as the new round's field forms (issue #16).
    if (r.inflight) {
      const proposed = r.challengers.length;
      const applied = r.challengers.filter((c) => c.status === 'applied').length;
      const rejected = r.challengers.filter((c) => c.status === 'rejected').length;
      const proposing = r.challengers.filter((c) => c.status === 'proposing').length;
      let tally = `${proposed} proposed · ${applied} applied`;
      if (rejected > 0) tally += ` · ${rejected} rejected`;
      if (proposing > 0) tally += ` · ${proposing} proposing…`;
      card.appendChild(el('div', { class: 'dn-roundtl-gate dn-roundtl-gate-live' }, [
        el('span', { class: 'dn-roundtl-gatemark', 'aria-hidden': 'true', text: '⋯' }),
        el('span', { text: 'proposing the field · ' + tally }),
      ]));
    } else {
      card.appendChild(el('div', { class: 'dn-roundtl-gate' + (promoted ? ' dn-roundtl-gate-win' : '') }, [
        el('span', { class: 'dn-roundtl-gatemark', 'aria-hidden': 'true', text: promoted ? CROWN.current : '=' }),
        el('span', {
          text: promoted
            ? `${r.gateOutcome.gen} promoted → next round's champion`
            : 'champion held — no promotion this round',
        }),
      ]));
    }
    episodes.appendChild(card);
  });
  wrap.appendChild(episodes);
  return wrap;
}

// ── the LOSS-FLOOR WATERFALL — the epoch's descent across rounds ─────
//
// The headline "is it improving + what drove each gain" figure: each ROUND is a
// step. A round that PROMOTED drops the running loss floor by its promotion Δ
// (a downward step, `good` by DIRECTION — a lower floor is the better outcome);
// a HELD round keeps the floor flat (no step). The running floor is annotated at
// each station; the champion SPINE baseline runs in `--v2-accent`. The winning
// mutation (the promoted gen) per step lives on HOVER.
//
//   opts: {
//     steps: [{ round_index, from, to, delta, promoted, gen }],
//        from/to = the loss floor BEFORE / AFTER the round; delta = to - from
//        (negative = improvement); promoted = a gate promotion this round;
//        gen = the promoted challenger (the winning mutation) | null.
//     onRound(round_index), onCompetitor(genId).
//   }
export function waterfall(opts) {
  const o = opts || {};
  const steps = (Array.isArray(o.steps) ? o.steps : []).filter((s) => s);
  const w = o.width || 720;
  const padL = 56;
  const padR = 18;
  const padTop = 26;
  const padBottom = 28;
  const colW = steps.length ? (w - padL - padR) / steps.length : (w - padL - padR);
  const barW = Math.max(8, Math.min(colW * 0.6, 54));
  const h = (o.height || 220);
  const svg = svgEl('svg', {
    class: 'dn-waterfall', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': 'Loss-floor descent across rounds',
  });
  if (!steps.length) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no rounds yet';
    svg.appendChild(t);
    return svg;
  }
  // the loss domain spans every from/to floor; lower loss sits LOWER on the y
  // axis (a descent reads as a downward staircase).
  const floors = [];
  for (const s of steps) { if (isNum(s.from)) floors.push(s.from); if (isNum(s.to)) floors.push(s.to); }
  let [lo, hi] = extent(floors);
  lo = Math.min(lo, hi);
  const pad = (hi - lo) * 0.12 || 1;
  const y = scale([lo - pad, hi + pad], [h - padBottom, padTop]);
  const colX = (i) => padL + i * colW + colW / 2;

  // the SPINE baseline (accent): the champion floor connecting station to
  // station — the structural highlight of the figure.
  let spineD = '';
  steps.forEach((s, i) => {
    const cx = colX(i);
    const yFrom = isNum(s.from) ? y(s.from) : null;
    const yTo = isNum(s.to) ? y(s.to) : null;
    if (yFrom != null) spineD += `${spineD ? 'L' : 'M'}${(cx - barW / 2).toFixed(1)},${yFrom.toFixed(1)} `;
    if (yTo != null) spineD += `L${(cx + barW / 2).toFixed(1)},${yTo.toFixed(1)} `;
  });
  if (spineD) svg.appendChild(svgEl('path', { d: spineD.trim(), class: 'dn-waterfall-spine', fill: 'none' }));

  svg.appendChild(svgEl('text', { x: padL - 2, y: padTop - 12, class: 'dn-waterfall-axis' }, ['loss floor ↓ improving · rounds →']));

  steps.forEach((s, i) => {
    const cx = colX(i);
    const yFrom = isNum(s.from) ? y(s.from) : null;
    const yTo = isNum(s.to) ? y(s.to) : null;
    const improved = isNum(s.delta) && s.delta < 0;
    const regressed = isNum(s.delta) && s.delta > 0;
    const held = !s.promoted || !isNum(s.delta) || s.delta === 0;
    const g = svgEl('g', {
      class: 'dn-waterfall-step', tabindex: o.onRound ? '0' : null,
      'aria-label': `Round ${s.round_index}: ` + (held ? 'champion held' : `${s.gen} promoted, Δ ${fmtSigned(s.delta, 1)}`),
    });
    // the step bar: from the incoming floor DOWN to the new floor (a promotion);
    // a held round is a flat tick at the floor.
    if (!held && yFrom != null && yTo != null) {
      const yA = Math.min(yFrom, yTo);
      const yB = Math.max(yFrom, yTo);
      const cls = 'dn-waterfall-bar ' + (improved ? 'dn-good' : regressed ? 'dn-bad' : 'dn-flat');
      g.appendChild(hov(svgEl('rect', { x: cx - barW / 2, y: yA, width: barW, height: Math.max(2, yB - yA), rx: 2, class: cls }),
        `round ${s.round_index} · ${s.gen ? s.gen + ' promoted' : 'promoted'} · floor ${fmt(s.from, 1)} → ${fmt(s.to, 1)} · Δ ${fmtSigned(s.delta, 1)}`));
      // the connector from the prior floor into this step's top.
    } else if (yTo != null) {
      g.appendChild(hov(svgEl('line', { x1: cx - barW / 2, x2: cx + barW / 2, y1: yTo, y2: yTo, class: 'dn-waterfall-held' }),
        `round ${s.round_index} · champion held · floor ${fmt(s.to, 1)}`));
    }
    // the running-floor annotation under the station.
    const flLbl = svgEl('text', { x: cx, y: (yTo != null ? yTo : h - padBottom) - 6, class: 'dn-waterfall-floor', 'text-anchor': 'middle' });
    flLbl.textContent = isNum(s.to) ? fmt(s.to, 1) : '·';
    g.appendChild(flLbl);
    // the round ordinal on the x axis.
    const rord = svgEl('text', { x: cx, y: h - padBottom + 14, class: 'dn-waterfall-rord', 'text-anchor': 'middle' });
    rord.textContent = 'r' + s.round_index;
    g.appendChild(rord);
    // the winning-mutation glyph (the promoted gen) — a crown over a promoting step.
    if (!held && s.gen != null && yTo != null) {
      const cr = svgEl('text', { x: cx, y: y(s.to) - (isNum(s.from) && isNum(s.to) && s.to < s.from ? 8 : -14), class: 'dn-waterfall-crown', 'text-anchor': 'middle' });
      cr.textContent = CROWN.current;
      g.appendChild(cr);
    }
    clickable(g, o.onRound && (() => o.onRound(s.round_index)));
    svg.appendChild(g);
  });
  return svg;
}

// ── the CHAMPION REIGN GANTT — tenure across rounds ─────────────────
//
// One BAR per champion spanning the rounds it HELD the title. The CURRENT
// champion's bar is `--v2-accent` + ♛; every FORMER champion's bar is ink / dim
// + ♔. The reign of one generation reads as a highlighted SEGMENT of the spine —
// the candidate page passes a single generation's reign as the "reign ribbon".
//
//   opts: {
//     reigns: [{ id, fromRound, toRound, current }]  — fromRound..toRound inclusive
//     rounds: total round count (the x extent), or inferred from reigns.
//     onCompetitor(id).
//   }
export function reignGantt(opts) {
  const o = opts || {};
  const reigns = (Array.isArray(o.reigns) ? o.reigns : []).filter((r) => r && r.id != null);
  const w = o.width || 640;
  const rowH = o.rowHeight || 22;
  const padL = o.labelWidth || 120;
  const padR = 18;
  const top = 22;
  const h = top + Math.max(1, reigns.length) * rowH + 10;
  const svg = svgEl('svg', {
    class: 'dn-reigngantt', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
    'aria-label': 'Champion reign across rounds',
  });
  if (!reigns.length) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no reign yet';
    svg.appendChild(t);
    return svg;
  }
  const maxRound = isNum(o.rounds) ? o.rounds
    : Math.max(1, ...reigns.map((r) => (isNum(r.toRound) ? r.toRound : 0)));
  const x = scale([0, Math.max(1, maxRound)], [padL + 4, w - padR]);
  // round-axis ticks along the top.
  for (let ri = 0; ri <= maxRound; ri++) {
    const tx = x(ri);
    const tk = svgEl('text', { x: tx, y: top - 8, class: 'dn-reigngantt-axis', 'text-anchor': 'middle' });
    tk.textContent = 'r' + ri;
    svg.appendChild(tk);
    svg.appendChild(svgEl('line', { x1: tx, x2: tx, y1: top - 4, y2: h - 6, class: 'dn-reigngantt-grid' }));
  }
  reigns.forEach((r, i) => {
    const cy = top + i * rowH + rowH / 2;
    const x0 = x(isNum(r.fromRound) ? r.fromRound : 0);
    const x1 = x(isNum(r.toRound) ? r.toRound : maxRound);
    const current = !!r.current;
    const g = svgEl('g', { class: 'dn-reigngantt-row', tabindex: o.onCompetitor ? '0' : null });
    const lbl = svgEl('text', { x: padL - 8, y: cy + 3, class: 'dn-reigngantt-name' + (current ? ' dn-reigngantt-current' : ' dn-reigngantt-former'), 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(r.id), 12) + ' ' + (current ? CROWN.current : CROWN.former);
    g.appendChild(lbl);
    const span = Math.max(4, x1 - x0);
    g.appendChild(hov(svgEl('rect', {
      x: x0, y: cy - rowH * 0.32, width: span, height: rowH * 0.64, rx: 3,
      class: 'dn-reigngantt-bar' + (current ? ' dn-reigngantt-bar-current' : ' dn-reigngantt-bar-former'),
    }), `${r.id} ${current ? CROWN.current + ' current champion' : CROWN.former + ' former champion'} · held r${isNum(r.fromRound) ? r.fromRound : 0}`
      + (isNum(r.toRound) && r.toRound !== r.fromRound ? `–r${r.toRound}` : '')));
    clickable(g, o.onCompetitor && (() => o.onCompetitor(String(r.id))));
    svg.appendChild(g);
  });
  return svg;
}

// ── the COMPOSED META-LOOP LEDGER — the cross-epoch zoom-level (study opt 7) ──
//
// The highest zoom-level figure: above any single tournament, the unit is the
// EPOCH. It braids the three operator-liked cross-epoch views into ONE figure
// over a single, EFFORT-proportional x-axis (each epoch owns a band whose WIDTH
// ∝ its generation_count), with three stacked zones read top→bottom:
//
//   (A) FLOOR STAIRCASE (opt 1)  — the held loss floor as held steps + risers;
//       a step DROPS when the floor improves, JUMPS UP on a reset; every roll
//       seam carries a component-coded change CHIP on a vertical rail.
//   (B) EFFORT BANDS    (opt 4)  — band width ∝ generation_count, fill ∝ floor
//       (good→bad), with a champion-reign tick marking the generation that set
//       the floor.
//   (C) COMPONENT HEATSTRIP (opt 3) — epochs(cols) × components(rows incl. the
//       proposer* column the contract-diff omits + structure); a filled cell =
//       that lever changed vs the predecessor. A floor-Δ chip sits per epoch in
//       the right gutter.
//
// A structure roll is a SOFT seam (the cross-roll floor comparison is not
// directly comparable) — stripped down the staircase + bands and dashed on the
// structure cell + the change chip.
//
// The decision it answers: "is the meta-loop making net progress across
// contracts, which lever moved each reset, and is effort buying floor."
//
// CONVERGENCE: the figure is a pure function of the model — the live (current
// epoch in flight, open=true → dashed) and the settled (closed) render are
// byte-identical for the same row data. The home view digest-gates on
// `metaLoopLedgerDigest` so a no-op heartbeat churns no DOM.
//
// DEGRADES on 0–1 epochs: an empty model paints an honest placeholder; a single
// epoch paints its band + floor + heatstrip column with no risers/seams (there
// is nothing to diff against — the change map is all-unchanged).
//
//   opts: {
//     epochs: [{ epoch_id, floor, champion_gen, champion_index, generation_count,
//                structure, open|closed, changed_components:{name:bool},
//                changed_list:[..], soft }],
//     currentEpochId, onEpoch(epoch_id), responsive
//   }
const LEDGER_COMPONENTS = ['board', 'brief', 'scoring', 'entrypoint', 'mutable_trees', 'structure', 'proposer'];
const LEDGER_COMP_LABEL = {
  board: 'board', brief: 'brief', scoring: 'scoring', entrypoint: 'entrypoint',
  mutable_trees: 'mutable_trees', structure: 'structure', proposer: 'proposer*',
};
// component → accent for the change-chip primary colour (mirrors the study's rcol).
const LEDGER_COMP_COLOR = {
  board: 'var(--v2-accent)', scoring: 'var(--v2-good)', brief: 'var(--v2-caution)',
  structure: 'var(--v2-bad)', entrypoint: 'var(--v2-accent)',
  mutable_trees: 'var(--v2-ink-soft)', proposer: 'var(--v2-ink-faint)',
};

export function metaLoopLedger(opts) {
  const o = opts || {};
  const rows = (Array.isArray(o.epochs) ? o.epochs : []).filter((e) => e && e.epoch_id != null);
  const W = o.width || 1120;
  const L = 96;
  const R = 28;
  const T = 78;
  const pw = W - L - R;
  const n = rows.length;

  // ---- empty / degenerate model: an honest placeholder, aspect-locked. ----
  if (n === 0) {
    const h = 120;
    const svg = svgEl('svg', applyResponsive({
      class: 'dn-metaledger', width: '100%', height: h,
      viewBox: `0 0 ${W} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
      'aria-label': 'cross-epoch meta-loop ledger',
    }, o, W, h, 'dn-metaledger-hero'));
    const t = svgEl('text', { x: W / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no epochs recorded yet';
    svg.appendChild(t);
    return svg;
  }

  // ---- effort-proportional x geometry: each epoch owns a band whose width ∝
  // generation_count. A floor of 1 gen-unit per epoch keeps a zero-gen epoch
  // from collapsing to a hairline (still a readable band). ----
  const efforts = rows.map((e) => Math.max(1, isNum(e.generation_count) ? e.generation_count : 1));
  const totalEffort = efforts.reduce((a, v) => a + v, 0) || 1;
  const bx = [];
  let acc = L;
  rows.forEach((e, i) => {
    const bw = pw * efforts[i] / totalEffort;
    bx.push({ x0: acc, x1: acc + bw, xc: acc + bw / 2, w: bw });
    acc += bw;
  });

  // ---- zone layout (top→bottom) ----
  const stairH = 190;
  const stairTop = T;
  const bandTop = stairTop + stairH + 34;
  const bandH = 62;
  const bandBot = bandTop + bandH;
  const hsTop = bandBot + 40;
  const hsRowH = 26;
  const hsBot = hsTop + LEDGER_COMPONENTS.length * hsRowH;
  const H = hsBot + 24;

  // ---- the floor domain (the staircase y-scale). Lower loss sits LOWER on the
  // axis (a descent reads downward); a flat/absent series gets a gentle pad. ----
  const floors = finiteValues(rows.map((e) => e.floor));
  let [flo, fhi] = extent(floors.length ? floors : [0, 1]);
  const fpad = (fhi - flo) * 0.14 || 0.5;
  flo -= fpad; fhi += fpad;
  const sy = scale([flo, fhi], [stairTop + stairH, stairTop]);
  // hue for a floor: good (low) → bad (high), area-honest mix.
  const floorMix = (f) => {
    if (!isNum(f)) return 'color-mix(in srgb, var(--v2-ink-faint) 30%, var(--v2-panel))';
    const t = Math.max(0, Math.min(1, (f - flo) / (fhi - flo || 1)));
    return `color-mix(in srgb, var(--v2-bad) ${Math.round(t * 100)}%, var(--v2-good))`;
  };

  const svg = svgEl('svg', applyResponsive({
    class: 'dn-metaledger', width: '100%', height: H,
    viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
    'aria-label': 'cross-epoch composed meta-loop ledger — floor staircase, effort bands, contract-component heatstrip',
  }, o, W, H, 'dn-metaledger-hero'));

  const onEpoch = typeof o.onEpoch === 'function' ? o.onEpoch : null;
  const txt = (x, y, s, attrs) => {
    const t = svgEl('text', Object.assign({ x, y }, attrs || {}));
    t.textContent = s == null ? '' : String(s);
    return t;
  };

  // ───────── (A) FLOOR STAIRCASE (opt 1) ─────────
  svg.appendChild(txt(L, T - 58, 'A · LOSS FLOOR — held steps across epochs (↓ better)',
    { class: 'dn-metaledger-zonecap', 'text-anchor': 'start' }));
  // y gridlines + ticks
  for (let i = 0; i <= 4; i++) {
    const v = flo + (fhi - flo) * i / 4;
    const yy = sy(v);
    svg.appendChild(svgEl('line', { x1: L, y1: yy, x2: W - R, y2: yy, class: 'dn-metaledger-grid' }));
    svg.appendChild(txt(L - 8, yy + 3, fmt(v, 2), { class: 'dn-metaledger-axis', 'text-anchor': 'end' }));
  }
  let prevF = null;
  rows.forEach((e, i) => {
    const b = bx[i];
    const open = !!e.open || e.closed === false;
    const hasF = isNum(e.floor);
    const yy = hasF ? sy(e.floor) : sy((flo + fhi) / 2);
    const improved = prevF == null ? true : (hasF && e.floor < prevF);
    const stepCls = open ? 'dn-metaledger-step-open'
      : improved ? 'dn-metaledger-step-good' : 'dn-metaledger-step-bad';
    // riser from the previous held level to this one
    if (prevF != null && hasF) {
      svg.appendChild(svgEl('line', {
        x1: b.x0, y1: sy(prevF), x2: b.x0, y2: yy,
        class: 'dn-metaledger-riser ' + stepCls, 'stroke-dasharray': open ? '5 4' : null,
      }));
    }
    // held horizontal level
    svg.appendChild(svgEl('line', {
      x1: b.x0, y1: yy, x2: b.x1, y2: yy,
      class: 'dn-metaledger-held ' + stepCls, 'stroke-dasharray': open ? '5 4' : null,
    }));
    svg.appendChild(svgEl('circle', { cx: b.xc, cy: yy, r: 4.5, class: 'dn-metaledger-floordot ' + stepCls }));
    svg.appendChild(txt(b.xc, yy - 11, hasF ? fmt(e.floor, 3) : '—',
      { class: 'dn-metaledger-floorlbl ' + stepCls, 'text-anchor': 'middle' }));
    if (hasF) prevF = e.floor;
  });
  // open badge under the last held level
  const last = rows[n - 1];
  if (last && (last.open || last.closed === false)) {
    const yy = isNum(last.floor) ? sy(last.floor) : sy((flo + fhi) / 2);
    svg.appendChild(txt(bx[n - 1].xc, yy + 20, '● OPEN',
      { class: 'dn-metaledger-openbadge', 'text-anchor': 'middle' }));
  }

  // ───────── contract-change RAIL + chips (opt 1), at each roll boundary ─────────
  // RAIL + SOFT seam stay at the TRUE boundary b.x0; only the label box moves.
  // Chips carry a COMPACT label (headline + "+N"), the FULL set on hover, and are
  // de-collided by their VARIABLE widths so adjacent rolls never overlap/clip.
  const chips = [];
  rows.forEach((e, i) => {
    if (i === 0) return;
    const changed = Array.isArray(e.changed_list) ? e.changed_list : [];
    if (!changed.length) return;
    const b = bx[i];
    const soft = !!e.soft;
    // rail + soft seam at the TRUE boundary (do not move with the label).
    svg.appendChild(svgEl('line', {
      x1: b.x0, y1: stairTop - 2, x2: b.x0, y2: bandBot + 4,
      class: 'dn-metaledger-rail', 'stroke-dasharray': soft ? '3 3' : null,
    }));
    if (soft) {
      svg.appendChild(svgEl('line', {
        x1: b.x0, y1: stairTop, x2: b.x0, y2: bandBot,
        class: 'dn-metaledger-soft',
      }));
      svg.appendChild(txt(b.x0 + 4, bandBot - 6, 'SOFT', { class: 'dn-metaledger-softlbl' }));
    }
    const primary = changed[0];
    const chipCol = LEDGER_COMP_COLOR[primary] || 'var(--v2-ink-faint)';
    // FULL set (hover) vs COMPACT (rendered: headline + "+N" overflow). Headline
    // is →<structure> when structure rolled, else the primary lever's label.
    const full = changed.map((c) => (c === 'structure'
      ? 'structure→' + (e.structure || '?') : (LEDGER_COMP_LABEL[c] || c))).join('+');
    const structRolled = changed.indexOf('structure') >= 0;
    const head = structRolled ? '→' + (e.structure || '?') : (LEDGER_COMP_LABEL[primary] || primary);
    const extra = changed.length - 1;
    const compact = extra > 0 ? head + ' +' + extra : head;
    const cw = Math.max(54, compact.length * 6.0 + 14);
    chips.push({ desiredCx: b.x0, cw, compact, full, chipCol, soft, x0: b.x0 });
  });

  // ---- variable-width de-collide (the shared `decollide` assumes a uniform
  // gap): sort by desired x, push left→right so no two boxes overlap (gap uses
  // each box's half-width); if the run overruns the right edge, clamp the last
  // and back-propagate the min gap. ----
  const GAP = 6;
  chips.sort((p, q) => p.desiredCx - q.desiredCx);
  for (let k = 0; k < chips.length; k++) {
    chips[k].cx = Math.max(L + chips[k].cw / 2, chips[k].desiredCx);
    if (k > 0) {
      const minCx = chips[k - 1].cx + chips[k - 1].cw / 2 + chips[k].cw / 2 + GAP;
      if (chips[k].cx < minCx) chips[k].cx = minCx;
    }
  }
  const lastK = chips.length - 1;
  if (lastK >= 0 && chips[lastK].cx + chips[lastK].cw / 2 > W - R) {
    chips[lastK].cx = W - R - chips[lastK].cw / 2;
    for (let k = lastK - 1; k >= 0; k--) {
      const maxCx = chips[k + 1].cx - chips[k + 1].cw / 2 - chips[k].cw / 2 - GAP;
      if (chips[k].cx > maxCx) chips[k].cx = maxCx;
    }
  }
  chips.forEach((c) => {
    // subtle connector chip → true boundary when the label is pushed well off it.
    if (Math.abs(c.cx - c.x0) > c.cw / 2 + 4) {
      svg.appendChild(svgEl('line', {
        x1: c.x0, y1: T - 26, x2: c.cx, y2: T - 26, class: 'dn-metaledger-chiplink',
      }));
    }
    svg.appendChild(hov(svgEl('rect', {
      x: c.cx - c.cw / 2, y: T - 44, width: c.cw, height: 18, rx: 4,
      class: 'dn-metaledger-chip', fill: 'var(--v2-panel)',
      stroke: c.chipCol, 'stroke-width': 1.5, 'stroke-dasharray': c.soft ? '3 3' : null,
    }), c.full));
    svg.appendChild(txt(c.cx, T - 31, c.compact, { class: 'dn-metaledger-chiptxt', 'text-anchor': 'middle' }));
  });

  // ───────── (B) EFFORT-PROPORTIONAL BANDS (opt 4) ─────────
  svg.appendChild(txt(L, bandTop - 8, 'B · EFFORT — band width ∝ generation_count · fill ∝ floor · │ = champion reign · position = when the floor was set',
    { class: 'dn-metaledger-zonecap', 'text-anchor': 'start' }));
  rows.forEach((e, i) => {
    const b = bx[i];
    const open = !!e.open || e.closed === false;
    const g = svgEl('g', { class: 'dn-metaledger-band-g', tabindex: onEpoch ? '0' : null });
    g.appendChild(hov(svgEl('rect', {
      x: b.x0 + 1, y: bandTop, width: Math.max(2, b.w - 3), height: bandH, rx: 4,
      class: 'dn-metaledger-band', fill: floorMix(e.floor),
      stroke: 'var(--v2-rule)', 'stroke-width': 1, 'stroke-dasharray': open ? '5 4' : null,
    }), `${e.epoch_id} · ${e.generation_count || 0} gen · floor ${fmt(e.floor, 3)}`
      + ` · ${e.structure || 'gauntlet'}` + (open ? ' · OPEN' : '')));
    g.appendChild(txt(b.x0 + 8, bandTop + 18, shortLabel(String(e.epoch_id), 14),
      { class: 'dn-metaledger-bandid', 'text-anchor': 'start' }));
    if (b.w > 84) {
      g.appendChild(txt(b.x0 + 8, bandTop + 34, e.structure || 'gauntlet',
        { class: 'dn-metaledger-bandsub', 'text-anchor': 'start' }));
    }
    g.appendChild(txt(b.x0 + 8, bandTop + 50,
      `${e.generation_count || 0} gen`,
      { class: 'dn-metaledger-bandsub', 'text-anchor': 'start' }));
    if (open) {
      g.appendChild(txt(b.x0 + b.w - 9, bandTop + 16, 'OPEN',
        { class: 'dn-metaledger-bandopen', 'text-anchor': 'end' }));
    }
    // champion-reign tick — anchored to WHEN in the epoch the floor was
    // set: x ∝ champion_index / generation_count, so an early champion
    // sits near the band's left edge, a late one near its right. With no
    // locatable champion_index we draw ONLY the label (no misleading bar).
    if (e.champion_gen != null) {
      const ci = e.champion_index;
      const gc = Math.max(1, e.generation_count || 0);
      const hasIdx = isNum(ci) && ci >= 0;
      const pad = 5;
      const x0 = b.x0 + pad;
      const x1 = b.x0 + Math.max(2, b.w - 3) - pad;
      let labelX;
      if (hasIdx) {
        const champX = Math.min(Math.max(b.x0 + ((ci + 0.5) / gc) * Math.max(2, b.w - 3), x0), x1);
        labelX = champX;
        g.appendChild(svgEl('rect', {
          x: champX - 2, y: bandTop - 4, width: 4, height: bandH + 8, rx: 1.5,
          class: 'dn-metaledger-champtick',
        }));
      } else {
        labelX = b.x0 + Math.max(6, (b.w - 3) * 0.5);
      }
      if (b.w > 104) {
        g.appendChild(txt(labelX, bandTop + bandH + 13, 'champ ' + shortLabel(String(e.champion_gen), 8),
          { class: 'dn-metaledger-champlbl', 'text-anchor': 'middle' }));
      }
    }
    clickable(g, onEpoch && (() => onEpoch(String(e.epoch_id))));
    svg.appendChild(g);
  });

  // ───────── (C) COMPONENT HEATSTRIP (opt 3) — epochs(cols) × components(rows) ─────────
  svg.appendChild(txt(L, hsTop - 12, 'C · CONTRACT DELTA — filled = lever changed vs predecessor (proposer* not in contract-diff)',
    { class: 'dn-metaledger-zonecap', 'text-anchor': 'start' }));
  LEDGER_COMPONENTS.forEach((c, r) => {
    const yc = hsTop + r * hsRowH + hsRowH / 2;
    const isProp = c === 'proposer';
    svg.appendChild(txt(L - 8, yc + 3, LEDGER_COMP_LABEL[c],
      { class: 'dn-metaledger-rowlbl' + (isProp ? ' dn-metaledger-rowlbl-prop' : ''), 'text-anchor': 'end' }));
  });
  rows.forEach((e, i) => {
    const b = bx[i];
    const cmap = (e.changed_components && typeof e.changed_components === 'object') ? e.changed_components : {};
    LEDGER_COMPONENTS.forEach((c, r) => {
      const yTop = hsTop + r * hsRowH + 2;
      const changed = !!cmap[c];
      const soft = c === 'structure' && changed;
      const cls = 'dn-metaledger-cell'
        + (changed ? (soft ? ' dn-metaledger-cell-soft' : ' dn-metaledger-cell-on') : ' dn-metaledger-cell-off');
      svg.appendChild(hov(svgEl('rect', {
        x: b.x0 + 2, y: yTop, width: Math.max(2, b.w - 5), height: hsRowH - 4, rx: 3, class: cls,
        'stroke-dasharray': soft ? '4 3' : null,
      }), `${e.epoch_id} · ${LEDGER_COMP_LABEL[c]} ${changed ? 'CHANGED vs predecessor' : 'unchanged'}`
        + (soft ? ` (→ ${e.structure || '?'} · SOFT seam)` : '')));
      if (changed) {
        svg.appendChild(txt(b.xc, yTop + (hsRowH - 4) / 2 + 3, soft ? ('→' + (e.structure || '?')) : '●',
          { class: soft ? 'dn-metaledger-cellmark-soft' : 'dn-metaledger-cellmark', 'text-anchor': 'middle' }));
      }
    });
    svg.appendChild(txt(b.xc, hsBot + 14, shortLabel(String(e.epoch_id), 14),
      { class: 'dn-metaledger-colid', 'text-anchor': 'middle' }));
  });

  return svg;
}

// The content DIGEST for the meta-loop ledger — the home view gates its DOM
// swap on this so a no-op SSE heartbeat (identical ledger) churns nothing. It
// quantizes the floor to 3 dp (the rendered precision) and folds every field
// the figure draws: floor, champion, champion_index (the tick POSITION → a
// position change regates the DOM), effort, structure, lifecycle, and the
// per-component change set (incl. proposer + structure). Two ledgers that
// render byte-identically MUST produce the same digest — the live (open) and
// settled (closed) paths included.
export function metaLoopLedgerDigest(opts) {
  const o = opts || {};
  const rows = (Array.isArray(o.epochs) ? o.epochs : []).filter((e) => e && e.epoch_id != null);
  return JSON.stringify({
    cur: o.currentEpochId != null ? String(o.currentEpochId) : null,
    e: rows.map((e) => [
      String(e.epoch_id),
      isNum(e.floor) ? e.floor.toFixed(3) : null,
      e.champion_gen != null ? String(e.champion_gen) : null,
      isNum(e.champion_index) ? e.champion_index : null,
      isNum(e.generation_count) ? e.generation_count : 0,
      e.structure || 'gauntlet',
      (e.open || e.closed === false) ? 'o' : 'c',
      LEDGER_COMPONENTS.map((c) => (e.changed_components && e.changed_components[c]) ? 1 : 0).join(''),
      e.soft ? 1 : 0,
    ]),
  });
}

// ── the CALIBRATION TREND (DIAGNOSTIC) ───────────────────────────────────────
//
// The proposer's prediction-accuracy fraction over one epoch's lineage — does
// its calibration drift as the epoch matures? Each generation is a point in the
// 0..1 score-fraction band (higher = better-calibrated); a generation that made
// NO falsifiable claim (score_fraction == null) is a hollow tick on the rolling-
// mean baseline (it scored nothing, so it neither lifts nor drops the line). The
// rolling mean is a dashed reference; the end dot earns good/bad by the trend
// sign. This reuses the sparkline/staircase grammar (band + baseline + end dot)
// and is DIAGNOSTIC — it NEVER feeds the gate (the caller captions it so).
//
// opts: { points:[{generation_id, score_fraction|null, total_claims, decision}],
//         rolling_mean|null, trend_sign:-1|0|1, width, height, responsive,
//         onGen(generation_id) }
//
// DEGRADES: 0 points → an honest placeholder; a single point → a centred dot.
export function calibrationTrend(opts) {
  const o = opts || {};
  const pts = (Array.isArray(o.points) ? o.points : []).filter((p) => p && p.generation_id != null);
  const W = o.width || 360;
  const H = o.height || 64;
  const padX = 8;
  const padY = 8;
  const svg = svgEl('svg', applyResponsive({
    class: 'dn-caltrend', width: '100%', height: H,
    viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': 'proposer calibration trend — prediction-accuracy fraction over generations (diagnostic)',
  }, o, W, H, 'dn-caltrend-hero'));

  if (pts.length === 0) {
    const t = svgEl('text', { x: W / 2, y: H / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no scored predictions yet';
    svg.appendChild(t);
    return svg;
  }

  // the fraction band is a FIXED 0..1 (a calibration fraction, not a free scale)
  // so the line reads on an absolute "did the proposer call it" axis.
  const x = scale([0, Math.max(1, pts.length - 1)], [padX, W - padX]);
  const y = scale([0, 1], [H - padY, padY]);

  // the 0..1 band backdrop + the 0.5 midline (the "half its calls landed" mark).
  svg.appendChild(svgEl('rect', { x: padX, y: padY, width: W - 2 * padX, height: H - 2 * padY, class: 'dn-spark-band' }));
  const midY = y(0.5);
  svg.appendChild(svgEl('line', { x1: padX, x2: W - padX, y1: midY, y2: midY, class: 'dn-caltrend-mid' }));

  // the rolling-mean dashed reference (the epoch's mean calibration).
  if (isNum(o.rolling_mean)) {
    const ry = y(Math.max(0, Math.min(1, o.rolling_mean)));
    svg.appendChild(svgEl('line', { x1: padX, x2: W - padX, y1: ry, y2: ry, class: 'dn-caltrend-mean' }));
  }

  // the connecting line over the SCORED points only (a null-fraction gen lifts
  // the pen — it scored nothing, so we don't draw a misleading drop to zero).
  let d = '';
  let penDown = false;
  pts.forEach((p, i) => {
    const f = p && isNum(p.score_fraction) ? p.score_fraction : null;
    if (f == null) { penDown = false; return; }
    d += `${penDown ? 'L' : 'M'}${x(i).toFixed(2)},${y(f).toFixed(2)} `;
    penDown = true;
  });
  if (d) svg.appendChild(svgEl('path', { d: d.trim(), class: 'dn-spark-line', fill: 'none' }));

  // the per-generation ticks: a scored gen is a solid dot, a no-claim gen is a
  // hollow tick on the midline. The LAST scored dot earns good/bad by trend sign.
  let lastScoredI = -1;
  for (let i = pts.length - 1; i >= 0; i--) { if (isNum(pts[i].score_fraction)) { lastScoredI = i; break; } }
  const onGen = typeof o.onGen === 'function' ? o.onGen : null;
  pts.forEach((p, i) => {
    const f = isNum(p.score_fraction) ? p.score_fraction : null;
    const cx = x(i);
    if (f == null) {
      // a no-claim generation: a hollow tick on the midline (it scored nothing).
      const node = hov(svgEl('circle', { cx, cy: midY, r: 1.8, class: 'dn-caltrend-noclaim',
        style: onGen ? 'cursor:pointer;' : null }),
        `${p.generation_id} · no falsifiable claim · ${p.decision || '—'}`);
      if (onGen) node.addEventListener('click', () => onGen(String(p.generation_id)));
      svg.appendChild(node);
      return;
    }
    let cls = 'dn-caltrend-dot';
    if (i === lastScoredI) {
      const sign = isNum(o.trend_sign) ? o.trend_sign : 0;
      cls += sign > 0 ? ' dn-good' : sign < 0 ? ' dn-bad' : '';
    }
    const r = (pts.length === 1 || i === lastScoredI) ? 2.6 : 2.0;
    const node = hov(svgEl('circle', { cx, cy: y(f), r, class: cls,
      style: onGen ? 'cursor:pointer;' : null }),
      `${p.generation_id} · ${Math.round(f * 100)}% of ${p.total_claims} claim${p.total_claims === 1 ? '' : 's'} landed · ${p.decision || '—'}`);
    if (onGen) node.addEventListener('click', () => onGen(String(p.generation_id)));
    svg.appendChild(node);
  });

  return svg;
}

// The content DIGEST for the calibration trend — the home view gates its DOM
// swap on this so a no-op SSE heartbeat (identical trend) churns nothing. Folds
// the rounded rolling mean + trend sign + each point's (generation_id, ROUNDED
// score_fraction, total_claims, decision). NO timestamps. Two trends that render
// byte-identically MUST produce the same digest; a new scored generation (a
// fraction moving past 2dp, a claim landing) flips it → repaint.
export function calibrationTrendDigest(opts) {
  const o = opts || {};
  const pts = (Array.isArray(o.points) ? o.points : []).filter((p) => p && p.generation_id != null);
  return JSON.stringify({
    rm: isNum(o.rolling_mean) ? o.rolling_mean.toFixed(3) : null,
    ts: isNum(o.trend_sign) ? o.trend_sign : 0,
    p: pts.map((p) => [
      String(p.generation_id),
      isNum(p.score_fraction) ? p.score_fraction.toFixed(3) : null,
      isNum(p.total_claims) ? p.total_claims : 0,
      p.decision == null ? null : String(p.decision),
    ]),
  });
}

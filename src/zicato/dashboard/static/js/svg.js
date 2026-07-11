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

// A centered "no data yet" placeholder label appended to `parent` (an <svg>)
// and returned — the ~15 identical empty-state blocks every figure shared (U5).
function emptyState(parent, width, height, label) {
  const t = svgEl('text', { x: width / 2, y: height / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
  t.textContent = label;
  parent.appendChild(t);
  return parent;
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

// ── digestOpts — the SINGLE generic figure-opts digest (U5) ───────────
//
// The one content digest that replaced the per-figure hand-rolled `*Digest`
// folds. Its rules are each LOAD-BEARING for the digest-gated no-op guarantee
// (a no-op heartbeat must produce a byte-identical digest so the gate does
// ZERO DOM writes):
//   * FUNCTIONS ARE DROPPED — figure opts carry per-render callbacks
//     (onCompetitor / onClick / onRound, a heatmap `value` accessor). A fresh
//     closure every render would flip the digest on every beat; dropping them
//     is the rule that keeps the gate quiet.
//   * KEY-SORTED so object key order never perturbs the string.
//   * a non-integer finite number rounds to 3dp — sub-precision jitter (a
//     re-derived scalar wobbling in the 4th place) must NOT flip the digest.
//   * NaN / undefined → null (a stable, JSON-safe sentinel; ±Infinity too).
//   * `omit` names TOP-LEVEL opts keys to exclude (mode flags / volatile
//     fields a given figure's fold deliberately ignored) so each wrapper keeps
//     its own fold semantics.
export function digestOpts(opts, omit = []) {
  const drop = new Set(Array.isArray(omit) ? omit : []);
  const norm = (v) => {
    if (typeof v === 'function') return undefined; // DROPPED (load-bearing)
    if (typeof v === 'number') return isFinite(v) ? (Number.isInteger(v) ? v : Number(v.toFixed(3))) : null;
    if (v === undefined || v === null) return null;
    if (Array.isArray(v)) return v.map((x) => { const n = norm(x); return n === undefined ? null : n; });
    if (typeof v === 'object') {
      const out = {};
      for (const k of Object.keys(v).sort()) {
        const n = norm(v[k]);
        if (n !== undefined) out[k] = n; // a dropped function simply vanishes
      }
      return out;
    }
    return v; // string / boolean
  };
  const top = (opts && typeof opts === 'object') ? opts : {};
  const root = {};
  for (const k of Object.keys(top).sort()) {
    if (drop.has(k)) continue;
    const n = norm(top[k]);
    if (n !== undefined) root[k] = n;
  }
  return JSON.stringify(root);
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

// MIDDLE-truncate to the same char budget as shortLabel, but keep the
// distinguishing TAIL as well as the head — ids that share a long common prefix
// (epoch-2026-06-13-a vs …-b) would otherwise head-truncate to an IDENTICAL
// visible stub. Byte-identical to shortLabel for any string at/under the cap
// (the normal short-id case), so it only ever diverges where the prefix would
// have collided. The '…' eats one slot; the rest is split head-heavy (the head
// carries the family, the tail the discriminator).
function midLabel(s, n) {
  const max = isNum(n) ? n : 12;
  const str = s == null ? '' : String(s);
  if (str.length <= max) return str;
  if (max <= 1) return '…';
  const budget = max - 1;
  const head = Math.ceil(budget / 2);
  const tail = budget - head;
  return str.slice(0, head) + '…' + (tail > 0 ? str.slice(str.length - tail) : '');
}

// ── shared text-fitting primitives (the ONE home for "size text to its box") ──
//
// The recurring dashboard clip/collision family — left-/start-anchored text whose
// extent is a GUESSED `shortLabel` char-cap that exceeds its column/gutter and is
// then clipped by `preserveAspectRatio` — came from every figure re-implementing
// the same fit math by hand (one bug, ~30 times). These centralise it so a figure
// cannot re-introduce the clip:
//   * CHAR_EM / textPx — the ONE mono char-width model (≈0.6 em/char, the CSS mono
//     face) every figure MEASURES from, instead of re-guessing a char cap.
//   * fitLabel — truncate (head, or `mid` to keep the discriminating tail) to a
//     PIXEL budget rather than a raw char count.
//   * edgeText — build a <text> whose full rendered extent is kept inside its box
//     by clamping x + flipping the anchor inward near an edge (no truncation).
//   * fitInto — fitLabel THEN edgeText: the common "fit this column AND never clip
//     the viewBox" case in one call.
// Exported so the figure builders + the node harness share one implementation.
export const CHAR_EM = 0.6;            // mono advance width ≈ 0.6 em/char
const DEFAULT_FONT_PX = 11;

// Estimated rendered width (px) of `s` at `fontPx` in the figures' mono face.
export function textPx(s, fontPx) {
  const str = s == null ? '' : String(s);
  const fpx = isNum(fontPx) ? fontPx : DEFAULT_FONT_PX;
  return str.length * fpx * CHAR_EM;
}

// Truncate `s` so its rendered width ≤ maxPx at fontPx, keeping an ellipsis.
// `opts.mid` middle-truncates (keeps the discriminating tail — the [[midLabel]]
// rule). Returns '' when not even one char + ellipsis fits, so a caller can drop
// the label entirely on a too-narrow band.
export function fitLabel(s, maxPx, fontPx, opts) {
  const str = s == null ? '' : String(s);
  const fpx = isNum(fontPx) ? fontPx : DEFAULT_FONT_PX;
  const per = fpx * CHAR_EM;
  if (!isNum(maxPx) || maxPx <= 0 || per <= 0) return '';
  const budget = Math.floor(maxPx / per);
  if (str.length <= budget) return str;
  if (budget < 1) return '';
  return (opts && opts.mid) ? midLabel(str, budget) : shortLabel(str, budget);
}

// Build a <text> placed near (x,y) with `anchor`, keeping its FULL rendered extent
// inside [pad, viewW-pad]: clamp x, and FLIP the anchor inward when the natural
// side would overrun the edge. Does NOT truncate — call fitLabel first (or use
// fitInto) when the text must also fit a column. `attrs` is merged onto the el;
// `cls` sets the class.
export function edgeText(o) {
  o = o || {};
  const fpx = isNum(o.fontPx) ? o.fontPx : DEFAULT_FONT_PX;
  const W = isNum(o.viewW) ? o.viewW : 0;
  const pad = isNum(o.pad) ? o.pad : 4;
  const lo = pad;
  const hi = Math.max(pad, W - pad);
  const text = o.text == null ? '' : String(o.text);
  const w = textPx(text, fpx);
  let x = isNum(o.x) ? o.x : 0;
  let anchor = o.anchor || 'start';
  if (anchor === 'middle') {
    const half = w / 2;
    x = (lo + half > hi - half) ? (lo + hi) / 2 : Math.min(Math.max(x, lo + half), hi - half);
  } else if (anchor === 'end') {
    if (x - w < lo) { anchor = 'start'; x = Math.max(Math.min(x, hi - w), lo); }
    else { x = Math.min(x, hi); }
  } else {                                   // 'start'
    if (x + w > hi) { anchor = 'end'; x = Math.min(Math.max(x, lo + w), hi); }
    else { x = Math.max(x, lo); }
  }
  const attrs = Object.assign({}, o.attrs || {}, { x, y: o.y, 'text-anchor': anchor });
  if (o.cls) attrs.class = o.cls;
  const t = svgEl('text', attrs);
  t.textContent = text;
  return t;
}

// The common compound case: truncate to a column width (`maxPx`) AND keep the
// (possibly-truncated) text inside the viewBox. One call replaces the bespoke
// "shortLabel(id,N) then hand-clamp x" pattern.
export function fitInto(o) {
  o = o || {};
  const fitted = fitLabel(o.text, o.maxPx, o.fontPx, { mid: !!o.mid });
  return edgeText(Object.assign({}, o, { text: fitted }));
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
  // OPT-IN measured-noise band: `noiseBand: {center, half}` shades the
  // horizontal [center−half, center+half] band (the epoch's measured A/A noise
  // floor around the champion floor) so scalar movement INSIDE the band reads
  // honestly as indistinguishable from a re-roll of the same generation. The
  // y-domain widens to keep the whole band in frame.
  const nb = (o.noiseBand && isNum(o.noiseBand.center) && isNum(o.noiseBand.half)
    && o.noiseBand.half > 0) ? o.noiseBand : null;
  if (nb) {
    lo = Math.min(lo, nb.center - nb.half);
    hi = Math.max(hi, nb.center + nb.half);
  }
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
  if (nb) {
    const top = y(nb.center + nb.half);
    const bot = y(nb.center - nb.half);
    svg.appendChild(hov(
      svgEl('rect', {
        x: pad, y: Math.min(top, bot), width: w - 2 * pad,
        height: Math.max(0.5, Math.abs(bot - top)), class: 'dn-spark-noise',
      }),
      'measured noise floor · movement inside this band is indistinguishable from a re-roll (±' + fmt(nb.half) + ')',
    ));
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
    return emptyState(svg, w, h, 'no generations yet');
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
  // Column headers draw rotated -45° (text-anchor:start), so each one rises
  // UP-AND-RIGHT from its anchor at (cx, headH-6). For long labels — the LAST
  // column most of all — that rotated run overruns the top/right of the viewBox
  // and `xMinYMin meet` clips it. Size the top reserve (headH) and a right pad
  // to the rotated extent of the longest rendered column label so the header
  // never escapes the viewBox. (~0.71·len ≈ cos(45°); ~6px/char is a
  // conservative width for the small dn-hm-col glyphs; the cap matches the
  // shortLabel(c.label) default of 12.) Short labels keep the old 44/6 reserve.
  const hdrCap = 12;
  const maxColChars = cols.reduce((m, c) => Math.max(m, Math.min(hdrCap, String(c.label == null ? '' : c.label).length)), 0);
  const rotExtent = Math.ceil(0.7071 * maxColChars * 6);
  const headH = Math.max(o.headHeight || 44, rotExtent + 8);
  const rightPad = rotExtent + 6;
  const w = labelW + cols.length * cw + rightPad;
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
  if (d && d.pass === true) { svg.appendChild(svgEl('circle', { cx, cy, r: 2.6, class: 'dn-glyph-pass' })); return hov(svg, 'passed'); }
  if (d && d.pass === false) {
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
    // The worst-judge bar always reaches the plot end (w-36); a left-anchored
    // value there overruns the 36px right gutter and clips the viewBox. When the
    // bar end lands in the right ~15% of the plot, right-anchor the value and
    // inset it just inside the bar end so it grows leftward and stays on-canvas.
    // Shorter bars keep the value to the RIGHT of the bar, as before.
    const plotEnd = w - 36;
    const inset = bx >= plotEnd - 0.15 * (plotEnd - x0);
    const vt = inset
      ? svgEl('text', { x: bx - 4, y: cy + 3, class: 'dn-vbar-val', 'text-anchor': 'end', 'data-inset': '1' })
      : svgEl('text', { x: bx + 4, y: cy + 3, class: 'dn-vbar-val' });
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
    return emptyState(svg, w, h, 'no paired board duels yet');
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
      // RIGHT label is start-anchored at rightX+8 and grows RIGHTWARD; a 14-char
      // label + a large-magnitude value overruns the right gutter and clips the
      // viewBox. Measure the run (10px mono ⇒ ~0.6em/char) and clamp the start x
      // inward so the whole text ends at or before w−edge. A no-op for short
      // labels (the common case): x stays at rightX+8.
      const rtext = `${fmt(s.b, 1)}  ${shortLabel(s.label, 14)}`;
      const rW = rtext.length * 6;
      const rEdge = 4;
      const rx0 = rightX + 8;
      const rx = Math.min(rx0, w - rEdge - rW);
      const rAttr = { x: rx, y: rl + 3, class: 'dn-pslope-label', 'text-anchor': 'start' };
      if (rx < rx0 - 0.01) rAttr['data-clamped'] = '1';
      const tx = svgEl('text', rAttr);
      tx.textContent = rtext;
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
    return emptyState(svg, w, h, 'no rungs yet');
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
      // spread the survivors down the band's inner span (2*hOut-16). In a NARROW
      // band (hOut<8, e.g. a wide entering field narrowing to 2-3 survivors) that
      // span goes ≤0, so floor the PER-LANE step to a legible row pitch (11px text)
      // — never stack runners on ~the same y. Wide bands keep their natural spread
      // (span/(n-1) already exceeds the pitch), so this is a no-op for normal data.
      const laneStep = Math.max(12, (2 * hOut - 16) / Math.max(1, survRunners.length - 1));
      const cy = survRunners.length === 1 ? midY
        : midY - hOut + 8 + i * laneStep;
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
        // bound the cut name + ` ✕` to the stage band: it is anchored at labelX and
        // must stay LEFT of the band's right edge x1, never bleeding into the stage
        // gap or the next band. Cap the id from the px budget (x1 − labelX) at ~6.2px
        // per glyph, reserving two glyphs for the ` ✕` suffix (≥3 so a name shows).
        const cutCap = Math.max(3, Math.floor((x1 - labelX) / 6.2) - 2);
        // a dead-end branch that drops from the band's lower edge and then a SHORT
        // stub that stops just LEFT of the label — the connector must lead INTO
        // the cut name, never run through it (it used to extend the full stage
        // width at the label's own baseline, slashing across the text).
        svg.appendChild(svgEl('path', {
          d: `M${elbowX},${edgeYAtElbow} V${branchY} H${labelX - 4}`,
          class: 'dn-funnel-deadedge', fill: 'none',
        }));
        funnelRunner(svg, o, sid, rung, j, labelX, branchY, 'cut', null, null, cutCap);
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
  // suppress the gate-flow when the last rung settled with EVERY lane cut and no
  // champion seated (lastLeave===0 && !crowned): bandHalf(0) floors to 6, which
  // would otherwise draw a thin converging sliver into a gate with no runner
  // feeding it. A pending/live last rung carries the full field (lastLeave>0),
  // and a crowned gate (empty survivors but seated champion) still flows.
  if (!(lastLeave === 0 && !crowned)) {
    svg.appendChild(svgEl('polygon', {
      points: `${lastX},${midY - flowH} ${gx},${midY - 11} ${gx},${midY + 11} ${lastX},${midY + flowH}`,
      class: 'dn-funnel-band dn-funnel-gateflow' + (crowned ? ' dn-good' : ''),
    }));
  }
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
function funnelRunner(svg, o, sid, rung, j, x, cy, verdict, lane, barW, cap) {
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
  t.textContent = shortLabel(sid, lane ? 8 : (cap || 13)) + glyph + laneSuffix + projSuffix;
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
    return emptyState(svg, w, h, 'no swiss rounds yet');
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
      // the primary label is ALWAYS just the `a v b` pairing — the status suffix
      // (winner / running N/M / queued) rides the cy+13 sub-line below, so a long
      // in-flight suffix can't overrun the ~colW round column into the next round.
      t.textContent = fitLabel(a, 6 * 11 * CHAR_EM, 11) + ' v ' + fitLabel(b, 6 * 11 * CHAR_EM, 11);
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
        // status moves onto the sub-line (same cy+13 baseline the decided branch
        // uses), reusing the primary label's class so the live/racing color carries.
        const sub = svgEl('text', { x: x + 6, y: cy + 13, class: cls });
        sub.textContent = progText.replace(/^ · /, '');
        g.appendChild(sub);
      } else if (queued) {
        // queued pairings carry their status on the sub-line too (no overrun).
        const sub = svgEl('text', { x: x + 6, y: cy + 13, class: cls });
        sub.textContent = progText.replace(/^ · /, '');
        g.appendChild(sub);
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
    // H7: this name shares its row with the right-anchored points value
    // (end-anchored at sx+standW-6). A two-digit rank, the crown, and the
    // ` ~proj` tag each eat horizontal budget; shrink the id cap by what those
    // decorations consume (floored at 4 so ids stay distinguishable) so the
    // name can never reach the points gutter. The common single-digit,
    // undecorated row keeps the full 9-char cap — no regression.
    const hasCrown = isChamp || isFormer || isLeader;
    const idCap = Math.max(4, 9 - (i + 1 >= 10 ? 1 : 0) - (hasCrown ? 2 : 0) - (proj ? 6 : 0));
    lab.textContent = `${i + 1}. ${fitLabel(sid, idCap * 11 * CHAR_EM, 11)}` + (isChamp ? ' ' + CROWN.current : (isFormer || isLeader ? ' ' + CROWN.former : '')) + (proj ? ' ~proj' : '');
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
  const x1 = sx + standW;
  if (leaderId) {
    // the committed feeding edge — a flat run from the standings column to the
    // gate. The gate row never changes y, so the path is a single horizontal
    // stroke; the earlier `V${cy}` mid-elbow was a no-op and is dropped.
    svg.appendChild(svgEl('path', { d: `M${x1},${cy} H${gx}`, class: 'dn-swissladder-edge' + (crowned ? ' dn-swissladder-edge-champ' : ''), fill: 'none' }));
  } else if (rounds.length) {
    // ORPHAN-GATE GUARD: rounds exist but no leader is committed yet (live — the
    // standings column is empty, or leaderId hasn't resolved). The gate box still
    // renders, so feed it a STUB edge from the standings column to the midpoint —
    // it signals "feeds from standings, no committed leader" without drawing a
    // full connection the data hasn't earned.
    const mx = (x1 + gx) / 2;
    svg.appendChild(svgEl('path', { d: `M${x1},${cy} H${mx}`, class: 'dn-swissladder-edge dn-swissladder-edge-stub', fill: 'none' }));
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
// Reads the SERVED elim model verbatim (DQ1: the server computes, the client
// renders): `rounds` arrive PRE-SORTED (temporal WB → LB → GF) with a per-round
// `bracket_side`, per-match `loser`, and duplicates already collapsed; the
// per-generation states (played / advanced / lost / eliminated-vs-dropped /
// side / LB entry / projected) arrive as the top-level `gen_states` fold
// (`derive_elim_states`, mirrored in the Rust supervisor + the node mock).
// The client-side re-sort, dedupe, elimination-vs-drop pass, and phantom-✕
// guards that used to live here are DELETED — this figure is geometry only.
// `opts`:
//   { rounds:[{label, bracket_side, matches:[{competitors, winner, loser,
//       decision, pending, bye, projected}]}],
//     gen_states:[{generation_id, played_rounds, advanced_rounds, lost_rounds,
//       eliminated_at_round, side_by_round, lb_entry_round, projected}],
//     championId, benchmarkId, gateState, live, onCompetitor(id) }
export function elimFlow(opts) {
  const o = opts || {};
  // COLUMN ORDER is the SERVED order — the server pre-sorts by round index
  // (temporal WB → LB → GF), so column ci here == the round's index in the
  // payload == the gen_states round references. No client re-sort.
  const rounds = Array.isArray(o.rounds) ? o.rounds : [];
  const live = !!o.live;
  const champId = o.championId != null ? String(o.championId) : null;
  const benchId = o.benchmarkId != null ? String(o.benchmarkId) : null;

  const nCols = rounds.length;
  // each column's bracket side + the match-node layer, read off the payload.
  const colSide = rounds.map((r) => ((r && r.bracket_side) === 'LB' ? 'LB' : 'WB'));
  const isDouble = colSide.indexOf('LB') >= 0;
  // the per-round MATCHES (a two-lane convergence each): competitors + the
  // SERVED winner/loser pair, with the live in-flight state per leg. A bye /
  // placeholder (fewer than two named competitors) draws no convergence node.
  const matchesByCol = rounds.map((r, ci) => (r && Array.isArray(r.matches) ? r.matches : [])
    .map((m) => {
      const comps = (Array.isArray(m.competitors) ? m.competitors : []).map(String).filter((c) => c && c !== 'tbd');
      if (comps.length < 2 || m.bye) return null;
      const winner = m.winner ? String(m.winner) : null;
      const pending = !!m.pending || (!winner && !m.bye && !m.decision);
      return { comps, winner, loser: m.loser != null ? String(m.loser) : null, pending,
        delta: isNum(m.delta_scalar) ? m.delta_scalar : null,
        slot: m.bracket_slot || m.match_id || '', isLB: colSide[ci] === 'LB',
        // the per-side live PROJECTED standing on an in-flight match.
        projected: (m.projected && typeof m.projected === 'object') ? m.projected : null };
    })
    .filter((m) => m));

  // ── the per-generation states, read VERBATIM from the served fold ──
  // gen id → { played, advanced, lostAt (Sets of column indices), eliminatedAt,
  // sideOf (col → WB|LB), lbEntryCol }. `pendingAt` is the residue: a played
  // column that is neither an advance nor a loss is still in flight.
  const genState = new Map();
  for (const gs of (Array.isArray(o.gen_states) ? o.gen_states : [])) {
    if (!gs || gs.generation_id == null) continue;
    const sideOf = new Map();
    const sbr = (gs.side_by_round && typeof gs.side_by_round === 'object') ? gs.side_by_round : {};
    for (const k of Object.keys(sbr)) sideOf.set(Number(k), sbr[k] === 'LB' ? 'LB' : 'WB');
    const played = new Set(Array.isArray(gs.played_rounds) ? gs.played_rounds : []);
    const advanced = new Set(Array.isArray(gs.advanced_rounds) ? gs.advanced_rounds : []);
    const lostAt = new Set(Array.isArray(gs.lost_rounds) ? gs.lost_rounds : []);
    const pendingAt = new Set([...played].filter((ci) => !advanced.has(ci) && !lostAt.has(ci)));
    genState.set(String(gs.generation_id), {
      id: String(gs.generation_id), played, advanced, lostAt, pendingAt,
      eliminatedAt: isNum(gs.eliminated_at_round) ? gs.eliminated_at_round : null,
      sideOf, lbEntryCol: isNum(gs.lb_entry_round) ? gs.lb_entry_round : null,
    });
  }
  // per-generation live PROJECTED standing: the SERVED gen-state projection
  // seeds it; a pending match's own `projected` map (the live overlay the
  // client stamps from SSE-fresh board progress — in-flight DECORATION, not
  // re-derivation) refreshes it, since the runner can write a projection
  // after the server's last publish.
  const projByGen = new Map();
  for (const gs of (Array.isArray(o.gen_states) ? o.gen_states : [])) {
    if (gs && gs.generation_id != null && gs.projected && isNum(gs.projected.scalar)) {
      projByGen.set(String(gs.generation_id), gs.projected);
    }
  }
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
  // from its WB-loss dot into its LB re-entry node, routed through a reserved
  // CHANNEL below the whole stack, each on its own horizontal lane. Collected
  // here (before geometry) so the channel count sizes the figure. Presentation
  // ROUTING only — the drop/elimination CLASSIFICATION itself is served.
  const demotions = [];
  if (isDouble) for (const g of genState.values()) {
    const cols = [...g.played].sort((a, b) => a - b);
    for (const ci of cols) {
      // a DROP is a non-terminal loss (served: lost here, not eliminated here).
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
    return emptyState(svg, w, h, 'no bracket rounds yet');
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

  // round-axis headers (e.g. WB R0 · LB R1 · Grand final · champion-gate).
  // A label that already fits is kept VERBATIM ("Semifinal", "Final",
  // "LB Round 1", "Grand final", "Rung 1"). Only the double-elim strategy's
  // GENERIC, over-long, round-INDISTINCT bracket-side labels — "Winners'
  // bracket" / "Losers' bracket" (15–16 chars, identical across every round of
  // that side) — get compacted to a tight side+round token ("WB R0" / "LB R1")
  // derived from the column's bracket_slot, since shortLabel() otherwise cut
  // them to an unreadable, ambiguous "Winners' br…".
  const colLabel = (r) => {
    const lab = String(r.label || '');
    if (/winners.*bracket/i.test(lab) || /losers.*bracket/i.test(lab)) {
      for (const m of (Array.isArray(r.matches) ? r.matches : [])) {
        const sm = String((m && m.bracket_slot) || '').match(/^(WB|LB)-R(\d+)/i);
        if (sm) return sm[1].toUpperCase() + ' R' + sm[2];
      }
      return /losers/i.test(lab) ? 'LB' : 'WB';
    }
    return shortLabel(lab || `R${isNum(r.round_index) ? r.round_index : ''}`, 12);
  };
  rounds.forEach((r, ci) => {
    const hx = colX(ci);
    const head = svgEl('text', { x: hx, y: top - 12, class: 'dn-elimflow-col', 'text-anchor': 'middle' });
    head.textContent = colLabel(r);
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
      // an UNDECIDED (pending) match is the figure's primary in-flight signal:
      // the convergence node reads as "deciding" — slightly larger + a soft pulse
      // (reduced-motion-safe) — since the lanes no longer draw a leg to the gate.
      const node = svgEl('circle', { cx: x, cy: ymid, r: m.pending ? 3.2 : 3,
        class: 'dn-elimflow-convnode' + (m.pending ? ' dn-elimflow-deciding' : m.winner ? ' dn-elimflow-good' : '') + (projMatch ? ' dn-proj' : '') });
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
      // A lane ELIMINATED at this column TERMINATES here (its ✕) — it must draw
      // NO forward segment, even when it ALSO won a different match in the SAME
      // column during a degenerate / live multi-match round (champion-vs-field
      // seeding can put one gen in two col-0 matches: a win AND a loss). Without
      // this guard the won-match marked the lane `advanced`, so a green segment
      // left the eliminated dot and ran to a column with no dot — the dangling
      // "disconnected" line. Elimination wins.
      if (!eliminated && (advanced || pending || dropped)) {
        const nextCi = cols.find((c) => c > ci);
        // a lane reaches the GATE from the last column only when it WON / is still
        // racing there (advanced or pending) — never on a drop (a dropped lane
        // always has a later played column, so it never falls through to here).
        //
        // LIVE-INITIALIZATION GAP: mid-tournament a lane can ADVANCE from a
        // non-final column while its NEXT match is not yet seeded into the
        // bracket (nextCi null, not at the final column). Previously that drew
        // NO segment, orphaning the dot so the lane read as "disconnected". We
        // instead draw a short DASHED stub into the next column slot — "advanced,
        // awaiting its next match" — so the lane always connects forward.
        // FORWARD-EDGE COMMITMENT: a lane earns an edge TOWARD the outcome (the
        // next round / the champion-gate) only once it has actually ADVANCED
        // (won its match). A still-undecided (pending) head-to-head is committed
        // NOWHERE — drawing a leg to the gate would imply BOTH competitors of one
        // match advance, which is nonsense in a double-elim (one wins, one drops
        // to the losers' bracket). So a pending lane draws only a SHORT
        // dashed "racing this match" stub; the
        // real forward edge appears when the match resolves.
        const atFinal = ci === nCols - 1;
        let toX = null;
        let awaiting = false;
        if (nextCi != null) toX = colX(nextCi);            // a DECIDED later column (advance / drop)
        else if (advanced && atFinal) toX = gateX;          // a WON lane reaches the champion-gate
        else if (advanced) { toX = colX(ci + 1); awaiting = true; }  // won; next match not yet seeded
        // UNDECIDED (pending): a short dashed in-flight stub only — committed nowhere yet.
        else if (pending) toX = Math.min(x + Math.max(18, colW * 0.4), gateX - 6);
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
          // an `awaiting` stub (advanced, next match not yet seeded) reads as
          // pending/dashed — it is not a confirmed advance.
          const segCls = 'dn-elimflow-seg ' + (dropped ? 'dn-elimflow-seg-drop dn-elimflow-bad'
            : (pending || awaiting) ? 'dn-elimflow-seg-pending' : 'dn-elimflow-good');
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
    return emptyState(svg, w, h, 'no swiss rounds yet');
  }

  // panel (1): the standings BUMP CHART
  const ttl = svgEl('text', { x: 2, y: 14, class: 'dn-swissover-title' });
  ttl.textContent = 'standings by round' + (live ? ' · LIVE' : '');
  svg.appendChild(ttl);
  // A SINGLE round (nR < 2) has no horizontal travel: scale() would pin the lone
  // column to padL (the left gutter), stacking every start/end dot + name + rank
  // label on one x. Center the lone column in the plot band instead so the dot
  // sits mid-figure with its name label in the left gutter and its rank label in
  // the right gutter. colX() routes both the axis and the bump points through it.
  const single = nR < 2;
  const Xscale = scale([0, Math.max(1, nR - 1)], [padL, w - padR]);
  const cX = padL + (w - padR - padL) / 2;
  const colX = (j) => (single ? cX : Xscale(j));
  const Y = scale([1, Math.max(2, nC)], [bumpTop, bumpTop + (nC - 1) * rowH]);
  labels.forEach((lab, j) => {
    const x = colX(j);
    const tk = svgEl('text', { x, y: bumpTop - 8, class: 'dn-swissover-round', 'text-anchor': single ? 'middle' : (j === 0 ? 'start' : (j === labels.length - 1 ? 'end' : 'middle')) });
    // compact axis ticks — "Swiss round 2" → "R2", "Champion gate" → "Gate" —
    // so the labels never truncate to an ambiguous "Swiss r…". A custom round
    // label (neither "round N" nor "gate", e.g. "Tiebreak"/"Tiebreaker") has no
    // canonical short form, so it keeps shortLabel's natural cap (12) — at 10px
    // mono with these middle ticks centered, a 12-char label still clears the
    // column gap — rather than clipping to an ambiguous "Tiebrea…".
    const ls = String(lab);
    const rm = ls.match(/(\d+)/);
    tk.textContent = /gate/i.test(ls) ? 'Gate' : (/round/i.test(ls) && rm ? 'R' + rm[1] : shortLabel(ls, 12));
    svg.appendChild(tk);
    svg.appendChild(svgEl('line', { x1: x, x2: x, y1: bumpTop - 4, y2: bumpTop + (nC - 1) * rowH + 4, class: 'dn-swissover-grid' }));
  });
  // one polyline per competitor; champion emphasised.
  series.forEach((s) => {
    const pts = [];
    s.ranks.forEach((r, j) => { if (isNum(r)) pts.push([colX(j), Y(r)]); });
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
    // end-dots (start + final rank) + left name label + right rank label. With a
    // single point (one round) the start and end coincide → draw ONE dot, never a
    // doubled-up pair on the same x.
    const [x0, y0] = pts[0];
    const [xn, yn] = pts[pts.length - 1];
    const dotCls = 'dn-swissover-dot' + (champ ? ' dn-swissover-dot-champ' : '');
    const r = champ ? 3.4 : 2.6;
    svg.appendChild(svgEl('circle', { cx: x0, cy: y0, r, class: dotCls }));
    if (pts.length > 1) svg.appendChild(svgEl('circle', { cx: xn, cy: yn, r, class: dotCls }));
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
  const nameW = 78;                              // left gutter: fits shortLabel(id,9)+glyph (~66px) inside the viewBox
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
  // with the name gutter (left) or the gate (right) even at the max |Δ|. The
  // rightmost IMPROVING lane (Δ<0) rides toward the gate, so its label can be a
  // 3+-integer-digit signed value (e.g. -128.4); size the reserve to the widest
  // such formatted-Δ label (10px mono ≈ 6px/char + a small margin) so it never
  // overruns toward the gate box. Floored at 32 so normal magnitudes are
  // unchanged. (The format here mirrors the per-lane draw site below.)
  const dWidth = (d) => textPx(fmtSigned(d, Math.abs(d) < 0.1 ? 2 : 1), 10);
  const maxImproveLabelW = Math.max(0, ...challengers
    .map((c) => c.delta).filter((d) => isNum(d) && d < 0).map(dWidth));
  const labelPad = Math.max(32, maxImproveLabelW + 6);
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
    return emptyState(svg, W, H, 'no challengers on the track yet');
  }

  // the axis line.
  svg.appendChild(svgEl('line', { x1: padL, y1: axisY, x2: W - padR, y2: axisY, class: 'dn-scalartrack-axis' }));
  if (!mini) {
    const cap = svgEl('text', { x: padL, y: top - 18, class: 'dn-scalartrack-cap' });
    // cap wide enough for a full multi-word rung label (e.g. "Quarterfinal gauntlet");
    // the caption is left-anchored at x=padL with ~470px of axis room, so a long
    // label keeps its distinguishing word instead of clipping at 14.
    cap.textContent = `${shortLabel(rung.label || `Rung ${focus}`, 30)} — scalar, lower is better`;
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
    const projSuffix = m.projected && isNum(m.lane.projected_scalar) ? ' ~' + fmt(m.lane.projected_scalar, 1) + ' proj' : '';
    const labText = shortLabel(m.id, mini ? 6 : 9) + projSuffix;
    // data-marker labels are middle-anchored at m.x; a far marker (sitting at padL
    // or W−padR) would clip half its label past the viewBox edge — axis/bench labels
    // are edge-anchored and safe, data markers are not. Measure the label (9px mono
    // ⇒ ~0.6em/char) and pull its x inboard
    // so the whole label stays in [edge, W−edge]. A no-op for any marker that fits.
    const labW = labText.length * (mini ? 5 : 5.4);
    const labEdge = mini ? 2 : 3;
    const labX = Math.max(labEdge + labW / 2, Math.min(m.x, W - labEdge - labW / 2));
    const lab = svgEl('text', { x: labX, y: ly, class: 'dn-scalartrack-name ' + verdictCls + (m.projected ? ' dn-proj' : ''), 'text-anchor': 'middle' });
    lab.textContent = labText;
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
// A stable content digest of the racing model (U5: the generic digestOpts fold
// — its 3dp number rounding subsumes the old per-scalar toFixed(3), so
// sub-precision projection jitter still does not flip the gate). Mode flags
// (mini/responsive) + the hover callback are dropped so the hero mini and the
// full figure gate on content alone.
export function racingScalarTrackDigest(opts) {
  return digestOpts(opts, ['mini', 'responsive', 'onCompetitor']);
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
  // The SERVER outcome is authoritative (U5/DQ1): a challenger's cleared /
  // failed / tied verdict is decided server-side against the gate, never
  // re-derived here. An absent outcome reads honestly as 'pending' (still on
  // boards / undecided) rather than a client guess vs champScalar.
  const outcomeOf = (c) => c.outcome ? String(c.outcome) : 'pending';

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
    return { c, v, lane, racing, projected, outcome: outcomeOf(c), survivor: !!c.survivor };
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
    return emptyState(svg, W, H, 'no challengers have entered the gauntlet');
  }
  const bandTop = top - 2;
  const bandBot = H - (mini ? 8 : 22);

  // the champion STANDARD line (solid accent) — the reference all bars start from.
  if (champScalar != null) {
    const cx = X(champScalar);
    svg.appendChild(hov(svgEl('line', { x1: cx, y1: bandTop, x2: cx, y2: bandBot, class: 'dn-fieldbars-standard' }),
      champId ? `champion ${champId} · standard ${fmt(champScalar, 3)}` : `champion standard ${fmt(champScalar, 3)}`));
    // the centered standard label clips when the champion is the field best/worst
    // (cx near padL / W-padR). Measure its real width (9.5px mono ⇒ ~0.6em/char)
    // and clamp the centered x to the plot band — a no-op for a mid-field champion.
    const ctText = mini ? 'champ' : (champId ? shortLabel(champId, 10) + ' standard' : 'champ standard');
    const ctHalf = ctText.length * (mini ? 3 : 2.85);
    const ctx = Math.max(padL + ctHalf, Math.min(cx, W - padR - ctHalf));
    const ct = svgEl('text', { x: ctx, y: bandTop - 4, class: 'dn-fieldbars-axis', 'text-anchor': 'middle' });
    ct.textContent = ctText;
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
    // the scalar value just past the marker (settled / projected). A worst-end
    // challenger lands its dot near the band's right edge (W − padR); a start-
    // anchored value there (`12.345`, ~42px) overruns the W viewBox, so when dx
    // sits in the right ~15% of the band we flip the value INBOARD (end-anchored
    // at dx − gap, growing leftward). Mid-band values are unchanged.
    if (!mini && isNum(row.v)) {
      const vgap = row.survivor ? 8 : 7;
      const nearRight = dx >= (W - padR) - 0.15 * (W - padR - padL);
      const vt = svgEl('text', { x: dx + (nearRight ? -vgap : vgap), y: cy + 3, class: 'dn-fieldbars-val ' + cls + (row.projected ? ' dn-proj' : ''), 'text-anchor': nearRight ? 'end' : 'start' });
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
// A stable content digest of the gauntlet field (U5: the generic digestOpts
// fold). Mode flags + the hover callback are dropped so the hero mini and the
// full figure gate on content alone.
export function gauntletFieldBarsDigest(opts) {
  return digestOpts(opts, ['mini', 'responsive', 'onCompetitor']);
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
    return emptyState(svg, W, H, 'not enough axes to plot');
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
    const shown = shortLabel(full, labelMax);
    // HORIZONTAL (non-rotated) right/left-quadrant labels: clamp x so the
    // estimated text extent stays inside the viewBox. The full-size non-legend
    // radar (W=360, cx=180, R=128) drops a start-anchored East-spoke label at
    // lx≈326; a labelMax-char name (~0.6em mono ≈ 5.7px/char) would run past W.
    // Pull the start in (right quadrant) / push it out (left quadrant) just
    // enough to fit, but never across the chart centre cx (so the label stays in
    // its own quadrant). Rotated near-vertical labels follow the spoke and are
    // bounded by labelPad already.
    let tx = lx;
    if (!rotate && anchor !== 'middle') {
      const estW = shown.length * 5.7;          // 9.5px mono ≈ 0.6em/char
      const edgePad = 4;
      if (anchor === 'start' && tx + estW > W - edgePad) tx = Math.max(cx, W - edgePad - estW);
      else if (anchor === 'end' && tx - estW < edgePad) tx = Math.min(cx, edgePad + estW);
    }
    const t = svgEl('text', {
      x: tx, y: ly + 3, class: 'dn-radar-axislab',
      'text-anchor': rotate ? (dy < 0 ? 'start' : 'end') : anchor,
    });
    if (rotate) {
      // rotate around the tip so the text runs outward along the spoke direction.
      const deg = Math.atan2(dy, dx) * 180 / Math.PI + (dy < 0 ? 90 : -90);
      t.setAttribute('transform', `rotate(${deg.toFixed(1)} ${lx.toFixed(1)} ${(ly + 3).toFixed(1)})`);
    }
    t.textContent = shown;
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
// A stable content digest of the radar (U5: the generic digestOpts fold — 3dp
// rounding keeps a BT credible-interval tightening repainting while a no-op
// beat stays byte-identical). `raw` (tooltip-only underlying values) + mode
// flags + the hover callback are dropped.
export function radarSilhouetteDigest(opts) {
  return digestOpts(opts, ['raw', 'mini', 'responsive', 'onAxis']);
}

// The elim epoch overview + Match-ups both render the BRACKET-AS-FLOW
// (`elimFlow`) — the seat/box bracket tree (`elimBracket`) is retired.

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
// A stable content digest of the proposing field-status list (U5: digestOpts
// over the normalized rows; the 'prop|' prefix is kept as the stable namespace).
export function proposingDigest(fieldStatus) {
  const list = (Array.isArray(fieldStatus) ? fieldStatus : []).map((f) => f
    ? { g: f.generation_id, s: f.status, a: typeof f.attempts === 'number' ? f.attempts : null, r: f.reason ? String(f.reason).slice(0, 48) : '' }
    : null);
  return 'prop|' + digestOpts({ f: list });
}

// ── the FIELD-DIVERSITY MATRIX — challenger × mutation-site ──────────
//
// The idea-overlap structure as a presence grid in the shipped dn-mtx grammar:
// one COLUMN per challenger, one ROW per distinct mutation-site, a filled square
// where that challenger touched that site. Coinciding columns ARE the same idea —
// the visual the mean/max-Jaccard ribbon summarises numerically. `opts.membership`
// is `[{generation_id, sites:[mutationId,…]}]` (the dashboard derives it). The
// membership is NOT on the `diversity` block (only the scalars + the max pair), so
// absent / <2 challengers / no sites → null → no matrix (byte-identical to today).
// `opts.highlightPair` is the max_overlap_pair whose columns get the accent rail.
export function diversityMatrix(opts) {
  const o = opts || {};
  const members = (Array.isArray(o.membership) ? o.membership : [])
    .filter((m) => m && m.generation_id != null && Array.isArray(m.sites) && m.sites.length);
  if (members.length < 2) return null;
  const gens = members.map((m) => String(m.generation_id));
  // the union of distinct sites (rows), stable-sorted so a no-op beat is identical.
  const siteSet = new Set();
  for (const m of members) for (const s of m.sites) if (s != null && s !== '') siteSet.add(String(s));
  const sites = Array.from(siteSet).sort();
  if (!sites.length) return null;
  const touched = new Map();   // gid -> Set(siteId)
  for (const m of members) touched.set(String(m.generation_id), new Set(m.sites.map(String)));
  const pair = Array.isArray(o.highlightPair) ? o.highlightPair.map(String) : [];
  const isPaired = (g) => pair.includes(g);
  const onCompetitor = typeof o.onCompetitor === 'function' ? o.onCompetitor : null;

  const table = el('table', { class: 'dn-mtx dn-divmtx' });
  const hr = el('tr');
  hr.appendChild(el('th', { class: 'dn-mtx-corner', text: 'site · challenger →' }));
  for (const g of gens) {
    const cell = el('th', { class: 'dn-mtx-gen' + (isPaired(g) ? ' dn-divmtx-paired' : '') }, [
      onCompetitor
        ? clickable(el('span', { class: 'dn-mtx-genlink', text: shortLabel(g, 14) }), () => onCompetitor(g))
        : el('span', { class: 'dn-mtx-genlink', text: shortLabel(g, 14) }),
    ]);
    hr.appendChild(cell);
  }
  table.appendChild(el('thead', null, [hr]));
  const tbody = el('tbody');
  for (const s of sites) {
    const tr = el('tr', { class: 'dn-mtx-row' });
    tr.appendChild(el('th', { class: 'dn-mtx-site', scope: 'row' }, [
      el('span', { class: 'dn-mtx-file', text: s }),
    ]));
    for (const g of gens) {
      const on = touched.get(g).has(s);
      const td = el('td', { class: 'dn-mtx-cell' + (on ? ' dn-mtx-on' : '') + (isPaired(g) ? ' dn-divmtx-paired' : ''),
        'data-gen': g, 'data-site': s });
      if (on) {
        td.appendChild(svgEl('svg', { class: 'dn-mtx-mark', width: 16, height: 16, viewBox: '0 0 16 16', role: 'img' }, [
          svgEl('rect', { x: 3, y: 3, width: 10, height: 10, rx: 2, class: 'dn-mtx-square' }),
        ]));
      } else {
        td.appendChild(el('span', { class: 'dn-mtx-blank', 'aria-hidden': 'true', text: '·' }));
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return el('div', { class: 'dn-divmtx-wrap' }, [
    el('div', { class: 'dn-table-scroll' }, [table]),
    el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;',
      text: 'column = challenger · row = mutation site · ▪ = touched here · coinciding columns are the same idea (the overlap the ribbon scores)' }),
  ]);
}

// Digest for the diversity matrix — the membership (gid → sorted site ids) + the
// highlighted pair, no floats / no timestamps. Empty (<2 members) → a stable
// sentinel so the absent state is byte-identical beat-over-beat.
// A stable content digest of the diversity matrix (U5: digestOpts over the
// NORMALIZED members — the < 2 collapse to the 'divmtx|none' sentinel is
// load-bearing: fewer than two members is "no matrix", a single stable state).
export function diversityMatrixDigest(opts) {
  const o = opts || {};
  const members = (Array.isArray(o.membership) ? o.membership : [])
    .filter((m) => m && m.generation_id != null && Array.isArray(m.sites) && m.sites.length);
  if (members.length < 2) return 'divmtx|none';
  const pair = Array.isArray(o.highlightPair) ? o.highlightPair.map(String).slice().sort() : [];
  return 'divmtx|' + digestOpts({
    m: members.map((m) => String(m.generation_id) + ':' + m.sites.map(String).slice().sort().join('+')),
    pair,
  });
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
      svgEl('text', { x: cx, y: spineY - 16, class: 'dn-roundtl-champid', 'text-anchor': 'middle' }, [fitLabel(champId, 10 * 12 * CHAR_EM, 12, { mid: true })]),
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
    return emptyState(svg, w, h, 'no rounds yet');
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
    // an IMPROVED step lifts the crown well clear of the floor label (which sits at
    // yTo − 6, also centred on cx) so the ♛ never overprints the number; a
    // regressed/flat step drops it below the station (yTo + 14).
    if (!held && s.gen != null && yTo != null) {
      const cr = svgEl('text', { x: cx, y: y(s.to) - (isNum(s.from) && isNum(s.to) && s.to < s.from ? 18 : -14), class: 'dn-waterfall-crown', 'text-anchor': 'middle' });
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
    return emptyState(svg, w, h, 'no reign yet');
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
    lbl.textContent = fitLabel(String(r.id), 12 * 10.5 * CHAR_EM, 10.5, { mid: true }) + ' ' + (current ? CROWN.current : CROWN.former);
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
    return emptyState(svg, W, h, 'no epochs recorded yet');
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
    // band-id is the band's PRIMARY label — like its width-gated siblings
    // (bandsub > 84 / champlbl > 104 below) it must NOT spill into the
    // neighbour band. Rather than hide it on a narrow band, shrink the
    // char-cap ∝ band width (≈6.5px/glyph; 8px left pad + 8px breathing
    // room): wide bands clamp to the full 14, narrow bands truncate in place.
    const idCap = Math.max(6, Math.min(14, Math.floor((b.w - 16) / 6.5)));
    g.appendChild(txt(b.x0 + 8, bandTop + 18, midLabel(String(e.epoch_id), idCap),
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
    // colid is middle-anchored at b.xc, so a full 14-char id (~92px) overprints
    // its neighbour once a band narrows below that. Same width-discipline as the
    // band's siblings (bandsub b.w>84 / champlbl b.w>104): shrink the cap ∝ b.w
    // (~6.6px/char, less a hair of intra-band margin) and DROP it when even ~2
    // chars won't fit. A wide band (single epoch, ≤~6 equal-effort epochs) keeps
    // cap≥14 — identical to the prior shortLabel(...,14).
    const colidCap = Math.min(14, Math.floor((b.w - 4) / 6.6));
    if (colidCap >= 2) {
      svg.appendChild(txt(b.xc, hsBot + 14, midLabel(String(e.epoch_id), colidCap),
        { class: 'dn-metaledger-colid', 'text-anchor': 'middle' }));
    }
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
// A stable content digest of the meta-loop ledger (U5: digestOpts over the
// NORMALIZED rows — the epoch_id filter + the absent-vs-empty collapse are
// load-bearing: `{}` and `{epochs:[]}` must digest identically, and a
// malformed `epochs` still yields a string).
export function metaLoopLedgerDigest(opts) {
  const o = opts || {};
  const rows = (Array.isArray(o.epochs) ? o.epochs : []).filter((e) => e && e.epoch_id != null);
  return digestOpts({ currentEpochId: o.currentEpochId != null ? String(o.currentEpochId) : null, epochs: rows });
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
    return emptyState(svg, W, H, 'no scored predictions yet');
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
  // A faint dashed BRIDGE re-connects scored points ACROSS the null gaps the
  // solid pen lifted over, so SPARSE scoring (scored→null→scored) reads as a
  // trend rather than nothing: with every scored gen isolated the solid line is
  // all move-tos (invisible), and the bridge is the only thing that draws. On
  // DENSE data no gap is ever bridged → the bridge string stays empty and the
  // figure is byte-identical to before. The bridge is drawn UNDER the solid
  // line so adjacent-scored runs keep their crisp solid stroke on top.
  let d = '';
  let bridge = '';
  let penDown = false;
  let prevScored = null; // { i, f } of the last scored gen, for gap-bridging
  pts.forEach((p, i) => {
    const f = p && isNum(p.score_fraction) ? p.score_fraction : null;
    if (f == null) { penDown = false; return; }
    if (!penDown && prevScored) {
      // the pen lifted since the last scored gen (≥1 null between) — span it.
      bridge += `M${x(prevScored.i).toFixed(2)},${y(prevScored.f).toFixed(2)} `
        + `L${x(i).toFixed(2)},${y(f).toFixed(2)} `;
    }
    d += `${penDown ? 'L' : 'M'}${x(i).toFixed(2)},${y(f).toFixed(2)} `;
    penDown = true;
    prevScored = { i, f };
  });
  if (bridge) svg.appendChild(svgEl('path', {
    d: bridge.trim(), class: 'dn-caltrend-bridge', fill: 'none',
    // mirror the faint-dashed reference grammar inline (no CSS edit needed) and
    // keep a class distinct from dn-caltrend-mean so the rolling-mean reference
    // stays the single dn-caltrend-mean element.
    stroke: 'var(--v2-ink-soft)', 'stroke-width': '1', 'stroke-dasharray': '4 3',
    'vector-effect': 'non-scaling-stroke', opacity: '0.55',
  }));
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
// A stable content digest of the calibration trend (U5: digestOpts over the
// NORMALIZED points — the generation_id filter + 3dp rounding keep sub-precision
// score jitter from flipping the gate).
export function calibrationTrendDigest(opts) {
  const o = opts || {};
  const pts = (Array.isArray(o.points) ? o.points : []).filter((p) => p && p.generation_id != null)
    .map((p) => ({
      g: String(p.generation_id),
      sf: isNum(p.score_fraction) ? p.score_fraction : null,
      tc: isNum(p.total_claims) ? p.total_claims : 0,
      d: p.decision == null ? null : String(p.decision),
    }));
  return digestOpts({ rm: isNum(o.rolling_mean) ? o.rolling_mean : null, ts: isNum(o.trend_sign) ? o.trend_sign : 0, p: pts });
}

// components/scalar_band.js — the promote-threshold number line.
//
// A 1-D scalar axis that answers "did the challenger clear the bar?".
// The scalar is a LOSS (lower is better), so a challenger PROMOTES when
// its scalar drops below the threshold:
//
//     threshold = champion - margin
//
//   * the champion value is the reference tick;
//   * a shaded band spans ±`margin` around the threshold — a challenger
//     landing inside it "improved but not enough";
//   * a challenger at or below the threshold is a real win (green);
//   * a challenger above the champion is a regression (red).
//
// `challengerInterval = { lo, hi }` is the forward-looking uncertainty
// hook: when present it draws an error bar (a replication confidence
// interval); when absent the challenger is just a point. CI-ready, but
// simple — the geometry handles either shape without a code branch at
// the call site.

import { el, svgEl } from '../core/dom.js';
import { fmtScalar } from '../core/format.js';

const W = 360;
const H = 64;
const PAD = 24;

/**
 * Render the scalar band number line.
 *
 * champion           — the reference (champion) scalar.
 * challenger         — the challenger scalar plotted against it.
 * margin             — promote margin; threshold = champion - margin.
 * challengerInterval — optional { lo, hi } drawn as an error bar.
 */
export function scalarBand({ champion, challenger, margin = 0, challengerInterval } = {}) {
  const wrap = el('div', { class: 'sband' });
  const finite = (v) => typeof v === 'number' && isFinite(v);
  if (!finite(champion) || !finite(challenger)) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No scalar to plot.']));
    return wrap;
  }

  const m = finite(margin) ? Math.abs(margin) : 0;
  const threshold = champion - m;
  const lo = challengerInterval && finite(challengerInterval.lo) ? challengerInterval.lo : null;
  const hi = challengerInterval && finite(challengerInterval.hi) ? challengerInterval.hi : null;

  // Domain spans every value we must show, with a little breathing room.
  const vals = [champion, challenger, threshold, champion + m];
  if (lo != null) vals.push(lo);
  if (hi != null) vals.push(hi);
  let dMin = Math.min(...vals);
  let dMax = Math.max(...vals);
  if (dMax === dMin) { dMin -= 0.5; dMax += 0.5; }
  const slack = (dMax - dMin) * 0.08;
  dMin -= slack;
  dMax += slack;
  const sx = (v) => PAD + ((v - dMin) / (dMax - dMin)) * (W - 2 * PAD);

  // A challenger at/below the threshold is a real win.
  const won = challenger <= threshold;
  const regressed = challenger > champion;
  const challengerCls = won ? 'sband-win' : (regressed ? 'sband-regress' : 'sband-near');

  const svg = svgEl('svg', {
    class: 'sband-svg', viewBox: `0 0 ${W} ${H}`,
    role: 'img', 'aria-label': 'scalar promote band',
  });
  const axisY = Math.round(H * 0.55);

  // baseline axis
  svg.appendChild(svgEl('line', {
    x1: PAD, y1: axisY, x2: W - PAD, y2: axisY, class: 'sband-axis',
  }));

  // ±margin band around the threshold
  if (m > 0) {
    const bx = sx(champion - m);
    const bw = Math.max(0, sx(champion + m) - bx);
    svg.appendChild(svgEl('rect', {
      x: bx, y: axisY - 10, width: bw, height: 20, class: 'sband-band',
    }));
  }

  // threshold tick
  svg.appendChild(svgEl('line', {
    x1: sx(threshold), y1: axisY - 14, x2: sx(threshold), y2: axisY + 14,
    class: 'sband-threshold',
  }));

  // champion reference tick
  svg.appendChild(svgEl('line', {
    x1: sx(champion), y1: axisY - 12, x2: sx(champion), y2: axisY + 12,
    class: 'sband-champion',
  }));

  // challenger interval (error bar), when present
  if (lo != null && hi != null) {
    svg.appendChild(svgEl('line', {
      x1: sx(lo), y1: axisY, x2: sx(hi), y2: axisY,
      class: 'sband-interval ' + challengerCls,
    }));
    for (const v of [lo, hi]) {
      svg.appendChild(svgEl('line', {
        x1: sx(v), y1: axisY - 5, x2: sx(v), y2: axisY + 5,
        class: 'sband-interval-cap ' + challengerCls,
      }));
    }
  }

  // challenger point
  svg.appendChild(svgEl('circle', {
    cx: sx(challenger), cy: axisY, r: 5, class: 'sband-point ' + challengerCls,
  }));

  wrap.appendChild(svg);

  // legend strip — text mirrors the geometry for grayscale / a11y.
  const verdict = won ? 'win' : (regressed ? 'regressed' : 'near-miss');
  wrap.appendChild(el('div', { class: 'sband-legend mono' }, [
    el('span', { class: 'sband-legend-item' }, ['champion ' + fmtScalar(champion)]),
    el('span', { class: 'sband-legend-item' }, ['threshold ' + fmtScalar(threshold)]),
    el('span', { class: 'sband-legend-item ' + challengerCls }, [
      'challenger ' + fmtScalar(challenger)
      + (lo != null && hi != null ? ` [${fmtScalar(lo)}, ${fmtScalar(hi)}]` : '')
      + ' · ' + verdict,
    ]),
  ]));
  return wrap;
}

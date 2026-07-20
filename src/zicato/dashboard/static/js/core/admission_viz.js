// js/core/admission_viz.js — the admission visuals (TRAJECTORY-UI.md §2.2a).
//
// The shared render vocabulary for a suggestion's admission stats, reused by
// BOTH the inbox cards (views/builder.js) and the Evals ghost rows
// (views/evals.js) so a suggested entry reads the SAME everywhere. No new colour
// vocabulary and no fabricated numbers — the marks reuse the shipped grammar:
//
//   * the FLIP-RATE WHISKER — the A/A flip rate as a point on a rail with the
//     advisory-ceiling reference rule (the BT-whisker vocabulary, candidate.js
//     `ratingWhisker`); over the ceiling the point rides `--v2-caution` (the
//     noisy-eval signal). Unmeasured → a faint rail, never a fabricated 0.
//   * the DISCRIMINATION PIPS — `separated` of `pairs` as filled/empty pips (the
//     `dt-rungstep` pip idiom, live.js); a dead channel is all-empty.
//   * the EVIDENCE TIER — `probed` renders FIRM, `planned`/unmeasured renders
//     `dn-faint` (the shade-by-evidence rule, EVAL-VIEW.md §4.1).
//
// Every measured number rides its mark WITH its n (the honesty rule); an
// unmeasured probe renders the word "unmeasured", never `0.0`.
//
// THE ONE SHAPE. The renderer reads the RENDER-READY `admission_viz` block the
// provenance reader emits (TRAJECTORY-UI.md §3.3): {measured, evidence_tier,
// flip:{measured,rate,runs,over_ceiling,ceiling}, discrimination:{measured,
// separated,pairs}, leakage_ok}. The `/builder/suggestions` feed carries the
// ENGINE admission shape ({noise, discrimination, leakage}) instead, so
// `vizFromFeedAdmission` adapts it to the one shape — the marks never branch on
// source.

import { el, svgEl } from './dom.js';
import { fmt, isNum } from '../svg.js';

export const FLIP_CEILING = 0.25;   // RECOMMENDED_FLIP_CEILING (suggestions.py)

// Adapt the `/builder/suggestions` feed admission ({noise, discrimination,
// leakage}) to the render-ready `admission_viz` shape. A null/plan-mode
// admission → an honest unmeasured block (planned tier, no fabricated numbers).
export function vizFromFeedAdmission(adm) {
  if (!adm || typeof adm !== 'object') {
    return {
      measured: false, evidence_tier: 'planned',
      flip: { measured: false, rate: null, runs: null, over_ceiling: false, ceiling: FLIP_CEILING },
      discrimination: { measured: false, separated: 0, pairs: 0 },
      leakage_ok: null,
    };
  }
  const noise = adm.noise && typeof adm.noise === 'object' ? adm.noise : {};
  const disc = adm.discrimination && typeof adm.discrimination === 'object' ? adm.discrimination : {};
  const leak = adm.leakage && typeof adm.leakage === 'object' ? adm.leakage : null;
  const noiseM = noise.measured === true && isNum(noise.flip_rate);
  const discM = disc.measured === true;
  const measured = noiseM || discM;
  return {
    measured,
    evidence_tier: measured ? 'probed' : 'planned',
    flip: {
      measured: noiseM,
      rate: noiseM ? noise.flip_rate : null,
      runs: noiseM ? (noise.runs != null ? noise.runs : null) : null,
      over_ceiling: noiseM && typeof noise.flip_rate === 'number' && noise.flip_rate > FLIP_CEILING,
      ceiling: FLIP_CEILING,
    },
    discrimination: {
      measured: discM,
      separated: discM && isNum(disc.separated) ? disc.separated : 0,
      pairs: discM && isNum(disc.pairs) ? disc.pairs : 0,
    },
    // null when the feed carried no leakage block (unchecked); false only when a
    // flag actually fired.
    leakage_ok: leak ? (leak.target_slice_ok !== false && leak.self_preference_flag !== true) : null,
  };
}

// The flip-rate whisker: a point on a [0, hi] rail with the advisory-ceiling
// reference rule. Reuses the BT-whisker figure vocabulary (dn-bt-whisker / rail
// / theta); over-ceiling adds the caution tone. Unmeasured → a faint rail with
// the honest "unmeasured" label. `flip` is the `admission_viz.flip` block.
export function flipWhisker(flip) {
  const f = flip && typeof flip === 'object' ? flip : {};
  const ceiling = isNum(f.ceiling) ? f.ceiling : FLIP_CEILING;
  const measured = f.measured === true && isNum(f.rate);
  const rate = measured ? f.rate : null;
  // a stable domain: twice the ceiling, widened if a measured rate overruns it
  // (so an off-scale noisy channel still shows a point at the right edge).
  const hi = Math.max(ceiling * 2, measured ? rate : 0, 0.4);
  const W = 150, H = 22, padX = 5, axW = W - 2 * padX;
  const X = (v) => padX + Math.max(0, Math.min(1, v / hi)) * axW;
  const mid = H / 2;
  const over = measured && rate > ceiling;
  const fig = svgEl('svg', {
    class: 'dn-adm-whisker dn-bt-whisker', width: '100%', viewBox: `0 0 ${W} ${H}`,
    preserveAspectRatio: 'none', role: 'img', style: `aspect-ratio: ${W} / ${H};`,
    'aria-label': measured
      ? `flip rate ${fmt(rate, 2)} over ${f.runs != null ? f.runs : 0} runs, advisory ceiling ${fmt(ceiling, 2)}`
      : 'flip rate unmeasured',
  });
  fig.appendChild(svgEl('line', { x1: padX, y1: mid, x2: W - padX, y2: mid, class: 'dn-bt-rail' }));
  // the advisory ceiling reference rule (a dashed vertical — the P(stronger)
  // threshold idiom).
  const cx = X(ceiling);
  fig.appendChild(svgEl('line', { x1: cx, y1: 3, x2: cx, y2: H - 3, class: 'dn-adm-ceiling dn-bt-prob-thr' }));
  if (measured) {
    fig.appendChild(svgEl('circle', {
      cx: X(rate), cy: mid, r: 3.5,
      class: 'dn-adm-flipdot dn-bt-theta' + (over ? ' dn-caution' : ''),
    }));
  } else {
    const t = svgEl('text', { x: W / 2, y: mid + 3.5, class: 'dn-bt-unfit', 'text-anchor': 'middle' });
    t.textContent = 'unmeasured';
    fig.appendChild(t);
  }
  return fig;
}

// The discrimination pips: `separated` filled of `pairs` total (the dt-rungstep
// idiom). A dead channel (separated 0) is all-empty; unmeasured → a faint word.
// Capped at a sane pip count so a large `pairs` never overruns the row.
export function discriminationPips(disc) {
  const d = disc && typeof disc === 'object' ? disc : {};
  if (d.measured !== true || !isNum(d.pairs) || d.pairs <= 0) {
    return el('span', { class: 'dn-adm-pips dn-adm-pips-unmeasured dn-faint', text: 'unmeasured' });
  }
  const pairs = Math.max(0, Math.round(d.pairs));
  const sep = Math.max(0, Math.min(pairs, isNum(d.separated) ? Math.round(d.separated) : 0));
  const shown = Math.min(pairs, 12);
  const wrap = el('div', {
    class: 'dn-adm-pips dt-rungstep', role: 'img',
    'aria-label': `discrimination ${sep} of ${pairs} pairs separated`,
  });
  for (let i = 0; i < shown; i++) {
    wrap.appendChild(el('span', {
      class: 'dt-rungstep-pip dn-adm-pip' + (i < sep ? ' dt-rungstep-done' : ''),
      'aria-hidden': 'true',
    }));
  }
  if (pairs > shown) wrap.appendChild(el('span', { class: 'dn-faint dn-adm-pipmore', text: `+${pairs - shown}` }));
  return wrap;
}

// The full admission-visuals block: the evidence-tier marker + the flip whisker
// (with its measured readout) + the discrimination pips (with their readout).
// `viz` is a render-ready `admission_viz` block. `opts.compact` drops the row
// labels (the ghost-cell placement, where the column header names the stat).
export function admissionVisuals(viz, opts) {
  const v = viz && typeof viz === 'object' ? viz : vizFromFeedAdmission(null);
  const compact = !!(opts && opts.compact);
  const flip = v.flip || {};
  const disc = v.discrimination || {};
  const probed = v.evidence_tier === 'probed';

  const flipReadout = flip.measured === true && isNum(flip.rate)
    ? el('span', {
      class: 'dn-adm-readout dn-mono' + (flip.over_ceiling ? ' dn-adm-over' : ''),
      text: `flip ${fmt(flip.rate, 2)} (n=${flip.runs != null ? flip.runs : 0})`
        + (flip.over_ceiling ? ' — over ceiling' : ''),
    })
    : el('span', { class: 'dn-adm-readout dn-faint', text: 'flip unmeasured' });

  const discReadout = disc.measured === true && isNum(disc.pairs)
    ? el('span', { class: 'dn-adm-readout dn-mono', text: `sep ${disc.separated}/${disc.pairs}` })
    : el('span', { class: 'dn-adm-readout dn-faint', text: 'sep unmeasured' });

  const tierMark = el('span', {
    class: 'dn-adm-tier' + (probed ? ' dn-adm-tier-firm' : ' dn-adm-tier-faint dn-faint'),
    title: probed
      ? 'probed — an admission probe was spent (firm evidence)'
      : 'planned — no probe was spent (unmeasured; plan mode)',
    text: probed ? 'probed' : 'planned',
  });

  const flipRow = el('div', { class: 'dn-adm-row dn-adm-flip' },
    (compact ? [] : [el('span', { class: 'dn-adm-lab dn-faint', text: 'flip' })])
      .concat([flipWhisker(flip), flipReadout]));
  const discRow = el('div', { class: 'dn-adm-row dn-adm-disc' },
    (compact ? [] : [el('span', { class: 'dn-adm-lab dn-faint', text: 'sep' })])
      .concat([discriminationPips(disc), discReadout]));

  const kids = [
    el('div', { class: 'dn-adm-tierrow' }, [tierMark]),
    flipRow,
    discRow,
  ];
  if (v.leakage_ok === false) {
    kids.push(el('div', { class: 'dn-adm-leak', text: 'leakage flag fired — the motivating proposer saw the target slice' }));
  }
  return el('div', { class: 'dn-adm' + (compact ? ' dn-adm-compact' : '') }, kids);
}

// js/v2/components/driftComposition.js — the EFFECT panel's drift-KIND view.
//
// DASHBOARD-V2 §3 + the causal-narrative redesign: the EFFECT panel does
// not show bare scalar deltas — it shows WHICH BEHAVIORS moved. Drift is
// typed (off_topic / looping_reasoning / schema_violation / verbosity /
// …); this component renders the champion→challenger movement of each
// kind as a horizontal diverging bar so the operator sees the *behavioral
// composition* of the change at a glance: green = the kind got rarer (an
// improvement, fewer such drift events), red = it got more common.
//
// It is the visual sibling of the v1 divergingBar but speaks the --v2-*
// token language (the v1 atom carries v1-token CSS), and it is
// INTERACTIVE: each kind row carries a `data-kind` hook + highlights when
// the parent asks (a patch hover lights up the kinds it plausibly moved).
//
// Pure factory: returns a detached node; holds no module state.

import { el } from '../../core/dom.js';

// Format a signed integer count delta with a true minus sign.
function fmtCount(d) {
  if (d > 0) return '+' + d;
  if (d < 0) return '−' + Math.abs(d);
  return '0';
}

/**
 * driftComposition — champion→challenger movement per drift kind.
 *
 * movements — [{ kind, champion_count, challenger_count, delta, direction }]
 *   from GET /api/drift-movements/{gen}. `delta` = challenger − champion
 *   (negative = improvement: fewer drift events on the challenger).
 * onHover / onHoverEnd — optional (kind) callbacks so a hover here can
 *   reflect back into the CAUSE panel (and vice-versa).
 */
export function driftComposition({ movements, onHover, onHoverEnd } = {}) {
  const list = (Array.isArray(movements) ? movements : [])
    .filter((m) => m && m.kind != null && typeof m.delta === 'number' && isFinite(m.delta));

  const wrap = el('div', { class: 'v2-driftcomp', role: 'list', 'aria-label': 'drift movement by kind' });
  if (list.length === 0) return wrap; // caller renders the honest empty state

  // Biggest absolute movement first; scale bars to the largest |delta|.
  const sorted = list.slice().sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta));
  let ceiling = 0;
  for (const m of sorted) { const a = Math.abs(m.delta); if (a > ceiling) ceiling = a; }
  if (ceiling === 0) ceiling = 1;

  for (const m of sorted) {
    const kind = String(m.kind);
    const good = m.delta < 0;          // fewer drift events → improvement
    const cls = m.delta === 0 ? 'v2-driftcomp-flat' : (good ? 'v2-driftcomp-good' : 'v2-driftcomp-bad');
    const onRight = m.delta === 0 ? null : good;
    const pct = Math.min(1, Math.abs(m.delta) / ceiling) * 100;

    const fill = el('span', { class: 'v2-driftcomp-fill ' + cls });
    fill.style.width = pct.toFixed(1) + '%';
    const half = el('span', {
      class: 'v2-driftcomp-half ' + (onRight === true ? 'v2-driftcomp-right' : onRight === false ? 'v2-driftcomp-left' : 'v2-driftcomp-center'),
    }, [fill]);
    const track = el('span', { class: 'v2-driftcomp-track' }, [
      el('span', { class: 'v2-driftcomp-axis', 'aria-hidden': 'true' }),
      half,
    ]);

    const counts = (typeof m.champion_count === 'number' && typeof m.challenger_count === 'number')
      ? el('span', { class: 'v2-driftcomp-counts v2-mono', title: 'champion → challenger event count' }, [
          String(m.champion_count), ' → ', String(m.challenger_count),
        ])
      : null;

    const row = el('div', {
      class: 'v2-driftcomp-row', role: 'listitem', 'data-kind': kind, tabindex: '0',
      title: kind + ': ' + (good ? 'fewer' : (m.delta > 0 ? 'more' : 'unchanged')) + ' drift events on the challenger',
    }, [
      el('span', { class: 'v2-driftcomp-kind v2-mono' }, [kind]),
      track,
      el('span', { class: 'v2-driftcomp-val v2-mono ' + cls }, [fmtCount(m.delta)]),
      counts,
    ]);

    if (typeof onHover === 'function' || typeof onHoverEnd === 'function') {
      const enter = () => { if (onHover) onHover(kind); };
      const leave = () => { if (onHoverEnd) onHoverEnd(kind); };
      row.addEventListener('mouseenter', enter);
      row.addEventListener('focusin', enter);
      row.addEventListener('mouseleave', leave);
      row.addEventListener('focusout', leave);
    }
    wrap.appendChild(row);
  }
  return wrap;
}

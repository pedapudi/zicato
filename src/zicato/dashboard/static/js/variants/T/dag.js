// variants/T/dag.js — the compact candidate-lifecycle DAG (Console).
//
// Self-contained, deliberately SMALL: a single static SVG (NO pan/zoom) that
// reads one candidate's life left-to-right as a flow of cause → effect →
// verdict:
//
//   PARENT ─▶ PATCH ─▶ [ board fan: one node per entry ] ─▶ Σ ─▶ GATE ─▶ TERMINAL
//
// Pure builder: (spec) -> detached <svg>. Mark classes are `ezn-*` and are
// styled (scoped under the variant root) by css/variants/N/console.css.

import { svgEl } from '../../core/dom.js';
import { isNum, fmt } from './svg.js';

export function verdictClass(verdict) {
  const v = String(verdict || '').toLowerCase();
  if (v.includes('promot')) return 'ezn-promoted';
  if (v.includes('reject')) return 'ezn-rejected';
  if (v.includes('defer')) return 'ezn-deferred';
  if (v === 'running' || v.includes('flight') || v === 'live') return 'ezn-running';
  if (v === 'baseline' || v === 'seed') return 'ezn-baseline';
  return 'ezn-neutral';
}

function clip(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

function flow(x1, y1, x2, y2) {
  const mx = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
}

function rectNode(layer, cx, cy, w, h, label, sub, cls) {
  const g = svgEl('g', { class: 'ezn-node ' + (cls || ''), 'data-cz': 'lc-step' }, [
    svgEl('rect', { x: cx - w / 2, y: cy - h / 2, width: w, height: h, rx: 6, class: 'ezn-node-box' }),
    svgEl('text', { x: cx, y: cy - (sub ? 5 : 0), class: 'ezn-node-id' }, [clip(label, 18)]),
    sub ? svgEl('text', { x: cx, y: cy + 12, class: 'ezn-node-sub' }, [clip(sub, 22)]) : null,
  ].filter(Boolean));
  layer.appendChild(g);
  return g;
}

export function lifecycleDag(spec) {
  const o = spec || {};
  const entries = Array.isArray(o.entries) ? o.entries : [];
  const baseline = !!o.baseline || !o.parentId;
  const dec = baseline ? 'baseline' : (o.decision || 'running');
  // `width` is now the viewBox's INTERNAL coordinate width — the SVG itself is
  // rendered at width:100% (see the attrs below) so it FITS its pane and never
  // overflows. A wider viewBox just means a finer internal grid (the figure is
  // scaled down to fit by preserveAspectRatio); a narrower one (compare split)
  // keeps labels legible at the smaller painted size.
  const w = o.width || 900;
  const h = o.height || 360;

  const svg = svgEl('svg', {
    // FIT-TO-WIDTH: width:100% + a viewBox (no fixed pixel width that exceeds
    // the panel, no horizontal-scroll wrapper). All six stages stay visible.
    class: 'ezn-dag', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': `Lifecycle of ${o.genId || 'candidate'}`,
  });

  const cols = { parent: 0.075, patch: 0.245, board: 0.46, agg: 0.66, gate: 0.82, term: 0.95 };
  const X = {};
  for (const k of Object.keys(cols)) X[k] = cols[k] * w;
  const midY = h * 0.5;
  const fanTop = h * 0.16;
  const fanBot = h * 0.84;

  const heads = [
    [X.parent, 'PARENT'], [X.patch, 'PATCH'], [X.board, 'BOARD'],
    [X.agg, 'Σ'], [X.gate, 'GATE'], [X.term, baseline ? 'SEED' : 'TERMINAL'],
  ];
  for (const [x, t] of heads) svg.appendChild(svgEl('text', { x, y: 16, class: 'ezn-col-head', 'text-anchor': 'middle' }, [t]));

  const edgeLayer = svgEl('g', { class: 'ezn-edge-layer' });
  const nodeLayer = svgEl('g', { class: 'ezn-node-layer' });

  rectNode(nodeLayer, X.parent, midY, 0.12 * w, 44, o.parentId || '∅ seed', baseline ? 'no parent' : 'champion', baseline ? 'ezn-baseline' : 'ezn-promoted');

  const patchSub = baseline ? 'seed snapshot'
    : (isNum(o.patchPoints) && o.patchPoints > 0 ? o.patchPoints + ' mutation point' + (o.patchPoints === 1 ? '' : 's') : 'patch');
  const patchNode = rectNode(nodeLayer, X.patch, midY, 0.13 * w, 44, baseline ? 'seed' : 'PATCH', patchSub, baseline ? 'ezn-baseline' : 'ezn-patch');
  // fix #2: the PATCH node is clickable → this candidate's side-by-side diff.
  if (!baseline && o.onPatch) {
    patchNode.classList.add('ezn-clickable');
    patchNode.style.cursor = 'pointer';
    patchNode.setAttribute('tabindex', '0');
    patchNode.setAttribute('role', 'button');
    patchNode.setAttribute('aria-label', 'Open this candidate’s patch diff');
    patchNode.appendChild(svgEl('title', null, ['Open this candidate’s side-by-side patch diff']));
    patchNode.addEventListener('click', () => o.onPatch());
    patchNode.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onPatch(); } });
  }
  edgeLayer.appendChild(svgEl('path', { d: flow(X.parent + 0.06 * w, midY, X.patch - 0.065 * w, midY), class: 'ezn-edge ezn-edge-spine', fill: 'none' }));

  const total = entries.reduce((a, e) => a + (isNum(e.drift_loss) ? e.drift_loss : 0), 0) || 1;
  const step = entries.length > 1 ? (fanBot - fanTop) / (entries.length - 1) : 0;
  if (entries.length === 0) {
    rectNode(nodeLayer, X.board, midY, 0.14 * w, 40, 'no board entries', 'scored', 'ezn-neutral');
  } else {
    entries.forEach((e, i) => {
      const y = entries.length > 1 ? fanTop + i * step : midY;
      const r = 12;
      const cls = e.pass_fail === 1 ? 'ezn-promoted' : (e.wall_clock_budget_exceeded ? 'ezn-deferred' : 'ezn-rejected');
      edgeLayer.appendChild(svgEl('path', { d: flow(X.patch + 0.065 * w, midY, X.board - r, y), class: 'ezn-edge ezn-edge-soft', fill: 'none' }));
      const contrib = (isNum(e.drift_loss) ? e.drift_loss : 0) / total;
      edgeLayer.appendChild(svgEl('path', { d: flow(X.board + r, y, X.agg - 0.05 * w, midY), class: 'ezn-edge ' + (cls === 'ezn-promoted' ? 'ezn-edge-good' : 'ezn-edge-bad'), 'stroke-width': Math.max(1, contrib * 12), fill: 'none' }));
      const g = svgEl('g', {
        class: 'ezn-node ezn-board-node ' + cls, 'data-cz': 'lc-board-node',
        'data-key': e.entry_id, tabindex: o.onEntry ? '0' : null,
        'aria-label': `${e.entry_id} drift loss ${isNum(e.drift_loss) ? fmt(e.drift_loss) : '—'}`,
      }, [
        svgEl('title', null),
        svgEl('circle', { cx: X.board, cy: y, r, class: 'ezn-board-disc' }),
        svgEl('text', { x: X.board, y: y - r - 4, class: 'ezn-board-label', 'text-anchor': 'middle' }, [clip(e.entry_id, 20)]),
        svgEl('text', { x: X.board, y: y + 3, class: 'ezn-board-loss', 'text-anchor': 'middle' }, [isNum(e.drift_loss) ? fmt(e.drift_loss, 0) : '—']),
      ]);
      const tt = g.childNodes[0];
      if (tt) tt.textContent = `${e.entry_id}: loss ${isNum(e.drift_loss) ? fmt(e.drift_loss) : '—'}`
        + (e.wall_clock_budget_exceeded ? ' · timed out' : '')
        + (e.pass_fail === 0 ? ' · failed' : e.pass_fail === 1 ? ' · passed' : '');
      if (o.onEntry) {
        g.style.cursor = 'pointer';
        g.addEventListener('click', () => o.onEntry(e.entry_id));
        g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onEntry(e.entry_id); } });
      }
      nodeLayer.appendChild(g);
    });
  }

  rectNode(nodeLayer, X.agg, midY, 0.1 * w, 48, 'Σ loss', entries.length ? fmt(total, 0) : '—', 'ezn-neutral');

  edgeLayer.appendChild(svgEl('path', { d: flow(X.agg + 0.05 * w, midY, X.gate - 0.06 * w, midY), class: 'ezn-edge ' + (verdictClass(dec) === 'ezn-promoted' ? 'ezn-edge-good' : 'ezn-edge-bad'), fill: 'none' }));
  const gateSub = baseline ? 'no gate (seed)' : (isNum(o.deltaScalar) ? (o.deltaScalar >= 0 ? '+' : '') + fmt(o.deltaScalar, 1) + ' Δ' : dec);
  rectNode(nodeLayer, X.gate, midY, 0.12 * w, 48, baseline ? 'BASELINE' : 'GATE', gateSub, verdictClass(dec));

  const promoted = dec === 'promoted' || (baseline && o.promoted === true);
  const termLabel = baseline ? 'seed' : (promoted ? '♛ promoted' : '✕ dead branch');
  const termCls = baseline ? 'ezn-baseline' : (promoted ? 'ezn-promoted' : 'ezn-rejected');
  edgeLayer.appendChild(svgEl('path', { d: flow(X.gate + 0.06 * w, midY, X.term - 0.045 * w, midY), class: 'ezn-edge ' + (promoted ? 'ezn-edge-good' : 'ezn-edge-bad'), fill: 'none' }));
  rectNode(nodeLayer, X.term, midY, 0.1 * w, 48, termLabel, baseline ? 'defines floor' : (promoted ? 'new champion' : 'champion stands'), termCls);

  svg.appendChild(edgeLayer);
  svg.appendChild(nodeLayer);
  return svg;
}

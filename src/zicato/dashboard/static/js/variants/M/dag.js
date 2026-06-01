// variants/M/dag.js — the compact candidate-lifecycle DAG (Ledger II).
//
// Self-contained, C-inspired but deliberately SMALL: a single static SVG
// (no pan/zoom surface) that reads one candidate's life left-to-right as a
// flow of cause → effect → verdict:
//
//   PARENT ─▶ PATCH ─▶ [ board fan: one node per entry ] ─▶ Σ ─▶ GATE ─▶ TERMINAL
//   (lineage)  (cause)   (effect — node colour = pass/fail/timeout)        (crown / dead branch)
//
// Pure builder: (spec) -> detached <svg>. Mark classes are `ez-*` and styled
// (scoped under the variant root) by css/variants/M/ledger2.css. No external
// charting library; every node is an addressable SVG element.

import { svgEl } from '../../core/dom.js';
import { isNum, fmt } from './svg.js';

// verdict → semantic class used by the gate + terminal nodes.
export function verdictClass(verdict) {
  const v = String(verdict || '').toLowerCase();
  if (v.includes('promot')) return 'ez-promoted';
  if (v.includes('reject')) return 'ez-rejected';
  if (v.includes('defer')) return 'ez-deferred';
  if (v === 'running' || v.includes('flight') || v === 'live') return 'ez-running';
  if (v === 'baseline' || v === 'seed') return 'ez-baseline';
  return 'ez-neutral';
}

function clip(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

// A smooth cubic between two world points (left→right flow).
function flow(x1, y1, x2, y2) {
  const mx = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
}

function rectNode(layer, cx, cy, w, h, label, sub, cls) {
  const g = svgEl('g', { class: 'ez-node ' + (cls || ''), 'data-cz': 'lc-step' }, [
    svgEl('rect', { x: cx - w / 2, y: cy - h / 2, width: w, height: h, rx: 8, class: 'ez-node-box' }),
    svgEl('text', { x: cx, y: cy - (sub ? 5 : 0), class: 'ez-node-id' }, [clip(label, 18)]),
    sub ? svgEl('text', { x: cx, y: cy + 12, class: 'ez-node-sub' }, [clip(sub, 22)]) : null,
  ].filter(Boolean));
  layer.appendChild(g);
  return g;
}

// spec: {
//   genId, parentId, baseline, decision, reason, deltaScalar,
//   patchPoints (number of mutation points),
//   entries: [{ entry_id, drift_loss, pass_fail, wall_clock_budget_exceeded }],
//   width, height, onEntry(entryId)
// }
export function lifecycleDag(spec) {
  const o = spec || {};
  const entries = Array.isArray(o.entries) ? o.entries : [];
  const baseline = !!o.baseline || !o.parentId;
  const dec = baseline ? 'baseline' : (o.decision || 'running');
  const w = o.width || 900;
  const h = o.height || 360;

  const svg = svgEl('svg', {
    class: 'ez-dag', width: w, height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': `Lifecycle of ${o.genId || 'candidate'}`,
  });

  // Column x positions, scaled to the requested width.
  const cols = { parent: 0.075, patch: 0.245, board: 0.46, agg: 0.66, gate: 0.82, term: 0.95 };
  const X = {};
  for (const k of Object.keys(cols)) X[k] = cols[k] * w;
  const midY = h * 0.5;
  const fanTop = h * 0.16;
  const fanBot = h * 0.84;

  // Column headers.
  const heads = [
    [X.parent, 'PARENT'], [X.patch, 'PATCH'], [X.board, 'BOARD'],
    [X.agg, 'Σ'], [X.gate, 'GATE'], [X.term, baseline ? 'SEED' : 'TERMINAL'],
  ];
  for (const [x, t] of heads) {
    svg.appendChild(svgEl('text', { x, y: 16, class: 'ez-col-head', 'text-anchor': 'middle' }, [t]));
  }

  const edgeLayer = svgEl('g', { class: 'ez-edge-layer' });
  const nodeLayer = svgEl('g', { class: 'ez-node-layer' });

  // PARENT.
  rectNode(nodeLayer, X.parent, midY, 0.12 * w, 44,
    o.parentId || '∅ seed', baseline ? 'no parent' : 'champion',
    baseline ? 'ez-baseline' : 'ez-promoted');

  // PATCH (the cause).
  const patchSub = baseline ? 'seed snapshot'
    : (isNum(o.patchPoints) && o.patchPoints > 0
      ? o.patchPoints + ' mutation point' + (o.patchPoints === 1 ? '' : 's') : 'patch');
  rectNode(nodeLayer, X.patch, midY, 0.13 * w, 44, baseline ? 'seed' : 'PATCH', patchSub,
    baseline ? 'ez-baseline' : 'ez-patch');
  edgeLayer.appendChild(svgEl('path', {
    d: flow(X.parent + 0.06 * w, midY, X.patch - 0.065 * w, midY),
    class: 'ez-edge ez-edge-spine', fill: 'none',
  }));

  // BOARD fan (the effect): one node per entry, colour = outcome.
  const total = entries.reduce((a, e) => a + (isNum(e.drift_loss) ? e.drift_loss : 0), 0) || 1;
  const step = entries.length > 1 ? (fanBot - fanTop) / (entries.length - 1) : 0;
  if (entries.length === 0) {
    rectNode(nodeLayer, X.board, midY, 0.14 * w, 40, 'no board entries', 'scored', 'ez-neutral');
  } else {
    entries.forEach((e, i) => {
      const y = entries.length > 1 ? fanTop + i * step : midY;
      const r = 12;
      const cls = e.pass_fail === 1 ? 'ez-promoted'
        : (e.wall_clock_budget_exceeded ? 'ez-deferred' : 'ez-rejected');
      edgeLayer.appendChild(svgEl('path', {
        d: flow(X.patch + 0.065 * w, midY, X.board - r, y),
        class: 'ez-edge ez-edge-soft', fill: 'none',
      }));
      const contrib = (isNum(e.drift_loss) ? e.drift_loss : 0) / total;
      edgeLayer.appendChild(svgEl('path', {
        d: flow(X.board + r, y, X.agg - 0.05 * w, midY),
        class: 'ez-edge ' + (cls === 'ez-promoted' ? 'ez-edge-good' : 'ez-edge-bad'),
        'stroke-width': Math.max(1, contrib * 12), fill: 'none',
      }));
      const g = svgEl('g', {
        class: 'ez-node ez-board-node ' + cls, 'data-cz': 'lc-board-node',
        'data-key': e.entry_id, tabindex: o.onEntry ? '0' : null,
        'aria-label': `${e.entry_id} drift loss ${isNum(e.drift_loss) ? fmt(e.drift_loss) : '—'}`,
      }, [
        svgEl('title', null), // tooltip text set below
        svgEl('circle', { cx: X.board, cy: y, r, class: 'ez-board-disc' }),
        svgEl('text', { x: X.board, y: y - r - 4, class: 'ez-board-label', 'text-anchor': 'middle' }, [clip(e.entry_id, 20)]),
        svgEl('text', { x: X.board, y: y + 3, class: 'ez-board-loss', 'text-anchor': 'middle' }, [isNum(e.drift_loss) ? fmt(e.drift_loss, 0) : '—']),
      ]);
      const tt = g.childNodes[0];
      if (tt) tt.textContent = `${e.entry_id}: loss ${isNum(e.drift_loss) ? fmt(e.drift_loss) : '—'}`
        + (e.wall_clock_budget_exceeded ? ' · timed out' : '')
        + (e.pass_fail === 0 ? ' · failed' : e.pass_fail === 1 ? ' · passed' : '');
      if (o.onEntry) {
        g.style.cursor = 'pointer';
        g.addEventListener('click', () => o.onEntry(e.entry_id));
        g.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onEntry(e.entry_id); }
        });
      }
      nodeLayer.appendChild(g);
    });
  }

  // AGGREGATE (Σ loss).
  rectNode(nodeLayer, X.agg, midY, 0.1 * w, 48, 'Σ loss',
    entries.length ? fmt(total, 0) : '—', 'ez-neutral');

  // GATE (the verdict climax).
  edgeLayer.appendChild(svgEl('path', {
    d: flow(X.agg + 0.05 * w, midY, X.gate - 0.06 * w, midY),
    class: 'ez-edge ' + (verdictClass(dec) === 'ez-promoted' ? 'ez-edge-good' : 'ez-edge-bad'), fill: 'none',
  }));
  const gateSub = baseline ? 'no gate (seed)' : (isNum(o.deltaScalar) ? (o.deltaScalar >= 0 ? '+' : '') + fmt(o.deltaScalar, 1) + ' Δ' : dec);
  rectNode(nodeLayer, X.gate, midY, 0.12 * w, 48, baseline ? 'BASELINE' : 'GATE', gateSub, verdictClass(dec));

  // TERMINAL — crown or dead branch.
  const promoted = dec === 'promoted' || (baseline && o.promoted === true);
  const termLabel = baseline ? 'seed' : (promoted ? '♛ promoted' : '✕ dead branch');
  const termCls = baseline ? 'ez-baseline' : (promoted ? 'ez-promoted' : 'ez-rejected');
  edgeLayer.appendChild(svgEl('path', {
    d: flow(X.gate + 0.06 * w, midY, X.term - 0.045 * w, midY),
    class: 'ez-edge ' + (promoted ? 'ez-edge-good' : 'ez-edge-bad'), fill: 'none',
  }));
  rectNode(nodeLayer, X.term, midY, 0.1 * w, 48, termLabel,
    baseline ? 'defines floor' : (promoted ? 'new champion' : 'champion stands'), termCls);

  svg.appendChild(edgeLayer);
  svg.appendChild(nodeLayer);
  return svg;
}

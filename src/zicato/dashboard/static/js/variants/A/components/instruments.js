// variants/A/components/instruments.js — the visual vocabulary.
//
// Fresh presentation built for the Mission Control variant. These do
// NOT reuse the v2 components — they paint the dark instrument look.
// Pure builders: (data) -> DOM node. No state, no fetch.

import { el, svgEl } from '../../../core/dom.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

// -- panel frame ------------------------------------------------------
export function panel({ title, sub, accent, actions, body }) {
  const head = el('div', { class: 'mcA-panel-head' }, [
    el('div', null, [
      el('span', { class: 'mcA-panel-title' }, [title || '']),
      sub ? el('span', { class: 'mcA-panel-sub' }, ['  ', sub]) : null,
    ]),
    actions ? el('div', { class: 'mcA-panel-actions' },
      Array.isArray(actions) ? actions : [actions]) : null,
  ]);
  const p = el('div', { class: 'mcA-panel', dataset: accent ? { accent } : {} }, [head]);
  const b = el('div', { class: 'mcA-panel-body' });
  if (Array.isArray(body)) for (const c of body) { if (c) b.appendChild(c); }
  else if (body) b.appendChild(body);
  p.appendChild(b);
  return p;
}

// -- big readout ------------------------------------------------------
export function readout({ label, value, tone, foot }) {
  return el('div', { class: 'mcA-readout' }, [
    el('div', { class: 'mcA-readout-label' }, [label || '']),
    el('div', { class: 'mcA-readout-value' + (tone ? ' is-' + tone : '') },
      [value == null ? '—' : String(value)]),
    foot ? el('div', { class: 'mcA-readout-foot' }, [foot]) : null,
  ]);
}

export function readouts(items) {
  return el('div', { class: 'mcA-readouts' }, items.map(readout));
}

// -- status chip ------------------------------------------------------
export function chip(label, kind) {
  return el('span', { class: 'mcA-chip', dataset: { kind: kind || 'idle' } }, [label]);
}

// -- empty / loading --------------------------------------------------
export function empty(msg) { return el('div', { class: 'mcA-empty' }, [msg || 'No data.']); }
export function loading(msg) { return el('div', { class: 'mcA-loading' }, [msg || 'Loading…']); }

// -- sparkline (filled area, glow) -----------------------------------
// values: number[] (lower is better). Lowest point marked.
export function sparkline(values, opts = {}) {
  const w = opts.width || 200;
  const h = opts.height || 44;
  const stroke = opts.stroke || 'var(--mc-go)';
  const fin = (values || []).filter((v) => typeof v === 'number' && isFinite(v));
  if (fin.length < 2) {
    return el('div', { class: 'mcA-readout-foot' }, ['—']);
  }
  const min = Math.min(...fin);
  const max = Math.max(...fin);
  const span = max - min || 1;
  const pad = 3;
  const xs = (i) => pad + (i / (fin.length - 1)) * (w - 2 * pad);
  const ys = (v) => pad + (1 - (v - min) / span) * (h - 2 * pad);
  let d = '';
  fin.forEach((v, i) => { d += (i === 0 ? 'M' : 'L') + xs(i).toFixed(1) + ' ' + ys(v).toFixed(1) + ' '; });
  const area = d + `L${xs(fin.length - 1).toFixed(1)} ${h - pad} L${xs(0).toFixed(1)} ${h - pad} Z`;
  const gid = 'mcA-spark-' + Math.random().toString(36).slice(2, 8);
  const svg = svgEl('svg', { width: w, height: h, viewBox: `0 0 ${w} ${h}`, class: 'mcA-spark' });
  const defs = svgEl('defs');
  const grad = svgEl('linearGradient', { id: gid, x1: '0', y1: '0', x2: '0', y2: '1' });
  grad.appendChild(svgEl('stop', { offset: '0', 'stop-color': stroke, 'stop-opacity': '0.28' }));
  grad.appendChild(svgEl('stop', { offset: '1', 'stop-color': stroke, 'stop-opacity': '0' }));
  defs.appendChild(grad);
  svg.appendChild(defs);
  svg.appendChild(svgEl('path', { d: area, fill: `url(#${gid})`, stroke: 'none' }));
  svg.appendChild(svgEl('path', { d: d.trim(), fill: 'none', stroke, 'stroke-width': '1.8', 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
  // mark the best (lowest) point
  const bi = fin.indexOf(min);
  svg.appendChild(svgEl('circle', { cx: xs(bi), cy: ys(min), r: '2.6', fill: stroke }));
  return svg;
}

// -- deadline / progress bar -----------------------------------------
export function bar(frac, tone) {
  const f = Math.max(0, Math.min(1, typeof frac === 'number' ? frac : 0));
  const fill = el('div', { class: 'mcA-bar-fill', style: `width:${(f * 100).toFixed(1)}%` });
  if (tone === 'go') fill.style.background = 'var(--mc-go)';
  if (tone === 'warn') fill.style.background = 'var(--mc-warn)';
  if (tone === 'stop') fill.style.background = 'var(--mc-stop)';
  return el('div', { class: 'mcA-bar' }, [fill]);
}

// -- diverging delta bar (negative = improvement = green, left) ------
// magnitude scaled against `scale` (default 1.0); 0 sits at center.
export function deltaBar(delta, scale) {
  const track = el('div', { class: 'mcA-delta-track' });
  if (typeof delta !== 'number' || !isFinite(delta) || delta === 0) {
    return track;
  }
  const s = scale || 1;
  const frac = Math.max(-1, Math.min(1, delta / s));
  const pct = Math.abs(frac) * 50;
  const good = delta < 0; // lower loss is better
  const fill = el('div', {
    class: 'mcA-delta-fill ' + (good ? 'is-go' : 'is-stop'),
    style: good
      ? `right:50%; width:${pct.toFixed(1)}%;`
      : `left:50%; width:${pct.toFixed(1)}%;`,
  });
  track.appendChild(fill);
  return track;
}

// -- heatmap (drift / loss status grid) ------------------------------
// rows: [{label, cells:[{value|null, title, tone}]}], cols: string[]
// onCell(rowIndex, colIndex, cell): click handler.
export function heatmap({ rows, cols, onCell, tip }) {
  const table = el('table', { class: 'mcA-heat' });
  const thead = el('thead');
  const hr = el('tr', null, [el('th', { class: 'mcA-heat-rowlabel' }, [''])]);
  for (const c of cols) hr.appendChild(el('th', { class: 'mono' }, [String(c)]));
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = el('tbody');
  rows.forEach((row, ri) => {
    const tr = el('tr');
    tr.appendChild(el('td', { class: 'mcA-heat-rowlabel', title: row.label }, [row.label]));
    row.cells.forEach((cell, ci) => {
      const td = el('td');
      const div = el('div', {
        class: 'mcA-heat-cell' + (cell && cell.value != null ? '' : ' is-empty'),
      });
      if (cell && cell.value != null) {
        const norm = typeof cell.norm === 'number' ? cell.norm : 0.5;
        // green (low/good) -> amber -> red (high/bad) for sequential loss
        const bg = heatColor(norm);
        div.style.background = bg;
        if (cell.showValue) div.appendChild(el('div', { class: 'mcA-heat-val' }, [cell.text || '']));
      }
      if (onCell) div.addEventListener('click', () => onCell(ri, ci, cell));
      if (tip && cell) {
        div.addEventListener('mouseenter', (e) => tip.show(e, cell.tipTitle, cell.tipRows));
        div.addEventListener('mousemove', (e) => tip.move(e));
        div.addEventListener('mouseleave', () => tip.hide());
      }
      td.appendChild(div);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
}

// norm in [0,1]; 0 = best (green), 1 = worst (red).
export function heatColor(norm) {
  const n = Math.max(0, Math.min(1, norm));
  // interpolate green(47,230,160) -> amber(255,194,75) -> red(255,93,108)
  let r, g, b;
  if (n < 0.5) {
    const t = n / 0.5;
    r = 47 + (255 - 47) * t; g = 230 + (194 - 230) * t; b = 160 + (75 - 160) * t;
  } else {
    const t = (n - 0.5) / 0.5;
    r = 255; g = 194 + (93 - 194) * t; b = 75 + (108 - 75) * t;
  }
  const a = 0.25 + 0.6 * n; // worse cells glow louder
  return `rgba(${r | 0},${g | 0},${b | 0},${a.toFixed(2)})`;
}

// -- shared tooltip controller ---------------------------------------
export function makeTip(root) {
  const node = el('div', { class: 'mcA-tip', hidden: 'true' });
  root.appendChild(node);
  return {
    node,
    show(e, title, rows) {
      node.removeAttribute('hidden');
      const kids = [];
      if (title) kids.push(el('div', { class: 'mcA-tip-title' }, [title]));
      for (const r of (rows || [])) kids.push(el('div', { class: 'mcA-tip-row' }, [r]));
      node.textContent = '';
      for (const k of kids) node.appendChild(k);
      this.move(e);
    },
    move(e) {
      const x = (e && e.clientX) || 0;
      const y = (e && e.clientY) || 0;
      node.style.left = (x + 14) + 'px';
      node.style.top = (y + 14) + 'px';
    },
    hide() { node.setAttribute('hidden', 'true'); },
  };
}

export { SVG_NS };

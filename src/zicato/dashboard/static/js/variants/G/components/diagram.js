// variants/G/components/diagram.js — render diagram layouts to SVG.
//
// Glue between the pure layout modules (diagram/{sankey,topology,
// primitives}.js) and the DOM. Each function takes a layout's
// { nodes, edges|links, box } and returns a detached <svg>. No pan/zoom
// surface here — Bridge keeps the diagrams inline and legible (a fixed
// viewBox that scales with the panel), which is calmer than C's
// full-canvas interaction and keeps the command-center feel.

import { svgEl, el } from '../../../core/dom.js';
import { flowPath, ribbonPath, layoutDag } from '../diagram/primitives.js';

// ---- causal-flow Sankey (PATCH → DRIFT → GATE) ----------------------
export function renderSankey(layout, opts = {}) {
  const { nodes, links, box } = layout;
  const svg = svgEl('svg', {
    class: 'g-diagram g-sankey', viewBox: `0 0 ${box.w} ${box.h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img', 'aria-label': 'causal flow: patch to drift to gate',
  });
  // ribbons first (under the node blocks)
  for (const l of links) {
    svg.appendChild(svgEl('path', {
      d: ribbonPath(l.sx, l.sy, l.tx, l.ty, l.hwS, l.hwT),
      class: 'g-ribbon ' + (l.cls || ''), fill: 'currentColor',
    }, [titleNode(l.value != null ? `${l.source} → ${l.target}: ${fmtNum(l.value)}` : `${l.source} → ${l.target}`)]));
  }
  for (const n of nodes) {
    const g = svgEl('g', { class: 'g-snode ' + (n.cls || ''), tabindex: opts.onSelect ? '0' : null });
    g.appendChild(svgEl('rect', { x: n.x, y: n.y, width: n.w, height: n.h, rx: 3, class: 'g-snode-box' }));
    const tx = svgEl('text', { x: n.x + n.w / 2, y: n.y + n.h / 2, class: 'g-snode-label', 'text-anchor': 'middle' });
    tx.textContent = n.label;
    g.appendChild(tx);
    if (n.sub) {
      const st = svgEl('text', { x: n.x + n.w / 2, y: n.y + n.h / 2 + 13, class: 'g-snode-sub', 'text-anchor': 'middle' });
      st.textContent = n.sub;
      g.appendChild(st);
    }
    if (opts.onSelect && n.ref) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', () => opts.onSelect(n));
    }
    svg.appendChild(g);
  }
  return svg;
}

// ---- tournament topology (gauntlet hub + conceptual structures) -----
export function renderTopology(layout, opts = {}) {
  const { nodes, edges, box } = layout;
  const svg = svgEl('svg', {
    class: 'g-diagram g-topology', viewBox: `0 0 ${box.w} ${box.h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img', 'aria-label': 'tournament topology',
  });
  for (const e of edges) {
    const dashed = e.kind === 'cut' || e.kind === 'drop' || e.kind === 'pair';
    svg.appendChild(svgEl('path', {
      d: flowPath(e.x1, e.y1, e.x2, e.y2),
      class: 'g-edge ' + (e.cls || '') + (dashed ? ' g-edge-dashed' : ''), fill: 'none',
    }));
    if (e.label) {
      const lt = svgEl('text', { x: (e.x1 + e.x2) / 2, y: (e.y1 + e.y2) / 2 - 4, class: 'g-edge-label', 'text-anchor': 'middle' });
      lt.textContent = e.label;
      svg.appendChild(lt);
    }
  }
  for (const n of nodes) {
    const g = svgEl('g', { class: 'g-tnode ' + (n.cls || ''), tabindex: opts.onSelect ? '0' : null });
    if (n.r) {
      g.appendChild(svgEl('circle', { cx: n.x, cy: n.y, r: n.r, class: 'g-tnode-shape' }));
      const tx = svgEl('text', { x: n.x, y: n.y + 3, class: 'g-tnode-label', 'text-anchor': 'middle' });
      tx.textContent = n.label;
      g.appendChild(tx);
      if (n.sub) {
        const st = svgEl('text', { x: n.x, y: n.y + n.r + 12, class: 'g-tnode-sub', 'text-anchor': 'middle' });
        st.textContent = n.sub;
        g.appendChild(st);
      }
    } else {
      const w = n.w || 120; const h = n.h || 36;
      g.appendChild(svgEl('rect', { x: n.x, y: n.y, width: w, height: h, rx: 3, class: 'g-tnode-shape' }));
      const tx = svgEl('text', { x: n.x + w / 2, y: n.y + h / 2, class: 'g-tnode-label', 'text-anchor': 'middle' });
      tx.textContent = n.label;
      g.appendChild(tx);
      if (n.sub) {
        const st = svgEl('text', { x: n.x + w / 2, y: n.y + h / 2 + 12, class: 'g-tnode-sub', 'text-anchor': 'middle' });
        st.textContent = n.sub;
        g.appendChild(st);
      }
    }
    if (opts.onSelect) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', () => opts.onSelect(n));
    }
    svg.appendChild(g);
  }
  return svg;
}

// ---- lineage DAG (parent → child, no colliding edges) ---------------
// nodes: [{ id, parent, cls, sub }]; clickable.
export function renderLineageDag(nodes, opts = {}) {
  const layout = layoutDag(nodes);
  const colW = 150; const rowH = 64; const padX = 30; const padY = 24;
  const nodeW = 110; const nodeH = 38;
  const w = padX * 2 + (layout.maxCol + 1) * colW;
  const h = padY * 2 + layout.maxRow * rowH;
  const svg = svgEl('svg', {
    class: 'g-diagram g-lineage', viewBox: `0 0 ${w} ${Math.max(h, nodeH + padY * 2)}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img', 'aria-label': 'lineage tree',
  });
  const px = (col) => padX + col * colW;
  const py = (rowIdx) => padY + rowIdx * rowH;
  const posOf = (id) => layout.pos.get(id);
  // edges
  for (const n of nodes) {
    if (!n.parent || !posOf(n.parent)) continue;
    const p = posOf(n.parent); const c = posOf(n.id);
    const x1 = px(p.col) + nodeW; const y1 = py(p.row) + nodeH / 2;
    const x2 = px(c.col); const y2 = py(c.row) + nodeH / 2;
    svg.appendChild(svgEl('path', { d: flowPath(x1, y1, x2, y2), class: 'g-edge ' + (n.edgeCls || ''), fill: 'none' }));
  }
  for (const n of nodes) {
    const pos = posOf(n.id);
    const x = px(pos.col); const y = py(pos.row);
    const g = svgEl('g', { class: 'g-lnode ' + (n.cls || ''), tabindex: opts.onSelect ? '0' : null });
    g.appendChild(svgEl('rect', { x, y, width: nodeW, height: nodeH, rx: 3, class: 'g-lnode-shape' }));
    const tx = svgEl('text', { x: x + nodeW / 2, y: y + nodeH / 2 - 1, class: 'g-lnode-label', 'text-anchor': 'middle' });
    tx.textContent = n.id;
    g.appendChild(tx);
    if (n.sub) {
      const st = svgEl('text', { x: x + nodeW / 2, y: y + nodeH / 2 + 11, class: 'g-lnode-sub', 'text-anchor': 'middle' });
      st.textContent = n.sub;
      g.appendChild(st);
    }
    if (opts.onSelect) { g.style.cursor = 'pointer'; g.addEventListener('click', () => opts.onSelect(n)); }
    svg.appendChild(g);
  }
  return svg;
}

function titleNode(text) {
  const t = svgEl('title', null);
  t.textContent = text == null ? '' : String(text);
  return t;
}
function fmtNum(v) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(2) : '—'; }

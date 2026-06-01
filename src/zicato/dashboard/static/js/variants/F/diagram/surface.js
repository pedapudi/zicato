// variants/F/diagram/surface.js — a pan/zoom SVG canvas.
//
// Variant F is diagram-first: every hero screen is an interactive SVG.
// This module builds the shared surface — an <svg> with a <g> viewport
// the caller draws into in *world* coordinates, plus wheel-zoom and
// drag-pan that transform the viewport rather than re-laying-out the
// graph. Dependency-free; uses only core/dom.js's svgEl.
//
// The surface exposes:
//   surface.svg        — the root <svg> node (append to the DOM)
//   surface.viewport   — the <g> to draw world-space children into
//   surface.fit(box)   — frame a world-space bounding box
//   surface.reset()    — re-fit to the last framed box
//   surface.zoomBy(f)  — programmatic zoom around the centre
//
// The transform is stored as { x, y, k } (translate + uniform scale) and
// applied to the viewport's `transform` attribute. World→screen is
// screen = world * k + (x, y).

import { svgEl } from '../../../core/dom.js';

const MIN_K = 0.25;
const MAX_K = 4;

export function createSurface(opts = {}) {
  const width = opts.width || 960;
  const height = opts.height || 560;
  const ariaLabel = opts.ariaLabel || 'Interactive diagram';

  const viewport = svgEl('g', { class: 'cz-viewport' });
  const svg = svgEl('svg', {
    class: 'cz-surface',
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: 'xMidYMid meet',
    role: 'group',
    'aria-label': ariaLabel,
    tabindex: '0',
  }, [viewport]);

  const state = { x: 0, y: 0, k: 1, width, height, lastBox: null };

  function apply() {
    viewport.setAttribute(
      'transform',
      `translate(${state.x.toFixed(2)} ${state.y.toFixed(2)}) scale(${state.k.toFixed(4)})`,
    );
  }

  function clampK(k) { return Math.max(MIN_K, Math.min(MAX_K, k)); }

  // Frame a world-space box { x, y, w, h } with a little padding.
  function fit(box) {
    if (!box || !(box.w > 0) || !(box.h > 0)) { apply(); return; }
    state.lastBox = { ...box };
    const pad = opts.padding == null ? 0.08 : opts.padding;
    const bw = box.w * (1 + pad * 2);
    const bh = box.h * (1 + pad * 2);
    const k = clampK(Math.min(state.width / bw, state.height / bh));
    state.k = k;
    // Centre the box in the surface.
    const cx = box.x + box.w / 2;
    const cy = box.y + box.h / 2;
    state.x = state.width / 2 - cx * k;
    state.y = state.height / 2 - cy * k;
    apply();
  }

  function reset() { if (state.lastBox) fit(state.lastBox); }

  function zoomAround(sx, sy, factor) {
    const nk = clampK(state.k * factor);
    if (nk === state.k) return;
    // Keep the world point under (sx, sy) fixed while scaling.
    const wx = (sx - state.x) / state.k;
    const wy = (sy - state.y) / state.k;
    state.k = nk;
    state.x = sx - wx * state.k;
    state.y = sy - wy * state.k;
    apply();
  }

  function zoomBy(factor) { zoomAround(state.width / 2, state.height / 2, factor); }

  // Map a pointer event to the svg's internal viewBox coordinate space —
  // the surface scales with the container, so a raw clientX is wrong.
  function toSurfaceXY(ev) {
    const rect = svg.getBoundingClientRect ? svg.getBoundingClientRect() : null;
    if (!rect || !rect.width) return { sx: state.width / 2, sy: state.height / 2 };
    const sx = ((ev.clientX - rect.left) / rect.width) * state.width;
    const sy = ((ev.clientY - rect.top) / rect.height) * state.height;
    return { sx, sy };
  }

  // -- wheel zoom -----------------------------------------------------
  svg.addEventListener('wheel', (ev) => {
    if (typeof ev.preventDefault === 'function') ev.preventDefault();
    const { sx, sy } = toSurfaceXY(ev);
    const factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
    zoomAround(sx, sy, factor);
  });

  // -- drag pan -------------------------------------------------------
  let dragging = false;
  let last = null;
  svg.addEventListener('pointerdown', (ev) => {
    // Only pan on empty-canvas drags or left button.
    if (ev.button != null && ev.button !== 0) return;
    dragging = true;
    last = { x: ev.clientX, y: ev.clientY };
    if (svg.setPointerCapture && ev.pointerId != null) {
      try { svg.setPointerCapture(ev.pointerId); } catch { /* harness */ }
    }
    svg.classList.add('cz-grabbing');
  });
  svg.addEventListener('pointermove', (ev) => {
    if (!dragging || !last) return;
    const rect = svg.getBoundingClientRect ? svg.getBoundingClientRect() : null;
    const scaleX = rect && rect.width ? state.width / rect.width : 1;
    const scaleY = rect && rect.height ? state.height / rect.height : 1;
    state.x += (ev.clientX - last.x) * scaleX;
    state.y += (ev.clientY - last.y) * scaleY;
    last = { x: ev.clientX, y: ev.clientY };
    apply();
  });
  function endDrag() { dragging = false; last = null; svg.classList.remove('cz-grabbing'); }
  svg.addEventListener('pointerup', endDrag);
  svg.addEventListener('pointerleave', endDrag);

  apply();

  return { svg, viewport, fit, reset, zoomBy, get transform() { return { ...state }; } };
}

// A small floating control cluster (fit / zoom-in / zoom-out) the caller
// can drop next to a surface. Returns a plain <div>; wiring is via the
// passed surface handle.
export function createSurfaceControls(surface, el) {
  const btn = (label, title, fn) => el('button', {
    type: 'button', class: 'cz-ctrl-btn', title, 'aria-label': title,
    onclick: (ev) => { if (ev && ev.preventDefault) ev.preventDefault(); fn(); },
  }, [label]);
  return el('div', { class: 'cz-controls', role: 'group', 'aria-label': 'Diagram controls' }, [
    btn('+', 'Zoom in', () => surface.zoomBy(1.25)),
    btn('−', 'Zoom out', () => surface.zoomBy(1 / 1.25)),
    btn('⤢', 'Fit to view', () => surface.reset()),
  ]);
}

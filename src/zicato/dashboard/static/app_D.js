// app_D.js — Variant D entry point ("Tufte data-viz" exploration).
//
// The orchestrator wires `?ui=D` and provides a `#variant-root` element;
// this module is loaded as the page's module entry for that variant and
// paints the whole Variant D dashboard into `#variant-root`. It owns ONE
// side effect beyond mounting: it injects the variant's stylesheet link
// (scoped to `#variant-root`) so the variant is self-contained and does
// not depend on index.html being edited to add the <link>.
//
// Everything else lives under js/variants/D/**. The shared data layer
// (js/core/*) is reused untouched.

import { mount } from './js/variants/D/app.js';

function ensureStylesheet() {
  const HREF = '/static/css/variants/D/tufte.css';
  if (document.querySelector(`link[href="${HREF}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = HREF;
  document.head.appendChild(link);
}

function boot() {
  ensureStylesheet();
  const root = document.getElementById('variant-root');
  if (!root) {
    // Honest failure: the orchestrator did not provide the mount point.
    console.error('variant-D: #variant-root not found; nothing to mount.');
    return;
  }
  mount(root);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}

export { boot };

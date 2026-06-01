// app_H.js — entry point for Variant H ("Atlas II").
//
// A round-3 convergence dashboard: E's flow refined to completion. The
// orchestrator wires the `?ui=H` flag (already in index.html), injects
// `#variant-root`, and loads this module as the page's module entry. This
// entry:
//   1. injects the variant's scoped stylesheet (the three-theme token system),
//   2. paints the whole Atlas II dashboard into #variant-root,
//   3. reuses the shared data layer (core/{api,sse,state}) untouched.
//
// Everything visual lives under js/variants/H/** + css/variants/H/**.

import { mountShell } from './js/variants/H/shell.js';

function ensureRoot() {
  let root = document.getElementById('variant-root');
  if (!root) {
    root = document.createElement('div');
    root.id = 'variant-root';
    document.body.appendChild(root);
  }
  return root;
}

function ensureStylesheet() {
  const id = 'atlas2-H-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/variants/H/atlas2.css', import.meta.url).href;
  document.head.appendChild(link);
}

export function boot() {
  ensureStylesheet();
  mountShell(ensureRoot());
}

if (typeof document !== 'undefined' && !globalThis.__ATLAS_H_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

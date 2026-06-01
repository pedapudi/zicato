// app_K.js — entry point for Variant K ("Monograph").
//
// One of the round-3 convergence dashboards. Report-first: the ACM-style
// epoch publication IS the home, with live Tufte figures embedded inline and
// drill-down into the live dashboard. The orchestrator wires the `?ui=K`
// flag (already in index.html), injects `#variant-root`, and loads this
// module as the page's module entry. This entry:
//   1. injects the variant's scoped stylesheet (self-contained — it does not
//      depend on index.html linking it),
//   2. paints the whole Monograph dashboard into #variant-root,
//   3. reuses the shared data layer (core/{api,sse,state}) untouched — the
//      shell opens the SSE stream and does the first environment read.
//
// Everything visual lives under js/variants/K/** + css/variants/K/**.

import { mountShell } from './js/variants/K/shell.js';

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
  const id = 'monograph-K-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/variants/K/monograph.css', import.meta.url).href;
  document.head.appendChild(link);
}

export function boot() {
  ensureStylesheet();
  mountShell(ensureRoot());
}

if (typeof document !== 'undefined' && !globalThis.__MONOGRAPH_K_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

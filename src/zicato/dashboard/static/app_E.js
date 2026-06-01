// app_E.js — entry point for Variant E ("Atlas").
//
// One of the round-2 synthesis dashboards. The orchestrator wires the
// `?ui=E` flag (already in index.html), injects `#variant-root`, and loads
// this module as the page's module entry. This entry:
//   1. injects the variant's scoped stylesheet (so the variant is
//      self-contained and does not depend on index.html linking it),
//   2. paints the whole Atlas dashboard into #variant-root,
//   3. reuses the shared data layer (core/{api,sse,state}) untouched — the
//      shell opens the SSE stream and does the first environment read.
//
// Everything visual lives under js/variants/E/** + css/variants/E/**.

import { mountShell } from './js/variants/E/shell.js';

function ensureRoot() {
  let root = document.getElementById('variant-root');
  if (!root) {
    // Standalone / dev fallback if the orchestrator did not inject it.
    root = document.createElement('div');
    root.id = 'variant-root';
    document.body.appendChild(root);
  }
  return root;
}

function ensureStylesheet() {
  const id = 'atlas-E-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  // Resolve relative to this module's URL so the sheet loads whether served
  // at `/` or under `/static/`.
  link.href = new URL('./css/variants/E/atlas.css', import.meta.url).href;
  document.head.appendChild(link);
}

export function boot() {
  ensureStylesheet();
  mountShell(ensureRoot());
}

if (typeof document !== 'undefined' && !globalThis.__ATLAS_E_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

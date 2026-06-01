// app_G.js — entry point for Variant G ("Bridge").
//
// The synthesis the orchestrator wires under `?ui=G`: A's navigation/IA
// and Fleet home, D's data-viz, C's diagrams, B/D's calmer theming, with
// the four A bugs fixed via the render discipline in js/variants/G/**.
// This entry:
//   1. paints the variant into #variant-root,
//   2. reuses the shared data layer (core/{api,sse,state}) — it does NOT
//      rebuild the data layer or touch v2,
//   3. opens the SSE stream and does the first environment read.

import { connectSSE } from './js/core/sse.js';
import { loadEnvironment, loadServiceIdentity } from './js/core/api.js';
import { mountShell } from './js/variants/G/shell.js';

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
  const id = 'mcG-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/variants/G/bridge.css', import.meta.url).href;
  document.head.appendChild(link);
}

export function boot() {
  ensureStylesheet();
  const root = ensureRoot();
  mountShell(root);
  loadEnvironment();
  loadServiceIdentity();
  connectSSE();
}

if (typeof document !== 'undefined' && !globalThis.__MC_G_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

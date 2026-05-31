// app_A.js — entry point for Variant A ("Mission Control").
//
// One of four parallel dashboard explorations. The orchestrator wires
// the `?ui=A` flag, injects `#variant-root`, links this CSS, and loads
// this module at integration. This entry:
//   1. paints the whole variant into #variant-root,
//   2. reuses the shared data layer (core/{api,sse,state}) — it does NOT
//      rebuild the data layer or touch the v2 presentation,
//   3. opens the SSE stream and does the first environment read.
//
// Everything visual lives under js/variants/A/** + css/variants/A/**.

import { connectSSE } from './js/core/sse.js';
import { loadEnvironment, loadServiceIdentity } from './js/core/api.js';
import { mountShell } from './js/variants/A/shell.js';

function ensureRoot() {
  let root = document.getElementById('variant-root');
  if (!root) {
    // Standalone / dev fallback: if the orchestrator has not injected
    // #variant-root (e.g. opened directly), create one on <body> so the
    // variant is viewable on its own.
    root = document.createElement('div');
    root.id = 'variant-root';
    document.body.appendChild(root);
  }
  return root;
}

function ensureStylesheet() {
  // In standalone/dev the CSS may not be linked by the host; link it.
  const id = 'mcA-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  // Resolve relative to this module's URL so the sheet loads whether the
  // bundle is served at `/` or under `/static/` (both are mounted by the
  // dashboard service; a plain dev file server only has `/`).
  link.href = new URL('./css/variants/A/mission-control.css', import.meta.url).href;
  document.head.appendChild(link);
}

export function boot() {
  ensureStylesheet();
  const root = ensureRoot();
  mountShell(root);
  // Shared data layer — first read, service identity, then live stream.
  loadEnvironment();
  loadServiceIdentity();
  connectSSE();
}

if (typeof document !== 'undefined' && !globalThis.__MC_A_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

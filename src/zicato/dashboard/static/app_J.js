// app_J.js — entry point for Variant J ("Console").
//
// A round-3 convergence dashboard. The orchestrator wires the `?ui=J` flag
// (already in index.html), injects `#variant-root`, and loads this module as
// the page's module entry. This entry:
//   1. injects the variant's scoped stylesheet (so the variant is
//      self-contained and does not depend on index.html linking it),
//   2. paints the whole Console dashboard into #variant-root,
//   3. reuses the shared data layer (core/{api,sse,state}) untouched.
//
// Everything visual lives under js/variants/J/** + css/variants/J/**.

import { mountShell } from './js/variants/J/shell.js';

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
  const id = 'console-J-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/variants/J/console.css', import.meta.url).href;
  document.head.appendChild(link);
}

export function boot() {
  ensureStylesheet();
  mountShell(ensureRoot());
}

if (typeof document !== 'undefined' && !globalThis.__CONSOLE_J_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

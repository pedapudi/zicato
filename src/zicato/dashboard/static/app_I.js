// app_I.js — entry point for Variant I ("Ledger").
//
// One of the round-3 convergence dashboards (built on Variant E's flow:
// editorial, light-first, publication-leaning). The orchestrator wires the
// `?ui=I` flag (already in index.html), injects `#variant-root`, and loads
// this module as the page's module entry. This entry:
//   1. injects the variant's scoped stylesheet (so the variant is
//      self-contained and does not depend on index.html linking it),
//   2. paints the whole Ledger dashboard into #variant-root,
//   3. reuses the shared data layer (core/{api,sse,state}) untouched — the
//      shell opens the SSE stream and does the first environment read,
//      initialises the three-theme system (solarized-light default), and
//      dispatches the editorial views (incl. the new Mutations + ACM
//      Publication tabs).
//
// Everything visual lives under js/variants/I/** + css/variants/I/**.

import { mountShell } from './js/variants/I/shell.js';

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
  const id = 'ledger-I-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/variants/I/ledger.css', import.meta.url).href;
  document.head.appendChild(link);
}

export function boot() {
  ensureStylesheet();
  mountShell(ensureRoot());
}

if (typeof document !== 'undefined' && !globalThis.__LEDGER_I_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

// app_M.js — entry point for Variant M ("Ledger II").
//
// Round-4 convergence-II dashboard. Built on Variant E's flow (dashboard-
// first), wearing an editorial publication skin that refines Variant I
// ("Ledger"): light-first, airy whitespace, a serif display voice, and the
// ACM-style epoch PUBLICATION as a prominent first-class tab (reusing K's
// paper renderer — judged the best of the round). The orchestrator wires the
// `?ui=M` flag (already in index.html), injects `#variant-root`, and loads
// this module as the page's module entry. This entry:
//   1. injects the variant's scoped stylesheet (so the variant is
//      self-contained and does not depend on index.html linking it),
//   2. injects the Google-Fonts stylesheet (the ONLY permitted external
//      dependency — fonts only, with system fallbacks + font-display:swap),
//   3. paints the whole Ledger-II dashboard into #variant-root,
//   4. reuses the shared data layer (core/{api,sse,state}) untouched.
//
// Everything visual lives under js/variants/M/** + css/variants/M/**.

import { mountShell } from './js/variants/M/shell.js';

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
  const id = 'ledger-M-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/variants/M/ledger2.css', import.meta.url).href;
  document.head.appendChild(link);
}

// The typeface picker is built on Open-Sans pairings served by Google Fonts.
// Loading them via a stylesheet <link> to fonts.googleapis.com is the ONLY
// permitted external dependency (the operator explicitly requested Google
// Fonts). System-font fallbacks + font-display:swap are baked into ledger2.css
// so the dashboard renders immediately and never blocks on the network.
function ensureFonts() {
  const id = 'ledger-M-fonts';
  if (document.getElementById(id)) return;
  // Preconnect first (perf), then the families the four typeface themes use:
  // Open Sans (body, all themes) + Source Serif 4 (Editorial) + JetBrains
  // Mono (Technical) + Archivo Narrow (Display).
  const pre1 = document.createElement('link');
  pre1.rel = 'preconnect'; pre1.href = 'https://fonts.googleapis.com';
  const pre2 = document.createElement('link');
  pre2.rel = 'preconnect'; pre2.href = 'https://fonts.gstatic.com'; pre2.crossOrigin = 'anonymous';
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = 'https://fonts.googleapis.com/css2'
    + '?family=Open+Sans:ital,wght@0,400;0,600;0,700;1,400'
    + '&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;0,8..60,700;1,8..60,400'
    + '&family=JetBrains+Mono:wght@400;600'
    + '&family=Archivo+Narrow:wght@500;600;700'
    + '&display=swap';
  document.head.appendChild(pre1);
  document.head.appendChild(pre2);
  document.head.appendChild(link);
}

export function boot() {
  ensureStylesheet();
  ensureFonts();
  mountShell(ensureRoot());
}

if (typeof document !== 'undefined' && !globalThis.__LEDGER_M_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

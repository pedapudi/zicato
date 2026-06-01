// app_O.js — entry point for Variant O ("Compass").
//
// One of the round-4 convergence-II dashboards: a master-detail two-pane
// workspace — a persistent epoch→generation→board selector RAIL + a detail
// PANE that follows the explicit, persistent selection. The orchestrator
// wires the `?ui=O` flag (already in index.html), injects `#variant-root`,
// and loads this module as the page's module entry. This entry:
//   1. injects the Google-Fonts <link> (the ONLY permitted external
//      dependency — fonts only, with system fallbacks + font-display:swap),
//   2. injects the variant's scoped stylesheet (self-contained — it does
//      not depend on index.html linking it),
//   3. paints the whole Compass dashboard into #variant-root,
//   4. reuses the shared data layer (core/{api,sse,state}) untouched — the
//      shell opens the SSE stream and does the first environment read.
//
// Everything visual lives under js/variants/O/** + css/variants/O/**.

import { mountShell } from './js/variants/O/shell.js';

function ensureRoot() {
  let root = document.getElementById('variant-root');
  if (!root) {
    root = document.createElement('div');
    root.id = 'variant-root';
    document.body.appendChild(root);
  }
  return root;
}

// Google Fonts (fonts ONLY — the operator explicitly requested them; this
// is the single permitted external dependency). The typeface picker swaps
// between Open-Sans-based pairings; we load every face this variant can
// select up front, all with `display=swap`, so a switch is instant and a
// slow/blocked fetch degrades to the system fallbacks in the CSS.
function ensureFonts() {
  const id = 'compass-O-fonts';
  if (document.getElementById(id)) return;
  // Preconnect for a faster first paint.
  for (const [rel, hrefVal, cross] of [
    ['preconnect', 'https://fonts.googleapis.com', false],
    ['preconnect', 'https://fonts.gstatic.com', true],
  ]) {
    const l = document.createElement('link');
    l.rel = rel; l.href = hrefVal;
    if (cross) l.crossOrigin = 'anonymous';
    document.head.appendChild(l);
  }
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = 'https://fonts.googleapis.com/css2'
    + '?family=Open+Sans:ital,wght@0,400;0,600;0,700;1,400'
    + '&family=Archivo+Narrow:wght@500;600;700'
    + '&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400'
    + '&family=JetBrains+Mono:wght@400;500;700'
    + '&display=swap';
  document.head.appendChild(link);
}

function ensureStylesheet() {
  const id = 'compass-O-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/variants/O/compass.css', import.meta.url).href;
  document.head.appendChild(link);
}

export function boot() {
  ensureFonts();
  ensureStylesheet();
  mountShell(ensureRoot());
}

if (typeof document !== 'undefined' && !globalThis.__COMPASS_O_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

// app_R.js — entry point for Variant R ("Strata").
//
// A round-5 convergence-III dashboard: the same data model + Variant N's
// data-ink content, navigated as macOS-Finder-style MILLER COLUMNS (cascading
// columns: Environment ▸ Epoch ▸ {Generations|Boards|Mutations|Publication} ▸
// item ▸ detail) rather than a nested accordion tree. Solarized-Dark default +
// Display typeface. The orchestrator wires `?ui=R` in index.html; this entry:
//   1. injects the variant's scoped stylesheet (self-contained),
//   2. injects the Google Fonts link (the ONLY permitted external dependency —
//      fonts only, with system fallbacks + font-display: swap),
//   3. paints the dashboard into #variant-root,
//   4. reuses the shared data layer (core/{api,sse,state}) untouched.
//
// Everything visual lives under js/variants/R/** + css/variants/R/**.

import { mountShell } from './js/variants/R/shell.js';

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
  const id = 'strata-R-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/variants/R/strata.css', import.meta.url).href;
  document.head.appendChild(link);
}

// The ONLY external dependency the brief permits: Google Fonts (fonts only).
// Open Sans (body, all themes) + Source Serif 4 (Editorial) + JetBrains Mono
// (Technical) + Archivo Narrow (Display, R's default). `display=swap` so a
// slow font never blocks paint; system fallbacks live in the stylesheet.
function ensureFonts() {
  const id = 'strata-R-fonts';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = 'https://fonts.googleapis.com/css2'
    + '?family=Open+Sans:ital,wght@0,400;0,600;0,700;1,400'
    + '&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400'
    + '&family=JetBrains+Mono:wght@400;500;700'
    + '&family=Archivo+Narrow:wght@500;600;700'
    + '&display=swap';
  document.head.appendChild(link);
}

export function boot() {
  ensureFonts();
  ensureStylesheet();
  mountShell(ensureRoot());
}

if (typeof document !== 'undefined' && !globalThis.__STRATA_R_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

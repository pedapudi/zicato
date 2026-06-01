// app_L.js — entry point for Variant L ("Atlas III").
//
// The main-line convergence-II dashboard. The orchestrator wires the
// `?ui=L` flag (already in index.html), injects `#variant-root`, and loads
// this module as the page's module entry. This entry:
//   1. injects the variant's scoped stylesheet (so the variant is
//      self-contained and does not depend on index.html linking it),
//   2. injects a Google-Fonts <link> for the Open-Sans-based typeface
//      pairings (the ONLY permitted external dependency — fonts only,
//      with system fallbacks + font-display:swap baked into the sheet),
//   3. paints the whole Atlas III dashboard into #variant-root,
//   4. reuses the shared data layer (core/{api,sse,state}) untouched.
//
// Everything visual lives under js/variants/L/** + css/variants/L/**.

import { mountShell } from './js/variants/L/shell.js';

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
  const id = 'atlas-L-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/variants/L/atlas.css', import.meta.url).href;
  document.head.appendChild(link);
}

// Google Fonts — the single permitted external dependency, and ONLY for
// fonts. We request every face the typeface picker can select (Open Sans,
// Source Serif 4, JetBrains Mono, Archivo Narrow) with `display=swap` so
// text paints immediately in the system fallback and re-flows when the web
// font arrives. Each typeface theme just re-maps the --l-* family tokens.
function ensureFonts() {
  const id = 'atlas-L-fonts';
  if (document.getElementById(id)) return;
  // preconnect for a faster first byte (best-effort; harmless if unused).
  const pre1 = document.createElement('link');
  pre1.rel = 'preconnect'; pre1.href = 'https://fonts.googleapis.com';
  const pre2 = document.createElement('link');
  pre2.rel = 'preconnect'; pre2.href = 'https://fonts.gstatic.com'; pre2.crossOrigin = 'anonymous';
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = 'https://fonts.googleapis.com/css2'
    + '?family=Open+Sans:ital,wght@0,400;0,600;0,700;1,400'
    + '&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600;8..60,700'
    + '&family=JetBrains+Mono:wght@400;500;700'
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

if (typeof document !== 'undefined' && !globalThis.__ATLAS_L_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

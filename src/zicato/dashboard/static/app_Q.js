// app_Q.js — entry point for Variant Q ("Atlas IV").
//
// A round-5 convergence-III dashboard: the roomy, comfortable take. N's content
// + diagrams with M's generous spacing/proportion and typographic comfort, L's
// mutation-viewer quality, on a persistent data-model TREE sidebar. Solarized-
// Dark default + Sans typeface. The orchestrator wires `?ui=Q` in index.html;
// this entry:
//   1. injects the variant's scoped stylesheet (self-contained),
//   2. injects the Google Fonts link (the ONLY permitted external dependency —
//      fonts only, with system fallbacks + font-display: swap),
//   3. paints the dashboard into #variant-root,
//   4. reuses the shared data layer (core/{api,sse,state}) untouched.
//
// Everything visual lives under js/variants/Q/** + css/variants/Q/**.

import { mountShell } from './js/variants/Q/shell.js';

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
  const id = 'atlas4-Q-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/variants/Q/atlas4.css', import.meta.url).href;
  document.head.appendChild(link);
}

// The ONLY external dependency the brief permits: Google Fonts (fonts only).
// Open Sans (body, all themes) + Source Serif 4 (Editorial) + JetBrains Mono
// (Technical) + Archivo Narrow (Display). `display=swap` so a slow font never
// blocks paint; system fallbacks live in the stylesheet. Q defaults Sans.
function ensureFonts() {
  const id = 'atlas4-Q-fonts';
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

if (typeof document !== 'undefined' && !globalThis.__ATLAS_Q_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

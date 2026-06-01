// app_U.js — entry point for Variant U ("Atlas V").
//
// Round-6 convergence-IV dashboard: the COMFORTABLE sibling of the anchor.
// Same P+S+Q synthesis as Variant T — a persistent data-model TREE sidebar
// drives a detail pane with FIRST-CLASS SIDE-BY-SIDE COMPARISON (two
// candidates' lifecycle / promote gate / match-ups / per-board scoring, and
// two candidates' transcripts on a board shown INLINE, side by side) — but
// rendered ROOMY and LIGHT: Q/M-forward generous spacing and proportion,
// Solarized-Light default colour + Sans default typeface. A calm, airy
// alternative to T's density. A FIXED back/up control (top-left) navigates UP
// the selection hierarchy and renders into the MAIN detail pane (never the
// sidebar — Q's bug). The orchestrator wires `?ui=U` in index.html; this entry:
//   1. injects the variant's scoped stylesheet (self-contained),
//   2. injects the Google Fonts link (the ONLY permitted external dependency —
//      fonts only, with system fallbacks + font-display: swap),
//   3. paints the dashboard into #variant-root,
//   4. reuses the shared data layer (core/{api,sse,state}) untouched.
//
// Everything visual lives under js/variants/U/** + css/variants/U/**.

import { mountShell } from './js/variants/U/shell.js';

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
  const id = 'atlasv-U-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/variants/U/atlasv.css', import.meta.url).href;
  document.head.appendChild(link);
}

// The ONLY external dependency the brief permits: Google Fonts (fonts only).
// Open Sans (body, all themes + U's default Sans voice) + Source Serif 4
// (Editorial) + JetBrains Mono (Technical / data) + Archivo Narrow (Display).
// `display=swap` so a slow font never blocks paint; system fallbacks live in
// the stylesheet. This literal lives ONLY in this root-level entry point (never
// under js/**) so the no-external-fetch bundle guard never trips on it.
function ensureFonts() {
  const id = 'atlasv-U-fonts';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = 'https://fonts.googleapis.com/css2'
    + '?family=Open+Sans:ital,wght@0,400;0,600;0,700;1,400'
    + '&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;0,8..60,700;1,8..60,400'
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

if (typeof document !== 'undefined' && !globalThis.__ATLASV_U_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

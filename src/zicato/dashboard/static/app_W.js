// app_W.js — entry point for Variant W ("Arena").
//
// The round-6 convergence-IV CREATIVE broadcast take: the tournament as a live
// STANDINGS / leaderboard + MATCH CARDS. It keeps Console III's (Variant P)
// data-model TREE sidebar + detail views, folds in S's side-by-side comparison,
// and adds a fixed back/up control that renders into the MAIN detail pane.
// Energetic but legible: Monokai default colour theme (Solarized-Dark the calm
// alternate) + Display typeface (Archivo Narrow headings / big numbers — the
// billboard voice). FIT-TO-WIDTH everywhere; NO pan/zoom viewport.
//
// The orchestrator wires `?ui=W` in index.html; this entry:
//   1. injects the variant's scoped stylesheet (self-contained),
//   2. injects the Google Fonts link (the ONLY permitted external dependency —
//      fonts only, with system fallbacks + font-display: swap),
//   3. paints the dashboard into #variant-root,
//   4. reuses the shared data layer (core/{api,sse,state}) untouched.
//
// Everything visual lives under js/variants/W/** + css/variants/W/**.

import { mountShell } from './js/variants/W/shell.js';

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
  const id = 'arena-W-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/variants/W/arena.css', import.meta.url).href;
  document.head.appendChild(link);
}

// The ONLY external dependency the brief permits: Google Fonts (fonts only).
// Open Sans (body, all themes) + Source Serif 4 (Editorial) + JetBrains Mono
// (Technical) + Archivo Narrow (Display, W's default — billboard headings).
// `display=swap` so a slow font never blocks paint; system fallbacks live in
// the stylesheet.
function ensureFonts() {
  const id = 'arena-W-fonts';
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

if (typeof document !== 'undefined' && !globalThis.__ARENA_W_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

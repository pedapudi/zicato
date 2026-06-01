// app_V.js — entry point for Variant V ("Reel").
//
// The round-6 convergence-IV CREATIVE-temporal take: the epoch as a horizontal
// REEL — a timeline / playback of the rounds. The champion spine runs across
// the top with challengers entering over time (ordered by ran_at); a
// scrubber/stepper moves along the rounds and selecting a station opens that
// round's match-up + promote gate + the challenger's lifecycle in the detail
// pane. Built on the Console III anchor (P): the data-model TREE sidebar stays
// (collapsible) for full-fidelity navigation, S's side-by-side compare is
// folded into the candidate detail, and the back/up control renders into the
// MAIN pane. Solarized-Dark default colour + Display typeface.
//
// The orchestrator wires `?ui=V` in index.html; this entry:
//   1. injects the variant's scoped stylesheet (self-contained),
//   2. injects the Google Fonts link (the ONLY permitted external dependency —
//      fonts only, with system fallbacks + font-display: swap),
//   3. paints the dashboard into #variant-root,
//   4. reuses the shared data layer (core/{api,sse,state}) untouched.
//
// Everything visual lives under js/variants/V/** + css/variants/V/**.

import { mountShell } from './js/variants/V/shell.js';

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
  const id = 'reel-V-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/variants/V/reel.css', import.meta.url).href;
  document.head.appendChild(link);
}

// The ONLY external dependency the brief permits: Google Fonts (fonts only).
// Open Sans (body, all typefaces) + Archivo Narrow (Display, V's default) +
// Source Serif 4 (Editorial) + JetBrains Mono (Technical). `display=swap` so a
// slow font never blocks paint; system fallbacks live in the stylesheet.
function ensureFonts() {
  const id = 'reel-V-fonts';
  if (document.getElementById(id)) return;
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

export function boot() {
  ensureFonts();
  ensureStylesheet();
  mountShell(ensureRoot());
}

if (typeof document !== 'undefined' && !globalThis.__REEL_V_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

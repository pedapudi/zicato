// app_T.js — entry point for Variant T ("Console IV").
//
// The round-6 convergence-IV ANCHOR: Variant P ("Console III", judged the
// best-looking console) with three folds — S's first-class side-by-side
// COMPARE detail, Q's generous proportional spacing, and a working back/up
// button (top-left) that renders the destination into the MAIN detail pane.
// Dense, data-ink-maximal; Monokai default colour theme + Technical typeface. A
// persistent collapsible nested tree (Environment → Epoch → {Generations|
// Boards|Mutation surface|Publication}) drives a single detail pane. The
// orchestrator wires `?ui=T` in index.html; this entry:
//   1. injects the variant's scoped stylesheet (self-contained), which itself
//      @font-faces the SELF-HOSTED monospace woff2 (iA Writer Mono + JetBrains
//      Mono) the default Technical mode uses,
//   2. injects the Google Fonts link for the NON-self-hosted families that
//      Editorial (serif) and Display (geometric/condensed) read (fonts only,
//      with system fallbacks + font-display: swap),
//   3. paints the dashboard into #variant-root,
//   4. reuses the shared data layer (core/{api,sse,state}) untouched.
//
// Everything visual lives under js/variants/T/** + css/variants/T/**.

import { mountShell } from './js/variants/T/shell.js';

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
  const id = 'console4-T-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/variants/T/console4.css', import.meta.url).href;
  document.head.appendChild(link);
}

// FONTS — a SPLIT loading strategy:
//   * Technical (DEFAULT) is all-monospace: iA Writer Mono (prose body) +
//     JetBrains Mono (code/data). Both are SELF-HOSTED woff2 under static/fonts/
//     via @font-face in console4.css, so the default mode never touches a CDN.
//   * Editorial (serif) + Display (geometric/condensed) read families that are
//     NOT self-hosted — Source Serif 4, Space Grotesk, Archivo Narrow (Open Sans
//     as a display fallback) — loaded from the ONLY permitted external dependency:
//     Google Fonts (fonts only). `display=swap` so a slow font never blocks paint;
//     system fallbacks live in the stylesheet.
function ensureFonts() {
  const id = 'console4-T-fonts';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = 'https://fonts.googleapis.com/css2'
    + '?family=Open+Sans:ital,wght@0,400;0,600;0,700;1,400'
    + '&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400'
    + '&family=Space+Grotesk:wght@400;500;700'
    + '&family=Archivo+Narrow:wght@500;600;700'
    + '&display=swap';
  document.head.appendChild(link);
}

export function boot() {
  ensureFonts();
  ensureStylesheet();
  mountShell(ensureRoot());
}

if (typeof document !== 'undefined' && !globalThis.__CONSOLE_T_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

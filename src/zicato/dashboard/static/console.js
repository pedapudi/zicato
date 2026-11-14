// console.js — the console's entry point.
//
// The layout is dense and data-ink-maximal: a persistent collapsible nested
// tree (Environment → Epoch → {Generations | Boards | Mutation surface |
// Publication}) drives a single detail pane, with a first-class side-by-side
// COMPARE detail, generous proportional spacing, and a top-left up button that
// renders the destination into the MAIN detail pane. It defaults to the Monokai
// colour theme and the Open Sans + Google Sans Mono typeface pairing. index.html
// loads this entry, which:
//   1. injects the console's scoped stylesheet (self-contained), which itself
//      @font-faces the SELF-HOSTED monospace woff2 (JetBrains Mono),
//   2. injects the Google Fonts link for the interface faces
//      (fonts only, with system fallbacks + font-display: swap),
//   3. paints the dashboard into #console-root,
//   4. reuses the shared data layer (core/{api,sse,state}) untouched.
//
// Everything visual lives under js/** + css/**.

import { mountShell } from './js/shell.js';

function ensureRoot() {
  let root = document.getElementById('console-root');
  if (!root) {
    root = document.createElement('div');
    root.id = 'console-root';
    document.body.appendChild(root);
  }
  return root;
}

function ensureStylesheet() {
  const id = 'console-stylesheet';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = new URL('./css/console.css', import.meta.url).href;
  document.head.appendChild(link);
}

// FONTS — a SPLIT loading strategy:
//   * JetBrains Mono stays SELF-HOSTED woff2 under static/fonts/ via
//     @font-face in console.css. It backs the fixed brand mono, so the brand
//     never touches a CDN.
//   * The interface pairing — Open Sans for prose, Google Sans Mono for data,
//     with Noto Sans Mono as the data fallback — is NOT self-hosted. It loads
//     from the ONLY permitted external dependency: Google Fonts (fonts only).
//     `display=swap` so a slow font never blocks paint; system fallbacks live
//     in the stylesheet. A preconnect to the Google-Fonts origins shaves the
//     connection-setup latency.
function ensureFonts() {
  const id = 'console-fonts';
  if (document.getElementById(id)) return;
  // preconnect to the Google-Fonts origins (CSS + the woff2 host) to cut the
  // connection-setup latency before the stylesheet request lands.
  const pre1 = document.createElement('link');
  pre1.id = 'console-fonts-preconnect-css';
  pre1.rel = 'preconnect';
  pre1.href = 'https://fonts.googleapis.com';
  document.head.appendChild(pre1);
  const pre2 = document.createElement('link');
  pre2.id = 'console-fonts-preconnect-files';
  pre2.rel = 'preconnect';
  pre2.href = 'https://fonts.gstatic.com';
  pre2.crossOrigin = 'anonymous';
  document.head.appendChild(pre2);

  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = 'https://fonts.googleapis.com/css2'
    + '?family=Open+Sans:ital,wght@0,400;0,600;0,700;1,400'
    + '&family=Google+Sans+Mono:wght@400;500;700'
    + '&family=Noto+Sans+Mono:wght@400;500;700'
    + '&display=swap';
  document.head.appendChild(link);
}

export function boot() {
  ensureFonts();
  ensureStylesheet();
  mountShell(ensureRoot());
}

if (typeof document !== 'undefined' && !globalThis.__CONSOLE_NO_AUTOBOOT__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}

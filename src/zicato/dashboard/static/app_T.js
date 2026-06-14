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
// Everything visual lives under js/** + css/**.

import { mountShell } from './js/shell.js';

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
  link.href = new URL('./css/console.css', import.meta.url).href;
  document.head.appendChild(link);
}

// FONTS — a SPLIT loading strategy:
//   * The two self-hosted monos — iA Writer Mono + JetBrains Mono — stay SELF-
//     HOSTED woff2 under static/fonts/ via @font-face in console.css (JetBrains
//     Mono still backs the fixed brand mono), so the brand never touches a CDN.
//   * The TYPEFACE PICKER's finalized 12 faces (4 per mode) read families that
//     are NOT self-hosted. They load from the ONLY permitted external dependency:
//     Google Fonts (fonts only). `display=swap` so a slow font never blocks paint;
//     system fallbacks live in the stylesheet. A preconnect to the Google-Fonts
//     origins shaves the connection-setup latency. The families requested cover
//     every face the 12 options reference:
//       Technical — Google Sans Mono, Noto Sans Mono, Source Sans 3,
//                   Source Code Pro, Inconsolata, Ubuntu, Ubuntu Mono
//       Editorial — Fraunces, Bitter, Literata, Domine
//       Display   — Archivo Narrow, Space Grotesk, Hanken Grotesk,
//                   Barlow Condensed, Bricolage Grotesque
//     (Open Sans stays for legacy display fallbacks already in the stylesheet.)
function ensureFonts() {
  const id = 'console4-T-fonts';
  if (document.getElementById(id)) return;
  // preconnect to the Google-Fonts origins (CSS + the woff2 host) to cut the
  // connection-setup latency before the stylesheet request lands.
  const pre1 = document.createElement('link');
  pre1.id = 'console4-T-fonts-pre1';
  pre1.rel = 'preconnect';
  pre1.href = 'https://fonts.googleapis.com';
  document.head.appendChild(pre1);
  const pre2 = document.createElement('link');
  pre2.id = 'console4-T-fonts-pre2';
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
    + '&family=Source+Sans+3:wght@400;500;700'
    + '&family=Source+Code+Pro:wght@400;500;700'
    + '&family=Inconsolata:wght@400;500;700'
    + '&family=Ubuntu:wght@400;500;700'
    + '&family=Ubuntu+Mono:wght@400;700'
    + '&family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700'
    + '&family=Bitter:wght@400;500;700'
    + '&family=Literata:opsz,wght@7..72,400;7..72,500;7..72,700'
    + '&family=Domine:wght@400;500;700'
    + '&family=Archivo+Narrow:wght@400;500;700'
    + '&family=Space+Grotesk:wght@400;500;700'
    + '&family=Hanken+Grotesk:wght@400;500;700'
    + '&family=Barlow+Condensed:wght@400;500;700'
    + '&family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,700'
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

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
//      @font-faces the three SELF-HOSTED monospace woff2 under static/fonts/
//      (no external dependency — a blocked CDN can never affect the page),
//   2. paints the dashboard into #variant-root,
//   3. reuses the shared data layer (core/{api,sse,state}) untouched.
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

// FONTS: there is NO external font dependency. The three typeface options are
// all MONOSPACE and SELF-HOSTED as woff2 under static/fonts/ — JetBrains Mono
// (code), iA Writer Mono (prose/humanist), Space Mono (display) — declared via
// @font-face in console4.css (font-display: swap; broad system-mono fallbacks).
// Injecting the stylesheet therefore loads the faces; no CDN link is needed.

export function boot() {
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

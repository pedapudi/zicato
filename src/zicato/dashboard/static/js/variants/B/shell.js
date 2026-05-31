// variants/B/shell.js — the Editorial Lab Notebook shell.
//
// The shell is the magazine's masthead + page. Deliberately quiet chrome:
// a wordmark, a thin breadcrumb, a few nav links, a live pill that appears
// only when a run is in flight, and a light/dark/sepia toggle. Everything
// else is the page — generous whitespace, the view painted into a single
// content column.
//
// Re-render-safe: the masthead is built once (mount) and patched in place;
// digest gates keep a heartbeat tick from rewriting unchanged chrome. On a
// VIEW switch the content host is replaced with a fresh node so a returning
// view always paints (the "nav does nothing" guard the v2 shell documents).

import { el, mount, patchText, patchAttr, clearChildren } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bRouter, B_VIEW_LABELS, bHref, crumbTrail } from './router.js';

const ROOT_ID = 'variant-root';

// --- Theme (Variant B's own light-first palette set) ----------------------
export const THEME_KEY = 'zicato.B.theme';
export const B_THEMES = ['paper', 'ink', 'sepia'];
export const B_DEFAULT_THEME = 'paper';
const THEME_LABELS = { paper: 'Paper', ink: 'Ink', sepia: 'Sepia' };

function normalizeTheme(t) { return B_THEMES.includes(t) ? t : B_DEFAULT_THEME; }
export function readTheme() {
  try { return normalizeTheme(window.localStorage && window.localStorage.getItem(THEME_KEY)); }
  catch { return B_DEFAULT_THEME; }
}
export function applyTheme(theme) {
  const t = normalizeTheme(theme);
  const root = document.getElementById(ROOT_ID);
  if (root) root.setAttribute('data-vb-theme', t);
  if (document.documentElement && document.documentElement.setAttribute) {
    document.documentElement.setAttribute('data-vb-theme', t);
  }
  try { if (window.localStorage) window.localStorage.setItem(THEME_KEY, t); } catch { /* private */ }
  return t;
}

// --- View registry --------------------------------------------------------
const _views = new Map();
export function registerBView(name, fn) {
  if (typeof fn === 'function') _views.set(name, fn);
}

// Is a run live? Mirror the v2 liveness predicate without importing it
// (keep B self-contained): a non-null active tournament OR a heartbeat
// status that reads "running".
export function runIsLive() {
  if (state.activeTournament) return true;
  const hb = state.heartbeat;
  if (hb && typeof hb.status === 'string' && /run/i.test(hb.status)) return true;
  return false;
}

// --- Navigation anchors (route through the router on a plain click) -------
function navOnClick(navigate) {
  return (ev) => {
    if (ev && (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey
      || (ev.button != null && ev.button !== 0))) return;
    if (ev && ev.preventDefault) ev.preventDefault();
    navigate();
  };
}
function navLink(props, children, view, ...segs) {
  return el('a', {
    ...props, href: bHref(view, ...segs),
    onclick: navOnClick(() => bRouter.go(view, ...segs)),
  }, children);
}

const NAV_LINKS = [
  ['environment', 'Environment'],
  ['epoch', 'Epoch'],
  ['board', 'The Board'],
  ['tournament', 'Lineage'],
  ['bench', 'Bench'],
];

// --- Frame build (once) ---------------------------------------------------
function buildFrame(root) {
  const shell = el('div', { class: 'vb-shell' });

  const masthead = el('header', { class: 'vb-masthead' }, [
    el('div', { class: 'vb-masthead-inner' }, [
      navLink({ class: 'vb-wordmark', 'aria-label': 'zicato — environment' }, [
        el('span', { class: 'vb-wordmark-name' }, ['zicato']),
        el('span', { class: 'vb-wordmark-sub' }, ['lab notebook']),
      ], 'environment'),
      el('nav', { class: 'vb-nav', id: 'vb-nav', 'aria-label': 'Sections' },
        NAV_LINKS.map(([v, label]) => navLink(
          { class: 'vb-nav-link', 'data-view': v }, [label], v))),
      el('div', { class: 'vb-masthead-right' }, [
        el('a', {
          class: 'vb-live-pill', id: 'vb-live-pill', href: bHref('bench'),
          hidden: 'hidden', 'aria-label': 'A run is live — open the Bench',
          onclick: navOnClick(() => bRouter.go('bench')),
        }, [
          el('span', { class: 'vb-live-dot', 'aria-hidden': 'true' }),
          el('span', null, ['live']),
        ]),
        buildThemeToggle(),
      ]),
    ]),
    el('nav', { class: 'vb-crumbs', id: 'vb-crumbs', 'aria-label': 'Breadcrumb' }),
  ]);
  shell.appendChild(masthead);

  shell.appendChild(el('main', {
    class: 'vb-page', id: 'vb-page', role: 'main',
  }));

  shell.appendChild(el('footer', { class: 'vb-colophon', id: 'vb-colophon' }, [
    el('span', { class: 'vb-colophon-mark' }, ['zicato']),
    el('span', { class: 'vb-colophon-meta', id: 'vb-colophon-meta' }, ['—']),
  ]));

  root.appendChild(shell);
}

function buildThemeToggle() {
  const btns = B_THEMES.map((t) => el('button', {
    type: 'button', class: 'vb-theme-opt', 'data-theme': t, 'aria-label': THEME_LABELS[t],
    onclick: () => { applyTheme(t); syncTheme(); },
  }, [THEME_LABELS[t][0]]));
  return el('div', { class: 'vb-theme-toggle', id: 'vb-theme-toggle', role: 'group',
    'aria-label': 'Color theme' }, btns);
}

// --- Digest-gated paints --------------------------------------------------
let _lastCrumbDigest = null;
let _lastNavDigest = null;
let _lastLiveDigest = null;
let _lastThemeDigest = null;
let _lastViewKey = null;
let _lastColophon = null;

function syncTheme() {
  const cur = readTheme();
  if (cur === _lastThemeDigest) return;
  _lastThemeDigest = cur;
  const toggle = document.getElementById('vb-theme-toggle');
  if (!toggle) return;
  for (const b of toggle.children) {
    patchAttr(b, 'aria-pressed', b.getAttribute('data-theme') === cur ? 'true' : 'false');
  }
}

function renderNav(route) {
  const digest = route.view;
  if (digest === _lastNavDigest) return;
  _lastNavDigest = digest;
  const nav = document.getElementById('vb-nav');
  if (!nav) return;
  for (const a of nav.children) {
    patchAttr(a, 'aria-current', a.getAttribute('data-view') === route.view ? 'page' : null);
  }
}

function renderCrumbs(route) {
  const host = document.getElementById('vb-crumbs');
  if (!host) return;
  const trail = crumbTrail(route);
  const digest = JSON.stringify(trail.map((c) => [c.view, c.label, c.current, c.segs]));
  if (digest === _lastCrumbDigest) return;
  _lastCrumbDigest = digest;
  clearChildren(host);
  trail.forEach((c, i) => {
    if (i > 0) host.appendChild(el('span', { class: 'vb-crumb-sep', 'aria-hidden': 'true' }, ['/']));
    if (c.current) {
      host.appendChild(el('span', { class: 'vb-crumb vb-crumb-current', 'aria-current': 'page' }, [c.label]));
    } else {
      host.appendChild(navLink({ class: 'vb-crumb' }, [c.label], c.view, ...c.segs));
    }
  });
}

function renderLive(route) {
  const pill = document.getElementById('vb-live-pill');
  if (!pill) return;
  const live = runIsLive();
  const onBench = route.view === 'bench';
  const digest = (live ? '1' : '0') + (onBench ? 'b' : '');
  if (digest === _lastLiveDigest) return;
  _lastLiveDigest = digest;
  patchAttr(pill, 'hidden', (live && !onBench) ? null : 'hidden');
}

function renderColophon() {
  const host = document.getElementById('vb-colophon-meta');
  if (!host) return;
  const svc = state.service || {};
  const text = `dashboard ${svc.version || '—'} · port ${svc.port || '—'}`;
  if (text === _lastColophon) return;
  _lastColophon = text;
  patchText(host, text);
}

function renderView(route) {
  const host = document.getElementById('vb-page');
  if (!host) return;
  const viewKey = route.view + '|' + JSON.stringify(route.params || {});
  const fn = _views.get(route.view);
  const prevView = _lastViewKey == null ? null : String(_lastViewKey).split('|')[0];
  if (fn) {
    if (prevView !== route.view) clearChildren(host);
    fn(host, route);
    _lastViewKey = viewKey;
    return;
  }
  if (viewKey === _lastViewKey) return;
  _lastViewKey = viewKey;
  clearChildren(host);
  host.appendChild(el('h1', { class: 'vb-page-title' }, [B_VIEW_LABELS[route.view] || 'zicato']));
  host.appendChild(el('p', { class: 'vb-muted' }, ['This page is not yet wired.']));
}

// --- The full paint -------------------------------------------------------
export function renderBShell(route) {
  const root = document.getElementById(ROOT_ID);
  if (!root) return;
  applyTheme(readTheme());
  mount(root, 'vb-frame', () => {
    const wrap = el('div', { 'data-node': 'vb-frame' });
    buildFrame(wrap);
    return wrap;
  });
  const r = route || bRouter.current();
  renderNav(r);
  renderCrumbs(r);
  renderLive(r);
  syncTheme();
  renderColophon();
  renderView(r);
}

export function resetBShellDigest() {
  _lastCrumbDigest = null; _lastNavDigest = null; _lastLiveDigest = null;
  _lastThemeDigest = null; _lastViewKey = null; _lastColophon = null;
}

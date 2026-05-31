// variants/C/chrome.js — the variant-C shell chrome.
//
// A thin top nav (the five reachable screens), a connection/phase pill,
// and a right-hand DRAWER used by the epoch screen's proposer brief and
// by every "click a node → see detail" interaction. The drawer is a
// single reusable surface so node clicks across screens feel consistent.
//
// Chrome is built ONCE into #variant-root and then patched: the active
// nav item and the pill text update in place; the drawer's body is
// swapped per open. Nothing here rebuilds the diagram hosts.

import { el } from '../../core/dom.js';
import { href } from './router.js';
import { parseIso, nowMs } from '../../core/format.js';

const NAV = [
  { view: 'env', label: 'Environment', hint: 'the cross-epoch map' },
  { view: 'epoch', label: 'Epoch', hint: 'lineage + objective' },
  { view: 'experiment', label: 'Experiment', hint: 'causal flow' },
  { view: 'tournament', label: 'Tournament', hint: 'the gauntlet' },
  { view: 'run', label: 'Run', hint: 'one run' },
  { view: 'bench', label: 'Bench', hint: 'status' },
];

// Build the chrome skeleton. Returns the nodes the entry point needs:
//   { root, stage, drawer, drawerBody, setActive, setPill }
export function buildChrome() {
  const navItems = NAV.map((n) => el('a', {
    class: 'czc-nav-item',
    'data-view': n.view,
    href: navHref(n.view),
    title: n.hint,
  }, [n.label]));

  const pill = el('span', { class: 'czc-pill', 'data-state': 'connecting' }, ['connecting']);

  const nav = el('nav', { class: 'czc-nav', 'aria-label': 'Variant C navigation' }, [
    el('div', { class: 'czc-brand' }, [
      el('span', { class: 'czc-brand-mark', 'aria-hidden': 'true' }, ['◇']),
      el('span', { class: 'czc-brand-name' }, ['zicato']),
      el('span', { class: 'czc-brand-tag' }, ['causal flow']),
    ]),
    el('div', { class: 'czc-nav-items' }, navItems),
    el('div', { class: 'czc-nav-right' }, [pill]),
  ]);

  const stage = el('div', { class: 'czc-stage', id: 'czc-stage', role: 'main' });

  const drawerBody = el('div', { class: 'czc-drawer-body', id: 'czc-drawer-body' });
  const drawerTitle = el('h2', { class: 'czc-drawer-title', id: 'czc-drawer-title' }, ['Detail']);
  const drawer = el('aside', {
    class: 'czc-drawer', id: 'czc-drawer', 'aria-hidden': 'true',
    'aria-label': 'Detail drawer',
  }, [
    el('div', { class: 'czc-drawer-head' }, [
      drawerTitle,
      el('button', {
        type: 'button', class: 'czc-drawer-close', 'aria-label': 'Close detail',
        onclick: () => closeDrawer(drawer),
      }, ['✕']),
    ]),
    drawerBody,
  ]);

  const root = el('div', { class: 'czc-shell' }, [nav, stage, drawer]);

  function setActive(view) {
    for (const item of navItems) {
      const on = item.getAttribute('data-view') === view;
      if (on) item.classList.add('is-active'); else item.classList.remove('is-active');
      if (on) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current');
    }
  }

  function setPill(state) {
    const { label, kind } = pillModel(state);
    if (pill.textContent !== label) pill.textContent = label;
    if (pill.getAttribute('data-state') !== kind) pill.setAttribute('data-state', kind);
  }

  return { root, stage, drawer, drawerBody, drawerTitle, setActive, setPill };
}

// The nav routes to a context-aware href: the epoch / experiment /
// tournament / run items point at the *current* epoch/run when one is
// known (filled in by the entry point via data-href), else a bare view.
function navHref(view) {
  if (view === 'env' || view === 'bench') return href(view);
  // Placeholder; the entry point rewrites these per-render to carry the
  // resolved epoch / run so a top-nav click lands on real content.
  return href(view, {});
}

export function openDrawer(chrome, title, bodyNode) {
  if (!chrome || !chrome.drawer) return;
  chrome.drawerTitle.textContent = title || 'Detail';
  // Swap the body.
  while (chrome.drawerBody.firstChild) chrome.drawerBody.removeChild(chrome.drawerBody.firstChild);
  if (bodyNode) chrome.drawerBody.appendChild(bodyNode);
  chrome.drawer.setAttribute('aria-hidden', 'false');
  chrome.drawer.classList.add('is-open');
}

export function closeDrawer(drawer) {
  if (!drawer) return;
  drawer.setAttribute('aria-hidden', 'true');
  drawer.classList.remove('is-open');
}

// Map the app state onto a pill label + kind. Mirrors the shipped status
// pill logic at a high level (connecting / running / stalled / idle).
function pillModel(s) {
  if (!s) return { label: 'connecting', kind: 'connecting' };
  if (s.connecting && !s.connected) return { label: 'connecting', kind: 'connecting' };
  const hb = s.heartbeat || {};
  const phase = (hb.phase || '').toString().toUpperCase();
  const runs = Array.isArray(s.activeRuns) ? s.activeRuns.length : 0;
  // Staleness from heartbeat age.
  const beatMs = parseIso(hb.emitted_at || hb.ts || hb.updated_at);
  const ageS = isFinite(beatMs) ? (nowMs() - beatMs) / 1000 : null;
  if (ageS != null && ageS > 90) return { label: 'STALE', kind: 'stalled' };
  if (phase === 'PAUSED') return { label: 'PAUSED', kind: 'paused' };
  if (phase === 'STALLED') return { label: 'STALLED', kind: 'stalled' };
  if (runs > 0 || phase.includes('TOURNAMENT') || phase.includes('RUNNING')) {
    return { label: phase || 'RUNNING', kind: 'running' };
  }
  if (phase) return { label: phase, kind: 'idle' };
  if (s.connected) return { label: 'IDLE', kind: 'idle' };
  return { label: 'offline', kind: 'connecting' };
}

// Rewrite the context-sensitive nav hrefs once per render, given the
// resolved epoch / run. Keeps a top-nav click landing on real content
// instead of an empty id.
export function updateNavContext(chrome, ctx) {
  if (!chrome || !chrome.root) return;
  const items = chrome.root.querySelectorAll('[data-view]');
  for (const item of items) {
    const view = item.getAttribute('data-view');
    if (view === 'epoch' && ctx.epochId) item.setAttribute('href', href('epoch', { epochId: ctx.epochId }));
    else if (view === 'experiment' && ctx.epochId && ctx.genId) {
      item.setAttribute('href', href('experiment', { epochId: ctx.epochId, genId: ctx.genId }));
    } else if (view === 'tournament' && ctx.epochId) {
      item.setAttribute('href', href('tournament', { epochId: ctx.epochId }));
    } else if (view === 'run' && ctx.runId) {
      item.setAttribute('href', href('run', { runId: ctx.runId }));
    }
  }
}

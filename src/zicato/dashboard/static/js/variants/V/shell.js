// variants/V/shell.js — the "Reel" shell: a REEL hero (timeline / playback of
// the rounds) over a data-model TREE sidebar + one persistent detail pane.
//
// Variant V ("Reel") is the round-6 CREATIVE-temporal take. It keeps Console
// III's dense data-model TREE sidebar (Environment → Epoch → {Generations,
// Boards, Mutation surface, Publication}) AND adds a horizontal REEL above the
// detail pane: the rounds of the current epoch on a time axis — the champion
// spine with challengers entering over time, each a station carrying its
// verdict + Δscalar, with a scrubber/stepper. The reel is the HERO and doubles
// as navigation: selecting a station drives the detail pane to that round's
// challenger (its match-up + promote gate + lifecycle, via the candidate view).
//
// The shell owns:
//   * a COMPACT top bar — a back/up control (top-left, renders into the MAIN
//     detail pane — the round-6 fix to Q's back-button bug) · branding ·
//     breadcrumb · colour-theme picker (solarized-dark default) · typeface
//     picker (Display default) · status pill;
//   * the persistent collapsible tree sidebar (its own digest gate);
//   * a persistent REEL hero host (its own digest gate — STRUCTURAL only);
//   * ONE persistent detail host (never recreated per repaint);
//   * digest-gated dispatch — a heartbeat tick writes ZERO DOM anywhere.
//
// Theme + typeface are CSS-only swaps (data-v-theme / data-v-type on the root).

import { el, clearChildren, patchText, patchClass } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parseRoute, navigate, href, crumbTrail, upTarget } from './router.js';
import * as D from './data.js';
import { invalidateLive } from './data.js';
import { buildTree, treeDigest } from './tree.js';
import { normaliseDecision } from './ui.js';
import {
  COLOR_THEMES, DEFAULT_COLOR, normaliseColor, readColor, persistColor,
  TYPE_THEMES, DEFAULT_TYPE, normaliseType, readType, persistType,
} from './ui.js';
import { reel, reelDigest } from './reel.js';

import * as home from './views/home.js';
import * as epoch from './views/epoch.js';
import * as gens from './views/gens.js';
import * as candidate from './views/candidate.js';
import * as diff from './views/diff.js';
import * as boards from './views/boards.js';
import * as board from './views/board.js';
import * as mutations from './views/mutations.js';
import * as publication from './views/publication.js';

const RENDERERS = { home, epoch, gens, candidate, diff, boards, board, mutations, publication };

export const THEMES = COLOR_THEMES.map((t) => t[0]);
export const TYPEFACES = TYPE_THEMES.map((t) => t[0]);

const KIND_TAG = {
  single_turn: '1-turn', multi_turn_scripted: 'scripted', multi_turn_emulated: 'emulated',
};

let _root = null;
let _viewHost = null;
let _treeHost = null;
let _reelHost = null;
let _crumbHost = null;
let _backEl = null;
let _statusEl = null;
let _colorEl = [];
let _typeEl = [];
let _renderToken = 0;
let _lastViewKey = null;
let _lastCrumbDigest = null;
let _lastStatusDigest = null;
let _lastTreeDigest = null;
let _lastReelDigest = null;
const _toggles = new Set();
const _ctx = { navigate, href };

export function applyTheme(theme, rootEl) {
  const t = normaliseColor(theme);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-v-theme', t);
  persistColor(t);
  for (const b of _colorEl) patchClass(b, 'dp-theme-active', b.getAttribute('data-theme') === t);
  return t;
}

export function applyTypeface(typeface, rootEl) {
  const t = normaliseType(typeface);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-v-type', t);
  persistType(t);
  for (const b of _typeEl) patchClass(b, 'dp-type-active', b.getAttribute('data-type') === t);
  return t;
}

export function mountShell(root) {
  _root = root;
  clearChildren(root);
  _toggles.clear();
  _lastTreeDigest = null;
  _lastReelDigest = null;
  root.setAttribute('data-variant', 'V');
  root.setAttribute('data-v-theme', readColor());
  root.setAttribute('data-v-type', readType());

  // back/up control — navigates UP the selection hierarchy and renders the
  // destination into the MAIN detail pane (NEVER the sidebar — round-6 fix).
  _backEl = el('button', { class: 'vr-back', type: 'button', title: 'back / up one level', 'aria-label': 'Back — up one level' }, [
    el('span', { class: 'vr-back-glyph', 'aria-hidden': 'true', text: '↑' }),
    el('span', { class: 'vr-back-lab', text: 'up' }),
  ]);
  _backEl.addEventListener('click', () => goUp());

  _crumbHost = el('nav', { class: 'dp-crumbs', 'aria-label': 'Breadcrumb' });

  _colorEl = COLOR_THEMES.map(([id, label]) =>
    el('button', { class: 'dp-theme-btn', type: 'button', 'data-theme': id, title: 'colour: ' + id, text: label }));
  for (const b of _colorEl) b.addEventListener('click', () => applyTheme(b.getAttribute('data-theme')));
  const colorSwitch = el('div', { class: 'dp-theme-switch', role: 'group', 'aria-label': 'Colour theme' }, _colorEl);

  _typeEl = TYPE_THEMES.map(([id, label]) =>
    el('button', { class: 'dp-type-btn', type: 'button', 'data-type': id, title: 'typeface: ' + id, text: label }));
  for (const b of _typeEl) b.addEventListener('click', () => applyTypeface(b.getAttribute('data-type')));
  const typeSwitch = el('div', { class: 'dp-type-switch', role: 'group', 'aria-label': 'Typeface' }, _typeEl);

  _statusEl = el('span', { class: 'dp-status' }, [
    el('span', { class: 'dp-status-dot' }),
    el('span', { class: 'dp-status-text', text: 'connecting…' }),
  ]);

  const topbar = el('header', { class: 'dp-topbar' }, [
    _backEl,
    el('div', { class: 'dp-brand' }, [
      el('span', { class: 'dp-brand-name', text: 'zicato' }),
      el('span', { class: 'dp-brand-variant', text: 'reel' }),
    ]),
    _crumbHost,
    el('span', { class: 'dp-topbar-spacer' }),
    colorSwitch,
    typeSwitch,
    _statusEl,
  ]);
  root.appendChild(topbar);

  // the REEL hero — a persistent host spanning the full width above the body.
  _reelHost = el('section', { class: 'vr-hero', 'aria-label': 'Epoch reel' });
  root.appendChild(_reelHost);

  _treeHost = el('aside', { class: 'dp-sidebar', 'aria-label': 'Data model navigation' });
  _viewHost = el('main', { class: 'dp-viewhost', role: 'main' });
  root.appendChild(el('div', { class: 'dp-body' }, [_treeHost, _viewHost]));

  applyTheme(readColor());
  applyTypeface(readType());

  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/V')) location.hash = '#/V/';
  else dispatch();
}

// the back/up affordance: resolve the parent of the current selection and
// navigate there. The result paints into the MAIN detail pane via the normal
// dispatch path (the sidebar is untouched).
function goUp() {
  const route = parseRoute(location.hash);
  const up = upTarget(route);
  if (up) navigate(up.view, up.params);
}

// Assemble the tree's structural model (same shape Console III uses).
async function buildTreeModel() {
  const [ws, lin, ep] = await Promise.all([D.workspace(), D.lineage(), D.epoch()]);
  const epochs = [];
  const seen = new Set();
  const current = ws ? ws.current_epoch_id : (ep && ep.epoch_id) || null;
  if (ws && Array.isArray(ws.epochs)) {
    for (const e of ws.epochs) {
      if (e && e.epoch_id != null && !seen.has(e.epoch_id)) {
        seen.add(e.epoch_id);
        epochs.push({ id: e.epoch_id, current: e.epoch_id === current });
      }
    }
  }
  if (ep && ep.epoch_id != null && !seen.has(ep.epoch_id)) {
    epochs.push({ id: ep.epoch_id, current: ep.epoch_id === current });
  }
  const byEpoch = {};
  for (const e of epochs) byEpoch[e.id] = { gens: [], boards: [] };
  if (ep && ep.epoch_id != null) {
    const id = ep.epoch_id;
    const gensList = (lin && Array.isArray(lin.generations) && lin.generations.length)
      ? lin.generations
        .filter((g) => !g.epoch_id || g.epoch_id === id)
        .map((g) => ({ id: g.generation_id, promoted: !!g.promoted, parent: g.parent_generation_id || null }))
      : (Array.isArray(ep.experiments) ? ep.experiments.map((x) => ({
          id: x.generation_id, parent: x.parent_generation_id || null,
          promoted: normaliseDecision(x.outcome) === 'promoted',
        })) : []);
    const boardList = (Array.isArray(ep.board) ? ep.board : []).map((b) => ({
      id: b.entry_id || b.id, kindTag: KIND_TAG[b.kind] || null,
    })).filter((b) => b.id);
    byEpoch[id] = { gens: gensList, boards: boardList };
  }
  return { epochs, byEpoch, current };
}

async function renderTree(route) {
  if (!_treeHost) return;
  const model = await buildTreeModel();
  const digest = treeDigest(model, route, _toggles);
  if (digest === _lastTreeDigest && _treeHost.firstChild) return;
  _lastTreeDigest = digest;
  buildTree(_treeHost, model, route, _toggles, _ctx, (key) => {
    if (_toggles.has(key)) _toggles.delete(key); else _toggles.add(key);
    _lastTreeDigest = null;
    renderTree(parseRoute(location.hash));
  });
}

// Assemble the reel's round model from /api/tournaments (order by ran_at) and
// /api/lineage (the champion), then build the timeline hero. Digest-gated on
// STRUCTURE only (round ids / verdicts / Δ / selection — not ran_at noise).
async function renderReel(route) {
  if (!_reelHost) return;
  const p = (route && route.params) || {};
  // the reel is an epoch-scoped hero; hide it at the environment root.
  const epochScoped = !!p.epochId || (route && route.view !== 'home');
  if (!epochScoped) {
    if (_reelHost.getAttribute('data-v-reel') !== 'env') {
      _reelHost.setAttribute('data-v-reel', 'env');
      _lastReelDigest = null;
      clearChildren(_reelHost);
      _reelHost.style.display = 'none';
    }
    return;
  }
  _reelHost.style.display = '';

  const [lin, br] = await Promise.all([D.lineage(), D.bracket()]);
  const gens = (lin && Array.isArray(lin.generations)) ? lin.generations : [];
  const champion = gens.find((g) => g.promoted) || gens.find((g) => !g.parent_generation_id) || null;
  const championId = champion ? champion.generation_id : (br && Array.isArray(br.champion_lineage) && br.champion_lineage.length ? br.champion_lineage[0] : null);

  const matchups = (br && Array.isArray(br.matchups)) ? br.matchups.slice() : [];
  // round order via ran_at (fallback to recorded order).
  matchups.sort((a, b) => {
    const ta = a.ran_at ? Date.parse(a.ran_at) : NaN;
    const tb = b.ran_at ? Date.parse(b.ran_at) : NaN;
    if (isFinite(ta) && isFinite(tb)) return ta - tb;
    return 0;
  });
  const rounds = matchups.map((m) => ({
    challenger: m.challenger, decision: m.decision,
    deltaScalar: typeof m.delta_scalar === 'number' ? m.delta_scalar : NaN,
    hypothesis: m.hypothesis_core_idea || null,
  }));

  // the selected round = the candidate currently in the detail pane (a
  // challenger id), so the reel highlights the open round on a deep-link.
  const selected = (route && route.view === 'candidate' && p.gen) ? p.gen : (route && (route.view === 'diff' || route.view === 'board') && p.gen ? p.gen : null);

  const spec = {
    championId, rounds, selected,
    onSelect: (chall) => navigate('candidate', { epochId: p.epochId, gen: chall }),
    onSeed: (champ) => { if (champ) navigate('candidate', { epochId: p.epochId, gen: champ }); },
  };
  const digest = reelDigest(spec) + '|' + (p.epochId || '');
  if (digest === _lastReelDigest && _reelHost.firstChild) return;
  _lastReelDigest = digest;
  _reelHost.setAttribute('data-v-reel', 'epoch');
  clearChildren(_reelHost);
  _reelHost.appendChild(el('div', { class: 'vr-hero-head' }, [
    el('span', { class: 'vr-hero-title', text: 'reel' }),
    el('span', { class: 'vr-hero-sub dn-faint', text: p.epochId ? p.epochId + ' · rounds on a time axis' : 'rounds on a time axis' }),
  ]));
  _reelHost.appendChild(reel(spec));
}

function renderCrumbs(route) {
  if (!_crumbHost) return;
  const trail = crumbTrail(route);
  const digest = JSON.stringify(trail.map((c) => [c.label, c.view || '', c.current || false]));
  if (digest === _lastCrumbDigest && _crumbHost.firstChild) return;
  _lastCrumbDigest = digest;
  clearChildren(_crumbHost);
  trail.forEach((c, i) => {
    if (i > 0) _crumbHost.appendChild(el('span', { class: 'dp-crumb-sep', 'aria-hidden': 'true', text: '›' }));
    if (c.current || !c.view) {
      _crumbHost.appendChild(el('span', { class: 'dp-crumb dp-crumb-current', 'aria-current': 'page', text: c.label }));
    } else {
      _crumbHost.appendChild(el('a', { class: 'dp-crumb', href: href(c.view, c.params), text: c.label }));
    }
  });
  // disable the back control at the environment root (nothing above it).
  if (_backEl) {
    const atRoot = route.view === 'home';
    _backEl.disabled = atRoot ? true : false;
    patchClass(_backEl, 'vr-back-off', atRoot);
  }
}

function renderStatus() {
  if (!_statusEl) return;
  const conn = state.connected ? 'live' : state.connecting ? 'connecting…' : 'offline';
  const live = !!state.activeTournament;
  const digest = conn + '|' + (live ? 'L' : '');
  if (digest === _lastStatusDigest) return;
  _lastStatusDigest = digest;
  patchText(_statusEl.querySelector('.dp-status-text') || _statusEl, conn);
  patchClass(_statusEl, 'dp-connected', state.connected);
  patchClass(_statusEl, 'dp-running', live);
}

async function dispatch() {
  const route = parseRoute(location.hash);
  const renderer = RENDERERS[route.view] || RENDERERS.home;
  const viewKey = route.view + '|' + JSON.stringify(route.params || {}) + '|cmp=' + (route.cmp || '');

  renderCrumbs(route);
  renderStatus();
  renderTree(route);
  renderReel(route);

  const prevView = _lastViewKey == null ? null : String(_lastViewKey).split('|')[0];
  const prevKey = _lastViewKey;
  // Clear the host (and bust caches) on ANY selection change.
  if (prevKey !== viewKey) {
    clearChildren(_viewHost);
    if (prevView !== route.view) invalidateLive();
  }
  _lastViewKey = viewKey;

  const token = ++_renderToken;
  try {
    await renderer.render(_viewHost, _ctx, route.params, route);
  } catch (err) {
    if (token !== _renderToken) return;
    clearChildren(_viewHost);
    _viewHost.appendChild(el('p', { class: 'dp-empty', text: 'This view hit an error: ' + ((err && err.message) || err) }));
    // eslint-disable-next-line no-console
    console.error('variant-V render error', err);
  }
}

let _reRenderTimer = null;
function onStateChanged() {
  renderStatus();
  if (_reRenderTimer != null) return;
  _reRenderTimer = setTimeout(() => {
    _reRenderTimer = null;
    dispatch();
  }, 400);
}

export { DEFAULT_COLOR, DEFAULT_TYPE };

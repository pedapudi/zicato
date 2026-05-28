// views/phase0_sidebar_search.js — always-visible sidebar search bar.
//
// The sidebar exposes an `<input type="search">` and a results panel
// below it. Typing in the input debounces and fetches /api/search?q=...;
// the response shape (entries / judges / patches / mutations) is
// rendered as four section blocks of inline links. Empty / blank query
// hides the results panel; "no matches in this category" still renders
// the section header with a muted `(no matches)` line so the operator
// can see the query was scoped to every category.
//
// Navigation: each result is an `<a>` with a phase-0 hash href so the
// shell's existing hashchange listener handles the navigation. Clicking
// a result does NOT submit a form — the input only filters, never
// navigates on Enter (the user navigates via the results below).
//
// State: ``state.heartbeat.epoch_id`` is the only piece of app state
// this module reads — it is the bind point for entry → run links so a
// match goes to a useful location even when the entry has runs in
// multiple generations. Patch + mutation results carry their own
// (epoch_id, generation_id) so they navigate independently of the
// current epoch.

import { $, el, clearChildren } from '../core/dom.js';
import { state } from '../core/state.js';
import { phase0Href } from './phase0_router.js';

// 150ms is short enough to feel responsive while keystrokes
// coalesce into one fetch — a typed word that lands inside the debounce
// window costs one network round-trip rather than one per character.
const SEARCH_DEBOUNCE_MS = 150;

let _debounceTimer = null;
let _lastQuery = '';

// Module-level overrides for the fetch and timer primitives. Tests
// stash a stub here to drive the search bar without a real fetch or a
// real setTimeout. Production code never sets these — the defaults
// resolve to the browser globals at call time.
export const _testHooks = { fetch: null, scheduleTimer: null };

function _fetchSearch(query) {
  // The endpoint returns `_empty_search_result()` for a blank query, so
  // the dashboard does not have to guess what an empty body means — but
  // we still short-circuit blank queries client-side to avoid a no-op
  // round trip.
  const f = _testHooks.fetch || ((url) => fetch(url));
  return f('/api/search?q=' + encodeURIComponent(query)).then((res) => {
    if (res && typeof res.json === 'function') return res.json();
    return res;
  });
}

// Build the navigation href for an entry result. The entry lives in
// `current epoch`'s board; the natural detail location is its run inside
// the currently-live generation. When the heartbeat carries no
// generation (idle workspace) we degrade to the epoch view — still a
// useful landing rather than a dead link.
function _entryHref(entryId) {
  const hb = state.heartbeat || {};
  const epochId = hb.epoch_id || state.epoch.id;
  const genId = hb.generation_id || state.epoch.generation;
  if (epochId && genId && entryId) {
    return phase0Href('run', {
      epochId, generationId: genId, entryId,
    });
  }
  if (epochId) {
    return phase0Href('epoch', { epochId });
  }
  return phase0Href('workspace');
}

function _judgeHref(_judgeName) {
  // The per-judge heatmap row lives on the L1 epoch view; jump there
  // and let the operator scan the matrix. (Anchor-linking to the
  // specific row is a follow-up — the row id is not yet stable.)
  const hb = state.heartbeat || {};
  const epochId = hb.epoch_id || state.epoch.id;
  if (epochId) return phase0Href('epoch', { epochId });
  return phase0Href('workspace');
}

function _patchHref(patch) {
  if (patch && patch.epoch_id && patch.generation_id) {
    return phase0Href('generation', {
      epochId: patch.epoch_id,
      generationId: patch.generation_id,
    });
  }
  return phase0Href('workspace');
}

function _mutationHref(mutation) {
  if (mutation && mutation.epoch_id && mutation.generation_id) {
    return phase0Href('generation', {
      epochId: mutation.epoch_id,
      generationId: mutation.generation_id,
    });
  }
  return phase0Href('workspace');
}

// Build one category block. ``items`` is the per-category result array
// (possibly empty); ``label`` is the section heading; ``rowFactory`` is
// a function that turns one record into a child node (typically an
// anchor). When ``items`` is empty the block still renders the header
// with a muted `(no matches)` row so the operator can see the category
// was scanned.
function _categoryBlock(label, items, rowFactory) {
  const children = [
    el('div', { class: 'phase0-sidebar-search-cat-label' }, [label]),
  ];
  if (!items || items.length === 0) {
    children.push(
      el('div', { class: 'phase0-sidebar-search-empty' }, ['(no matches)']),
    );
  } else {
    for (const item of items) children.push(rowFactory(item));
  }
  return el('div', { class: 'phase0-sidebar-search-cat' }, children);
}

// Render the results panel from a backend payload. Returns nothing —
// mutates the panel DOM in place. Hide-and-show toggles the ``hidden``
// attribute (CSS controls whether the panel appears) so an empty
// query collapses the panel completely without a layout reflow on
// every keystroke.
export function renderSearchResults(results) {
  const panel = $('phase0-sidebar-search-results');
  if (!panel) return;
  clearChildren(panel);

  const entries = (results && results.entries) || [];
  const judges = (results && results.judges) || [];
  const patches = (results && results.patches) || [];
  const mutations = (results && results.mutations) || [];

  panel.appendChild(_categoryBlock('ENTRIES', entries, (e) =>
    el('a', {
      class: 'phase0-sidebar-search-row',
      href: _entryHref(e.id),
      'data-kind': 'entry',
      'data-id': e.id,
    }, [e.id])
  ));
  panel.appendChild(_categoryBlock('JUDGES', judges, (j) =>
    el('a', {
      class: 'phase0-sidebar-search-row',
      href: _judgeHref(j.name),
      'data-kind': 'judge',
      'data-name': j.name,
    }, [j.name])
  ));
  panel.appendChild(_categoryBlock('PATCHES', patches, (p) => {
    const label = (p.mutation_id || p.patch_id || '').slice(0, 24);
    const snippet = p.rationale_snippet || '';
    const children = [
      el('span', { class: 'phase0-sidebar-search-row-id mono' }, [label]),
    ];
    if (snippet) {
      children.push(
        el('span', { class: 'phase0-sidebar-search-row-snippet' }, [snippet]),
      );
    }
    return el('a', {
      class: 'phase0-sidebar-search-row',
      href: _patchHref(p),
      'data-kind': 'patch',
      'data-patch-id': p.patch_id || '',
    }, children);
  }));
  panel.appendChild(_categoryBlock('MUTATIONS', mutations, (m) =>
    el('a', {
      class: 'phase0-sidebar-search-row',
      href: _mutationHref(m),
      'data-kind': 'mutation',
      'data-mutation-id': m.mutation_id || '',
    }, [(m.mutation_id || '').slice(0, 24)])
  ));
}

// Hide the results panel and clear its contents. Called for an empty
// query so an operator who clears the input lands on a clean sidebar.
function _hideResults() {
  const panel = $('phase0-sidebar-search-results');
  if (!panel) return;
  clearChildren(panel);
  panel.setAttribute('hidden', '');
}

function _showResults() {
  const panel = $('phase0-sidebar-search-results');
  if (!panel) return;
  panel.removeAttribute('hidden');
}

// Run one search cycle: fetch and render. Exported so a test can drive
// the search bar without going through the debounced input listener.
export async function runSearch(query) {
  const q = (query || '').trim();
  _lastQuery = q;
  if (!q) {
    _hideResults();
    return;
  }
  let results;
  try {
    results = await _fetchSearch(q);
  } catch (err) {
    // A transient fetch failure leaves the previous results visible
    // rather than wiping them — the next keystroke retries.
    return;
  }
  // The user might have typed more before the response landed; only
  // render if the query is still current.
  if (q !== _lastQuery) return;
  _showResults();
  renderSearchResults(results);
}

function _schedule(fn, ms) {
  const s = _testHooks.scheduleTimer || setTimeout;
  return s(fn, ms);
}

// Wire the input's listener once on first call. Idempotent: a re-init
// (e.g. when the shell rebuilds) re-binds without leaking a duplicate
// handler.
let _wired = false;

export function initSidebarSearch() {
  const input = $('phase0-sidebar-search-input');
  if (!input) return;
  if (_wired) return;
  _wired = true;
  input.addEventListener('input', () => {
    if (_debounceTimer) {
      // Best-effort clear; if the test hook returned a non-numeric
      // handle, clearTimeout is still safe to call.
      try { clearTimeout(_debounceTimer); } catch (_) { /* noop */ }
    }
    _debounceTimer = _schedule(() => runSearch(input.value), SEARCH_DEBOUNCE_MS);
  });
  // Pressing Enter must not submit a (non-existent) form and reload
  // the page — guard so the search bar stays consistent with the
  // "filter inline, never navigate from the input" contract.
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') ev.preventDefault();
  });
}

// Reset module state — used by tests so a fresh case does not see a
// stale debounce handle or wired flag.
export function _resetSidebarSearch() {
  _debounceTimer = null;
  _lastQuery = '';
  _wired = false;
}

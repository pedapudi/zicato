// components/command_palette.js — the ⌘K command palette.
//
// A modal overlay with a single text input + a categorised results list.
// Always shows the static Pages section above any search hits so the
// palette is useful even before the operator types anything.
//
// Keyboard:
//   Cmd+K / Ctrl+K     — open (wired at app level)
//   Esc                — close
//   ArrowDown / ArrowUp — move active result
//   Enter               — activate the active result
//
// Mouse:
//   Click a result      — activate it
//   Click the backdrop  — close
//
// Categories rendered:
//   Pages       — Workspace / Current epoch / Current generation /
//                 Files / Harmonograf (when URL is set)
//   Entries     — from /api/search?q=
//   Judges      — same source
//   Patches     — same source
//   Mutations   — same source
//
// The search request is debounced ~150ms; the response replaces only the
// non-Pages section, so the static rows are never wiped while typing.

import { $, el, clearChildren } from '../core/dom.js';
import { state } from '../core/state.js';
import { phase0Href } from '../../js/views/phase0_router.js';
import { harmonografBase } from '../core/harmonograf.js';

const DEBOUNCE_MS = 150;

// Test hooks for the fetch + the debounce timer — production code never
// sets these. Tests stash stubs so the palette can be driven without a
// real network or wall-clock timer.
export const _testHooks = { fetch: null, scheduleTimer: null };

let _activeIdx = 0;
let _flatResults = [];     // [{ category, label, href, hint, external }]
let _debounceTimer = null;
let _lastQuery = '';
let _wired = false;
let _onKeyDownDoc = null;

function _fetchSearch(query) {
  const f = _testHooks.fetch || ((url) => fetch(url));
  return f('/api/search?q=' + encodeURIComponent(query)).then((res) => {
    if (res && typeof res.json === 'function') return res.json();
    return res;
  });
}

function _schedule(fn, ms) {
  const s = _testHooks.scheduleTimer || setTimeout;
  return s(fn, ms);
}

function _staticPageRows() {
  const rows = [];
  rows.push({
    category: 'Pages',
    label: 'Workspace',
    hint: 'L0',
    href: phase0Href('workspace'),
  });
  const hb = state.heartbeat || {};
  if (hb.epoch_id) {
    rows.push({
      category: 'Pages',
      label: 'Current epoch · ' + hb.epoch_id,
      hint: 'L1',
      href: phase0Href('epoch', { epochId: hb.epoch_id }),
    });
    if (hb.generation_id) {
      rows.push({
        category: 'Pages',
        label: 'Current generation · ' + hb.generation_id,
        hint: 'L2',
        href: phase0Href('generation', {
          epochId: hb.epoch_id, generationId: hb.generation_id,
        }),
      });
    }
  }
  rows.push({
    category: 'Pages',
    label: 'Files',
    hint: 'Artifacts',
    href: phase0Href('files'),
  });
  const hgBase = harmonografBase();
  if (hgBase) {
    rows.push({
      category: 'Pages',
      label: 'Harmonograf ↗',
      hint: 'External',
      href: hgBase,
      external: true,
    });
  }
  return rows;
}

// The task spec ("static Pages results — always shown above search
// hits") wins: Pages are NEVER filtered, even when the user types a
// query that doesn't match any of them. This keeps Workspace + Files
// reachable as a single-keystroke jump from any search.
function _filteredPages(_query) {
  return _staticPageRows();
}

function _entryHref(entryId) {
  const hb = state.heartbeat || {};
  const epochId = hb.epoch_id || state.epoch.id;
  const genId = hb.generation_id || state.epoch.generation;
  if (epochId && genId && entryId) {
    return phase0Href('run', { epochId, generationId: genId, entryId });
  }
  if (epochId) return phase0Href('epoch', { epochId });
  return phase0Href('workspace');
}

function _judgeHref(_name) {
  const hb = state.heartbeat || {};
  const epochId = hb.epoch_id || state.epoch.id;
  if (epochId) return phase0Href('epoch', { epochId });
  return phase0Href('workspace');
}

function _patchHref(p) {
  if (p && p.epoch_id && p.generation_id) {
    return phase0Href('generation', {
      epochId: p.epoch_id, generationId: p.generation_id,
    });
  }
  return phase0Href('workspace');
}

function _mutationHref(m) { return _patchHref(m); }

function _rowsFromSearch(results) {
  const rows = [];
  const entries = (results && results.entries) || [];
  for (const e of entries) {
    rows.push({
      category: 'Entries',
      label: e.id,
      href: _entryHref(e.id),
    });
  }
  const judges = (results && results.judges) || [];
  for (const j of judges) {
    rows.push({
      category: 'Judges',
      label: j.name,
      hint: j.from_generations ? 'from ' + j.from_generations + ' generations' : null,
      href: _judgeHref(j.name),
    });
  }
  const patches = (results && results.patches) || [];
  for (const p of patches) {
    const label = (p.mutation_id || p.patch_id || '').slice(0, 32);
    rows.push({
      category: 'Patches',
      label,
      hint: p.rationale_snippet || null,
      href: _patchHref(p),
    });
  }
  const mutations = (results && results.mutations) || [];
  for (const m of mutations) {
    rows.push({
      category: 'Mutations',
      label: (m.mutation_id || '').slice(0, 32),
      href: _mutationHref(m),
    });
  }
  return rows;
}

// Render the flat results list grouped by category into the panel.
// Updates the module-level _flatResults so the keyboard nav can index
// into it without re-querying the DOM.
function _paintResults(rows) {
  const panel = $('phase0-palette-results');
  if (!panel) return;
  clearChildren(panel);
  _flatResults = rows.slice();
  if (rows.length === 0) {
    panel.appendChild(el('p', { class: 'phase0-palette-empty' },
      ['No matches.']));
    return;
  }
  // Group by category, preserving insertion order.
  const groups = new Map();
  for (const r of rows) {
    if (!groups.has(r.category)) groups.set(r.category, []);
    groups.get(r.category).push(r);
  }
  let flatIdx = 0;
  for (const [cat, items] of groups.entries()) {
    panel.appendChild(el('div', { class: 'phase0-palette-eyebrow' }, [cat]));
    for (const r of items) {
      const rowIdx = flatIdx;
      const children = [
        el('span', { class: 'phase0-palette-row-label' }, [r.label]),
      ];
      if (r.hint) {
        children.push(el('span', { class: 'phase0-palette-row-hint' },
          [r.hint]));
      }
      const cls = 'phase0-palette-row'
        + (rowIdx === _activeIdx ? ' phase0-palette-row-active' : '');
      const row = el('a', {
        class: cls,
        role: 'option',
        href: r.href,
        target: r.external ? '_blank' : null,
        rel: r.external ? 'noopener' : null,
        'data-row-index': String(rowIdx),
        'aria-selected': rowIdx === _activeIdx ? 'true' : 'false',
        onClick: (ev) => {
          if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
          _activate(rowIdx);
        },
      }, children);
      panel.appendChild(row);
      flatIdx += 1;
    }
  }
}

// Re-paint with the current static pages + cached search results.
function _refresh(query, results) {
  const pageRows = _filteredPages(query);
  const searchRows = results ? _rowsFromSearch(results) : [];
  const rows = pageRows.concat(searchRows);
  if (_activeIdx >= rows.length) _activeIdx = rows.length > 0 ? 0 : 0;
  _paintResults(rows);
}

function _runSearch(query) {
  const q = (query || '').trim();
  _lastQuery = q;
  if (!q) {
    _refresh('', null);
    return Promise.resolve();
  }
  return _fetchSearch(q).then((results) => {
    if (q !== _lastQuery) return;
    _refresh(q, results);
  }).catch(() => {
    // On error, keep the static-pages-only view.
    _refresh(q, null);
  });
}

function _activate(idx) {
  if (!_flatResults || idx < 0 || idx >= _flatResults.length) return;
  const row = _flatResults[idx];
  close();
  if (row.external) {
    if (typeof window !== 'undefined' && window.open) {
      window.open(row.href, '_blank', 'noopener');
    } else if (typeof window !== 'undefined') {
      window.location.href = row.href;
    }
    return;
  }
  if (typeof window !== 'undefined' && window.location) {
    // hash-only navigation — leaves the page intact, the router picks it up.
    window.location.hash = row.href.replace(/^#/, '');
  }
}

function _moveActive(delta) {
  if (_flatResults.length === 0) return;
  let next = _activeIdx + delta;
  if (next < 0) next = _flatResults.length - 1;
  if (next >= _flatResults.length) next = 0;
  _activeIdx = next;
  _refresh(_lastQuery, null);  // re-paint with new highlight
  // Best-effort: scroll the active row into view (no-op in harness).
  const panel = $('phase0-palette-results');
  if (panel) {
    const row = panel.querySelector('[data-row-index="' + next + '"]');
    if (row && typeof row.scrollIntoView === 'function') {
      row.scrollIntoView({ block: 'nearest' });
    }
  }
}

export function open() {
  const overlay = $('phase0-palette-overlay');
  const input = $('phase0-palette-input');
  if (!overlay || !input) return;
  overlay.removeAttribute('hidden');
  input.value = '';
  _lastQuery = '';
  _activeIdx = 0;
  _refresh('', null);
  if (typeof input.focus === 'function') input.focus();
  _wireOnce();
}

export function close() {
  const overlay = $('phase0-palette-overlay');
  if (!overlay) return;
  overlay.setAttribute('hidden', '');
  _flatResults = [];
  _lastQuery = '';
  _activeIdx = 0;
  const input = $('phase0-palette-input');
  if (input) input.value = '';
  const panel = $('phase0-palette-results');
  if (panel) clearChildren(panel);
}

export function isOpen() {
  const overlay = $('phase0-palette-overlay');
  return !!(overlay && !overlay.hasAttribute('hidden'));
}

function _wireOnce() {
  if (_wired) return;
  _wired = true;
  const input = $('phase0-palette-input');
  const overlay = $('phase0-palette-overlay');
  if (!input || !overlay) return;
  input.addEventListener('input', () => {
    if (_debounceTimer) {
      try { clearTimeout(_debounceTimer); } catch (_) { /* noop */ }
    }
    _debounceTimer = _schedule(() => _runSearch(input.value), DEBOUNCE_MS);
  });
  input.addEventListener('keydown', (ev) => {
    if (!ev || !ev.key) return;
    if (ev.key === 'Escape') {
      if (ev.preventDefault) ev.preventDefault();
      close();
    } else if (ev.key === 'ArrowDown') {
      if (ev.preventDefault) ev.preventDefault();
      _moveActive(1);
    } else if (ev.key === 'ArrowUp') {
      if (ev.preventDefault) ev.preventDefault();
      _moveActive(-1);
    } else if (ev.key === 'Enter') {
      if (ev.preventDefault) ev.preventDefault();
      _activate(_activeIdx);
    }
  });
  // Backdrop click — close. The input + results live inside
  // `.phase0-palette`; clicks on the backdrop bubble straight to the
  // overlay container itself (we marked it with `data-palette-backdrop`).
  const backdrop = overlay.querySelector('[data-palette-backdrop]');
  if (backdrop) {
    backdrop.addEventListener('click', () => close());
  }
}

// App-level Cmd/Ctrl+K listener. Bound once at bootstrap from app.js.
export function installKeyboardShortcut() {
  if (_onKeyDownDoc) return;
  _onKeyDownDoc = (ev) => {
    if (!ev || !ev.key) return;
    const key = String(ev.key).toLowerCase();
    if (key === 'k' && (ev.metaKey || ev.ctrlKey)) {
      if (ev.preventDefault) ev.preventDefault();
      if (isOpen()) close();
      else open();
    }
  };
  if (typeof document !== 'undefined'
      && typeof document.addEventListener === 'function') {
    document.addEventListener('keydown', _onKeyDownDoc);
  }
}

// Reset module state — used by tests to drop captured listeners +
// stale results between cases.
export function _resetCommandPalette() {
  _activeIdx = 0;
  _flatResults = [];
  _debounceTimer = null;
  _lastQuery = '';
  _wired = false;
  if (_onKeyDownDoc && typeof document !== 'undefined'
      && typeof document.removeEventListener === 'function') {
    document.removeEventListener('keydown', _onKeyDownDoc);
  }
  _onKeyDownDoc = null;
}

// Exported for the tests — the flat order rows are activated in.
export function _flatRowsForTest() { return _flatResults.slice(); }

// Exported for tests — drive a query without the debounce wrapper.
export async function runQuery(query) {
  const input = $('phase0-palette-input');
  if (input) input.value = query;
  _lastQuery = String(query || '');
  await _runSearch(query);
}

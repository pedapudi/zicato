// core/dom.js — the incremental render spine.
//
// This module is the structural flashing fix. A delta NEVER rebuilds a
// panel's innerHTML. Instead:
//   * mount(host, key, builder)  — builds a keyed node ONCE, reuses it.
//   * patchText / patchAttr      — write only when the value changed.
//   * reconcileList(...)         — keyed list diff: existing rows are
//                                  updated in place, new rows appended,
//                                  gone rows removed. The list is never
//                                  cleared-and-rebuilt.
//
// Because nodes keep identity across a re-render, their event listeners
// survive — that is the matchup-click fix — and the browser never
// repaints an unchanged subtree, so panels do not flash.

import { SVG_NS } from './format.js';

export function $(id) { return document.getElementById(id); }

// Build a DOM element. `props`: class | dataset | text | html | on*
// handlers | plain attributes. `children`: array of nodes/strings.
export function el(tag, props, children) {
  const node = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === null || v === undefined) continue;
      if (k === 'class') node.className = v;
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else if (k === 'text') node.textContent = v;
      else if (k === 'html') node.innerHTML = v;
      else if (k.startsWith('on') && typeof v === 'function') {
        node.addEventListener(k.slice(2).toLowerCase(), v);
      } else {
        node.setAttribute(k, v);
      }
    }
  }
  appendChildren(node, children);
  return node;
}

export function svgEl(tag, attrs, children) {
  const node = document.createElementNS(SVG_NS, tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null) continue;
      node.setAttribute(k, String(v));
    }
  }
  appendChildren(node, children);
  return node;
}

function appendChildren(node, children) {
  if (!children) return;
  for (const c of children) {
    if (c == null) continue;
    node.appendChild(toChildNode(c));
  }
}

// Coerce a child into something appendChild accepts. A string becomes a
// text node; a DOM Node passes through; ANYTHING ELSE (a number, a
// bool, or — the bug this guards — an object a producer handed in where
// a string was expected) is stringified into a text node rather than
// thrown back by the real DOM. The harness DOM was permissive here; a
// browser is not, so this coercion is what keeps a malformed datum from
// aborting a whole panel render.
function toChildNode(c) {
  if (typeof c === 'string') return document.createTextNode(c);
  if (c && typeof c === 'object' && c.nodeType !== undefined) return c;
  // A non-Node, non-string value — stringify defensively.
  if (c && typeof c === 'object') {
    let text;
    try { text = JSON.stringify(c); }
    catch { text = String(c); }
    return document.createTextNode(text);
  }
  return document.createTextNode(String(c));
}

export function clearChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

// Set textContent only if it actually differs — avoids a layout/paint
// on an unchanged node.
export function patchText(node, text) {
  if (!node) return;
  const next = text == null ? '' : String(text);
  if (node.textContent !== next) node.textContent = next;
}

// Set an attribute only when it changed; a null/undefined value removes
// the attribute (idempotently).
export function patchAttr(node, name, value) {
  if (!node) return;
  if (value == null) {
    if (node.hasAttribute(name)) node.removeAttribute(name);
    return;
  }
  const next = String(value);
  if (node.getAttribute(name) !== next) node.setAttribute(name, next);
}

// Toggle a class only when its presence must change.
export function patchClass(node, name, on) {
  if (!node) return;
  if (on) { if (!node.classList.contains(name)) node.classList.add(name); }
  else if (node.classList.contains(name)) node.classList.remove(name);
}

// Idempotent keyed mount. The first call builds `builder()` and stores
// it under `data-node=<key>` inside `host`. Every later call with the
// same key returns the SAME node — the caller then patches it. This is
// what keeps a panel's node identity (and its listeners) stable.
//
// `builder` may return either a node, or an object { node, update }.
// When it returns the object form, `mount` calls `update()` on every
// invocation (including the first), so a panel can register its own
// in-place refresh logic.
export function mount(host, key, builder) {
  if (!host) return null;
  let entry = host.querySelector(`:scope > [data-node="${cssEscape(key)}"]`);
  if (!entry) {
    const built = builder();
    if (built && built.node) {
      built.node.setAttribute('data-node', key);
      host.appendChild(built.node);
      if (typeof built.update === 'function') {
        _updaters.set(built.node, built.update);
        built.update();
      }
      return built.node;
    }
    built.setAttribute('data-node', key);
    host.appendChild(built);
    return built;
  }
  const upd = _updaters.get(entry);
  if (upd) upd();
  return entry;
}

const _updaters = new WeakMap();

// Keyed list reconciliation. `items` is the desired list; `keyFn(item)`
// yields a stable string key; `buildFn(item)` builds a fresh row for a
// NEW key; `updateFn(row, item)` updates an EXISTING row in place.
//
// Rows already present (matched by data-key) are kept and updated —
// their listeners and any open sub-state survive. New rows are inserted
// in the correct order; rows whose key vanished are removed. The host
// is never cleared. This is the append-only / no-flash guarantee for
// every table and the log tail.
export function reconcileList(host, items, keyFn, buildFn, updateFn) {
  if (!host) return;
  const desired = items.map((item) => ({ item, key: String(keyFn(item)) }));
  const existing = new Map();
  for (const child of [...host.children]) {
    const k = child.getAttribute('data-key');
    if (k != null) existing.set(k, child);
  }
  let cursor = host.firstChild;
  for (const { item, key } of desired) {
    let row = existing.get(key);
    if (row) {
      existing.delete(key);
      if (updateFn) updateFn(row, item);
    } else {
      row = buildFn(item);
      row.setAttribute('data-key', key);
    }
    // Place `row` at `cursor`. If it is already there, advance; else
    // move it (a move keeps the same node — listeners intact).
    if (cursor === row) {
      cursor = cursor.nextSibling;
    } else {
      host.insertBefore(row, cursor);
    }
  }
  // Anything left in `existing` is no longer desired.
  for (const stale of existing.values()) host.removeChild(stale);
}

// Append-only list growth — used by the activity log. Rows already
// present (by data-key) are left strictly untouched; only genuinely-new
// keys are appended at the end. Nothing is ever moved or removed, so
// the log tail cannot flash or reorder.
export function appendRows(host, items, keyFn, buildFn) {
  if (!host) return 0;
  const present = new Set();
  for (const child of host.children) {
    const k = child.getAttribute('data-key');
    if (k != null) present.add(k);
  }
  let added = 0;
  for (const item of items) {
    const key = String(keyFn(item));
    if (present.has(key)) continue;
    const row = buildFn(item);
    row.setAttribute('data-key', key);
    host.appendChild(row);
    present.add(key);
    added += 1;
  }
  return added;
}

// Trim a host to its last `max` data-key rows, oldest-first. Used to
// bound the log tail without re-rendering surviving rows.
export function trimRows(host, max) {
  if (!host) return;
  const rows = [...host.children].filter((c) => c.hasAttribute('data-key'));
  const excess = rows.length - max;
  for (let i = 0; i < excess; i++) host.removeChild(rows[i]);
}

// Replace the body of a panel ONLY when its content key changed. Used
// for the coarse case where a section's whole content must swap (e.g.
// an empty state ⇄ a populated panel). `contentKey` is a cheap digest
// the caller computes; when it is unchanged the builder is not run and
// the DOM is untouched — no flash.
export function swapIfChanged(host, contentKey, builder) {
  if (!host) return;
  if (host.getAttribute('data-content-key') === String(contentKey)) return;
  clearChildren(host);
  const built = builder();
  if (Array.isArray(built)) appendChildren(host, built);
  else if (built) host.appendChild(built);
  host.setAttribute('data-content-key', String(contentKey));
}

// Minimal CSS.escape shim — querySelector needs keys escaped, and
// CSS.escape is unavailable in the jsdom-free test harness.
function cssEscape(value) {
  return String(value).replace(/["\\\]#.:>~+*\s]/g, '\\$&');
}

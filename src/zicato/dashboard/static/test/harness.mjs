// test/harness.mjs — a minimal DOM + assertion harness.
//
// The dashboard JS is verifiable behaviour: clicks, incremental
// updates, no-flash reconciliation. This harness provides just enough
// of the DOM to exercise core/dom.js and the components without pulling
// in jsdom — it is dependency-free so `node` runs it directly.
//
// It is NOT a full DOM. It implements the subset the render spine
// touches: element creation, children, attributes, classList, dataset,
// textContent, querySelector(':scope > [data-node=..]' / '[data-key]'),
// event listeners + dispatch, and SVG-namespace nodes. A test that
// needs more should mock it explicitly.

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------

let _idCounter = 0;

class ClassList {
  constructor(node) { this._node = node; this._set = new Set(); }
  add(...names) { for (const n of names) this._set.add(n); this._sync(); }
  remove(...names) { for (const n of names) this._set.delete(n); this._sync(); }
  contains(n) { return this._set.has(n); }
  toggle(n, force) {
    const has = this._set.has(n);
    const want = force === undefined ? !has : force;
    if (want) this._set.add(n); else this._set.delete(n);
    this._sync();
    return want;
  }
  _sync() { this._node._attrs.class = [...this._set].join(' '); }
  _load(str) { this._set = new Set(String(str || '').split(/\s+/).filter(Boolean)); }
  get length() { return this._set.size; }
}

class TextNode {
  constructor(text) { this.nodeType = 3; this.textContent = String(text); this.parentNode = null; }
  get firstChild() { return null; }
  cloneNode() { return new TextNode(this.textContent); }
}

class StyleDecl {
  constructor() { this._props = {}; }
  setProperty(k, v) { this._props[k] = v; }
  get cssText() {
    return Object.entries(this._props).map(([k, v]) => `${k}:${v}`).join(';');
  }
}

const STYLE_KEYS = new Set(['width', 'height', 'left', 'top', 'transform', 'display']);

class Element {
  constructor(tag, ns) {
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.localName = String(tag).toLowerCase();
    this.namespaceURI = ns || 'http://www.w3.org/1999/xhtml';
    this.childNodes = [];
    this.parentNode = null;
    this._attrs = {};
    this._listeners = {};
    this._style = new StyleDecl();
    this.classList = new ClassList(this);
    this.dataset = new Proxy({}, {
      get: (t, k) => this._attrs['data-' + camelToKebab(k)],
      set: (t, k, v) => { this._attrs['data-' + camelToKebab(k)] = String(v); return true; },
    });
  }

  // -- attributes -------------------------------------------------
  setAttribute(name, value) {
    this._attrs[name] = String(value);
    if (name === 'class') this.classList._load(value);
  }
  getAttribute(name) {
    return name in this._attrs ? this._attrs[name] : null;
  }
  hasAttribute(name) { return name in this._attrs; }
  removeAttribute(name) {
    delete this._attrs[name];
    if (name === 'class') this.classList._load('');
  }

  get className() { return this._attrs.class || ''; }
  set className(v) { this.setAttribute('class', v); }

  get id() { return this._attrs.id || ''; }
  set id(v) { this.setAttribute('id', v); }

  get style() { return this._style; }

  // -- text -------------------------------------------------------
  get textContent() {
    return this.childNodes.map((c) =>
      c.nodeType === 3 ? c.textContent : c.textContent).join('');
  }
  set textContent(v) {
    this.childNodes = [];
    if (v !== '' && v != null) this.appendChild(new TextNode(v));
  }

  get innerHTML() { return '<!-- harness: innerHTML not serialized -->'; }
  set innerHTML(v) {
    // The render spine forbids innerHTML for deltas; the harness flags
    // any write so a no-flash test can assert it never happened.
    this.childNodes = [];
    this._innerHTMLWrites = (this._innerHTMLWrites || 0) + 1;
    if (v) this.appendChild(new TextNode('[html]'));
  }

  // -- children ---------------------------------------------------
  appendChild(child) {
    // Match the real DOM: appendChild rejects a non-Node. A permissive
    // harness here once masked a render bug (an object handed in where
    // a string was expected) — so the harness now throws like a
    // browser, and the render spine must coerce before it appends.
    if (!child || child.nodeType === undefined) {
      throw new TypeError(
        "Failed to execute 'appendChild' on 'Node': parameter 1 is not of type 'Node'.",
      );
    }
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  insertBefore(child, ref) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    if (ref == null) { this.childNodes.push(child); return child; }
    const idx = this.childNodes.indexOf(ref);
    if (idx < 0) this.childNodes.push(child);
    else this.childNodes.splice(idx, 0, child);
    return child;
  }
  removeChild(child) {
    const idx = this.childNodes.indexOf(child);
    if (idx >= 0) { this.childNodes.splice(idx, 1); child.parentNode = null; }
    return child;
  }
  cloneNode(deep) {
    const copy = new Element(this.localName, this.namespaceURI);
    copy._attrs = { ...this._attrs };
    copy.classList._load(copy._attrs.class || '');
    if (deep) for (const c of this.childNodes) copy.appendChild(c.cloneNode(true));
    return copy;
  }

  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const idx = this.parentNode.childNodes.indexOf(this);
    return this.parentNode.childNodes[idx + 1] || null;
  }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }

  // -- events -----------------------------------------------------
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  removeEventListener(type, fn) {
    const list = this._listeners[type];
    if (list) this._listeners[type] = list.filter((f) => f !== fn);
  }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    let node = this;
    while (node) {
      ev.currentTarget = node;
      const list = node._listeners && node._listeners[ev.type];
      if (list) for (const fn of [...list]) { fn(ev); }
      if (ev._stopped) break;
      node = node.parentNode;
    }
    return !ev._defaultPrevented;
  }

  // -- queries ----------------------------------------------------
  // Supports the two forms core/dom.js uses:
  //   ':scope > [data-node="K"]'  and  '[data-key]'
  querySelector(sel) {
    return this._query(sel, true)[0] || null;
  }
  querySelectorAll(sel) { return this._query(sel, false); }
  _query(sel, first) {
    const scopeChild = sel.startsWith(':scope > ');
    const inner = scopeChild ? sel.slice(':scope > '.length) : sel;
    const m = inner.match(/^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/);
    const out = [];
    const candidates = scopeChild ? this.children : this._descendants();
    for (const c of candidates) {
      if (m) {
        const attr = m[1];
        const val = m[2];
        if (val === undefined) { if (c.hasAttribute(attr)) out.push(c); }
        else if (unescapeCss(c.getAttribute(attr)) === unescapeCss(val)
                 || c.getAttribute(attr) === unescapeCss(val)) out.push(c);
      }
      if (first && out.length) break;
    }
    return out;
  }
  _descendants() {
    const out = [];
    const walk = (n) => {
      for (const c of n.children) { out.push(c); walk(c); }
    };
    walk(this);
    return out;
  }

  // -- harness introspection -------------------------------------
  // Total innerHTML writes in this subtree — a no-flash test asserts 0.
  innerHTMLWriteCount() {
    let total = this._innerHTMLWrites || 0;
    for (const c of this.children) total += c.innerHTMLWriteCount();
    return total;
  }
}

function camelToKebab(s) { return String(s).replace(/[A-Z]/g, (m) => '-' + m.toLowerCase()); }
function unescapeCss(s) { return s == null ? s : String(s).replace(/\\(.)/g, '$1'); }

class DocumentImpl {
  constructor() {
    this.body = new Element('body');
    this.documentElement = new Element('html');
    this._byId = new Map();
    this.readyState = 'complete';
    this._listeners = {};
  }
  createElement(tag) { return new Element(tag); }
  createElementNS(ns, tag) { return new Element(tag, ns); }
  createTextNode(text) { return new TextNode(text); }
  getElementById(id) {
    // Walk both the registry and the live tree.
    if (this._byId.has(id)) return this._byId.get(id);
    return findById(this.body, id) || findById(this.documentElement, id);
  }
  registerId(id, node) { this._byId.set(id, node); }
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  dispatchEvent(ev) {
    const list = this._listeners[ev.type];
    if (list) for (const fn of [...list]) fn(ev);
  }
}

function findById(node, id) {
  if (!node || node.nodeType !== 1) return null;
  if (node.getAttribute && node.getAttribute('id') === id) return node;
  for (const c of node.children) {
    const hit = findById(c, id);
    if (hit) return hit;
  }
  return null;
}

// A minimal Event with the methods handlers use.
export function makeEvent(type, props = {}) {
  return {
    type, _stopped: false, _defaultPrevented: false,
    stopPropagation() { this._stopped = true; },
    preventDefault() { this._defaultPrevented = true; },
    ...props,
  };
}

// A minimal in-memory localStorage shim. The shell-toggle persists the
// user's chosen UI through this; tests need to read / clear it.
class MemoryStorage {
  constructor() { this._kv = new Map(); }
  getItem(k) { return this._kv.has(k) ? this._kv.get(k) : null; }
  setItem(k, v) { this._kv.set(String(k), String(v)); }
  removeItem(k) { this._kv.delete(k); }
  clear() { this._kv.clear(); }
  get length() { return this._kv.size; }
  key(i) {
    const keys = [...this._kv.keys()];
    return i < keys.length ? keys[i] : null;
  }
}

// Install a fresh document/window onto globalThis. Returns the document.
export function installDom() {
  const document = new DocumentImpl();
  globalThis.document = document;
  globalThis.window = globalThis.window || {};
  globalThis.window.location = globalThis.window.location || { hash: '', search: '' };
  globalThis.window.localStorage = new MemoryStorage();
  globalThis.window.addEventListener = (t, fn) => {
    (globalThis.window._listeners = globalThis.window._listeners || {});
    (globalThis.window._listeners[t] = globalThis.window._listeners[t] || []).push(fn);
  };
  globalThis.Element = Element;
  globalThis.Node = { ELEMENT_NODE: 1, TEXT_NODE: 3 };
  globalThis.console = globalThis.console || console;
  return document;
}

// ---------------------------------------------------------------------------
// Test runner + assertions
// ---------------------------------------------------------------------------

const _tests = [];
let _passed = 0;
let _failed = 0;
const _failures = [];

export function test(name, fn) { _tests.push({ name, fn }); }

// Totals for this isolated test-file worker. run-all.mjs combines the workers'
// reports; direct file execution still receives the same local summary.
let _fileCount = 0;
export function totals() {
  return { passed: _passed, failed: _failed, files: _fileCount, failures: _failures.slice() };
}

export function assert(cond, msg) {
  if (!cond) throw new Error('assertion failed: ' + (msg || ''));
}
export function assertEqual(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`
      + (msg ? ` — ${msg}` : ''));
  }
}
export function assertDeep(actual, expected, msg) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  if (a !== b) throw new Error(`deep-equal failed: ${a} !== ${b}` + (msg ? ` — ${msg}` : ''));
}

const _runs = new Set();

export function run() {
  const pending = _run();
  _runs.add(pending);
  pending.finally(() => _runs.delete(pending));
  return pending;
}

export async function waitForRuns() {
  await Promise.all(_runs);
}

async function _run() {
  // Drain the queue: each test file calls run() once, and the harness
  // module is shared across files when run-all imports them in turn —
  // so a run() consumes (and clears) only the tests registered since
  // the previous run().
  const batch = _tests.splice(0, _tests.length);
  _fileCount += 1;
  let batchPassed = 0;
  let batchFailed = 0;
  for (const { name, fn } of batch) {
    try {
      await fn();
      _passed += 1;
      batchPassed += 1;
      process.stdout.write(`  ok  ${name}\n`);
    } catch (err) {
      _failed += 1;
      batchFailed += 1;
      _failures.push({ name, err });
      process.stdout.write(`  FAIL ${name}\n       ${err.message}\n`);
    }
  }
  process.stdout.write(`\n${batchPassed} passed, ${batchFailed} failed\n`);
  if (batchFailed > 0) process.exitCode = 1;
  return { passed: batchPassed, failed: batchFailed, failures: _failures };
}

export { Element, TextNode };

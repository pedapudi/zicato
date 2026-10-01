// test/write_log.mjs — record the DOM writes the harness elements receive.
//
// The console's render rule is that a frame whose content did not change
// writes nothing: no attribute, class, text or child change. A browser
// reports every such write as a mutation record, even one that stores the
// value already there. `recordWrites(root, fn)` runs `fn` and returns one
// line per write made to `root` or a node inside it, so a test can assert
// that an unchanged frame leaves the list empty.

import { Element } from './harness.mjs';

let _log = null;

function note(node, what) { if (_log) _log.push({ node, what }); }

function inside(root, node) {
  for (let n = node; n; n = n.parentNode) if (n === root) return true;
  return false;
}

let _installed = false;
function install() {
  if (_installed) return;
  _installed = true;
  const P = Element.prototype;
  for (const name of ['setAttribute', 'removeAttribute']) {
    const orig = P[name];
    P[name] = function (attr, ...rest) { note(this, name + ' ' + attr); return orig.call(this, attr, ...rest); };
  }
  for (const name of ['appendChild', 'removeChild', 'insertBefore']) {
    const orig = P[name];
    P[name] = function (...args) { note(this, name); return orig.apply(this, args); };
  }
  const text = Object.getOwnPropertyDescriptor(P, 'textContent');
  Object.defineProperty(P, 'textContent', {
    configurable: true,
    get: text.get,
    set(v) { note(this, 'textContent'); text.set.call(this, v); },
  });
  const CL = Object.getPrototypeOf(new Element('span').classList);
  for (const name of ['add', 'remove', 'toggle']) {
    const orig = CL[name];
    CL[name] = function (...args) { note(this._node, 'class ' + name + ' ' + args[0]); return orig.apply(this, args); };
  }
}

// Run `fn` (sync or async) and return the writes made inside `root`.
export async function recordWrites(root, fn) {
  install();
  _log = [];
  try {
    await fn();
    return _log.filter((w) => inside(root, w.node)).map((w) => w.what);
  } finally {
    _log = null;
  }
}

// Count assignments to an element's `disabled` property, which the harness
// stores as a plain property. Returns a reader for the count.
export function countDisabledWrites(node) {
  let value = !!node.disabled;
  let count = 0;
  Object.defineProperty(node, 'disabled', {
    configurable: true,
    get() { return value; },
    set(v) { count += 1; value = !!v; },
  });
  return () => count;
}

// core/dom.js — element builders + write-only-on-change patch helpers.
//
// The ACTUAL anti-flash mechanism in this app is `gatedSwap` (ui.js): a
// view computes a cheap content digest, and when the digest is unchanged
// the DOM is left strictly untouched (no builder run, no writes — the
// digest no-op the tests pin via firstChild identity +
// innerHTMLWriteCount). When the digest DID change, the panel's subtree
// is rebuilt with `el`/`svgEl` and swapped in whole.
//
// This module supplies the building blocks under that discipline:
//   * el / svgEl                 — element construction (never innerHTML
//                                  for structure).
//   * patchText / patchClass     — write only when the value actually
//                                  changed, for the few long-lived chrome
//                                  nodes (status pill, readouts) that are
//                                  patched in place rather than swapped.
//   * clearChildren              — explicit child teardown for a rebuild.

const SVG_NS = 'http://www.w3.org/2000/svg';

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
  // The render digest describes the CHILDREN gatedSwap painted; a host with
  // no children must not claim them. Leaving the attribute behind makes the
  // gate skip the next paint into the emptied host — the view then shows its
  // loading placeholder forever (a never-paint, strictly worse than the
  // extra repaint dropping the digest can cost).
  if (node.removeAttribute) node.removeAttribute('data-t-digest');
}

// Set textContent only if it actually differs — avoids a layout/paint
// on an unchanged node.
export function patchText(node, text) {
  if (!node) return;
  const next = text == null ? '' : String(text);
  if (node.textContent !== next) node.textContent = next;
}

// Toggle a class only when its presence must change.
export function patchClass(node, name, on) {
  if (!node) return;
  if (on) { if (!node.classList.contains(name)) node.classList.add(name); }
  else if (node.classList.contains(name)) node.classList.remove(name);
}

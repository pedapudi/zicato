// js/hovercard.js — a small, theme-aware HOVERCARD that replaces the
// browser-default, off-brand native SVG `<title>` tooltip.
//
// Why this exists: native `<title>` tooltips are unstyled OS chrome — they
// ignore the console design language (panel/ink/rule tokens, the mono face)
// and they cannot carry richly-formatted detail. This helper attaches a
// styled, positioned card to ANY target element (SVG or HTML), shown on
// `mouseenter`/`focus` and hidden on `mouseleave`/`blur`/`Escape`.
//
// Design notes that matter:
//   * THEME-AWARE: the card is mounted INSIDE the console root (#console-root)
//     so it inherits the per-theme `--v2-*` custom properties — it reads
//     correctly across all 16 themes (light and dark) with no hardcoded hex.
//     Its look is entirely in css/console.css (`.dn-hovercard`).
//   * NOT PART OF THE DIGEST-GATED RENDER: the card is a SINGLETON transient
//     OVERLAY appended once, OUTSIDE any panel the digest reconciler touches.
//     Showing/hiding only toggles a class + writes the card's own text — it
//     never mutates a digest-gated panel, so it cannot trigger a repaint loop
//     or the SSE-heartbeat flashing bug. Listeners are attached when nodes are
//     (re)built, so they survive node identity, rather than on every heartbeat.
//   * POSITIONED so it never clips at a panel/viewport edge: it measures the
//     target + itself and FLIPS above↔below / CLAMPS horizontally to the
//     viewport. All measurement is feature-guarded so it is a no-op in the
//     test DOM (which has no layout) rather than throwing.
//   * KEYBOARD-ACCESSIBLE: a wired target is made focusable (tabindex 0 if it
//     has none) and the card carries role="tooltip" + an id linked from the
//     target via aria-describedby; Escape dismisses it.
//   * prefers-reduced-motion: any fade is dropped (CSS handles this); JS does
//     no animation of its own.

let _card = null;        // the singleton card element.
let _cardId = 'dn-hovercard-singleton';
let _activeTarget = null;
let _escBound = false;

function canMeasure(node) {
  return node && typeof node.getBoundingClientRect === 'function';
}

// The mount point: the nearest ancestor that IS the console root (so the card
// inherits the live per-theme tokens), else document.body as a safe fallback.
function mountFor(target) {
  let n = target;
  while (n) {
    if (n.id === 'console-root') return n;
    n = n.parentNode;
  }
  return (typeof document !== 'undefined' && document.body) || null;
}

function ensureCard(target) {
  const host = mountFor(target);
  if (_card && _card.parentNode === host) return _card;
  // (re)create under the correct theme root.
  if (_card && _card.parentNode) _card.parentNode.removeChild(_card);
  const card = document.createElement('div');
  card.className = 'dn-hovercard';
  card.id = _cardId;
  card.setAttribute('role', 'tooltip');
  card.setAttribute('aria-hidden', 'true');
  if (host) host.appendChild(card);
  _card = card;
  if (!_escBound && typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('keydown', (ev) => { if (ev && ev.key === 'Escape') hide(); });
    _escBound = true;
  }
  return card;
}

// Fill the card with `content`: a string (→ text), a Node (→ adopted), or an
// array of strings/Nodes. Strings honour newlines (each line its own div).
function fill(card, content) {
  while (card.firstChild) card.removeChild(card.firstChild);
  const add = (c) => {
    if (c == null) return;
    if (typeof c === 'string') {
      const lines = c.split('\n');
      for (const line of lines) {
        const row = document.createElement('div');
        row.className = 'dn-hovercard-line';
        row.textContent = line;
        card.appendChild(row);
      }
    } else if (c && c.nodeType !== undefined) {
      card.appendChild(c);
    } else {
      const row = document.createElement('div');
      row.className = 'dn-hovercard-line';
      row.textContent = String(c);
      card.appendChild(row);
    }
  };
  if (Array.isArray(content)) content.forEach(add);
  else add(content);
}

// Position the card relative to the target, flipping above↔below and clamping
// to the viewport so it never clips. A no-op when the DOM cannot measure.
function position(card, target) {
  if (!canMeasure(target) || !canMeasure(card)) return;
  const vw = (typeof window !== 'undefined' && window.innerWidth) || 1024;
  const vh = (typeof window !== 'undefined' && window.innerHeight) || 768;
  const t = target.getBoundingClientRect();
  const c = card.getBoundingClientRect();
  const gap = 8;
  // prefer BELOW the target; flip ABOVE if it would clip the bottom edge.
  let top = t.bottom + gap;
  if (top + c.height > vh - 4 && t.top - gap - c.height > 4) top = t.top - gap - c.height;
  // centre horizontally on the target, then CLAMP into the viewport.
  let left = t.left + t.width / 2 - c.width / 2;
  if (left < 4) left = 4;
  if (left + c.width > vw - 4) left = vw - 4 - c.width;
  card.style.setProperty('top', Math.round(Math.max(4, top)) + 'px');
  card.style.setProperty('left', Math.round(left) + 'px');
}

export function show(target, content) {
  if (!target) return;
  const card = ensureCard(target);
  if (!card) return;
  fill(card, content);
  card.setAttribute('aria-hidden', 'false');
  card.classList.add('dn-hovercard-on');
  _activeTarget = target;
  position(card, target);
}

export function hide() {
  if (!_card) return;
  _card.classList.remove('dn-hovercard-on');
  _card.setAttribute('aria-hidden', 'true');
  _activeTarget = null;
}

// Attach hovercard behaviour to `target`. `content` may be a string/Node/array,
// OR a function `() => content` evaluated lazily at show time (so a card can
// carry up-to-the-moment text). Returns the target for chaining.
//
// Idempotent-ish: re-attaching to the same node simply adds fresh listeners,
// which is fine because nodes are rebuilt (not heartbeat-mutated). The visible
// affordance is the card; a trivial native `<title>` may be kept ALONGSIDE as a
// pure no-JS/a11y fallback by the caller.
export function attachHovercard(target, content) {
  if (!target || !target.addEventListener) return target;
  const get = () => (typeof content === 'function' ? content() : content);
  // make the target keyboard-focusable + announce the relationship.
  if (target.getAttribute && target.getAttribute('tabindex') == null && !target.hasAttribute('tabindex')) {
    target.setAttribute('tabindex', '0');
  }
  if (target.setAttribute) target.setAttribute('aria-describedby', _cardId);
  target.setAttribute('data-hovercard', '1');
  const onShow = () => show(target, get());
  const onHide = () => { if (_activeTarget === target) hide(); };
  target.addEventListener('mouseenter', onShow);
  target.addEventListener('focus', onShow);
  target.addEventListener('mouseleave', onHide);
  target.addEventListener('blur', onHide);
  return target;
}

// Test/инспection helpers — exported so the suite can assert wiring without a
// real browser: whether a node is hovercard-wired, and the current card text.
export function hasHovercard(target) {
  return !!(target && target.getAttribute && target.getAttribute('data-hovercard') === '1');
}
export function cardText() { return _card ? _card.textContent : ''; }
export function isShown() { return !!(_card && _card.classList && _card.classList.contains('dn-hovercard-on')); }

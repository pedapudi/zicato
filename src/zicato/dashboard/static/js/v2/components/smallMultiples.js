// v2/components/smallMultiples.js — a responsive grid of mini charts.
//
// The "small multiples" idea (Tufte / ACM density): many tiny,
// identically-scaled comparative panels laid out in a grid so the eye
// compares across them at a glance. zicato uses it for the landscape /
// at-a-glance views — one mini diverging bar per board entry, or one
// per judge — where the shape of N little charts side by side tells the
// operator more than one big chart could.
//
// This primitive owns ONLY the layout + per-item framing (an optional
// title + the slot the chart drops into). The actual mini chart is the
// caller's: `render(item, i)` returns a detached node (e.g. a
// divergingBar). Keeping the chart pluggable is what lets the same grid
// host bars, sparklines, or heatmap chips without this file knowing
// about any of them.
//
// Pure factory; returns a detached node. Responsive layout is CSS-grid
// driven (auto-fill with a min track) so the grid reflows to the
// container; an explicit `columns` pins a fixed column count when the
// caller wants a row of exactly N.

import { el } from '../../core/dom.js';

/**
 * Lay out N mini charts in a responsive grid.
 *
 * opts:
 *   items     — array of item data. One panel per item.
 *   render    — (item, i) => Node|string, builds the mini chart body.
 *               A nullish return yields an empty slot (kept, so the
 *               grid alignment is stable).
 *   columns   — optional fixed column count. When set, the grid uses
 *               exactly that many equal columns; otherwise it auto-fills
 *               responsively from `--v2-sm-min` (a min track width).
 *   title     — optional (item, i) => string, a caption above each
 *               panel. Omit for chartless-label multiples.
 *   minWidth  — optional CSS length for the responsive min track
 *               (default '180px'); ignored when `columns` is set.
 *   gap       — optional CSS length for the inter-panel gap.
 *   onItem    — optional (item, i, ev) => void; makes each panel a
 *               drillable button (pointer, hover, keyboard).
 *   itemKey   — optional (item, i) => string|number, stable panel key.
 *   ariaLabel — optional container aria-label.
 *   emptyText — text when there are no items (default '—').
 *
 * Returns the detached grid container node.
 */
export function smallMultiples(opts) {
  const o = opts || {};
  const items = Array.isArray(o.items) ? o.items : [];
  const render = typeof o.render === 'function' ? o.render : () => null;
  const onItem = typeof o.onItem === 'function' ? o.onItem : null;
  const titleFn = typeof o.title === 'function' ? o.title : null;
  const itemKey = typeof o.itemKey === 'function' ? o.itemKey : (_i, i) => i;

  const grid = el('div', {
    class: 'v2-sm' + (typeof o.columns === 'number' && o.columns > 0 ? ' v2-sm-fixed' : ' v2-sm-auto'),
    role: 'list',
    'aria-label': o.ariaLabel || 'small multiples',
  });

  // Layout knobs are inline style props so a view can tune density per
  // call site without a per-view stylesheet.
  if (typeof o.columns === 'number' && o.columns > 0) {
    grid.style.setProperty('grid-template-columns', `repeat(${o.columns}, minmax(0, 1fr))`);
  } else {
    grid.style.setProperty('grid-template-columns', `repeat(auto-fill, minmax(${o.minWidth || '180px'}, 1fr))`);
  }
  if (o.gap) grid.style.setProperty('gap', String(o.gap));

  if (items.length === 0) {
    grid.appendChild(el('div', { class: 'v2-sm-empty' }, [o.emptyText == null ? '—' : o.emptyText]));
    return grid;
  }

  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    const panel = el('div', { class: 'v2-sm-panel', role: 'listitem' });
    panel.setAttribute('data-key', String(itemKey(item, i)));
    if (titleFn) {
      const t = titleFn(item, i);
      if (t != null && t !== '') {
        panel.appendChild(el('div', { class: 'v2-sm-panel-title' }, [String(t)]));
      }
    }
    const body = render(item, i);
    panel.appendChild(el('div', { class: 'v2-sm-panel-body' }, [
      body == null ? document.createTextNode('') : body,
    ]));
    if (onItem) {
      panel.classList.add('v2-sm-panel-drillable');
      panel.setAttribute('role', 'button');
      panel.setAttribute('tabindex', '0');
      const fire = (ev) => onItem(item, i, ev);
      panel.addEventListener('click', fire);
      panel.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); fire(ev); }
      });
    }
    grid.appendChild(panel);
  }
  return grid;
}

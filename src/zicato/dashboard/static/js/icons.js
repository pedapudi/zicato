// js/icons.js — the console's one drawn icon set.
//
// Every mark the chrome draws (loop controls, close, verdicts, crowns, tree
// marks, live-feed marks, chevrons, refresh, overflow, execution kinds) is
// drawn here, never typed as a Unicode character. A typed symbol falls back
// to whatever system font carries it: the bundled faces lack most of them, so
// weight, baseline and advance change with the operating system and with the
// typeface picker, and some symbols render as colour emoji.
//
// One set, one construction: a 16-unit grid, a 1.5-unit stroke with round
// caps and joins (the brand mark's line character), `currentColor`, so an
// icon takes the colour of the text around it. A filled shape fills with
// `currentColor` too. Every icon is `aria-hidden`: the control or row that
// carries it names itself through its text or its `aria-label`.
//
// An icon is static chrome. A view that rebuilds on a changed digest builds
// fresh icons with the rest of its subtree; a long-lived node patched in
// place goes through `patchIconLabel`, which writes only when the icon or the
// words change.

import { svgEl, clearChildren } from './core/dom.js';

// Each drawing: a list of [tag, attributes]. `fill: 'currentColor'` marks a
// filled shape (the stroke still draws, so filled and open marks share one
// outline weight).
const FILLED = { fill: 'currentColor' };
// A gear: the settings entry and a tool call share it.
const GEAR = [['circle', { cx: 8, cy: 8, r: 2 }], ['path', { d: 'M8 2v2M8 12v2M2 8h2M12 8h2M3.8 3.8l1.4 1.4M10.8 10.8l1.4 1.4M3.8 12.2l1.4-1.4M10.8 5.2l1.4-1.4' }]];
const DRAWINGS = {
  // loop controls
  pause: [['path', { d: 'M5.5 3.5v9M10.5 3.5v9' }]],
  resume: [['path', { d: 'M5 3.5 12 8l-7 4.5z', ...FILLED }]],
  skip: [['path', { d: 'M3.5 3.5 9.5 8l-6 4.5z', ...FILLED }], ['path', { d: 'M12.5 3.5v9' }]],
  // dismiss a panel or an armed control
  close: [['path', { d: 'M4.5 4.5l7 7M11.5 4.5l-7 7' }]],
  // verdicts
  pass: [['path', { d: 'M3.5 8.5l3 3 6-7' }]],
  fail: [['path', { d: 'M4 4l8 8M12 4l-8 8' }]],
  timeout: [['circle', { cx: 8, cy: 9, r: 4.5 }], ['path', { d: 'M8 9V6.5M6.5 2.5h3' }]],
  unscored: [['circle', { cx: 8, cy: 8, r: 4.5, 'stroke-dasharray': '1.6 1.9' }]],
  unpredicted: [['path', { d: 'M8 3.5v9M3.5 8h9' }]],
  // champions: the current champion's crown is solid, a former champion's open
  crown: [['path', { d: 'M2.5 12.5v-7l3 2.5L8 3.5l2.5 4.5 3-2.5v7z', ...FILLED }]],
  'crown-former': [['path', { d: 'M2.5 12.5v-7l3 2.5L8 3.5l2.5 4.5 3-2.5v7z' }]],
  // direction and movement
  up: [['path', { d: 'M8 12.5v-9M4.5 7 8 3.5 11.5 7' }]],
  down: [['path', { d: 'M8 3.5v9M4.5 9 8 12.5 11.5 9' }]],
  'to-start': [['path', { d: 'M3.5 3h9M8 13V6M5 9l3-3 3 3' }]],
  'to-end': [['path', { d: 'M3.5 13h9M8 3v7M5 7l3 3 3-3' }]],
  swap: [['path', { d: 'M3 5.5h9.5M10 3l2.5 2.5L10 8M13 10.5H3.5M6 8l-2.5 2.5L6 13' }]],
  external: [['path', { d: 'M7 3.5h5.5V9M12.5 3.5 4 12' }]],
  forward: [['path', { d: 'M3 8h10M9 4l4 4-4 4' }]],
  back: [['path', { d: 'M13 8H3M7 4 3 8l4 4' }]],
  // disclosure
  expand: [['path', { d: 'M6 3.5 10.5 8 6 12.5' }]],
  collapse: [['path', { d: 'M3.5 6 8 10.5 12.5 6' }]],
  separator: [['path', { d: 'M6.5 4 10.5 8l-4 4' }]],
  // actions and states
  refresh: [['path', { d: 'M12.5 8a4.5 4.5 0 1 1-1.3-3.2' }], ['path', { d: 'M11.5 2v3h-3' }]],
  reset: [['path', { d: 'M3.5 8a4.5 4.5 0 1 0 1.3-3.2' }], ['path', { d: 'M4.5 2v3h3' }]],
  more: [['circle', { cx: 3.5, cy: 8, r: 0.9, ...FILLED }], ['circle', { cx: 8, cy: 8, r: 0.9, ...FILLED }], ['circle', { cx: 12.5, cy: 8, r: 0.9, ...FILLED }]],
  empty: [['circle', { cx: 8, cy: 8, r: 4.5 }], ['path', { d: 'M4 12l8-8' }]],
  message: [['path', { d: 'M2.5 3.5h11v7.5H7l-3 2.5V11H2.5z' }]],
  note: [['path', { d: 'M3.5 3.5h9v9h-9z' }], ['path', { d: 'M6 6.5h4M6 9.5h2.5' }]],
  // the navigation tree
  generation: [['path', { d: 'M8 3l5 5-5 5-5-5z', ...FILLED }]],
  child: [['path', { d: 'M4.5 3v6.5h7.5M9.5 7 12 9.5 9.5 12' }]],
  board: [['rect', { x: 3, y: 3, width: 10, height: 10, rx: 1 }], ['path', { d: 'M3 8h10M8 3v10' }]],
  evals: [['rect', { x: 3, y: 3, width: 10, height: 10, rx: 1 }], ['path', { d: 'M3 6.5h10M3 9.5h10' }]],
  instrument: [['circle', { cx: 8, cy: 8, r: 5 }], ['circle', { cx: 8, cy: 8, r: 1.3, ...FILLED }]],
  traces: [['path', { d: 'M2 8c2-5 4-5 6 0s4 5 6 0' }]],
  mutations: [['path', { d: 'M6.5 3 5.5 13M10.5 3l-1 10M3 6h10M3 10h10' }]],
  publication: [['path', { d: 'M4 2.5h5.5L12 5v8.5H4z' }], ['path', { d: 'M6.5 8h3M6.5 10.5h3' }]],
  log: [['path', { d: 'M3 4.5h10M3 8h10M3 11.5h10' }]],
  // execution kinds
  tool: GEAR,
  agent: [['circle', { cx: 8, cy: 5.5, r: 2.5 }], ['path', { d: 'M3.5 13.5a4.5 4.5 0 0 1 9 0' }]],
  artifact: [['rect', { x: 3.5, y: 3.5, width: 9, height: 9, rx: 1 }]],
  // settings: the top-bar entry and its sections
  settings: GEAR,
  contract: [['rect', { x: 3.5, y: 3, width: 9, height: 10.5, rx: 1 }], ['path', { d: 'M6 2.5h4v2H6zM6 8h4M6 10.5h4' }]],
  models: [['path', { d: 'M8 2.5 9.4 6.6 13.5 8 9.4 9.4 8 13.5 6.6 9.4 2.5 8l4.1-1.4z' }]],
  appearance: [['circle', { cx: 8, cy: 8, r: 5 }], ['path', { d: 'M8 3a5 5 0 0 1 0 10z', ...FILLED }]],
  // figure legend keys
  dot: [['circle', { cx: 8, cy: 8, r: 3.5, ...FILLED }]],
  ring: [['circle', { cx: 8, cy: 8, r: 3.5 }]],
  cell: [['rect', { x: 4.5, y: 4.5, width: 7, height: 7, ...FILLED }]],
};

// Every icon name this module draws.
export const ICON_NAMES = Object.freeze(Object.keys(DRAWINGS));

// The icon for one champion state: the solid crown for the current champion,
// the open crown for a former champion (or a round leader the gate has not
// yet decided).
export const CROWN = Object.freeze({ current: 'crown', former: 'crown-former' });

// Build one icon as an <svg>. `opts`:
//   size   — side length in CSS pixels, or a CSS length; the stylesheet's
//            `.zi` default is 1em, so an icon in text matches the text size.
//   x, y   — place the icon inside a figure's <svg>: the icon's TOP-LEFT
//            corner in the figure's user units (needs a numeric `size`).
//   class  — extra classes.
export function icon(name, opts) {
  const drawing = DRAWINGS[name];
  if (!drawing) throw new Error('unknown icon: ' + name);
  const o = opts || {};
  const attrs = {
    class: 'zi zi-' + name + (o.class ? ' ' + o.class : ''),
    'data-icon': name,
    viewBox: '0 0 16 16',
    fill: 'none', stroke: 'currentColor', 'stroke-width': '1.5',
    'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    'aria-hidden': 'true', focusable: 'false',
  };
  if (o.size != null) { attrs.width = o.size; attrs.height = o.size; }
  if (o.x != null) attrs.x = o.x;
  if (o.y != null) attrs.y = o.y;
  return svgEl('svg', attrs, drawing.map(([tag, a]) => svgEl(tag, a)));
}

// The children for a label with an icon: the icon, a space, then the words.
// `after: true` puts the icon after the words. Pass the result as an
// element's children.
export function iconLabel(name, text, opts) {
  const o = opts || {};
  const mark = icon(name, o);
  if (text == null || text === '') return [mark];
  return o.after ? [String(text), ' ', mark] : [mark, ' ', String(text)];
}

// Set a long-lived node's children to an icon and words, writing only when
// either changed (the no-op-heartbeat rule for chrome patched in place).
// `name` may be null for words alone; empty words and a null name clear it.
// `opts.after` puts the icon after the words, as in iconLabel.
export function patchIconLabel(node, name, text, opts) {
  if (!node) return;
  const after = !!(opts && opts.after);
  const key = (name || '') + (after ? '>' : '|') + (text == null ? '' : String(text));
  if (node.getAttribute('data-icon-label') === key) return;
  node.setAttribute('data-icon-label', key);
  clearChildren(node);
  const kids = name ? iconLabel(name, text, { after }) : (text ? [String(text)] : []);
  for (const k of kids) node.appendChild(typeof k === 'string' ? document.createTextNode(k) : k);
}

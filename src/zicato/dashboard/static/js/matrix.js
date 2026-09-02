// js/matrix.js — the `dn-mtx` table grid, as primitives.
//
// Three surfaces present a presence grid in the same table grammar: the
// mutation surface (mutation site × generation), the field-diversity figure
// (mutation site × challenger), and the evals view (board entry × candidate).
// The vocabulary they share is the corner cell naming both axes, a header per
// column, a header per row, a body cell that is on or off, the mark drawn in an
// on cell, and the dot standing in for an off one. That vocabulary is defined
// here once, and `css/console.css` styles it once.
//
// What the three do NOT share is interaction: the mutation surface pins a row
// and a cell to drive its diff pane, the diversity figure rails the pair of
// challengers with the highest overlap, and the evals matrix appends ghost rows
// for proposed board entries and shades a scored cell by how much evidence
// stands behind it. So every primitive here takes the caller's own extra
// classes, attributes and children and adds nothing of its own beyond the
// shared class — a surface's divergence stays in that surface's module.
//
// `test/matrix_grid.test.mjs` holds all three to the DOM they rendered before
// the grammar was shared.

import { el, svgEl } from './core/dom.js';

// A primitive's shared class, then whatever the call site adds.
function withExtra(base, extra) { return extra ? base + ' ' + extra : base; }

// One grid node: the shared class, the call site's own attributes (`el` drops a
// null value, which is how an optional attribute is passed), and its children.
function gridNode(tag, base, opts, kids) {
  const o = opts || {};
  return el(tag, { class: withExtra(base, o.extra), ...(o.attrs || {}) }, kids);
}

// The grid root. A matrix is as wide as its column count, so it is expected to
// sit inside `matrixScroll`.
export function matrixTable(extra) {
  return el('table', { class: withExtra('dn-mtx', extra) });
}

// The cell above the row labels, where the two axes meet. It carries the text
// naming them, unless the surface stacks a second header row that leaves this
// one empty and hidden from a screen reader.
export function matrixCorner(text, opts) {
  const o = opts || {};
  return el('th', { class: withExtra('dn-mtx-corner', o.extra), text: text || null, ...(o.attrs || {}) });
}

// One column header.
export function matrixColumnHeader(opts, kids) {
  return gridNode('th', 'dn-mtx-gen', opts, kids);
}

// The label inside a column header: an anchor when the column routes somewhere,
// otherwise a span the surface may attach its own handler to.
export function matrixColumnLabel(text, opts) {
  const o = opts || {};
  return el(o.href ? 'a' : 'span',
    { class: withExtra('dn-mtx-genlink', o.extra), href: o.href || null, text });
}

// One row: the header cell down the left edge, then a body cell per column.
export function matrixRow(opts, kids) {
  return gridNode('tr', 'dn-mtx-row', opts, kids);
}

// A row's header cell.
export function matrixRowHeader(opts, kids) {
  const o = opts || {};
  return el('th', { class: withExtra('dn-mtx-site', o.extra), scope: 'row', ...(o.attrs || {}) }, kids);
}

// One body cell. `on` says the row's subject is present in this column, which
// is the distinction the whole grid exists to show.
export function matrixCell(on, opts, kids) {
  return gridNode('td', 'dn-mtx-cell' + (on ? ' dn-mtx-on' : ''), opts, kids);
}

// The frame a mark is drawn in — a square viewBox `size` units on a side,
// holding whatever glyph the surface draws for a present cell.
export function matrixMarkFrame(size, opts, kids) {
  const o = opts || {};
  return svgEl('svg', {
    class: withExtra('dn-mtx-mark', o.extra),
    width: size, height: size, viewBox: `0 0 ${size} ${size}`, role: 'img',
  }, kids);
}

// The plain mark: a rounded square, for a grid whose cells say only present or
// absent and carry no verdict.
export function matrixMark() {
  return matrixMarkFrame(16, null,
    [svgEl('rect', { x: 3, y: 3, width: 10, height: 10, rx: 2, class: 'dn-mtx-square' })]);
}

// What an off cell holds. The row and column labels already say which pairing
// it is, so the dot is hidden from a screen reader rather than read out per
// empty cell.
export function matrixBlank() {
  return el('span', { class: 'dn-mtx-blank', 'aria-hidden': 'true', text: '·' });
}

// The container a matrix scrolls sideways inside, so a grid wider than its
// panel never makes the page body scroll.
export function matrixScroll(table) {
  return el('div', { class: 'dn-table-scroll' }, [table]);
}

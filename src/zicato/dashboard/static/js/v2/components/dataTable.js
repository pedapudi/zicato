// v2/components/dataTable.js — the dense, drillable tabular primitive.
//
// This is the workhorse of the v2 information-dense surfaces: the
// experiment ledger (gen × hypothesis × verdict × Δscalar / Δdrift /
// Δpass × fired-rule), the per-entry table, and the per-judge table all
// render through here. The design language is ACM-derived density:
// monospace numerals, tight rhythm, semantic color that is ALWAYS
// redundant to a glyph or label (a colour-blind operator must read the
// same verdict from the text alone).
//
// Every row is drillable. That is not optional dressing — the ledger's
// whole job is to be a launchpad into the matchup / generation detail —
// so a row gets a hover state, `cursor: pointer`, role/tabindex for
// keyboard reach, and an explicit drill cue (a trailing chevron column)
// whenever an `onRowClick` is supplied.
//
// Pure factory. `dataTable(opts)` returns a detached <table> node; it
// holds no module state and can be called as many times as a view
// needs. Sorting re-renders the body in place (the <tbody> node keeps
// identity; only its rows are rebuilt) so a re-render is cheap.

import { el } from '../../core/dom.js';

// A monospace, sign-aware delta cell renderer the ledger leans on. The
// caller can use it directly from a column's `render`, or rely on a
// column's `semantic: 'delta'` shorthand (see below). Negative is an
// improvement in zicato's drift-loss world, but the *direction* of
// "good" is the caller's call via `improveWhenNegative`.
export function deltaCell(value, opts) {
  const o = opts || {};
  const improveWhenNegative = o.improveWhenNegative !== false; // default true
  if (typeof value !== 'number' || !isFinite(value)) {
    return el('span', { class: 'v2-dt-num v2-dt-delta v2-dt-delta-na' }, ['—']);
  }
  const improved = improveWhenNegative ? value < 0 : value > 0;
  const regressed = improveWhenNegative ? value > 0 : value < 0;
  const sentiment = value === 0 ? 'flat' : (improved ? 'improve' : 'regress');
  // Glyph is the redundant, color-independent signal.
  const glyph = value === 0 ? '·' : (improved ? '▼' : '▲');
  const sign = value > 0 ? '+' : (value < 0 ? '−' : '');
  const mag = Math.abs(value);
  const text = sign + mag.toFixed(o.digits == null ? 3 : o.digits);
  return el('span', {
    class: `v2-dt-num v2-dt-delta v2-dt-delta-${sentiment}`,
    'data-sentiment': sentiment,
    'aria-label': `${sentiment} ${text}`,
  }, [
    el('span', { class: 'v2-dt-delta-glyph', 'aria-hidden': 'true' }, [glyph]),
    el('span', { class: 'v2-dt-delta-val' }, [text]),
  ]);
}

// Coerce a column's `render(row)` output (or its `value(row)`) into a
// DOM node. A bare string / number becomes a text node; a Node passes
// through. `semantic: 'delta'` short-circuits to deltaCell.
function _renderCellBody(col, row, rowIndex) {
  if (col.semantic === 'delta') {
    const v = typeof col.value === 'function' ? col.value(row) : row[col.key];
    return deltaCell(v, { improveWhenNegative: col.improveWhenNegative, digits: col.digits });
  }
  if (typeof col.render === 'function') {
    const out = col.render(row, rowIndex);
    return out == null ? document.createTextNode('') : out;
  }
  const v = typeof col.value === 'function' ? col.value(row) : row[col.key];
  return document.createTextNode(v == null ? '' : String(v));
}

// The value a sort comparator keys on for a column. Prefer an explicit
// `sortValue(row)`, then `value(row)`, then `row[key]`.
function _sortKey(col, row) {
  if (typeof col.sortValue === 'function') return col.sortValue(row);
  if (typeof col.value === 'function') return col.value(row);
  return row[col.key];
}

function _compare(a, b) {
  // Nullish sorts last regardless of direction sign (handled by caller
  // re-inverting); here we keep a stable numeric/string ordering.
  const an = a == null || (typeof a === 'number' && !isFinite(a));
  const bn = b == null || (typeof b === 'number' && !isFinite(b));
  if (an && bn) return 0;
  if (an) return 1;
  if (bn) return -1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b));
}

/**
 * Build a dense, drillable data table.
 *
 * opts:
 *   columns      — array of column descriptors:
 *       { key, header,
 *         align?: 'left'|'right'|'center'   (default: numeric→right via `mono`)
 *         mono?: boolean                    — monospace + right-align numerals
 *         render?: (row, i) => Node|string  — custom cell body
 *         value?: (row) => any              — datum (for default render + sort)
 *         semantic?: 'delta'                — render via deltaCell
 *         improveWhenNegative?: boolean     — delta sentiment direction
 *         digits?: number                   — delta decimal places
 *         sortable?: boolean                — header click sorts (default: true)
 *         sortValue?: (row) => any          — explicit sort key
 *         width?: string                    — CSS width hint
 *       }
 *   rows         — array of row data objects.
 *   onRowClick   — (row, i, ev) => void. When present every row is
 *                  drillable: pointer cursor, hover state, keyboard
 *                  Enter/Space, and a trailing drill-cue column.
 *   sort         — initial { key, dir: 'asc'|'desc' } (optional).
 *   rowKey       — (row, i) => string|number, a stable key (default: i).
 *   caption      — optional <caption> text.
 *   ariaLabel    — optional table aria-label.
 *   dense        — boolean (default true) — the tight ACM rhythm.
 *   emptyText    — text shown when there are no rows (default '—').
 *
 * Returns a detached <table> node.
 */
export function dataTable(opts) {
  const o = opts || {};
  const columns = Array.isArray(o.columns) ? o.columns : [];
  const rows = Array.isArray(o.rows) ? o.rows : [];
  const onRowClick = typeof o.onRowClick === 'function' ? o.onRowClick : null;
  const rowKey = typeof o.rowKey === 'function' ? o.rowKey : (_row, i) => i;
  const dense = o.dense !== false;
  const emptyText = o.emptyText == null ? '—' : o.emptyText;

  // Sort state lives on the closure; header clicks mutate it then
  // rebuild the tbody in place.
  let sort = (o.sort && o.sort.key) ? { key: o.sort.key, dir: o.sort.dir === 'asc' ? 'asc' : 'desc' } : null;

  const tbl = el('table', {
    class: 'v2-dt' + (dense ? ' v2-dt-dense' : '') + (onRowClick ? ' v2-dt-drillable' : ''),
    'aria-label': o.ariaLabel || 'data table',
  });
  if (o.caption) tbl.appendChild(el('caption', { class: 'v2-dt-caption' }, [o.caption]));

  const thead = el('thead');
  const headRow = el('tr', { class: 'v2-dt-head-row' });
  for (const col of columns) {
    const sortable = col.sortable !== false && (col.key != null || typeof col.sortValue === 'function');
    const alignClass = _alignClass(col);
    const isSorted = sort && sort.key === col.key;
    const th = el('th', {
      class: `v2-dt-th ${alignClass}` + (sortable ? ' v2-dt-th-sortable' : '') + (isSorted ? ' v2-dt-th-sorted' : ''),
      style: col.width ? `width:${col.width}` : null,
      'aria-sort': isSorted ? (sort.dir === 'asc' ? 'ascending' : 'descending') : (sortable ? 'none' : null),
      role: sortable ? 'columnheader' : null,
      tabindex: sortable ? '0' : null,
    }, [
      el('span', { class: 'v2-dt-th-label' }, [String(col.header == null ? (col.key || '') : col.header)]),
      sortable ? el('span', {
        class: 'v2-dt-sort-cue' + (isSorted ? ' v2-dt-sort-cue-active' : ''),
        'aria-hidden': 'true',
      }, [isSorted ? (sort.dir === 'asc' ? '▲' : '▼') : '↕']) : null,
    ]);
    if (sortable) {
      const doSort = () => {
        if (sort && sort.key === col.key) {
          sort = { key: col.key, dir: sort.dir === 'asc' ? 'desc' : 'asc' };
        } else {
          sort = { key: col.key, dir: 'desc' };
        }
        _rebuildHead();
        _rebuildBody();
      };
      th.addEventListener('click', doSort);
      th.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); doSort(); }
      });
    }
    headRow.appendChild(th);
  }
  // Drill-cue header column (empty header, holds the chevron column).
  if (onRowClick) headRow.appendChild(el('th', { class: 'v2-dt-th v2-dt-th-drill', 'aria-hidden': 'true' }, ['']));
  thead.appendChild(headRow);
  tbl.appendChild(thead);

  const tbody = el('tbody', { class: 'v2-dt-body' });
  tbl.appendChild(tbody);

  function _rebuildHead() {
    // Repaint the sort cues / aria-sort without rebuilding listeners:
    // the simplest correct approach is to refresh classes on the
    // existing <th> nodes by index.
    const ths = headRow.children;
    for (let i = 0; i < columns.length; i++) {
      const col = columns[i];
      const th = ths[i];
      if (!th) continue;
      const isSorted = sort && sort.key === col.key;
      th.classList.toggle('v2-dt-th-sorted', !!isSorted);
      // The sort cue is the th's last child span; refresh its glyph.
      const cueNode = th.childNodes[th.childNodes.length - 1];
      if (cueNode && cueNode.classList && cueNode.classList.contains('v2-dt-sort-cue')) {
        cueNode.classList.toggle('v2-dt-sort-cue-active', !!isSorted);
        cueNode.textContent = isSorted ? (sort.dir === 'asc' ? '▲' : '▼') : '↕';
      }
      if (th.classList.contains('v2-dt-th-sortable')) {
        th.setAttribute('aria-sort', isSorted ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
      }
    }
  }

  function _orderedRows() {
    if (!sort) return rows.map((r, i) => ({ r, i }));
    const col = columns.find((c) => c.key === sort.key);
    if (!col) return rows.map((r, i) => ({ r, i }));
    const decorated = rows.map((r, i) => ({ r, i, k: _sortKey(col, r) }));
    decorated.sort((x, y) => {
      const c = _compare(x.k, y.k);
      if (c !== 0) return sort.dir === 'asc' ? c : -c;
      return x.i - y.i; // stable
    });
    return decorated;
  }

  function _buildRow(r, i) {
    const tr = el('tr', { class: 'v2-dt-row' });
    tr.setAttribute('data-key', String(rowKey(r, i)));
    for (const col of columns) {
      const td = el('td', { class: `v2-dt-td ${_alignClass(col)}` }, [_renderCellBody(col, r, i)]);
      tr.appendChild(td);
    }
    if (onRowClick) {
      tr.classList.add('v2-dt-row-drillable');
      tr.setAttribute('role', 'button');
      tr.setAttribute('tabindex', '0');
      tr.appendChild(el('td', {
        class: 'v2-dt-td v2-dt-td-drill',
        'aria-hidden': 'true',
      }, [el('span', { class: 'v2-dt-drill-cue' }, ['›'])]));
      const fire = (ev) => onRowClick(r, i, ev);
      tr.addEventListener('click', fire);
      tr.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); fire(ev); }
      });
    }
    return tr;
  }

  function _rebuildBody() {
    // Rebuild rows in place: keep the <tbody> node identity, swap its
    // children. Cheap and re-render-safe for the caller's container.
    while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
    const ordered = _orderedRows();
    if (ordered.length === 0) {
      const span = columns.length + (onRowClick ? 1 : 0);
      const tr = el('tr', { class: 'v2-dt-row v2-dt-empty-row' }, [
        el('td', { class: 'v2-dt-td v2-dt-empty-cell', colspan: String(span || 1) }, [emptyText]),
      ]);
      tbody.appendChild(tr);
      return;
    }
    for (const { r, i } of ordered) tbody.appendChild(_buildRow(r, i));
  }

  _rebuildBody();
  return tbl;
}

function _alignClass(col) {
  if (col.align === 'right') return 'v2-dt-right';
  if (col.align === 'center') return 'v2-dt-center';
  if (col.align === 'left') return 'v2-dt-left';
  if (col.mono || col.semantic === 'delta') return 'v2-dt-right v2-dt-mono';
  return 'v2-dt-left';
}

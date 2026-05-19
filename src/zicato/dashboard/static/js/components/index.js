// components/index.js — the shared component library.
//
// Every component is a pure factory: it returns a detached DOM node and
// never mounts itself. Components are re-render-safe — calling a factory
// again with new data yields a fresh node the caller reconciles via the
// core/dom.js spine. Phase-2 view agents import from here; they never
// edit this file.

import { el, svgEl, patchText, clearChildren } from '../core/dom.js';
import { COLORS, fmtScalar } from '../core/format.js';

// -- empty state --------------------------------------------------

// The single-line muted empty state. Compact: not a tall placeholder.
export function emptyLine(text) {
  return el('p', { class: 'empty' }, [text || 'Nothing here yet.']);
}

// -- badge --------------------------------------------------------

// kind ∈ ok | warn | err | muted | pending | info | promoted | rejected
const _BADGE_KINDS = new Set([
  'ok', 'warn', 'err', 'muted', 'pending', 'info', 'promoted', 'rejected', 'deferred', 'running',
]);

export function badge(text, kind) {
  const k = _BADGE_KINDS.has(kind) ? kind : 'muted';
  return el('span', { class: `badge ${k}`, role: 'status' }, [String(text)]);
}

// Map a run / board-entry status to a badge.
export function statusBadge(status) {
  const s = String(status || '').toLowerCase();
  if (s === 'done' || s === 'completed' || s === 'passed' || s === 'pass') {
    return badge(status || 'done', 'ok');
  }
  if (s === 'failed' || s === 'error' || s === 'fail') return badge(status || 'failed', 'err');
  if (s === 'running' || s === 'in_progress' || s === 'active') {
    return badge(status || 'running', 'running');
  }
  if (s === 'pending' || s === 'queued' || s === 'waiting') {
    return badge(status || 'pending', 'pending');
  }
  if (s === 'promoted') return badge('promoted', 'promoted');
  if (s === 'rejected') return badge('rejected', 'rejected');
  return badge(status || '—', 'muted');
}

// A verdict pill distinguishing regression from near-miss from win.
// verdict ∈ promoted | rejected | regression | near_miss | tbd
export function verdictBadge(verdict) {
  const v = String(verdict || '').toLowerCase();
  const label = {
    promoted: 'promoted', rejected: 'rejected', regression: 'regression',
    near_miss: 'near-miss', tbd: 'pending',
  }[v] || verdict || '—';
  const kind = {
    promoted: 'promoted', rejected: 'rejected', regression: 'err',
    near_miss: 'warn', tbd: 'pending',
  }[v] || 'muted';
  return badge(label, kind);
}

// -- card ---------------------------------------------------------

// card({ title, meta, body, key, className }) → <section class="card">.
// `body` may be a node, an array of nodes, or a string.
export function card({ title, meta, body, key, className } = {}) {
  const head = el('div', { class: 'card-header' }, [
    title ? el('h3', { class: 'card-title' }, [title]) : null,
    meta != null ? el('span', { class: 'card-meta meta mono' },
      [typeof meta === 'string' ? meta : null].filter(Boolean)) : null,
  ]);
  if (meta != null && typeof meta !== 'string') head.lastChild.appendChild(meta);
  const bodyNode = el('div', { class: 'card-body' });
  appendBody(bodyNode, body);
  const node = el('section', {
    class: 'card' + (className ? ' ' + className : ''),
  }, [head, bodyNode]);
  if (key != null) node.setAttribute('data-key', String(key));
  return node;
}

function appendBody(host, body) {
  if (body == null) return;
  if (Array.isArray(body)) {
    for (const b of body) { if (b != null) host.appendChild(toNode(b)); }
  } else {
    host.appendChild(toNode(body));
  }
}

function toNode(x) {
  return typeof x === 'string' ? document.createTextNode(x) : x;
}

// -- key/value row ------------------------------------------------

// A label : value pair, the workhorse of identity / contract panels.
export function kv(label, value) {
  return el('div', { class: 'kv-row' }, [
    el('span', { class: 'kv-label' }, [String(label)]),
    el('span', { class: 'kv-value mono' },
      [value == null ? '—' : (typeof value === 'string' ? value : null)].filter((x) => x != null)
        .concat(value != null && typeof value !== 'string' ? [value] : [])),
  ]);
}

// -- table --------------------------------------------------------

// table({ columns, rows, key, onRowClick }) → a keyed <table>.
//   columns: [{ key, label, className?, render?(row)->node|string }]
//   rows:    [{ key, ...cells }]
// Rows carry data-key so the caller can reconcile them incrementally.
export function table({ columns, rows, key, onRowClick, emptyText } = {}) {
  const cols = columns || [];
  const thead = el('thead', null, [
    el('tr', null, cols.map((c) =>
      el('th', { class: c.className || null }, [c.label != null ? String(c.label) : '']))),
  ]);
  const tbody = el('tbody');
  for (const row of (rows || [])) {
    tbody.appendChild(buildTableRow(cols, row, onRowClick));
  }
  if (!(rows && rows.length)) {
    tbody.appendChild(el('tr', { class: 'table-empty' }, [
      el('td', { colspan: String(cols.length || 1) }, [emptyText || 'No rows.']),
    ]));
  }
  const node = el('table', { class: 'data-table' }, [thead, tbody]);
  if (key != null) node.setAttribute('data-key', String(key));
  return node;
}

export function buildTableRow(columns, row, onRowClick) {
  const tr = el('tr', null, columns.map((c) => {
    let cell;
    if (typeof c.render === 'function') cell = c.render(row);
    else cell = row[c.key];
    return el('td', { class: c.className || null },
      [cell == null ? '—' : (typeof cell === 'string' ? cell : null)]
        .filter((x) => x != null)
        .concat(cell != null && typeof cell !== 'string' ? [cell] : []));
  }));
  if (row.key != null) tr.setAttribute('data-key', String(row.key));
  if (typeof onRowClick === 'function') {
    tr.classList.add('clickable');
    tr.setAttribute('role', 'button');
    tr.setAttribute('tabindex', '0');
    tr.addEventListener('click', () => onRowClick(row));
    tr.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onRowClick(row); }
    });
  }
  return tr;
}

// -- diff renderer ------------------------------------------------

// A line-level diff. mode ∈ unified | split. Returns a <div class=diff>.
// The algorithm is a simple LCS — adequate for source-snapshot diffs.
export function diff(oldText, newText, { mode = 'unified' } = {}) {
  const a = String(oldText == null ? '' : oldText).split('\n');
  const b = String(newText == null ? '' : newText).split('\n');
  const ops = lcsDiff(a, b);
  const wrap = el('div', { class: `diff diff-${mode}` });
  if (mode === 'split') {
    const left = el('div', { class: 'diff-side diff-old' });
    const right = el('div', { class: 'diff-side diff-new' });
    for (const op of ops) {
      if (op.kind === 'equal') {
        left.appendChild(diffLine(op.a, 'equal'));
        right.appendChild(diffLine(op.b, 'equal'));
      } else if (op.kind === 'del') {
        left.appendChild(diffLine(op.a, 'del'));
        right.appendChild(diffLine('', 'pad'));
      } else {
        left.appendChild(diffLine('', 'pad'));
        right.appendChild(diffLine(op.b, 'add'));
      }
    }
    wrap.appendChild(left);
    wrap.appendChild(right);
  } else {
    for (const op of ops) {
      if (op.kind === 'equal') wrap.appendChild(diffLine(op.a, 'equal'));
      else if (op.kind === 'del') wrap.appendChild(diffLine(op.a, 'del'));
      else wrap.appendChild(diffLine(op.b, 'add'));
    }
  }
  return wrap;
}

function diffLine(text, kind) {
  const sign = { del: '-', add: '+', equal: ' ', pad: '' }[kind] || ' ';
  return el('div', { class: `diff-line diff-${kind}` }, [
    el('span', { class: 'diff-gutter' }, [sign]),
    el('span', { class: 'diff-text' }, [text || ' ']),
  ]);
}

// Classic LCS line diff → [{ kind:'equal'|'del'|'add', a?, b? }].
export function lcsDiff(a, b) {
  const n = a.length;
  const m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { ops.push({ kind: 'equal', a: a[i], b: b[j] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push({ kind: 'del', a: a[i] }); i++; }
    else { ops.push({ kind: 'add', b: b[j] }); j++; }
  }
  while (i < n) { ops.push({ kind: 'del', a: a[i] }); i++; }
  while (j < m) { ops.push({ kind: 'add', b: b[j] }); j++; }
  return ops;
}

// -- line chart ---------------------------------------------------

// Paint a polyline chart into a provided <svg>. Incremental: the svg is
// cleared and repainted, but the <svg> node identity is preserved by
// the caller so layout does not jump.
//   lineChart({ svg, points, x, y, width, height, color })
// `points` is an array; `x(pt,i)` and `y(pt,i)` map to numbers.
export function lineChart({ svg, points, x, y, width = 720, height = 220,
  color = COLORS.running, pad = 28 } = {}) {
  if (!svg) return;
  clearChildren(svg);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const pts = (points || []).map((p, i) => ({ x: x(p, i), y: y(p, i) }))
    .filter((p) => isFinite(p.x) && isFinite(p.y));
  if (pts.length === 0) {
    svg.appendChild(svgEl('text', {
      x: width / 2, y: height / 2, 'text-anchor': 'middle',
      class: 'chart-empty',
    }, ['no data']));
    return;
  }
  const xs = pts.map((p) => p.x);
  const ys = pts.map((p) => p.y);
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
  const sx = (v) => pad + (xMax === xMin ? 0.5 : (v - xMin) / (xMax - xMin)) * (width - 2 * pad);
  const sy = (v) => height - pad - (yMax === yMin ? 0.5 : (v - yMin) / (yMax - yMin)) * (height - 2 * pad);
  // axes
  svg.appendChild(svgEl('line', {
    x1: pad, y1: height - pad, x2: width - pad, y2: height - pad,
    stroke: COLORS.grid, 'stroke-width': 1,
  }));
  svg.appendChild(svgEl('line', {
    x1: pad, y1: pad, x2: pad, y2: height - pad,
    stroke: COLORS.grid, 'stroke-width': 1,
  }));
  // polyline
  const d = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${sx(p.x)},${sy(p.y)}`).join(' ');
  svg.appendChild(svgEl('path', {
    d, fill: 'none', stroke: color, 'stroke-width': 2,
  }));
  // markers
  for (const p of pts) {
    svg.appendChild(svgEl('circle', {
      cx: sx(p.x), cy: sy(p.y), r: 3, fill: color,
    }));
  }
}

// -- progress meter ----------------------------------------------

// progress(value0to1, { label, danger }) → a meter row.
export function progressMeter(value, { label, danger = false } = {}) {
  const pct = (typeof value === 'number' && isFinite(value))
    ? Math.max(0, Math.min(1, value)) : 0;
  const bar = el('div', {
    class: 'meter-fill' + (danger ? ' meter-danger' : ''),
  });
  bar.style.width = (pct * 100).toFixed(1) + '%';
  return el('div', { class: 'meter' }, [
    el('div', { class: 'meter-track' }, [bar]),
    label != null ? el('span', { class: 'meter-label mono' }, [String(label)]) : null,
  ]);
}

// -- scalar delta cell -------------------------------------------

// A signed-Δ cell coloured by direction (improvement is negative loss).
export function deltaCell(delta, { goodIsNegative = true } = {}) {
  if (typeof delta !== 'number' || !isFinite(delta)) {
    return el('span', { class: 'delta-cell mono' }, ['—']);
  }
  const good = goodIsNegative ? delta < 0 : delta > 0;
  const cls = delta === 0 ? 'delta-flat' : (good ? 'delta-good' : 'delta-bad');
  const sign = delta >= 0 ? '+' : '';
  return el('span', { class: `delta-cell mono ${cls}` }, [sign + delta.toFixed(3)]);
}

export { fmtScalar };

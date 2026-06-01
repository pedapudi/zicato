// variants/H/patchDiff.js — themed patch-diff cards (Atlas II).
//
// Ported in (self-contained) from js/v2/components/patchDiff.js — the same
// dependency-free LCS line diff, re-skinned to H's `hd-*` token classes so
// it reads red/green in all three themes. The mutation-sites view uses
// `mutationPatchCard` to render WHAT each generation changed at a site (the
// patch's op + rationale + new content, rendered as a line diff). A pure
// factory: returns a detached node, holds no module state.

import { el } from '../../core/dom.js';

// A tiny LCS line diff (O(n·m); the payloads are tens of lines).
function lineDiff(oldText, newText) {
  const a = String(oldText == null ? '' : oldText).split('\n');
  const b = String(newText == null ? '' : newText).split('\n');
  if (a.length > 1 && a[a.length - 1] === '') a.pop();
  if (b.length > 1 && b[b.length - 1] === '') b.pop();

  const n = a.length;
  const m = b.length;
  const lcs = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const rows = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { rows.push({ kind: 'ctx', text: a[i], oldNo: i + 1, newNo: j + 1 }); i++; j++; }
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) { rows.push({ kind: 'del', text: a[i], oldNo: i + 1, newNo: null }); i++; }
    else { rows.push({ kind: 'add', text: b[j], oldNo: null, newNo: j + 1 }); j++; }
  }
  while (i < n) { rows.push({ kind: 'del', text: a[i], oldNo: i + 1, newNo: null }); i++; }
  while (j < m) { rows.push({ kind: 'add', text: b[j], oldNo: null, newNo: j + 1 }); j++; }
  return rows;
}

const _GLYPH = { add: '+', del: '−', ctx: ' ' };

function diffBody(oldText, newText) {
  const rows = lineDiff(oldText, newText);
  let added = 0;
  let removed = 0;
  const body = el('div', { class: 'hd-body', role: 'list' });
  for (const r of rows) {
    if (r.kind === 'add') added++;
    else if (r.kind === 'del') removed++;
    body.appendChild(el('div', { class: 'hd-line hd-' + r.kind, role: 'listitem' }, [
      el('span', { class: 'hd-gutter', 'aria-hidden': 'true' }, [r.oldNo == null ? '' : String(r.oldNo)]),
      el('span', { class: 'hd-sign', 'aria-hidden': 'true' }, [_GLYPH[r.kind]]),
      el('span', { class: 'hd-text' }, [r.text === '' ? '​' : r.text]),
    ]));
  }
  return { body, added, removed };
}

function statChip(added, removed) {
  return el('span', { class: 'hd-stat' }, [
    el('span', { class: 'hd-stat-add' }, ['+' + added]),
    ' ',
    el('span', { class: 'hd-stat-del' }, ['−' + removed]),
  ]);
}

// mutationPatchCard(patch, opts)
//   patch — { id, mutation_id, op, new_content, new_numeric, new_enum,
//             rationale } plus an optional { generation_id, old_content }.
//   opts  — { generationId, oldContent, path }.
export function mutationPatchCard(patch, opts) {
  const p = patch || {};
  const o = opts || {};
  const mid = p.mutation_id != null ? String(p.mutation_id) : (p.id != null ? String(p.id) : '');
  const op = String(p.op || 'replace');

  const head = el('div', { class: 'hd-head' }, [
    el('span', { class: 'hd-mid' }, [mid || '(unnamed mutation)']),
    el('span', { class: 'hd-op' }, [op]),
    o.generationId ? el('span', { class: 'hd-gen' }, ['gen ' + o.generationId]) : null,
    o.path ? el('span', { class: 'hd-path' }, [String(o.path)]) : null,
  ].filter(Boolean));

  const rationale = (typeof p.rationale === 'string' && p.rationale.trim())
    ? el('p', { class: 'hd-rationale' }, [el('span', { class: 'hd-rationale-lead' }, ['Why. ']), p.rationale.trim()])
    : null;

  const card = el('div', { class: 'hd-card', tabindex: '0', role: 'group',
    'aria-label': 'patch to ' + (mid || 'a mutation point') }, [head, rationale].filter(Boolean));

  // numeric / enum set → a value chip; else a line diff of new content.
  if (op === 'set_numeric' && p.new_numeric != null) {
    head.appendChild(statChip(1, 0));
    card.appendChild(el('p', { class: 'hd-note' }, ['new numeric value']));
    const { body } = diffBody('', String(p.new_numeric));
    card.appendChild(body);
    return card;
  }
  if (op === 'set_enum' && p.new_enum != null) {
    head.appendChild(statChip(1, 0));
    card.appendChild(el('p', { class: 'hd-note' }, ['new enum value']));
    const { body } = diffBody('', String(p.new_enum));
    card.appendChild(body);
    return card;
  }

  const oldText = o.oldContent != null ? String(o.oldContent) : '';
  const newText = p.new_content == null ? '' : String(p.new_content);
  const { body, added, removed } = diffBody(oldText, newText);
  head.appendChild(statChip(added, removed));
  if (oldText === '') {
    card.appendChild(el('p', { class: 'hd-note' }, ['new instruction text (baseline shown as added)']));
  }
  card.appendChild(body);
  return card;
}

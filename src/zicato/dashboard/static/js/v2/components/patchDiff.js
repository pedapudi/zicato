// js/v2/components/patchDiff.js — the CAUSE panel's diff viewer.
//
// DASHBOARD-V2 §3: the dashboard is GRAPHICAL & INTERACTIVE. The
// Experiment view reads like a causal sentence — a CODE CHANGE caused a
// BEHAVIORAL CHANGE that earned a VERDICT — and this is the first clause:
// WHAT changed. It restores the patch/diff viewer the first v2 attempt
// dropped, rendered as a themed red/green line diff that speaks the
// --v2-* token language (so it restyles across all three themes), NOT
// the v1 atoms' own (v1-token) CSS.
//
// Two faces, one per data source:
//   * mutationPatchCard(patch) — a structured Patch
//       ({ id, mutation_id, op, new_content, new_numeric, new_enum,
//          rationale }) from the experiment record / GET …/patches. This
//       is the *intent*: the mutation-point id, the op, the operator's
//       rationale, and the new value (the text/number/enum the patch set).
//   * fileDiffCard(file) — a file delta
//       ({ path, status, old_content, new_content, … }) from
//       GET /api/files/{epoch}/{gen}/diff. This is the *realized* change:
//       a line-level red/green diff of the instruction / tool-description
//       text that actually moved on disk.
//
// Interactivity (the point): every hunk / mutation id carries a
// `data-mutation` (or `data-path`) hook and fires an `onHover(id)` /
// `onHoverEnd()` so the EFFECT panel can highlight the entries / drift
// kinds a change plausibly moved. Hover is redundant to a persistent
// click-to-pin in the view; this component only emits, never owns state.
//
// Pure factory: returns a detached node; holds no module state.

import { el } from '../../core/dom.js';

// A tiny longest-common-subsequence line diff. The payloads here are
// instruction / tool-description blocks (tens of lines), so an O(n·m)
// table is well within budget and keeps the diff dependency-free.
function lineDiff(oldText, newText) {
  const a = String(oldText == null ? '' : oldText).split('\n');
  const b = String(newText == null ? '' : newText).split('\n');
  // Strip a single trailing empty line that split() leaves on a file
  // ending in "\n" — it would otherwise read as a spurious context line.
  if (a.length > 1 && a[a.length - 1] === '') a.pop();
  if (b.length > 1 && b[b.length - 1] === '') b.pop();

  const n = a.length;
  const m = b.length;
  // lcs[i][j] = LCS length of a[i:] and b[j:].
  const lcs = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j]
        ? lcs[i + 1][j + 1] + 1
        : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const rows = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ kind: 'ctx', text: a[i], oldNo: i + 1, newNo: j + 1 });
      i++; j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      rows.push({ kind: 'del', text: a[i], oldNo: i + 1, newNo: null });
      i++;
    } else {
      rows.push({ kind: 'add', text: b[j], oldNo: null, newNo: j + 1 });
      j++;
    }
  }
  while (i < n) { rows.push({ kind: 'del', text: a[i], oldNo: i + 1, newNo: null }); i++; }
  while (j < m) { rows.push({ kind: 'add', text: b[j], oldNo: null, newNo: j + 1 }); j++; }
  return rows;
}

const _GLYPH = { add: '+', del: '−', ctx: ' ' };

function diffBody(oldText, newText, opts) {
  const o = opts || {};
  const rows = lineDiff(oldText, newText);
  let added = 0;
  let removed = 0;
  const body = el('div', { class: 'v2-diff-body', role: 'list' });
  for (const r of rows) {
    if (r.kind === 'add') added++;
    else if (r.kind === 'del') removed++;
    const line = el('div', { class: 'v2-diff-line v2-diff-' + r.kind, role: 'listitem' }, [
      el('span', { class: 'v2-diff-gutter v2-diff-gutter-old', 'aria-hidden': 'true' }, [r.oldNo == null ? '' : String(r.oldNo)]),
      el('span', { class: 'v2-diff-gutter v2-diff-gutter-new', 'aria-hidden': 'true' }, [r.newNo == null ? '' : String(r.newNo)]),
      el('span', { class: 'v2-diff-sign', 'aria-hidden': 'true' }, [_GLYPH[r.kind]]),
      // Render the line text as a child text node so an empty line still
      // occupies a row (the &nbsp; equivalent is min-height in CSS).
      el('span', { class: 'v2-diff-text' }, [r.text === '' ? '​' : r.text]),
    ]);
    body.appendChild(line);
  }
  if (o.maxHeight) body.classList.add('v2-diff-body-scroll');
  return { body, added, removed };
}

// Wire the hover emit/clear onto a card root so a parent can light up the
// entries / drift kinds the change plausibly moved.
function wireHover(node, id, hooks) {
  if (!hooks) return node;
  const enter = () => { if (typeof hooks.onHover === 'function') hooks.onHover(id); };
  const leave = () => { if (typeof hooks.onHoverEnd === 'function') hooks.onHoverEnd(id); };
  node.addEventListener('mouseenter', enter);
  node.addEventListener('focusin', enter);
  node.addEventListener('mouseleave', leave);
  node.addEventListener('focusout', leave);
  return node;
}

// The label for a patch op + its new value, in human terms.
function _opLine(patch) {
  const op = String(patch.op || 'replace');
  if (op === 'set_numeric' && patch.new_numeric != null) {
    return { verb: 'set numeric', value: String(patch.new_numeric), text: null };
  }
  if (op === 'set_enum' && patch.new_enum != null) {
    return { verb: 'set enum', value: String(patch.new_enum), text: null };
  }
  // replace (or anything text-bearing) — the new instruction / tool text.
  return { verb: 'replace', value: null, text: patch.new_content == null ? '' : String(patch.new_content) };
}

/**
 * mutationPatchCard — the structured-intent face.
 *
 * patch — { id, mutation_id, op, new_content, new_numeric, new_enum,
 *           rationale } (a Patch dataclass on the wire).
 * fileDiff — optional matching file delta ({ old_content, new_content })
 *   from the diff endpoint; when present the card renders the realized
 *   red/green line diff instead of just the new value. The mutation_id is
 *   the hover key the EFFECT panel listens on.
 * hooks — { onHover(mutationId), onHoverEnd(mutationId) }.
 */
export function mutationPatchCard(patch, fileDiff, hooks) {
  const p = patch || {};
  const mid = p.mutation_id != null ? String(p.mutation_id) : (p.id != null ? String(p.id) : '');
  const opl = _opLine(p);

  const head = el('div', { class: 'v2-diff-head' }, [
    el('span', { class: 'v2-diff-mid v2-mono', 'data-mutation': mid }, [mid || '(unnamed mutation)']),
    el('span', { class: 'v2-diff-op v2-mono' }, [opl.verb]),
  ]);

  const rationale = (typeof p.rationale === 'string' && p.rationale.trim())
    ? el('p', { class: 'v2-diff-rationale' }, [
        el('span', { class: 'v2-diff-rationale-lead' }, ['Why. ']), p.rationale.trim(),
      ])
    : null;

  const card = el('div', {
    class: 'v2-diff-card', 'data-mutation': mid, tabindex: '0', role: 'group',
    'aria-label': 'patch to ' + (mid || 'a mutation point'),
  }, [head, rationale]);

  // A numeric / enum set has no line diff — render the value as a chip.
  if (opl.value != null) {
    card.appendChild(el('div', { class: 'v2-diff-scalarset' }, [
      el('span', { class: 'v2-diff-scalarset-label' }, ['new value']),
      el('span', { class: 'v2-diff-scalarset-val v2-mono' }, [opl.value]),
    ]));
    return wireHover(card, mid, hooks);
  }

  // A text replace: render the realized line diff when the file delta is
  // available (old vs new), else the new text alone (intent only).
  if (fileDiff && (fileDiff.old_content != null || fileDiff.new_content != null)) {
    const { body, added, removed } = diffBody(fileDiff.old_content, fileDiff.new_content, { maxHeight: true });
    if (fileDiff.path) {
      head.appendChild(el('span', { class: 'v2-diff-path v2-mono', 'data-path': String(fileDiff.path) }, [String(fileDiff.path)]));
    }
    head.appendChild(_statChip(added, removed));
    card.appendChild(body);
  } else {
    // Intent-only: the patch's new text, as all-added (the parent text is
    // unknown without the diff endpoint). Still a themed, line-numbered
    // diff so the CAUSE reads visually, not as a prose blob.
    const { body, added } = diffBody('', opl.text, { maxHeight: true });
    head.appendChild(_statChip(added, 0));
    card.appendChild(el('p', { class: 'v2-diff-note' }, ['new instruction text (parent unavailable — shown as added)']));
    card.appendChild(body);
  }
  return wireHover(card, mid, hooks);
}

// A "+N −M" line-count chip, color-redundant to the glyphs.
function _statChip(added, removed) {
  return el('span', { class: 'v2-diff-stat v2-mono', 'aria-label': `${added} added, ${removed} removed` }, [
    el('span', { class: 'v2-diff-stat-add' }, ['+' + added]),
    ' ',
    el('span', { class: 'v2-diff-stat-del' }, ['−' + removed]),
  ]);
}

const _STATUS_WORD = { added: 'added', removed: 'removed', modified: 'modified' };

/**
 * fileDiffCard — the realized-change face, keyed on a file path.
 *
 * file — { path, status, old_content, new_content } from the diff
 *   endpoint. The path is the hover key.
 * hooks — { onHover(path), onHoverEnd(path) }.
 */
export function fileDiffCard(file, hooks) {
  const f = file || {};
  const path = f.path != null ? String(f.path) : '';
  const status = _STATUS_WORD[f.status] || 'modified';
  const { body, added, removed } = diffBody(f.old_content, f.new_content, { maxHeight: true });
  const head = el('div', { class: 'v2-diff-head' }, [
    el('span', { class: 'v2-diff-path v2-mono', 'data-path': path }, [path || '(file)']),
    el('span', { class: 'v2-diff-op v2-diff-status-' + status + ' v2-mono' }, [status]),
    _statChip(added, removed),
  ]);
  const card = el('div', {
    class: 'v2-diff-card', 'data-path': path, tabindex: '0', role: 'group',
    'aria-label': status + ' ' + (path || 'file'),
  }, [head, body]);
  return wireHover(card, path, hooks);
}

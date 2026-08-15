// Shared transcript renderer for board comparison, imported traces, and live follow.

import { el } from './core/dom.js';

export function buildTurnNode(t, annBySeq, execution) {
  const turn = el('div', { class: 'dn-turn dn-turn-' + (t.role || 'agent') }, [
    el('div', { class: 'dn-turn-head dn-faint dn-mono' }, [
      el('span', { text: t.agent || t.role || 'turn' }),
      t.kind ? el('span', { text: ' · ' + t.kind }) : null,
    ].filter(Boolean)),
    t.text ? el('div', { class: 'dn-turn-text', text: t.text }) : null,
  ].filter(Boolean));
  if (Array.isArray(t.tool_calls)) for (const tc of t.tool_calls) {
    turn.appendChild(el('div', { class: 'dn-tool dn-mono', text: '⚙ ' + (tc.name || tc.tool || 'tool') }));
  }
  const activity = buildExecutionOutline(execution, t.activity_ids);
  if (activity) turn.appendChild(activity);
  for (const a of ((annBySeq && annBySeq.get(t.seq)) || [])) {
    turn.appendChild(el('div', { class: 'dn-annot dn-annot-' + (a.kind || 'note'), text: '◂ ' + (a.summary || a.kind) }));
  }
  return turn;
}

// Follow only server-supplied edges; empty and foreign transcripts keep their old DOM.
export function buildExecutionOutline(execution, rootIds) {
  const index = executionIndex(execution);
  const nodes = index.nodes;
  const roots = Array.isArray(rootIds) ? rootIds : [];
  if (!nodes.length || !roots.length) return null;
  const rail = el('div', {
    class: 'dn-execution', role: 'tree',
    'aria-label': 'Execution activity',
    'data-execution-fidelity': index.source.fidelity || 'unavailable',
  });
  for (const id of roots) {
    const node = buildExecutionNode(index.byId.get(id), index.byId, index.children, new Set());
    if (node) rail.appendChild(node);
  }
  return rail.childNodes.length ? rail : null;
}

// `seen` bounds malformed cycles while preserving their known prefix.
export function buildExecutionNode(node, byId, children, seen) {
  if (!node || !node.node_id) return null;
  const visited = seen || new Set();
  if (visited.has(node.node_id)) return null;
  const nextSeen = new Set(visited);
  nextSeen.add(node.node_id);
  const childIds = (children && children.get(node.node_id)) || [];
  const kind = node.kind || 'activity';
  const status = node.status || 'completed';
  const fidelity = node.fidelity || 'exact';
  const attrs = {
    class: 'dn-exec-node dn-exec-' + kind + ' dn-exec-' + status,
    role: 'treeitem',
    'data-node-id': node.node_id,
    'data-node-kind': kind,
    'data-status': status,
    'data-fidelity': fidelity,
  };
  const label = el('span', { class: 'dn-exec-label' }, [
    el('span', { class: 'dn-exec-glyph', 'aria-hidden': 'true', text: executionGlyph(kind) }),
    el('span', { class: 'dn-exec-name', text: node.name || kind }),
    el('span', { class: 'dn-exec-status dn-faint', text: ' · ' + status }),
  ]);
  if (node.summary) label.appendChild(el('span', { class: 'dn-exec-summary dn-faint', text: ' · ' + node.summary }));
  if (!childIds.length) return el('div', attrs, [label]);

  const summary = el('summary', { class: 'dn-exec-toggle', 'aria-expanded': 'false' }, [label]);
  const branch = el('details', attrs, [summary]);
  const childRail = el('div', { class: 'dn-exec-children', role: 'group' });
  for (const id of childIds) {
    const child = buildExecutionNode(byId && byId.get(id), byId, children, nextSeen);
    if (child) childRail.appendChild(child);
  }
  if (childRail.childNodes.length) branch.appendChild(childRail);
  if (typeof branch.addEventListener === 'function') {
    branch.addEventListener('toggle', () => summary.setAttribute('aria-expanded', branch.open ? 'true' : 'false'));
  }
  return branch;
}

function executionGlyph(kind) {
  if (kind === 'tool') return '⚙';
  if (kind === 'agent') return '◇';
  if (kind === 'artifact') return '□';
  return '·';
}

// Fold only the adjacent literal goal duplicate emitted by run start and derivation.
export function dedupConsecutiveTurns(turns) {
  // Sparse live deltas heal by re-reading; they must not blank the pane first.
  const list = Array.isArray(turns) ? turns.filter(Boolean) : [];
  const out = [];
  for (const t of list) {
    const prev = out[out.length - 1];
    if (prev && isDuplicateTurn(prev, t)) continue;
    out.push(t);
  }
  return out;
}

function isDuplicateTurn(a, b) {
  if (!a || !b) return false;
  const aText = (a.text || '').trim();
  const bText = (b.text || '').trim();
  if (aText === '' || aText !== bText) return false;
  if ((a.role || '') !== (b.role || '')) return false;
  const aTools = Array.isArray(a.tool_calls) && a.tool_calls.length;
  const bTools = Array.isArray(b.tool_calls) && b.tool_calls.length;
  if (aTools || bTools) return false;
  return true;
}

// Fold every field that changes a turn's rendered content (G10).
export function turnSig(t, annBySeq, execution) {
  const na = annBySeq && annBySeq.get(t.seq);
  const ids = Array.isArray(t.activity_ids) ? t.activity_ids : [];
  const index = executionIndex(execution);
  const activity = ids.map((id) => executionNodeSig(id, index, new Set())).join('|');
  return [t.seq, t.role, (t.text || '').length, Array.isArray(t.tool_calls) ? t.tool_calls.length : 0, na ? na.length : 0, activity].join(':');
}

function executionNodeSig(id, index, seen) {
  if (seen.has(id)) return id + ':cycle';
  const node = index.byId.get(id);
  if (!node) return id + ':missing';
  const next = new Set(seen); next.add(id);
  const children = (index.children.get(id) || []).map((child) => executionNodeSig(child, index, next));
  return [node.node_id, node.kind, node.name, node.status, node.summary, node.fidelity, children.join(',')].join('~');
}

function executionIndex(value) {
  if (value && value.byId instanceof Map) return value;
  const source = value || {};
  const nodes = Array.isArray(source.nodes) ? source.nodes : [];
  const byId = new Map(nodes.map((node) => [node.node_id, node]));
  const children = new Map();
  for (const node of nodes) {
    if (!node.parent_id) continue;
    if (!children.has(node.parent_id)) children.set(node.parent_id, []);
    children.get(node.parent_id).push(node.node_id);
  }
  return { source, nodes, byId, children };
}

export function nearBottom(scroller) {
  const sh = scroller.scrollHeight, st = scroller.scrollTop, ch = scroller.clientHeight;
  if (typeof sh !== 'number' || typeof ch !== 'number' || typeof st !== 'number') return true;
  return (sh - st - ch) <= 8;
}

export function annotationsBySeq(annotations) {
  const map = new Map();
  for (const a of (Array.isArray(annotations) ? annotations : [])) {
    const k = a.anchor_seq;
    if (!map.has(k)) map.set(k, []);
    map.get(k).push(a);
  }
  return map;
}

// Patch by turn index because a late annotation or completion can change an older turn.
// Unchanged nodes retain scroll, selection, and focus; an unchanged beat writes no DOM.
export function reconcileTurns(scroller, rawTurns, annotations, execution) {
  const turns = dedupConsecutiveTurns(Array.isArray(rawTurns) ? rawTurns : []);
  const annBySeq = annotationsBySeq(annotations);

  const executionData = executionIndex(execution);
  const wantSig = turns.map((t) => turnSig(t, annBySeq, executionData));
  const haveSig = Array.isArray(scroller._turnSig) ? scroller._turnSig : [];

  const out = { rendered: turns.length, appended: 0, patched: 0, pinned: nearBottom(scroller) };
  const overlap = Math.min(haveSig.length, wantSig.length);

  // Patch changed turns, append new ones, then trim a shortened final transcript.
  for (let i = 0; i < overlap; i += 1) {
    if (haveSig[i] === wantSig[i]) continue;
    const oldNode = scroller.childNodes[i];
    const fresh = buildTurnNode(turns[i], annBySeq, executionData);
    if (oldNode) { scroller.insertBefore(fresh, oldNode); scroller.removeChild(oldNode); }
    else scroller.appendChild(fresh);
    out.patched += 1;
  }

  for (let i = overlap; i < turns.length; i += 1) {
    scroller.insertBefore(buildTurnNode(turns[i], annBySeq, executionData), scroller._executionUnresolvedNode || null);
    out.appended += 1;
  }

  for (let i = haveSig.length - 1; i >= overlap; i -= 1) {
    const extra = scroller.childNodes[i];
    if (extra) scroller.removeChild(extra);
  }

  if (out.patched === 0 && out.appended === 0 && haveSig.length === wantSig.length) {
    reconcileRunActivity(scroller, turns, executionData);
    return out;
  }

  scroller._turnSig = wantSig;
  reconcileRunActivity(scroller, turns, executionData);
  if (out.pinned && typeof scroller.scrollHeight === 'number') scroller.scrollTop = scroller.scrollHeight;
  return out;
}

function reconcileRunActivity(scroller, turns, execution) {
  const index = executionIndex(execution);
  const attached = new Set(turns.flatMap((turn) => Array.isArray(turn.activity_ids) ? turn.activity_ids : []));
  const unresolved = new Set(Array.isArray(index.source.unresolved_ids) ? index.source.unresolved_ids : []);
  const roots = Array.isArray(index.source.root_ids) ? index.source.root_ids : [];
  const ids = [...roots, ...unresolved].filter((id, index, all) => !attached.has(id) && all.indexOf(id) === index);
  const sig = ids.map(String).join('|') + ':' + ids.map((id) => {
    const node = index.byId.get(id);
    return node ? [node.status, node.summary, node.fidelity].join('~') : 'missing';
  }).join('|');
  if (scroller._executionUnresolvedSig === sig) return;
  if (scroller._executionUnresolvedNode) scroller.removeChild(scroller._executionUnresolvedNode);
  scroller._executionUnresolvedNode = null;
  scroller._executionUnresolvedSig = sig;
  if (!ids.length) return;
  const tree = buildExecutionOutline(index, ids);
  if (!tree) return;
  const node = el('aside', {
    class: 'dn-exec-unresolved',
    'data-execution-unresolved': '',
    'aria-label': 'Unresolved execution activity',
  }, [el('div', {
    class: 'dn-exec-unresolved-title dn-faint',
    text: ids.every((id) => unresolved.has(id)) ? 'Unresolved activity' : 'Run activity',
  }), tree]);
  scroller._executionUnresolvedNode = node;
  scroller.appendChild(node);
}

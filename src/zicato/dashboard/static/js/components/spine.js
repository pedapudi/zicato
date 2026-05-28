// components/spine.js — lineage spine visualization.
//
// The spine is the dashboard's signature L1 element. It tells the
// story of an epoch as a left-to-right walk through generations: the
// promoted lineage forms a solid spine row, and each rejected
// challenger is drawn as a small branch node ABOVE its parent on the
// spine so a viewer can instantly see "v4-v7 all challenged v3, none
// beat it." The live node (if any) is the right-most node on the
// spine row with a dashed border + pulse.
//
//        v2(R)         v4(R) v5(R) v6(R) v7(R)
//         │             │     │     │     │
//   v0 ── v1 ───────── v3 ────────────── v8 LIVE
//
// Each spine node renders three lines: the dot, the version label,
// and the scalar value. Rejected branch nodes render as small chips
// attached to their parent by a vertical connector.
//
// A workspace with no parent metadata (legacy data) degrades to the
// previous "rejected footnote" form so the spine still renders.

import { el } from '../core/dom.js';

/**
 * Render a horizontal lineage spine.
 *
 * opts:
 *   nodes — [{id, scalar, promoted, decision, live, parent_id, href}]
 *   liveId — optional id of the live node (also flagged via node.live)
 *   onNodeClick — optional handler (id) => void
 *   showRejectedFootnote — if true (default), rejected nodes that have
 *     no known parent in the spine still appear in the rejected footer.
 *     Rejected nodes WITH a known parent always render as parent
 *     branches above the spine instead.
 */
export function renderSpine(opts) {
  const o = opts || {};
  const allNodes = Array.isArray(o.nodes) ? o.nodes.slice() : [];
  const showFootnote = o.showRejectedFootnote !== false;

  if (allNodes.length === 0) {
    return el('div', { class: 'spine spine-empty' }, [
      el('p', { class: 'spine-empty-msg' }, ['No generations yet.']),
    ]);
  }

  // Promoted (and live) nodes form the inline spine row.
  let spineNodes = allNodes.filter((n) => n && (n.promoted || n.live));
  let rejectedNodes = allNodes.filter((n) => n && !n.promoted && !n.live);

  // If nothing made it onto the spine (e.g. only rejected so far) show
  // ALL of them inline so the spine never collapses to nothing.
  if (spineNodes.length === 0) {
    spineNodes = allNodes;
    rejectedNodes = [];
  }

  // Bucket rejected challengers by their parent id. A rejected node
  // whose parent is on the spine renders as a branch above that
  // parent. Anything left over (no parent recorded, or parent not on
  // the spine) falls back to the rejected footer.
  const spineIds = new Set(spineNodes.map((n) => n.id));
  const branchesByParent = new Map();
  const orphanRejected = [];
  for (const rn of rejectedNodes) {
    const pid = rn && rn.parent_id ? rn.parent_id : null;
    if (pid && spineIds.has(pid)) {
      if (!branchesByParent.has(pid)) branchesByParent.set(pid, []);
      branchesByParent.get(pid).push(rn);
    } else {
      orphanRejected.push(rn);
    }
  }

  // Build the spine row: each promoted/live node is rendered as a
  // column. The column carries an optional row of rejected-branch
  // chips ABOVE the spine node, connected by a short vertical line.
  const rowChildren = [];
  for (let i = 0; i < spineNodes.length; i += 1) {
    const node = spineNodes[i];
    const branches = branchesByParent.get(node.id) || [];
    rowChildren.push(_renderSpineColumn(node, branches, o.onNodeClick));
    if (i < spineNodes.length - 1) {
      const next = spineNodes[i + 1];
      rowChildren.push(_renderConnector(node, next));
    }
  }

  const children = [
    el('div', { class: 'spine-row', role: 'list', 'aria-label': 'generation lineage' },
      rowChildren),
  ];

  if (showFootnote && orphanRejected.length > 0) {
    const footChildren = [
      el('span', { class: 'spine-footer-label' }, ['rejected (no parent):']),
    ];
    for (const rn of orphanRejected) {
      footChildren.push(_renderRejectedChip(rn, o.onNodeClick));
    }
    children.push(el('div', { class: 'spine-footer' }, footChildren));
  }

  return el('div', { class: 'spine' }, children);
}

// A spine column: the rejected-challenger branches (if any) sit above
// the promoted spine node, connected by a short vertical tee. The
// branches render in id-order so the picture matches the journal's
// generation order.
function _renderSpineColumn(node, branches, onClick) {
  const id = String(node.id || '?');
  const branchList = (branches || []).slice().sort(_byGenId);

  let branchRow = null;
  let branchConnector = null;
  if (branchList.length > 0) {
    const chips = [];
    for (const b of branchList) {
      chips.push(_renderBranchChip(b, onClick));
    }
    branchRow = el('div', {
      class: 'spine-branches',
      role: 'list',
      'aria-label': 'rejected challengers of ' + id,
    }, chips);
    branchConnector = el('div', { class: 'spine-branch-tee', 'aria-hidden': 'true' });
  }

  const spineNode = _renderSpineNode(node, onClick);
  return el('div', { class: 'spine-col' },
    [branchRow, branchConnector, spineNode].filter(Boolean));
}

function _byGenId(a, b) {
  const ai = String(a && a.id || '');
  const bi = String(b && b.id || '');
  // Natural sort on the numeric suffix when both ids look like vN; fall
  // back to string compare otherwise.
  const ma = ai.match(/^v(\d+)$/);
  const mb = bi.match(/^v(\d+)$/);
  if (ma && mb) return Number(ma[1]) - Number(mb[1]);
  return ai < bi ? -1 : ai > bi ? 1 : 0;
}

function _renderSpineNode(node, onClick) {
  const id = String(node.id || '?');
  const scalar = (typeof node.scalar === 'number' && isFinite(node.scalar))
    ? node.scalar.toFixed(3) : '—';
  const isLive = !!node.live;
  const isPromoted = !!node.promoted;

  const dotCls = ['spine-dot'];
  if (isLive) dotCls.push('spine-dot-live');
  else if (isPromoted) dotCls.push('spine-dot-promoted');
  else dotCls.push('spine-dot-rejected');

  const nodeCls = ['spine-node'];
  if (isLive) nodeCls.push('spine-node-live');
  else if (isPromoted) nodeCls.push('spine-node-promoted');
  else nodeCls.push('spine-node-rejected');

  const handler = onClick
    ? ((ev) => { ev.preventDefault(); onClick(id); })
    : null;

  const versionLabelChildren = [id];
  if (isLive) {
    versionLabelChildren.unshift(el('span', { class: 'spine-live-tag' }, ['LIVE ']));
  }

  const inner = el('div', { class: 'spine-node-inner' }, [
    el('div', { class: dotCls.join(' '), 'aria-hidden': 'true' }),
    el('div', { class: 'spine-node-label mono' }, versionLabelChildren),
    el('div', { class: 'spine-node-scalar mono' }, [scalar]),
  ]);

  if (node.href) {
    return el('a', {
      class: nodeCls.join(' ') + ' spine-node-link',
      href: node.href,
      role: 'listitem',
      'aria-label': `generation ${id}, scalar ${scalar}`,
    }, [inner]);
  }
  if (handler) {
    return el('div', {
      class: nodeCls.join(' ') + ' spine-node-clickable',
      role: 'listitem button',
      tabindex: '0',
      onclick: handler,
      'aria-label': `generation ${id}, scalar ${scalar}`,
    }, [inner]);
  }
  return el('div', {
    class: nodeCls.join(' '),
    role: 'listitem',
    'aria-label': `generation ${id}, scalar ${scalar}`,
  }, [inner]);
}

function _renderConnector(fromNode, toNode) {
  const cls = ['spine-connector'];
  // Solid when stepping from promoted to promoted; dashed when stepping
  // into a live or rejected node.
  if (toNode.live) cls.push('spine-connector-live');
  else if (fromNode.promoted && toNode.promoted) cls.push('spine-connector-promoted');
  else cls.push('spine-connector-dashed');
  return el('div', { class: cls.join(' '), 'aria-hidden': 'true' }, [
    el('span', { class: 'spine-connector-line' }),
    el('span', { class: 'spine-connector-arrow' }, [toNode.live ? '⇢' : '→']),
  ]);
}

// A rejected challenger drawn as a small branch chip above its parent
// on the spine. Visually distinct from the orphan-rejected pill in the
// footer: the branch chip carries the implicit affordance "I challenged
// the parent below me".
function _renderBranchChip(node, onClick) {
  const id = String(node.id || '?');
  const scalar = (typeof node.scalar === 'number' && isFinite(node.scalar))
    ? node.scalar.toFixed(3) : null;
  const inner = [
    el('span', { class: 'spine-branch-id mono' }, [id]),
    scalar ? el('span', { class: 'spine-branch-scalar mono' }, [scalar]) : null,
  ].filter(Boolean);
  const ariaLabel = `rejected challenger ${id}`
    + (scalar ? `, scalar ${scalar}` : '');
  if (node.href) {
    return el('a', {
      class: 'spine-branch',
      href: node.href,
      role: 'listitem',
      'aria-label': ariaLabel,
    }, inner);
  }
  if (onClick) {
    const handler = (ev) => { ev.preventDefault(); onClick(id); };
    return el('span', {
      class: 'spine-branch',
      role: 'listitem button',
      tabindex: '0',
      onclick: handler,
      'aria-label': ariaLabel,
    }, inner);
  }
  return el('span', {
    class: 'spine-branch',
    role: 'listitem',
    'aria-label': ariaLabel,
  }, inner);
}

function _renderRejectedChip(node, onClick) {
  const id = String(node.id || '?');
  const scalar = (typeof node.scalar === 'number' && isFinite(node.scalar))
    ? node.scalar.toFixed(3) : null;
  const handler = onClick
    ? ((ev) => { ev.preventDefault(); onClick(id); })
    : null;
  const inner = [
    el('span', { class: 'spine-rejected-id mono' }, [id]),
    scalar ? el('span', { class: 'spine-rejected-scalar mono' }, [scalar]) : null,
  ];
  if (node.href) {
    return el('a', {
      class: 'spine-rejected-chip',
      href: node.href,
    }, inner);
  }
  if (handler) {
    return el('span', {
      class: 'spine-rejected-chip',
      role: 'button',
      tabindex: '0',
      onclick: handler,
    }, inner);
  }
  return el('span', { class: 'spine-rejected-chip' }, inner);
}

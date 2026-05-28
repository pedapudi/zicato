// components/spine.js — lineage spine visualization.
//
// The spine is the dashboard's signature L1 element. It tells the
// story of an epoch as a left-to-right walk through generations:
// promoted nodes form a solid lineage; rejected nodes are listed below
// as a footnote; the live node (if any) is the right-most node with
// a dashed border + pulse.
//
//   v0   v1   v3   LIVE v4
//   ●─→  ●─→  ●⇢   ◌
//   0.62 0.48 0.23      ?
//
// Each node renders three lines: the dot, the version label, and the
// scalar value. A decision pill (for rejected nodes that ARE shown
// inline rather than as footnotes) renders below the scalar.

import { el } from '../core/dom.js';

/**
 * Render a horizontal lineage spine.
 *
 * opts:
 *   nodes — [{id, scalar, promoted, decision, live, parent_id, href}]
 *   liveId — optional id of the live node (also flagged via node.live)
 *   onNodeClick — optional handler (id) => void
 *   showRejectedFootnote — if true, rejected nodes are pulled out of
 *     the inline spine into a "rejected:" footer. Default: true.
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

  let inlineNodes;
  let rejectedNodes;
  if (showFootnote) {
    inlineNodes = allNodes.filter((n) => n && (n.promoted || n.live));
    rejectedNodes = allNodes.filter((n) => n && !n.promoted && !n.live);
  } else {
    inlineNodes = allNodes;
    rejectedNodes = [];
  }

  // If nothing inline (e.g. only rejected so far) show ALL of them
  // inline so the spine never collapses to nothing.
  if (inlineNodes.length === 0) {
    inlineNodes = allNodes;
    rejectedNodes = [];
  }

  const rowChildren = [];
  for (let i = 0; i < inlineNodes.length; i += 1) {
    const node = inlineNodes[i];
    rowChildren.push(_renderSpineNode(node, o.onNodeClick));
    if (i < inlineNodes.length - 1) {
      const next = inlineNodes[i + 1];
      rowChildren.push(_renderConnector(node, next));
    }
  }

  const children = [
    el('div', { class: 'spine-row', role: 'list', 'aria-label': 'generation lineage' },
      rowChildren),
  ];

  if (rejectedNodes.length > 0) {
    const footChildren = [
      el('span', { class: 'spine-footer-label' }, ['rejected:']),
    ];
    for (const rn of rejectedNodes) {
      footChildren.push(_renderRejectedChip(rn, o.onNodeClick));
    }
    children.push(el('div', { class: 'spine-footer' }, footChildren));
  }

  return el('div', { class: 'spine' }, children);
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

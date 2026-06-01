// variants/Q/tree.js — the persistent NESTED TREE sidebar (Atlas IV headline).
//
// A data-model tree that mirrors the real hierarchy:
//
//   Environment (workspace)
//   └─ Epoch <id>
//      ├─ Generations
//      │  └─ <gen> (champion / rejected)
//      ├─ Boards
//      │  └─ <entry>
//      ├─ Mutation surface
//      └─ Publication
//
// Expandable / collapsible; selecting ANY node drives the detail pane via the
// router (selection is explicit + URL-encoded). It navigates MULTIPLE epochs
// AND MULTIPLE generations. Only one epoch exists in the live data — the tree
// degrades gracefully but is structured all-epochs-first.
//
// Pure builder: buildTree(model, ctx) → a detached <nav>. The model is loaded
// by the shell; expansion state persists in localStorage and the ancestors of
// the current selection are force-expanded so a cold deep-link reveals the path.

import { el } from '../../core/dom.js';

const EXPAND_KEY = 'zicato.Q.tree.expanded';

function readExpanded() {
  try {
    const raw = window.localStorage.getItem(EXPAND_KEY);
    if (!raw) return new Set();
    return new Set(JSON.parse(raw));
  } catch (e) { return new Set(); }
}
function writeExpanded(set) {
  try { window.localStorage.setItem(EXPAND_KEY, JSON.stringify([...set])); } catch (e) { /* ignore */ }
}

// Which tree node ids are the ancestors of the current route — these are
// force-expanded so the selection is always visible (cold-deep-link hydration).
function ancestorsFor(route) {
  const p = route.params || {};
  const out = new Set(['env']);
  if (p.epochId) {
    out.add('epoch:' + p.epochId);
    if (route.view === 'gen' || route.view === 'matchups') out.add('gens:' + p.epochId);
    if (route.view === 'board') out.add('boards:' + p.epochId);
  }
  return out;
}

// The selected tree node id — the leaf the route points at (for highlight).
export function selectedNodeId(route) {
  const p = route.params || {};
  switch (route.view) {
    case 'home': return 'env';
    case 'epoch': return 'epoch:' + p.epochId;
    case 'gen': return 'gen:' + p.epochId + ':' + p.gen;
    case 'matchups': return 'gen:' + p.epochId + ':' + p.gen; // matchups is a candidate facet
    case 'board': return p.entry ? 'board:' + p.epochId + ':' + p.entry : 'boards:' + p.epochId;
    case 'mutations': return 'muts:' + p.epochId;
    case 'publication': return 'pub:' + p.epochId;
    case 'run': return 'gen:' + p.epochId + ':' + p.gen;
    default: return 'env';
  }
}

export function buildTree(model, ctx, route) {
  const expanded = readExpanded();
  const forced = ancestorsFor(route);
  const selected = selectedNodeId(route);

  const isOpen = (id) => forced.has(id) || expanded.has(id);
  const toggle = (id) => {
    if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
    writeExpanded(expanded);
    ctx.rerenderTree();
  };

  const nav = el('nav', { class: 'dq-tree', 'aria-label': 'Data-model tree' });

  // ENVIRONMENT root.
  const envChildren = el('div', { class: 'dq-tree-children' });
  const epochs = (model && Array.isArray(model.epochs)) ? model.epochs : [];

  if (!epochs.length) {
    envChildren.appendChild(el('div', { class: 'dq-tree-empty', text: 'no epochs yet' }));
  }
  for (const ep of epochs) {
    envChildren.appendChild(epochBranch(ep, model, ctx, route, { isOpen, toggle, selected }));
  }

  nav.appendChild(branch({
    id: 'env',
    label: 'Environment',
    sub: epochs.length + ' epoch' + (epochs.length === 1 ? '' : 's'),
    kind: 'env',
    open: true, // env is always open — the root of the model
    expandable: epochs.length > 0,
    selected: selected === 'env',
    onSelect: () => ctx.navigate('home', {}),
    onToggle: null, // root stays open
    children: envChildren,
  }));

  return nav;
}

function epochBranch(ep, model, ctx, route, h) {
  const epochId = ep.epoch_id;
  const id = 'epoch:' + epochId;
  const open = h.isOpen(id);
  const gens = (model.lineageByEpoch && model.lineageByEpoch[epochId]) || [];
  const board = (model.boardByEpoch && model.boardByEpoch[epochId]) || [];

  const children = el('div', { class: 'dq-tree-children' });
  if (open) {
    // Generations group.
    const gensId = 'gens:' + epochId;
    const gensChildren = el('div', { class: 'dq-tree-children' });
    if (!gens.length) gensChildren.appendChild(el('div', { class: 'dq-tree-empty', text: 'no generations' }));
    for (const g of gens) {
      const gid = 'gen:' + epochId + ':' + g.id;
      gensChildren.appendChild(branch({
        id: gid, label: g.id, kind: 'gen',
        badge: g.promoted ? '♛' : null,
        tone: g.promoted ? 'good' : 'bad',
        selected: h.selected === gid,
        expandable: false,
        onSelect: () => ctx.navigate('gen', { epochId, gen: g.id }),
      }));
    }
    children.appendChild(branch({
      id: gensId, label: 'Generations', kind: 'group',
      sub: gens.length + '', open: h.isOpen(gensId), expandable: gens.length > 0,
      onToggle: () => h.toggle(gensId), onSelect: () => h.toggle(gensId),
      children: gensChildren,
    }));

    // Boards group.
    const boardsId = 'boards:' + epochId;
    const boardsChildren = el('div', { class: 'dq-tree-children' });
    if (!board.length) boardsChildren.appendChild(el('div', { class: 'dq-tree-empty', text: 'no board entries' }));
    for (const b of board) {
      const eid = b.entry_id || b.id;
      const bid = 'board:' + epochId + ':' + eid;
      boardsChildren.appendChild(branch({
        id: bid, label: eid, kind: 'board',
        selected: h.selected === bid, expandable: false,
        onSelect: () => ctx.navigate('board', { epochId, entry: eid }),
      }));
    }
    children.appendChild(branch({
      id: boardsId, label: 'Boards', kind: 'group',
      sub: board.length + '', open: h.isOpen(boardsId), expandable: board.length > 0,
      selected: h.selected === boardsId,
      onToggle: () => h.toggle(boardsId),
      onSelect: () => ctx.navigate('board', { epochId }),
      children: boardsChildren,
    }));

    // Mutation surface (leaf).
    children.appendChild(branch({
      id: 'muts:' + epochId, label: 'Mutation surface', kind: 'leaf',
      selected: h.selected === 'muts:' + epochId, expandable: false,
      onSelect: () => ctx.navigate('mutations', { epochId }),
    }));
    // Publication (leaf).
    children.appendChild(branch({
      id: 'pub:' + epochId, label: 'Publication', kind: 'leaf',
      selected: h.selected === 'pub:' + epochId, expandable: false,
      onSelect: () => ctx.navigate('publication', { epochId }),
    }));
  }

  return branch({
    id, label: epochId, kind: 'epoch',
    sub: ep.closed ? 'closed' : 'open',
    tone: ep.closed ? null : 'live',
    open, expandable: true,
    selected: h.selected === id,
    onToggle: () => h.toggle(id),
    onSelect: () => ctx.navigate('epoch', { epochId }),
    children,
  });
}

// A single tree row + its (optional) children container. The disclosure
// triangle toggles expansion; the label selects (navigates). Indentation comes
// from nesting the children container, which the CSS indents.
function branch(o) {
  const row = el('div', { class: 'dq-tree-row dq-tree-' + (o.kind || 'leaf') + (o.selected ? ' dq-tree-selected' : '') });

  // disclosure
  if (o.expandable) {
    const tw = el('button', {
      class: 'dq-tree-twisty' + (o.open ? ' dq-open' : ''), type: 'button',
      'aria-expanded': o.open ? 'true' : 'false', 'aria-label': (o.open ? 'collapse ' : 'expand ') + o.label,
      text: '▸',
    });
    if (o.onToggle) tw.addEventListener('click', (ev) => { if (ev && ev.stopPropagation) ev.stopPropagation(); o.onToggle(); });
    row.appendChild(tw);
  } else {
    row.appendChild(el('span', { class: 'dq-tree-twisty dq-tree-twisty-none', 'aria-hidden': 'true' }));
  }

  const label = el('button', { class: 'dq-tree-label', type: 'button' }, [
    el('span', { class: 'dq-tree-text', text: o.label }),
    o.badge ? el('span', { class: 'dq-tree-badge', text: o.badge }) : null,
    o.sub != null ? el('span', { class: 'dq-tree-sub' + (o.tone ? ' dq-tone-' + o.tone : ''), text: o.sub }) : null,
  ].filter(Boolean));
  if (o.onSelect) label.addEventListener('click', () => o.onSelect());
  row.appendChild(label);

  const wrap = el('div', { class: 'dq-tree-node', 'data-tree-node': o.id }, [row]);
  if (o.children && o.open) wrap.appendChild(o.children);
  return wrap;
}

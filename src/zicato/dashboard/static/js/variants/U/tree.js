// variants/U/tree.js — the HEADLINE: a persistent data-model TREE sidebar.
//
// A nested, expandable/collapsible tree that mirrors the REAL hierarchy:
//
//   Environment (workspace)
//   └─ Epoch <id>                       (one per epoch — switches which epoch)
//      ├─ Generations
//      │  └─ <gen> (champion ♛ / rejected)
//      ├─ Boards
//      │  └─ <entry>
//      ├─ Mutation surface
//      └─ Publication
//
// Selecting ANY node drives the detail pane (navigate). The tree MUST navigate
// MULTIPLE epochs AND MULTIPLE generations — every epoch and every generation
// is its own selectable node. The selection is URL-encoded by the shell's
// router, so a cold deep-link expands the tree to the selection.
//
// Render discipline: digest-gated — the tree only rebuilds when the resolved
// hierarchy OR the selection changes; a heartbeat tick is a no-op. Expansion
// state persists in localStorage so it survives re-renders + reloads.

import { el, clearChildren } from '../../core/dom.js';
import * as model from './model.js';
import { href } from './router.js';

const OPEN_KEY = 'zicato.U.tree.open';

function readOpen() {
  try { return new Set(JSON.parse(window.localStorage.getItem(OPEN_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function persistOpen(set) {
  try { window.localStorage.setItem(OPEN_KEY, JSON.stringify([...set])); } catch (e) { /* ignore */ }
}

let _open = null;

// Build the tree model: { epochs, currentId, gens, championId, board }. The
// generations / board belong to the CURRENT epoch (the live data has one); a
// non-current epoch node still selects to its overview.
export async function buildModel() {
  const [{ list, currentId }, { gens, championId }, board] = await Promise.all([
    model.epochs(), model.generations(), model.board(),
  ]);
  return { epochs: list, currentId, gens, championId, board };
}

// A stable digest of the hierarchy + the selection — the no-flash gate input.
export function treeDigest(m, route) {
  return JSON.stringify({
    epochs: m.epochs.map((e) => [e.epoch_id, e.closed]),
    gens: m.gens.map((g) => [g.id, g.parent, g.promoted]),
    board: m.board.map((b) => b.id),
    champ: m.championId,
    sel: selectionKey(route),
    open: [..._open].sort(),
  });
}

// A compact key identifying the selected node, for highlight + auto-expand.
function selectionKey(route) {
  const p = route.params || {};
  switch (route.view) {
    case 'epoch': return 'epoch:' + (p.epochId || '');
    case 'candidate': return 'gen:' + (p.epochId || '') + ':' + (p.gen || '');
    case 'board': return 'board:' + (p.epochId || '') + ':' + (p.entry || '');
    case 'mutations': return 'mut:' + (p.epochId || '');
    case 'publication': return 'pub:' + (p.epochId || '');
    default: return 'env';
  }
}

// Auto-expand the branches that contain the current selection so a cold
// deep-link reveals the selected node without a click.
function autoExpand(route, m) {
  const p = route.params || {};
  const epochId = p.epochId || m.currentId;
  if (route.view === 'env') return;
  if (epochId) _open.add('e:' + epochId);
  if (route.view === 'candidate') _open.add('e:' + epochId + ':gens');
  if (route.view === 'board') _open.add('e:' + epochId + ':boards');
}

// Render (or digest-gated re-render) the tree into host.
export function paintTree(host, m, route, nav) {
  if (_open == null) _open = readOpen();
  autoExpand(route, m);
  const dg = treeDigest(m, route);
  if (host.getAttribute('data-u-tree-digest') === dg && host.firstChild) return false;
  clearChildren(host);
  host.setAttribute('data-u-tree-digest', dg);

  const sel = selectionKey(route);
  const tree = el('div', { class: 'vu-tree', role: 'tree', 'aria-label': 'Data model' });

  // ── Environment root ──
  tree.appendChild(leaf({
    label: 'Environment', sub: 'workspace', glyph: '◆', depth: 0,
    selected: sel === 'env', hrefStr: href('env', {}), nav, view: 'env', params: {},
  }));

  // ── one branch per epoch ──
  for (const e of m.epochs) {
    const isCurrent = e.epoch_id === m.currentId;
    const epochKey = 'e:' + e.epoch_id;
    const epochOpen = _open.has(epochKey);
    tree.appendChild(branchRow({
      key: epochKey, depth: 0, open: epochOpen,
      label: e.epoch_id, sub: isCurrent ? 'current' : (e.closed ? 'closed' : 'epoch'),
      glyph: '▣', selected: sel === 'epoch:' + e.epoch_id,
      view: 'epoch', params: { epochId: e.epoch_id }, nav, host, m, route,
    }));
    if (!epochOpen) continue;

    // Generations group
    const gensKey = epochKey + ':gens';
    const gensOpen = _open.has(gensKey);
    tree.appendChild(groupRow({ key: gensKey, depth: 1, open: gensOpen, label: 'Generations', count: m.gens.length, nav, host, m, route }));
    if (gensOpen) {
      for (const g of m.gens) {
        const isChamp = g.id === m.championId || g.promoted;
        tree.appendChild(leaf({
          label: g.id, sub: isChamp ? 'champion' : (g.parent ? 'rejected' : 'seed'),
          glyph: isChamp ? '♛' : '•', depth: 2, dim: g.parent && !g.promoted,
          selected: sel === 'gen:' + e.epoch_id + ':' + g.id,
          view: 'candidate', params: { epochId: e.epoch_id, gen: g.id }, nav,
        }));
      }
    }

    // Boards group
    const boardsKey = epochKey + ':boards';
    const boardsOpen = _open.has(boardsKey);
    tree.appendChild(groupRow({ key: boardsKey, depth: 1, open: boardsOpen, label: 'Boards', count: m.board.length, nav, host, m, route }));
    if (boardsOpen) {
      for (const b of m.board) {
        tree.appendChild(leaf({
          label: b.id, sub: kindShort(b.kind), glyph: '▤', depth: 2,
          selected: sel === 'board:' + e.epoch_id + ':' + b.id,
          view: 'board', params: { epochId: e.epoch_id, entry: b.id }, nav,
        }));
      }
    }

    // Mutation surface + Publication (leaves)
    tree.appendChild(leaf({
      label: 'Mutation surface', sub: 'site × generation', glyph: '⌥', depth: 1,
      selected: sel === 'mut:' + e.epoch_id,
      view: 'mutations', params: { epochId: e.epoch_id }, nav,
    }));
    tree.appendChild(leaf({
      label: 'Publication', sub: 'epoch paper', glyph: '¶', depth: 1,
      selected: sel === 'pub:' + e.epoch_id,
      view: 'publication', params: { epochId: e.epoch_id }, nav,
    }));
  }

  host.appendChild(tree);
  return true;
}

// A toggle disclosure for an EPOCH node (it is both expandable AND selectable).
function branchRow(o) {
  const row = el('div', { class: 'vu-tnode vu-depth-' + o.depth + (o.selected ? ' vu-tsel' : '') });
  const tw = el('button', { class: 'vu-ttwist', type: 'button', 'aria-expanded': String(o.open), title: o.open ? 'collapse' : 'expand', text: o.open ? '▾' : '▸' });
  tw.addEventListener('click', (ev) => { ev.preventDefault(); ev.stopPropagation(); toggle(o.key, o.host, o.m, o.route, o.nav); });
  const link = el('a', { class: 'vu-tlabel', href: href(o.view, o.params) });
  link.appendChild(el('span', { class: 'vu-tglyph', text: o.glyph }));
  link.appendChild(el('span', { class: 'vu-ttext', text: o.label }));
  if (o.sub) link.appendChild(el('span', { class: 'vu-tsub', text: o.sub }));
  link.addEventListener('click', (ev) => { ev.preventDefault(); o.nav(o.view, o.params); });
  row.appendChild(tw);
  row.appendChild(link);
  return row;
}

// A group node (Generations / Boards): expandable, NOT itself a destination.
function groupRow(o) {
  const row = el('div', { class: 'vu-tnode vu-tgroup vu-depth-' + o.depth });
  const tw = el('button', { class: 'vu-ttwist', type: 'button', 'aria-expanded': String(o.open), title: o.open ? 'collapse' : 'expand', text: o.open ? '▾' : '▸' });
  tw.addEventListener('click', (ev) => { ev.preventDefault(); toggle(o.key, o.host, o.m, o.route, o.nav); });
  const lbl = el('button', { class: 'vu-tlabel vu-tgrouplbl', type: 'button' }, [
    el('span', { class: 'vu-ttext', text: o.label }),
    el('span', { class: 'vu-tcount', text: String(o.count) }),
  ]);
  lbl.addEventListener('click', (ev) => { ev.preventDefault(); toggle(o.key, o.host, o.m, o.route, o.nav); });
  row.appendChild(tw);
  row.appendChild(lbl);
  return row;
}

// A leaf node (selectable, no children).
function leaf(o) {
  const row = el('div', { class: 'vu-tnode vu-tleaf vu-depth-' + o.depth + (o.selected ? ' vu-tsel' : '') + (o.dim ? ' vu-tdim' : '') });
  row.appendChild(el('span', { class: 'vu-ttwist vu-ttwist-empty', 'aria-hidden': 'true' }));
  const link = el('a', { class: 'vu-tlabel', href: href(o.view, o.params) });
  link.appendChild(el('span', { class: 'vu-tglyph', text: o.glyph }));
  link.appendChild(el('span', { class: 'vu-ttext', text: o.label }));
  if (o.sub) link.appendChild(el('span', { class: 'vu-tsub', text: o.sub }));
  link.addEventListener('click', (ev) => { ev.preventDefault(); o.nav(o.view, o.params); });
  row.appendChild(link);
  return row;
}

function toggle(key, host, m, route, nav) {
  if (_open.has(key)) _open.delete(key); else _open.add(key);
  persistOpen(_open);
  // Force a repaint of the tree (the digest changed because `open` changed).
  host.removeAttribute('data-u-tree-digest');
  paintTree(host, m, route, nav);
}

function kindShort(kind) {
  if (kind === 'single_turn') return 'single';
  if (kind === 'multi_turn_scripted') return 'scripted';
  if (kind === 'multi_turn_emulated') return 'emulated';
  return kind || '';
}

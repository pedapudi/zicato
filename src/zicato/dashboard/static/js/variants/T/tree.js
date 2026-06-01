// variants/T/tree.js — the data-model TREE sidebar (Console III's headline).
//
// A persistent, collapsible LEFT tree that mirrors the real zicato hierarchy:
//
//   Environment (workspace)
//   └─ Epoch <id>                       (one node per epoch — multi-epoch nav)
//      ├─ Generations
//      │  └─ <gen> (champion / rejected)
//      ├─ Boards
//      │  └─ <entry>
//      ├─ Mutation surface
//      └─ Publication
//
// Selecting ANY node drives the single detail pane (via ctx.navigate). The
// selection is derived ENTIRELY from the parsed route, so a cold deep-link
// hydrates the open branches + the highlighted node with no extra state. The
// expand/collapse set is the union of (the route's open path) ∪ (the user's
// manual toggles) — so the active branch is always visible, and the user can
// open siblings without losing their place.
//
// Render discipline: the tree is digest-gated like every pane. A heartbeat
// that changes no structural data (epochs, generations, board entries, or the
// selected node) writes ZERO DOM.

import { el, clearChildren } from '../../core/dom.js';

// `data` is the structural model the shell assembles once per dispatch:
//   { epochs:[{id, current}], expanded:{ epochs:{<id>:{gens, boards}} },
//     byEpoch:{ <id>:{ gens:[{id, promoted, parent}], boards:[{id, kind}] } } }
// `route` is the parsed route (drives the highlighted node + open path).
// `ctx`  carries navigate(view, params) + href(view, params).
// `toggles` is a Set of manually-opened node keys (owned by the shell).
export function treeDigest(model, route, toggles) {
  const p = (route && route.params) || {};
  return JSON.stringify({
    epochs: model.epochs.map((e) => [e.id, !!e.current]),
    by: model.epochs.map((e) => {
      const b = model.byEpoch[e.id] || { gens: [], boards: [] };
      // include the current/former champion split so the badge re-stamps when
      // the crown moves (a steady heartbeat with the same crown is a no-op).
      return [e.id, b.gens.map((g) => [g.id, !!g.promoted, !!g.currentChampion, !!g.formerChampion]), b.boards.map((x) => x.id)];
    }),
    sel: [route ? route.view : 'home', p.epochId || '', p.gen || '', p.entry || '', p.mutId || '', p.gen2 || ''],
    open: [...toggles].sort(),
  });
}

// The set of node-keys the route implies must be OPEN (so the active node is
// always visible regardless of manual toggles).
export function routeOpenKeys(route) {
  const p = (route && route.params) || {};
  const keys = new Set();
  if (!p.epochId) return keys;
  const e = 'e:' + p.epochId;
  keys.add(e);
  const v = route.view;
  if (v === 'gens' || v === 'candidate' || v === 'diff') keys.add(e + '/gens');
  if (v === 'boards' || v === 'board') keys.add(e + '/boards');
  return keys;
}

export function buildTree(host, model, route, toggles, ctx, onToggle) {
  clearChildren(host);
  const open = new Set([...toggles, ...routeOpenKeys(route)]);
  const p = (route && route.params) || {};
  const sel = route ? route.view : 'home';

  const tree = el('div', { class: 'dt-tree', role: 'tree', 'aria-label': 'Data model' });

  // ── Environment (root) ──────────────────────────────────────────────
  tree.appendChild(branchRow({
    key: 'env', depth: 0, kind: 'env', label: 'Environment',
    sub: 'workspace', expandable: true, isOpen: true, lockOpen: true,
    selected: sel === 'home',
    onSelect: () => ctx.navigate('home', {}),
    onToggle: null,
  }));

  // ── one Epoch node per epoch (multi-epoch navigation) ───────────────
  for (const epoch of model.epochs) {
    const eKey = 'e:' + epoch.id;
    const eOpen = open.has(eKey);
    tree.appendChild(branchRow({
      key: eKey, depth: 1, kind: 'epoch',
      label: epoch.id, sub: epoch.current ? 'current' : null,
      expandable: true, isOpen: eOpen, selected: sel === 'epoch' && p.epochId === epoch.id,
      onSelect: () => ctx.navigate('epoch', { epochId: epoch.id }),
      onToggle: () => onToggle(eKey),
    }));
    if (!eOpen) continue;

    const bundle = model.byEpoch[epoch.id] || { gens: [], boards: [] };

    // Generations group
    const gKey = eKey + '/gens';
    const gOpen = open.has(gKey);
    tree.appendChild(branchRow({
      key: gKey, depth: 2, kind: 'group', label: 'Generations',
      sub: bundle.gens.length ? String(bundle.gens.length) : null,
      expandable: bundle.gens.length > 0, isOpen: gOpen,
      selected: sel === 'gens' && p.epochId === epoch.id,
      onSelect: () => ctx.navigate('gens', { epochId: epoch.id }),
      onToggle: () => onToggle(gKey),
    }));
    if (gOpen) {
      for (const g of bundle.gens) {
        const selected = (sel === 'candidate' || sel === 'diff') && p.epochId === epoch.id && p.gen === g.id;
        // Only the CURRENT champion (the last id in champion_lineage) gets the
        // ♚ "champion" badge; a FORMER champion (held the title, then was
        // succeeded) gets a distinct, dimmer hollow-crown "former" marker.
        // Pre-feature models without the current/former split keep the legacy
        // ♛ "champion" badge for any promoted generation (back-compat).
        const isCurrent = g.currentChampion === true;
        const isFormer = g.formerChampion === true;
        const legacyChamp = g.promoted && g.currentChampion === undefined && g.formerChampion === undefined;
        let kind = 'gen', glyph = (g.parent ? '↳' : '◆'), tag = (g.parent ? 'rejected' : 'seed');
        if (isCurrent || legacyChamp) { kind = 'gen-champ'; glyph = '♚'; tag = 'champion'; }
        else if (isFormer) { kind = 'gen-former'; glyph = '♔'; tag = 'former champion'; }
        tree.appendChild(leafRow({
          depth: 3, kind, label: g.id, glyph, tag,
          selected,
          onSelect: () => ctx.navigate('candidate', { epochId: epoch.id, gen: g.id }),
        }));
      }
    }

    // Boards group
    const bKey = eKey + '/boards';
    const bOpen = open.has(bKey);
    tree.appendChild(branchRow({
      key: bKey, depth: 2, kind: 'group', label: 'Boards',
      sub: bundle.boards.length ? String(bundle.boards.length) : null,
      expandable: bundle.boards.length > 0, isOpen: bOpen,
      selected: sel === 'boards' && p.epochId === epoch.id,
      onSelect: () => ctx.navigate('boards', { epochId: epoch.id }),
      onToggle: () => onToggle(bKey),
    }));
    if (bOpen) {
      for (const b of bundle.boards) {
        const selected = sel === 'board' && p.epochId === epoch.id && p.entry === b.id;
        tree.appendChild(leafRow({
          depth: 3, kind: 'board', label: b.id, glyph: '▦', tag: b.kindTag || null,
          selected,
          onSelect: () => ctx.navigate('board', { epochId: epoch.id, entry: b.id }),
        }));
      }
    }

    // Mutation surface (leaf)
    tree.appendChild(leafRow({
      depth: 2, kind: 'mutations', label: 'Mutation surface', glyph: '⌗', tag: null,
      selected: sel === 'mutations' && p.epochId === epoch.id,
      onSelect: () => ctx.navigate('mutations', { epochId: epoch.id }),
    }));

    // Publication (leaf)
    tree.appendChild(leafRow({
      depth: 2, kind: 'paper', label: 'Publication', glyph: '¶', tag: null,
      selected: sel === 'publication' && p.epochId === epoch.id,
      onSelect: () => ctx.navigate('publication', { epochId: epoch.id }),
    }));
  }

  if (!model.epochs.length) {
    tree.appendChild(el('div', { class: 'dt-tree-empty', text: 'No epochs in this workspace yet.' }));
  }

  host.appendChild(tree);
}

function branchRow(o) {
  const row = el('div', {
    class: 'dt-node dt-branch dt-d' + o.depth + (o.selected ? ' dt-sel' : ''),
    'data-kind': o.kind, role: 'treeitem',
    'aria-expanded': o.expandable ? String(!!o.isOpen) : null,
    style: '--dt-depth:' + o.depth + ';',
  });
  const twisty = el('button', {
    class: 'dt-twisty' + (o.expandable ? '' : ' dt-twisty-leaf'),
    type: 'button', 'aria-hidden': o.expandable ? null : 'true',
    title: o.expandable ? (o.isOpen ? 'collapse' : 'expand') : '',
    text: o.expandable ? (o.isOpen ? '▾' : '▸') : '',
  });
  if (o.expandable && !o.lockOpen && o.onToggle) {
    twisty.addEventListener('click', (ev) => { ev.stopPropagation(); o.onToggle(); });
  } else {
    twisty.disabled = true;
  }
  const label = el('button', { class: 'dt-label', type: 'button' }, [
    el('span', { class: 'dt-icon dt-icon-' + o.kind, 'aria-hidden': 'true' }),
    el('span', { class: 'dt-text', text: o.label }),
    o.sub ? el('span', { class: 'dt-sub', text: o.sub }) : null,
  ].filter(Boolean));
  if (o.selected) label.setAttribute('aria-current', 'true');
  label.addEventListener('click', () => o.onSelect());
  row.appendChild(twisty);
  row.appendChild(label);
  return row;
}

function leafRow(o) {
  const row = el('div', {
    class: 'dt-node dt-leaf dt-d' + o.depth + (o.selected ? ' dt-sel' : ''),
    'data-kind': o.kind, role: 'treeitem',
    style: '--dt-depth:' + o.depth + ';',
  });
  row.appendChild(el('span', { class: 'dt-twisty dt-twisty-leaf', 'aria-hidden': 'true' }));
  const label = el('button', { class: 'dt-label', type: 'button' }, [
    el('span', { class: 'dt-glyph dt-glyph-' + o.kind, 'aria-hidden': 'true', text: o.glyph || '·' }),
    el('span', { class: 'dt-text', text: o.label }),
    o.tag ? el('span', { class: 'dt-tag dt-tag-' + o.kind, text: o.tag }) : null,
  ].filter(Boolean));
  if (o.selected) label.setAttribute('aria-current', 'true');
  label.addEventListener('click', () => o.onSelect());
  row.appendChild(label);
  return row;
}

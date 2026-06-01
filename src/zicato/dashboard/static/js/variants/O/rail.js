// variants/O/rail.js — the persistent LEFT SELECTOR RAIL.
//
// Compass's master pane: a tree/list of epoch → generation → board entry.
// Selection is EXPLICIT and PERSISTENT — the rail highlights the currently
// selected item and every node routes to a TYPED selection (a generation
// → the candidate detail; a board entry → the per-board cross-candidate
// view, keyed by ENTRY ID, never an arbitrary candidate). The rail is its
// OWN constrained-scroll column and digest-gates its repaint, so a steady
// heartbeat never rebuilds it.

import { el } from '../../core/dom.js';
import { gatedSwap } from './ui.js';

// Build the rail content into `host`, given the structural data + the
// active selection. Returns nothing; writes are digest-gated.
export function renderRail(host, ctx, model) {
  const { epochId, gens, board, selection } = model;
  const sel = selection || {};

  const digest = JSON.stringify({
    epochId,
    gens: gens.map((g) => [g.id, g.promoted, g.scalar == null ? null : Number(g.scalar).toFixed(2)]),
    board: board.map((b) => [b.id, b.kind]),
    selKind: sel.kind, selId: sel.id, selGen: sel.gen, selEntry: sel.entry,
  });

  gatedSwap(host, digest, () => {
    const out = [];

    // Epoch header (the root of the tree).
    out.push(el('div', { class: 'vo-rail-epoch' }, [
      el('span', { class: 'vo-rail-epoch-eyebrow', text: 'EPOCH' }),
      el('span', { class: 'vo-rail-epoch-id vo-mono', text: epochId || '—' }),
    ]));

    // Generations group.
    const genGroup = el('div', { class: 'vo-rail-group' }, [
      el('div', { class: 'vo-rail-grouphead', text: 'Generations' }),
    ]);
    if (!gens.length) {
      genGroup.appendChild(el('p', { class: 'vo-rail-empty', text: 'no generations yet' }));
    } else {
      const list = el('ul', { class: 'vo-rail-list' });
      for (const g of gens) {
        const active = sel.kind === 'gen' && sel.gen === g.id && sel.entry == null;
        const item = el('li', {
          class: 'vo-rail-item vo-rail-gen' + (active ? ' vo-rail-active' : ''),
          tabindex: '0', role: 'button', 'data-gen': g.id,
        }, [
          el('span', { class: 'vo-rail-dot vo-' + (g.promoted ? 'promoted' : 'rejected') }),
          el('span', { class: 'vo-rail-label vo-mono', text: g.id }),
          g.promoted ? el('span', { class: 'vo-rail-tag vo-crown', text: '♚' }) : null,
          g.scalar != null ? el('span', { class: 'vo-rail-scalar', text: Number(g.scalar).toFixed(1) }) : null,
        ].filter(Boolean));
        const go = () => ctx.navigate('gen', { gen: g.id, facet: 'lifecycle' });
        item.addEventListener('click', go);
        item.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(); } });
        list.appendChild(item);
      }
      genGroup.appendChild(list);
    }
    out.push(genGroup);

    // Board entries group — selecting one opens the per-board view (by id).
    const boardGroup = el('div', { class: 'vo-rail-group' }, [
      el('div', { class: 'vo-rail-grouphead', text: 'Board · the tests every candidate faces' }),
    ]);
    if (!board.length) {
      boardGroup.appendChild(el('p', { class: 'vo-rail-empty', text: 'no board recorded' }));
    } else {
      const list = el('ul', { class: 'vo-rail-list' });
      for (const b of board) {
        const active = sel.kind === 'board' && sel.entry === b.id;
        const item = el('li', {
          class: 'vo-rail-item vo-rail-board' + (active ? ' vo-rail-active' : ''),
          tabindex: '0', role: 'button', 'data-entry': b.id,
        }, [
          el('span', { class: 'vo-rail-kind vo-kind-' + kindClass(b.kind), text: kindGlyph(b.kind) }),
          el('span', { class: 'vo-rail-label vo-mono', text: b.id }),
        ]);
        // Board entries route to the per-board cross-candidate view, keyed
        // by ENTRY ID — never an arbitrary candidate.
        const go = () => ctx.navigate('board', { entry: b.id });
        item.addEventListener('click', go);
        item.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(); } });
        list.appendChild(item);
      }
      boardGroup.appendChild(list);
    }
    out.push(boardGroup);

    return out;
  });
}

function kindClass(kind) {
  const k = String(kind || '');
  if (k.startsWith('single')) return 'single';
  if (k.includes('scripted')) return 'scripted';
  if (k.includes('emulated')) return 'emulated';
  return 'other';
}
function kindGlyph(kind) {
  const k = String(kind || '');
  if (k.startsWith('single')) return '•';
  if (k.includes('scripted')) return '⇉';
  if (k.includes('emulated')) return '⇄';
  return '·';
}

// variants/O/rail.js — the persistent LEFT SELECTOR RAIL.
//
// Compass's master pane, scoped by LEVEL: WORKSPACE (all epochs) at the top
// → each EPOCH is a selectable group header → under the SELECTED epoch the
// rail expands to that epoch's generations + board entries. Selection is
// EXPLICIT and PERSISTENT — the rail highlights the active item and every
// node routes to a TYPED selection (an epoch → epoch-scoped facets; a
// generation → the candidate detail; a board entry → the per-board
// cross-candidate view, keyed by ENTRY ID, never an arbitrary candidate).
// The rail is its OWN constrained-scroll column and digest-gates its
// repaint, so a steady heartbeat never rebuilds it.

import { el } from '../../core/dom.js';
import { gatedSwap } from './ui.js';

// Build the rail content into `host`, given the all-epochs workspace model,
// the (optionally) expanded epoch's generations + board, and the active
// selection. Writes are digest-gated.
//   model = { epochs:[{epochId, live, gens, promoted}], selectedEpochId,
//             gens, board, selection }
export function renderRail(host, ctx, model) {
  const { epochs, selectedEpochId, gens, board, selection } = model;
  const sel = selection || {};

  const digest = JSON.stringify({
    epochs: (epochs || []).map((e) => [e.epochId, e.live, e.gens.length, e.promoted]),
    selEpoch: selectedEpochId || '',
    gens: (gens || []).map((g) => [g.id, g.promoted, g.scalar == null ? null : Number(g.scalar).toFixed(2)]),
    board: (board || []).map((b) => [b.id, b.kind]),
    selKind: sel.kind, selId: sel.id, selEpochSel: sel.epoch, selGen: sel.gen, selEntry: sel.entry, selFacet: sel.facet,
  });

  gatedSwap(host, digest, () => {
    const out = [];

    // ---- WORKSPACE (rail top / root) ----------------------------------
    const wsActive = sel.kind === 'workspace';
    const wsHead = el('div', {
      class: 'vo-rail-workspace' + (wsActive ? ' vo-rail-active' : ''),
      tabindex: '0', role: 'button',
    }, [
      el('span', { class: 'vo-rail-workspace-eyebrow', text: 'WORKSPACE' }),
      el('span', { class: 'vo-rail-workspace-sub', text: 'all epochs' }),
    ]);
    const goWs = () => ctx.navigate('workspace', {});
    wsHead.addEventListener('click', goWs);
    wsHead.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); goWs(); } });
    out.push(wsHead);

    // ---- EPOCHS group (all epochs first) ------------------------------
    const epochGroup = el('div', { class: 'vo-rail-group' }, [
      el('div', { class: 'vo-rail-grouphead', text: 'Epochs' }),
    ]);
    const eps = epochs || [];
    if (!eps.length) {
      epochGroup.appendChild(el('p', { class: 'vo-rail-empty', text: 'no epochs yet' }));
    } else {
      const list = el('ul', { class: 'vo-rail-list' });
      for (const e of eps) {
        const expanded = e.epochId === selectedEpochId;
        const epActive = sel.kind === 'epoch' && sel.epoch === e.epochId;
        const item = el('li', {
          class: 'vo-rail-item vo-rail-epoch-item'
            + (epActive ? ' vo-rail-active' : '') + (expanded ? ' vo-rail-expanded' : ''),
          tabindex: '0', role: 'button', 'data-epoch': e.epochId,
        }, [
          el('span', { class: 'vo-rail-disc', text: expanded ? '▾' : '▸' }),
          el('span', { class: 'vo-rail-label vo-mono', text: e.epochId }),
          e.live ? el('span', { class: 'vo-rail-tag vo-live', text: 'live' }) : null,
          el('span', { class: 'vo-rail-scalar', text: `${e.gens.length}g` }),
        ].filter(Boolean));
        const go = () => ctx.navigate('epoch', { epoch: e.epochId, facet: 'overview' });
        item.addEventListener('click', go);
        item.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(); } });
        list.appendChild(item);

        // Under the SELECTED epoch, expand its generations + board.
        if (expanded) list.appendChild(epochChildren(ctx, e, gens, board, sel));
      }
      epochGroup.appendChild(list);
    }
    out.push(epochGroup);

    return out;
  });
}

// The expanded sub-tree under the selected epoch: generations + board.
function epochChildren(ctx, epochMeta, gens, board, sel) {
  const wrap = el('li', { class: 'vo-rail-subtree' });

  // Generations.
  const genGroup = el('div', { class: 'vo-rail-subgroup' }, [
    el('div', { class: 'vo-rail-subhead', text: 'Generations' }),
  ]);
  const gs = gens || [];
  if (!gs.length) {
    genGroup.appendChild(el('p', { class: 'vo-rail-empty', text: 'no generations yet' }));
  } else {
    const list = el('ul', { class: 'vo-rail-list' });
    for (const g of gs) {
      const active = sel.kind === 'gen' && sel.gen === g.id;
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
  wrap.appendChild(genGroup);

  // Board entries — selecting one opens the per-board view (by id).
  const boardGroup = el('div', { class: 'vo-rail-subgroup' }, [
    el('div', { class: 'vo-rail-subhead', text: 'Board · the tests every candidate faces' }),
  ]);
  const bs = board || [];
  if (!bs.length) {
    boardGroup.appendChild(el('p', { class: 'vo-rail-empty', text: 'no board recorded' }));
  } else {
    const list = el('ul', { class: 'vo-rail-list' });
    for (const b of bs) {
      const active = sel.kind === 'board' && sel.entry === b.id;
      const item = el('li', {
        class: 'vo-rail-item vo-rail-board' + (active ? ' vo-rail-active' : ''),
        tabindex: '0', role: 'button', 'data-entry': b.id,
      }, [
        el('span', { class: 'vo-rail-kind vo-kind-' + kindClass(b.kind), text: kindGlyph(b.kind) }),
        el('span', { class: 'vo-rail-label vo-mono', text: b.id }),
      ]);
      const go = () => ctx.navigate('board', { entry: b.id });
      item.addEventListener('click', go);
      item.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(); } });
      list.appendChild(item);
    }
    boardGroup.appendChild(list);
  }
  wrap.appendChild(boardGroup);

  return wrap;
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

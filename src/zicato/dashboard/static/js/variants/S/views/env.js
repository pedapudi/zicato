// variants/S/views/env.js — ENVIRONMENT overview (the all-epochs root).
//
// The tree's root. Structured all-epochs-first even though the live data has
// one epoch: a card per epoch (goal · generations · best scalar · state),
// linking into each epoch's overview. Digest-gated.

import { el } from '../../../core/dom.js';
import * as model from '../model.js';
import { gatedSwap, section, empty, stat } from '../ui.js';

export async function render(host, ctx) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading the workspace…' }));

  const { list, currentId } = await model.epochs();

  const digest = JSON.stringify({
    currentId,
    epochs: list.map((e) => [e.epoch_id, e.closed, e.generation_count, e.promoted_count,
      model.isNum(e.best_scalar) ? e.best_scalar.toFixed(3) : null]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: 'Environment' }),
      el('p', { class: 'dn-lede', text: 'The workspace — every epoch the harness has run. Pick an epoch in the tree (or a card below) to open its overview; drill to a generation to compare candidates side by side.' }),
    ]));

    if (!list.length) {
      nodes.push(empty('No epochs recorded in this workspace yet.'));
      return nodes;
    }

    const grid = el('div', { class: 'vs-epoch-grid' });
    for (const e of list) {
      const card = el('a', {
        class: 'vs-epoch-card' + (e.epoch_id === currentId ? ' vs-epoch-current' : ''),
        href: ctx.href('epoch', { epochId: e.epoch_id }),
      });
      card.addEventListener('click', (ev) => { ev.preventDefault(); ctx.navigate('epoch', { epochId: e.epoch_id }); });
      card.appendChild(el('div', { class: 'vs-epoch-head' }, [
        el('span', { class: 'vs-epoch-id', text: e.epoch_id }),
        el('span', { class: 'dn-chip ' + (e.closed ? 'dn-chip-closed' : 'dn-chip-open'), text: e.epoch_id === currentId ? 'current' : (e.closed ? 'closed' : 'open') }),
      ]));
      if (e.goal) card.appendChild(el('p', { class: 'vs-epoch-goal', text: e.goal }));
      card.appendChild(el('div', { class: 'dn-row', style: 'gap:20px;margin-top:8px;' }, [
        stat(model.isNum(e.generation_count) ? String(e.generation_count) : '—', 'generations'),
        stat(model.isNum(e.promoted_count) ? String(e.promoted_count) : '—', 'promoted'),
        stat(model.isNum(e.best_scalar) ? e.best_scalar.toFixed(1) : '—', 'best scalar'),
      ]));
      grid.appendChild(card);
    }
    nodes.push(section('Epochs · all-epochs-first', grid));
    return nodes;
  });
}

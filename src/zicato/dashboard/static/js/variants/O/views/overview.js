// variants/O/views/overview.js — the detail pane when NOTHING is selected.
//
// A calm landing: the workspace at a glance + the lineage bumps (clickable
// → select a generation) + a one-line prompt to pick something in the rail.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, loading, stat } from '../ui.js';
import { loadRailModel } from '../model.js';

export async function render(host, ctx) {
  if (!host.firstChild) host.appendChild(loading('Reading the workspace…'));
  const m = await loadRailModel();
  const { epochId, gens } = m;

  const digest = JSON.stringify({
    epochId,
    gens: gens.map((g) => [g.id, g.promoted, g.x, g.parent, g.scalar == null ? null : Number(g.scalar).toFixed(2)]),
  });

  gatedSwap(host, digest, () => {
    const out = [];
    out.push(el('div', { class: 'vo-pagehead' }, [
      el('h1', { class: 'vo-h1', text: 'Compass' }),
      el('p', { class: 'vo-lede', text: 'Pick a generation or a board entry in the rail. The selection stays fixed — the detail follows it.' }),
    ]));

    const promoted = gens.filter((g) => g.promoted).length;
    out.push(el('div', { class: 'vo-glance' }, [
      stat(epochId || '—', 'epoch'),
      stat(String(gens.length), 'generations'),
      stat(String(promoted), 'promoted'),
      stat(String(gens.length - promoted), 'dead branches'),
    ]));

    const nodes = gens.map((g) => ({ id: g.id, x: g.x, promoted: g.promoted, parent: g.parent, scalar: g.scalar }));
    out.push(section('Lineage', el('div', { class: 'vo-figure' }, [
      el('div', { class: 'vo-figure-mark' }, [svg.bumps({
        width: 720, height: 200, nodes,
        onClick: (n) => ctx.navigate('gen', { gen: n.id, facet: 'lifecycle' }),
      })]),
      el('figcaption', { class: 'vo-figcaption', text: 'The champion spine with challengers branching off. Click a node to select that generation.' }),
    ])));

    if (!gens.length) out.push(empty('No generations recorded for this epoch yet.'));
    return out;
  });
}

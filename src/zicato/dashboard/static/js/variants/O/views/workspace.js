// variants/O/views/workspace.js — the WORKSPACE (all-epochs) detail pane.
//
// The default / root of Compass (`#/O/`). NOT a single-epoch view: it is an
// overview of ALL epochs in the workspace — cross-epoch trajectory, the
// per-epoch lineage at a glance, and the recent promote decisions. Each
// epoch card selects that epoch (epoch scope); each generation node selects
// that candidate. The structure is all-epochs-first; with only one epoch in
// the live data it degrades gracefully to a single epoch group.

import { el } from '../../../core/dom.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, loading, stat } from '../ui.js';
import { loadWorkspaceModel } from '../model.js';

export async function render(host, ctx) {
  if (!host.firstChild) host.appendChild(loading('Reading the workspace…'));
  const ws = await loadWorkspaceModel();
  const { epochs, liveEpochId, trajPoints } = ws;

  const totalGens = epochs.reduce((a, e) => a + e.gens.length, 0);
  const totalPromoted = epochs.reduce((a, e) => a + e.promoted, 0);

  const digest = JSON.stringify({
    liveEpochId,
    epochs: epochs.map((e) => [e.epochId, e.live, e.gens.map((g) => [g.id, g.promoted, g.x, g.parent,
      g.scalar == null ? null : Number(g.scalar).toFixed(2)])]),
    traj: trajPoints.length,
  });

  gatedSwap(host, digest, () => {
    const out = [];
    out.push(el('div', { class: 'vo-pagehead' }, [
      el('h1', { class: 'vo-h1', text: 'Workspace' }),
      el('p', { class: 'vo-lede', text: 'Every epoch in this workspace. Pick an epoch to read its publication, mutation surface, and lineage; pick a generation to follow one candidate. The selection stays fixed — the detail follows it.' }),
    ]));

    out.push(el('div', { class: 'vo-glance' }, [
      stat(String(epochs.length), epochs.length === 1 ? 'epoch' : 'epochs'),
      stat(String(totalGens), 'generations'),
      stat(String(totalPromoted), 'promoted'),
      stat(String(totalGens - totalPromoted), 'dead branches'),
    ]));

    // Cross-epoch score trajectory (workspace-wide evolution curve), when
    // the workspace carries one.
    if (trajPoints.length > 1) {
      const items = trajPoints.map((p, i) => ({
        id: String(p.generation_id || p.epoch_id || i),
        label: String(p.generation_id || p.epoch_id || i),
        value: svg.isNum(p.scalar) ? p.scalar : (svg.isNum(p.loss) ? p.loss : null),
      })).filter((d) => svg.isNum(d.value));
      if (items.length) {
        out.push(section('Score trajectory · across the workspace',
          el('div', { class: 'vo-figure' }, [
            el('div', { class: 'vo-figure-mark' }, [svg.valueDotPlot({
              width: 640, rowHeight: 22, labelWidth: 160, items,
            })]),
            el('figcaption', { class: 'vo-figcaption', text: 'The workspace-wide scalar (loss) trajectory; lower is better.' }),
          ])));
      }
    }

    // Per-epoch lineage at a glance — one card per epoch (all-epochs first).
    if (!epochs.length) {
      out.push(empty('No epochs recorded in this workspace yet.'));
    } else {
      const cards = el('div', { class: 'vo-epoch-cards' });
      for (const e of epochs) {
        const nodes = e.gens.map((g) => ({ id: g.id, x: g.x, promoted: g.promoted, parent: g.parent, scalar: g.scalar }));
        const card = el('div', { class: 'vo-epoch-card', tabindex: '0', role: 'button', 'data-epoch': e.epochId }, [
          el('div', { class: 'vo-epoch-card-head' }, [
            el('span', { class: 'vo-eyebrow', text: 'EPOCH' }),
            el('span', { class: 'vo-mono vo-epoch-card-id', text: e.epochId }),
            e.live ? el('span', { class: 'vo-pill vo-live', text: 'live' }) : null,
            el('span', { class: 'vo-faint', text: `${e.gens.length} gen · ${e.promoted} promoted` }),
          ].filter(Boolean)),
          el('div', { class: 'vo-figure' }, [
            el('div', { class: 'vo-figure-mark' }, [svg.bumps({
              width: 680, height: 180, nodes,
              onClick: (n) => ctx.navigate('gen', { gen: n.id, facet: 'lifecycle' }),
            })]),
            el('figcaption', { class: 'vo-figcaption', text: 'The champion spine with challengers branching off. Click a node to select that generation, or the card to open the epoch.' }),
          ]),
        ]);
        const go = () => ctx.navigate('epoch', { epoch: e.epochId, facet: 'overview' });
        // Card click opens the epoch, but a node click (handled by the SVG)
        // selects the generation — guard so they don't both fire.
        card.addEventListener('click', (ev) => {
          const t = ev.target;
          const onNode = t && t.getAttribute && (t.getAttribute('data-vo') === 'bump-node'
            || (t.parentNode && t.parentNode.getAttribute && t.parentNode.getAttribute('data-vo') === 'bump-node'));
          if (!onNode) go();
        });
        card.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(); } });
        cards.appendChild(card);
      }
      out.push(section('Epochs · lineage at a glance', cards));
    }

    return out;
  });
}

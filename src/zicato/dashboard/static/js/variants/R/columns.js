// variants/R/columns.js — the Miller-columns cascade (Strata's identity).
//
// Strata navigates the data model as macOS-Finder-style CASCADING COLUMNS, not
// a nested accordion. Each column's selection drives the NEXT column; the
// rightmost pane is the detail. Three navigator columns:
//
//   Col 1 — Environment → the epoch(s).
//   Col 2 — the selected epoch's SECTIONS:
//             Generations · Boards · Mutation surface · Publication.
//   Col 3 — ITEMS in the selected section:
//             the generations list, or the board entries list.
//             (Mutation surface / Publication have no items — they go straight
//              to the detail pane.)
//
// Each column is independently scrollable and digest-gated: a column rebuilds
// ONLY when its own structural data (or its selection) changes — a heartbeat
// re-dispatch with identical data writes ZERO DOM. Selection is explicit and
// URL-encoded, so a cold deep-link reconstructs the whole column path.

import { el } from '../../core/dom.js';
import { gatedSwap, normaliseDecision } from './ui.js';

const SECTION_LABEL = {
  generations: 'Generations', boards: 'Boards',
  mutations: 'Mutation surface', publication: 'Publication',
};
const SECTION_SUB = {
  generations: 'lifecycle · gate · matchups · diff', boards: 'per-board cross-candidate',
  mutations: 'site × generation diff', publication: 'epoch ACM paper',
};

// Build one column: a titled, scrollable list of rows. `rows` are
// {key,label,sub,active,onClick,badge?}. Digest-gated on its own structural
// content (+ which row is active).
function column(host, title, rows, opts) {
  const o = opts || {};
  const digest = title + '::' + (o.empty || '') + '::' + JSON.stringify(rows.map((r) => [r.key, r.label, r.sub || '', !!r.active, r.badge || '']));
  gatedSwap(host, digest, () => {
    const col = el('div', { class: 'dr-col-inner' });
    col.appendChild(el('div', { class: 'dr-col-title', text: title }));
    if (!rows.length) {
      col.appendChild(el('p', { class: 'dr-col-empty', text: o.empty || 'nothing here' }));
      return [col];
    }
    const list = el('div', { class: 'dr-col-list', role: 'listbox' });
    for (const r of rows) {
      const row = el('button', { class: 'dr-col-row' + (r.active ? ' dr-col-active' : ''), type: 'button', role: 'option', 'aria-selected': r.active ? 'true' : 'false' }, [
        el('span', { class: 'dr-col-row-main' }, [
          el('span', { class: 'dr-col-row-label', text: r.label }),
          r.sub ? el('span', { class: 'dr-col-row-sub', text: r.sub }) : null,
        ].filter(Boolean)),
        r.badge ? el('span', { class: 'dr-col-badge dr-col-badge-' + (r.badgeCls || 'flat'), text: r.badge }) : null,
        el('span', { class: 'dr-col-chevron', 'aria-hidden': 'true', text: '›' }),
      ].filter(Boolean));
      if (r.onClick) row.addEventListener('click', r.onClick);
      list.appendChild(row);
    }
    col.appendChild(list);
    return [col];
  });
}

// ---- column 1: environment → epochs --------------------------------

export function renderEpochColumn(host, ctx, model) {
  const path = model.path;
  const rows = model.epochs.map((e) => ({
    key: e.epoch_id, label: e.epoch_id, sub: e.goal || '',
    badge: e.closed ? 'closed' : 'open', badgeCls: e.closed ? 'flat' : 'good',
    active: path.epoch === e.epoch_id,
    onClick: () => ctx.navigate({ epoch: e.epoch_id }),
  }));
  column(host, 'Environment', rows, { empty: 'no epochs' });
}

// ---- column 2: the epoch's sections --------------------------------

export function renderSectionColumn(host, ctx, model) {
  const path = model.path;
  if (!path.epoch) { gatedSwap(host, 'no-epoch-sections', () => [el('div', { class: 'dr-col-inner' }, [el('p', { class: 'dr-col-empty', text: 'select an epoch →' })])]); return; }
  const rows = ['generations', 'boards', 'mutations', 'publication'].map((s) => ({
    key: s, label: SECTION_LABEL[s], sub: SECTION_SUB[s],
    active: path.section === s,
    onClick: () => ctx.navigate({ epoch: path.epoch, section: s }),
  }));
  column(host, 'Epoch ' + path.epoch, rows, { empty: 'no sections' });
}

// ---- column 3: items in the selected section -----------------------

export function renderItemColumn(host, ctx, model) {
  const path = model.path;
  if (!path.epoch || !path.section) { gatedSwap(host, 'no-items', () => [el('div', { class: 'dr-col-inner' }, [el('p', { class: 'dr-col-empty', text: 'select a section →' })])]); return; }

  if (path.section === 'generations') {
    const rows = model.gens.map((g) => ({
      key: g.id, label: g.id + (g.promoted ? ' ♛' : ''),
      sub: g.parent ? 'from ' + g.parent : 'seed (v0)',
      badge: g.decision, badgeCls: g.decision === 'promoted' ? 'good' : g.decision === 'rejected' ? 'bad' : 'flat',
      active: path.gen === g.id,
      onClick: () => ctx.navigate({ epoch: path.epoch, section: 'generations', gen: g.id }),
    }));
    column(host, 'Generations', rows, { empty: 'no generations' });
    return;
  }
  if (path.section === 'boards') {
    const rows = model.boards.map((b) => ({
      key: b.id, label: b.id, sub: b.kind || '',
      active: path.entry === b.id,
      onClick: () => ctx.navigate({ epoch: path.epoch, section: 'boards', entry: b.id }),
    }));
    column(host, 'Boards', rows, { empty: 'no board entries' });
    return;
  }
  // mutations / publication: no items — the section IS the detail.
  const label = SECTION_LABEL[path.section];
  gatedSwap(host, 'noitem-' + path.section, () => [el('div', { class: 'dr-col-inner' }, [
    el('div', { class: 'dr-col-title', text: label }),
    el('p', { class: 'dr-col-empty', text: 'this section opens straight in the detail pane →' }),
  ])]);
}

// Derive the list models (generations + boards) from the loaded epoch +
// lineage payloads — pure, so the shell can digest + reuse them.
export function deriveModel(path, epoch, lineage, workspace) {
  const epochs = (workspace && Array.isArray(workspace.epochs)) ? workspace.epochs.map((e) => ({
    epoch_id: e.epoch_id, goal: e.goal || '', closed: !!e.closed,
  })) : (epoch && epoch.epoch_id ? [{ epoch_id: epoch.epoch_id, goal: epoch.goal || '', closed: !!epoch.closed }] : []);

  const experiments = (epoch && Array.isArray(epoch.experiments)) ? epoch.experiments : [];
  const lineGens = (lineage && Array.isArray(lineage.generations)) ? lineage.generations : [];
  const gens = (lineGens.length ? lineGens.map((g) => ({
    id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted,
  })) : experiments.map((x) => ({
    id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted',
  }))).map((g) => ({
    ...g,
    decision: !g.parent ? 'seed' : (g.promoted ? 'promoted' : decisionFor(experiments, g.id)),
  }));

  const boards = ((epoch && Array.isArray(epoch.board)) ? epoch.board : []).map((b) => ({
    id: b.entry_id || b.id, kind: b.kind || '',
  }));

  return { path, epochs, gens, boards };
}

function decisionFor(experiments, id) {
  const x = experiments.find((e) => e.generation_id === id);
  return x ? (normaliseDecision(x.outcome) || 'rejected') : 'rejected';
}

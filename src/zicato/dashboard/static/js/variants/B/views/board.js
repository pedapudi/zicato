// variants/B/views/board.js — "The Board" view (theme 2).
//
// The fixed task suite every candidate in the epoch faces, set as a
// considered editorial PLATE — a typeset figure, not a grid of cells and
// emphatically not the rejected tables. Entries are grouped by `kind`
// (single_turn / multi_turn_scripted / multi_turn_emulated), each rendered as
// a quiet card: the entry id as a small heading, its input preview as a
// pull-line, tags set in small caps, and budget + weight hung as fine
// marginal annotations. A figure caption closes the plate.
//
// Data: state.epochDef.board (folded by the env poll) or /api/epoch.

import { el, clearChildren } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import { bRouter } from '../router.js';
import { registerBView } from '../shell.js';
import { makeCache, currentEpochId, boardEntries } from '../lib/data.js';
import { section, note } from '../lib/prose.js';
import { fin } from '../lib/charts.js';

let _epochCache = null;
function repaint() {
  const host = document.getElementById('vb-page');
  if (host && bRouter.current().view === 'board') renderBoard(host, bRouter.current());
}
function caches() { if (!_epochCache) _epochCache = makeCache(repaint); return _epochCache; }
export function resetBoardView() { _epochCache = null; }

// Human-set names for the three kinds.
const KIND_META = {
  single_turn: { name: 'Single-turn tasks', dek: 'One prompt, one response — scored against a predicate or rubric.' },
  multi_turn_scripted: { name: 'Multi-turn, scripted', dek: 'A fixed script of follow-ups the agent must carry.' },
  multi_turn_emulated: { name: 'Multi-turn, emulated', dek: 'A collusion-guarded emulator plays a demanding interlocutor.' },
  multi_turn: { name: 'Multi-turn tasks', dek: 'A conversation the agent must carry across turns.' },
};
function kindMeta(kind) {
  return KIND_META[kind] || { name: kind ? String(kind).replace(/_/g, ' ') : 'Other', dek: '' };
}

function budgetLabel(s) {
  if (!fin(s)) return null;
  if (s >= 60) {
    const m = s / 60;
    return (Number.isInteger(m) ? m : m.toFixed(1)) + ' min';
  }
  return s + ' s';
}

// One board entry as a typeset plate card.
function entryCard(entry) {
  const id = String(entry.id);
  const preview = (typeof entry.input_preview === 'string' && entry.input_preview.trim())
    ? entry.input_preview.trim() : null;
  const tags = Array.isArray(entry.tags) ? entry.tags.filter((t) => t != null) : [];
  const budget = budgetLabel(entry.budget_s);
  const weight = fin(entry.weight) ? entry.weight : null;
  const expectation = (typeof entry.expectation_kind === 'string' && entry.expectation_kind)
    ? entry.expectation_kind.replace(/_/g, ' ') : null;

  return el('article', { class: 'vb-plate-card' }, [
    el('div', { class: 'vb-plate-card-main' }, [
      el('h3', { class: 'vb-plate-card-id vb-mono' }, [id]),
      preview
        ? el('p', { class: 'vb-plate-card-preview' }, ['“', preview, '”'])
        : el('p', { class: 'vb-plate-card-preview vb-muted' }, [
            'A multi-turn exchange — no single opening prompt to quote.',
          ]),
      tags.length
        ? el('p', { class: 'vb-plate-card-tags' }, tags.map((t) => el('span', { class: 'vb-smallcaps-tag' }, [String(t)])))
        : null,
    ].filter(Boolean)),
    // Fine marginal annotations: budget, weight, expectation kind.
    el('aside', { class: 'vb-plate-card-margin', 'aria-label': 'entry annotations' }, [
      budget ? el('span', { class: 'vb-plate-anno' }, [
        el('span', { class: 'vb-plate-anno-k' }, ['budget']),
        el('span', { class: 'vb-plate-anno-v vb-mono' }, [budget]),
      ]) : null,
      weight != null ? el('span', { class: 'vb-plate-anno' }, [
        el('span', { class: 'vb-plate-anno-k' }, ['weight']),
        el('span', { class: 'vb-plate-anno-v vb-mono' }, ['×' + weight]),
      ]) : null,
      expectation ? el('span', { class: 'vb-plate-anno' }, [
        el('span', { class: 'vb-plate-anno-k' }, ['judged by']),
        el('span', { class: 'vb-plate-anno-v' }, [expectation]),
      ]) : null,
    ].filter(Boolean)),
  ]);
}

export function renderBoard(host, _route) {
  if (!host) return;
  const epochId = currentEpochId();
  const c = caches();
  if (epochId && (!state.epochDef || !Array.isArray(state.epochDef.board) || !state.epochDef.board.length)) {
    c.ensure(epochId, '/api/epoch', { epoch_id: epochId, board: [], __broken: true });
  }

  // Prefer state.epochDef.board; fall back to the fetched payload.
  let entries = boardEntries();
  if (!entries.length && epochId && c.has(epochId)) {
    const fetched = c.get(epochId);
    entries = (fetched && Array.isArray(fetched.board)) ? fetched.board.filter((b) => b && b.id != null) : [];
  }

  clearChildren(host);

  host.appendChild(el('div', { class: 'vb-board-lead' }, [
    el('p', { class: 'vb-eyebrow' }, [
      'The board',
      epochId ? el('span', { class: 'vb-muted' }, [' · ', el('span', { class: 'vb-mono' }, [String(epochId)])]) : null,
    ].filter(Boolean)),
    el('h1', { class: 'vb-page-title' }, ['The suite every candidate faces']),
    el('p', { class: 'vb-env-dek' }, [
      'The board is fixed for the epoch — every generation runs all of it, paired against the champion. ',
      'Grouped below by kind; budgets and weights are set in the margin.',
    ]),
  ]));

  if (!entries.length) {
    host.appendChild(note('empty', {
      label: 'No board recorded for this epoch',
      detail: 'The epoch contract lists its board entries here once a run has been registered.',
    }));
    return;
  }

  // Group by kind, in a stable, meaningful order.
  const ORDER = ['single_turn', 'multi_turn_scripted', 'multi_turn_emulated', 'multi_turn'];
  const byKind = new Map();
  for (const e of entries) {
    const k = e.kind || 'other';
    if (!byKind.has(k)) byKind.set(k, []);
    byKind.get(k).push(e);
  }
  const kinds = [...byKind.keys()].sort((a, b) => {
    const ia = ORDER.indexOf(a); const ib = ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });

  const totalWeight = entries.reduce((s, e) => s + (fin(e.weight) ? e.weight : 0), 0);

  const plate = el('div', { class: 'vb-plate' }, kinds.map((kind) => {
    const meta = kindMeta(kind);
    const group = byKind.get(kind);
    return el('div', { class: 'vb-plate-group' }, [
      el('div', { class: 'vb-plate-group-head' }, [
        el('h2', { class: 'vb-plate-group-name' }, [meta.name]),
        el('span', { class: 'vb-plate-group-count vb-muted' }, [
          `${group.length} ${group.length === 1 ? 'entry' : 'entries'}`,
        ]),
        meta.dek ? el('p', { class: 'vb-plate-group-dek vb-muted' }, [meta.dek]) : null,
      ].filter(Boolean)),
      el('div', { class: 'vb-plate-cards' }, group.map(entryCard)),
    ]);
  }));

  host.appendChild(section('The plate', [
    plate,
    el('figcaption', { class: 'vb-plate-caption' }, [
      el('span', { class: 'vb-plate-caption-no' }, ['Fig. ', el('em', null, ['board'])]),
      `${entries.length} board ${entries.length === 1 ? 'entry' : 'entries'} across `
      + `${kinds.length} ${kinds.length === 1 ? 'kind' : 'kinds'}`
      + (totalWeight ? `; total weight ×${Number.isInteger(totalWeight) ? totalWeight : totalWeight.toFixed(1)}` : '')
      + '. The same suite is run, paired, under every challenger.',
    ]),
  ], { sub: 'A figure, not a table — each entry with its budget and weight in the margin.' }));
}

registerBView('board', renderBoard);

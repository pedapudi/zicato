// variants/A/components/sortie.js — Themes 2 & 3: the sortie board.
//
// The fixed per-epoch board, rendered as a status-light tile grid — the
// field of tests a candidate "faces" on its sortie. Each tile shows:
//   kind (single_turn / multi_turn_scripted / multi_turn_emulated),
//   budget_s, weight, tags, and — when scored — a status LAMP
//   (pass = green improve, fail = red regress, timeout = amber caution)
//   plus a horizontal loss bar. Clicking a tile fires onSelect so a view
//   can slide in the instrument panel (theme 3 depth 2/3).
//
// Pure builders: (data) -> DOM node. No fetch, no module state.

import { el } from '../../../core/dom.js';
import { bar, empty } from './instruments.js';

const KIND_LABEL = {
  single_turn: 'single',
  multi_turn_scripted: 'scripted',
  multi_turn_emulated: 'emulated',
};
const KIND_GLYPH = {
  single_turn: '◆',
  multi_turn_scripted: '⋯◆',
  multi_turn_emulated: '⟳◆',
};

function fmt(v, d = 1) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(d) : '—'; }

// Resolve the status lamp for an entry from its score record (if any).
//   score: { drift_loss, pass_fail (0|1|null), wall_clock_budget_exceeded }
// Returns { light, label } where light ∈ go|stop|warn|idle.
export function lampFor(score) {
  if (!score) return { light: 'idle', label: 'unflown' };
  if (score.wall_clock_budget_exceeded) return { light: 'warn', label: 'timeout' };
  if (score.pass_fail === 1) return { light: 'go', label: 'pass' };
  if (score.pass_fail === 0) return { light: 'stop', label: 'fail' };
  return { light: 'idle', label: 'no predicate' };
}

// One sortie tile.
//   entry: board entry from /api/epoch board[]
//   score: per-entry score (optional) keyed by entry_id
//   maxLoss: scale for the loss bar
//   selected: highlight flag
//   onSelect(entry, score): click handler
function tile(entry, score, maxLoss, selected, onSelect) {
  const kind = entry.kind || 'single_turn';
  const lamp = lampFor(score);
  const node = el('div', {
    class: 'mcA-sortie-tile' + (selected ? ' is-selected' : '') + (onSelect ? ' is-clickable' : ''),
    'data-light': lamp.light,
    'data-kind': kind,
    role: onSelect ? 'button' : null,
    tabindex: onSelect ? '0' : null,
    'aria-label': 'board entry ' + (entry.id || '') + ' — ' + lamp.label,
  });

  // header: lamp + id + kind
  node.appendChild(el('div', { class: 'mcA-sortie-tile-head' }, [
    el('span', { class: 'mcA-sortie-lamp', 'data-light': lamp.light }),
    el('span', { class: 'mcA-sortie-id mono' }, [entry.id || '?']),
    el('span', { class: 'mcA-sortie-kind mono', title: kind }, [
      (KIND_GLYPH[kind] || '◆') + ' ' + (KIND_LABEL[kind] || kind),
    ]),
  ]));

  // input preview (single-turn only carries one)
  if (entry.input_preview) {
    node.appendChild(el('div', { class: 'mcA-sortie-preview' }, [truncate(entry.input_preview, 84)]));
  } else {
    node.appendChild(el('div', { class: 'mcA-sortie-preview is-muted mono' }, [
      kind === 'single_turn' ? '(no preview)' : '(multi-turn script)',
    ]));
  }

  // loss bar + scalar (theme 3 depth 1: per-board scoring inline)
  if (score && typeof score.drift_loss === 'number' && isFinite(score.drift_loss)) {
    const frac = maxLoss > 0 ? score.drift_loss / maxLoss : 0;
    const tone = lamp.light === 'go' ? 'go' : lamp.light === 'warn' ? 'warn' : 'stop';
    node.appendChild(el('div', { class: 'mcA-sortie-loss' }, [
      bar(frac, tone),
      el('span', { class: 'mcA-sortie-loss-val mono' }, [fmt(score.drift_loss, 1)]),
    ]));
  } else {
    node.appendChild(el('div', { class: 'mcA-sortie-loss is-empty mono' }, ['loss —']));
  }

  // foot: budget · weight · status label
  node.appendChild(el('div', { class: 'mcA-sortie-foot mono' }, [
    el('span', null, ['⏱ ' + (entry.budget_s != null ? fmt(entry.budget_s, 0) + 's' : '—')]),
    el('span', null, ['×' + (entry.weight != null ? fmt(entry.weight, 1) : '1.0')]),
    el('span', { class: 'mcA-sortie-status', 'data-light': lamp.light }, [lamp.label]),
  ]));

  // tags
  const tags = Array.isArray(entry.tags) ? entry.tags.filter(Boolean) : [];
  if (tags.length) {
    node.appendChild(el('div', { class: 'mcA-sortie-tags' },
      tags.slice(0, 5).map((t) => el('span', { class: 'mcA-sortie-tag mono' }, [t]))));
  }

  if (onSelect && entry.id) {
    node.addEventListener('click', () => onSelect(entry, score));
    node.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') onSelect(entry, score); });
  }
  return node;
}

function truncate(s, n) {
  s = String(s || '');
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

// The sortie board grid.
//   board: array of board entries (from /api/epoch)
//   scoresById: Map<entry_id, score> (optional — when a candidate is set)
//   selectedId: highlighted tile
//   onSelect(entry, score): tile click
export function sortieBoard({ board, scoresById, selectedId, onSelect }) {
  board = Array.isArray(board) ? board : [];
  if (!board.length) return empty('No board entries recorded for this epoch.');
  scoresById = scoresById || new Map();

  // scale loss bars against the worst observed loss across the board.
  let maxLoss = 0;
  for (const e of board) {
    const sc = scoresById.get(e.id);
    if (sc && typeof sc.drift_loss === 'number' && isFinite(sc.drift_loss)) {
      maxLoss = Math.max(maxLoss, sc.drift_loss);
    }
  }

  const grid = el('div', { class: 'mcA-sortie-grid' });
  for (const e of board) {
    const sc = scoresById.get(e.id) || null;
    grid.appendChild(tile(e, sc, maxLoss, e.id === selectedId, onSelect));
  }
  return grid;
}

// A compact tally strip for the board: counts by lamp + by kind.
export function sortieTally(board, scoresById) {
  board = Array.isArray(board) ? board : [];
  scoresById = scoresById || new Map();
  let pass = 0, fail = 0, timeout = 0, unflown = 0;
  for (const e of board) {
    const lamp = lampFor(scoresById.get(e.id));
    if (lamp.light === 'go') pass += 1;
    else if (lamp.light === 'stop') fail += 1;
    else if (lamp.light === 'warn') timeout += 1;
    else unflown += 1;
  }
  const seg = (n, light, label) => el('span', { class: 'mcA-sortie-tally-seg', 'data-light': light }, [
    el('b', { class: 'mono' }, [String(n)]), ' ' + label,
  ]);
  return el('div', { class: 'mcA-sortie-tally mono' }, [
    el('span', { class: 'mcA-sortie-tally-total' }, [board.length + ' entries']),
    seg(pass, 'go', 'pass'),
    seg(fail, 'stop', 'fail'),
    seg(timeout, 'warn', 'timeout'),
    seg(unflown, 'idle', 'unflown'),
  ]);
}

export { KIND_LABEL };

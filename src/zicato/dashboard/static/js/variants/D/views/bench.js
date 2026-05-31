// variants/D/views/bench.js — the board bench, reachable from nav.
//
// A quiet read of the current epoch's board: each entry with its typed
// expectations (Predicate / Rubric) and in-run judges, so the operator
// can see the evaluation substrate the loop scores against. Numbers that
// have no trend stay as a tight table; the bench is reference, not a
// dashboard of motion.
//
// Data: /api/epoch (board, scoring).

import { el, clearChildren } from '../../../core/dom.js';
import * as D from '../data.js';
import { section, crumb, empty, loading } from '../ui.js';

export async function render(host) {
  clearChildren(host);
  host.appendChild(crumb([{ label: 'environment', view: 'environment' }, { label: 'bench' }]));
  host.appendChild(el('h1', { class: 'd-h1', text: 'Bench · board' }));
  host.appendChild(el('p', { class: 'd-lede', text: 'The task board the epoch scores against — each entry’s typed expectations and judges.' }));

  const body = el('div'); host.appendChild(body);
  body.appendChild(loading('Reading board…'));
  const ep = await D.epoch();
  clearChildren(body);
  if (!ep || ep.epoch_id == null) { body.appendChild(empty('No current epoch.')); return; }
  const board = Array.isArray(ep.board) ? ep.board : [];
  if (!board.length) { body.appendChild(empty('No board entries recorded.')); return; }

  const table = el('table', { class: 'd-tbl' });
  table.appendChild(el('thead', null, [el('tr', null, [
    el('th', { text: 'entry' }), el('th', { text: 'kind' }),
    el('th', { text: 'expectations' }), el('th', { text: 'judges' }),
  ])]));
  const tb = el('tbody');
  for (const b of board) {
    const id = b.entry_id || b.id || '—';
    const kind = b.kind || (Array.isArray(b.turns) ? 'multi-turn' : 'single-turn');
    const exps = collect(b, ['expectations', 'expectation']);
    const judges = collect(b, ['judges', 'judge']);
    tb.appendChild(el('tr', null, [
      el('td', { class: 'd-mono', text: String(id) }),
      el('td', { class: 'd-faint', text: String(kind) }),
      el('td', { text: exps.length ? exps.map(describeExpect).join('; ') : '—' }),
      el('td', { class: 'd-faint', text: judges.length ? judges.map(describeJudge).join(', ') : '—' }),
    ]));
  }
  table.appendChild(tb);
  body.appendChild(section(`${board.length} board entries`, el('div', { class: 'd-panel', style: 'overflow-x:auto;' }, [table])));
}

function collect(obj, keys) {
  for (const k of keys) {
    const v = obj[k];
    if (Array.isArray(v)) return v;
    if (v && typeof v === 'object') return [v];
  }
  return [];
}
function describeExpect(e) {
  if (typeof e === 'string') return e;
  if (!e || typeof e !== 'object') return String(e);
  const t = e.type || e.kind || (e.predicate ? 'Predicate' : e.rubric ? 'Rubric' : 'expectation');
  const detail = e.predicate || e.rubric || e.description || e.outcome || '';
  return detail ? `${t}: ${truncate(String(detail), 40)}` : t;
}
function describeJudge(j) {
  if (typeof j === 'string') return j;
  if (!j || typeof j !== 'object') return String(j);
  return j.judge_name || j.name || j.type || 'judge';
}
function truncate(s, n) { return s.length > n ? s.slice(0, n - 1) + '…' : s; }

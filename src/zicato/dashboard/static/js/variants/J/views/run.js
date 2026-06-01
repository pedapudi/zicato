// variants/J/views/run.js — RUN DETAIL: the transcript.
//
// The deepest screen: one run's reconstructed conversation. Deep-link
// hydration is first-class — on a COLD deep-link (#/J/run/v1/waffles_single
// opened directly) this view resolves the run_id from per-entry, then FETCHES
// /api/conversation/{run_id} itself: loading → content, NEVER an empty panel.
// The transcript turns render in normal block flow inside a single
// constrained, scrollable container — no absolute positioning that overlaps.
//
// Data: /api/generation/{e}/{g}/per-entry (the run_id),
// /api/conversation/{run_id} (the transcript).

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat } from '../ui.js';

export async function render(host, ctx, params) {
  const genId = params && params.gen;
  const entryId = params && params.entry;

  if (!host.firstChild) host.appendChild(el('p', { class: 'dj-empty', text: 'Loading run…' }));

  if (!genId || !entryId) {
    gatedSwap(host, 'no-run', () => [
      el('h1', { class: 'dj-h1', text: 'Run' }),
      empty('No run selected — open a run from a candidate’s per-board scoring or a match-up duel.'),
    ]);
    return;
  }

  const ep = await D.epoch();
  const epochId = (ep && ep.epoch_id) || (state.epochDef && state.epochDef.epoch_id) || null;
  let row = null, runId = null, conv = null;
  if (epochId) {
    const pe = await D.perEntry(epochId, genId);
    row = (pe && Array.isArray(pe.entries)) ? pe.entries.find((e) => e.entry_id === entryId) : null;
    runId = row ? row.run_id : null;
  }
  if (runId) conv = await D.conversation(runId);

  const turns = (conv && Array.isArray(conv.turns)) ? conv.turns : [];
  const anns = (conv && Array.isArray(conv.annotations)) ? conv.annotations : [];

  const digest = JSON.stringify({
    genId, entryId, runId,
    loss: row && svg.isNum(row.drift_loss) ? row.drift_loss.toFixed(3) : null,
    pass: row ? row.pass_fail : null,
    err: conv && conv.error ? conv.error : null,
    turns: turns.map((t) => [t.seq, t.role, t.agent, t.kind, (t.text || '').length, Array.isArray(t.tool_calls) ? t.tool_calls.length : 0]),
    anns: anns.map((a) => [a.anchor_seq, a.kind, a.summary]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dj-pagehead' }, [
      el('h1', { class: 'dj-h1', text: `Run · ${entryId}` }),
      el('p', { class: 'dj-lede' }, [
        `The reconstructed conversation for ${genId} on this board entry`,
        runId ? el('span', { class: 'dj-faint dj-mono', text: '  ' + runId.slice(0, 12) + '…' }) : null,
      ].filter(Boolean)),
    ]));

    nodes.push(el('div', { class: 'dj-panel dj-row' }, [
      stat(row && svg.isNum(row.drift_loss) ? svg.fmt(row.drift_loss, 1) : '—', 'drift loss'),
      stat(row ? passLabel(row.pass_fail) : '—', 'predicate'),
      stat(row && row.wall_clock_budget_exceeded ? 'timed out' : (row && svg.isNum(row.runtime_ms) ? `${(row.runtime_ms / 1000).toFixed(0)}s` : '—'), 'runtime'),
      stat(genId, 'candidate'),
    ]));

    nodes.push(section('Transcript · the run itself', transcriptPanel(conv, runId, turns, anns)));
    return nodes;
  });
}

function transcriptPanel(conv, runId, turns, anns) {
  const card = el('div', { class: 'dj-panel' });
  if (!runId) { card.appendChild(empty('No run id for this entry — the transcript is unavailable.')); return card; }
  if (conv && conv.error) { card.appendChild(empty(conv.error)); return card; }
  if (!conv) { card.appendChild(empty('Transcript unavailable (the conversation could not be reconstructed).')); return card; }
  if (!turns.length) { card.appendChild(empty('No turns reconstructed (the run may have produced no conversation).')); return card; }

  const annBySeq = new Map();
  for (const a of anns) {
    const k = a.anchor_seq;
    if (!annBySeq.has(k)) annBySeq.set(k, []);
    annBySeq.get(k).push(a);
  }

  const scroller = el('div', { class: 'dj-transcript' });
  for (const t of turns) {
    const turn = el('div', { class: 'dj-turn dj-turn-' + (t.role || 'agent') }, [
      el('div', { class: 'dj-turn-head dj-faint dj-mono' }, [
        el('span', { text: t.agent || t.role || 'turn' }),
        t.kind ? el('span', { text: ' · ' + t.kind }) : null,
      ].filter(Boolean)),
      t.text ? el('div', { class: 'dj-turn-text', text: t.text }) : null,
    ].filter(Boolean));
    if (Array.isArray(t.tool_calls)) for (const tc of t.tool_calls) {
      turn.appendChild(el('div', { class: 'dj-tool dj-mono', text: '⚙ ' + (tc.name || tc.tool || 'tool') }));
    }
    for (const a of (annBySeq.get(t.seq) || [])) {
      turn.appendChild(el('div', { class: 'dj-annot dj-annot-' + (a.kind || 'note'), text: '◂ ' + (a.summary || a.kind) }));
    }
    scroller.appendChild(turn);
  }
  card.appendChild(scroller);
  return card;
}

function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}

// variants/Q/views/run.js — RUN DETAIL: the transcript.
//
// The deepest screen: one run's reconstructed conversation. Deep-link
// hydration is first-class — on a COLD deep-link this view resolves the run_id
// from per-entry, then FETCHES /api/conversation/{run_id} itself: loading →
// content, NEVER an empty panel. The transcript turns render in normal block
// flow inside a single constrained, scrollable container.
//
// (Note: the BOARD view already shows two transcripts INLINE, side by side —
// this is the deep single-run drill for when the operator wants only one.)
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

  if (!host.firstChild) host.appendChild(el('p', { class: 'dq-empty', text: 'Loading run…' }));

  if (!genId || !entryId) {
    gatedSwap(host, 'no-run', () => [
      el('h1', { class: 'dq-h1', text: 'Run' }),
      empty('No run selected — open a run from a candidate’s per-board scoring or a match-up duel.'),
    ]);
    return;
  }

  const ep = await D.epoch();
  const epochId = (params && params.epochId) || (ep && ep.epoch_id) || (state.epochDef && state.epochDef.epoch_id) || null;
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
    epochId, genId, entryId, runId,
    loss: row && svg.isNum(row.drift_loss) ? row.drift_loss.toFixed(3) : null,
    pass: row ? row.pass_fail : null,
    err: conv && conv.error ? conv.error : null,
    turns: turns.map((t) => [t.seq, t.role, t.agent, t.kind, (t.text || '').length, Array.isArray(t.tool_calls) ? t.tool_calls.length : 0]),
    anns: anns.map((a) => [a.anchor_seq, a.kind, a.summary]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dq-pagehead' }, [
      el('h1', { class: 'dq-h1', text: `Run · ${entryId}` }),
      el('p', { class: 'dq-lede' }, [
        `The reconstructed conversation for ${genId} on this board entry`,
        runId ? el('span', { class: 'dq-faint dq-mono', text: '  ' + runId.slice(0, 12) + '…' }) : null,
      ].filter(Boolean)),
    ]));

    nodes.push(el('div', { class: 'dq-panel dq-row' }, [
      stat(row && svg.isNum(row.drift_loss) ? svg.fmt(row.drift_loss, 1) : '—', 'drift loss'),
      stat(row ? passLabel(row.pass_fail) : '—', 'predicate'),
      stat(row && row.wall_clock_budget_exceeded ? 'timed out' : (row && svg.isNum(row.runtime_ms) ? `${(row.runtime_ms / 1000).toFixed(0)}s` : '—'), 'runtime'),
      stat(genId, 'candidate'),
    ]));

    nodes.push(el('div', { class: 'dq-quicklinks' }, [
      el('a', { class: 'dq-linkbtn', href: ctx.href('board', { epochId, entry: entryId, cmp: genId }), text: 'See this board across every candidate (side-by-side) →' }),
    ]));

    nodes.push(section('Transcript · the run itself', transcriptPanel(conv, runId, turns, anns)));
    return nodes;
  });
}

function transcriptPanel(conv, runId, turns, anns) {
  const card = el('div', { class: 'dq-panel' });
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

  const scroller = el('div', { class: 'dq-transcript' });
  for (const t of turns) {
    const turn = el('div', { class: 'dq-turn dq-turn-' + (t.role || 'agent') }, [
      el('div', { class: 'dq-turn-head dq-faint dq-mono' }, [
        el('span', { text: t.agent || t.role || 'turn' }),
        t.kind ? el('span', { text: ' · ' + t.kind }) : null,
      ].filter(Boolean)),
      t.text ? el('div', { class: 'dq-turn-text', text: t.text }) : null,
    ].filter(Boolean));
    if (Array.isArray(t.tool_calls)) for (const tc of t.tool_calls) {
      turn.appendChild(el('div', { class: 'dq-tool dq-mono', text: '⚙ ' + (tc.name || tc.tool || 'tool') }));
    }
    for (const a of (annBySeq.get(t.seq) || [])) {
      turn.appendChild(el('div', { class: 'dq-annot dq-annot-' + (a.kind || 'note'), text: '◂ ' + (a.summary || a.kind) }));
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

// variants/K/views/run.js — RUN DETAIL: the transcript (the deepest drill).

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, loading, stat } from '../ui.js';

export async function render(host, ctx, params) {
  const genId = params && params.gen;
  const entryId = params && params.entry;
  if (!host.firstChild) host.appendChild(loading('Loading run…'));

  if (!genId || !entryId) {
    gatedSwap(host, 'no-run', () => [
      el('h1', { class: 'vk-h1', text: 'Run' }),
      empty('No run selected — open a run from a candidate’s per-board scoring or a match-up duel.'),
    ]);
    return;
  }

  // Cold-deep-link hydration: epoch → run_id → conversation, from the URL
  // alone, with no prior navigation.
  const ep = await D.epoch();
  const epochId = (ep && ep.epoch_id) || (state.epochDef && state.epochDef.epoch_id) || null;
  let row = null; let runId = null; let conv = null;
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
    nodes.push(el('div', { class: 'vk-pagehead' }, [
      el('h1', { class: 'vk-h1', text: `Run · ${entryId}` }),
      el('p', { class: 'vk-lede' }, [
        `The reconstructed conversation for ${genId} on this board entry`,
        runId ? el('span', { class: 'vk-faint vk-mono', text: '  ' + runId.slice(0, 12) + '…' }) : null,
      ].filter(Boolean)),
    ]));
    nodes.push(el('div', { class: 'vk-panel vk-row' }, [
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
  const card = el('div', { class: 'vk-panel' });
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
  const scroller = el('div', { class: 'vk-transcript' });
  for (const t of turns) {
    const turn = el('div', { class: 'vk-turn vk-turn-' + (t.role || 'agent') }, [
      el('div', { class: 'vk-turn-head vk-faint vk-mono' }, [
        el('span', { text: t.agent || t.role || 'turn' }),
        t.kind ? el('span', { text: ' · ' + t.kind }) : null,
      ].filter(Boolean)),
      t.text ? el('div', { class: 'vk-turn-text', text: t.text }) : null,
    ].filter(Boolean));
    if (Array.isArray(t.tool_calls)) for (const tc of t.tool_calls) {
      turn.appendChild(el('div', { class: 'vk-tool vk-mono', text: '⚙ ' + (tc.name || tc.tool || 'tool') }));
    }
    for (const a of (annBySeq.get(t.seq) || [])) {
      turn.appendChild(el('div', { class: 'vk-annot vk-annot-' + (a.kind || 'note'), text: '◂ ' + (a.summary || a.kind) }));
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

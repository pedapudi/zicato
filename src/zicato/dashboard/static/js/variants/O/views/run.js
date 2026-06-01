// variants/O/views/run.js — RUN DETAIL: the reconstructed transcript.
//
// The deepest drill. Cold-deep-link hydration: epoch → run_id (from the
// generation's per-entry) → conversation, from the URL alone, with no
// prior navigation. The transcript lives in a constrained-scroll container
// (max-height + overflow-y:auto), so lines never overlap. A clearly-themed
// "back to board" link (the E bug: an unstyled anchor) routes to the
// per-board cross-candidate view for fidelity.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, loading, stat, linkButton } from '../ui.js';
import { loadEpochId } from '../model.js';

export async function render(host, ctx, route) {
  const genId = route.gen;
  const entryId = route.entry;
  if (!host.firstChild) host.appendChild(loading('Loading run…'));
  if (!genId || !entryId) {
    gatedSwap(host, 'no-run', () => [empty('No run selected — open a run from a board entry or a match-up duel.')]);
    return;
  }

  const epochId = await loadEpochId();
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
    const out = [];
    out.push(el('div', { class: 'vo-pagehead' }, [
      el('div', { class: 'vo-pagehead-row' }, [
        el('h1', { class: 'vo-h1', text: 'Run · ' + entryId }),
        linkButton('← back to board', '#', () => ctx.navigate('board', { entry: entryId })),
      ]),
      el('p', { class: 'vo-lede' }, [
        `The reconstructed conversation for ${genId} on this board entry`,
        runId ? el('span', { class: 'vo-faint vo-mono', text: '  ' + String(runId).slice(0, 12) + '…' }) : null,
      ].filter(Boolean)),
    ]));
    out.push(el('div', { class: 'vo-glance' }, [
      stat(row && svg.isNum(row.drift_loss) ? svg.fmt(row.drift_loss, 1) : '—', 'drift loss'),
      stat(row ? passLabel(row.pass_fail) : '—', 'predicate'),
      stat(row && row.wall_clock_budget_exceeded ? 'timed out' : (row && svg.isNum(row.runtime_ms) ? `${(row.runtime_ms / 1000).toFixed(0)}s` : '—'), 'runtime'),
      stat(genId, 'candidate'),
    ]));
    out.push(section('Transcript', transcriptPanel(conv, runId, turns, anns)));
    return out;
  });
}

function transcriptPanel(conv, runId, turns, anns) {
  const card = el('div', { class: 'vo-panel' });
  if (!runId) { card.appendChild(empty('No run id for this entry — the transcript is unavailable.')); return card; }
  if (conv && conv.error) { card.appendChild(empty(conv.error)); return card; }
  if (!conv) { card.appendChild(empty('Transcript unavailable (the conversation could not be reconstructed).')); return card; }
  if (!turns.length) { card.appendChild(empty('No turns reconstructed (the run may have produced no conversation).')); return card; }

  const annBySeq = new Map();
  for (const a of anns) { const k = a.anchor_seq; if (!annBySeq.has(k)) annBySeq.set(k, []); annBySeq.get(k).push(a); }
  const scroller = el('div', { class: 'vo-transcript' });
  for (const t of turns) {
    const turn = el('div', { class: 'vo-turn vo-turn-' + (t.role || 'agent') }, [
      el('div', { class: 'vo-turn-head vo-faint vo-mono' }, [
        el('span', { text: t.agent || t.role || 'turn' }),
        t.kind ? el('span', { text: ' · ' + t.kind }) : null,
      ].filter(Boolean)),
      t.text ? el('div', { class: 'vo-turn-text', text: t.text }) : null,
    ].filter(Boolean));
    if (Array.isArray(t.tool_calls)) for (const tc of t.tool_calls) {
      turn.appendChild(el('div', { class: 'vo-tool vo-mono', text: '⚙ ' + (tc.name || tc.tool || 'tool') }));
    }
    for (const a of (annBySeq.get(t.seq) || [])) {
      turn.appendChild(el('div', { class: 'vo-annot vo-annot-' + (a.kind || 'note'), text: '◂ ' + (a.summary || a.kind) }));
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

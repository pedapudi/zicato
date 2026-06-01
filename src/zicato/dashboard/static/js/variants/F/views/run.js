// variants/F/views/run.js — Run detail: the transcript as a readable narrative.
//
// Variant F treats the run as a story you read top to bottom: a small
// status header, then the reconstructed conversation — turns, tool calls,
// and inline drift annotations — in normal block flow inside a constrained
// scroll container (never overlapping absolute rows).
//
// COLD DEEP-LINK: a direct load of #/F/run/<run_id> has no live state, so
// this view FETCHES its own transcript from /api/conversation/{run_id} and
// renders loading → content. It never shows an empty screen on a cold link.
//
// Render discipline: digest-gated. A heartbeat-only re-render (identical
// run id + identical transcript-loaded state) returns early without
// rebuilding DOM. The transcript is cached per run id and only refetched
// when the run id changes.

import { el, clearChildren } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { fmtDuration } from '../../../core/format.js';
import { harmonografLink } from '../../../core/harmonograf.js';

const _convCache = new Map();   // runId -> conversation payload (or {error})
const _convLoading = new Set();
let _lastDigest = null;

export function resetRunCaches() {
  _convCache.clear();
  _convLoading.clear();
  _lastDigest = null;
}

export function renderRun(ctx) {
  const { stage, state, params, repaint } = ctx;
  const runId = params.runId || null;

  const runs = Array.isArray(state.activeRuns) ? state.activeRuns : [];
  const liveRun = runId ? runs.find((r) => (r.run_id || r.id) === runId) : (runs[0] || null);
  const resolvedId = runId || (liveRun ? (liveRun.run_id || liveRun.id) : null);

  // Cold deep-link hydration: fetch the transcript for the route's run id.
  if (resolvedId) ensureConversation(resolvedId, repaint);
  const conv = resolvedId ? _convCache.get(resolvedId) : null;
  const convState = !resolvedId ? 'none'
    : (conv === undefined ? (_convLoading.has(resolvedId) ? 'loading' : 'idle')
      : (conv && conv.error ? 'error' : 'loaded'));

  // Digest gate: structural facts only (no timestamps). A heartbeat tick
  // that does not change the run id / transcript state is a no-op.
  const turnCount = (conv && Array.isArray(conv.turns)) ? conv.turns.length : -1;
  const digest = JSON.stringify({
    runId: resolvedId,
    convState,
    turnCount,
    runCount: runs.length,
    phase: liveRun ? (liveRun.phase || liveRun.gen || '') : '',
  });
  if (digest === _lastDigest && stage.firstChild) return;
  _lastDigest = digest;

  clearChildren(stage);

  stage.appendChild(el('div', { class: 'cz-screen-head' }, [
    el('div', { class: 'cz-epoch-eyebrow' }, ['RUN', el('span', { class: 'cz-mono' }, [resolvedId || '—'])]),
    el('h1', { class: 'cz-screen-title' }, ['The run, as a narrative']),
    el('p', { class: 'cz-screen-sub' }, [
      'One goldfive trace inside a matchup, read top to bottom — each turn, the '
      + 'tools it reached for, and the drift the judges annotated as it happened.',
    ]),
  ]));

  // Live status header (when we have it).
  if (liveRun) stage.appendChild(buildStatus(liveRun));

  // The active-runs strip — so the screen is reachable without a run id.
  if (runs.length) {
    stage.appendChild(el('h2', { class: 'cz-section-title' }, ['Active runs']));
    const list = el('div', { class: 'cz-board-cluster' });
    for (const r of runs) {
      const rid = r.run_id || r.id || '?';
      const prog = typeof r.progress === 'number' ? Math.round(r.progress * 100) + '%' : '—';
      list.appendChild(el('a', { class: 'cz-board-node', 'data-cz': 'run-pick', href: '#/F/run/' + encodeURIComponent(rid) }, [
        el('div', { class: 'cz-board-node-head' }, [
          el('span', { class: 'cz-board-id cz-mono' }, [rid]),
          el('span', { class: 'cz-board-weight' }, [prog]),
        ]),
        el('div', { class: 'cz-board-kind' }, [(r.phase || r.gen || '—').toString()]),
      ]));
    }
    stage.appendChild(list);
  }

  // The transcript narrative.
  stage.appendChild(el('h2', { class: 'cz-section-title' }, ['Transcript']));
  if (convState === 'none') {
    stage.appendChild(el('div', { class: 'cz-empty' }, [
      'No run selected. Pick an active run above, or open a run from a board entry on the scoring screen.',
    ]));
  } else if (convState === 'loading' || convState === 'idle') {
    stage.appendChild(el('div', { class: 'cz-empty', 'data-cz': 'transcript-loading' }, ['Reconstructing the conversation…']));
  } else if (convState === 'error') {
    stage.appendChild(el('div', { class: 'cz-empty' }, [
      (conv && conv.error) ? String(conv.error) : 'Transcript unavailable for this run.',
    ]));
  } else {
    stage.appendChild(buildTranscript(conv));
  }
}

function buildStatus(run) {
  const tile = (label, value) => el('div', { class: 'cz-tile' }, [
    el('div', { class: 'cz-tile-label' }, [label]),
    el('div', { class: 'cz-tile-value cz-mono' }, [value == null ? '—' : String(value)]),
  ]);
  const elapsed = typeof run.elapsed_seconds === 'number' ? fmtDuration(run.elapsed_seconds) : '—';
  const budget = typeof run.budget_seconds === 'number' ? fmtDuration(run.budget_seconds) : '—';
  return el('div', { class: 'cz-run-status' }, [
    el('div', { class: 'cz-tile-strip' }, [
      tile('phase', run.phase),
      tile('generation', run.generation_id || run.gen),
      tile('entry', run.entry_id),
      tile('elapsed', elapsed),
      tile('budget', budget),
    ]),
    harmonografLink(run, 'Open in harmonograf →'),
  ]);
}

// The transcript in normal block flow inside a height-constrained, scroll
// container — no overlapping absolute rows (render-discipline rule 7).
function buildTranscript(conv) {
  const turns = conv && (Array.isArray(conv.turns) ? conv.turns
    : (Array.isArray(conv.messages) ? conv.messages : (Array.isArray(conv) ? conv : [])));
  const anns = (conv && Array.isArray(conv.annotations)) ? conv.annotations : [];
  const annBySeq = new Map();
  for (const a of anns) {
    const k = a.anchor_seq;
    if (!annBySeq.has(k)) annBySeq.set(k, []);
    annBySeq.get(k).push(a);
  }

  const scroll = el('div', { class: 'czF-transcript', role: 'log', 'aria-label': 'Run transcript' });
  if (!turns || !turns.length) {
    scroll.appendChild(el('div', { class: 'cz-empty cz-empty-inline' }, [
      'No turns reconstructed — the run may have produced no conversation.',
    ]));
    return scroll;
  }
  turns.forEach((t) => {
    const role = (t.role || t.speaker || t.kind || 'turn');
    const text = t.text || t.content || t.summary || (typeof t === 'string' ? t : '');
    const turn = el('div', { class: 'czF-turn czF-turn-' + String(role).toLowerCase().replace(/[^a-z]/g, '') }, [
      el('div', { class: 'czF-turn-head cz-mono' }, [
        el('span', { class: 'czF-turn-role' }, [String(t.agent || role)]),
        t.kind ? el('span', { class: 'czF-turn-kind' }, [' · ' + t.kind]) : null,
      ]),
      text ? el('div', { class: 'czF-turn-body' }, [String(text)]) : null,
    ]);
    if (Array.isArray(t.tool_calls) && t.tool_calls.length) {
      for (const tc of t.tool_calls) {
        turn.appendChild(el('div', { class: 'czF-tool cz-mono' }, ['⚙ ' + (tc.name || tc.tool || 'tool')]));
      }
    }
    for (const a of (annBySeq.get(t.seq) || [])) {
      turn.appendChild(el('div', { class: 'czF-annot czF-annot-' + (a.kind || 'note') }, ['◂ ' + (a.summary || a.kind)]));
    }
    scroll.appendChild(turn);
  });
  return scroll;
}

async function ensureConversation(runId, repaint) {
  if (!runId) return;
  if (_convCache.has(runId) || _convLoading.has(runId)) return;
  _convLoading.add(runId);
  try {
    const d = await fetchJson('/api/conversation/' + encodeURIComponent(runId));
    _convCache.set(runId, d || { turns: [] });
  } catch (err) {
    _convCache.set(runId, { error: 'Transcript unavailable: ' + (err && err.message ? err.message : 'fetch failed') });
  } finally {
    _convLoading.delete(runId);
    if (typeof repaint === 'function') repaint();
  }
}

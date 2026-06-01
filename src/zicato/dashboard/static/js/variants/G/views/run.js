// variants/G/views/run.js — L4 run (working transcript).
//
// A run's status + the reconstructed CONVERSATION transcript (turns,
// tool calls, drift annotations), with the harmonograf handoff.
//
// A BUG #2 FIX (empty transcript on cold deep-link): A's run view never
// rendered the /api/conversation/{run_id} payload, so a cold deep-link
// `#/G/run/{run_id}` showed empty. Here, on ANY entry (cold or warm),
// the view reads its run id from the route params and fetches BOTH
// /api/run/{run_id} (status) AND /api/conversation/{run_id} (transcript)
// — loading → content, never empty. The transcript turns are rendered
// from the conversation payload.

import { el } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { state } from '../../../core/state.js';
import { panel, readouts, empty, loading, bar, fmt } from '../components/ui.js';
import { href } from '../router.js';

const cache = new Map();   // runId -> { run, convo }
const loadingSet = new Set();
let _lastDigest = null;

export function resetRunCache() { cache.clear(); loadingSet.clear(); _lastDigest = null; }

// Cold deep-link hydration: fetch the run status AND the conversation
// transcript from the URL's run id. Both are cached so a repaint does
// not re-fetch; a transient failure degrades to an honest state, never
// a blank.
async function ensure(runId, repaint) {
  if (!runId || cache.has(runId) || loadingSet.has(runId)) return;
  loadingSet.add(runId);
  const out = {};
  try { out.run = await fetchJson('/api/run/' + encodeURIComponent(runId)); } catch { out.run = null; }
  try { out.convo = await fetchJson('/api/conversation/' + encodeURIComponent(runId)); } catch { out.convo = null; }
  cache.set(runId, out);
  loadingSet.delete(runId);
  if (repaint) repaint();
}

export function runDigest(params) {
  const runId = params.runId || null;
  const data = runId ? cache.get(runId) : null;
  const convo = data && data.convo;
  const turns = convo && Array.isArray(convo.turns) ? convo.turns.length : null;
  const run = data && (data.run && (data.run.run || data.run));
  return JSON.stringify({
    runId,
    loaded: data != null,
    phase: run && run.phase,
    turns,
    activeRuns: !runId ? (Array.isArray(state.activeRuns) ? state.activeRuns.map((r) => r.run_id || r.id) : null) : null,
  });
}

function transcript(convo) {
  if (!convo || convo.error) return empty(convo && convo.error ? convo.error : 'Transcript unavailable.');
  const turns = Array.isArray(convo.turns) ? convo.turns : [];
  const anns = Array.isArray(convo.annotations) ? convo.annotations : [];
  if (!turns.length) return empty('No turns reconstructed (the run may have produced no conversation).');
  const annBySeq = new Map();
  for (const a of anns) {
    const k = a.anchor_seq;
    if (!annBySeq.has(k)) annBySeq.set(k, []);
    annBySeq.get(k).push(a);
  }
  const wrap = el('div', { class: 'g-transcript' });
  for (const t of turns) {
    const turn = el('div', { class: 'g-turn g-turn-' + (t.role || 'agent') }, [
      el('div', { class: 'g-turn-head g-mono' }, [
        el('span', null, [t.agent || t.role || 'turn']),
        t.kind ? el('span', null, [' · ' + t.kind]) : null,
      ]),
      t.text ? el('div', { class: 'g-turn-text' }, [t.text]) : null,
    ]);
    if (Array.isArray(t.tool_calls)) {
      for (const tc of t.tool_calls) {
        turn.appendChild(el('div', { class: 'g-turn-tool g-mono' }, ['⚙ ' + (tc.name || tc.tool || 'tool')]));
      }
    }
    for (const a of (annBySeq.get(t.seq) || [])) {
      turn.appendChild(el('div', { class: 'g-turn-annot', dataset: { kind: a.kind || 'note' } }, ['◂ ' + (a.summary || a.kind)]));
    }
    wrap.appendChild(turn);
  }
  return wrap;
}

function harmoUrl(run) {
  const base = (state.heartbeat && state.heartbeat.harmonograf_url) || '';
  const sid = run && (run.adk_session_id || run.session_id);
  if (base && sid) return base.replace(/\/$/, '') + '/#/session/' + encodeURIComponent(sid);
  return base || '#';
}

export function renderRun(root, params, repaint) {
  const runId = params.runId;
  if (runId) ensure(runId, repaint);

  const digest = runDigest(params);
  if (digest === _lastDigest && root.firstChild) return;
  _lastDigest = digest;
  root.textContent = '';

  root.appendChild(el('div', { class: 'g-pagehead' }, [
    el('h1', null, ['Run']),
    el('span', { class: 'g-pagehead-sub g-mono' }, [runId || '—']),
  ]));

  if (!runId) {
    const runs = Array.isArray(state.activeRuns) ? state.activeRuns : [];
    if (!runs.length) { root.appendChild(empty('No run selected and no active runs.')); return; }
    const rows = el('div', { class: 'g-run-picker' });
    for (const r of runs) {
      const rid = r.run_id || r.id;
      const row = el('a', { class: 'g-run-pick', href: rid ? href('run', { runId: rid }) : null }, [
        el('span', { class: 'g-mono' }, [rid || '?']),
        el('span', null, [r.phase || '—']),
        el('span', { class: 'g-run-pick-bar' }, [bar(r.progress, r.progress > 0.85 ? 'caution' : 'live')]),
      ]);
      rows.appendChild(row);
    }
    root.appendChild(panel({ title: 'Active runs', sub: 'pick a run to inspect', accent: 'live', body: rows }));
    return;
  }

  const data = cache.get(runId);
  // Loading → content, never empty (A bug #2 fix).
  if (!data) { root.appendChild(loading('Reading run + reconstructing transcript')); return; }

  const run = (data.run && (data.run.run || data.run)) || {};
  root.appendChild(el('div', { class: 'g-section' }, [
    readouts([
      { label: 'phase', value: run.phase || '—', tone: 'live' },
      { label: 'wall clock', value: fmt(run.elapsed_seconds, 0) + 's', foot: run.budget_seconds ? 'of ' + run.budget_seconds + 's' : '' },
      { label: 'drift count', value: run.drift_count != null ? run.drift_count : '—' },
    ]),
  ]));

  root.appendChild(panel({
    title: 'Transcript',
    sub: 'reconstructed turns · tool calls · drift annotations',
    accent: 'live',
    actions: (state.heartbeat && state.heartbeat.harmonograf_url)
      ? el('a', { class: 'g-btn', href: harmoUrl(run), target: '_blank', rel: 'noopener' }, ['open in harmonograf →'])
      : null,
    body: transcript(data.convo),
  }));
}

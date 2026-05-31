// variants/B/views/run.js — the Run view (the transcript).
//
// The conversation as evidence, beautifully typeset: an article of turns,
// each role set in its own voice, tool calls folded into quiet aside blocks,
// and the framework annotations (drift / steering / judge verdicts) hung in
// the margin near the turn they explain. Honest zero-turn / aborted
// fallbacks. Reached from an experiment's entry rows.
//
// Data: /api/run/{epoch}/{gen}/{entry}/transcript (preferred, when a
// generation is in the route), else /api/conversation/{run_id}.

import { el, clearChildren } from '../../../core/dom.js';
import { bRouter } from '../router.js';
import { registerBView } from '../shell.js';
import { makeCache, currentEpochId } from '../lib/data.js';
import { section, note } from '../lib/prose.js';
import { fmtClock } from '../../../core/format.js';

let _cache = null;
function repaint() {
  const host = document.getElementById('vb-page');
  if (host && bRouter.current().view === 'run') renderRun(host, bRouter.current());
}
function caches() { if (!_cache) _cache = makeCache(repaint); return _cache; }
export function resetRunView() { _cache = null; }

const ROLE_VOICE = {
  user: { label: 'user', cls: 'user' },
  agent: { label: 'agent', cls: 'agent' },
  assistant: { label: 'agent', cls: 'agent' },
  system: { label: 'system', cls: 'system' },
  tool: { label: 'tool', cls: 'tool' },
};

function turnNode(turn, annotationsBySeq) {
  const voice = ROLE_VOICE[turn.role] || ROLE_VOICE.agent;
  const kids = [
    el('div', { class: 'vb-turn-meta' }, [
      el('span', { class: `vb-turn-role vb-turn-role-${voice.cls}` }, [
        turn.agent && turn.agent !== turn.role ? `${voice.label} · ${turn.agent}` : voice.label,
      ]),
      turn.ts ? el('span', { class: 'vb-turn-ts vb-mono' }, [fmtClock(turn.ts)]) : null,
    ].filter(Boolean)),
  ];
  if (turn.text && turn.text.trim()) {
    kids.push(el('div', { class: 'vb-turn-text' },
      turn.text.split(/\n{2,}/).map((para) => el('p', null, [para]))));
  }
  const calls = Array.isArray(turn.tool_calls) ? turn.tool_calls : [];
  const results = Array.isArray(turn.tool_results) ? turn.tool_results : [];
  if (calls.length || results.length) {
    kids.push(el('div', { class: 'vb-turn-tools' }, [
      ...calls.map((tc) => el('div', { class: 'vb-tool vb-tool-call' }, [
        el('span', { class: 'vb-tool-tag' }, ['call']),
        el('span', { class: 'vb-mono vb-tool-name' }, [String(tc.name || tc.tool || tc.function || 'tool')]),
      ])),
      ...results.map((tr) => el('div', { class: 'vb-tool vb-tool-result' }, [
        el('span', { class: 'vb-tool-tag' }, ['result']),
        el('span', { class: 'vb-mono vb-tool-name' }, [String(tr.name || tr.tool || 'result')]),
      ])),
    ]));
  }
  // Margin annotations anchored to this turn.
  const anns = (turn.seq != null && annotationsBySeq.get(turn.seq)) || [];
  const aside = anns.length
    ? el('aside', { class: 'vb-turn-aside' }, anns.map((a) => el('div', {
        class: `vb-anno vb-anno-${String(a.kind || '').replace(/[^\w-]/g, '')}`,
      }, [
        el('span', { class: 'vb-anno-kind' }, [String(a.kind || 'note')]),
        el('span', { class: 'vb-anno-summary' }, [String(a.summary || '')]),
      ])))
    : null;

  return el('div', { class: `vb-turn vb-turn-${voice.cls}` }, [
    el('div', { class: 'vb-turn-main' }, kids),
    aside,
  ].filter(Boolean));
}

export function renderRun(host, route) {
  if (!host) return;
  const p = (route && route.params) || {};
  const entryId = p.entryId;
  const genId = p.generationId;
  clearChildren(host);

  if (!entryId) {
    host.appendChild(el('h1', { class: 'vb-page-title' }, ['Run']));
    host.appendChild(note('empty', { label: 'No run selected', detail: 'Open one from an experiment.' }));
    return;
  }

  const c = caches();
  const epochId = currentEpochId();
  const key = `${entryId}|${genId || ''}`;
  const path = genId && epochId
    ? '/api/run/' + [epochId, genId, entryId].map(encodeURIComponent).join('/') + '/transcript'
    : '/api/conversation/' + encodeURIComponent(entryId);
  c.ensure(key, path, { turns: [], annotations: [], __broken: true });
  const data = c.get(key);

  host.appendChild(el('div', { class: 'vb-run-lead' }, [
    el('p', { class: 'vb-eyebrow' }, [
      'Run · ', el('span', { class: 'vb-mono' }, [String(entryId)]),
      genId ? el('span', { class: 'vb-muted' }, [' · generation ', el('span', { class: 'vb-mono' }, [String(genId)])]) : null,
    ].filter(Boolean)),
    el('h1', { class: 'vb-page-title' }, ['What actually happened']),
  ]));

  if (data === undefined) {
    host.appendChild(note('running', { label: 'Reconstructing transcript' }));
    return;
  }
  if (!data || data.__broken || data.error) {
    host.appendChild(note('broken', { reason: (data && data.error) || 'transcript unavailable' }));
    return;
  }
  const turns = Array.isArray(data.turns) ? data.turns : [];
  if (!turns.length) {
    host.appendChild(note('empty', {
      label: 'No conversation turns',
      detail: data.complete === false ? 'The run is still in flight or produced no turns yet.'
        : 'This run produced no recorded conversation (a zero-turn or aborted run).',
    }));
    return;
  }

  const annotationsBySeq = new Map();
  for (const a of (Array.isArray(data.annotations) ? data.annotations : [])) {
    if (a && a.anchor_seq != null) {
      if (!annotationsBySeq.has(a.anchor_seq)) annotationsBySeq.set(a.anchor_seq, []);
      annotationsBySeq.get(a.anchor_seq).push(a);
    }
  }

  // Multi-turn boundaries: emit a divider when run_index changes.
  const body = el('div', { class: 'vb-transcript' });
  let lastRunIndex = null;
  for (const t of turns) {
    if (t.run_index != null && t.run_index !== lastRunIndex && lastRunIndex !== null) {
      body.appendChild(el('div', { class: 'vb-transcript-divider' }, [`turn ${t.run_index}`]));
    }
    lastRunIndex = t.run_index;
    body.appendChild(turnNode(t, annotationsBySeq));
  }

  host.appendChild(section('Transcript', [body], {
    sub: `${turns.length} turn${turns.length === 1 ? '' : 's'}`
      + (data.complete === false ? ' · still in flight' : '') + '. Annotations are framework steering, hung in the margin.',
  }));
}

registerBView('run', renderRun);

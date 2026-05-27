// views/phase0_run.js — L4 (run-level) view.
//
// Renders the per-run shell: header metrics, expectation outcomes,
// per-judge breakdown (stub until #179 lands), transcript (single-run
// layout for phase 0; side-by-side toggle is phase 2), and a live
// events stream. Live transcript rendering belongs ONLY at L4 per the
// design agreement — every other level shows summary data.

import { $, el, clearChildren } from '../core/dom.js';
import { state } from '../core/state.js';

function findActiveRun(entryId) {
  if (!entryId) return null;
  const runs = Array.isArray(state.activeRuns) ? state.activeRuns : [];
  for (const r of runs) {
    if (r && r.entry_id === entryId) return r;
  }
  return null;
}

function renderRunHeaderMetrics(params, run) {
  const node = $('phase0-run-header');
  if (!node) return;
  clearChildren(node);
  if (!params || (!params.entryId && !params.generationId)) {
    node.appendChild(el('p', { class: 'empty' }, ['No run selected.']));
    return;
  }
  const wrap = el('div', { class: 'phase0-run-header-inner' });
  if (params.epochId) {
    wrap.appendChild(el('div', { class: 'mono' },
      ['epoch · ', params.epochId]));
  }
  if (params.generationId) {
    wrap.appendChild(el('div', { class: 'mono' },
      ['gen · ', params.generationId]));
  }
  if (params.entryId) {
    wrap.appendChild(el('div', { class: 'mono' },
      ['entry · ', params.entryId]));
  }
  if (run) {
    if (typeof run.progress === 'number') {
      wrap.appendChild(el('div', { class: 'mono' },
        ['progress · ', String(Math.round((run.progress || 0) * 100)), '%']));
    }
    if (typeof run.elapsed_seconds === 'number') {
      wrap.appendChild(el('div', { class: 'mono' },
        ['elapsed · ', String(Math.round(run.elapsed_seconds)), 's']));
    }
    if (run.status) {
      wrap.appendChild(el('div', { class: 'mono' },
        ['status · ', String(run.status)]));
    }
  } else {
    wrap.appendChild(el('p', { class: 'panel-subheader' },
      ['Run is not currently active — historical metrics land once L4 fetches them.']));
  }
  node.appendChild(wrap);
}

function renderExpectation() {
  const node = $('phase0-run-expectation');
  if (!node) return;
  clearChildren(node);
  // The expectation outcome is sourced from the per-run loss.json,
  // which the matchup-conversations endpoint already projects. Phase 0
  // surfaces the slot; the wire-up lands once the L4 fetch path
  // migrates from the legacy conversation view.
  node.appendChild(el('p', { class: 'panel-subheader' },
    ['Expectation outcomes land once the L4 fetch path migrates.']));
}

function renderJudges() {
  const node = $('phase0-run-judges');
  if (!node) return;
  clearChildren(node);
  node.appendChild(el('p', { class: 'empty phase0-stub-msg' },
    ['(per-judge breakdown — populated once #179 lands)']));
}

function renderTranscript() {
  const node = $('phase0-run-transcript');
  if (!node) return;
  clearChildren(node);
  node.appendChild(el('p', { class: 'panel-subheader' },
    ['Transcript renders here (single-run layout for phase 0; '
      + 'side-by-side compare toggles in phase 2).']));
}

function renderEvents() {
  const node = $('phase0-run-events');
  if (!node) return;
  clearChildren(node);
  // Reuse the same run-log tail the legacy log panel reads from. The
  // live transcript only belongs at L4 per the design agreement;
  // every other level shows summary data.
  const events = (state.logTail && Array.isArray(state.logTail.events))
    ? state.logTail.events.slice(-12) : [];
  if (events.length === 0) {
    node.appendChild(el('p', { class: 'empty' }, ['No events yet.']));
    return;
  }
  const list = el('div', { class: 'phase0-events-list mono' });
  for (const ev of events) {
    const line = el('div', { class: 'phase0-events-line' }, [
      ev.kind || '—',
      ' · ',
      ev.summary || '',
    ]);
    list.appendChild(line);
  }
  node.appendChild(list);
}

export function renderPhase0Run(params) {
  const run = findActiveRun(params && params.entryId);
  renderRunHeaderMetrics(params, run);
  renderExpectation();
  renderJudges();
  renderTranscript();
  renderEvents();
}

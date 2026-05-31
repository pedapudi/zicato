// variants/C/views/run.js — Screen 5a: one run (basic, reachable from nav).
//
// A run is a single goldfive trace inside a matchup. Variant C keeps the
// competition view diagram-first, so the run screen is intentionally
// light: live status tiles, the rolling event tail, and the
// "open in harmonograf" handoff (harmonograf owns the execution view).
//
// Data: state.activeRuns (live status), state.logTail (event tail).

import { el, clearChildren, reconcileList } from '../../../core/dom.js';
import { fmtClock, fmtDuration } from '../../../core/format.js';
import { harmonografLink } from '../../../core/harmonograf.js';

export function renderRun(ctx) {
  const { stage, state, params } = ctx;
  const runId = params.runId || null;
  clearChildren(stage);

  const runs = Array.isArray(state.activeRuns) ? state.activeRuns : [];
  const run = runId ? runs.find((r) => (r.run_id || r.id) === runId) : (runs[0] || null);

  stage.appendChild(el('div', { class: 'cz-screen-head' }, [
    el('div', { class: 'cz-epoch-eyebrow' }, ['RUN', el('span', { class: 'cz-mono' }, [run ? (run.run_id || run.id) : (runId || '—')])]),
    el('h1', { class: 'cz-screen-title' }, ['Run']),
    el('p', { class: 'cz-screen-sub' }, [
      'A single goldfive trace inside a matchup. Step across into harmonograf for the full execution view.',
    ]),
  ]));

  if (!run && runs.length === 0) {
    stage.appendChild(el('div', { class: 'cz-empty' }, ['No active runs. Runs appear here while a tournament is in flight.']));
  } else if (run) {
    stage.appendChild(buildStatus(run));
  } else {
    stage.appendChild(el('div', { class: 'cz-empty' }, [`Run "${runId}" is not currently active.`]));
  }

  // Active-runs picker — every in-flight run, so the screen is reachable
  // even without a run id in the route.
  if (runs.length) {
    stage.appendChild(el('h2', { class: 'cz-section-title' }, ['Active runs']));
    const list = el('div', { class: 'cz-board-cluster' });
    for (const r of runs) {
      const rid = r.run_id || r.id || '?';
      const prog = typeof r.progress === 'number' ? Math.round(r.progress * 100) + '%' : '—';
      list.appendChild(el('a', { class: 'cz-board-node', href: '#/C/run/' + encodeURIComponent(rid) }, [
        el('div', { class: 'cz-board-node-head' }, [
          el('span', { class: 'cz-board-id cz-mono' }, [rid]),
          el('span', { class: 'cz-board-weight' }, [prog]),
        ]),
        el('div', { class: 'cz-board-kind' }, [(r.phase || r.gen || '—').toString()]),
      ]));
    }
    stage.appendChild(list);
  }

  stage.appendChild(buildEventTail(state));
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

function buildEventTail(state) {
  const wrap = el('div', { class: 'cz-events' });
  wrap.appendChild(el('h2', { class: 'cz-section-title' }, ['Event tail']));
  const host = el('div', { class: 'cz-event-list', role: 'log', 'aria-label': 'Live events' });
  const events = (state.logTail && Array.isArray(state.logTail.events)) ? state.logTail.events : [];
  if (events.length === 0) {
    host.appendChild(el('p', { class: 'cz-empty cz-empty-inline' }, ['No events yet.']));
  } else {
    const recent = events.slice(-80);
    reconcileList(host, recent,
      (e) => (e.seq != null ? 's' + e.seq : 'k' + (e.ts || '') + (e.kind || '')),
      (e) => el('div', { class: 'cz-event-row' }, [
        el('span', { class: 'cz-event-ts cz-mono' }, [fmtClock(e.ts)]),
        el('span', { class: 'cz-event-kind' }, [String(e.kind || '')]),
        el('span', { class: 'cz-event-sum' }, [String(e.summary || '')]),
      ]),
      (row, e) => {
        const sum = row.childNodes[2];
        if (sum) sum.textContent = String(e.summary || '');
      });
  }
  wrap.appendChild(host);
  return wrap;
}

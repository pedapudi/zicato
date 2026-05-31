// variants/D/views/run.js — a single run, reachable from nav.
//
// Basic run view: the active runs the environment read carries, each as
// a quiet progress line, plus the run-log tail. Reads from the shared
// AppState (populated by core/api + core/sse) so it stays live under
// SSE without its own fetch. Honest empty state when nothing is running.

import { el, clearChildren } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as svg from '../svg.js';
import { section, crumb, empty } from '../ui.js';
import { fmtDuration } from '../../../core/format.js';

export function render(host, ctx, params) {
  clearChildren(host);
  host.appendChild(crumb([{ label: 'environment', view: 'environment' }, { label: 'run' }]));
  host.appendChild(el('h1', { class: 'd-h1', text: 'Runs' }));

  const runs = Array.isArray(state.activeRuns) ? state.activeRuns : [];
  const runsCard = el('div', { class: 'd-panel' });
  if (runs.length) {
    for (const r of runs) {
      const prog = svg.isNum(r.progress) ? Math.max(0, Math.min(1, r.progress)) : null;
      const bar = el('div', { class: 'd-gate-track', style: 'max-width:240px;' });
      if (prog != null) {
        bar.appendChild(el('div', { class: 'd-gate-margin', style: `left:0;width:${(prog * 100).toFixed(0)}%;background:var(--v2-good-soft);` }));
      }
      runsCard.appendChild(el('div', { class: 'd-gate', style: 'border-top:1px solid var(--v2-rule-soft);' }, [
        el('div', { class: 'd-mono', style: 'min-width:160px;', text: r.entry_id || r.run_id || r.id || 'run' }),
        bar,
        el('div', { class: 'd-faint d-mono', text: svg.isNum(r.elapsed_seconds) ? fmtDuration(r.elapsed_seconds) : '—' }),
      ]));
    }
  } else {
    runsCard.appendChild(empty('No runs in flight.'));
  }
  host.appendChild(section('In flight', runsCard));

  // ---- log tail ----
  const tail = (state.logTail && Array.isArray(state.logTail.events)) ? state.logTail.events : [];
  const logCard = el('div', { class: 'd-panel', style: 'max-height:320px;overflow-y:auto;' });
  if (tail.length) {
    for (const ev of tail.slice(-60)) {
      const line = typeof ev === 'string' ? ev
        : (ev.detail || ev.message || ev.kind || JSON.stringify(ev));
      logCard.appendChild(el('div', { class: 'd-mono', style: 'font-size:11px;padding:1px 0;color:var(--v2-ink-soft);', text: String(line) }));
    }
  } else {
    logCard.appendChild(empty('No events yet.'));
  }
  host.appendChild(section('Activity', logCard));
}

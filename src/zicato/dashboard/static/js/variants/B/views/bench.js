// variants/B/views/bench.js — the Bench (live operations), editorial-style.
//
// The Bench answers "what is happening right now?" — kept in the same airy
// voice as the rest of the notebook rather than a dense ops grid. The
// challenger's hypothesis is pinned as a pull-quote, the board entries
// stream as a quiet roster (champion | challenger, with a progress ring per
// running entry), and a recent-activity strip tails the run log. Honest
// when idle: a calm "no run in flight" with a route back to the Environment.
//
// Data: state.activeTournament (entries + sides + statuses), state.logTail.

import { el, clearChildren } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import { bRouter } from '../router.js';
import { registerBView } from '../shell.js';
import { experimentFor, hypothesisText, hypothesisPrediction, liveChallengerId } from '../lib/data.js';
import { section, note, pullQuote, verdictBadge } from '../lib/prose.js';
import { progressRing, fmtNum, fin } from '../lib/charts.js';
import { fmtClock } from '../../../core/format.js';

function progressFor(entryId) {
  const runs = Array.isArray(state.activeRuns) ? state.activeRuns : [];
  for (const r of runs) {
    if (r && (r.entry_id === entryId || r.run_id === entryId)) {
      if (fin(r.progress)) return r.progress;
    }
  }
  return null;
}

function sideCell(entry) {
  if (!entry) return el('span', { class: 'vb-bench-cell vb-bench-empty' }, ['—']);
  const status = entry.status || 'queued';
  if (status === 'done' || status === 'cached') {
    return el('span', { class: 'vb-bench-cell vb-improve' }, [
      el('span', { class: 'vb-bench-status' }, [status === 'cached' ? 'cached' : 'done']),
      fin(entry.drift_loss) ? el('span', { class: 'vb-mono vb-bench-loss' }, [fmtNum(entry.drift_loss, 1)]) : null,
    ].filter(Boolean));
  }
  if (status === 'running') {
    const frac = progressFor(entry.entry_id);
    return el('span', { class: 'vb-bench-cell vb-running' }, [
      progressRing(frac == null ? 0.4 : frac, { size: 22 }),
      el('span', { class: 'vb-bench-status' }, [frac != null ? `${Math.round(frac * 100)}%` : 'running']),
    ]);
  }
  if (status === 'aborted' || status === 'error') {
    return el('span', { class: 'vb-bench-cell vb-regress' }, [el('span', { class: 'vb-bench-status' }, [status])]);
  }
  return el('span', { class: 'vb-bench-cell vb-neutral' }, [el('span', { class: 'vb-bench-status' }, ['queued'])]);
}

export function renderBench(host, _route) {
  if (!host) return;
  clearChildren(host);
  const at = state.activeTournament;

  host.appendChild(el('div', { class: 'vb-bench-lead' }, [
    el('p', { class: 'vb-eyebrow' }, [
      at ? el('span', { class: 'vb-live-dot vb-live-inline', 'aria-hidden': 'true' }) : null,
      at ? 'Live' : 'The Bench',
    ].filter(Boolean)),
    el('h1', { class: 'vb-page-title' }, [at ? 'A run is in flight' : 'Nothing is running']),
  ]));

  if (!at) {
    host.appendChild(note('empty', {
      label: 'No tournament in flight',
      detail: 'When an evolve run starts, the live board and the challenger hypothesis appear here.',
    }));
    host.appendChild(el('p', { class: 'vb-bench-back' }, [
      el('a', {
        class: 'vb-link-arrow', href: '#/B/environment',
        onclick: (ev) => { if (ev && ev.preventDefault) ev.preventDefault(); bRouter.go('environment'); },
      }, ['← Back to the Environment']),
    ]));
    return;
  }

  const champ = at.parent_generation_id || at.champion_id || at.champion;
  const chall = at.child_generation_id || at.challenger_id || at.challenger || liveChallengerId();
  const exp = chall ? experimentFor(String(chall)) : null;
  const hyp = exp ? hypothesisText(exp) : '';
  const prediction = exp ? hypothesisPrediction(exp) : '';

  // The pinned hypothesis.
  host.appendChild(section('Testing', [
    hyp ? pullQuote(hyp, { class: 'vb-bench-hyp', attribution: `challenger ${chall}` })
      : el('p', { class: 'vb-muted' }, ['The challenger has not recorded a hypothesis yet.']),
    prediction ? el('p', { class: 'vb-exp-prediction' }, [el('span', { class: 'vb-tag' }, ['predicted']), ' ', prediction]) : null,
    el('p', { class: 'vb-bench-vs' }, [
      'champion ', el('span', { class: 'vb-mono' }, [String(champ || '—')]),
      ' → challenger ', el('span', { class: 'vb-mono' }, [String(chall || '—')]),
      at.phase ? el('span', { class: 'vb-tag' }, [String(at.phase)]) : null,
    ].filter(Boolean)),
  ].filter(Boolean)));

  // The board roster: group entries by entry_id, champion | challenger.
  const entries = Array.isArray(at.entries) ? at.entries : [];
  const byEntry = new Map();
  for (const e of entries) {
    if (!e || !e.entry_id) continue;
    if (!byEntry.has(e.entry_id)) byEntry.set(e.entry_id, { parent: null, child: null });
    const slot = byEntry.get(e.entry_id);
    if (e.side === 'parent') slot.parent = e; else if (e.side === 'child') slot.child = e;
    else (slot.child ? (slot.parent = e) : (slot.child = e));
  }
  const done = entries.filter((e) => e && (e.status === 'done' || e.status === 'cached')).length;

  const rosterHead = el('div', { class: 'vb-bench-roster-head' }, [
    el('span', { class: 'vb-bench-col-entry' }, ['board entry']),
    el('span', { class: 'vb-bench-col-side' }, ['champion']),
    el('span', { class: 'vb-bench-col-side' }, ['challenger']),
  ]);
  const rosterRows = [...byEntry.entries()].map(([entryId, slot]) => el('div', {
    class: 'vb-bench-roster-row vb-clickable', role: 'button', tabindex: '0',
    'aria-label': `entry ${entryId}`,
    onclick: () => bRouter.go('run', entryId, chall),
    onkeydown: (ev) => { if (ev && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); bRouter.go('run', entryId, chall); } },
  }, [
    el('span', { class: 'vb-bench-col-entry vb-mono' }, [String(entryId)]),
    sideCell(slot.parent),
    sideCell(slot.child),
  ]));

  host.appendChild(section('The board', [
    el('div', { class: 'vb-bench-roster' }, [rosterHead, ...rosterRows]),
  ], { sub: `${done} / ${entries.length} runs complete. Click an entry to open its transcript.` }));

  // Activity tail.
  const events = (state.logTail && Array.isArray(state.logTail.events)) ? state.logTail.events : [];
  const tail = events.slice(-12).reverse();
  host.appendChild(section('Activity', [
    tail.length
      ? el('ol', { class: 'vb-activity' }, tail.map((ev) => el('li', { class: 'vb-activity-row' }, [
          el('span', { class: 'vb-mono vb-activity-ts' }, [fmtClock(ev.ts || ev.emittedAt || ev.time)]),
          el('span', { class: 'vb-activity-msg' }, [
            String(ev.message || ev.summary || ev.kind || ev.event || JSON.stringify(ev)).slice(0, 120),
          ]),
        ])))
      : note('empty', { label: 'No recent activity' }),
  ], { sub: 'The proposer / judge / agent ticker from the run log.' }));
}

registerBView('bench', renderBench);

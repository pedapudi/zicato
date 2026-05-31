// variants/A/views/bench.js — Bench / live ops, lighter.
//
// The "what's happening right now" board: in-flight runs with their
// deadline bars, the live phase, and a rolling event tail. Lighter than
// the competition views but present in the nav.
//
// Data: state.activeRuns, state.logTail, state.heartbeat.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import { panel, readouts, empty, bar, chip } from '../components/instruments.js';

export function renderBench(root, _params, _repaint) {
  root.textContent = '';
  root.appendChild(el('div', { class: 'mcA-pagehead' }, [
    el('h1', null, ['Bench']),
    el('span', { class: 'mcA-pagehead-sub' }, ['live ops — runs in flight right now']),
  ]));

  const hb = state.heartbeat || {};
  const runs = Array.isArray(state.activeRuns) ? state.activeRuns : [];

  root.appendChild(el('div', { style: 'margin-bottom:16px' }, [
    readouts([
      { label: 'phase', value: String(hb.phase || 'idle').toUpperCase(), tone: runs.length ? 'live' : null },
      { label: 'active runs', value: runs.length, tone: runs.length ? 'live' : null },
      { label: 'epoch', value: hb.epoch_id || '—' },
      { label: 'generation', value: hb.generation_id || '—' },
    ]),
  ]));

  // active runs
  let runsBody;
  if (!runs.length) {
    runsBody = empty('No runs in flight.');
  } else {
    const tbl = el('table', { class: 'mcA-table mcA-table-clickable' });
    tbl.appendChild(el('thead', null, [el('tr', null, [
      el('th', null, ['run']), el('th', null, ['entry']), el('th', null, ['phase']),
      el('th', { style: 'width:220px' }, ['deadline (elapsed/budget)']),
    ])]));
    const tb = el('tbody');
    for (const r of runs) {
      const rid = r.run_id || r.id || '?';
      const row = el('tr', null, [
        el('td', { class: 'mono' }, [rid]),
        el('td', { class: 'mono' }, [r.entry_id || '—']),
        el('td', null, [r.phase || '—']),
        el('td', null, [el('div', { style: 'display:flex;align-items:center;gap:8px' }, [
          bar(r.progress, typeof r.progress === 'number' && r.progress > 0.85 ? 'warn' : 'live'),
          el('span', { class: 'mono mcA-readout-foot' }, [typeof r.progress === 'number' ? (r.progress * 100 | 0) + '%' : '—']),
        ])]),
      ]);
      row.addEventListener('click', () => { window.location.hash = '#/A/run/' + encodeURIComponent(rid); });
      tb.appendChild(row);
    }
    tbl.appendChild(tb);
    runsBody = tbl;
  }
  root.appendChild(el('div', { style: 'margin-bottom:16px' }, [
    panel({ title: 'In flight', accent: runs.length ? 'live' : null, body: runsBody }),
  ]));

  // log tail
  const events = (state.logTail && Array.isArray(state.logTail.events)) ? state.logTail.events : [];
  const log = el('div', { class: 'mcA-log' });
  if (events.length) {
    for (const ev of events.slice(-120)) {
      log.appendChild(el('div', { class: 'mcA-log-row' }, [
        el('span', { class: 'mcA-log-ts' }, [String(ev.ts || '').slice(11, 23) || '—']),
        el('span', { class: 'mcA-log-kind' }, [ev.kind || '—']),
        el('span', { class: 'mcA-log-sum' }, [ev.summary || ev.kind || '']),
      ]));
    }
  } else {
    log.appendChild(empty('No events yet.'));
  }
  root.appendChild(panel({ title: 'Event tail', sub: 'rolling goldfive events across active runs', body: log }));
}

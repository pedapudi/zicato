// variants/G/views/bench.js — Bench / live ops with a clean event tail.
//
// What's happening right now: in-flight runs with deadline bars, the
// live phase, and a rolling event tail.
//
// A BUG #4 FIX (overlapping log lines): the event tail is a CONSTRAINED
// SCROLL container — a fixed-height box with `overflow-y:auto` — and the
// rows are laid out in NORMAL block/flex-column flow (siblings stacked,
// never absolutely positioned), so they cannot overlap. The flex chain
// up to it carries `min-height:0` so the scroll box can actually shrink
// and scroll. The container gets the `.g-eventtail` class the test
// asserts on; rows are `.g-event-row` siblings.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import { panel, readouts, empty, bar, chip, fmt } from '../components/ui.js';
import { href } from '../router.js';
import { fmtClock } from '../../../core/format.js';

let _lastDigest = null;

export function resetBenchCache() { _lastDigest = null; }

export function benchDigest() {
  const hb = state.heartbeat || {};
  const runs = Array.isArray(state.activeRuns) ? state.activeRuns : [];
  const events = (state.logTail && Array.isArray(state.logTail.events)) ? state.logTail.events : [];
  return JSON.stringify({
    phase: hb.phase || 'idle',
    epoch: hb.epoch_id,
    gen: hb.generation_id,
    runs: runs.map((r) => [r.run_id || r.id, r.entry_id, r.phase, typeof r.progress === 'number' ? r.progress.toFixed(2) : null]),
    // event identity by (kind, summary) tail — NOT timestamps — so a
    // re-stamped heartbeat does not force a repaint.
    events: events.slice(-120).map((e) => [e.kind, e.summary]),
  });
}

export function renderBench(root, _params, _repaint) {
  const digest = benchDigest();
  if (digest === _lastDigest && root.firstChild) return;
  _lastDigest = digest;
  root.textContent = '';

  root.appendChild(el('div', { class: 'g-pagehead' }, [
    el('h1', null, ['Bench']),
    el('span', { class: 'g-pagehead-sub' }, ['live ops — runs in flight right now']),
  ]));

  const hb = state.heartbeat || {};
  const runs = Array.isArray(state.activeRuns) ? state.activeRuns : [];

  root.appendChild(el('div', { class: 'g-section' }, [
    readouts([
      { label: 'phase', value: String(hb.phase || 'idle').toUpperCase(), tone: runs.length ? 'live' : null },
      { label: 'active runs', value: runs.length, tone: runs.length ? 'live' : null },
      { label: 'epoch', value: hb.epoch_id || '—' },
      { label: 'generation', value: hb.generation_id || '—' },
    ]),
  ]));

  // in-flight runs
  let runsBody;
  if (!runs.length) {
    runsBody = empty('No runs in flight.');
  } else {
    const list = el('div', { class: 'g-inflight' });
    for (const r of runs) {
      const rid = r.run_id || r.id || '?';
      list.appendChild(el('a', { class: 'g-inflight-row', href: href('run', { runId: rid }) }, [
        el('span', { class: 'g-mono g-inflight-id' }, [rid]),
        el('span', { class: 'g-mono g-inflight-entry' }, [r.entry_id || '—']),
        el('span', { class: 'g-inflight-phase' }, [r.phase || '—']),
        el('span', { class: 'g-inflight-bar' }, [
          bar(r.progress, typeof r.progress === 'number' && r.progress > 0.85 ? 'caution' : 'live'),
          el('span', { class: 'g-mono g-readout-foot' }, [typeof r.progress === 'number' ? (r.progress * 100 | 0) + '%' : '—']),
        ]),
      ]));
    }
    runsBody = list;
  }
  root.appendChild(el('div', { class: 'g-section' }, [
    panel({ title: 'In flight', accent: runs.length ? 'live' : null, body: runsBody }),
  ]));

  // event tail — constrained scroll container, rows in normal flow.
  const events = (state.logTail && Array.isArray(state.logTail.events)) ? state.logTail.events : [];
  const tail = el('div', { class: 'g-eventtail' });
  if (events.length) {
    for (const ev of events.slice(-120)) {
      tail.appendChild(el('div', { class: 'g-event-row' }, [
        el('span', { class: 'g-event-ts g-mono' }, [fmtClock(ev.ts || ev.emitted_at) || '—']),
        el('span', { class: 'g-event-kind g-mono' }, [ev.kind || '—']),
        el('span', { class: 'g-event-sum' }, [ev.summary || ev.kind || '']),
      ]));
    }
  } else {
    tail.appendChild(empty('No events yet.'));
  }
  root.appendChild(panel({ title: 'Event tail', sub: 'rolling events across active runs', body: tail }));
  void chip;
}

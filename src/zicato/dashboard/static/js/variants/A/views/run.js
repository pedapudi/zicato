// variants/A/views/run.js — L4 run (transcript), lighter.
//
// A run's live status + a transcript readout, with the harmonograf
// handoff. Reachable from nav; deliberately lighter than the
// competition views.
//
// Data: /api/run/{runId} (status + event tail) or
// /api/conversation/{runId} (transcript).

import { el } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { state } from '../../../core/state.js';
import { panel, readouts, empty, loading, bar } from '../components/instruments.js';

const cache = new Map();
const loadingSet = new Set();

export function resetRunCache() { cache.clear(); loadingSet.clear(); }

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

function fmt(v, d = 1) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(d) : '—'; }

export function renderRun(root, params, repaint) {
  const runId = params.runId;
  root.textContent = '';
  root.appendChild(el('div', { class: 'mcA-pagehead' }, [
    el('h1', null, ['Run']),
    el('span', { class: 'mcA-pagehead-sub mono' }, [runId || '—']),
  ]));
  if (!runId) {
    // surface the active runs as a picker
    const runs = Array.isArray(state.activeRuns) ? state.activeRuns : [];
    if (!runs.length) { root.appendChild(empty('No run selected and no active runs.')); return; }
    const tbl = el('table', { class: 'mcA-table mcA-table-clickable' });
    tbl.appendChild(el('thead', null, [el('tr', null, [
      el('th', null, ['run']), el('th', null, ['phase']), el('th', { style: 'width:200px' }, ['deadline']),
    ])]));
    const tb = el('tbody');
    for (const r of runs) {
      const row = el('tr', null, [
        el('td', { class: 'mono' }, [r.run_id || r.id || '?']),
        el('td', null, [r.phase || '—']),
        el('td', null, [el('div', { style: 'display:flex;align-items:center;gap:8px' }, [
          bar(r.progress, r.progress > 0.85 ? 'warn' : 'live'),
          el('span', { class: 'mono mcA-readout-foot' }, [typeof r.progress === 'number' ? (r.progress * 100 | 0) + '%' : '—']),
        ])]),
      ]);
      const rid = r.run_id || r.id;
      if (rid) row.addEventListener('click', () => { window.location.hash = '#/A/run/' + encodeURIComponent(rid); });
      tb.appendChild(row);
    }
    tbl.appendChild(tb);
    root.appendChild(panel({ title: 'Active runs', sub: 'pick a run to inspect', accent: 'live', body: tbl }));
    return;
  }

  ensure(runId, repaint);
  const data = cache.get(runId);
  if (!data) { root.appendChild(loading('Reading run')); return; }

  const run = (data.run && (data.run.run || data.run)) || {};
  root.appendChild(el('div', { style: 'margin-bottom:16px' }, [
    readouts([
      { label: 'phase', value: run.phase || '—', tone: 'live' },
      { label: 'wall clock', value: fmt(run.elapsed_seconds, 0) + 's', foot: run.budget_seconds ? 'of ' + run.budget_seconds + 's' : '' },
      { label: 'drift count', value: run.drift_count != null ? run.drift_count : '—' },
    ]),
  ]));

  // transcript / events
  const events = (data.run && Array.isArray(data.run.events)) ? data.run.events
    : (data.run && data.run.run && Array.isArray(data.run.run.events)) ? data.run.run.events : [];
  const log = el('div', { class: 'mcA-log' });
  if (events.length) {
    for (const ev of events) {
      log.appendChild(el('div', { class: 'mcA-log-row' }, [
        el('span', { class: 'mcA-log-ts' }, [String(ev.ts || ev.emitted_at || '').slice(11, 23) || '—']),
        el('span', { class: 'mcA-log-kind' }, [ev.kind || '—']),
        el('span', { class: 'mcA-log-sum' }, [ev.summary || ev.kind || '']),
      ]));
    }
  } else {
    log.appendChild(empty('No events for this run.'));
  }
  root.appendChild(panel({
    title: 'Event tail',
    sub: 'live goldfive events for this run',
    accent: 'live',
    actions: state.heartbeat && state.heartbeat.harmonograf_url
      ? el('a', { class: 'mcA-btn', href: harmoUrl(run), target: '_blank', rel: 'noopener' }, ['open in harmonograf →'])
      : null,
    body: log,
  }));
}

function harmoUrl(run) {
  const base = (state.heartbeat && state.heartbeat.harmonograf_url) || '';
  const sid = run.adk_session_id || run.session_id;
  if (base && sid) return base.replace(/\/$/, '') + '/#/session/' + encodeURIComponent(sid);
  return base || '#';
}

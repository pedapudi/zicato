// variants/A/views/tournament.js — the bold lineage / gauntlet viz.
//
// A full-lineage, non-colliding bracket of the whole epoch's
// king-of-the-hill gauntlet: the champion through-line plus every
// challenger matchup, promote/reject color-coded, each node clickable
// into its experiment telemetry. Plus the live active matchup when a
// tournament is in flight.
//
// Data: state.epochDef.experiments (resolved lineage),
// /api/active-tournament (live matchup).

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import { panel, readouts, empty, loading, chip, bar } from '../components/instruments.js';
import { gauntlet, gauntletLegend } from '../components/gauntlet.js';
import { navigate } from '../router.js';

function decisionOf(exp) {
  const o = exp && exp.outcome;
  if (!o || typeof o !== 'object') return null;
  const d = String(o.tournament_decision || o.decision || '').toLowerCase();
  if (d.includes('promot')) return 'promoted';
  if (d.includes('reject')) return 'rejected';
  return d || null;
}
function fmt(v, d = 3) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(d) : '—'; }

function buildLineage(def, epochId) {
  const exps = Array.isArray(def.experiments) ? def.experiments : [];
  const hb = state.heartbeat || {};
  const liveGen = (hb.epoch_id === epochId && hb.generation_id) ? hb.generation_id : null;
  const spine = [], challengers = [];
  let last = null;
  for (const exp of exps) {
    const id = exp.generation_id || '?';
    const dec = decisionOf(exp);
    const isBaseline = !exp.parent_generation_id && exp.outcome == null;
    const scalar = exp.outcome && typeof exp.outcome.scalar_score === 'number' ? exp.outcome.scalar_score : null;
    if (isBaseline || dec === 'promoted') { spine.push({ id, scalar }); last = id; }
    else if (dec === 'rejected') {
      challengers.push({ id, parentId: exp.parent_generation_id || last, decision: 'rejected', delta: exp.outcome ? exp.outcome.scalar_score_delta : null });
    }
  }
  if (liveGen && !spine.find((n) => n.id === liveGen) && !challengers.find((c) => c.id === liveGen)) {
    spine.push({ id: liveGen, scalar: null, live: true });
  }
  return { spine, challengers };
}

// live active matchup, rendered from active_tournament.json.
function activeMatchup() {
  const at = state.activeTournament;
  if (!at) return null;
  const entries = Array.isArray(at.entries) ? at.entries : [];
  // group by entry_id, side
  const byEntry = new Map();
  for (const e of entries) {
    if (!byEntry.has(e.entry_id)) byEntry.set(e.entry_id, {});
    byEntry.get(e.entry_id)[e.side] = e;
  }
  let done = 0, running = 0, queued = 0;
  for (const e of entries) {
    if (e.status === 'done' || e.status === 'completed') done += 1;
    else if (e.status === 'running') running += 1;
    else queued += 1;
  }
  const champAgg = at.partial_champion_agg || {};
  const challAgg = at.partial_challenger_agg || {};

  const tbl = el('table', { class: 'mcA-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['board entry']),
    el('th', null, ['champion (' + (at.parent_generation_id || '?') + ')']),
    el('th', null, ['challenger (' + (at.child_generation_id || '?') + ')']),
  ])]));
  const tb = el('tbody');
  for (const [eid, sides] of byEntry) {
    tb.appendChild(el('tr', null, [
      el('td', { class: 'mono' }, [eid]),
      el('td', null, [sideCell(sides.parent)]),
      el('td', null, [sideCell(sides.child)]),
    ]));
  }
  tbl.appendChild(tb);

  return panel({
    title: 'Active matchup',
    sub: 'round ' + ((at.round_index || 0) + 1) + (at.total_rounds ? ' of ' + at.total_rounds : '') + ' · live',
    accent: 'live',
    body: [
      readouts([
        { label: 'done', value: done, tone: 'go' },
        { label: 'running', value: running, tone: 'live' },
        { label: 'queued', value: queued },
        { label: 'champion scalar', value: fmt(champAgg.scalar), foot: 'partial' },
        { label: 'challenger scalar', value: fmt(challAgg.scalar), tone: typeof challAgg.scalar === 'number' && typeof champAgg.scalar === 'number' && challAgg.scalar < champAgg.scalar ? 'go' : null, foot: 'partial' },
      ]),
      el('div', { style: 'margin-top:14px' }, [tbl]),
    ],
  });
}

function sideCell(e) {
  if (!e) return el('span', { class: 'mcA-readout-foot' }, ['—']);
  const st = e.status;
  if (st === 'done' || st === 'completed') {
    const loss = e.loss_summary && e.loss_summary.drift_loss;
    const pass = e.loss_summary && e.loss_summary.pass_fail;
    return el('span', { class: 'mono' }, [
      typeof loss === 'number' ? loss.toFixed(3) : '✓', ' ',
      el('span', { class: pass ? 'mcA-tag-good' : 'mcA-tag-bad' }, [pass ? '✓' : (pass === 0 ? '✗' : '')]),
    ]);
  }
  if (st === 'running') return el('span', { style: 'display:flex;align-items:center;gap:8px' }, [chip('running', 'live')]);
  if (st === 'aborted' || st === 'failed') return chip('aborted', 'stop');
  return chip('queued', 'idle');
}

export function renderTournament(root, params, repaint) {
  const epochId = params.epochId || (state.epochDef && state.epochDef.epoch_id) || (state.heartbeat && state.heartbeat.epoch_id);
  const def = state.epochDef;
  root.textContent = '';

  root.appendChild(el('div', { class: 'mcA-pagehead' }, [
    el('h1', null, ['Lineage']),
    el('span', { class: 'mcA-pagehead-sub mono' }, [epochId || '—']),
  ]));

  if (def == null) { root.appendChild(loading('Loading lineage')); return; }

  const { spine, challengers } = buildLineage(def, def.epoch_id || epochId);
  root.appendChild(panel({
    title: 'Gauntlet — full lineage',
    sub: spine.length + ' champion hop' + (spine.length === 1 ? '' : 's') + ' · ' + challengers.length + ' challenger' + (challengers.length === 1 ? '' : 's') + ' rejected · click any node',
    accent: 'accent',
    body: spine.length === 0 ? empty('No lineage yet.') : el('div', null, [
      el('div', { class: 'mcA-gauntlet' }, [
        gauntlet({ spine, challengers, onSelect: (id) => navigate('experiment', { epochId: def.epoch_id || epochId, genId: id }) }),
      ]),
      gauntletLegend(),
    ]),
  }));

  const am = activeMatchup();
  if (am) root.appendChild(el('div', { style: 'margin-top:16px' }, [am]));
}

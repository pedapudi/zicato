// variants/A/views/tournament.js — lineage + the match-up theatre.
//
// This view carries two of the four enrichment themes:
//   * theme 1 (lineage) — the command roster: champion crowned at top
//     defending, challengers as call-signs, dead branches dimmed.
//   * theme 4 (match-ups) — the REAL king-of-the-hill gauntlet ladder
//     from /api/tournaments + /api/matchup-grid (paired per-board duels),
//     plus a style switcher that re-renders the SAME candidate set under
//     single-elim / double-elim / Swiss / racing topologies, each labelled
//     conceptual (SELECTION.md §2/§5/§6).
//
// The bold lineage bracket SVG (gauntlet component) is kept below as the
// spatial overview, and the live active matchup renders when in flight.
//
// Data: /api/tournaments (real matchups), /api/matchup-grid/{e}/{c}/{ch}
// (paired duel), state.epochDef.experiments (lineage), state.lineage.

import { el } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { state } from '../../../core/state.js';
import { panel, readouts, empty, loading, chip } from '../components/instruments.js';
import { gauntlet, gauntletLegend } from '../components/gauntlet.js';
import { commandRoster } from '../components/lifecycle.js';
import { gauntletLadder, styleSwitcher, styleView } from '../components/matchups.js';
import { navigate, href } from '../router.js';

const tournaments = new Map(); // epochId -> /api/tournaments payload
const grids = new Map();       // challengerId -> entry_grid[]
const loadingSet = new Set();
let activeStyle = 'gauntlet';
let expandedRung = null;       // challengerId whose per-board duel is open

export function resetTournamentCache() {
  tournaments.clear(); grids.clear(); loadingSet.clear();
  activeStyle = 'gauntlet'; expandedRung = null;
}

function enc(v) { return encodeURIComponent(v == null ? '' : String(v)); }
function fmt(v, d = 3) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(d) : '—'; }

function decisionOf(exp) {
  const o = exp && exp.outcome;
  if (!o || typeof o !== 'object') return null;
  const d = String(o.tournament_decision || o.decision || '').toLowerCase();
  if (d.includes('promot')) return 'promoted';
  if (d.includes('reject')) return 'rejected';
  return d || null;
}

async function ensureTournaments(epochId, repaint) {
  if (tournaments.has(epochId) || loadingSet.has('t' + epochId)) return;
  loadingSet.add('t' + epochId);
  try { tournaments.set(epochId, await fetchJson('/api/tournaments')); }
  catch { tournaments.set(epochId, { matchups: [], champion_lineage: [] }); }
  loadingSet.delete('t' + epochId);
  if (repaint) repaint();
}

async function ensureGrid(epochId, champId, chalId, repaint) {
  if (!chalId || grids.has(chalId) || loadingSet.has('g' + chalId)) return;
  loadingSet.add('g' + chalId);
  try {
    const r = await fetchJson('/api/matchup-grid/' + enc(epochId) + '/' + enc(champId) + '/' + enc(chalId));
    grids.set(chalId, Array.isArray(r.entry_grid) ? r.entry_grid : []);
  } catch { grids.set(chalId, []); }
  loadingSet.delete('g' + chalId);
  if (repaint) repaint();
}

// resolve the lineage (spine + challengers + champion) from experiments,
// falling back to /api/lineage generations.
function buildLineage(def, epochId) {
  const exps = Array.isArray(def && def.experiments) ? def.experiments : [];
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
    } else {
      challengers.push({ id, parentId: exp.parent_generation_id || last, decision: null, delta: exp.outcome ? exp.outcome.scalar_score_delta : null, live: id === liveGen });
    }
  }
  if (liveGen && !spine.find((n) => n.id === liveGen) && !challengers.find((c) => c.id === liveGen)) {
    challengers.push({ id: liveGen, parentId: last, decision: null, live: true });
  }
  return { spine, challengers };
}

// candidate set for the style switcher: champion first, then challengers,
// each with a scalar (lower = better). Seed scalar from experiments.
function candidateSet(def, spine, challengers) {
  const exps = Array.isArray(def && def.experiments) ? def.experiments : [];
  const scalarById = new Map();
  for (const e of exps) {
    const s = e.outcome && typeof e.outcome.scalar_score === 'number' ? e.outcome.scalar_score : null;
    if (s != null) scalarById.set(e.generation_id, s);
  }
  const out = [];
  const champ = spine[spine.length - 1];
  if (champ) out.push({ id: champ.id, scalar: champ.scalar != null ? champ.scalar : scalarById.get(champ.id), role: 'champion' });
  for (const c of challengers) {
    out.push({ id: c.id, scalar: scalarById.get(c.id), role: 'challenger' });
  }
  return out;
}

// live active matchup, rendered from active_tournament.json.
function activeMatchup() {
  const at = state.activeTournament;
  if (!at) return null;
  const entries = Array.isArray(at.entries) ? at.entries : [];
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
  ensureTournaments(epochId, repaint);

  const { spine, challengers } = buildLineage(def, def.epoch_id || epochId);
  const champ = spine[spine.length - 1] || null;
  const onSelect = (id) => navigate('experiment', { epochId: def.epoch_id || epochId, genId: id });

  // ---- theme 1: command roster (defended hill) --------------------
  root.appendChild(el('div', { style: 'margin-bottom:16px' }, [
    panel({
      title: 'Command roster',
      sub: 'champion crowned and defending · challengers as call-signs · dead branches dimmed',
      accent: 'go',
      body: commandRoster({ champion: champ, challengers, onSelect }),
    }),
  ]));

  // ---- theme 4: the match-up theatre ------------------------------
  const tdata = tournaments.get(epochId) || { matchups: [], champion_lineage: [] };
  const realChampion = (Array.isArray(tdata.champion_lineage) && tdata.champion_lineage.length)
    ? tdata.champion_lineage[tdata.champion_lineage.length - 1]
    : (champ && champ.id);
  const matchups = Array.isArray(tdata.matchups) ? tdata.matchups : [];

  // fetch the grid for the currently-expanded rung.
  if (expandedRung) {
    const m = matchups.find((x) => x.challenger === expandedRung);
    if (m) ensureGrid(epochId, m.champion || realChampion, m.challenger, repaint);
  }

  const candidates = candidateSet(def, spine, challengers);
  const theatreBody = [];
  theatreBody.push(styleSwitcher(activeStyle, (k) => { activeStyle = k; if (repaint) repaint(); }));
  if (activeStyle === 'gauntlet') {
    theatreBody.push(gauntletLadder({
      champion: realChampion,
      matchups,
      grids,
      expanded: expandedRung,
      onSelectGrid: (id) => { expandedRung = id; if (id) ensureGrid(epochId, realChampion, id, repaint); if (repaint) repaint(); },
    }));
  } else {
    theatreBody.push(styleView(activeStyle, candidates));
  }
  root.appendChild(el('div', { style: 'margin-bottom:16px' }, [
    panel({
      title: 'Match-up theatre',
      sub: 'the real gauntlet zicato ran · switch styles to see the same candidates under other structures',
      accent: 'accent',
      body: theatreBody,
    }),
  ]));

  // ---- the bold lineage bracket SVG (spatial overview) ------------
  root.appendChild(el('div', { style: 'margin-bottom:16px' }, [
    panel({
      title: 'Gauntlet bracket',
      sub: spine.length + ' champion hop' + (spine.length === 1 ? '' : 's') + ' · ' + challengers.length + ' challenger' + (challengers.length === 1 ? '' : 's') + ' · click any node',
      body: spine.length === 0 ? empty('No lineage yet.') : el('div', null, [
        el('div', { class: 'mcA-gauntlet' }, [
          gauntlet({ spine, challengers, onSelect }),
        ]),
        gauntletLegend(),
      ]),
    }),
  ]));

  const am = activeMatchup();
  if (am) root.appendChild(el('div', { style: 'margin-top:16px' }, [am]));
}

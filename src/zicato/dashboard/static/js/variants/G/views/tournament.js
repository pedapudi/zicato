// variants/G/views/tournament.js — match-ups (theme 4).
//
// The real gauntlet zicato ran, as D's NON-COLLIDING paired slopegraphs
// (champion loss → challenger loss per board entry) plus C's topology
// switcher: the same candidate set re-laid-out under gauntlet (real) /
// single-elim / double-elim / Swiss / racing (conceptual overlays,
// honestly labelled). Click a challenger → its candidate view.
//
// Digest-gated; the active style + selected matchup live in module scope.

import { el } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { state } from '../../../core/state.js';
import { panel, empty, loading, chip, fmt } from '../components/ui.js';
import { pairedSlopegraph } from '../svg.js';
import { renderTopology } from '../components/diagram.js';
import { TOURNAMENT_STYLES } from '../diagram/topology.js';
import { candidateSet, lineageModel, matchupGridRows, scalarOf } from '../model.js';
import { navigate } from '../router.js';

const tournaments = new Map(); // epochId -> /api/tournaments
const grids = new Map();       // challengerId -> matchup grid rows
const loadingSet = new Set();
let activeStyle = 'gauntlet';
let selectedMatchup = null;    // challenger id whose slopegraph is open
let _lastDigest = null;

export function resetTournamentCache() {
  tournaments.clear(); grids.clear(); loadingSet.clear();
  activeStyle = 'gauntlet'; selectedMatchup = null; _lastDigest = null;
}

function enc(v) { return encodeURIComponent(v == null ? '' : String(v)); }

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
    grids.set(chalId, matchupGridRows(r));
  } catch { grids.set(chalId, []); }
  loadingSet.delete('g' + chalId);
  if (repaint) repaint();
}

export function tournamentDigest(params) {
  const epochId = params.epochId || (state.epochDef && state.epochDef.epoch_id) || (state.heartbeat && state.heartbeat.epoch_id);
  const td = tournaments.get(epochId);
  const exps = state.epochDef && Array.isArray(state.epochDef.experiments) ? state.epochDef.experiments : null;
  return JSON.stringify({
    epochId,
    loaded: !!state.epochDef,
    exps: exps ? exps.map((e) => [e.generation_id, e.parent_generation_id, scalarOf(e)]) : null,
    matchups: td && Array.isArray(td.matchups) ? td.matchups.map((m) => [m.champion, m.challenger, m.decision]) : null,
    style: activeStyle,
    selected: selectedMatchup,
    grid: selectedMatchup ? grids.has(selectedMatchup) : false,
  });
}

function styleSwitcher() {
  return el('div', { class: 'g-styles', role: 'tablist' }, TOURNAMENT_STYLES.map((s) => {
    const b = el('button', {
      class: 'g-style-btn' + (s.id === activeStyle ? ' is-active' : ''),
      type: 'button', role: 'tab', 'aria-selected': s.id === activeStyle ? 'true' : 'false',
    }, [s.label, s.real ? el('span', { class: 'g-style-real' }, ['real']) : el('span', { class: 'g-style-illus' }, ['illustrative'])]);
    b.addEventListener('click', () => { activeStyle = s.id; });
    return b;
  }));
}

// the paired slopegraphs — one per real matchup (champion vs challenger).
function slopegraphs(epochId, repaint) {
  const td = tournaments.get(epochId);
  if (!td) return loading('Reading the gauntlet');
  const matchups = Array.isArray(td.matchups) ? td.matchups : [];
  if (!matchups.length) return empty('No matchups recorded for this epoch yet.');
  const champion = (Array.isArray(td.champion_lineage) && td.champion_lineage.length)
    ? td.champion_lineage[td.champion_lineage.length - 1] : null;

  // ensure the open matchup's grid is loaded.
  if (selectedMatchup) ensureGrid(epochId, champion, selectedMatchup, repaint);

  const wrap = el('div', { class: 'g-matchups' });
  for (const m of matchups) {
    const open = selectedMatchup === m.challenger;
    const headBtn = el('button', { class: 'g-matchup-head' + (open ? ' is-open' : ''), type: 'button' }, [
      el('span', { class: 'g-mono' }, [m.champion + ' → ' + m.challenger]),
      chip(m.decision || 'pending', m.decision === 'promoted' ? 'improve' : m.decision === 'rejected' ? 'regress' : 'idle'),
      typeof m.delta_scalar === 'number' ? el('span', { class: 'g-readout-foot' }, ['Δ ' + fmt(m.delta_scalar, 2)]) : null,
      el('span', { class: 'g-matchup-toggle' }, [open ? '∧' : '∨']),
    ]);
    headBtn.addEventListener('click', () => {
      selectedMatchup = open ? null : m.challenger;
      if (selectedMatchup) ensureGrid(epochId, champion, selectedMatchup, repaint);
      if (repaint) repaint();
    });
    const block = el('div', { class: 'g-matchup' }, [headBtn]);
    if (open) {
      const rows = grids.get(m.challenger);
      if (!rows) block.appendChild(loading('Reading paired per-board duel'));
      else if (!rows.length) block.appendChild(empty('No per-board grid recorded.'));
      else {
        block.appendChild(el('div', { class: 'g-slope-wrap' }, [
          pairedSlopegraph({
            width: 560, height: 320, labelGap: 160,
            left: { title: m.champion }, right: { title: m.challenger },
            series: rows.map((r) => ({ id: r.entryId, label: r.entryId, a: r.championLoss, b: r.challengerLoss, verdict: r.verdict })),
            onClick: () => navigate('experiment', { epochId, genId: m.challenger }),
          }),
        ]));
        if (m.rejection_reason) block.appendChild(el('div', { class: 'g-readout-foot is-regress g-matchup-reason' }, ['↳ ' + m.rejection_reason]));
      }
    }
    wrap.appendChild(block);
  }
  return wrap;
}

export function renderTournament(root, params, repaint) {
  const epochId = params.epochId || (state.epochDef && state.epochDef.epoch_id) || (state.heartbeat && state.heartbeat.epoch_id);
  if (epochId) ensureTournaments(epochId, repaint);

  const digest = tournamentDigest(params);
  if (digest === _lastDigest && root.firstChild) return;
  _lastDigest = digest;
  root.textContent = '';

  root.appendChild(el('div', { class: 'g-pagehead' }, [
    el('h1', null, ['Match-ups']),
    el('span', { class: 'g-pagehead-sub g-mono' }, [epochId || '—']),
  ]));

  if (state.epochDef == null) { root.appendChild(loading('Loading match-ups')); return; }

  // paired slopegraphs (the real gauntlet duels).
  root.appendChild(el('div', { class: 'g-section' }, [
    panel({
      title: 'Paired board duels',
      sub: 'each line is one board entry: champion loss → challenger loss · non-colliding · click a matchup to open',
      body: slopegraphs(epochId, repaint),
    }),
  ]));

  // topology switcher (C diagrams).
  const cands = candidateSet(state, epochId);
  const styleDef = TOURNAMENT_STYLES.find((s) => s.id === activeStyle) || TOURNAMENT_STYLES[0];
  const td = tournaments.get(epochId) || {};
  const champion = (Array.isArray(td.champion_lineage) && td.champion_lineage.length)
    ? td.champion_lineage[td.champion_lineage.length - 1] : (cands[0] && cands[0].id);
  // annotate gauntlet edges with the real verdict deltas.
  if (activeStyle === 'gauntlet' && Array.isArray(td.matchups)) {
    for (const c of cands) {
      const m = td.matchups.find((x) => x.challenger === c.id);
      if (m && typeof m.delta_scalar === 'number') c.deltaLabel = 'Δ ' + fmt(m.delta_scalar, 1);
    }
  }
  const layout = styleDef.fn(cands, { cx: 380, cy: 220, radius: 150 });
  root.appendChild(el('div', { class: 'g-section' }, [
    panel({
      title: 'Tournament topology',
      sub: styleDef.real ? 'the shipped king-of-the-hill structure — real paired duels' : 'illustrative overlay of the same candidates under ' + styleDef.topology,
      body: [
        styleSwitcher(),
        el('div', { class: 'g-style-blurb' }, [styleDef.blurb]),
        el('div', { class: 'g-topology-wrap' }, [
          renderTopology(layout, { onSelect: (n) => { if (n.role === 'challenger' || n.role === 'champion') navigate('experiment', { epochId, genId: n.id }); } }),
        ]),
      ],
    }),
  ]));
  void champion; void lineageModel;
}

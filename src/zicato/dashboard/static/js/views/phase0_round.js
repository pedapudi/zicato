// views/phase0_round.js — L3 (round/tournament-level) view.
//
// Renders the per-round shell: always-on champion vs challenger header,
// per-entry comparison (table), per-judge comparison (stub until #179 /
// #180 land — the per-judge data path and the tournament_id FK need
// to be wired before this can be honestly populated), and a decision +
// driver call-out at the bottom. Sources its data from the already-
// populated bracket payload (state.bracket.matchups) so no additional
// fetch is needed for the minimal phase-0 render.

import { $, el, clearChildren } from '../core/dom.js';
import { state } from '../core/state.js';

function findMatchup(championId, challengerId) {
  const br = state.bracket || {};
  const matchups = Array.isArray(br.matchups) ? br.matchups : [];
  for (const m of matchups) {
    if (m && m.champion === championId && m.challenger === challengerId) return m;
  }
  return null;
}

function renderVs(params, matchup) {
  const node = $('phase0-round-vs');
  if (!node) return;
  clearChildren(node);
  if (!params || (!params.championId && !params.challengerId)) {
    node.appendChild(el('p', { class: 'empty' }, ['No matchup selected.']));
    return;
  }
  const champ = params.championId || (matchup && matchup.champion) || '—';
  const chal = params.challengerId || (matchup && matchup.challenger) || '—';
  const wrap = el('div', { class: 'phase0-vs' });
  wrap.appendChild(el('div', { class: 'phase0-vs-side' }, [
    el('div', { class: 'phase0-vs-label' }, ['champion']),
    el('div', { class: 'phase0-vs-id mono' }, [champ]),
  ]));
  wrap.appendChild(el('div', { class: 'phase0-vs-versus' }, ['vs']));
  wrap.appendChild(el('div', { class: 'phase0-vs-side' }, [
    el('div', { class: 'phase0-vs-label' }, ['challenger']),
    el('div', { class: 'phase0-vs-id mono' }, [chal]),
  ]));
  node.appendChild(wrap);
}

function renderEntries() {
  const node = $('phase0-round-entries');
  if (!node) return;
  clearChildren(node);
  // The per-entry A/B grid is sourced from /api/matchup-grid and is
  // intentionally NOT fetched here (the existing tournament view's grid
  // loader is the canonical path). For phase 0 we surface the slot.
  node.appendChild(el('p', { class: 'panel-subheader' },
    ['Per-entry comparison lands once the L3 fetch path migrates from the legacy view.']));
}

function renderJudges() {
  const node = $('phase0-round-judges');
  if (!node) return;
  clearChildren(node);
  node.appendChild(el('p', { class: 'empty phase0-stub-msg' },
    ['(per-judge comparison — populated once #179 / #180 land)']));
}

function renderDecision(matchup) {
  const node = $('phase0-round-decision');
  if (!node) return;
  clearChildren(node);
  if (!matchup) {
    node.appendChild(el('p', { class: 'empty' }, ['No decision yet.']));
    return;
  }
  const decision = matchup.decision || '—';
  const dsv = (typeof matchup.delta_scalar === 'number' && isFinite(matchup.delta_scalar))
    ? matchup.delta_scalar.toFixed(3) : '—';
  const driver = matchup.driver || matchup.rejection_reason || '';
  const wrap = el('div', { class: 'phase0-decision' });
  wrap.appendChild(el('div', { class: 'phase0-decision-line' }, [
    el('strong', null, ['decision: ']),
    el('span', { class: 'mono' }, [String(decision)]),
  ]));
  wrap.appendChild(el('div', { class: 'phase0-decision-line' }, [
    el('strong', null, ['Δscalar: ']),
    el('span', { class: 'mono' }, [dsv]),
  ]));
  if (driver) {
    wrap.appendChild(el('div', { class: 'phase0-decision-line' }, [
      el('strong', null, ['driver: ']),
      String(driver),
    ]));
  }
  node.appendChild(wrap);
}

export function renderPhase0Round(params) {
  const matchup = (params && params.championId && params.challengerId)
    ? findMatchup(params.championId, params.challengerId)
    : null;
  renderVs(params, matchup);
  renderEntries();
  renderJudges();
  renderDecision(matchup);
}

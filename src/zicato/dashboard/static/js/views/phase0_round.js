// views/phase0_round.js — L3 (round/tournament-level) view.
//
// Renders the per-round shell: always-on champion vs challenger header,
// per-entry comparison (via tournament_id FK + champion comparison),
// per-judge comparison (Δ per judge with the primary-driver call-out),
// and a decision + driver call-out at the bottom. Sources matchup
// metadata from ``state.bracket.matchups``; the comparison tables are
// lazy-fetched per (champion, challenger) key.

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';

const _entriesCache = new Map(); // "epoch/champ->chall" -> {champ, chall} payloads
const _judgeCmpCache = new Map(); // "epoch/champ->chall" -> payload
const _loadingEntries = new Set();
const _loadingJudges = new Set();

export function resetRoundCaches() {
  _entriesCache.clear();
  _judgeCmpCache.clear();
  _loadingEntries.clear();
  _loadingJudges.clear();
}

export function roundEntriesPayload(epochId, champId, chalId) {
  return _entriesCache.get(epochId + '/' + champId + '->' + chalId) || null;
}

export function roundJudgesPayload(epochId, champId, chalId) {
  return _judgeCmpCache.get(epochId + '/' + champId + '->' + chalId) || null;
}

async function ensureEntries(epochId, champId, chalId, repaint) {
  if (!epochId || !champId || !chalId) return null;
  const key = epochId + '/' + champId + '->' + chalId;
  if (_entriesCache.has(key)) return _entriesCache.get(key);
  if (_loadingEntries.has(key)) return null;
  _loadingEntries.add(key);
  try {
    const champData = await fetchJson('/api/generation/'
      + encodeURIComponent(epochId) + '/' + encodeURIComponent(champId) + '/per-entry');
    const chalData = await fetchJson('/api/generation/'
      + encodeURIComponent(epochId) + '/' + encodeURIComponent(chalId) + '/per-entry');
    _entriesCache.set(key, { champion: champData, challenger: chalData });
  } catch {
    _entriesCache.set(key, { champion: null, challenger: null });
  } finally {
    _loadingEntries.delete(key);
    if (typeof repaint === 'function') repaint();
  }
  return _entriesCache.get(key);
}

async function ensureJudgesComparison(epochId, champId, chalId, repaint) {
  if (!epochId || !champId || !chalId) return null;
  const key = epochId + '/' + champId + '->' + chalId;
  if (_judgeCmpCache.has(key)) return _judgeCmpCache.get(key);
  if (_loadingJudges.has(key)) return null;
  _loadingJudges.add(key);
  try {
    const data = await fetchJson('/api/round/'
      + encodeURIComponent(epochId) + '/'
      + encodeURIComponent(champId) + '/' + encodeURIComponent(chalId)
      + '/per-judge-comparison');
    if (data && typeof data === 'object') _judgeCmpCache.set(key, data);
  } catch {
    _judgeCmpCache.set(key, {
      epoch_id: epochId, champion: champId, challenger: chalId,
      judges: [], primary_driver: null,
    });
  } finally {
    _loadingJudges.delete(key);
    if (typeof repaint === 'function') repaint();
  }
  return _judgeCmpCache.get(key);
}

function _fmtNum(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  return v.toFixed(3);
}

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

function renderEntries(epochId, champId, chalId) {
  const node = $('phase0-round-entries');
  if (!node) return;
  clearChildren(node);
  if (!epochId || !champId || !chalId) {
    node.appendChild(el('p', { class: 'empty' }, ['No matchup selected.']));
    return;
  }
  const cached = _entriesCache.get(epochId + '/' + champId + '->' + chalId);
  if (!cached) {
    node.appendChild(el('p', { class: 'empty' }, ['loading per-entry comparison…']));
    return;
  }
  const champEntries = (cached.champion && Array.isArray(cached.champion.entries))
    ? cached.champion.entries : [];
  const chalEntries = (cached.challenger && Array.isArray(cached.challenger.entries))
    ? cached.challenger.entries : [];
  const byEntry = new Map();
  for (const r of champEntries) {
    if (r && r.entry_id) byEntry.set(r.entry_id, { champ: r });
  }
  for (const r of chalEntries) {
    if (r && r.entry_id) {
      const slot = byEntry.get(r.entry_id) || {};
      slot.chal = r;
      byEntry.set(r.entry_id, slot);
    }
  }
  if (byEntry.size === 0) {
    node.appendChild(el('p', { class: 'empty' },
      ['No per-entry data recorded for this round.']));
    return;
  }
  // Surface the FK we used so the operator can see this is a real
  // tournament-keyed query, not a generation-scoped fallback.
  if (cached.challenger && cached.challenger.tournament_id) {
    node.appendChild(el('p', { class: 'panel-subheader mono' },
      ['tournament_id · ', cached.challenger.tournament_id]));
  }
  const tbl = el('table', { class: 'phase0-mini-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['entry']),
    el('th', null, ['champ drift']),
    el('th', null, ['chal drift']),
    el('th', null, ['Δ']),
    el('th', null, ['champ pass']),
    el('th', null, ['chal pass']),
  ])]));
  const tbody = el('tbody');
  const ids = Array.from(byEntry.keys()).sort();
  for (const eid of ids) {
    const { champ, chal } = byEntry.get(eid);
    const c = (champ && typeof champ.drift_loss === 'number') ? champ.drift_loss : null;
    const ch = (chal && typeof chal.drift_loss === 'number') ? chal.drift_loss : null;
    const delta = (c != null && ch != null) ? (ch - c) : null;
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'mono' }, [eid]),
      el('td', { class: 'mono' }, [_fmtNum(c)]),
      el('td', { class: 'mono' }, [_fmtNum(ch)]),
      el('td', { class: 'mono' }, [_fmtNum(delta)]),
      el('td', { class: 'mono' }, [String(champ && champ.pass_fail != null ? champ.pass_fail : '—')]),
      el('td', { class: 'mono' }, [String(chal && chal.pass_fail != null ? chal.pass_fail : '—')]),
    ]));
  }
  tbl.appendChild(tbody);
  node.appendChild(tbl);
}

function renderJudges(epochId, champId, chalId) {
  const node = $('phase0-round-judges');
  if (!node) return;
  clearChildren(node);
  if (!epochId || !champId || !chalId) {
    node.appendChild(el('p', { class: 'empty' }, ['No matchup selected.']));
    return;
  }
  const data = _judgeCmpCache.get(epochId + '/' + champId + '->' + chalId);
  if (!data) {
    node.appendChild(el('p', { class: 'empty' }, ['loading per-judge comparison…']));
    return;
  }
  const judges = Array.isArray(data.judges) ? data.judges : [];
  if (judges.length === 0) {
    const msg = data.note ? '(no per-judge data: ' + data.note + ')'
      : '(no per-judge data recorded for either side)';
    node.appendChild(el('p', { class: 'empty' }, [msg]));
    return;
  }
  if (data.primary_driver) {
    node.appendChild(el('p', { class: 'panel-subheader' }, [
      el('strong', null, ['primary driver: ']),
      el('span', { class: 'mono' }, [data.primary_driver]),
    ]));
  }
  const tbl = el('table', { class: 'phase0-mini-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['judge']),
    el('th', null, ['champion']),
    el('th', null, ['challenger']),
    el('th', null, ['Δ']),
  ])]));
  const tbody = el('tbody');
  for (const j of judges) {
    const driverClass = j.judge_name === data.primary_driver
      ? 'phase0-judge-driver mono' : 'mono';
    tbody.appendChild(el('tr', null, [
      el('td', { class: driverClass }, [j.judge_name || '—']),
      el('td', { class: 'mono' }, [_fmtNum(j.champion_weighted_loss)]),
      el('td', { class: 'mono' }, [_fmtNum(j.challenger_weighted_loss)]),
      el('td', { class: 'mono' }, [_fmtNum(j.delta)]),
    ]));
  }
  tbl.appendChild(tbody);
  node.appendChild(tbl);
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

export function renderPhase0Round(params, repaint) {
  const matchup = (params && params.championId && params.challengerId)
    ? findMatchup(params.championId, params.challengerId)
    : null;
  const epochId = (params && params.epochId)
    || (state.epochDef && state.epochDef.epoch_id) || null;
  const champId = params && params.championId;
  const chalId = params && params.challengerId;
  ensureEntries(epochId, champId, chalId, repaint);
  ensureJudgesComparison(epochId, champId, chalId, repaint);
  renderVs(params, matchup);
  renderEntries(epochId, champId, chalId);
  renderJudges(epochId, champId, chalId);
  renderDecision(matchup);
}

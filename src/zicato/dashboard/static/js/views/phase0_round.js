// views/phase0_round.js — L3 (round/tournament-level) view.
//
// The vs slot carries the big champion / challenger card; the
// entries slot the per-entry comparison; the judges slot the per-judge
// comparison + a primary-driver call-out; the decision slot the
// decision pill + driver text.

import { $, el, clearChildren } from '../core/dom.js';
import { fetchJson } from '../core/api.js';
import { state } from '../core/state.js';
import { renderCard, renderCalloutCard } from '../components/card.js';
import { renderPill } from '../components/pill.js';
import { renderLoadingState, renderEmptyState } from '../components/loading.js';

const _entriesCache = new Map();
const _judgeCmpCache = new Map();
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

function _passRate(entries) {
  if (!Array.isArray(entries) || entries.length === 0) return null;
  let passed = 0;
  let counted = 0;
  for (const e of entries) {
    const pf = e && e.pass_fail;
    if (pf == null) continue;
    counted += 1;
    if (pf === true || pf === 1 || String(pf).toLowerCase() === 'pass') passed += 1;
  }
  if (counted === 0) return null;
  return passed / counted;
}

function _meanLoss(entries) {
  if (!Array.isArray(entries) || entries.length === 0) return null;
  let sum = 0;
  let n = 0;
  for (const e of entries) {
    if (typeof e.drift_loss === 'number' && isFinite(e.drift_loss)) {
      sum += e.drift_loss;
      n += 1;
    }
  }
  if (n === 0) return null;
  return sum / n;
}

function findMatchup(championId, challengerId) {
  const br = state.bracket || {};
  const matchups = Array.isArray(br.matchups) ? br.matchups : [];
  for (const m of matchups) {
    if (m && m.champion === championId && m.challenger === challengerId) return m;
  }
  return null;
}

function _renderRoundSide(label, id, entries, opts) {
  const o = opts || {};
  const pr = _passRate(entries);
  const ml = _meanLoss(entries);
  const cls = ['round-side'];
  if (label === 'champion') cls.push('round-side-champion');
  else cls.push('round-side-challenger');
  if (o.rejected) cls.push('is-rejected');
  return el('div', { class: cls.join(' ') }, [
    el('div', { class: 'round-side-label' }, [label]),
    el('div', { class: 'round-side-id mono' }, [id || '—']),
    el('div', { class: 'round-side-stats' }, [
      el('div', { class: 'round-side-stat' }, [
        el('div', { class: 'round-side-stat-label' }, ['pass rate']),
        el('div', { class: 'round-side-stat-value' },
          [pr == null ? '—' : (pr * 100).toFixed(0) + '%']),
      ]),
      el('div', { class: 'round-side-stat' }, [
        el('div', { class: 'round-side-stat-label' }, ['drift loss']),
        el('div', { class: 'round-side-stat-value' }, [_fmtNum(ml)]),
      ]),
    ]),
  ]);
}

function _renderVs(params, matchup) {
  const node = $('phase0-round-vs');
  if (!node) return;
  clearChildren(node);
  if (!params || (!params.championId && !params.challengerId)) {
    node.appendChild(renderCard({
      title: 'Matchup',
      body: el('p', { class: 'empty' }, ['No matchup selected.']),
    }));
    return;
  }
  // Both sides of the matchup need per-entry data to render the
  // pass-rate / drift-loss columns. If neither side has landed yet,
  // call out the loading state at the top-level card instead of
  // letting it fall through to the all-zero round-side card.
  const epochKey = (state.epochDef && state.epochDef.epoch_id) || params.epochId;
  const champId0 = (params && params.championId) || (matchup && matchup.champion);
  const chalId0  = (params && params.challengerId) || (matchup && matchup.challenger);
  if (champId0 && chalId0
      && !_entriesCache.get(epochKey + '/' + champId0 + '->' + chalId0)) {
    node.appendChild(renderCard({
      title: 'Matchup',
      body: renderLoadingState({ label: 'Loading matchup' }),
    }));
    return;
  }
  const champId = (params && params.championId) || (matchup && matchup.champion) || '—';
  const chalId  = (params && params.challengerId) || (matchup && matchup.challenger) || '—';
  const cached = _entriesCache.get(
    ((state.epochDef && state.epochDef.epoch_id) || params.epochId)
    + '/' + champId + '->' + chalId);
  const champEntries = (cached && cached.champion && Array.isArray(cached.champion.entries))
    ? cached.champion.entries : [];
  const chalEntries = (cached && cached.challenger && Array.isArray(cached.challenger.entries))
    ? cached.challenger.entries : [];

  const champMean = _meanLoss(champEntries);
  const chalMean  = _meanLoss(chalEntries);
  const delta = (champMean != null && chalMean != null) ? chalMean - champMean : null;
  let sentimentClass = '';
  let deltaText = '—';
  if (delta != null) {
    deltaText = (delta > 0 ? '+' : '') + delta.toFixed(3);
    sentimentClass = delta < 0 ? 'is-good' : (delta > 0 ? 'is-bad' : '');
  }
  const decision = matchup ? (matchup.decision || '—').toString().toLowerCase() : null;
  let pillVariant = 'neutral';
  if (decision === 'promoted') pillVariant = 'promoted';
  else if (decision === 'rejected') pillVariant = 'rejected';
  else if (decision === 'deferred') pillVariant = 'deferred';
  const isRejected = (decision === 'rejected');

  const body = el('div', { class: 'round-vs' }, [
    _renderRoundSide('champion', champId, champEntries),
    el('div', { class: 'round-divider' }, [
      el('div', { class: 'round-divider-arrow' }, ['vs']),
      el('div', { class: 'round-divider-delta ' + sentimentClass }, [deltaText]),
      decision ? renderPill(decision, pillVariant) : null,
    ]),
    _renderRoundSide('challenger', chalId, chalEntries, { rejected: isRejected }),
  ]);
  node.appendChild(renderCard({
    title: 'Matchup',
    body,
  }));
}

function _renderEntries(epochId, champId, chalId) {
  const node = $('phase0-round-entries');
  if (!node) return;
  clearChildren(node);
  let body;
  if (!epochId || !champId || !chalId) {
    body = el('p', { class: 'empty' }, ['No matchup selected.']);
  } else {
    const cached = _entriesCache.get(epochId + '/' + champId + '->' + chalId);
    if (!cached) {
      body = renderLoadingState({ label: 'Loading per-entry comparison' });
    } else {
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
        body = renderEmptyState('No per-entry data recorded for this round.');
      } else {
        const wrap = el('div');
        if (cached.challenger && cached.challenger.tournament_id) {
          wrap.appendChild(el('p', {
            style: 'font-size:var(--font-size-11); color:var(--color-text-muted); margin:0 0 var(--space-2); font-family:var(--font-mono);',
          }, ['tournament · ', cached.challenger.tournament_id]));
        }
        const tbl = el('table', { class: 'ds-table' });
        tbl.appendChild(el('thead', null, [el('tr', null, [
          el('th', null, ['entry']),
          el('th', null, ['champ drift']),
          el('th', null, ['chal drift']),
          el('th', null, ['Δ']),
          el('th', null, ['pass flip']),
        ])]));
        const tbody = el('tbody');
        const ids = Array.from(byEntry.keys()).sort();
        for (const eid of ids) {
          const { champ, chal } = byEntry.get(eid);
          const c = (champ && typeof champ.drift_loss === 'number') ? champ.drift_loss : null;
          const ch = (chal && typeof chal.drift_loss === 'number') ? chal.drift_loss : null;
          const delta = (c != null && ch != null) ? (ch - c) : null;
          let deltaClass = 'mono';
          if (delta != null) {
            if (delta < 0) deltaClass += ' delta-cell-good';
            else if (delta > 0) deltaClass += ' delta-cell-bad';
          }
          const champPass = champ && (
            champ.pass_fail === true || champ.pass_fail === 1
            || String(champ.pass_fail).toLowerCase() === 'pass');
          const chalPass = chal && (
            chal.pass_fail === true || chal.pass_fail === 1
            || String(chal.pass_fail).toLowerCase() === 'pass');
          let flipNode;
          if (champ && chal) {
            if (champPass && !chalPass) flipNode = el('span', { class: 'mono flip-bad' }, ['pass → fail']);
            else if (!champPass && chalPass) flipNode = el('span', { class: 'mono flip-good' }, ['fail → pass']);
            else if (champPass && chalPass) flipNode = el('span', { class: 'mono', style: 'color:var(--color-text-muted)' }, ['both pass']);
            else flipNode = el('span', { class: 'mono', style: 'color:var(--color-text-muted)' }, ['both fail']);
          } else {
            flipNode = el('span', { class: 'mono' }, ['—']);
          }
          tbody.appendChild(el('tr', null, [
            el('td', { class: 'mono' }, [eid]),
            el('td', { class: 'mono' }, [_fmtNum(c)]),
            el('td', { class: 'mono' }, [_fmtNum(ch)]),
            el('td', { class: deltaClass }, [_fmtNum(delta)]),
            el('td', null, [flipNode]),
          ]));
        }
        tbl.appendChild(tbody);
        wrap.appendChild(tbl);
        body = wrap;
      }
    }
  }
  node.appendChild(renderCard({
    title: 'Per-entry comparison',
    body,
  }));
}

function _renderJudges(epochId, champId, chalId) {
  const node = $('phase0-round-judges');
  if (!node) return;
  clearChildren(node);
  let body;
  if (!epochId || !champId || !chalId) {
    body = el('p', { class: 'empty' }, ['No matchup selected.']);
  } else {
    const data = _judgeCmpCache.get(epochId + '/' + champId + '->' + chalId);
    if (!data) {
      body = renderLoadingState({ label: 'Loading per-judge comparison' });
    } else {
      const judges = Array.isArray(data.judges) ? data.judges : [];
      if (judges.length === 0) {
        const msg = data.note ? '(no per-judge data: ' + data.note + ')'
          : '(no per-judge data recorded for either side)';
        body = renderEmptyState(msg);
      } else {
        const wrap = el('div');
        if (data.primary_driver) {
          // The Phase 1 test asserts the text "primary driver" appears.
          wrap.appendChild(el('p', {
            style: 'font-size:var(--font-size-13); margin:0 0 var(--space-3); color:var(--color-text-primary);',
          }, [
            el('strong', null, ['primary driver: ']),
            el('span', { class: 'mono' }, [data.primary_driver]),
          ]));
        }
        const tbl = el('table', { class: 'ds-table' });
        tbl.appendChild(el('thead', null, [el('tr', null, [
          el('th', null, ['judge']),
          el('th', null, ['champion']),
          el('th', null, ['challenger']),
          el('th', null, ['Δ']),
        ])]));
        const tbody = el('tbody');
        for (const j of judges) {
          const delta = (typeof j.delta === 'number' && isFinite(j.delta)) ? j.delta : null;
          let dCls = 'mono';
          if (delta != null) {
            if (delta < 0) dCls += ' delta-cell-good';
            else if (delta > 0) dCls += ' delta-cell-bad';
          }
          const isDriver = j.judge_name === data.primary_driver;
          const nameContent = isDriver
            ? [el('strong', null, [j.judge_name || '—'])]
            : [j.judge_name || '—'];
          tbody.appendChild(el('tr', null, [
            el('td', { class: 'mono' }, nameContent),
            el('td', { class: 'mono' }, [_fmtNum(j.champion_weighted_loss)]),
            el('td', { class: 'mono' }, [_fmtNum(j.challenger_weighted_loss)]),
            el('td', { class: dCls }, [_fmtNum(delta)]),
          ]));
        }
        tbl.appendChild(tbody);
        wrap.appendChild(tbl);
        body = wrap;
      }
    }
  }
  node.appendChild(renderCard({
    title: 'Per-judge comparison',
    body,
  }));
}

function _renderDecision(matchup) {
  const node = $('phase0-round-decision');
  if (!node) return;
  clearChildren(node);
  // The bracket lives in state.bracket; until it lands we say "Loading",
  // not "No decision yet." — the latter implies the bracket exists and
  // has no entry, which is not the loading case.
  if (state.bracket == null) {
    node.appendChild(renderCard({
      title: 'Decision',
      body: renderLoadingState({ label: 'Loading decision' }),
    }));
    return;
  }
  if (!matchup) {
    node.appendChild(renderCard({
      title: 'Decision',
      body: renderEmptyState('No decision yet.'),
    }));
    return;
  }
  const decision = (matchup.decision || '—').toString().toLowerCase();
  let pillVariant = 'neutral';
  if (decision === 'promoted') pillVariant = 'promoted';
  else if (decision === 'rejected') pillVariant = 'rejected';
  else if (decision === 'deferred') pillVariant = 'deferred';
  const dsv = (typeof matchup.delta_scalar === 'number' && isFinite(matchup.delta_scalar))
    ? matchup.delta_scalar.toFixed(3) : '—';
  const driver = matchup.driver || matchup.rejection_reason || '';
  const callAccent = decision === 'promoted' ? 'success'
    : (decision === 'rejected' ? 'error' : 'warning');
  const callBody = el('div', null, [
    el('div', { style: 'display:flex; gap:var(--space-3); align-items:center; flex-wrap:wrap;' }, [
      renderPill(decision, pillVariant),
      el('span', { class: 'mono', style: 'font-size:var(--font-size-13);' },
        ['Δ scalar · ', dsv]),
    ]),
    driver ? el('p', {
      style: 'margin:var(--space-3) 0 0; font-size:var(--font-size-13); color:var(--color-text-secondary);',
    }, [String(driver)]) : null,
  ]);
  node.appendChild(renderCalloutCard({
    title: 'Decision',
    accent: callAccent,
    body: callBody,
  }));
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
  _renderVs(params, matchup);
  _renderEntries(epochId, champId, chalId);
  _renderJudges(epochId, champId, chalId);
  _renderDecision(matchup);
}

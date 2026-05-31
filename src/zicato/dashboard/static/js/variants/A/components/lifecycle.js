// variants/A/components/lifecycle.js — Theme 1: candidate lifecycle.
//
// Two builders, both in the Mission Control idiom:
//
//   missionTrack(...)  — one candidate's life as a horizontal telemetry
//     timeline: BORN (patch armed off a parent) → SORTIE (board entries
//     firing) → GATE (GO / NO-GO) → OUTCOME (crowned champion, or aborted
//     dead branch). Each station carries a status light; the rail between
//     stations lights up to the reached stage.
//
//   commandRoster(...) — the lineage as a command board: the crowned
//     champion defending at the top, challengers as call-signs below,
//     dead branches dimmed. Reads as a roster, not a family tree form.
//
// Pure builders: (data) -> DOM node. No fetch, no module state.

import { el } from '../../../core/dom.js';

// -- mission track ----------------------------------------------------
// stations: ordered [{ key, label, sub, light }] where light is one of
//   'go' | 'stop' | 'live' | 'warn' | 'idle' | 'done'.
// reached: index of the furthest reached station (rail lights up to it).
export function missionTrack(stations, reached) {
  stations = Array.isArray(stations) ? stations : [];
  const n = stations.length;
  const reachIdx = typeof reached === 'number' ? reached : n - 1;

  const track = el('div', { class: 'mcA-track', role: 'list', 'aria-label': 'candidate lifecycle' });
  stations.forEach((s, i) => {
    if (i > 0) {
      const lit = i <= reachIdx;
      track.appendChild(el('div', {
        class: 'mcA-track-rail' + (lit ? ' is-lit' : ''),
        'data-light': lit ? (s.light || 'idle') : 'idle',
        'aria-hidden': 'true',
      }));
    }
    const light = s.light || 'idle';
    const station = el('div', {
      class: 'mcA-track-station',
      'data-light': light,
      role: 'listitem',
    }, [
      el('div', { class: 'mcA-track-dot', 'data-light': light }, [stationGlyph(light)]),
      el('div', { class: 'mcA-track-label' }, [s.label || s.key || '']),
      s.sub ? el('div', { class: 'mcA-track-sub mono' }, [s.sub]) : null,
    ]);
    track.appendChild(station);
  });
  return track;
}

function stationGlyph(light) {
  if (light === 'go') return '▲';
  if (light === 'stop') return '✗';
  if (light === 'live') return '◉';
  if (light === 'warn') return '!';
  if (light === 'done') return '✓';
  return '·';
}

// Build the four canonical lifecycle stations for one candidate from
// what we know: it always was BORN; SORTIE fired if it has per-entry
// results; GATE resolved to the decision; OUTCOME crowns or aborts.
//   info: {
//     parentId, genId, isSeed,
//     sortieFired (bool), entryCount,
//     decision ('promoted'|'rejected'|null),
//     live (bool),
//   }
export function lifecycleStations(info) {
  info = info || {};
  const dec = info.decision || null;
  const born = {
    key: 'born', label: info.isSeed ? 'Seeded' : 'Born',
    sub: info.isSeed ? 'baseline v0' : (info.parentId ? 'patch off ' + info.parentId : 'patch armed'),
    light: 'done',
  };
  const sortie = {
    key: 'sortie', label: 'Board sortie',
    sub: info.entryCount != null ? info.entryCount + ' entries' : (info.sortieFired ? 'fired' : 'queued'),
    light: info.sortieFired ? 'done' : (info.live ? 'live' : 'idle'),
  };
  const gate = {
    key: 'gate', label: 'Promote gate',
    sub: (info.isSeed || dec === 'promoted') ? 'GO' : dec === 'rejected' ? 'NO-GO' : (info.live ? 'pending' : '—'),
    light: (info.isSeed || dec === 'promoted') ? 'go' : dec === 'rejected' ? 'stop' : (info.live ? 'live' : 'idle'),
  };
  let outcome;
  if (info.isSeed) {
    outcome = { key: 'outcome', label: 'Crowned', sub: 'reigning champion', light: 'go' };
  } else if (dec === 'promoted') {
    outcome = { key: 'outcome', label: 'Crowned', sub: 'new champion', light: 'go' };
  } else if (dec === 'rejected') {
    outcome = { key: 'outcome', label: 'Aborted', sub: 'dead branch', light: 'stop' };
  } else {
    outcome = { key: 'outcome', label: 'Outcome', sub: info.live ? 'in flight' : 'pending', light: info.live ? 'live' : 'idle' };
  }
  // furthest reached station
  let reached = 0; // born
  if (info.sortieFired || info.live) reached = 1;
  if (dec || info.isSeed) reached = 3;
  else if (info.sortieFired) reached = 2;
  return { stations: [born, sortie, gate, outcome], reached };
}

// -- command roster ---------------------------------------------------
// Lineage as a defended command board. The champion crowned at top;
// challengers as call-signs; dead branches dimmed.
//   rows: [{ id, role ('champion'|'challenger'), decision, parentId,
//            delta, live, onSelect }]
// champion is rendered with a crown banner; the rest as roster rows.
export function commandRoster({ champion, challengers, onSelect }) {
  champion = champion || null;
  challengers = Array.isArray(challengers) ? challengers : [];

  const wrap = el('div', { class: 'mcA-roster' });

  // crowned champion banner
  if (champion) {
    const banner = el('div', {
      class: 'mcA-roster-crown' + (onSelect ? ' is-clickable' : ''),
      role: onSelect ? 'button' : null,
      tabindex: onSelect ? '0' : null,
    }, [
      el('div', { class: 'mcA-roster-crown-mark' }, ['♚']),
      el('div', { class: 'mcA-roster-crown-body' }, [
        el('div', { class: 'mcA-roster-crown-label' }, ['Reigning champion · defending']),
        el('div', { class: 'mcA-roster-crown-id mono' }, [champion.id || '?']),
      ]),
      el('div', { class: 'mcA-roster-crown-light', 'data-light': 'go' }),
    ]);
    if (onSelect && champion.id) {
      banner.addEventListener('click', () => onSelect(champion.id));
      banner.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') onSelect(champion.id); });
    }
    wrap.appendChild(banner);
  }

  // challenger call-signs
  if (!challengers.length) {
    wrap.appendChild(el('div', { class: 'mcA-roster-empty' }, ['No challengers have mounted the hill yet.']));
    return wrap;
  }
  const list = el('div', { class: 'mcA-roster-list' });
  challengers.forEach((c) => {
    const dead = c.decision === 'rejected';
    const live = !!c.live;
    const light = c.decision === 'promoted' ? 'go' : dead ? 'stop' : live ? 'live' : 'idle';
    const row = el('div', {
      class: 'mcA-roster-row' + (dead ? ' is-dead' : '') + (live ? ' is-live' : '') + (onSelect ? ' is-clickable' : ''),
      role: onSelect ? 'button' : null,
      tabindex: onSelect ? '0' : null,
    }, [
      el('span', { class: 'mcA-roster-light', 'data-light': light }),
      el('span', { class: 'mcA-roster-callsign mono' }, [c.id || '?']),
      el('span', { class: 'mcA-roster-lineage mono' }, ['↳ ' + (c.parentId || '?')]),
      el('span', { class: 'mcA-roster-verdict' }, [
        c.decision === 'promoted' ? 'crowned'
          : dead ? 'dead branch'
          : live ? 'in flight'
          : 'pending',
      ]),
      el('span', {
        class: 'mcA-roster-delta mono ' + (typeof c.delta === 'number' && c.delta < 0 ? 'mcA-tag-good' : (c.delta > 0 ? 'mcA-tag-bad' : '')),
      }, [typeof c.delta === 'number' && isFinite(c.delta) ? (c.delta > 0 ? '+' : '') + c.delta.toFixed(2) : '—']),
    ]);
    if (onSelect && c.id) {
      row.addEventListener('click', () => onSelect(c.id));
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') onSelect(c.id); });
    }
    list.appendChild(row);
  });
  wrap.appendChild(list);
  return wrap;
}

// A small status-light legend for the lifecycle stations.
export function trackLegend() {
  const item = (light, label) => el('span', { class: 'mcA-track-legend-item' }, [
    el('span', { class: 'mcA-track-legend-dot', 'data-light': light }),
    el('span', null, [label]),
  ]);
  return el('div', { class: 'mcA-track-legend mono' }, [
    item('done', 'reached'),
    item('go', 'go / crowned'),
    item('stop', 'no-go / aborted'),
    item('live', 'in flight'),
    item('idle', 'not reached'),
  ]);
}

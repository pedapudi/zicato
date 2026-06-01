// variants/V/reel.js — the REEL: the epoch as a horizontal timeline / playback.
//
// V's hero AND its primary navigation. The rounds of the epoch are laid on a
// time axis (ordered by `ran_at` from /api/tournaments; lineage from
// /api/lineage): the CHAMPION SPINE runs along the top, and challengers ENTER
// over time, each round a STATION carrying its verdict (promote / reject) and
// Δscalar. A SCRUBBER / stepper moves along the rounds; selecting a station
// drives the detail pane to that round's challenger (its match-up + promote
// gate + lifecycle, via the candidate view).
//
// Render discipline (the brief): the reel is a single STATIC, fit-to-width SVG
// — NO pan/zoom viewport (viewBox + width:100%, laid out to the container). It
// is digest-gated on STRUCTURE ONLY (round ids / verdicts / Δ / selection —
// NOT timestamps-as-noise). The scrub is a STATE CHANGE (a `data-sel` class
// swap + a CSS `transition`), never a re-fired infinite keyframe.
//
// Pure builder: (spec) -> detached <div>. Marks use `vr-*`, styled (scoped
// under the variant root) by css/variants/V/reel.css.

import { el, svgEl } from '../../core/dom.js';
import { isNum, fmt, fmtSigned } from './svg.js';

// A stable structural digest of the reel — round ids + verdicts + Δ + selection
// (NOT ran_at). Identical structure → identical digest → a true repaint no-op.
export function reelDigest(spec) {
  const o = spec || {};
  return JSON.stringify({
    champ: o.championId || '',
    sel: o.selected || '',
    rounds: (o.rounds || []).map((r) => [
      r.challenger, r.decision || '',
      isNum(r.deltaScalar) ? r.deltaScalar.toFixed(2) : null,
    ]),
  });
}

// `spec`:
//   championId — the reigning champion (the spine).
//   rounds:[{ challenger, decision, deltaScalar, hypothesis, scalar }]  (time-ordered)
//   selected   — the currently-scrubbed round (a challenger id), or null.
//   onSelect(challengerId) — drive the detail pane to that round.
//   onSeed(championId)     — open the seed/champion candidate (station 0).
export function reel(spec) {
  const o = spec || {};
  const rounds = Array.isArray(o.rounds) ? o.rounds : [];
  const championId = o.championId || null;
  const selected = o.selected || null;

  const wrap = el('div', { class: 'vr-reel', role: 'group', 'aria-label': 'Epoch reel — rounds on a time axis' });

  if (!rounds.length) {
    wrap.appendChild(el('p', { class: 'vr-reel-empty dn-empty', text: 'No rounds have run in this epoch yet — the reel fills as challengers enter.' }));
    return wrap;
  }

  // ── the fit-to-width SVG film strip ────────────────────────────────
  // A FIXED viewBox laid out left→right; `width:100%` (in CSS) scales the whole
  // strip to the container — no horizontal scroll, no pan/zoom. One station per
  // round PLUS a leading champion/seed station.
  const stationCount = rounds.length + 1;             // +1 for the seed/champion
  const VBW = 120 + stationCount * 150;               // virtual width
  const VBH = 220;
  const spineY = 96;                                  // the champion spine (pushed down to clear the caption + seed labels above it)
  const stationY = 150;                               // the challenger stations
  const x0 = 70;
  const step = (VBW - x0 - 60) / Math.max(1, stationCount - 1);
  const xAt = (i) => x0 + i * step;

  const svg = svgEl('svg', {
    class: 'vr-strip', viewBox: `0 0 ${VBW} ${VBH}`,
    preserveAspectRatio: 'xMinYMid meet', role: 'img',
    'aria-label': `Reel of ${rounds.length} round${rounds.length === 1 ? '' : 's'} on the time axis`,
  });

  // the time axis baseline
  svg.appendChild(svgEl('line', { x1: x0, y1: spineY, x2: xAt(stationCount - 1), y2: spineY, class: 'vr-spine' }));
  svg.appendChild(svgEl('text', { x: x0, y: 22, class: 'vr-axis-lab' }, ['champion spine →']));
  svg.appendChild(svgEl('text', { x: x0, y: VBH - 12, class: 'vr-axis-lab vr-axis-lab-time' }, ['time (round order) →']));

  // ── station 0: the seed / reigning champion ────────────────────────
  const seedX = xAt(0);
  svg.appendChild(svgEl('circle', { cx: seedX, cy: spineY, r: 12, class: 'vr-champ-disc' }));
  svg.appendChild(svgEl('text', { x: seedX, y: spineY + 4, class: 'vr-champ-glyph', 'text-anchor': 'middle' }, ['♛']));
  const seedG = svgEl('g', {
    class: 'vr-station vr-station-seed' + (selected && selected === championId ? ' vr-sel' : ''),
    tabindex: o.onSeed ? '0' : null, role: o.onSeed ? 'button' : null,
    'aria-label': `Champion ${championId || 'seed'}`,
  }, [
    svgEl('text', { x: seedX, y: spineY - 44, class: 'vr-champ-id', 'text-anchor': 'middle' }, [championId || 'seed']),
    svgEl('text', { x: seedX, y: spineY - 28, class: 'vr-champ-tag', 'text-anchor': 'middle' }, ['reigning']),
  ]);
  if (o.onSeed) {
    seedG.style.cursor = 'pointer';
    seedG.addEventListener('click', () => o.onSeed(championId));
    seedG.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onSeed(championId); } });
  }
  svg.appendChild(seedG);

  // ── one station per round (a challenger entering over time) ─────────
  rounds.forEach((r, i) => {
    const idx = i + 1;
    const sx = xAt(idx);
    const promoted = String(r.decision || '').toLowerCase().includes('promot');
    const cls = promoted ? 'vr-promote' : 'vr-reject';
    const isSel = selected && selected === r.challenger;

    // the connector from the spine down to the challenger station (entering)
    svg.appendChild(svgEl('path', {
      d: `M ${sx} ${spineY} C ${sx} ${(spineY + stationY) / 2}, ${sx} ${(spineY + stationY) / 2}, ${sx} ${stationY - 16}`,
      class: 'vr-enter ' + cls, fill: 'none',
    }));
    // a tick on the spine marking this round's position in time
    svg.appendChild(svgEl('circle', { cx: sx, cy: spineY, r: 4, class: 'vr-tick ' + cls }));

    const g = svgEl('g', {
      class: 'vr-station ' + cls + (isSel ? ' vr-sel' : ''),
      'data-challenger': r.challenger, tabindex: '0', role: 'button',
      'aria-label': `Round ${idx}: ${championId || 'champion'} vs ${r.challenger} — ${r.decision || 'pending'}`
        + (isNum(r.deltaScalar) ? `, Δ ${fmtSigned(r.deltaScalar, 2)}` : ''),
    });
    // the station card: a rounded rect with the challenger id + verdict + Δ
    const cw = 116; const ch = 50; const cy = stationY;
    g.appendChild(svgEl('rect', { x: sx - cw / 2, y: cy - 6, width: cw, height: ch, rx: 7, class: 'vr-card' }));
    g.appendChild(svgEl('text', { x: sx, y: cy + 12, class: 'vr-card-id', 'text-anchor': 'middle' }, [r.challenger]));
    g.appendChild(svgEl('text', { x: sx, y: cy + 28, class: 'vr-card-verdict', 'text-anchor': 'middle' }, [promoted ? 'promoted ♛' : 'rejected ✕']));
    g.appendChild(svgEl('text', {
      x: sx, y: cy + 41, class: 'vr-card-delta', 'text-anchor': 'middle',
    }, [isNum(r.deltaScalar) ? 'Δ ' + fmtSigned(r.deltaScalar, 1) : 'Δ —']));
    // round ordinal above the spine tick
    g.appendChild(svgEl('text', { x: sx, y: spineY - 14, class: 'vr-round-no', 'text-anchor': 'middle' }, ['r' + idx]));
    g.appendChild(svgEl('title', null, [`Round ${idx}: ${championId} vs ${r.challenger} — ${r.decision || 'pending'}`]));

    g.style.cursor = 'pointer';
    if (o.onSelect) {
      g.addEventListener('click', () => o.onSelect(r.challenger));
      g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onSelect(r.challenger); } });
    }
    svg.appendChild(g);
  });

  wrap.appendChild(el('div', { class: 'vr-strip-frame' }, [svg]));

  // ── the SCRUBBER / stepper — a linear control under the strip ───────
  wrap.appendChild(scrubber({ championId, rounds, selected, onSelect: o.onSelect, onSeed: o.onSeed }));

  return wrap;
}

// The scrubber: prev / next steppers + a row of round pips. Selecting a pip (or
// stepping) moves along the rounds — a pure state change driving onSelect.
function scrubber(o) {
  const rounds = o.rounds || [];
  // the ordered list of "stations": [seed, ...challengers]
  const stations = [{ id: o.championId, seed: true }, ...rounds.map((r) => ({ id: r.challenger, decision: r.decision, deltaScalar: r.deltaScalar }))];
  const selIdx = Math.max(0, stations.findIndex((s) => s.id === o.selected));
  const curIdx = o.selected ? (selIdx < 0 ? 0 : selIdx) : -1;

  const go = (idx) => {
    if (idx < 0 || idx >= stations.length) return;
    const s = stations[idx];
    if (s.seed) { if (o.onSeed) o.onSeed(s.id); }
    else if (o.onSelect) o.onSelect(s.id);
  };

  const prev = el('button', {
    class: 'vr-step vr-step-prev', type: 'button', title: 'previous round', text: '◀',
    disabled: curIdx <= 0 ? '' : null,
  });
  prev.addEventListener('click', () => go(curIdx <= 0 ? 0 : curIdx - 1));
  const next = el('button', {
    class: 'vr-step vr-step-next', type: 'button', title: 'next round', text: '▶',
    disabled: curIdx >= stations.length - 1 ? '' : null,
  });
  next.addEventListener('click', () => go(curIdx < 0 ? 0 : Math.min(stations.length - 1, curIdx + 1)));

  const pipRow = el('div', { class: 'vr-pips', role: 'tablist', 'aria-label': 'round scrubber' });
  stations.forEach((s, i) => {
    const promoted = String(s.decision || '').toLowerCase().includes('promot');
    const cls = s.seed ? 'vr-pip-seed' : (promoted ? 'vr-pip-promote' : 'vr-pip-reject');
    const isSel = curIdx === i;
    const pip = el('button', {
      class: 'vr-pip ' + cls + (isSel ? ' vr-pip-on' : ''), type: 'button',
      role: 'tab', 'aria-selected': String(isSel),
      title: s.seed ? `champion ${s.id}` : `round ${i}: ${s.id} (${s.decision || 'pending'})`,
    }, [
      el('span', { class: 'vr-pip-dot', 'aria-hidden': 'true' }),
      el('span', { class: 'vr-pip-lab', text: s.seed ? (s.id || 'seed') : s.id }),
    ]);
    pip.addEventListener('click', () => go(i));
    pipRow.appendChild(pip);
  });

  const readout = el('span', { class: 'vr-scrub-readout dn-faint', text:
    curIdx < 0 ? 'scrub the rounds — select a station' : (curIdx === 0 ? 'champion' : 'round ' + curIdx + ' of ' + rounds.length) });

  return el('div', { class: 'vr-scrubber' }, [
    el('div', { class: 'vr-scrub-controls' }, [prev, pipRow, next]),
    readout,
  ]);
}

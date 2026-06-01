// variants/T/reel.js — the SLIM REEL: the epoch's rounds as a compact spine.
//
// Adopted from Variant V's reel (the round-6 "Reel" dashboard) but DELIBERATELY
// trimmed to a slim, FIT-TO-WIDTH "rounds" spine for Console IV's epoch view:
// the champion spine runs left→right, the seed/champion sits at station 0, and
// each round is a small TICK on the spine carrying its ordinal (r1…rN) and a
// verdict-coloured dot — NO big challenger cards hang off it (those do not
// scale; the per-challenger detail lives in the generations match cards). A
// click on a tick (or the seed) drives the detail pane to that round's
// candidate.
//
// Render discipline (the T brief): a single STATIC, fit-to-width SVG — NO
// pan/zoom (a fixed viewBox + width:100% in CSS). With many rounds the ticks
// COMPRESS along the fixed-width spine — there is no horizontal scroll and no
// element ever exceeds the viewBox width. The selected / hovered tick
// highlights via a CSS state class swap, never an infinite keyframe.
//
// Self-contained: ported INTO Variant T (no import from other variant dirs).
// Marks use `tr-*`, styled (scoped under the variant root) by console4.css.

import { el, svgEl } from '../../core/dom.js';
import { isNum, fmtSigned } from './svg.js';

// A stable structural digest — round ids + verdicts + Δ + selection (NOT
// ran_at). Identical structure → identical digest → a true repaint no-op.
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
//   championId — the reigning champion (the spine's seed node, station 0).
//   rounds:[{ challenger, decision, deltaScalar }]  (round-ordered)
//   selected   — the currently-selected round (a challenger id), or null.
//   onSelect(challengerId) — drive the detail pane to that round.
//   onSeed(championId)     — open the seed/champion candidate (station 0).
//
// The reel is fit-to-width: a FIXED viewBox (no element exceeds VBW) laid out
// left→right; CSS sets width:100% so the whole spine scales to the container.
// As `rounds` grows, `step` shrinks so the ticks compress — never overflow.
export function reel(spec) {
  const o = spec || {};
  const rounds = Array.isArray(o.rounds) ? o.rounds : [];
  const championId = o.championId || null;
  const selected = o.selected || null;

  const wrap = el('div', { class: 'tr-reel', role: 'group', 'aria-label': 'Epoch reel — rounds along the champion spine' });

  if (!rounds.length) {
    wrap.appendChild(el('p', { class: 'tr-reel-empty dn-empty', text: 'No rounds have run in this epoch yet — the reel fills as challengers enter.' }));
    return wrap;
  }

  // ── the fit-to-width SVG spine ─────────────────────────────────────
  // A FIXED viewBox; the spine occupies a constant fraction of it regardless of
  // round count. Stations are evenly distributed between x0 and xMax, so more
  // rounds ⇒ smaller `step` ⇒ ticks compress (they never run past xMax / VBW).
  const stationCount = rounds.length + 1;             // +1 for the seed/champion
  const VBW = 1000;                                   // fixed virtual width (fit-to-width)
  const VBH = 92;
  const spineY = 56;                                  // the champion spine
  const x0 = 60;
  const xMax = VBW - 48;
  const step = (xMax - x0) / Math.max(1, stationCount - 1);
  const xAt = (i) => x0 + i * step;
  // tick radius shrinks a touch when stations crowd, so they never collide.
  const tickR = Math.max(2.4, Math.min(5, step / 5));

  const svg = svgEl('svg', {
    class: 'tr-strip', viewBox: `0 0 ${VBW} ${VBH}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': `Reel of ${rounds.length} round${rounds.length === 1 ? '' : 's'} along the champion spine`,
  });

  // the champion spine baseline + a faint caption above its left end
  svg.appendChild(svgEl('line', { x1: x0, y1: spineY, x2: xAt(stationCount - 1), y2: spineY, class: 'tr-spine' }));
  svg.appendChild(svgEl('text', { x: x0, y: 18, class: 'tr-axis-lab' }, ['champion spine · rounds →']));

  // ── station 0: the seed / reigning champion ────────────────────────
  const seedX = xAt(0);
  const seedSel = selected && selected === championId;
  const seedG = svgEl('g', {
    class: 'tr-station tr-station-seed' + (seedSel ? ' tr-sel' : ''),
    tabindex: o.onSeed ? '0' : null, role: o.onSeed ? 'button' : null,
    'data-station': championId || 'seed',
    'aria-label': `Champion ${championId || 'seed'}`,
  }, [
    svgEl('circle', { cx: seedX, cy: spineY, r: 8, class: 'tr-champ-disc' }),
    svgEl('text', { x: seedX, y: spineY + 3.5, class: 'tr-champ-glyph', 'text-anchor': 'middle' }, ['♛']),
    svgEl('text', { x: seedX, y: spineY - 16, class: 'tr-champ-id', 'text-anchor': 'middle' }, [championId || 'seed']),
    svgEl('text', { x: seedX, y: spineY + 26, class: 'tr-tick-ord', 'text-anchor': 'middle' }, ['r0']),
    svgEl('title', null, [`Champion ${championId || 'seed'}`]),
  ]);
  if (o.onSeed) {
    seedG.style.cursor = 'pointer';
    seedG.addEventListener('click', () => o.onSeed(championId));
    seedG.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onSeed(championId); } });
  }
  svg.appendChild(seedG);

  // ── one TICK per round (a challenger entering over the rounds) ──────
  rounds.forEach((r, i) => {
    const idx = i + 1;
    const sx = xAt(idx);
    const promoted = String(r.decision || '').toLowerCase().includes('promot');
    const cls = promoted ? 'tr-promote' : 'tr-reject';
    const isSel = selected && selected === r.challenger;

    const g = svgEl('g', {
      class: 'tr-station ' + cls + (isSel ? ' tr-sel' : ''),
      'data-station': r.challenger, 'data-challenger': r.challenger,
      tabindex: '0', role: 'button',
      'aria-label': `Round ${idx}: ${championId || 'champion'} vs ${r.challenger} — ${r.decision || 'pending'}`
        + (isNum(r.deltaScalar) ? `, Δ ${fmtSigned(r.deltaScalar, 2)}` : ''),
    });
    // the round ordinal above the tick, the challenger id below it
    g.appendChild(svgEl('circle', { cx: sx, cy: spineY, r: tickR, class: 'tr-tick ' + cls }));
    g.appendChild(svgEl('text', { x: sx, y: spineY - 12, class: 'tr-tick-ord', 'text-anchor': 'middle' }, ['r' + idx]));
    g.appendChild(svgEl('text', { x: sx, y: spineY + 22, class: 'tr-tick-id', 'text-anchor': 'middle' }, [r.challenger]));
    g.appendChild(svgEl('title', null, [`Round ${idx}: ${championId} vs ${r.challenger} — ${r.decision || 'pending'}`
      + (isNum(r.deltaScalar) ? ` (Δ ${fmtSigned(r.deltaScalar, 2)})` : '')]));

    g.style.cursor = 'pointer';
    if (o.onSelect) {
      g.addEventListener('click', () => o.onSelect(r.challenger));
      g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onSelect(r.challenger); } });
    }
    svg.appendChild(g);
  });

  wrap.appendChild(el('div', { class: 'tr-strip-frame' }, [svg]));
  wrap.appendChild(el('p', { class: 'dn-faint tr-reel-foot', text:
    'champion at r0 · each tick is a round (challenger vs champion) · click a station → its candidate · per-round detail lives in the generations match cards' }));
  return wrap;
}

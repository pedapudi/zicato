// v2/components/boardCell.js — ONE small-multiple cell of the Bench.
//
// DASHBOARD-V2 §3 + §4.1: the Bench is SMALL MULTIPLES — a grid of one
// tiny VISUAL per board entry, NOT a table. This factory builds a single
// cell: a compact, graphical champion-vs-challenger comparison that the
// eye reads at a glance across the whole grid.
//
// What one cell shows:
//   - a terse entry-id label + a pass/fail verdict glyph,
//   - a champion-vs-challenger PAIRED COMPARISON drawn as two mini
//     horizontal bars (drift loss, normalized to a shared cell max),
//     color-coded green (challenger better) / red (challenger worse)
//     from the --v2-* signal tokens — always paired with a glyph,
//   - a live PROGRESS RING (SVG arc) for any in-flight side, sweeping to
//     its % of the wall-clock budget (NOT task completion — honest),
//   - the four honest states, color ALWAYS redundant to a glyph:
//       queued  — faint / empty bars, ⋯ glyph,
//       running — an animated progress ring + ▶ glyph,
//       done    — filled bars + the loss number + ✓/✗ verdict,
//       aborted — a red flag (⚑) + 'aborted' word.
//
// Re-render discipline (the no-flash contract, like liveMatrix): the
// cell builds its DOM ONCE and exposes `update(data)` which PATCHES the
// bar widths, ring sweep, glyphs and text in place via the core/dom.js
// patch* helpers + raw setAttribute on the persistent SVG nodes. An SSE
// burst therefore repaints just the moving ring/bar, never the cell.
//
// Color is redundant to a glyph + a state/verdict word in every cell, so
// the grid reads identically without color (a11y / grayscale-safe).

import { el, svgEl, patchText, patchClass, patchAttr } from '../../core/dom.js';

// The four honest side-states. A side `data` MUST carry one of these as
// `state`; anything unknown is coerced to 'queued'.
export const CELL_STATES = ['queued', 'running', 'done', 'aborted'];

// Per-state glyph (redundant to color, grayscale-safe, mono face).
const STATE_GLYPH = { queued: '⋯', running: '▶', done: '✓', aborted: '⚑' };
const SIDE_LABEL = { champion: 'champ', challenger: 'chal' };

// Ring geometry — a fixed-radius SVG circle whose stroke-dasharray is
// driven by progress. r is chosen so circumference is a round-ish number;
// the value is irrelevant since we drive offset by fraction.
const RING_R = 13;
const RING_C = 2 * Math.PI * RING_R; // circumference

function _normState(s) { return CELL_STATES.indexOf(s) >= 0 ? s : 'queued'; }

function _clampFrac(p) {
  if (typeof p !== 'number' || !isFinite(p)) return 0;
  // Accept a 0..1 fraction OR a 0..100 percent; normalize to 0..1.
  const f = p > 1 ? p / 100 : p;
  return Math.max(0, Math.min(1, f));
}

function _fmtLoss(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  return v.toFixed(3);
}

// Diverging sentiment of the challenger vs the champion on drift loss
// (lower is better). Returns improve | regress | flat — drives the bar
// color and the comparison glyph.
function _sentiment(champLoss, chalLoss) {
  if (typeof champLoss !== 'number' || typeof chalLoss !== 'number'
      || !isFinite(champLoss) || !isFinite(chalLoss) || champLoss === chalLoss) {
    return 'flat';
  }
  return chalLoss < champLoss ? 'improve' : 'regress';
}

/**
 * Build one Bench small-multiple cell.
 *
 * opts:
 *   entryId   — the board-entry id (terse label). Required.
 *   onDrill   — () => void; click / Enter / Space drills into the run.
 *   champion  — initial side data (see `update`). Optional.
 *   challenger— initial side data (see `update`). Optional.
 *
 * Returns { node, update }:
 *   node          — the detached cell element.
 *   update({champion, challenger}) — reconcile both sides in place. Each
 *     side is { state, loss?, pass?, progress?, note? } (see the file
 *     header for the per-state field meaning). Call with no args to keep
 *     the current data (a structural re-render).
 */
export function boardCell(opts) {
  const o = opts || {};
  const entryId = String(o.entryId == null ? '' : o.entryId);
  const onDrill = typeof o.onDrill === 'function' ? o.onDrill : null;

  let champion = o.champion || { state: 'queued' };
  let challenger = o.challenger || { state: 'queued' };

  // ---- frame (built once) -------------------------------------------
  const card = el('div', { class: 'v2-bc', 'data-entry': entryId });

  // header: terse id + verdict glyph
  const head = el('div', { class: 'v2-bc-head' }, [
    el('span', { class: 'v2-bc-id v2-num', title: entryId }, [entryId || '—']),
    el('span', { class: 'v2-bc-verdict', 'aria-hidden': 'true' }, ['']),
  ]);
  card.appendChild(head);
  const verdictEl = head.children[1];

  // The visual body: a progress ring (for any running side) on the left,
  // and the paired comparison bars on the right.
  const body = el('div', { class: 'v2-bc-body' });

  // -- progress ring (SVG arc) --
  // Two stacked arcs (champion + challenger), each shown only while its
  // side is running. The track is a faint full circle; the sweep arc is
  // the budget fraction. Re-render patches stroke-dashoffset in place.
  const ringWrap = el('div', { class: 'v2-bc-ring' });
  const ringSvg = svgEl('svg', {
    class: 'v2-bc-ring-svg', viewBox: '0 0 34 34',
    width: '34', height: '34', 'aria-hidden': 'true',
  });
  const ringTrack = svgEl('circle', {
    class: 'v2-bc-ring-track', cx: '17', cy: '17', r: String(RING_R),
  });
  // Champion sweep (outer) and challenger sweep (we render a single ring
  // that prefers the challenger when both run, else whichever runs). To
  // keep it legible we draw ONE sweep + a small side dot label.
  const ringSweep = svgEl('circle', {
    class: 'v2-bc-ring-sweep', cx: '17', cy: '17', r: String(RING_R),
    'stroke-dasharray': String(RING_C),
    'stroke-dashoffset': String(RING_C),
    transform: 'rotate(-90 17 17)',
  });
  ringSvg.appendChild(ringTrack);
  ringSvg.appendChild(ringSweep);
  // center text: the % (or the running side glyph fallback)
  const ringPct = el('span', { class: 'v2-bc-ring-pct v2-num' }, ['']);
  ringWrap.appendChild(ringSvg);
  ringWrap.appendChild(ringPct);
  body.appendChild(ringWrap);

  // -- paired comparison bars --
  const bars = el('div', { class: 'v2-bc-bars' });
  function buildBar(sideKey) {
    const fill = el('div', { class: 'v2-bc-bar-fill' });
    const track = el('div', { class: 'v2-bc-bar-track' }, [fill]);
    const val = el('span', { class: 'v2-bc-bar-val v2-num' }, ['']);
    const row = el('div', { class: 'v2-bc-bar', 'data-side': sideKey }, [
      el('span', { class: 'v2-bc-bar-glyph', 'aria-hidden': 'true' }, ['']),
      el('span', { class: 'v2-bc-bar-label' }, [SIDE_LABEL[sideKey] || sideKey]),
      track,
      val,
    ]);
    return { row, fill, val, glyph: row.children[0] };
  }
  const champBar = buildBar('champion');
  const chalBar = buildBar('challenger');
  bars.appendChild(champBar.row);
  bars.appendChild(chalBar.row);
  body.appendChild(bars);

  card.appendChild(body);

  // footer: the comparison sentiment as a glyph + word (color-redundant).
  const foot = el('div', { class: 'v2-bc-foot' }, [
    el('span', { class: 'v2-bc-cmp-glyph', 'aria-hidden': 'true' }, ['']),
    el('span', { class: 'v2-bc-cmp-text' }, ['']),
  ]);
  card.appendChild(foot);
  const cmpGlyph = foot.children[0];
  const cmpText = foot.children[1];

  // Drill affordance — the whole cell is a door (§5).
  if (onDrill) {
    card.classList.add('v2-bc-drillable');
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');
    card.setAttribute('aria-label', `${entryId} — open run`);
    const fire = (ev) => onDrill(ev);
    card.addEventListener('click', fire);
    card.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); fire(ev); }
    });
  }

  // ---- patch one side's bar in place --------------------------------
  // `maxLoss` is the shared normalizer across both sides of THIS cell so
  // the two bars are comparable; null when neither side has a loss yet.
  function patchBar(b, side, maxLoss) {
    const st = _normState(side.state);
    patchAttr(b.row, 'data-state', st);
    patchText(b.glyph, STATE_GLYPH[st]);
    if (st === 'done') {
      const loss = typeof side.loss === 'number' && isFinite(side.loss) ? side.loss : null;
      const frac = (loss != null && maxLoss != null && maxLoss > 0)
        ? Math.max(0.04, Math.min(1, loss / maxLoss)) : 0;
      b.fill.style.setProperty('width', `${(frac * 100).toFixed(1)}%`);
      patchClass(b.fill, 'v2-bc-bar-fill-on', loss != null);
      const verdict = side.pass === true ? ' ✓' : (side.pass === false ? ' ✗' : '');
      patchText(b.val, _fmtLoss(loss) + verdict);
    } else if (st === 'running') {
      b.fill.style.setProperty('width', '0%');
      patchClass(b.fill, 'v2-bc-bar-fill-on', false);
      patchText(b.val, 'running');
    } else if (st === 'aborted') {
      b.fill.style.setProperty('width', '0%');
      patchClass(b.fill, 'v2-bc-bar-fill-on', false);
      patchText(b.val, side.note ? String(side.note) : 'aborted');
    } else { // queued
      b.fill.style.setProperty('width', '0%');
      patchClass(b.fill, 'v2-bc-bar-fill-on', false);
      patchText(b.val, 'queued');
    }
  }

  // ---- patch the progress ring in place -----------------------------
  // Prefer the challenger's progress when it runs (it is the bet under
  // test); else the champion's. The ring is hidden when nothing runs.
  function patchRing() {
    const chalRunning = _normState(challenger.state) === 'running';
    const champRunning = _normState(champion.state) === 'running';
    const active = chalRunning ? challenger : (champRunning ? champion : null);
    const activeSide = chalRunning ? 'challenger' : (champRunning ? 'champion' : null);
    if (!active) {
      patchClass(ringWrap, 'v2-bc-ring-on', false);
      patchAttr(ringSweep, 'stroke-dashoffset', String(RING_C));
      patchText(ringPct, '');
      patchAttr(ringWrap, 'data-side', null);
      return;
    }
    patchClass(ringWrap, 'v2-bc-ring-on', true);
    patchAttr(ringWrap, 'data-side', activeSide);
    const frac = _clampFrac(active.progress);
    // dashoffset shrinks from full circumference (0%) to 0 (100%).
    const offset = RING_C * (1 - frac);
    patchAttr(ringSweep, 'stroke-dashoffset', offset.toFixed(2));
    patchText(ringPct, `${Math.round(frac * 100)}%`);
  }

  // ---- patch the header verdict + footer comparison -----------------
  function patchVerdict() {
    // overall cell verdict glyph: the challenger's pass/fail when done.
    const chSt = _normState(challenger.state);
    if (chSt === 'done' && challenger.pass === true) {
      patchText(verdictEl, '✓'); patchAttr(verdictEl, 'data-v', 'pass');
    } else if (chSt === 'done' && challenger.pass === false) {
      patchText(verdictEl, '✗'); patchAttr(verdictEl, 'data-v', 'fail');
    } else if (chSt === 'aborted') {
      patchText(verdictEl, '⚑'); patchAttr(verdictEl, 'data-v', 'aborted');
    } else if (chSt === 'running' || _normState(champion.state) === 'running') {
      patchText(verdictEl, '▶'); patchAttr(verdictEl, 'data-v', 'running');
    } else {
      patchText(verdictEl, '⋯'); patchAttr(verdictEl, 'data-v', 'queued');
    }
  }

  function patchComparison() {
    const champDone = _normState(champion.state) === 'done';
    const chalDone = _normState(challenger.state) === 'done';
    // A meaningful comparison needs both sides settled with a loss.
    const champLoss = champDone && typeof champion.loss === 'number' ? champion.loss : null;
    const chalLoss = chalDone && typeof challenger.loss === 'number' ? challenger.loss : null;
    let sentiment = 'flat';
    let text = '—';
    let glyph = '·';
    if (champLoss != null && chalLoss != null) {
      sentiment = _sentiment(champLoss, chalLoss);
      const d = chalLoss - champLoss; // negative = challenger improved
      const mag = Math.abs(d).toFixed(3);
      if (sentiment === 'improve') { glyph = '▼'; text = `−${mag} drift`; }
      else if (sentiment === 'regress') { glyph = '▲'; text = `+${mag} drift`; }
      else { glyph = '='; text = 'no Δ'; }
    } else if (_normState(challenger.state) === 'aborted' || _normState(champion.state) === 'aborted') {
      sentiment = 'regress'; glyph = '⚑'; text = 'aborted';
    } else {
      const anyRunning = _normState(challenger.state) === 'running'
        || _normState(champion.state) === 'running';
      text = anyRunning ? 'in flight' : 'pending';
      glyph = anyRunning ? '▶' : '·';
    }
    patchAttr(card, 'data-sentiment', sentiment);
    patchText(cmpGlyph, glyph);
    patchText(cmpText, text);
  }

  function update(next) {
    if (next) {
      if (next.champion) champion = next.champion;
      if (next.challenger) challenger = next.challenger;
    }
    // Shared loss normalizer across the two sides of this cell.
    const losses = [champion, challenger]
      .filter((s) => _normState(s.state) === 'done' && typeof s.loss === 'number' && isFinite(s.loss))
      .map((s) => s.loss);
    const maxLoss = losses.length ? Math.max(...losses) : null;

    patchBar(champBar, champion, maxLoss);
    patchBar(chalBar, challenger, maxLoss);
    patchRing();
    patchVerdict();
    patchComparison();
  }

  update(); // initial paint
  return { node: card, update };
}

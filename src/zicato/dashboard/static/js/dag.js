// js/dag.js — the compact candidate-lifecycle DAG (Console).
//
// Self-contained and SMALL: a single static SVG (NO pan/zoom) that
// reads one candidate's life left-to-right as a flow of cause → effect →
// verdict:
//
//   PARENT ─▶ PATCH ─▶ [ board fan: one node per entry ] ─▶ Σ ─▶ GATE ─▶ TERMINAL
//
// Pure builder: (spec) -> detached <svg>. Mark classes are `ezn-*` and are
// styled (scoped under the variant root) by css/console.css.

import { svgEl } from './core/dom.js';
import { isNum, fmt, CROWN } from './svg.js';
import { attachHovercard } from './hovercard.js';
import { truncate } from './ui.js';

export function verdictClass(verdict) {
  const v = String(verdict || '').toLowerCase();
  if (v.includes('promot')) return 'ezn-promoted';
  if (v.includes('reject')) return 'ezn-rejected';
  if (v.includes('defer')) return 'ezn-deferred';
  if (v === 'running' || v === 'pending' || v.includes('flight') || v === 'live') return 'ezn-running';
  if (v === 'baseline' || v === 'seed') return 'ezn-baseline';
  return 'ezn-neutral';
}


// A signed Δ, formatted with an explicit sign + a worse/better word — the
// gate convention is challenger − champion, so POSITIVE = worse (loss rose).
function signedDelta(d, digits) {
  if (!isNum(d)) return '—';
  const dd = digits == null ? 1 : digits;
  return (d > 0 ? '+' : '') + d.toFixed(dd);
}
function worseBetter(d) {
  if (!isNum(d) || d === 0) return 'even';
  return d > 0 ? 'worse' : 'better';
}

// The SCORE channel runs the other way: a score is a quality in [0,1], so a
// POSITIVE Δ is a gain. The two channels are never mixed in one readout — the
// figure paints exactly one of them and says which in its key.
function betterWorseScore(d) {
  if (!isNum(d) || d === 0) return 'even';
  return d > 0 ? 'better' : 'worse';
}
function scoreCmpClass(d) {
  if (!isNum(d) || d === 0) return 'ezn-cmp-even';
  return d > 0 ? 'ezn-cmp-better' : 'ezn-cmp-worse';
}
// A Δ score compact enough to sit INSIDE a 24px disc: sign + two decimals with
// the leading zero dropped (a score lives in [0,1], so |Δ| ≥ 1 is the rare case
// that keeps one decimal). Four characters at most.
function discDelta(d) {
  if (!isNum(d)) return '—';
  const sign = d > 0 ? '+' : d < 0 ? '-' : '±';
  const a = Math.abs(d);
  return sign + (a >= 1 ? a.toFixed(1) : a.toFixed(2).slice(1));
}
// Replicate uncertainty on a score. UNAVAILABLE renders as `--`, never as
// ±0.000: a single draw measures no spread, and printing a zero there claims a
// precision the run does not have.
function seText(se) {
  return isNum(se) ? '±' + se.toFixed(3) : '--';
}

function flow(x1, y1, x2, y2) {
  const mx = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
}

function rectNode(layer, cx, cy, w, h, label, sub, cls) {
  const g = svgEl('g', { class: 'ezn-node ' + (cls || ''), 'data-cz': 'lc-step' }, [
    svgEl('rect', { x: cx - w / 2, y: cy - h / 2, width: w, height: h, rx: 6, class: 'ezn-node-box' }),
    svgEl('text', { x: cx, y: cy - (sub ? 5 : 0), class: 'ezn-node-id' }, [truncate(label, 18)]),
    sub ? svgEl('text', { x: cx, y: cy + 12, class: 'ezn-node-sub' }, [truncate(sub, 22)]) : null,
  ].filter(Boolean));
  layer.appendChild(g);
  return g;
}

// The PER-RUN expansion for a re-raced board entry: a tiny inline panel placed
// to the RIGHT of the disc, holding a sparkline of the N per-run values + one
// row per run (rung label when present, the value, a pass/fail/timeout dot).
// `channel` selects WHICH quantity the panel reads — the run's score when the
// board is scored, its drift loss otherwise — so the panel never shows a column
// of structural zeroes for an adapter that emits no drift stream.
// Clicking a row drills into THAT run's transcript (o.onRun(entry, run) or, as
// a fallback, o.onEntry(entry)). CSS keeps it hidden until the node opens.
function perRunStack(e, x, cy, o, maxRight, channel) {
  const scored = channel === 'score';
  const runValue = (rn) => (scored ? rn.score : rn.drift_loss);
  const runDigits = scored ? 2 : 1;
  const runs = Array.isArray(e.runs) ? e.runs : [];
  const rowH = 13;
  const sparkH = 18;
  const pad = 6;
  // CLAMP the panel WIDTH so it never crosses the Σ node to its right: in the
  // narrow compare-split (width 560) the disc-anchored panel at X.board+r+6
  // (~276) ran the fixed 150px panel to ~426, overlapping Σ (X.agg~370) + the
  // Σ→GATE edge. `maxRight` is the Σ node x — keep the panel's right edge a pad
  // short of it. The clamp ONLY ever shrinks (Math.min with the default 150), so
  // the wide width-900 layout (where the panel already clears Σ) is unchanged;
  // a floor keeps the sparkline + right-anchored loss readout legible.
  const panelW = isNum(maxRight)
    ? Math.max(64, Math.min(150, maxRight - pad - x))
    : 150;
  const h = sparkH + pad + runs.length * rowH + pad;
  const top = cy - h / 2;
  const g = svgEl('g', { class: 'ezn-board-runs', 'data-cz': 'lc-board-runs' });
  g.appendChild(svgEl('rect', { x, y: top, width: panelW, height: h, rx: 5, class: 'ezn-board-runs-box' }));

  // a sparkline of the per-run values (left→right = rung0→…→final).
  const losses = runs.map((rn) => (isNum(runValue(rn)) ? runValue(rn) : null));
  const known = losses.filter((v) => v != null);
  if (known.length >= 2) {
    const min = Math.min(...known), max = Math.max(...known);
    const span = max - min || 1;
    const sx = x + pad, sw = panelW - 2 * pad;
    const sy = top + pad, sh = sparkH - pad;
    let d = '';
    losses.forEach((v, i) => {
      if (v == null) return;
      const px = sx + (losses.length > 1 ? (i / (losses.length - 1)) * sw : 0);
      const py = sy + sh - ((v - min) / span) * sh;
      d += (d ? ' L ' : 'M ') + px.toFixed(1) + ' ' + py.toFixed(1);
    });
    g.appendChild(svgEl('path', { d, class: 'ezn-board-spark', fill: 'none' }));
  }

  runs.forEach((rn, i) => {
    const ry = top + sparkH + pad + i * rowH + rowH - 3;
    const dotCls = rn.pass_fail === true ? 'ezn-promoted' : (rn.wall_clock_budget_exceeded ? 'ezn-deferred' : 'ezn-rejected');
    const tag = rn.rung || (rn.match_id ? rn.match_id : null);
    const rowG = svgEl('g', { class: 'ezn-board-run ' + dotCls, 'data-cz': 'lc-board-run', 'data-run': rn.run_id || '' });
    rowG.appendChild(svgEl('circle', { cx: x + pad + 3, cy: ry - 3, r: 3, class: 'ezn-board-run-dot' }));
    // rung label (only when the backend tagged it — never fabricated).
    if (tag) rowG.appendChild(svgEl('text', { x: x + pad + 12, y: ry, class: 'ezn-board-run-rung', 'text-anchor': 'start' }, [truncate(tag, 12)]));
    rowG.appendChild(svgEl('text', { x: x + panelW - pad, y: ry, class: 'ezn-board-run-loss', 'text-anchor': 'end' }, [isNum(runValue(rn)) ? fmt(runValue(rn), runDigits) : '—']));
    attachHovercard(rowG, (tag ? tag + ' — ' : '') + (scored ? 'score ' : 'loss ') + (isNum(runValue(rn)) ? fmt(runValue(rn), 2) : '—'));
    if (o.onRun || o.onEntry) {
      rowG.style.cursor = 'pointer';
      rowG.setAttribute('tabindex', '0');
      const go = (ev) => {
        if (ev && ev.stopPropagation) ev.stopPropagation();
        if (o.onRun) o.onRun(e.entry_id, rn.run_id || null); else o.onEntry(e.entry_id);
      };
      rowG.addEventListener('click', go);
      rowG.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(ev); } });
    }
    g.appendChild(rowG);
  });
  return g;
}

// The CANDIDATE RUNG-PROGRESSION strip: a small left→right path through the
// tournament the candidate ran — rung 0 → rung 1 → racing-final — each stage
// carrying its Δ-vs-champion and a won/cut verdict. Always available from the
// candidate's tournament-structure record (reconstructed in tournament_model.js),
// it relates a board run to the rounds/rungs even when the per-run records carry
// NO rung tags. Returns a detached <svg> (fit-to-width). `spec.stages` is an
// ordered array of { label, kind:'rung'|'final', delta, verdict:'won'|'cut'|… }.
export function rungProgression(spec) {
  const o = spec || {};
  const stages = Array.isArray(o.stages) ? o.stages : [];
  const w = o.width || 720;
  const h = 64;
  const svg = svgEl('svg', {
    class: 'ezn-rungprog', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': 'Candidate progression through the tournament rungs',
  });
  if (!stages.length) return svg;
  const n = stages.length;
  const padX = 14;
  const usable = w - 2 * padX;
  const step = n > 1 ? usable / (n - 1) : 0;
  const midY = 30;
  const xOf = (i) => padX + (n > 1 ? i * step : usable / 2);
  // step-proportional label cap: a long swiss/elim run (n≥7) in the narrow
  // compare width shrinks `step` until middle-anchored 14-char labels (and their
  // Δ sublabels) collide with their neighbours. Tighten the clip ∝ step (~6px per
  // glyph at the label font), floored at 4 and bounded ABOVE by the prior cap of
  // 14 — so every wide/few-stage figure (step ≥ 84px) renders byte-identically.
  const labelCap = Math.min(14, Math.max(4, Math.floor(step / 6)));

  // connecting spine.
  if (n > 1) {
    svg.appendChild(svgEl('line', { x1: xOf(0), y1: midY, x2: xOf(n - 1), y2: midY, class: 'ezn-rungprog-spine' }));
  }
  stages.forEach((st, i) => {
    const x = xOf(i);
    const v = String(st.verdict || '').toLowerCase();
    const cls = v.includes('won') || v.includes('promot') || v.includes('surv') ? 'ezn-promoted'
      : v.includes('cut') || v.includes('reject') || v.includes('elim') ? 'ezn-rejected'
      : v.includes('decid') || v.includes('live') ? 'ezn-running' : 'ezn-neutral';
    const g = svgEl('g', { class: 'ezn-rungprog-stage ' + cls, 'data-cz': 'lc-rungprog-stage', 'data-kind': st.kind || 'rung' });
    g.appendChild(svgEl('circle', { cx: x, cy: midY, r: 7, class: 'ezn-rungprog-dot' }));
    g.appendChild(svgEl('text', { x, y: 14, class: 'ezn-rungprog-label', 'text-anchor': 'middle' }, [truncate(st.label || ('rung ' + i), labelCap)]));
    const sub = isNum(st.delta) ? (st.delta >= 0 ? '+' : '') + fmt(st.delta, 1) + ' Δ' : (st.verdict || '');
    g.appendChild(svgEl('text', { x, y: midY + 20, class: 'ezn-rungprog-sub', 'text-anchor': 'middle' }, [truncate(sub, labelCap)]));
    if (st.verdict) attachHovercard(g, (st.label || 'rung') + ' — ' + st.verdict + (isNum(st.delta) ? ' (Δ ' + fmt(st.delta, 2) + ')' : ''));
    svg.appendChild(g);
  });
  return svg;
}

// fixed, density-independent vertical geometry — the SAME for a seed/baseline
// (full board, more entries) and a racing challenger (deduped slice, fewer):
// the board fan uses a CONSTANT per-node ROW PITCH (never a fixed proportion of
// an arbitrary height), so 6 entries and 7 entries get identical comfortable
// spacing — neither stretched nor compressed. The internal viewBox height is
// SIZED TO the fan (header pad + N×pitch + key pad), eliminating the large
// top gap, and the structural spine is centred on the fan's TRUE vertical
// centre so it always aligns with the board nodes regardless of entry count.
const ROW_PITCH = 46; // px between adjacent board-fan rows (internal viewBox units).
const HEAD_PAD = 40;  // top band reserved for the column heads.
// the TALLEST node box height in the figure — the Σ/GATE/TERMINAL spine boxes
// (48px) and the slightly shorter PARENT/PATCH boxes (44px) — so the key line's
// vertical clearance is computed from the SAME geometry the boxes actually use
// (never a mismatched hardcoded value). A box centred at cy spans cy ± NODE_BOX_H/2.
const NODE_BOX_H = 48;
// gap below the lowest node box's bottom edge before the key line's baseline,
// and the key line's own line height — both feed BOTH the key-line y and the
// reserved bottom band (KEY_PAD), so the key line is ALWAYS clear of the boxes.
const KEY_GAP = 16;
const KEY_LINE_H = 12;
// bottom band reserved for the per-disc cmp sublabel + the ONE concise key line.
// Must reserve room for the key line BELOW the lowest node box's bottom edge:
// half a node box + a comfortable gap + the line height. (An earlier de-crowd
// trimmed this to a flat 26, which is < a box half (24) and let the key line
// render through the node boxes when the fan span was small — the bug this fixes.)
const KEY_PAD = NODE_BOX_H / 2 + KEY_GAP + KEY_LINE_H;

// The pending TERMINAL label is STRUCTURE-AWARE: a swiss/elim candidate awaiting
// the gate must not read "racing". racing → "⋯ racing", swiss → "⋯ competing",
// single/double elim → "⋯ in bracket"; an unknown/absent structure degrades to a
// neutral "⋯ awaiting gate".
function pendingTermLabel(structure) {
  switch (String(structure || '').toLowerCase()) {
    case 'racing': return '⋯ racing';
    case 'swiss': return '⋯ competing';
    case 'single_elim':
    case 'double_elim': return '⋯ in bracket';
    case 'gauntlet': return '⋯ at gate';
    default: return '⋯ awaiting gate';
  }
}

export function lifecycleDag(spec) {
  const o = spec || {};
  const entries = Array.isArray(o.entries) ? o.entries : [];
  const baseline = !!o.baseline || !o.parentId;
  const dec = baseline ? 'baseline' : (o.decision || 'running');
  // `width` is the viewBox's INTERNAL coordinate width — the SVG is rendered at
  // width:100% (see the attrs below) so it FITS its pane and never overflows. A
  // wider viewBox just means a finer internal grid (the figure is scaled down
  // to fit by preserveAspectRatio); a narrower one (compare split) keeps labels
  // legible at the smaller painted size.
  const w = o.width || 900;

  // DEDUPE to ONE node per distinct board ENTRY first (a racing candidate
  // re-runs the SAME entry across rungs), because the FAN HEIGHT — and thus the
  // whole figure's height — is normalized to the number of DISTINCT nodes, not
  // the raw run count. (The full per-entry dedupe, carrying every run for the
  // expansion panel, is finished below; here we only need the distinct count.)
  const groups = [];
  const byId = new Map();
  for (const e of entries) {
    const id = e == null ? '' : e.entry_id;
    let grp = byId.get(id);
    if (!grp) { grp = { entry_id: id, runs: [] }; byId.set(id, grp); groups.push(grp); }
    grp.runs.push(e);
  }
  const board = groups.map((grp) => {
    const rep = grp.runs[grp.runs.length - 1] || {};
    return {
      entry_id: grp.entry_id,
      drift_loss: rep.drift_loss,
      score: rep.score,
      pass_fail: rep.pass_fail,
      wall_clock_budget_exceeded: rep.wall_clock_budget_exceeded,
      mult: grp.runs.length,
      runs: grp.runs.map((rn) => ({
        run_id: rn && rn.run_id != null ? rn.run_id : null,
        drift_loss: rn ? rn.drift_loss : undefined,
        score: rn ? rn.score : undefined,
        pass_fail: rn ? rn.pass_fail : undefined,
        wall_clock_budget_exceeded: rn ? !!rn.wall_clock_budget_exceeded : false,
        // the rung tag, when the backend supplies it (else null → no label).
        rung: rn && rn.rung != null ? String(rn.rung) : null,
        match_id: rn && rn.match_id != null ? String(rn.match_id) : null,
      })),
    };
  });

  // NORMALIZED height: fixed pitch × node count + padding bands. `o.height`, if
  // passed, acts only as a MINIMUM floor (keeps a tiny board from looking
  // cramped) — it NEVER stretches the fan, so the seed and the challenger that
  // differ only by entry count still share the exact same per-row spacing.
  const nNodes = Math.max(board.length, 1);
  const fanSpan = Math.max((nNodes - 1) * ROW_PITCH, 0);
  // total height = header band + one row of half-pitch + the fan span + bottom
  // band. Exactly as tall as the fan needs — no arbitrary minimum, which is what
  // removes the empty top band on the seed side. `o.height` is intentionally
  // ignored: the figure's height is DERIVED from the node count so the seed and
  // the challenger that differ only by entry count share identical row spacing.
  const h = HEAD_PAD + ROW_PITCH / 2 + fanSpan + KEY_PAD;

  const svg = svgEl('svg', {
    // FIT-TO-WIDTH: width:100% + a viewBox (no fixed pixel width that exceeds
    // the panel, no horizontal-scroll wrapper). All six stages stay visible.
    class: 'ezn-dag', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': `Lifecycle of ${o.genId || 'candidate'}`,
  });

  const cols = { parent: 0.075, patch: 0.245, board: 0.46, agg: 0.66, gate: 0.82, term: 0.95 };
  const X = {};
  for (const k of Object.keys(cols)) X[k] = cols[k] * w;
  // the board fan: first row sits one half-pitch below the header band, each
  // subsequent row a CONSTANT pitch below. The spine (parent/patch/Σ/gate/
  // terminal) is pinned to the fan's TRUE centre so it aligns with the fan for
  // ANY entry count — no floating-in-the-middle, no detachment from the fan.
  const fanTop = HEAD_PAD + ROW_PITCH / 2;
  const fanBot = fanTop + fanSpan;
  const midY = (fanTop + fanBot) / 2;

  const heads = [
    [X.parent, 'PARENT'], [X.patch, 'PATCH'], [X.board, 'BOARD'],
    [X.agg, 'Σ'], [X.gate, 'GATE'], [X.term, baseline ? 'SEED' : 'TERMINAL'],
  ];
  for (const [x, t] of heads) svg.appendChild(svgEl('text', { x, y: 16, class: 'ezn-col-head', 'text-anchor': 'middle' }, [t]));

  const edgeLayer = svgEl('g', { class: 'ezn-edge-layer' });
  const nodeLayer = svgEl('g', { class: 'ezn-node-layer' });

  rectNode(nodeLayer, X.parent, midY, 0.12 * w, 44, o.parentId || '∅ seed', baseline ? 'no parent' : 'champion', baseline ? 'ezn-baseline' : 'ezn-promoted');

  const patchSub = baseline ? 'seed snapshot'
    : (isNum(o.patchPoints) && o.patchPoints > 0 ? o.patchPoints + ' mutation point' + (o.patchPoints === 1 ? '' : 's') : 'patch');
  const patchNode = rectNode(nodeLayer, X.patch, midY, 0.13 * w, 44, baseline ? 'seed' : 'PATCH', patchSub, baseline ? 'ezn-baseline' : 'ezn-patch');
  // fix #2: the PATCH node is clickable → this candidate's side-by-side diff.
  if (!baseline && o.onPatch) {
    patchNode.classList.add('ezn-clickable');
    patchNode.style.cursor = 'pointer';
    patchNode.setAttribute('tabindex', '0');
    patchNode.setAttribute('role', 'button');
    patchNode.setAttribute('aria-label', 'Open this candidate’s patch diff');
    attachHovercard(patchNode, 'Open this candidate’s side-by-side patch diff');
    patchNode.addEventListener('click', () => o.onPatch());
    patchNode.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onPatch(); } });
  }
  edgeLayer.appendChild(svgEl('path', { d: flow(X.parent + 0.06 * w, midY, X.patch - 0.065 * w, midY), class: 'ezn-edge ezn-edge-spine', fill: 'none' }));

  // (`board` — the per-entry dedupe carrying every run for the expansion panel —
  // was assembled up-front so the FAN HEIGHT could be normalized to the distinct
  // node count. A RACING candidate re-runs the SAME entry across rungs, so the
  // raw stream repeats an entry_id N times; the lifecycle keeps ONE node per
  // entry but each deduped node EXPANDS to its N per-run losses, labelled by
  // rung when the backend tags them. A gauntlet candidate has size-1 groups →
  // no expansion, identical to a plain single-node rendering.)

  // The SERVER's per-entry champion-vs-candidate comparison, keyed by entry_id
  // (`/api/matchup-grid`, one row per entry). The join and the verdict are the
  // server's — this figure only paints them. Each row carries the movement on
  // whichever channel the contract populates (`decided_by`), the continuous
  // scores behind it, and the candidate's replicate spread. Absent for a
  // baseline / when there is no champion to compare against.
  const compare = o.compare && typeof o.compare === 'object' ? o.compare : {};
  const cmpOf = (entryId) => compare[entryId] || null;
  const champId = o.championId || 'champion';
  // WHICH CHANNEL this figure paints — the same fall-through the server uses to
  // resolve an entry's verdict, so the figure and the verdict beside it are
  // reading the same quantity: the continuous score when the board is scored,
  // then drift, then the bare pass predicate. Drift is skipped entirely when the
  // adapter emits no drift stream (`drift_present: false`): its losses are then
  // structurally 0.000 on every entry, and a column of zeroes explains nothing.
  const driftPresent = o.driftPresent !== false;
  const scored = board.some((e) => {
    const c = cmpOf(e.entry_id);
    return (c && isNum(c.deltaScore)) || isNum(e.score);
  });
  const channel = scored ? 'score' : driftPresent ? 'drift' : 'pass';
  // Edge thickness = this entry's share of the figure's total movement, so the
  // widest ribbons are the entries that carried the verdict.
  const magnitude = (e) => {
    if (channel === 'score') {
      const c = cmpOf(e.entry_id);
      return c && isNum(c.deltaScore) ? Math.abs(c.deltaScore) : 0;
    }
    if (channel === 'pass') return 1;
    return isNum(e.drift_loss) ? e.drift_loss : 0;
  };
  const total = board.reduce((a, e) => a + magnitude(e), 0) || 1;
  const step = board.length > 1 ? (fanBot - fanTop) / (board.length - 1) : 0;
  if (board.length === 0) {
    rectNode(nodeLayer, X.board, midY, 0.14 * w, 40, 'no board entries', 'scored', 'ezn-neutral');
  } else {
    board.forEach((e, i) => {
      const y = board.length > 1 ? fanTop + i * step : midY;
      const r = 12;
      // the loss value lives INSIDE the disc; the entry label sits to the LEFT
      // of the disc (anchored at its end), so a label can NEVER overlap the
      // circle or the loss text. The rung-multiplicity badge sits to the RIGHT.
      const labelDX = -(r + 8);
      const cls = e.pass_fail === true ? 'ezn-promoted' : (e.wall_clock_budget_exceeded ? 'ezn-deferred' : 'ezn-rejected');
      edgeLayer.appendChild(svgEl('path', { d: flow(X.patch + 0.065 * w, midY, X.board - r, y), class: 'ezn-edge ezn-edge-soft', fill: 'none' }));
      const cmp = cmpOf(e.entry_id);
      const dScore = cmp && isNum(cmp.deltaScore) ? cmp.deltaScore : null;
      const contrib = magnitude(e) / total;
      // On the score channel the ribbon's tone follows the MOVEMENT (a gain is
      // good, a regression is bad) rather than the pass bit, so a control entry
      // slipping the wrong way reads as a bad ribbon even while it still passes.
      const edgeTone = channel === 'score' && dScore != null
        ? (dScore >= 0 ? 'ezn-edge-good' : 'ezn-edge-bad')
        : (cls === 'ezn-promoted' ? 'ezn-edge-good' : 'ezn-edge-bad');
      edgeLayer.appendChild(svgEl('path', { d: flow(X.board + r, y, X.agg - 0.05 * w, midY), class: 'ezn-edge ' + edgeTone, 'stroke-width': Math.max(1, contrib * 12), fill: 'none' }));
      const raced = e.mult > 1;
      const taggedRuns = raced && e.runs.some((rn) => rn.rung || rn.match_id);
      // The DISC carries the quantity the gate aggregated: on a scored board the
      // per-entry Δ score vs the champion (POSITIVE = better); on a drift-only
      // board the drift loss. The sublabel below it names both sides
      // — `0.415 → 1.000 ±0.065` — so an entry stuck at the floor and a control
      // held at the ceiling, which both move by 0.000, read as different facts.
      const discText = channel === 'score'
        ? (dScore != null ? discDelta(dScore) : (isNum(e.score) ? fmt(e.score, 2) : '—'))
        : channel === 'pass'
          ? (e.pass_fail === true ? '✓' : e.pass_fail === false ? '✕' : '—')
          : (isNum(e.drift_loss) ? fmt(e.drift_loss, 0) : '—');
      const children = [
        svgEl('circle', { cx: X.board, cy: y, r, class: 'ezn-board-disc' }),
        // label to the LEFT of the disc, vertically centred, never on the circle.
        svgEl('text', { x: X.board + labelDX, y: y + 3, class: 'ezn-board-label', 'text-anchor': 'end' }, [truncate(e.entry_id, 18)]),
        svgEl('text', { x: X.board, y: y + 3, class: 'ezn-board-loss', 'text-anchor': 'middle' }, [discText]),
      ];
      if (channel === 'score' && cmp && (isNum(cmp.champScore) || isNum(cmp.candScore))) {
        children.push(svgEl('text', {
          x: X.board, y: y + r + 11, class: 'ezn-board-cmp ' + scoreCmpClass(dScore), 'text-anchor': 'middle',
          'data-champ-score': isNum(cmp.champScore) ? fmt(cmp.champScore, 3) : '',
          'data-delta-score': dScore != null ? signedDelta(dScore, 3) : '',
          'data-se': isNum(cmp.se) ? cmp.se.toFixed(3) : '',
        }, [(isNum(cmp.champScore) ? fmt(cmp.champScore, 2) : '—') + ' → '
            + (isNum(cmp.candScore) ? fmt(cmp.candScore, 2) : '—') + ' ' + seText(cmp.se)]));
      } else if (channel === 'drift' && cmp && isNum(cmp.champDrift)) {
        const dLoss = isNum(e.drift_loss) ? e.drift_loss - cmp.champDrift : null;
        const dCls = dLoss > 0 ? 'ezn-cmp-worse' : dLoss < 0 ? 'ezn-cmp-better' : 'ezn-cmp-even';
        children.push(svgEl('text', {
          x: X.board, y: y + r + 11, class: 'ezn-board-cmp ' + dCls, 'text-anchor': 'middle',
          'data-champ-loss': fmt(cmp.champDrift, 1), 'data-delta': signedDelta(dLoss, 1),
        }, ['champ ' + fmt(cmp.champDrift, 0) + ' · Δ ' + signedDelta(dLoss, 0)]));
      }
      if (raced) {
        // rung-multiplicity badge to the RIGHT of the disc — makes it clear the
        // SAME entry was re-raced across rungs (not a random duplicate).
        children.push(svgEl('text', { x: X.board + r + 6, y: y + 3, class: 'ezn-board-mult', 'text-anchor': 'start' }, ['×' + e.mult]));
        // the EXPANSION: a small per-run stack (a sparkline + one row per run,
        // labelled by rung when present) revealed on hover/focus/click. It is
        // appended to THIS node group so it inherits the node's position; CSS
        // hides it until :hover / :focus-within / .ezn-board-open. Pass X.agg
        // (the Σ node x) as the right boundary so the panel is clamped to stop
        // short of Σ in the narrow compare-split instead of overlapping it.
        children.push(perRunStack(e, X.board + r + 6, y, o, X.agg, channel));
      }
      const aria = channel === 'score'
        ? `${e.entry_id} delta score ${dScore != null ? signedDelta(dScore, 3) : '—'} versus champion`
          + (isNum(cmp && cmp.se) ? `, standard error ${cmp.se.toFixed(3)}` : ', standard error unavailable')
        : channel === 'pass'
          ? `${e.entry_id} ${e.pass_fail === true ? 'passed' : e.pass_fail === false ? 'failed' : 'no outcome recorded'}`
          : `${e.entry_id} drift loss ${isNum(e.drift_loss) ? fmt(e.drift_loss) : '—'}`;
      const g = svgEl('g', {
        class: 'ezn-node ezn-board-node ' + cls + (raced ? ' ezn-board-raced' : '') + (raced ? ' ezn-board-expandable' : ''), 'data-cz': 'lc-board-node',
        'data-key': e.entry_id, 'data-mult': e.mult, 'data-tagged': taggedRuns ? '1' : '0',
        'data-channel': channel,
        tabindex: (o.onEntry || raced) ? '0' : null,
        'aria-label': aria + (raced ? ` · ${e.mult} per-run values across rungs` : ''),
      }, children);
      // the styled hovercard carries the per-board champ-comparison detail: both
      // sides on the painted channel, the replicate spread, and the per-run values.
      const head = channel === 'score'
        ? `${e.entry_id} · score (higher is better): champion ${champId} ${isNum(cmp && cmp.champScore) ? fmt(cmp.champScore, 3) : '—'}`
          + ` → this ${isNum(cmp && cmp.candScore) ? fmt(cmp.candScore, 3) : '—'}`
          + (dScore != null ? ` · Δ ${signedDelta(dScore, 3)} (${betterWorseScore(dScore)})` : '')
          + ` · ${seText(cmp && cmp.se)}`
          + (cmp && cmp.replicates > 1 ? ` over ${cmp.replicates} replicates` : ' (one replicate — no spread measured)')
        : channel === 'pass'
          ? `${e.entry_id} · pass predicate only — this board carries no continuous score and the adapter emits no drift stream, so the entry's outcome is its pass bit`
          : `${e.entry_id} · drift loss (lower is better): this ${isNum(e.drift_loss) ? fmt(e.drift_loss) : '—'}`
            + (cmp && isNum(cmp.champDrift) && isNum(e.drift_loss)
              ? ` · champion ${champId} ${fmt(cmp.champDrift, 1)} · Δ ${signedDelta(e.drift_loss - cmp.champDrift, 1)} (${worseBetter(e.drift_loss - cmp.champDrift)})`
              : '');
      attachHovercard(g, head
        + (raced ? ` · ${e.mult} runs — ` + e.runs.map((rn) => (rn.rung ? rn.rung + ': ' : '') + (isNum(channel === 'score' ? rn.score : rn.drift_loss) ? fmt(channel === 'score' ? rn.score : rn.drift_loss, channel === 'score' ? 2 : 1) : '—')).join(' · ') + ' (representative = full-board run)' : '')
        + (e.wall_clock_budget_exceeded ? ' · timed out' : '')
        + (e.pass_fail === false ? ' · failed' : e.pass_fail === true ? ' · passed' : ''));
      // a raced node toggles its expansion on click of the DISC (clicking a
      // per-run row still drills into that run/transcript); a gauntlet node
      // clicks straight through to its drill-down (unchanged).
      if (raced) {
        g.addEventListener('click', () => { if (g.classList) g.classList.toggle('ezn-board-open'); });
        g.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); if (g.classList) g.classList.toggle('ezn-board-open'); }
        });
      } else if (o.onEntry) {
        g.style.cursor = 'pointer';
        g.addEventListener('click', () => o.onEntry(e.entry_id));
        g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onEntry(e.entry_id); } });
      }
      nodeLayer.appendChild(g);
    });
  }

  // ---- Σ node: the aggregate of the per-board losses over the rung's slice,
  // PLUS the champion's Σ on the same slice and the Δ between them (this is the
  // score the gate compares). A sublabel + rich tooltip make the circles→Σ
  // linkage legible ("these per-board losses sum to Σ; lower is better") and
  // expose the Δ-vs-champion the gate acts on.
  // On the SCORE channel the aggregate is the MEAN of the per-entry Δ scores
  // over the entries both sides ran — which is exactly the movement in the
  // board-level mean score, so the discs above it and the gate beside it are
  // the same arithmetic rather than two unrelated numbers stacked in one
  // diagram. On the drift channel it stays the sum of the per-board losses.
  const scoredDeltas = board.map((e) => cmpOf(e.entry_id)).filter((c) => c && isNum(c.deltaScore)).map((c) => c.deltaScore);
  const meanDeltaScore = scoredDeltas.length
    ? scoredDeltas.reduce((a, v) => a + v, 0) / scoredDeltas.length : null;
  let aggLabel, aggSub, aggCls, aggTip, aggNodeData;
  if (channel === 'score') {
    aggLabel = 'Σ Δ score';
    aggSub = meanDeltaScore != null
      ? signedDelta(meanDeltaScore, 3) + ' · ' + scoredDeltas.length + ' entr' + (scoredDeltas.length === 1 ? 'y' : 'ies')
      : '—';
    aggCls = scoreCmpClass(meanDeltaScore);
    aggTip = 'Σ Δ score — the mean of the per-entry Δ scores above, over the entries BOTH the candidate and the champion ran (higher is better).'
      + (meanDeltaScore != null ? ` ${signedDelta(meanDeltaScore, 3)} across ${scoredDeltas.length} entr${scoredDeltas.length === 1 ? 'y' : 'ies'}.` : '')
      + ' That mean IS the movement in the board-level mean score, so this node is the arithmetic of the discs feeding it — not a separate quantity.'
      + ' An entry only one side ran carries no Δ and contributes nothing, the same restriction the gate applies.';
    aggNodeData = { cand: null, champ: null, delta: meanDeltaScore };
  } else if (channel === 'pass') {
    const passed = board.filter((e) => e.pass_fail === true).length;
    aggLabel = 'Σ pass';
    aggSub = passed + '/' + board.length;
    aggCls = '';
    aggTip = `Σ pass — ${passed} of ${board.length} board entries passed. `
      + 'This board carries no continuous score and the adapter emits no drift stream, so the pass predicate is the only outcome the entries record; '
      + 'there is no per-entry magnitude to sum.';
    aggNodeData = { cand: null, champ: null, delta: null };
  } else {
    const candSigma = isNum(o.candidateSigma) ? o.candidateSigma : (entries.length ? total : null);
    const champSigma = isNum(o.championSigma) ? o.championSigma : null;
    const sigmaDelta = isNum(o.deltaSigma) ? o.deltaSigma
      : (candSigma != null && champSigma != null ? candSigma - champSigma : null);
    aggLabel = 'Σ loss';
    aggSub = candSigma != null
      ? (champSigma != null ? fmt(candSigma, 0) + ' · Δ ' + signedDelta(sigmaDelta, 0) : fmt(candSigma, 0))
      : '—';
    aggCls = sigmaDelta == null ? '' : sigmaDelta > 0 ? 'ezn-cmp-worse' : sigmaDelta < 0 ? 'ezn-cmp-better' : 'ezn-cmp-even';
    aggTip = 'Σ loss — the per-board drift losses summed over this rung’s board slice (lower is better).'
      + (candSigma != null ? ` Candidate Σ ${fmt(candSigma, 1)}` : '')
      + (champSigma != null ? ` vs champion ${champId} Σ ${fmt(champSigma, 1)} · Δ ${signedDelta(sigmaDelta, 1)} (${worseBetter(sigmaDelta)})` : '')
      + '. The gate compares these scalars on the SAME boards — Δ = challenger − champion, positive = worse.';
    aggNodeData = { cand: candSigma, champ: champSigma, delta: sigmaDelta };
  }
  const aggNode = rectNode(nodeLayer, X.agg, midY, 0.1 * w, 48, aggLabel, aggSub, 'ezn-neutral');
  if (aggCls) aggNode.classList.add(aggCls);
  aggNode.setAttribute('data-channel', channel);
  aggNode.setAttribute('data-cand-sigma', aggNodeData.cand != null ? fmt(aggNodeData.cand, 1) : '');
  if (aggNodeData.champ != null) aggNode.setAttribute('data-champ-sigma', fmt(aggNodeData.champ, 1));
  if (aggNodeData.delta != null) aggNode.setAttribute('data-delta-sigma', signedDelta(aggNodeData.delta, channel === 'score' ? 3 : 1));
  attachHovercard(aggNode, aggTip);

  edgeLayer.appendChild(svgEl('path', { d: flow(X.agg + 0.05 * w, midY, X.gate - 0.06 * w, midY), class: 'ezn-edge ' + (verdictClass(dec) === 'ezn-promoted' ? 'ezn-edge-good' : 'ezn-edge-bad'), fill: 'none' }));
  const gateSub = baseline ? 'no gate (seed)' : (isNum(o.deltaScalar) ? (o.deltaScalar >= 0 ? '+' : '') + fmt(o.deltaScalar, 1) + ' Δ' : dec);
  const gateNode = rectNode(nodeLayer, X.gate, midY, 0.12 * w, 48, baseline ? 'BASELINE' : 'GATE', gateSub, verdictClass(dec));
  // ---- GATE node: the 3-rule acceptance test, made self-explanatory. The
  // promote gate is NOT "smallest Σ wins": a challenger is accepted only if it
  // (1) beats the champion's scalar by the promote margin (Δ < margin), AND
  // (2) regresses no previously-passing predicate (pass-rate monotonicity), AND
  // (3) regresses no namespace (namespace monotonicity). Rules short-circuit in
  // order, so a challenger with a BETTER scalar can still be rejected by rule 2
  // or 3. `o.gateExplain` (assembled in the view from D.gate) names which rule
  // was the PRIMARY DRIVER and carries the decisive numbers.
  if (!baseline) {
    const gx = o.gateExplain || null;
    let gateTip;
    if (gx && gx.decidingRule) {
      const verb = (gx.decision === 'promoted') ? 'promoted' : 'rejected';
      gateTip = `Promote gate — a 3-rule test (scalar margin · pass-rate monotonicity · namespace monotonicity), short-circuiting in order. `;
      if (gx.decidingRule === 'scalar_margin') {
        gateTip += `Decided by the SCALAR-MARGIN rule: Δ scalar ${signedDelta(gx.deltaScalar, 1)} vs champion ${champId}`
          + (isNum(gx.margin) ? ` (needs ≤ ${fmt(gx.margin, 2)})` : '')
          + ` → ${gx.deltaScalar > 0 ? 'worse than champion → fails the scalar-margin rule → ' : ''}${verb}.`;
      } else if (gx.decidingRule === 'pass_rate_monotonicity') {
        // Scope is operator-selected (per-entry vs aggregate); the backend
        // detail/reason already says which way it regressed, so prefer that
        // wording over hard-coding per-entry semantics here.
        gateTip += `Scalar may be better, BUT it failed the pass-rate-monotonicity rule (rule 2)`
          + (gx.detail ? ` — ${gx.detail}` : (gx.regressed ? ` — regressed \`${gx.regressed}\`` : ''))
          + ` → ${verb}.`;
      } else if (gx.decidingRule === 'namespace_monotonicity') {
        gateTip += `Scalar may be better, BUT it regressed a namespace`
          + (gx.regressed ? ` (\`${gx.regressed}\`)` : '') + ` — fails the namespace-monotonicity rule (rule 3) → ${verb}.`;
      } else {
        gateTip += `Primary driver: ${gx.decidingLabel || gx.decidingRule} → ${verb}.`;
      }
      if (gx.reason) gateTip += ` (${gx.reason})`;
      gateNode.setAttribute('data-deciding-rule', gx.decidingRule);
      if (gx.decidingLabel) gateNode.setAttribute('data-deciding-label', gx.decidingLabel);
      if (isNum(gx.deltaScalar)) gateNode.setAttribute('data-delta-scalar', signedDelta(gx.deltaScalar, 2));
      if (isNum(gx.margin)) gateNode.setAttribute('data-margin', fmt(gx.margin, 2));
      if (gx.regressed) gateNode.setAttribute('data-regressed', gx.regressed);
    } else {
      gateTip = 'Promote gate — a 3-rule acceptance test: (1) beat the champion’s scalar by the promote margin, (2) no pass-rate regression, (3) no namespace regression. Δ = challenger − champion on the same boards; positive = worse.';
    }
    gateNode.classList.add('ezn-gate-node');
    gateNode.setAttribute('data-cz', 'lc-gate');
    // the full 3-rule GATE explanation now lives in the styled hovercard.
    attachHovercard(gateNode, gateTip);
  }

  const promoted = dec === 'promoted' || (baseline && o.promoted === true);
  // Class B: a PENDING candidate (in-flight / not yet raced — promoted == null,
  // no resolved decision) must NOT read "✕ dead branch / champion stands". Show
  // a non-terminal racing/awaiting-gate state instead.
  const pending = !baseline && !promoted && (dec === 'pending' || dec === 'running' || (o.promoted == null && (!dec || dec === 'running' || dec === 'pending')));
  let termLabel, termSub, termCls;
  if (baseline) { termLabel = 'seed'; termSub = 'defines floor'; termCls = 'ezn-baseline'; }
  else if (promoted) { termLabel = CROWN.current + ' promoted'; termSub = 'new champion'; termCls = 'ezn-promoted'; }
  else if (pending) { termLabel = pendingTermLabel(o.structure); termSub = 'awaiting gate'; termCls = 'ezn-running'; }
  else { termLabel = '✕ dead branch'; termSub = 'champion stands'; termCls = 'ezn-rejected'; }
  edgeLayer.appendChild(svgEl('path', { d: flow(X.gate + 0.06 * w, midY, X.term - 0.045 * w, midY), class: 'ezn-edge ' + (promoted ? 'ezn-edge-good' : pending ? 'ezn-edge-neutral' : 'ezn-edge-bad'), fill: 'none' }));
  rectNode(nodeLayer, X.term, midY, 0.1 * w, 48, termLabel, termSub, termCls);

  svg.appendChild(edgeLayer);
  svg.appendChild(nodeLayer);

  // ---- the KEY beneath the DAG. DE-CROWDED: where the figure once carried two
  // long, largely-redundant always-on prose blocks (this legend line + a verbose
  // view caption), it now reads with ONE short key line plus a "?" affordance.
  // The full how-to walkthrough (parent → patch → … → terminal, the 3-rule gate,
  // the click/hover affordances) moved into the "?" hovercard + the GATE/Σ
  // hovercards — detail on demand, a clean figure at a glance. Skipped for a
  // baseline (no gate). Theme-aware (CSS), no motion.
  if (!baseline) {
    // CLEARANCE: pin the key line's baseline BELOW the lowest node box's bottom
    // edge — `fanBot` is the bottom-most board circle's centre AND (since
    // fanBot ≥ midY) at/below the Σ/GATE/TERMINAL spine boxes, so
    // `fanBot + NODE_BOX_H/2` is the lowest box edge for ANY node count; a
    // KEY_GAP below that is the key baseline. It also clears each board circle's
    // `champ N · Δ` sublabel (≈ fanBot + r + 11 = fanBot + 23 < fanBot + 24).
    // KEY_PAD reserves the matching room in the derived height `h`, so the key
    // line never overlaps a node box whether the fan has 1 row or many.
    const ky = fanBot + NODE_BOX_H / 2 + KEY_GAP;
    // The key states the SIGN CONVENTION of the channel actually painted. The
    // two channels run opposite ways, and mixing them in one figure is the easy
    // misread this line exists to prevent.
    const key = svgEl('text', { class: 'ezn-dag-key', x: w / 2, y: ky, 'text-anchor': 'middle', 'data-cz': 'lc-key' }, [
      channel === 'score'
        ? 'Δ score vs champion · + = better · ± = replicate spread, -- when unmeasured · hover nodes for detail'
        : channel === 'pass'
          ? 'pass predicate only · no continuous score, no drift stream · hover nodes for detail'
          : 'Δ vs champion · + = worse · lower loss better · hover nodes for detail',
    ]);
    svg.appendChild(key);

    // the "?" info affordance — opens the FULL walkthrough in a hovercard. A
    // small focusable badge top-right of the figure; keyboard-accessible.
    const infoG = svgEl('g', { class: 'ezn-dag-info', 'data-cz': 'lc-info', role: 'button', 'aria-label': 'How to read this lifecycle figure' });
    infoG.appendChild(svgEl('circle', { cx: w - 14, cy: 14, r: 8, class: 'ezn-dag-info-badge' }));
    infoG.appendChild(svgEl('text', { x: w - 14, y: 18, class: 'ezn-dag-info-mark', 'text-anchor': 'middle' }, ['?']));
    attachHovercard(infoG,
      'How to read this lifecycle: parent → patch → board (one node per entry, colour = pass/fail/timeout) → Σ → gate → terminal. '
      + (channel === 'score'
        ? 'Each board circle = this candidate’s Δ score vs the champion on the SAME board (positive = better), with both sides and the replicate spread beneath it; '
          + 'Σ is the mean of those Δs over the entries both sides ran — the movement in the board-level mean score; the GATE applies a 3-rule test '
        : channel === 'pass'
          ? 'Each board circle = whether this candidate passed that entry; the board carries no continuous score and no drift stream, so there is no magnitude to plot; the GATE applies a 3-rule test '
          : 'Each board circle = this candidate’s drift loss vs the champion’s on the SAME board (Δ, positive = worse); '
            + 'Σ sums those losses on the slice; the GATE compares Σ-vs-champion under a 3-rule test ')
      + '(scalar margin · pass-rate monotonicity · namespace monotonicity — hover the GATE to see which rule decided). '
      + 'Click the PATCH node → this candidate’s side-by-side diff; hover/click a re-raced board node → its per-run values (by rung).');
    svg.appendChild(infoG);
  }
  return svg;
}

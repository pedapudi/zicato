// variants/T/dag.js — the compact candidate-lifecycle DAG (Console).
//
// Self-contained, deliberately SMALL: a single static SVG (NO pan/zoom) that
// reads one candidate's life left-to-right as a flow of cause → effect →
// verdict:
//
//   PARENT ─▶ PATCH ─▶ [ board fan: one node per entry ] ─▶ Σ ─▶ GATE ─▶ TERMINAL
//
// Pure builder: (spec) -> detached <svg>. Mark classes are `ezn-*` and are
// styled (scoped under the variant root) by css/variants/N/console.css.

import { svgEl } from '../../core/dom.js';
import { isNum, fmt } from './svg.js';

export function verdictClass(verdict) {
  const v = String(verdict || '').toLowerCase();
  if (v.includes('promot')) return 'ezn-promoted';
  if (v.includes('reject')) return 'ezn-rejected';
  if (v.includes('defer')) return 'ezn-deferred';
  if (v === 'running' || v === 'pending' || v.includes('flight') || v === 'live') return 'ezn-running';
  if (v === 'baseline' || v === 'seed') return 'ezn-baseline';
  return 'ezn-neutral';
}

function clip(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

function flow(x1, y1, x2, y2) {
  const mx = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
}

function rectNode(layer, cx, cy, w, h, label, sub, cls) {
  const g = svgEl('g', { class: 'ezn-node ' + (cls || ''), 'data-cz': 'lc-step' }, [
    svgEl('rect', { x: cx - w / 2, y: cy - h / 2, width: w, height: h, rx: 6, class: 'ezn-node-box' }),
    svgEl('text', { x: cx, y: cy - (sub ? 5 : 0), class: 'ezn-node-id' }, [clip(label, 18)]),
    sub ? svgEl('text', { x: cx, y: cy + 12, class: 'ezn-node-sub' }, [clip(sub, 22)]) : null,
  ].filter(Boolean));
  layer.appendChild(g);
  return g;
}

// The PER-RUN expansion for a re-raced board entry: a tiny inline panel placed
// to the RIGHT of the disc, holding a sparkline of the N run losses + one row
// per run (rung label when present, the loss value, a pass/fail/timeout dot).
// Clicking a row drills into THAT run's transcript (o.onRun(entry, run) or, as
// a fallback, o.onEntry(entry)). CSS keeps it hidden until the node opens.
function perRunStack(e, x, cy, o) {
  const runs = Array.isArray(e.runs) ? e.runs : [];
  const rowH = 13;
  const panelW = 150;
  const sparkH = 18;
  const pad = 6;
  const h = sparkH + pad + runs.length * rowH + pad;
  const top = cy - h / 2;
  const g = svgEl('g', { class: 'ezn-board-runs', 'data-cz': 'lc-board-runs' });
  g.appendChild(svgEl('rect', { x, y: top, width: panelW, height: h, rx: 5, class: 'ezn-board-runs-box' }));

  // a sparkline of the per-run losses (left→right = rung0→…→final).
  const losses = runs.map((rn) => (isNum(rn.drift_loss) ? rn.drift_loss : null));
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
    const dotCls = rn.pass_fail === 1 ? 'ezn-promoted' : (rn.wall_clock_budget_exceeded ? 'ezn-deferred' : 'ezn-rejected');
    const tag = rn.rung || (rn.match_id ? rn.match_id : null);
    const rowG = svgEl('g', { class: 'ezn-board-run ' + dotCls, 'data-cz': 'lc-board-run', 'data-run': rn.run_id || '' });
    rowG.appendChild(svgEl('circle', { cx: x + pad + 3, cy: ry - 3, r: 3, class: 'ezn-board-run-dot' }));
    // rung label (only when the backend tagged it — never fabricated).
    if (tag) rowG.appendChild(svgEl('text', { x: x + pad + 12, y: ry, class: 'ezn-board-run-rung', 'text-anchor': 'start' }, [clip(tag, 12)]));
    rowG.appendChild(svgEl('text', { x: x + panelW - pad, y: ry, class: 'ezn-board-run-loss', 'text-anchor': 'end' }, [isNum(rn.drift_loss) ? fmt(rn.drift_loss, 1) : '—']));
    rowG.appendChild(svgEl('title', null, [(tag ? tag + ' — ' : '') + 'loss ' + (isNum(rn.drift_loss) ? fmt(rn.drift_loss, 2) : '—')]));
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
// candidate's tournament-structure record (reconstructed in views/structure.js),
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
    g.appendChild(svgEl('text', { x, y: 14, class: 'ezn-rungprog-label', 'text-anchor': 'middle' }, [clip(st.label || ('rung ' + i), 14)]));
    const sub = isNum(st.delta) ? (st.delta >= 0 ? '+' : '') + fmt(st.delta, 1) + ' Δ' : (st.verdict || '');
    g.appendChild(svgEl('text', { x, y: midY + 20, class: 'ezn-rungprog-sub', 'text-anchor': 'middle' }, [clip(sub, 14)]));
    if (st.verdict) g.appendChild(svgEl('title', null, [(st.label || 'rung') + ' — ' + st.verdict + (isNum(st.delta) ? ' (Δ ' + fmt(st.delta, 2) + ')' : '')]));
    svg.appendChild(g);
  });
  return svg;
}

export function lifecycleDag(spec) {
  const o = spec || {};
  const entries = Array.isArray(o.entries) ? o.entries : [];
  const baseline = !!o.baseline || !o.parentId;
  const dec = baseline ? 'baseline' : (o.decision || 'running');
  // `width` is now the viewBox's INTERNAL coordinate width — the SVG itself is
  // rendered at width:100% (see the attrs below) so it FITS its pane and never
  // overflows. A wider viewBox just means a finer internal grid (the figure is
  // scaled down to fit by preserveAspectRatio); a narrower one (compare split)
  // keeps labels legible at the smaller painted size.
  const w = o.width || 900;
  const h = o.height || 360;

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
  const midY = h * 0.5;
  const fanTop = h * 0.16;
  const fanBot = h * 0.84;

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
    patchNode.appendChild(svgEl('title', null, ['Open this candidate’s side-by-side patch diff']));
    patchNode.addEventListener('click', () => o.onPatch());
    patchNode.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onPatch(); } });
  }
  edgeLayer.appendChild(svgEl('path', { d: flow(X.parent + 0.06 * w, midY, X.patch - 0.065 * w, midY), class: 'ezn-edge ezn-edge-spine', fill: 'none' }));

  // DEDUPE to ONE node per distinct board ENTRY. A RACING candidate re-runs the
  // SAME entry across rungs (rung0 slice → rung1 larger slice → racing-final
  // full board), so the raw `entries` stream carries the same entry_id N times.
  // The lifecycle keeps the clean cause→effect SUMMARY (one node per entry), but
  // is NO LONGER LOSSY on the values: each deduped node EXPANDS (hover / click)
  // to reveal its N per-run losses as a small inline stack + sparkline, and —
  // when the per-entry records carry `match_id`/`rung` (a parallel backend
  // change) — each per-run row is LABELLED by its rung/matchup (rung 0 / rung 1
  // / final). When the rung fields are ABSENT (legacy data, e.g. the current
  // e0), the expansion still lists the per-run losses but fabricates NO rung
  // labels. For a gauntlet candidate (one run per entry) every group has size 1
  // → no expansion, identical to the old single-node rendering.
  const groups = [];
  const byId = new Map();
  for (const e of entries) {
    const id = e == null ? '' : e.entry_id;
    let grp = byId.get(id);
    if (!grp) { grp = { entry_id: id, runs: [] }; byId.set(id, grp); groups.push(grp); }
    grp.runs.push(e);
  }
  // representative = the LAST run for the entry (racing-final / full board). Its
  // loss + pass/fail + timeout drive the node; multiplicity = the run count; and
  // we carry EVERY run (loss + rung tag, when present) for the expansion panel.
  const board = groups.map((grp) => {
    const rep = grp.runs[grp.runs.length - 1] || {};
    return {
      entry_id: grp.entry_id,
      drift_loss: rep.drift_loss,
      pass_fail: rep.pass_fail,
      wall_clock_budget_exceeded: rep.wall_clock_budget_exceeded,
      mult: grp.runs.length,
      runs: grp.runs.map((rn) => ({
        run_id: rn && rn.run_id != null ? rn.run_id : null,
        drift_loss: rn ? rn.drift_loss : undefined,
        pass_fail: rn ? rn.pass_fail : undefined,
        wall_clock_budget_exceeded: rn ? !!rn.wall_clock_budget_exceeded : false,
        // the rung tag, when the backend supplies it (else null → no label).
        rung: rn && rn.rung != null ? String(rn.rung) : null,
        match_id: rn && rn.match_id != null ? String(rn.match_id) : null,
      })),
    };
  });

  const total = board.reduce((a, e) => a + (isNum(e.drift_loss) ? e.drift_loss : 0), 0) || 1;
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
      const cls = e.pass_fail === 1 ? 'ezn-promoted' : (e.wall_clock_budget_exceeded ? 'ezn-deferred' : 'ezn-rejected');
      edgeLayer.appendChild(svgEl('path', { d: flow(X.patch + 0.065 * w, midY, X.board - r, y), class: 'ezn-edge ezn-edge-soft', fill: 'none' }));
      const contrib = (isNum(e.drift_loss) ? e.drift_loss : 0) / total;
      edgeLayer.appendChild(svgEl('path', { d: flow(X.board + r, y, X.agg - 0.05 * w, midY), class: 'ezn-edge ' + (cls === 'ezn-promoted' ? 'ezn-edge-good' : 'ezn-edge-bad'), 'stroke-width': Math.max(1, contrib * 12), fill: 'none' }));
      const raced = e.mult > 1;
      const taggedRuns = raced && e.runs.some((rn) => rn.rung || rn.match_id);
      const children = [
        svgEl('title', null),
        svgEl('circle', { cx: X.board, cy: y, r, class: 'ezn-board-disc' }),
        // label to the LEFT of the disc, vertically centred, never on the circle.
        svgEl('text', { x: X.board + labelDX, y: y + 3, class: 'ezn-board-label', 'text-anchor': 'end' }, [clip(e.entry_id, 18)]),
        // representative loss INSIDE the disc.
        svgEl('text', { x: X.board, y: y + 3, class: 'ezn-board-loss', 'text-anchor': 'middle' }, [isNum(e.drift_loss) ? fmt(e.drift_loss, 0) : '—']),
      ];
      if (raced) {
        // rung-multiplicity badge to the RIGHT of the disc — makes it clear the
        // SAME entry was re-raced across rungs (not a random duplicate).
        children.push(svgEl('text', { x: X.board + r + 6, y: y + 3, class: 'ezn-board-mult', 'text-anchor': 'start' }, ['×' + e.mult]));
        // the EXPANSION: a small per-run stack (a sparkline + one row per run,
        // labelled by rung when present) revealed on hover/focus/click. It is
        // appended to THIS node group so it inherits the node's position; CSS
        // hides it until :hover / :focus-within / .ezn-board-open.
        children.push(perRunStack(e, X.board + r + 6, y, o));
      }
      const g = svgEl('g', {
        class: 'ezn-node ezn-board-node ' + cls + (raced ? ' ezn-board-raced' : '') + (raced ? ' ezn-board-expandable' : ''), 'data-cz': 'lc-board-node',
        'data-key': e.entry_id, 'data-mult': e.mult, 'data-tagged': taggedRuns ? '1' : '0',
        tabindex: (o.onEntry || raced) ? '0' : null,
        'aria-label': `${e.entry_id} drift loss ${isNum(e.drift_loss) ? fmt(e.drift_loss) : '—'}` + (raced ? ` · ${e.mult} per-run losses across rungs` : ''),
      }, children);
      const tt = g.childNodes[0];
      if (tt) tt.textContent = `${e.entry_id}: loss ${isNum(e.drift_loss) ? fmt(e.drift_loss) : '—'}`
        + (raced ? ` · ${e.mult} runs — ` + e.runs.map((rn) => (rn.rung ? rn.rung + ': ' : '') + (isNum(rn.drift_loss) ? fmt(rn.drift_loss, 1) : '—')).join(' · ') + ' (representative = full-board run)' : '')
        + (e.wall_clock_budget_exceeded ? ' · timed out' : '')
        + (e.pass_fail === 0 ? ' · failed' : e.pass_fail === 1 ? ' · passed' : '');
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

  rectNode(nodeLayer, X.agg, midY, 0.1 * w, 48, 'Σ loss', entries.length ? fmt(total, 0) : '—', 'ezn-neutral');

  edgeLayer.appendChild(svgEl('path', { d: flow(X.agg + 0.05 * w, midY, X.gate - 0.06 * w, midY), class: 'ezn-edge ' + (verdictClass(dec) === 'ezn-promoted' ? 'ezn-edge-good' : 'ezn-edge-bad'), fill: 'none' }));
  const gateSub = baseline ? 'no gate (seed)' : (isNum(o.deltaScalar) ? (o.deltaScalar >= 0 ? '+' : '') + fmt(o.deltaScalar, 1) + ' Δ' : dec);
  rectNode(nodeLayer, X.gate, midY, 0.12 * w, 48, baseline ? 'BASELINE' : 'GATE', gateSub, verdictClass(dec));

  const promoted = dec === 'promoted' || (baseline && o.promoted === true);
  // Class B: a PENDING candidate (in-flight / not yet raced — promoted == null,
  // no resolved decision) must NOT read "✕ dead branch / champion stands". Show
  // a non-terminal racing/awaiting-gate state instead.
  const pending = !baseline && !promoted && (dec === 'pending' || dec === 'running' || (o.promoted == null && (!dec || dec === 'running' || dec === 'pending')));
  let termLabel, termSub, termCls;
  if (baseline) { termLabel = 'seed'; termSub = 'defines floor'; termCls = 'ezn-baseline'; }
  else if (promoted) { termLabel = '♛ promoted'; termSub = 'new champion'; termCls = 'ezn-promoted'; }
  else if (pending) { termLabel = '⋯ racing'; termSub = 'awaiting gate'; termCls = 'ezn-running'; }
  else { termLabel = '✕ dead branch'; termSub = 'champion stands'; termCls = 'ezn-rejected'; }
  edgeLayer.appendChild(svgEl('path', { d: flow(X.gate + 0.06 * w, midY, X.term - 0.045 * w, midY), class: 'ezn-edge ' + (promoted ? 'ezn-edge-good' : pending ? 'ezn-edge-neutral' : 'ezn-edge-bad'), fill: 'none' }));
  rectNode(nodeLayer, X.term, midY, 0.1 * w, 48, termLabel, termSub, termCls);

  svg.appendChild(edgeLayer);
  svg.appendChild(nodeLayer);
  return svg;
}

// variants/F/views/scoring.js — Screen: PER-BOARD SCORING + DRILL-DOWN (theme 3).
//
// Three depths, each a graph, drilling deeper:
//
//   DEPTH 1 — a Sankey: candidate ─▶ per-board loss ─▶ aggregate scalar.
//             Each board's ribbon WIDTH = its contribution to the total
//             loss. Reuses the existing sankey.js layout engine.
//   DEPTH 2 — click a board node → it expands into an expectation +
//             per-judge sub-graph in the drawer (…/expectations, …/per-judge).
//   DEPTH 3 — a deeper link opens the transcript flow
//             (/api/conversation/{run_id}) as a turn-by-turn flow.
//
// Data: /api/generation/{e}/{g}/per-entry (depth 1),
//       /api/run/{e}/{g}/{entry}/expectations + .../per-judge (depth 2),
//       /api/conversation/{run_id} (depth 3).

import { el, svgEl, clearChildren } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { createSurface, createSurfaceControls } from '../diagram/surface.js';
import { ribbonPath } from '../diagram/primitives.js';
import { layoutSankey } from '../diagram/sankey.js';
import { perEntryRows } from '../model.js';
import { openDrawer } from '../chrome.js';
import { fmtScalar } from '../../../core/format.js';

const _entryCache = new Map();
const _entryLoading = new Set();
let _lastDigest = null;

export function resetScoringCaches() {
  _entryCache.clear(); _entryLoading.clear();
  _lastDigest = null;
}

export function renderScoring(ctx) {
  const { stage, state, params, chrome, repaint } = ctx;
  const epochId = params.epochId || (state.epochDef && state.epochDef.epoch_id) || null;
  const genId = params.genId;

  // Digest gate: gen id + the loaded per-board rows. No timestamps.
  const rowsPre = _entryCache.get(key(epochId, genId)) || null;
  const digest = JSON.stringify({
    epochId, genId,
    rows: rowsPre ? rowsPre.map((r) => [r.entryId, r.driftLoss, r.passFail, r.budgetExceeded]) : null,
  });
  if (digest === _lastDigest && stage.firstChild) return;
  _lastDigest = digest;

  clearChildren(stage);

  stage.appendChild(el('div', { class: 'cz-screen-head' }, [
    el('div', { class: 'cz-epoch-eyebrow' }, [
      'SCORING', el('span', { class: 'cz-mono' }, [genId || '—']),
      epochId ? el('span', { class: 'cz-tag cz-tag-open' }, [epochId]) : null,
    ]),
    el('h1', { class: 'cz-screen-title' }, ['Per-board scoring']),
    el('p', { class: 'cz-screen-sub' }, [
      'How the candidate scored on every board entry, as flow: candidate → per-board loss → aggregate scalar. '
      + 'Ribbon width is each board’s contribution. Click a board node to expand its expectations and per-judge '
      + 'losses; from there, open the transcript flow.',
    ]),
  ]));

  if (!genId) {
    stage.appendChild(el('div', { class: 'cz-empty' }, ['No candidate selected. Pick one from the lineage or environment map.']));
    return;
  }
  ensureEntries(epochId, genId, repaint);

  const rows = _entryCache.get(key(epochId, genId));
  if (rows == null) {
    stage.appendChild(el('div', { class: 'cz-empty' }, ['Loading per-board scores…']));
    return;
  }
  if (rows.length === 0) {
    stage.appendChild(el('div', { class: 'cz-empty' }, ['No board entries scored for this candidate yet.']));
    return;
  }

  stage.appendChild(buildScoringSankey(rows, epochId, genId, chrome));
}

function buildScoringSankey(rows, epochId, genId, chrome) {
  const total = rows.reduce((a, r) => a + (r.driftLoss || 0), 0) || 0.0001;

  // Stage "patch" = the candidate; "drift" = the board entries;
  // "gate" = the aggregate scalar. We reuse the 3-stage Sankey engine.
  const candNode = { id: 'cand', label: genId, sub: 'candidate', cls: 'cz-flow-patch', value: total };
  const boardNodes = rows.map((r) => {
    const cls = r.passFail === 1 ? 'cz-v-promoted' : (r.budgetExceeded ? 'cz-flow-worsened' : 'cz-v-rejected');
    const passLabel = r.passFail === 1 ? 'pass' : (r.passFail === 0 ? 'fail' : 'no predicate');
    const flags = r.budgetExceeded ? ' · timeout' : '';
    return {
      id: 'b:' + r.entryId, label: r.entryId,
      sub: fmtScalar(r.driftLoss) + ' · ' + passLabel + flags,
      cls, value: Math.max(0.0001, r.driftLoss || 0), ref: r,
    };
  });
  const aggNode = { id: 'agg', label: 'Σ ' + fmtScalar(total), sub: 'aggregate scalar', cls: 'cz-v-neutral', value: total };

  const links = [];
  for (const b of boardNodes) {
    links.push({ source: 'cand', target: b.id, value: b.value, cls: b.cls });
    links.push({ source: b.id, target: 'agg', value: b.value, cls: b.cls });
  }

  const layout = layoutSankey({
    patch: [candNode], drift: boardNodes, gate: [aggNode], links,
    colW: 170, colGap: 220, nodeW: 160, colHeight: Math.max(440, boardNodes.length * 60),
  });

  const surface = createSurface({
    width: Math.max(layout.box.w, 980), height: Math.max(layout.box.h, 380),
    ariaLabel: 'Per-board scoring Sankey',
  });
  const vp = surface.viewport;

  const headerY = 22;
  const headers = [
    { x: layout.nodes.find((n) => n.stage === 'patch')?.x ?? 0, t: 'CANDIDATE' },
    { x: layout.nodes.find((n) => n.stage === 'drift')?.x ?? 0, t: 'PER-BOARD LOSS' },
    { x: layout.nodes.find((n) => n.stage === 'gate')?.x ?? 0, t: 'AGGREGATE' },
  ];
  for (const h of headers) vp.appendChild(svgEl('text', { x: h.x, y: headerY, class: 'cz-sankey-col-head' }, [h.t]));

  const ribbonLayer = svgEl('g', { class: 'cz-ribbon-layer' });
  for (const l of layout.links) {
    ribbonLayer.appendChild(svgEl('path', {
      d: ribbonPath(l.sx, l.sy, l.tx, l.ty, l.hwS, l.hwT),
      class: 'cz-ribbon ' + (l.cls || ''), 'data-source': l.source, 'data-target': l.target,
    }));
  }
  vp.appendChild(ribbonLayer);

  const nodeLayer = svgEl('g', { class: 'cz-node-layer' });
  for (const n of layout.nodes) {
    const clickable = n.ref != null;
    const grp = svgEl('g', {
      class: 'cz-sankey-node ' + (n.cls || '') + (clickable ? ' cz-clickable' : ''),
      'data-id': n.id, 'data-cz': clickable ? 'scoring-board-node' : 'scoring-node',
      'data-key': n.id, tabindex: clickable ? '0' : null,
      role: clickable ? 'button' : 'group', 'aria-label': `${n.label} ${n.sub}`,
    }, [
      svgEl('rect', { x: n.x, y: n.y, width: n.w, height: n.h, rx: 6, class: 'cz-sankey-rect' }),
      svgEl('text', { x: n.x + 10, y: n.y + Math.min(n.h / 2, 18), class: 'cz-sankey-label' }, [clip(n.label, 22)]),
      n.sub ? svgEl('text', { x: n.x + 10, y: n.y + Math.min(n.h / 2, 18) + 14, class: 'cz-sankey-sub' }, [clip(n.sub, 26)]) : null,
    ]);
    grp.addEventListener('mouseenter', () => lightFlow(ribbonLayer, n.id));
    grp.addEventListener('mouseleave', () => unlightFlow(ribbonLayer));
    if (clickable) {
      const open = () => openBoardDrill(chrome, epochId, genId, n.ref);
      grp.addEventListener('click', open);
      grp.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') open(); });
    }
    nodeLayer.appendChild(grp);
  }
  vp.appendChild(nodeLayer);
  surface.fit(layout.box);

  return el('div', { class: 'cz-canvas-wrap' }, [createSurfaceControls(surface, el), surface.svg]);
}

function lightFlow(ribbonLayer, nodeId) {
  ribbonLayer.classList.add('cz-dimmed');
  for (const r of ribbonLayer.children) {
    if (r.getAttribute('data-source') === nodeId || r.getAttribute('data-target') === nodeId) r.classList.add('cz-lit');
  }
}
function unlightFlow(ribbonLayer) {
  ribbonLayer.classList.remove('cz-dimmed');
  for (const r of ribbonLayer.children) r.classList.remove('cz-lit');
}

// -- DEPTH 2: expand a board node into expectations + per-judge -------
async function openBoardDrill(chrome, epochId, genId, row) {
  const entryId = row.entryId;
  const body = el('div', { class: 'cz-drill' });
  body.appendChild(el('div', { class: 'cz-drill-head' }, [
    el('span', { class: 'cz-drill-entry cz-mono' }, [entryId]),
    el('span', { class: 'cz-board-weight' }, [row.driftLoss == null ? '—' : fmtScalar(row.driftLoss) + ' loss']),
  ]));

  // The expectation sub-graph slot.
  const expSlot = el('div', { class: 'cz-drill-block', 'data-cz': 'drill-expectations' }, [
    el('h4', { class: 'cz-drill-h' }, ['Expectations']),
    el('div', { class: 'cz-section-sub' }, ['Loading…']),
  ]);
  const judgeSlot = el('div', { class: 'cz-drill-block', 'data-cz': 'drill-judges' }, [
    el('h4', { class: 'cz-drill-h' }, ['Per-judge loss']),
    el('div', { class: 'cz-section-sub' }, ['Loading…']),
  ]);
  body.appendChild(expSlot);
  body.appendChild(judgeSlot);

  // DEPTH 3: a button into the transcript flow.
  if (row.runId) {
    body.appendChild(el('button', {
      type: 'button', class: 'cz-btn cz-btn-ghost', 'data-cz': 'drill-transcript-btn',
      onclick: () => openTranscriptFlow(chrome, row.runId, entryId),
    }, ['Open transcript flow →']));
  }

  openDrawer(chrome, 'Board · ' + entryId, body);

  // Fetch expectations.
  fetchJson('/api/run/' + p(epochId) + '/' + p(genId) + '/' + p(entryId) + '/expectations')
    .then((d) => paintExpectations(expSlot, d))
    .catch(() => paintExpectations(expSlot, null));
  // Fetch per-judge.
  fetchJson('/api/run/' + p(epochId) + '/' + p(genId) + '/' + p(entryId) + '/per-judge')
    .then((d) => paintJudges(judgeSlot, d))
    .catch(() => fetchJson('/api/generation/' + p(epochId) + '/' + p(genId) + '/per-judge')
      .then((d) => paintJudges(judgeSlot, d)).catch(() => paintJudges(judgeSlot, null)));
}

function paintExpectations(slot, data) {
  clearChildren(slot);
  slot.appendChild(el('h4', { class: 'cz-drill-h' }, ['Expectations']));
  const outcomes = data && Array.isArray(data.outcomes) ? data.outcomes : [];
  if (!outcomes.length) {
    slot.appendChild(el('div', { class: 'cz-section-sub' }, ['No expectation outcomes recorded.']));
    return;
  }
  // A tiny expectation → outcome sub-graph as a node row.
  const graph = el('div', { class: 'cz-mini-graph' });
  for (const o of outcomes) {
    const passed = o.passed === true;
    const cls = passed ? 'cz-v-promoted' : (o.passed === false ? 'cz-v-rejected' : 'cz-v-neutral');
    graph.appendChild(el('div', { class: 'cz-mini-node ' + cls }, [
      el('span', { class: 'cz-mini-kind' }, [String(o.kind || 'expectation')]),
      el('span', { class: 'cz-mini-arrow' }, ['→']),
      el('span', { class: 'cz-mini-verdict' }, [passed ? 'passed' : (o.passed === false ? 'failed' : '—')]),
      o.judge_name ? el('span', { class: 'cz-mini-judge cz-mono' }, [String(o.judge_name)]) : null,
      o.detail ? el('div', { class: 'cz-mini-detail' }, [String(o.detail)]) : null,
    ]));
  }
  slot.appendChild(graph);
}

function paintJudges(slot, data) {
  clearChildren(slot);
  slot.appendChild(el('h4', { class: 'cz-drill-h' }, ['Per-judge loss']));
  const judges = data && Array.isArray(data.judges) ? data.judges : [];
  if (!judges.length) {
    slot.appendChild(el('div', { class: 'cz-section-sub' }, ['No per-judge breakdown for this entry.']));
    return;
  }
  const max = Math.max(...judges.map((j) => Math.abs(j.weighted_loss || 0)), 0.0001);
  const bars = el('div', { class: 'cz-judge-bars' });
  for (const j of judges) {
    const wl = typeof j.weighted_loss === 'number' ? j.weighted_loss : 0;
    const pct = Math.max(2, (Math.abs(wl) / max) * 100);
    bars.appendChild(el('div', { class: 'cz-judge-row' }, [
      el('span', { class: 'cz-judge-name cz-mono' }, [String(j.judge_name || '?')]),
      el('div', { class: 'cz-judge-track' }, [
        el('div', { class: 'cz-judge-fill', style: 'width:' + pct + '%' }),
      ]),
      el('span', { class: 'cz-judge-val cz-mono' }, [fmtScalar(wl)]),
    ]));
  }
  slot.appendChild(bars);
}

// -- DEPTH 3: the transcript flow -------------------------------------
async function openTranscriptFlow(chrome, runId, entryId) {
  const body = el('div', { class: 'cz-transcript' });
  body.appendChild(el('p', { class: 'cz-brief-meta' }, [
    'Transcript flow for run ', el('span', { class: 'cz-mono' }, [String(runId)]), ' — turn by turn.',
  ]));
  const slot = el('div', { class: 'cz-flow-turns', 'data-cz': 'transcript-turns' }, [
    el('div', { class: 'cz-section-sub' }, ['Loading transcript…']),
  ]);
  body.appendChild(slot);
  openDrawer(chrome, 'Transcript · ' + entryId, body);

  try {
    const d = await fetchJson('/api/conversation/' + encodeURIComponent(runId));
    paintTranscript(slot, d);
  } catch {
    paintTranscript(slot, null);
  }
}

function paintTranscript(slot, data) {
  clearChildren(slot);
  const turns = data && (Array.isArray(data.turns) ? data.turns
    : (Array.isArray(data.messages) ? data.messages : (Array.isArray(data) ? data : [])));
  if (!turns || !turns.length) {
    slot.appendChild(el('div', { class: 'cz-section-sub' }, ['No transcript turns available for this run.']));
    return;
  }
  turns.forEach((t, i) => {
    const role = t.role || t.speaker || t.kind || 'turn';
    const text = t.text || t.content || t.summary || (typeof t === 'string' ? t : '');
    slot.appendChild(el('div', { class: 'cz-turn cz-turn-' + String(role).toLowerCase().replace(/[^a-z]/g, '') }, [
      el('div', { class: 'cz-turn-role' }, [String(role)]),
      el('div', { class: 'cz-turn-body' }, [clip(String(text), 600)]),
    ]));
    if (i < turns.length - 1) slot.appendChild(el('div', { class: 'cz-turn-arrow' }, ['↓']));
  });
}

// -- helpers ----------------------------------------------------------
function key(e, g) { return e + '/' + g; }
function p(s) { return encodeURIComponent(String(s == null ? '' : s)); }
function clip(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

async function ensureEntries(epochId, genId, repaint) {
  if (!epochId || !genId) return;
  const k = key(epochId, genId);
  if (_entryCache.has(k) || _entryLoading.has(k)) return;
  _entryLoading.add(k);
  try {
    const d = await fetchJson('/api/generation/' + encodeURIComponent(epochId) + '/' + encodeURIComponent(genId) + '/per-entry');
    _entryCache.set(k, perEntryRows(d));
  } catch {
    _entryCache.set(k, []);
  } finally {
    _entryLoading.delete(k);
    if (typeof repaint === 'function') repaint();
  }
}

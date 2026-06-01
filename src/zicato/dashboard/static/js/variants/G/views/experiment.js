// variants/G/views/experiment.js — L3 candidate (experiment).
//
// The causal story PATCH → DRIFT → GATE told as C's Sankey flow, the
// verdict + go/no-go gate, and D's per-board dot-plot of the candidate's
// loss profile with a clean drill-down into one entry (theme 3, depths
// 1→2→3). The patch diff is a secondary drawer.
//
// A BUG #1 FIX (drill-down flashing / constant refresh): the SELECTED
// entry lives in MODULE SCOPE (`selectedEntryId`). The whole view is
// digest-gated, and the digest folds in the selection + whether the
// selected entry's drill data has loaded — but NOT timestamps. So a
// heartbeat tick leaves the digest unchanged and `renderExperiment`
// returns early: the drilldown panel is NOT rebuilt every tick. It is
// rebuilt only when the selection changes or its data arrives.

import { el } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { state } from '../../../core/state.js';
import { panel, readouts, empty, loading, drawer, fmt, signed } from '../components/ui.js';
import { valueDotPlot } from '../svg.js';
import { instrumentPanel } from '../components/drilldown.js';
import { renderSankey } from '../components/diagram.js';
import { layoutSankey } from '../diagram/sankey.js';
import { experimentById, decisionOf, isBaselineSeed, perEntryRows } from '../model.js';
import { navigate } from '../router.js';

const perEntry = new Map();    // e/g -> per-entry payload
const drift = new Map();       // genId -> movements
const diffData = new Map();    // e/g -> diff text
const gateData = new Map();    // e/c/ch -> gate payload
const expectCache = new Map(); // e/g/entry -> expectations
const judgeCache = new Map();  // e/g/entry -> per-judge
const loadingSet = new Set();
let selectedEntryId = null;    // MODULE-SCOPE selection (A bug #1 fix)
let _ctxKey = null;            // epoch/gen the selection belongs to
let _lastDigest = null;

export function resetExperimentCache() {
  perEntry.clear(); drift.clear(); diffData.clear(); gateData.clear();
  expectCache.clear(); judgeCache.clear(); loadingSet.clear();
  selectedEntryId = null; _ctxKey = null; _lastDigest = null;
}

// Test seam: read/set the module-scope selection directly.
export function _selectedEntryId() { return selectedEntryId; }
export function _setSelectedEntry(id) { selectedEntryId = id; }

function enc(v) { return encodeURIComponent(v == null ? '' : String(v)); }

async function ensureData(epochId, genId, parentId, repaint) {
  const pkey = epochId + '/' + genId;
  if (!perEntry.has(pkey) && !loadingSet.has('p' + pkey)) {
    loadingSet.add('p' + pkey);
    try { perEntry.set(pkey, await fetchJson('/api/generation/' + enc(epochId) + '/' + enc(genId) + '/per-entry')); }
    catch { perEntry.set(pkey, { entries: [] }); }
    loadingSet.delete('p' + pkey);
    if (repaint) repaint();
  }
  if (!drift.has(genId) && !loadingSet.has('d' + genId)) {
    loadingSet.add('d' + genId);
    try { drift.set(genId, await fetchJson('/api/drift-movements/' + enc(genId))); }
    catch { drift.set(genId, { movements: [] }); }
    loadingSet.delete('d' + genId);
    if (repaint) repaint();
  }
  const dkey = epochId + '/' + genId;
  if (!diffData.has(dkey) && !loadingSet.has('f' + dkey)) {
    loadingSet.add('f' + dkey);
    try { diffData.set(dkey, await fetchJson('/api/files/' + enc(epochId) + '/' + enc(genId) + '/diff')); }
    catch { diffData.set(dkey, null); }
    loadingSet.delete('f' + dkey);
    if (repaint) repaint();
  }
  if (parentId) {
    const gkey = epochId + '/' + parentId + '/' + genId;
    if (!gateData.has(gkey) && !loadingSet.has('gt' + gkey)) {
      loadingSet.add('gt' + gkey);
      try { gateData.set(gkey, await fetchJson('/api/round/' + enc(epochId) + '/' + enc(parentId) + '/' + enc(genId) + '/gate')); }
      catch { gateData.set(gkey, null); }
      loadingSet.delete('gt' + gkey);
      if (repaint) repaint();
    }
  }
}

async function ensureDrill(epochId, genId, entryId, repaint) {
  if (!entryId) return;
  const k = epochId + '/' + genId + '/' + entryId;
  if (!expectCache.has(k) && !loadingSet.has('e' + k)) {
    loadingSet.add('e' + k);
    try { expectCache.set(k, await fetchJson('/api/run/' + enc(epochId) + '/' + enc(genId) + '/' + enc(entryId) + '/expectations')); }
    catch { expectCache.set(k, { outcomes: [] }); }
    loadingSet.delete('e' + k);
    if (repaint) repaint();
  }
  if (!judgeCache.has(k) && !loadingSet.has('j' + k)) {
    loadingSet.add('j' + k);
    try { judgeCache.set(k, await fetchJson('/api/run/' + enc(epochId) + '/' + enc(genId) + '/' + enc(entryId) + '/per-judge')); }
    catch { judgeCache.set(k, { judges: [] }); }
    loadingSet.delete('j' + k);
    if (repaint) repaint();
  }
}

function scoresMap(epochId, genId) {
  const rows = perEntryRows(perEntry.get(epochId + '/' + genId));
  const m = new Map();
  for (const r of rows) m.set(r.entryId, r);
  return m;
}

export function experimentDigest(params) {
  const epochId = params.epochId || (state.epochDef && state.epochDef.epoch_id);
  const genId = params.genId;
  const exp = experimentById(state, genId);
  const pkey = epochId + '/' + genId;
  const rows = perEntryRows(perEntry.get(pkey));
  const dkey = selectedEntryId ? epochId + '/' + genId + '/' + selectedEntryId : null;
  return JSON.stringify({
    epochId, genId,
    decision: decisionOf(exp),
    outcome: exp && exp.outcome ? [exp.outcome.scalar_score_delta, exp.outcome.drift_loss_delta, exp.outcome.pass_rate_delta] : null,
    entries: rows.map((r) => [r.entryId, r.driftLoss, r.passFail, r.timeout]),
    drift: drift.has(genId),
    diff: diffData.has(dkey ? epochId + '/' + genId : epochId + '/' + genId),
    selected: selectedEntryId,
    // fold in whether the SELECTED entry's drill data has loaded, so the
    // panel rebuilds when its data arrives — but a heartbeat tick (no
    // new data, same selection) is a no-op (A bug #1 fix).
    expLoaded: dkey ? expectCache.has(dkey) : false,
    judgeLoaded: dkey ? judgeCache.has(dkey) : false,
  });
}

// -- gate panel (from /api/round/.../gate, falling back to outcome) ---
function gatePanel(epochId, genId, parentId, exp, decision) {
  const gkey = parentId ? epochId + '/' + parentId + '/' + genId : null;
  const gate = gkey ? gateData.get(gkey) : null;
  const o = (exp && exp.outcome) || {};

  let verdictCls = 'is-pending', big = 'PENDING', icon = '◌';
  if (decision === 'promoted') { verdictCls = 'is-improve'; big = 'PROMOTE'; icon = '▲'; }
  else if (decision === 'rejected') { verdictCls = 'is-regress'; big = 'REJECT'; icon = '▼'; }

  let rules;
  if (gate && Array.isArray(gate.rules) && gate.rules.length) {
    rules = gate.rules.map((r) => ({
      mark: r.status === 'pass' ? 'pass' : r.status === 'fail' ? 'fail' : 'na',
      text: [el('b', null, [r.label || r.id]), r.detail ? el('span', { class: 'g-readout-foot' }, ['  ' + r.detail]) : null],
    }));
  } else {
    const margin = (state.scoring && state.scoring.margin) || 0.01;
    const dScalar = o.scalar_score_delta;
    rules = [
      { mark: typeof dScalar !== 'number' ? 'na' : (dScalar <= -margin ? 'pass' : 'fail'),
        text: ['Scalar margin — Δscalar = ', el('b', null, [signed(dScalar)])] },
      { mark: o.pass_rate_delta == null ? 'na' : (o.pass_rate_delta >= 0 ? 'pass' : 'fail'),
        text: ['Pass-rate monotonicity — Δpass = ', el('b', null, [signed(o.pass_rate_delta, 3)])] },
      { mark: decision === 'promoted' ? 'pass' : 'na', text: ['Namespace monotonicity'] },
    ];
  }

  const rulesNode = el('div', { class: 'g-gate-rules' }, rules.map((r) =>
    el('div', { class: 'g-gate-rule' }, [
      el('span', { class: 'g-gate-rule-mark is-' + r.mark }, [r.mark === 'pass' ? '✓' : r.mark === 'fail' ? '✗' : '·']),
      el('span', { class: 'g-gate-rule-text' }, r.text),
    ])));
  const reason = (gate && gate.reason) || o.rejection_reason;
  if (decision === 'rejected' && reason) {
    rulesNode.appendChild(el('div', { class: 'g-readout-foot is-regress g-gate-reason' }, ['↳ ' + reason]));
  }

  return el('div', { class: 'g-gate' }, [
    el('div', { class: 'g-gate-verdict ' + verdictCls }, [
      el('div', { class: 'g-gate-icon' }, [icon]),
      el('div', { class: 'g-gate-big' }, [big]),
      el('div', { class: 'g-readout-foot' }, ['promote gate']),
    ]),
    rulesNode,
  ]);
}

// -- causal flow Sankey: patch → drift → gate -------------------------
function causalFlow(epochId, genId, exp, decision) {
  const dm = drift.get(genId);
  const moves = dm && Array.isArray(dm.movements) ? dm.movements : [];
  const hyp = (exp && exp.hypothesis) || {};
  const points = Array.isArray(hyp.modulating) ? hyp.modulating
    : (typeof hyp.modulating === 'string' ? hyp.modulating.split(/[;,]/).map((s) => s.trim()).filter(Boolean) : []);

  const patch = (points.length ? points : ['patch']).slice(0, 4).map((p, i) => ({
    id: 'p' + i, label: String(p).split('/').pop().slice(0, 18), sub: 'mutation', cls: 'cz-v-baseline', value: 1,
  }));
  const driftNodes = (moves.length ? moves : [{ kind: 'drift', delta: 1 }]).slice(0, 5).map((m, i) => ({
    id: 'd' + i, label: m.kind || 'drift', sub: signed(m.delta, 0),
    cls: (m.delta || 0) < 0 ? 'cz-v-promoted' : 'cz-v-rejected', value: Math.max(1, Math.abs(m.delta || 1)),
  }));
  const gateNode = [{
    id: 'gate', label: decision === 'promoted' ? 'PROMOTE' : decision === 'rejected' ? 'REJECT' : 'GATE',
    sub: 'verdict', cls: decision === 'promoted' ? 'cz-v-promoted' : decision === 'rejected' ? 'cz-v-rejected' : 'cz-v-neutral',
    value: driftNodes.reduce((a, n) => a + n.value, 0) || 1,
  }];
  const links = [];
  for (const p of patch) for (const d of driftNodes) links.push({ source: p.id, target: d.id, value: d.value / patch.length, cls: d.cls });
  for (const d of driftNodes) links.push({ source: d.id, target: 'gate', value: d.value, cls: d.cls });

  const layout = layoutSankey({ patch, drift: driftNodes, gate: gateNode, links, colHeight: 300, colW: 150, colGap: 130, nodeW: 130 });
  return el('div', { class: 'g-flow-wrap' }, [renderSankey(layout)]);
}

// -- per-board dot-plot + drill-down (theme 3) ------------------------
function scoringSection(epochId, genId, isSeed, parentId, repaint) {
  const def = state.epochDef;
  const board = def && Array.isArray(def.board) ? def.board : [];
  const scores = scoresMap(epochId, genId);
  const loaded = perEntry.has(epochId + '/' + genId);

  const rows = perEntryRows(perEntry.get(epochId + '/' + genId));
  // champion reference line (parent's best/avg) for the value dot-plot.
  let ref = null;
  if (parentId) {
    const prows = perEntryRows(perEntry.get(epochId + '/' + parentId));
    const vals = prows.map((r) => r.driftLoss).filter((v) => typeof v === 'number');
    if (vals.length) ref = { value: vals.reduce((a, b) => a + b, 0) / vals.length, label: 'champion avg' };
  }

  const onSelect = (d) => {
    const id = d.id != null ? d.id : d.label;
    selectedEntryId = (selectedEntryId === id) ? null : id;
    if (selectedEntryId) ensureDrill(epochId, genId, selectedEntryId, repaint);
    if (repaint) repaint();
  };

  const bodyKids = [];
  if (!loaded && !rows.length) {
    bodyKids.push(loading('Reading per-board loss profile'));
  } else if (!rows.length) {
    bodyKids.push(empty('No per-entry results recorded.'));
  } else {
    bodyKids.push(el('div', { class: 'g-dotplot-wrap' }, [
      valueDotPlot({
        width: 520, labelWidth: 200,
        items: rows.map((r) => ({
          id: r.entryId, label: r.entryId, value: r.driftLoss,
          pass: r.passFail, timeout: r.timeout, ran: r.runId != null || r.driftLoss != null,
        })),
        reference: ref,
        onClick: onSelect,
      }),
    ]));
  }

  // depth 2: instrument panel for the selected entry. Rebuilt ONLY when
  // the digest (which folds in selection + drill-load) changed.
  if (selectedEntryId) {
    const k = epochId + '/' + genId + '/' + selectedEntryId;
    const entry = board.find((b) => b.id === selectedEntryId) || { id: selectedEntryId };
    const score = scores.get(selectedEntryId) || null;
    bodyKids.push(instrumentPanel({
      entry, score,
      expectations: expectCache.has(k) ? expectCache.get(k) : null,
      perJudge: judgeCache.has(k) ? judgeCache.get(k) : null,
      runId: score && score.runId,
      onOpenRun: (rid) => { if (rid) navigate('run', { runId: rid }); },
      onClose: () => { selectedEntryId = null; if (repaint) repaint(); },
    }));
  }

  return panel({
    title: 'Per-board scoring',
    sub: 'absolute drift loss per board entry · left = lower = better' + (ref ? ' · the rule line is the champion average' : '') + ' · click a row to drill in',
    body: bodyKids,
  });
}

function extractDiffText(d) {
  if (!d) return '';
  if (typeof d === 'string') return d;
  if (typeof d.diff === 'string') return d.diff;
  if (Array.isArray(d.files)) return d.files.map((f) => (f.path ? '=== ' + f.path + ' ===\n' : '') + (f.diff || f.content || '')).join('\n\n');
  if (typeof d.unified === 'string') return d.unified;
  return '';
}

function diffDrawer(epochId, genId) {
  const d = diffData.get(epochId + '/' + genId);
  let body;
  if (d == null) body = loading('Loading diff');
  else {
    const text = extractDiffText(d);
    body = text
      ? el('pre', { class: 'g-diff' }, [el('code', { class: 'g-mono' }, [text])])
      : el('div', { class: 'g-empty' }, ['No diff recorded for this generation (seed generations carry no patch).']);
  }
  return drawer({ title: 'Patch diff', sub: 'the exact change — secondary to the verdict above', openByDefault: false, body });
}

export function renderExperiment(root, params, repaint) {
  const epochId = params.epochId || (state.epochDef && state.epochDef.epoch_id);
  const genId = params.genId;

  // Invalidate the module-scope selection ONLY on a candidate change
  // (route change), never on a heartbeat — A bug #1.
  const ctxKey = epochId + '/' + genId;
  if (ctxKey !== _ctxKey) { selectedEntryId = null; _ctxKey = ctxKey; }

  const exp = experimentById(state, genId);
  const decision = decisionOf(exp);
  const isSeed = isBaselineSeed(exp);
  const parentId = exp && exp.parent_generation_id;

  if (epochId && genId) ensureData(epochId, genId, parentId, repaint);

  const digest = experimentDigest(params);
  if (digest === _lastDigest && root.firstChild) return;
  _lastDigest = digest;
  root.textContent = '';

  root.appendChild(el('div', { class: 'g-pagehead' }, [
    el('h1', null, ['Candidate']),
    el('span', { class: 'g-pagehead-sub g-mono' }, [(epochId || '—') + ' · ' + (genId || '—')]),
  ]));

  if (!epochId || !genId) { root.appendChild(empty('Select a generation from the epoch view.')); return; }
  if (state.epochDef == null) { root.appendChild(loading('Loading candidate')); return; }

  const o = (exp && exp.outcome) || {};

  if (isSeed) {
    root.appendChild(el('div', { class: 'g-section' }, [
      readouts([
        { label: 'role', value: 'SEED (v0)', tone: 'live', foot: 'absolute baseline' },
        { label: 'board entries', value: scoresMap(epochId, genId).size },
      ]),
    ]));
    root.appendChild(el('div', { class: 'g-section' }, [scoringSection(epochId, genId, true, parentId, repaint)]));
    return;
  }

  root.appendChild(el('div', { class: 'g-section' }, [
    readouts([
      { label: 'verdict', value: decision ? decision.toUpperCase() : 'PENDING', tone: decision === 'promoted' ? 'improve' : decision === 'rejected' ? 'regress' : 'caution' },
      { label: 'Δ scalar', value: signed(o.scalar_score_delta), tone: typeof o.scalar_score_delta === 'number' && o.scalar_score_delta < 0 ? 'improve' : (o.scalar_score_delta > 0 ? 'regress' : null), foot: 'lower is better' },
      { label: 'Δ drift loss', value: signed(o.drift_loss_delta), tone: typeof o.drift_loss_delta === 'number' && o.drift_loss_delta < 0 ? 'improve' : (o.drift_loss_delta > 0 ? 'regress' : null) },
      { label: 'Δ pass rate', value: signed(o.pass_rate_delta), tone: typeof o.pass_rate_delta === 'number' && o.pass_rate_delta > 0 ? 'improve' : (o.pass_rate_delta < 0 ? 'regress' : null) },
    ]),
  ]));

  // causal lifecycle: patch → drift → gate (C flow) + the gate verdict.
  root.appendChild(el('div', { class: 'g-section' }, [
    panel({
      title: 'Lifecycle — patch → drift → gate',
      sub: 'the cause (mutation points) flows through the drift that moved into the gate verdict',
      accent: decision === 'promoted' ? 'improve' : decision === 'rejected' ? 'regress' : null,
      body: causalFlow(epochId, genId, exp, decision),
    }),
  ]));

  root.appendChild(el('div', { class: 'g-section' }, [
    panel({
      title: 'The verdict',
      sub: 'promote gate — three rules in order, short-circuiting',
      accent: decision === 'promoted' ? 'improve' : decision === 'rejected' ? 'regress' : null,
      body: gatePanel(epochId, genId, parentId, exp, decision),
    }),
  ]));

  root.appendChild(el('div', { class: 'g-section' }, [scoringSection(epochId, genId, false, parentId, repaint)]));

  root.appendChild(diffDrawer(epochId, genId));
}

// variants/A/views/experiment.js — L3 experiment TELEMETRY READOUT.
//
// The causal story CODE CHANGE → DRIFT MOVEMENT → VERDICT, told
// VISUAL-FIRST: lead with the verdict + a go/no-go gate panel + the
// drift that moved as charts. The patch DIFF is SECONDARY — a
// collapsible drawer, never the overwhelming top wall.
//
// Seed (v0) shows its absolute baseline board results, not a comparison.
//
// Data: /api/matchup-grid/{e}/{champion}/{challenger} (per-entry A/B),
// /api/drift-movements/{g} (what drift moved), /api/files/{e}/{g}/diff
// (the patch — drawer only), and state.epochDef.experiments (verdict +
// hypothesis + deltas).

import { el } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { state } from '../../../core/state.js';
import { panel, readouts, empty, loading, chip, deltaBar } from '../components/instruments.js';
import { href } from '../router.js';

const grid = new Map();      // key e/c/ch -> matchup grid
const drift = new Map();     // genId -> movements
const diff = new Map();      // e/g -> diff text
const loadingSet = new Set();
let diffOpen = false;

export function resetExperimentCache() { grid.clear(); drift.clear(); diff.clear(); loadingSet.clear(); }

function expFor(epochId, genId) {
  const def = state.epochDef;
  if (!def || !Array.isArray(def.experiments)) return null;
  return def.experiments.find((e) => e.generation_id === genId) || null;
}

function decisionOf(exp) {
  const o = exp && exp.outcome;
  if (!o || typeof o !== 'object') return null;
  const d = String(o.tournament_decision || o.decision || '').toLowerCase();
  if (d.includes('promot')) return 'promoted';
  if (d.includes('reject')) return 'rejected';
  return d || null;
}

async function ensureData(epochId, genId, parentId, repaint) {
  const gkey = epochId + '/' + (parentId || '') + '/' + genId;
  if (!grid.has(gkey) && !loadingSet.has('g' + gkey)) {
    loadingSet.add('g' + gkey);
    try { grid.set(gkey, await fetchJson('/api/matchup-grid/' + enc(epochId) + '/' + enc(parentId || genId) + '/' + enc(genId))); }
    catch { grid.set(gkey, { entry_grid: [], scalar: null }); }
    loadingSet.delete('g' + gkey);
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
  if (!diff.has(dkey) && !loadingSet.has('f' + dkey)) {
    loadingSet.add('f' + dkey);
    try {
      const r = await fetchJson('/api/files/' + enc(epochId) + '/' + enc(genId) + '/diff');
      diff.set(dkey, r);
    } catch { diff.set(dkey, null); }
    loadingSet.delete('f' + dkey);
    if (repaint) repaint();
  }
}

function enc(v) { return encodeURIComponent(v == null ? '' : String(v)); }
function fmt(v, d = 3) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(d) : '—'; }
function signed(v, d = 3) { return (typeof v === 'number' && isFinite(v)) ? (v > 0 ? '+' : '') + v.toFixed(d) : '—'; }

// -- go/no-go gate ----------------------------------------------------
function gatePanel(exp, decision) {
  const o = (exp && exp.outcome) || {};
  const dScalar = o.scalar_score_delta;
  const dDrift = o.drift_loss_delta;
  const margin = (state.scoring && state.scoring.margin) || 0.01;

  let verdict = decision;
  let verdictCls = 'is-pending', big = 'PENDING', icon = '◌';
  if (decision === 'promoted') { verdictCls = 'is-go'; big = 'PROMOTE'; icon = '▲'; }
  else if (decision === 'rejected') { verdictCls = 'is-stop'; big = 'REJECT'; icon = '✗'; }

  // Rule evaluation (deterministic, mirrors SELECTION.md §3.2).
  const r1pass = typeof dScalar === 'number' && dScalar <= -margin;
  const rules = [
    {
      mark: typeof dScalar !== 'number' ? 'na' : (r1pass ? 'pass' : 'fail'),
      text: ['Scalar margin — challenger loss must beat champion by ≥ ',
        el('b', null, [margin.toFixed(2)]), '. Δscalar = ',
        el('b', null, [signed(dScalar)])],
    },
    {
      mark: o.pass_rate_delta == null ? 'na' : (o.pass_rate_delta >= 0 ? 'pass' : 'fail'),
      text: ['Pass-rate monotonicity — no entry the champion passed may fail. Δpass = ',
        el('b', null, [signed(o.pass_rate_delta, 3)])],
    },
    {
      mark: o.rejection_reason && /namespace/i.test(String(o.rejection_reason)) ? 'fail' : (decision === 'promoted' ? 'pass' : 'na'),
      text: ['Namespace monotonicity — no tracked namespace moved in its worse direction.'],
    },
  ];

  const rulesNode = el('div', { class: 'mcA-gate-rules' }, rules.map((r) =>
    el('div', { class: 'mcA-gate-rule' }, [
      el('span', { class: 'mcA-gate-rule-mark is-' + r.mark },
        [r.mark === 'pass' ? '✓' : r.mark === 'fail' ? '✗' : '·']),
      el('span', { class: 'mcA-gate-rule-text' }, r.text),
    ])));

  if (decision === 'rejected' && o.rejection_reason) {
    rulesNode.appendChild(el('div', { class: 'mcA-readout-foot', style: 'margin-top:4px;color:var(--mc-stop)' },
      ['↳ ' + o.rejection_reason]));
  }

  return el('div', { class: 'mcA-gate' }, [
    el('div', { class: 'mcA-gate-verdict ' + verdictCls }, [
      el('div', { class: 'mcA-gate-icon ' + (verdictCls === 'is-go' ? 'is-go' : verdictCls === 'is-stop' ? 'is-stop' : '') }, [icon]),
      el('div', { class: 'mcA-gate-big ' + verdictCls }, [big]),
      el('div', { class: 'mcA-readout-foot' }, ['promote gate']),
    ]),
    rulesNode,
  ]);
}

// -- drift movement chart (diverging bars) ----------------------------
function driftChart(genId) {
  const dm = drift.get(genId);
  if (!dm) return loading('Reading drift movements');
  const moves = Array.isArray(dm.movements) ? dm.movements : [];
  if (!moves.length) {
    return empty(dm.note || 'No drift movement recorded (index may be unbuilt).');
  }
  const maxAbs = Math.max(1, ...moves.map((m) => Math.abs(m.delta || 0)));
  const tbl = el('table', { class: 'mcA-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['drift kind']),
    el('th', null, ['champion']),
    el('th', null, ['challenger']),
    el('th', { style: 'width:200px' }, ['movement']),
  ])]));
  const tb = el('tbody');
  for (const m of moves) {
    const improved = (m.delta || 0) < 0;
    tb.appendChild(el('tr', null, [
      el('td', { class: 'mono' }, [m.kind]),
      el('td', { class: 'mono' }, [String(m.champion_count)]),
      el('td', { class: 'mono' }, [String(m.challenger_count)]),
      el('td', null, [
        el('div', { style: 'display:flex;align-items:center;gap:8px' }, [
          deltaBar(m.delta, maxAbs),
          el('span', { class: 'mono ' + (improved ? 'mcA-tag-good' : (m.delta > 0 ? 'mcA-tag-bad' : '')), style: 'font-size:11px' },
            [signed(m.delta, 0)]),
        ]),
      ]),
    ]));
  }
  tbl.appendChild(tb);
  return tbl;
}

// -- A/B per-entry grid -----------------------------------------------
function abGrid(epochId, genId, parentId, isSeed) {
  const gkey = epochId + '/' + (parentId || '') + '/' + genId;
  const g = grid.get(gkey);
  if (!g) return loading('Reading per-entry loss files');
  const rows = Array.isArray(g.entry_grid) ? g.entry_grid : [];
  if (!rows.length) return empty('No per-entry results recorded.');

  const tbl = el('table', { class: 'mcA-table' });
  if (isSeed) {
    tbl.appendChild(el('thead', null, [el('tr', null, [
      el('th', null, ['board entry']), el('th', null, ['drift loss']), el('th', null, ['pass']),
    ])]));
    const tb = el('tbody');
    for (const r of rows) {
      tb.appendChild(el('tr', null, [
        el('td', { class: 'mono' }, [r.entry_id]),
        el('td', { class: 'mono' }, [fmt(r.child_drift_loss != null ? r.child_drift_loss : r.parent_drift_loss)]),
        el('td', null, [passMark(r.child_pass != null ? r.child_pass : r.parent_pass)]),
      ]));
    }
    tbl.appendChild(tb);
    return tbl;
  }
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['board entry']),
    el('th', null, ['champion']),
    el('th', null, ['challenger']),
    el('th', null, ['Δ']),
    el('th', null, ['verdict']),
  ])]));
  const tb = el('tbody');
  for (const r of rows) {
    const improved = typeof r.delta === 'number' && r.delta < 0;
    tb.appendChild(el('tr', null, [
      el('td', { class: 'mono' }, [r.entry_id]),
      el('td', { class: 'mono' }, [fmt(r.parent_drift_loss), ' ', passMark(r.parent_pass)]),
      el('td', { class: 'mono' }, [fmt(r.child_drift_loss), ' ', passMark(r.child_pass)]),
      el('td', { class: 'mono ' + (improved ? 'mcA-tag-good' : (r.delta > 0 ? 'mcA-tag-bad' : '')) }, [signed(r.delta)]),
      el('td', null, [r.verdict ? chip(r.verdict, improved ? 'go' : (r.delta > 0 ? 'stop' : 'idle')) : '—']),
    ]));
  }
  tbl.appendChild(tb);
  return tbl;
}

function passMark(v) {
  if (v == null) return el('span', { class: 'mcA-readout-foot' }, ['']);
  return el('span', { class: v ? 'mcA-tag-good' : 'mcA-tag-bad' }, [v ? '✓' : '✗']);
}

// -- patch diff drawer (secondary, collapsible) -----------------------
function diffDrawer(epochId, genId) {
  const dkey = epochId + '/' + genId;
  const card = el('div', { class: 'mcA-brief-card' });
  const head = el('div', { class: 'mcA-brief-head' }, [
    el('div', null, [
      el('span', { class: 'mcA-panel-title' }, ['Patch diff']),
      el('span', { class: 'mcA-panel-sub' }, ['  the exact change — secondary to the verdict above']),
    ]),
    el('span', { class: 'mcA-brief-toggle' }, [diffOpen ? 'collapse ∧' : 'expand ∨']),
  ]);
  const body = el('div', { class: 'mcA-brief-body' });
  if (!diffOpen) body.setAttribute('hidden', 'true');
  const d = diff.get(dkey);
  if (d == null) body.appendChild(loading('Loading diff'));
  else {
    const text = extractDiffText(d);
    if (text) body.appendChild(el('pre', { style: 'margin:0' }, [el('code', { class: 'mono' }, [text])]));
    else body.appendChild(el('div', { class: 'mcA-brief-empty' }, ['No diff recorded for this generation (seed generations carry no patch).']));
  }
  head.addEventListener('click', () => {
    diffOpen = !diffOpen;
    if (diffOpen) body.removeAttribute('hidden'); else body.setAttribute('hidden', 'true');
    const t = head.childNodes[head.childNodes.length - 1];
    if (t) t.textContent = diffOpen ? 'collapse ∧' : 'expand ∨';
  });
  card.appendChild(head); card.appendChild(body);
  return card;
}

function extractDiffText(d) {
  if (!d) return '';
  if (typeof d === 'string') return d;
  if (typeof d.diff === 'string') return d.diff;
  if (Array.isArray(d.files)) {
    return d.files.map((f) => (f.path ? '=== ' + f.path + ' ===\n' : '') + (f.diff || f.content || '')).join('\n\n');
  }
  if (typeof d.unified === 'string') return d.unified;
  return '';
}

export function renderExperiment(root, params, repaint) {
  const epochId = params.epochId || (state.epochDef && state.epochDef.epoch_id);
  const genId = params.genId;
  root.textContent = '';

  root.appendChild(el('div', { class: 'mcA-pagehead' }, [
    el('h1', null, ['Experiment']),
    el('span', { class: 'mcA-pagehead-sub mono' }, [(epochId || '—') + ' · ' + (genId || '—')]),
  ]));

  if (!epochId || !genId) { root.appendChild(empty('Select a generation from the epoch view.')); return; }

  const exp = expFor(epochId, genId);
  const decision = decisionOf(exp);
  const isSeed = exp && !exp.parent_generation_id && exp.outcome == null;
  const parentId = exp && exp.parent_generation_id;

  ensureData(epochId, genId, parentId, repaint);

  if (state.epochDef == null) { root.appendChild(loading('Loading experiment')); return; }

  // --- visual summary first: verdict + headline deltas -------------
  const o = (exp && exp.outcome) || {};
  if (isSeed) {
    root.appendChild(el('div', { style: 'margin-bottom:16px' }, [
      readouts([
        { label: 'role', value: 'SEED (v0)', tone: 'live', foot: 'absolute baseline' },
        { label: 'board entries', value: ((grid.get(epochId + '/' + (parentId || '') + '/' + genId) || {}).entry_grid || []).length },
      ]),
    ]));
    root.appendChild(panel({
      title: 'Baseline board results',
      sub: 'the seed has no comparison — these are its absolute drift losses',
      accent: 'live',
      body: abGrid(epochId, genId, parentId, true),
    }));
    root.appendChild(el('div', { style: 'margin-top:16px' }, [
      panel({ title: 'Drift profile', sub: 'baseline drift counts', body: driftChart(genId) }),
    ]));
    return;
  }

  // verdict readout strip
  root.appendChild(el('div', { style: 'margin-bottom:16px' }, [
    readouts([
      { label: 'verdict', value: decision ? decision.toUpperCase() : 'PENDING', tone: decision === 'promoted' ? 'go' : decision === 'rejected' ? 'stop' : 'warn' },
      { label: 'Δ scalar', value: signed(o.scalar_score_delta), tone: typeof o.scalar_score_delta === 'number' && o.scalar_score_delta < 0 ? 'go' : (o.scalar_score_delta > 0 ? 'stop' : null), foot: 'lower is better' },
      { label: 'Δ drift loss', value: signed(o.drift_loss_delta), tone: typeof o.drift_loss_delta === 'number' && o.drift_loss_delta < 0 ? 'go' : (o.drift_loss_delta > 0 ? 'stop' : null) },
      { label: 'Δ pass rate', value: signed(o.pass_rate_delta), tone: typeof o.pass_rate_delta === 'number' && o.pass_rate_delta > 0 ? 'go' : (o.pass_rate_delta < 0 ? 'stop' : null) },
    ]),
  ]));

  // the causal story: hypothesis (cause) -> gate (verdict)
  const hyp = (exp && exp.hypothesis) || {};
  const causeBody = [];
  if (hyp.core_idea) causeBody.push(el('p', { style: 'color:var(--mc-text);margin:0 0 8px' }, [hyp.core_idea]));
  if (hyp.why) causeBody.push(el('p', { class: 'mcA-readout-foot' }, ['why · ' + hyp.why]));
  if (Array.isArray(hyp.modulating) && hyp.modulating.length) {
    causeBody.push(el('div', { style: 'margin-top:8px' }, hyp.modulating.map((m) => chip(m, 'idle'))));
  }
  if (!causeBody.length) causeBody.push(empty('No hypothesis recorded.'));

  root.appendChild(el('div', { class: 'mcA-grid mcA-grid-2', style: 'margin-bottom:16px' }, [
    panel({ title: 'The change (cause)', sub: 'what the proposer modulated and why', body: causeBody }),
    panel({ title: 'The verdict', sub: 'promote gate — go / no-go', accent: decision === 'promoted' ? 'go' : decision === 'rejected' ? 'stop' : null, body: gatePanel(exp, decision) }),
  ]));

  // the effect: drift that moved
  root.appendChild(el('div', { style: 'margin-bottom:16px' }, [
    panel({ title: 'Drift movement (effect)', sub: 'champion → challenger drift-kind deltas · green = improved', body: driftChart(genId) }),
  ]));

  // per-entry A/B grid
  root.appendChild(el('div', { style: 'margin-bottom:16px' }, [
    panel({ title: 'Per-entry A/B', sub: 'champion vs challenger across the board', body: abGrid(epochId, genId, parentId, false) }),
  ]));

  // patch diff — drawer, secondary
  root.appendChild(diffDrawer(epochId, genId));
}

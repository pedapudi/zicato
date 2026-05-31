// variants/B/views/experiment.js — the Experiment view (the notebook entry).
//
// Visual-first, narrative order: the BET first (the hypothesis as a
// pull-quote, with what it predicted), then the RESULT (an elegant
// champion→challenger slopegraph + a per-entry diverging chart of how drift
// moved) and a clear VERDICT (the gate, read as a sentence with its fired
// rule). The patch DIFF is a TASTEFUL, SECONDARY, collapsible block at the
// end — the cause is shown, but it does not lead with an overwhelming wall.
//
// The seed (v0) has no parent champion: instead of red "no champion"
// errors, it shows its absolute baseline board results honestly.
//
// Data: state.epochDef.experiments (hypothesis + outcome + patches),
// /api/matchup-grid/{e}/{champ}/{chall}, /api/round/.../gate,
// /api/drift-movements/{g}, /api/files/{e}/{g}/diff.

import { el, clearChildren } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import { bRouter } from '../router.js';
import { registerBView } from '../shell.js';
import {
  makeCache, experimentFor, findGen, genId, parentId, decisionOf, isBaselineSeed,
  hypothesisText, hypothesisPrediction, hypothesisRationale, outcomeNum, currentEpochId, scalarOf,
} from '../lib/data.js';
import { section, note, pullQuote, verdictBadge, stat } from '../lib/prose.js';
import { slopegraph, divergingBars, fmtSigned, fmtNum, fin } from '../lib/charts.js';

let _gridCache = null;
let _gateCache = null;
let _driftCache = null;
let _diffCache = null;
function repaint() {
  const host = document.getElementById('vb-page');
  if (host && bRouter.current().view === 'experiment') renderExperiment(host, bRouter.current());
}
function caches() {
  if (!_gridCache) _gridCache = makeCache(repaint);
  if (!_gateCache) _gateCache = makeCache(repaint);
  if (!_driftCache) _driftCache = makeCache(repaint);
  if (!_diffCache) _diffCache = makeCache(repaint);
  return { grid: _gridCache, gate: _gateCache, drift: _driftCache, diff: _diffCache };
}
export function resetExperimentView() {
  _gridCache = null; _gateCache = null; _driftCache = null; _diffCache = null;
}

function epochIdFor(gen, exp) {
  const fromGen = gen && (gen.epoch_id || gen.epochId);
  if (fromGen) return String(fromGen);
  if (exp && exp.epoch_id) return String(exp.epoch_id);
  return currentEpochId();
}

// The verdict, read as a sentence + its fired rule.
function verdictBlock(gate, exp, seed) {
  const decision = (gate && gate.decision) || decisionOf(exp)
    || (seed ? 'baseline' : 'open');
  const verdict = decision === 'promoted' ? 'promoted'
    : decision === 'rejected' ? 'rejected'
      : decision === 'deferred' ? 'deferred'
        : seed ? 'baseline' : 'open';
  const rules = (gate && Array.isArray(gate.rules)) ? gate.rules : [];
  const fired = rules.find((r) => r && r.fired);
  const reason = (gate && typeof gate.reason === 'string' && gate.reason.trim())
    ? gate.reason.trim() : '';

  const lede = verdict === 'promoted'
    ? 'The challenger cleared the gate and was promoted to champion.'
    : verdict === 'rejected'
      ? 'The challenger was rejected — the change did not earn promotion.'
      : verdict === 'deferred'
        ? 'The decision was deferred.'
        : verdict === 'baseline'
          ? 'This is the seed — the absolute baseline the lineage measures against.'
          : 'The verdict is still forming.';

  const kids = [
    el('div', { class: 'vb-verdict-head' }, [verdictBadge(verdict, { large: true })]),
    el('p', { class: 'vb-verdict-lede' }, [lede]),
  ];
  if (fired) {
    kids.push(el('p', { class: 'vb-verdict-rule' }, [
      'Decided by ', el('span', { class: 'vb-mono vb-regress' }, [String(fired.label || fired.id)]),
      fired.detail ? el('span', { class: 'vb-muted' }, [' — ' + fired.detail]) : null,
    ].filter(Boolean)));
  } else if (reason) {
    kids.push(el('p', { class: 'vb-verdict-rule vb-muted' }, [reason]));
  }
  // The rule ladder as a compact, glyph-led list (read top to bottom).
  if (rules.length) {
    kids.push(el('ol', { class: 'vb-gate-ladder' }, rules.map((r) => {
      const s = String(r.status || 'unknown');
      const tone = s === 'pass' ? 'improve' : s === 'fail' ? 'regress'
        : s === 'not_reached' ? 'neutral' : 'caution';
      const glyph = s === 'pass' ? '✓' : s === 'fail' ? '✗'
        : s === 'not_reached' ? '·' : '○';
      return el('li', { class: `vb-gate-rule vb-${tone}` + (r.fired ? ' vb-gate-fired' : '') }, [
        el('span', { class: 'vb-gate-glyph', 'aria-hidden': 'true' }, [glyph]),
        el('span', { class: 'vb-gate-name' }, [String(r.label || r.id)]),
        el('span', { class: 'vb-gate-status' }, [s.replace(/_/g, ' ')]),
      ]);
    })));
  }
  return el('div', { class: 'vb-verdict-block' }, kids);
}

// Collapsible, secondary diff block.
function diffBlock(epochId, gid, diffData) {
  const det = el('details', { class: 'vb-diff' }, [
    el('summary', { class: 'vb-diff-summary' }, [
      el('span', { class: 'vb-diff-marker', 'aria-hidden': 'true' }, ['▸']),
      'The change — patch diff',
      el('span', { class: 'vb-muted vb-diff-hint' }, [' (the cause, shown in full)']),
    ]),
  ]);
  const body = el('div', { class: 'vb-diff-body' });
  if (diffData === undefined) {
    body.appendChild(note('running', { label: 'Reading patch diff' }));
  } else if (!diffData || diffData.__broken) {
    body.appendChild(note('broken', { reason: 'patch diff unavailable' }));
  } else {
    const files = Array.isArray(diffData.files) ? diffData.files
      : (Array.isArray(diffData.diffs) ? diffData.diffs : []);
    const unified = typeof diffData.diff === 'string' ? diffData.diff
      : (typeof diffData.unified === 'string' ? diffData.unified : '');
    if (files.length) {
      for (const f of files) {
        const text = typeof f.diff === 'string' ? f.diff
          : (typeof f.unified === 'string' ? f.unified : (typeof f.patch === 'string' ? f.patch : ''));
        body.appendChild(el('div', { class: 'vb-diff-file' }, [
          el('p', { class: 'vb-diff-path vb-mono' }, [String(f.path || f.name || f.file || 'patch')]),
          diffPre(text),
        ]));
      }
    } else if (unified) {
      body.appendChild(diffPre(unified));
    } else {
      body.appendChild(note('empty', { label: 'No textual diff recorded for this generation.' }));
    }
  }
  det.appendChild(body);
  return det;
}

// Render a unified diff as colorised, monospace lines (no innerHTML).
function diffPre(text) {
  const lines = String(text || '').split('\n');
  const pre = el('pre', { class: 'vb-diff-pre' });
  for (const ln of lines) {
    const cls = ln.startsWith('+') && !ln.startsWith('+++') ? 'vb-diff-add'
      : ln.startsWith('-') && !ln.startsWith('---') ? 'vb-diff-del'
        : ln.startsWith('@@') ? 'vb-diff-hunk' : 'vb-diff-ctx';
    pre.appendChild(el('span', { class: 'vb-diff-line ' + cls }, [ln || ' ']));
  }
  return pre;
}

// The patch mutations from the experiment record (id + op + rationale).
function mutationsBlock(exp) {
  const patches = (exp && exp.patches && typeof exp.patches === 'object') ? exp.patches : {};
  const ids = Object.keys(patches);
  if (!ids.length) return null;
  return el('div', { class: 'vb-muts' }, ids.map((mid) => {
    const p = patches[mid] || {};
    return el('div', { class: 'vb-mut' }, [
      el('span', { class: 'vb-mono vb-mut-id' }, [mid]),
      p.op ? el('span', { class: 'vb-tag' }, [String(p.op)]) : null,
      p.rationale ? el('p', { class: 'vb-mut-rationale' }, [String(p.rationale)]) : null,
    ].filter(Boolean));
  }));
}

export function renderExperiment(host, route) {
  if (!host) return;
  const gid = route && route.params && route.params.generationId;
  clearChildren(host);
  if (!gid) {
    host.appendChild(el('h1', { class: 'vb-page-title' }, ['Experiment']));
    host.appendChild(note('empty', { label: 'No experiment selected', detail: 'Open one from the lineage or a chapter.' }));
    return;
  }

  const gen = findGen(gid);
  const exp = experimentFor(gid);
  const champId = parentId(gen) || (exp && exp.parent_generation_id) || null;
  const seed = champId == null;
  const epochId = epochIdFor(gen, exp);
  const c = caches();

  // Lazy fetches.
  if (epochId) {
    c.grid.ensure(gid, '/api/matchup-grid/' + [epochId, champId || gid, gid].map(encodeURIComponent).join('/'));
    if (!seed) c.gate.ensure(gid, '/api/round/' + [epochId, champId, gid].map(encodeURIComponent).join('/') + '/gate');
    c.drift.ensure(gid, '/api/drift-movements/' + encodeURIComponent(gid));
    c.diff.ensure(gid, '/api/files/' + [epochId, gid].map(encodeURIComponent).join('/') + '/diff');
  }
  const grid = c.grid.get(gid);
  const gate = seed ? null : c.gate.get(gid);
  const drift = c.drift.get(gid);
  const diffData = c.diff.get(gid);

  const hyp = hypothesisText(exp);
  const prediction = hypothesisPrediction(exp);
  const rationale = hypothesisRationale(exp);

  // --- Lead: the bet ---
  host.appendChild(el('div', { class: 'vb-exp-lead' }, [
    el('p', { class: 'vb-eyebrow' }, [
      'Generation ', el('span', { class: 'vb-mono' }, [String(gid)]),
      seed ? el('span', { class: 'vb-tag' }, ['seed · v0']) : champId
        ? el('span', { class: 'vb-muted' }, [' vs champion ', el('span', { class: 'vb-mono' }, [String(champId)])]) : null,
    ].filter(Boolean)),
    hyp
      ? pullQuote(hyp, { class: 'vb-exp-bet', attribution: seed ? 'the seed instructions' : 'the bet' })
      : pullQuote(seed ? 'The baseline agent, unmodified — the reference every later bet is measured against.'
          : '(no hypothesis was recorded for this generation)', { class: 'vb-exp-bet vb-muted' }),
    prediction ? el('p', { class: 'vb-exp-prediction' }, [
      el('span', { class: 'vb-tag' }, ['predicted']), ' ', prediction,
    ]) : null,
    rationale ? el('p', { class: 'vb-exp-rationale vb-muted' }, [rationale]) : null,
  ].filter(Boolean)));

  // --- The verdict (lead with the answer, per the spec) ---
  if (!seed) {
    host.appendChild(section('Verdict', [verdictBlock(gate, exp, seed)], {
      sub: 'What the gate decided, and the rule that decided it.',
    }));
  } else {
    host.appendChild(section('The baseline', [
      el('p', { class: 'vb-muted' }, [
        'This is v0 — there is no champion to compare against. Its board results below are the '
        + 'absolute reference every later generation is scored against.',
      ]),
    ]));
  }

  // --- Drift movement (the effect) ---
  const driftBody = [];
  // Per-entry A/B from the matchup grid (or absolute baseline for the seed).
  if (grid === undefined) {
    driftBody.push(note('running', { label: 'Reading per-entry results' }));
  } else if (grid && Array.isArray(grid.entry_grid) && grid.entry_grid.length) {
    const rows = grid.entry_grid;
    if (seed) {
      // Absolute baseline board results — child_drift_loss per entry.
      driftBody.push(el('div', { class: 'vb-entry-baseline' }, rows.map((r) => {
        const v = fin(r.child_drift_loss) ? r.child_drift_loss : r.parent_drift_loss;
        return el('div', {
          class: 'vb-entry-row vb-clickable', role: 'button', tabindex: '0',
          'aria-label': `entry ${r.entry_id}`,
          onclick: () => bRouter.go('run', r.entry_id, gid),
          onkeydown: (ev) => { if (ev && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); bRouter.go('run', r.entry_id, gid); } },
        }, [
          el('span', { class: 'vb-entry-name' }, [String(r.entry_id)]),
          el('span', { class: 'vb-entry-val vb-mono' }, [fmtNum(v, 2)]),
        ]);
      })));
    } else {
      const items = rows.map((r) => ({
        label: r.entry_id,
        delta: fin(r.delta) ? r.delta
          : (fin(r.child_drift_loss) && fin(r.parent_drift_loss) ? r.child_drift_loss - r.parent_drift_loss : null),
        title: `${r.entry_id}: champion ${fmtNum(r.parent_drift_loss, 2)} → challenger ${fmtNum(r.child_drift_loss, 2)}`,
        onClick: () => bRouter.go('run', r.entry_id, gid),
      }));
      driftBody.push(el('p', { class: 'vb-fig-lead' }, [
        'Per board entry, drift loss moving from champion to challenger. ',
        el('span', { class: 'vb-improve' }, ['left = improved']), ', ',
        el('span', { class: 'vb-regress' }, ['right = worsened']), '. Click an entry to read its run.',
      ]));
      driftBody.push(divergingBars(items, { digits: 2 }));
    }
  } else if (grid) {
    driftBody.push(note('empty', { label: 'No per-entry results recorded yet' }));
  }

  // Drift-kind composition movement.
  if (drift && Array.isArray(drift.movements) && drift.movements.length) {
    const items = drift.movements.slice(0, 8).map((m) => ({
      label: m.kind,
      delta: fin(m.delta) ? m.delta : null,
      verdict: m.direction === 'improved' ? 'improve' : m.direction === 'worsened' ? 'regress' : 'neutral',
      title: `${m.kind}: ${m.champion_count} → ${m.challenger_count}`,
    }));
    driftBody.push(el('p', { class: 'vb-fig-lead vb-fig-lead-sub' }, ['Which drift kinds moved (event-count delta):']));
    driftBody.push(divergingBars(items, { digits: 0 }));
  } else if (drift && drift.note) {
    driftBody.push(el('p', { class: 'vb-muted vb-fig-lead-sub' }, [String(drift.note)]));
  }

  host.appendChild(section(seed ? 'Board results' : 'Drift movement',
    driftBody.length ? driftBody : [note('empty', { label: 'No movement recorded.' })], {
      sub: seed ? 'The absolute drift loss this baseline produced per board entry.'
        : 'How the agent behaved differently, by board entry and by drift kind.',
    }));

  // --- Scalar slopegraph (the headline numbers, champion → challenger) ---
  if (!seed && grid && grid.scalar && (fin(grid.scalar.parent) || fin(grid.scalar.child))) {
    const sc = grid.scalar;
    const dPass = outcomeNum(exp, 'pass_rate_delta');
    const series = [{
      label: 'scalar loss',
      from: fin(sc.parent) ? sc.parent : null,
      to: fin(sc.child) ? sc.child : null,
      verdict: fin(sc.delta) ? (sc.delta < 0 ? 'improve' : sc.delta > 0 ? 'regress' : 'neutral') : 'neutral',
    }];
    host.appendChild(section('Scalar', [
      slopegraph(series, {
        fromLabel: 'champion', toLabel: 'challenger', digits: 3,
        caption: 'Lower is better — the tournament ranks by this scalar loss.',
      }),
      el('div', { class: 'vb-exp-stats' }, [
        fin(sc.delta) ? stat(fmtSigned(sc.delta, 3), 'Δ scalar', { tone: sc.delta < 0 ? 'improve' : 'regress' }) : null,
        dPass != null ? stat(fmtSigned(dPass, 2), 'Δ pass rate', { tone: dPass > 0 ? 'improve' : dPass < 0 ? 'regress' : 'neutral' }) : null,
        (gate && gate.primary_driver && gate.primary_driver.judge)
          ? stat(gate.primary_driver.judge, 'primary driver', { tone: 'neutral' }) : null,
      ].filter(Boolean)),
    ], { sub: 'The headline movement, champion to challenger.' }));
  }

  // --- The change (secondary, collapsible) ---
  const muts = mutationsBlock(exp);
  host.appendChild(section('The change', [
    muts || null,
    diffBlock(epochId, gid, diffData),
  ].filter(Boolean), {
    sub: 'The patch that caused all of the above — the mutation points it edited, with the full diff.',
  }));
}

registerBView('experiment', renderExperiment);

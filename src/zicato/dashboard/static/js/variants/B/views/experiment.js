// variants/B/views/experiment.js — the Experiment view (the notebook entry).
//
// This is the richest entry: it carries THREE of the four enrichment themes.
//
//   Theme 1 — candidate lifecycle. The entry reads as a life-story: a running
//   MARGIN TIMELINE down the side (conceived → patched → ran the board →
//   judged → verdict), the hypothesis (`hypothesis_core_idea`) set as a
//   pull-quote, the rejection reason set as a second, contrasting pull-quote,
//   and a hand-drawn-feeling GENEALOGY figure (v0 → {v1, v2}) with the
//   champion crowned.
//
//   Theme 3 — per-board scoring + drill-down. Each board entry the candidate
//   ran is a prose row with a small INLINE dot-plot (loss) and a pass/fail
//   glyph, bound to /api/generation/{e}/{g}/per-entry. Clicking a row opens a
//   footnote/ASIDE in place — its expectation outcomes (…/expectations) and
//   per-judge losses (…/per-judge). A "read the transcript" link drills to the
//   run dialogue (depth 3). Three depths: per-entry list → one entry aside →
//   the run.
//
// The seed (v0) has no parent champion: instead of red "no champion" errors,
// it shows its absolute baseline board results honestly.

import { el, clearChildren } from '../../../core/dom.js';
import { bRouter } from '../router.js';
import { registerBView } from '../shell.js';
import {
  makeCache, experimentFor, findGen, parentId, decisionOf,
  hypothesisText, hypothesisPrediction, hypothesisRationale, currentEpochId,
  lineageNodes, gauntlet, lifecycleSteps,
} from '../lib/data.js';
import { section, note, pullQuote, verdictBadge, stat } from '../lib/prose.js';
import {
  slopegraph, fmtSigned, fmtNum, fin, marginTimeline, genealogy, dotPlot,
} from '../lib/charts.js';

let _perEntryCache = null;
let _gateCache = null;
let _diffCache = null;
let _expectCache = null;
let _judgeCache = null;
let _openEntries = null; // Set of entry ids whose aside is expanded.
function repaint() {
  const host = document.getElementById('vb-page');
  if (host && bRouter.current().view === 'experiment') renderExperiment(host, bRouter.current());
}
function caches() {
  if (!_perEntryCache) _perEntryCache = makeCache(repaint);
  if (!_gateCache) _gateCache = makeCache(repaint);
  if (!_diffCache) _diffCache = makeCache(repaint);
  if (!_expectCache) _expectCache = makeCache(repaint);
  if (!_judgeCache) _judgeCache = makeCache(repaint);
  if (!_openEntries) _openEntries = new Set();
  return {
    perEntry: _perEntryCache, gate: _gateCache, diff: _diffCache,
    expect: _expectCache, judge: _judgeCache, open: _openEntries,
  };
}
export function resetExperimentView() {
  _perEntryCache = null; _gateCache = null; _diffCache = null;
  _expectCache = null; _judgeCache = null; _openEntries = null;
}

function epochIdFor(gen, exp) {
  const fromGen = gen && (gen.epoch_id || gen.epochId);
  if (fromGen) return String(fromGen);
  if (exp && exp.epoch_id) return String(exp.epoch_id);
  return currentEpochId();
}

// The rejection reason for this challenger, from the gauntlet record.
function rejectionReasonFor(gid, exp) {
  const g = gauntlet();
  for (const r of g.rounds) {
    if (r.challenger === String(gid) && r.reason) return r.reason;
  }
  const o = exp && exp.outcome;
  if (o && typeof o.rejection_reason === 'string' && o.rejection_reason.trim()) return o.rejection_reason.trim();
  return '';
}

// --- the verdict, read as a sentence + its fired rule ----------------------
function verdictBlock(gate, exp, seed) {
  const decision = (gate && gate.decision) || decisionOf(exp) || (seed ? 'baseline' : 'open');
  const verdict = decision === 'promoted' ? 'promoted'
    : decision === 'rejected' ? 'rejected'
      : decision === 'deferred' ? 'deferred'
        : seed ? 'baseline' : 'open';
  const rules = (gate && Array.isArray(gate.rules)) ? gate.rules : [];
  const fired = rules.find((r) => r && r.fired);
  const reason = (gate && typeof gate.reason === 'string' && gate.reason.trim()) ? gate.reason.trim() : '';

  const lede = verdict === 'promoted' ? 'The challenger cleared the gate and was promoted to champion.'
    : verdict === 'rejected' ? 'The challenger was rejected — the change did not earn promotion.'
      : verdict === 'deferred' ? 'The decision was deferred.'
        : verdict === 'baseline' ? 'This is the seed — the absolute baseline the lineage measures against.'
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
  if (rules.length) {
    kids.push(el('ol', { class: 'vb-gate-ladder' }, rules.map((r) => {
      const s = String(r.status || 'unknown');
      const tone = s === 'pass' ? 'improve' : s === 'fail' ? 'regress' : s === 'not_reached' ? 'neutral' : 'caution';
      const glyph = s === 'pass' ? '✓' : s === 'fail' ? '✗' : s === 'not_reached' ? '·' : '○';
      return el('li', { class: `vb-gate-rule vb-${tone}` + (r.fired ? ' vb-gate-fired' : '') }, [
        el('span', { class: 'vb-gate-glyph', 'aria-hidden': 'true' }, [glyph]),
        el('span', { class: 'vb-gate-name' }, [String(r.label || r.id)]),
        el('span', { class: 'vb-gate-status' }, [s.replace(/_/g, ' ')]),
      ]);
    })));
  }
  return el('div', { class: 'vb-verdict-block' }, kids);
}

// --- Theme 3: one per-entry scoring row, with an in-place drill-down aside --
function scoreRow(entry, gid, epochId, c) {
  const entryId = String(entry.entry_id != null ? entry.entry_id : entry.id);
  const loss = fin(entry.drift_loss) ? entry.drift_loss : null;
  const pass = entry.pass_fail === 1 ? true : entry.pass_fail === 0 ? false : null;
  const exceeded = entry.wall_clock_budget_exceeded === true;
  const open = c.open.has(entryId);

  const passGlyph = pass === true ? '✓' : pass === false ? '✗' : '·';
  const passTone = pass === true ? 'improve' : pass === false ? 'regress' : 'neutral';

  const header = el('div', {
    class: 'vb-score-row-head vb-clickable', role: 'button', tabindex: '0',
    'aria-expanded': open ? 'true' : 'false', 'aria-label': `scoring for ${entryId}`,
  }, [
    el('span', { class: 'vb-score-marker', 'aria-hidden': 'true' }, [open ? '▾' : '▸']),
    el('span', { class: 'vb-score-name vb-mono' }, [entryId]),
    el('span', { class: 'vb-score-fig' }, [
      dotPlot(loss, { pass, max: undefined, ariaLabel: `drift loss ${fmtNum(loss, 1)}` }),
    ]),
    el('span', { class: 'vb-score-loss vb-mono' }, [loss == null ? '—' : fmtNum(loss, 1)]),
    el('span', { class: `vb-score-pass vb-${passTone}` }, [
      el('span', { 'aria-hidden': 'true' }, [passGlyph]),
      el('span', { class: 'vb-score-pass-label' }, [pass === true ? 'pass' : pass === false ? 'fail' : 'no predicate']),
    ]),
    exceeded ? el('span', { class: 'vb-score-flag vb-caution' }, ['⏱ over budget']) : null,
  ].filter(Boolean));

  const toggle = () => {
    if (c.open.has(entryId)) c.open.delete(entryId); else c.open.add(entryId);
    repaint();
  };
  header.addEventListener('click', toggle);
  header.addEventListener('keydown', (ev) => { if (ev && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); toggle(); } });

  const kids = [header];
  if (open) kids.push(entryAside(entryId, gid, epochId, c));
  return el('div', { class: 'vb-score-row' + (open ? ' vb-score-open' : '') }, kids);
}

// Depth 2: the footnote/aside — expectation outcomes + per-judge losses.
function entryAside(entryId, gid, epochId, c) {
  const base = '/api/run/' + [epochId, gid, entryId].map(encodeURIComponent).join('/');
  if (epochId) {
    c.expect.ensure(entryId, base + '/expectations');
    c.judge.ensure(entryId, base + '/per-judge');
  }
  const expect = c.expect.get(entryId);
  const judge = c.judge.get(entryId);

  const blocks = [];

  // Expectation outcomes.
  blocks.push(el('p', { class: 'vb-aside-label' }, ['Expectation outcomes']));
  if (expect === undefined) {
    blocks.push(note('running', { label: 'Reading expectations' }));
  } else if (!expect || expect.__broken) {
    blocks.push(note('broken', { reason: 'expectations unavailable' }));
  } else {
    const outcomes = Array.isArray(expect.outcomes) ? expect.outcomes : [];
    if (!outcomes.length) {
      blocks.push(note('empty', { label: 'No expectation outcomes recorded' }));
    } else {
      blocks.push(el('ul', { class: 'vb-aside-outcomes' }, outcomes.map((o) => {
        const passed = o.passed === true;
        const tone = passed ? 'improve' : o.passed === false ? 'regress' : 'neutral';
        return el('li', { class: `vb-aside-outcome vb-${tone}` }, [
          el('span', { class: 'vb-aside-outcome-glyph', 'aria-hidden': 'true' }, [passed ? '✓' : o.passed === false ? '✗' : '·']),
          el('span', { class: 'vb-aside-outcome-kind' }, [String(o.kind || 'expectation')]),
          o.judge_name ? el('span', { class: 'vb-mono vb-muted' }, [String(o.judge_name)]) : null,
          o.detail ? el('span', { class: 'vb-aside-outcome-detail vb-muted' }, [String(o.detail)]) : null,
          fin(o.score) ? el('span', { class: 'vb-mono' }, [fmtNum(o.score, 2)]) : null,
        ].filter(Boolean));
      })));
    }
  }

  // Per-judge losses for this entry.
  blocks.push(el('p', { class: 'vb-aside-label' }, ['Per-judge loss']));
  if (judge === undefined) {
    blocks.push(note('running', { label: 'Reading per-judge detail' }));
  } else if (!judge || judge.__broken) {
    blocks.push(note('broken', { reason: 'per-judge detail unavailable' }));
  } else {
    const judges = Array.isArray(judge.judges) ? judge.judges : [];
    if (!judges.length) {
      blocks.push(el('p', { class: 'vb-muted vb-aside-nojudge' }, ['No process judges scored this entry.']));
    } else {
      blocks.push(el('ul', { class: 'vb-aside-judges' }, judges.map((j) => el('li', { class: 'vb-aside-judge' }, [
        el('span', { class: 'vb-mono vb-aside-judge-name' }, [String(j.judge_name || 'judge')]),
        el('span', { class: 'vb-aside-judge-loss vb-mono' }, [
          'loss ', fmtNum(j.weighted_loss != null ? j.weighted_loss : j.raw_loss, 1),
        ]),
        fin(j.weight) ? el('span', { class: 'vb-muted' }, ['×' + j.weight]) : null,
      ].filter(Boolean)))));
    }
  }

  // Depth 3 — the transcript.
  blocks.push(el('p', { class: 'vb-aside-deeper' }, [
    el('a', {
      class: 'vb-link-arrow', href: '#/B/run/' + [entryId, gid].map(encodeURIComponent).join('/'),
      onclick: (ev) => { if (ev && ev.preventDefault) ev.preventDefault(); bRouter.go('run', entryId, gid); },
    }, ['Read the run transcript →']),
  ]));

  return el('aside', { class: 'vb-score-aside', 'aria-label': `detail for ${entryId}` }, blocks);
}

// --- secondary, collapsible diff -------------------------------------------
function diffBlock(diffData) {
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
    const files = Array.isArray(diffData.files) ? diffData.files : (Array.isArray(diffData.diffs) ? diffData.diffs : []);
    const unified = typeof diffData.diff === 'string' ? diffData.diff : (typeof diffData.unified === 'string' ? diffData.unified : '');
    if (files.length) {
      for (const f of files) {
        const t = typeof f.diff === 'string' ? f.diff : (typeof f.unified === 'string' ? f.unified : (typeof f.patch === 'string' ? f.patch : ''));
        body.appendChild(el('div', { class: 'vb-diff-file' }, [
          el('p', { class: 'vb-diff-path vb-mono' }, [String(f.path || f.name || f.file || 'patch')]),
          diffPre(t),
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

// Theme 1: the lineage as a hand-drawn genealogy, this generation highlighted.
function genealogyBlock(gid) {
  const g = gauntlet();
  const crown = g.championLineage.length ? g.championLineage[g.championLineage.length - 1] : null;
  const nodes = lineageNodes().map((n) => ({
    id: n.id, label: n.label || n.id, parentId: n.parentId, verdict: n.verdict,
    crowned: crown != null && n.id === String(crown), live: n.live,
  }));
  return genealogy(nodes, {
    onSelect: (id) => bRouter.go('experiment', id),
    caption: [
      'Fig. ', el('em', null, ['lineage']),
      '. The seed branches into its challengers; ',
      el('span', { class: 'vb-gen-crown-inline', 'aria-hidden': 'true' }, ['♔']),
      ' marks the reigning champion. Click a node to read its entry.',
    ],
  });
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

  // Lazy fetches: per-entry scoring (theme 3), gate (theme 1 climax), diff.
  if (epochId) {
    c.perEntry.ensure(gid, '/api/generation/' + [epochId, gid].map(encodeURIComponent).join('/') + '/per-entry');
    if (!seed) c.gate.ensure(gid, '/api/round/' + [epochId, champId, gid].map(encodeURIComponent).join('/') + '/gate');
    c.diff.ensure(gid, '/api/files/' + [epochId, gid].map(encodeURIComponent).join('/') + '/diff');
  }
  const perEntry = c.perEntry.get(gid);
  const gate = seed ? null : c.gate.get(gid);
  const diffData = c.diff.get(gid);

  const hyp = hypothesisText(exp);
  const prediction = hypothesisPrediction(exp);
  const rationale = hypothesisRationale(exp);
  const decision = (gate && gate.decision) || decisionOf(exp) || null;
  const rejection = !seed ? rejectionReasonFor(gid, exp) : '';

  // --- The notebook-entry frame: a margin timeline + the lead, side by side.
  const lead = el('div', { class: 'vb-exp-lead' }, [
    el('p', { class: 'vb-eyebrow' }, [
      'Generation ', el('span', { class: 'vb-mono' }, [String(gid)]),
      seed ? el('span', { class: 'vb-tag' }, ['seed · baseline']) : champId
        ? el('span', { class: 'vb-muted' }, [' vs champion ', el('span', { class: 'vb-mono' }, [String(champId)])]) : null,
    ].filter(Boolean)),
    hyp
      ? pullQuote(hyp, { class: 'vb-exp-bet', attribution: seed ? 'the seed instructions' : 'the hypothesis' })
      : pullQuote(seed ? 'The baseline agent, unmodified — the reference every later bet is measured against.'
          : '(no hypothesis was recorded for this generation)', { class: 'vb-exp-bet vb-muted' }),
    prediction ? el('p', { class: 'vb-exp-prediction' }, [el('span', { class: 'vb-tag' }, ['predicted']), ' ', prediction]) : null,
    rationale ? el('p', { class: 'vb-exp-rationale vb-muted' }, [rationale]) : null,
    // The rejection reason as a contrasting pull-quote (theme 1).
    rejection
      ? pullQuote(rejection, { class: 'vb-exp-rejection', attribution: 'why it was rejected' })
      : null,
  ].filter(Boolean));

  host.appendChild(el('div', { class: 'vb-exp-entry' }, [
    el('div', { class: 'vb-exp-margin' }, [
      el('p', { class: 'vb-exp-margin-title' }, ['Life of the candidate']),
      marginTimeline(lifecycleSteps(exp, decision, seed), { ariaLabel: `lifecycle of ${gid}` }),
    ]),
    el('div', { class: 'vb-exp-lead-col' }, [lead]),
  ]));

  // --- The verdict (lead with the answer) ---
  if (!seed) {
    host.appendChild(section('Verdict', [verdictBlock(gate, exp, seed)], {
      sub: 'What the gate decided, and the rule that decided it.',
    }));
  } else {
    host.appendChild(section('The baseline', [
      el('p', { class: 'vb-muted' }, [
        'This is the seed — there is no champion to compare against. Its board results below are the '
        + 'absolute reference every later generation is scored against.',
      ]),
    ]));
  }

  // --- Theme 3: per-board scoring + drill-down ---
  const scoreBody = [];
  if (perEntry === undefined) {
    scoreBody.push(note('running', { label: 'Reading per-entry scoring' }));
  } else if (perEntry && Array.isArray(perEntry.entries) && perEntry.entries.length) {
    scoreBody.push(el('p', { class: 'vb-fig-lead' }, [
      'How this candidate scored on each board entry — its drift loss (lower is better, the dot rides the track) '
      + 'and pass/fail. Click an entry to open its expectation outcomes and per-judge detail; from there, the run.',
    ]));
    scoreBody.push(el('div', { class: 'vb-score-list' },
      perEntry.entries.map((e) => scoreRow(e, gid, epochId, c))));
  } else if (perEntry) {
    scoreBody.push(note('empty', { label: 'No per-entry scoring recorded yet' }));
  } else {
    scoreBody.push(note('not_yet', { label: 'Per-entry scoring not yet available' }));
  }
  host.appendChild(section(seed ? 'Board results' : 'Per-board scoring', scoreBody, {
    sub: seed ? 'The absolute drift loss this baseline produced per entry — click to drill in.'
      : 'Three depths: the board → one entry’s outcomes → the run dialogue.',
  }));

  // --- Scalar slopegraph (champion → challenger headline) ---
  if (!seed && gate && gate.scalar_components
      && (gate.scalar_components.champion || gate.scalar_components.challenger)) {
    const cc = gate.scalar_components.champion || {};
    const ch = gate.scalar_components.challenger || {};
    const champDrift = fin(cc.drift) ? cc.drift : null;
    const challDrift = fin(ch.drift) ? ch.drift : null;
    const dScalar = fin(gate.delta_scalar) ? gate.delta_scalar : null;
    const series = [{
      label: 'drift loss',
      from: champDrift, to: challDrift,
      verdict: fin(dScalar) ? (dScalar < 0 ? 'improve' : dScalar > 0 ? 'regress' : 'neutral') : 'neutral',
    }];
    host.appendChild(section('Scalar', [
      slopegraph(series, {
        fromLabel: 'champion', toLabel: 'challenger', digits: 2,
        caption: 'Lower is better — the tournament ranks by this scalar loss.',
      }),
      el('div', { class: 'vb-exp-stats' }, [
        dScalar != null ? stat(fmtSigned(dScalar, 2), 'Δ scalar', { tone: dScalar < 0 ? 'improve' : 'regress' }) : null,
        fin(gate.delta_pass_rate) ? stat(fmtSigned(gate.delta_pass_rate, 2), 'Δ pass rate', { tone: gate.delta_pass_rate > 0 ? 'improve' : gate.delta_pass_rate < 0 ? 'regress' : 'neutral' }) : null,
        (gate.primary_driver && gate.primary_driver.judge) ? stat(gate.primary_driver.judge, 'primary driver', { tone: 'neutral' }) : null,
      ].filter(Boolean)),
    ], { sub: 'The headline movement, champion to challenger.' }));
  }

  // --- Theme 1: the genealogy figure ---
  host.appendChild(section('Lineage', [genealogyBlock(gid)], {
    sub: 'Where this generation sits in the family tree.',
  }));

  // --- The change (secondary, collapsible) ---
  const muts = mutationsBlock(exp);
  host.appendChild(section('The change', [
    muts || null,
    diffBlock(diffData),
  ].filter(Boolean), {
    sub: 'The patch that caused all of the above — the mutation points it edited, with the full diff.',
  }));
}

registerBView('experiment', renderExperiment);

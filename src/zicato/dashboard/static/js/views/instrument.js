// js/views/instrument.js — the Instrument lens (board reflection · R5).
//
// Reflection treats `board + scoring + judges + gate` as a MEASUREMENT
// INSTRUMENT and audits it. This view is the minimal transliteration of the
// docs/design/reflection-viz-study/ visual spec — the bill-of-health,
// judge-audit, and x-ray mockups (the other five mockups are the deferred
// component spec). Three modes, keyed by the route depth
// (#/e/<epochId>/instrument[/<reflectionId>[/<judge>[/<runRef>]]]):
//
//   * LANDING  (no reflection)          — a dataTable of the epoch's reflections.
//   * BILL OF HEALTH (reflectionId)     — the four-pillar quadrant + ranked
//                                         findings + the per-judge audit cards.
//   * X-RAY    (judge + run_ref)        — the annotated transcript + the judge
//                                         verdict vs the meta-judge adjudication.
//
// SERVER AUTHORITY: the view RENDERS reader payloads and computes NO domain
// conclusions. It never derives a rate / κ / verdict — query/reflection_view.py
// owns every metric (precision/recall/F1/FPR, aggregate_f1, decision-flip P,
// margin_clears_floor, the confusion matrix, the ranked findings, the verdict).
// The only aggregation done here is DISPLAY tallying of the reader's own
// per-item booleans/counts (e.g. "N of M entries differentiate" from the
// per-entry `differentiates` flags) — never a withheld number reconstructed.
//
// A completed reflection is IMMUTABLE ⇒ the view is fetch-once, digest-folded,
// and gatedSwap-painted (renderView): two identical fetches rebuild ZERO DOM.

import { el } from '../core/dom.js';
import * as D from '../data.js';
import { section, empty, chip, dataTable, renderView, isNum, fmt } from '../ui.js';

// ---- small local coercions (display-only) ---------------------------
function num(v, d) { return isNum(v) ? fmt(v, isNum(d) ? d : 3) : '—'; }
function pct(v) { return isNum(v) ? Math.round(v * 100) + '%' : '—'; }
function yn(v) { return v === true ? 'yes' : v === false ? 'no' : '—'; }

// The verdict → tone class (TP good · FP bad · FN caution · TN/ambiguous faint).
// The vocabulary is the server's (adjudicator.py); the view only colours it.
function verdictTone(verdict) {
  const v = String(verdict || '').toUpperCase();
  if (v === 'TP') return 'tp';
  if (v === 'FP') return 'fp';
  if (v === 'FN') return 'fn';
  if (v === 'TN') return 'tn';
  return 'amb';
}
function severityTone(sev) {
  const s = String(sev || '').toLowerCase();
  if (s === 'critical') return 'crit';
  if (s === 'warning') return 'warn';
  return 'info';
}

// ---- entry point ----------------------------------------------------

export async function render(host, ctx, params) {
  const p = params || {};
  const routeEpoch = p.epochId || null;
  const reflectionId = p.reflectionId || null;
  const judge = p.judge || null;
  const runRef = p.runRef || null;
  // the x-ray needs BOTH a judge and a run_ref; a bare judge (the `up()`
  // intermediate) degrades to the bill of health.
  const xray = !!(reflectionId && judge && runRef);

  await renderView(host, ctx, {
    loading: 'Reading reflections…',
    epoch: true, routeEpoch, title: 'Instrument',
    emptyText: 'No current epoch — the Instrument lens audits one epoch’s contract.',
    load: async ({ epochId }) => {
      if (xray) {
        const data = await D.reflectionXray(reflectionId, judge, runRef);
        return { mode: 'xray', epochId, reflectionId, judge, runRef, xray: data };
      }
      if (reflectionId) {
        const [summary, scorecards] = await Promise.all([
          D.reflectionSummary(reflectionId), D.reflectionScorecards(reflectionId),
        ]);
        return { mode: 'bill', epochId, reflectionId, summary, scorecards };
      }
      const list = await D.reflections(epochId);
      return { mode: 'landing', epochId, list };
    },
    digest: (d) => digestFor(d),
    build: (d) => buildFor(d, ctx),
  });
}

// ---- the timestamp-free content digest (the no-flash guarantee) -----
//
// Folds ONLY structural/content data; a completed reflection is immutable, so a
// re-fetch yields an identical digest and gatedSwap writes nothing. Every leg is
// stable-stringified with the reader's own numbers (no derivation).
function digestFor(d) {
  if (d.mode === 'landing') {
    const items = ((d.list && d.list.reflections) || []).map((r) => [
      r.reflection_id, r.created_at, r.mode, !!r.executed,
      r.n_findings, isNum(r.decision_flip_p) ? r.decision_flip_p.toFixed(4) : null,
    ]);
    return JSON.stringify({ m: 'landing', e: d.epochId, items });
  }
  if (d.mode === 'bill') {
    const s = d.summary || {};
    const cards = ((d.scorecards && d.scorecards.judges) || []).map((j) => [
      j.judge_name, j.tp, j.fp, j.fn, j.tn, j.ambiguous, j.exercised,
      isNum(j.precision) ? j.precision.toFixed(3) : null,
      isNum(j.recall) ? j.recall.toFixed(3) : null,
      isNum(j.f1) ? j.f1.toFixed(3) : null,
      isNum(j.self_consistency_kappa) ? j.self_consistency_kappa.toFixed(3) : null,
    ]);
    return JSON.stringify({
      m: 'bill', id: d.reflectionId, found: !!s.found,
      p: s.pillars || {},
      flip: isNum(s.decision_flip_p) ? s.decision_flip_p.toFixed(4) : null,
      findings: (s.findings || []).map((f) => [f.finding_id, f.severity, f.title, (f.evidence || []).length, f.proposed_op && f.proposed_op.op]),
      cards,
    });
  }
  // x-ray
  const x = d.xray || {};
  const adj = x.adjudication || {};
  const jv = x.judge_verdict || {};
  return JSON.stringify({
    m: 'xray', id: d.reflectionId, judge: d.judge, run: d.runRef, found: !!x.found,
    fidelity: x.transcript && x.transcript.fidelity,
    turns: (x.transcript && x.transcript.turns) || [],
    jv: [jv.fired, jv.severity, jv.claim, jv.transcript_span],
    adj: [adj.verdict, adj.evidence_span, adj.meta_judge_rationale, adj.meta_judge_model,
      adj.fidelity, adj.prompt_version, adj.adjudicator_self_agreement],
  });
}

function buildFor(d, ctx) {
  if (d.mode === 'landing') return buildLanding(d, ctx);
  if (d.mode === 'bill') return buildBill(d, ctx);
  return buildXray(d, ctx);
}

// ====================================================================
// LANDING — the epoch's reflections (newest first).
// ====================================================================
function buildLanding(d, ctx) {
  const nodes = [];
  nodes.push(el('div', { class: 'dn-pagehead' }, [
    el('h1', { class: 'dn-h1', text: 'Instrument · board reflection' }),
    el('p', { class: 'dn-lede', text: 'Reflection treats the board + scoring + judges + gate as a measurement instrument and audits it — is the evaluation contract trustworthy enough to evolve against? Pick a completed reflection for its bill of health.' }),
  ]));

  const items = (d.list && Array.isArray(d.list.reflections)) ? d.list.reflections : [];
  if (!items.length) {
    nodes.push(section('Reflections', el('div', { class: 'dn-panel' }, [
      empty('No reflections for this epoch yet.'),
      el('p', { class: 'dn-faint', style: 'font-size:12px;margin:6px 0 0;' }, [
        'Run one with ', el('code', { class: 'dn-instr-apply', text: 'zicato reflect run' }), ' (off the happy path — diagnose-and-recommend only; it never edits the contract).',
      ]),
    ])));
    return nodes;
  }

  const table = dataTable({
    class: 'dn-board-table dn-instr-list',
    columns: [
      { label: 'reflection' }, { label: 'created' }, { label: 'mode' },
      { label: 'executed' }, { label: 'findings', class: 'dn-num' }, { label: 'flip P', class: 'dn-num' },
    ],
    rows: items.map((r) => ({
      cells: [
        { el: el('a', { class: 'dn-instr-link dn-mono', href: ctx.href('instrument', { epochId: d.epochId, reflectionId: r.reflection_id }), text: r.reflection_id }) },
        { text: r.created_at || '—', class: 'dn-mono' },
        { text: r.mode || '—' },
        { text: yn(r.executed) },
        { text: isNum(r.n_findings) ? String(r.n_findings) : '—', class: 'dn-num' },
        { text: isNum(r.decision_flip_p) ? pct(r.decision_flip_p) : 'n/a', class: 'dn-num' },
      ],
    })),
  });
  nodes.push(section('Reflections', el('div', { class: 'dn-panel dn-table-scroll' }, [table])));
  return nodes;
}

// ====================================================================
// BILL OF HEALTH — the four-pillar quadrant + findings + judge audit.
// (Transliterates bill-of-health.html's boh-quad sub-rows + the judge-audit
//  scorecard. The mockup's arc GAUGE + top-line VERDICT are DEFERRED: the
//  reader carries no 0–1 pillar score / verdict, and server-authority forbids
//  synthesising one client-side.)
// ====================================================================
function buildBill(d, ctx) {
  const s = d.summary || {};
  const nodes = [];
  nodes.push(el('div', { class: 'dn-pagehead' }, [
    el('h1', { class: 'dn-h1' }, ['Bill of health · ', el('span', { class: 'dn-mono', text: d.reflectionId })]),
    el('p', { class: 'dn-lede', text: 'The one-screen verdict on the instrument: four pillars — reliability (consistent?), discrimination (tells candidates apart?), validity (correct, per adjudication?), calibration (margin tuned?) — over the ranked findings and the per-judge audit.' }),
  ]));
  if (!s.found) {
    nodes.push(section('Bill of health', el('div', { class: 'dn-panel' }, [empty('No such reflection (it may not be indexed yet).')])));
    return nodes;
  }

  // identity strip
  nodes.push(el('div', { class: 'dn-panel dn-row dn-instr-ident' }, [
    identKv('created', s.created_at || '—'),
    identKv('mode', s.mode || '—'),
    identKv('executed', yn(s.executed)),
    identKv('fidelity', (s.fidelity_tiers || []).join(' · ') || '—'),
  ]));

  const pl = s.pillars || {};
  const quad = el('div', { class: 'dn-instr-quad' }, [
    pillarCard('Reliability', reliabilityRows(pl.reliability || {}, s)),
    pillarCard('Discrimination', discriminationRows(pl.discrimination || {})),
    pillarCard('Validity', validityRows(pl.validity || {}, d.scorecards)),
    pillarCard('Calibration', calibrationRows(pl.calibration || {})),
  ]);
  nodes.push(section('Four-pillar quadrant', quad));

  // findings
  const findings = Array.isArray(s.findings) ? s.findings : [];
  nodes.push(section('Findings', findingsList(findings, d.reflectionId)));

  // judge audit
  const judges = (d.scorecards && Array.isArray(d.scorecards.judges)) ? d.scorecards.judges : [];
  nodes.push(section('Judge audit', judgeAudit(judges, findings, d, ctx)));
  return nodes;
}

function identKv(k, v) {
  return el('div', { class: 'dn-instr-kv' }, [
    el('span', { class: 'dn-instr-k', text: k }),
    el('span', { class: 'dn-instr-v dn-mono', text: v }),
  ]);
}

function pillarCard(name, rows) {
  return el('div', { class: 'dn-instr-pillar dn-panel' }, [
    el('div', { class: 'dn-instr-pillar-name', text: name }),
    el('div', { class: 'dn-instr-subs' }, rows.map(([k, v, tone]) =>
      el('div', { class: 'dn-instr-sub' }, [
        el('span', { class: 'dn-instr-sub-k', text: k }),
        el('span', { class: 'dn-instr-sub-v' + (tone ? ' dn-instr-t-' + tone : ''), text: v }),
      ]))),
  ]);
}

// reliability: noise floor + decision-flip P (n/a + reason when null).
function reliabilityRows(rel, s) {
  const rows = [];
  rows.push(['noise floor max|Δ|', num(s.noise_floor_max_abs_delta, 4)]);
  const flip = rel.decision_flip || null;
  if (isNum(s.decision_flip_p)) {
    rows.push(['decision-flip P', pct(s.decision_flip_p), s.decision_flip_p > 0 ? 'warn' : 'good']);
  } else {
    // null p_flip — surface the honest reason (S2). "insufficient replication"
    // is the standing reason; the reader's own `reason` string rides the title.
    rows.push(['decision-flip P', 'n/a — insufficient replication', 'faint']);
    if (flip && flip.reason) rows.push(['flip reason', String(flip.reason), 'faint']);
  }
  if (rel.preflight_verdict) rows.push(['preflight verdict', String(rel.preflight_verdict)]);
  return rows;
}

// discrimination: % differentiating entries + coverage (display tallies of the
// reader's per-item flags — no rate is reconstructed).
function discriminationRows(disc) {
  const rows = [];
  const ed = disc.entry_differentiation || {};
  const entries = Array.isArray(ed.entries) ? ed.entries : [];
  const judgeable = entries.filter((e) => e.differentiates !== null && e.differentiates !== undefined);
  const differ = judgeable.filter((e) => e.differentiates === true);
  rows.push(['differentiating entries', judgeable.length ? `${differ.length} / ${judgeable.length}` : '—']);
  const red = disc.redundancy || {};
  const rc = Array.isArray(red.redundant_clusters) ? red.redundant_clusters : [];
  rows.push(['redundant clusters', String(rc.length)]);
  const cov = disc.coverage || {};
  const exK = Array.isArray(cov.exercised_kinds) ? cov.exercised_kinds : [];
  const watK = Array.isArray(cov.watched_kinds) ? cov.watched_kinds : [];
  rows.push(['kinds covered', watK.length ? `${exK.length} / ${watK.length}` : '—']);
  const unt = Array.isArray(cov.untested_judges) ? cov.untested_judges : [];
  rows.push(['untested judges', String(unt.length), unt.length ? 'warn' : 'good']);
  return rows;
}

// validity: aggregate judge F1 + the ambiguous pile (Σ ambiguous over cards).
function validityRows(val, scorecards) {
  const rows = [];
  rows.push(['judges', isNum(val.n_judges) ? String(val.n_judges) : '—']);
  rows.push(['aggregate F1', num(val.aggregate_f1, 2)]);
  const judges = (scorecards && Array.isArray(scorecards.judges)) ? scorecards.judges : [];
  const ambiguous = judges.reduce((a, j) => a + (isNum(j.ambiguous) ? j.ambiguous : 0), 0);
  rows.push(['ambiguous pile', String(ambiguous), ambiguous ? 'warn' : 'good']);
  const unt = Array.isArray(val.untested_judges) ? val.untested_judges : [];
  if (unt.length) rows.push(['untested', unt.join(', '), 'warn']);
  return rows;
}

// calibration: margin-to-noise (the margin FINDING rides the findings list).
function calibrationRows(cal) {
  const rows = [];
  rows.push(['promote_margin', num(cal.promote_margin, 4)]);
  rows.push(['noise floor max|Δ|', num(cal.noise_floor_max_abs_delta, 4)]);
  const clears = cal.margin_clears_floor;
  rows.push(['margin clears floor', yn(clears), clears === true ? 'good' : clears === false ? 'bad' : null]);
  return rows;
}

// ---- findings list --------------------------------------------------
function findingsList(findings, reflectionId) {
  if (!findings.length) {
    return el('div', { class: 'dn-panel' }, [empty('No findings — the instrument reads healthy on every pillar this reflection measured.')]);
  }
  const wrap = el('div', { class: 'dn-instr-findings' });
  for (const f of findings) {
    const tone = severityTone(f.severity);
    const row = el('div', { class: 'dn-panel dn-instr-finding dn-instr-fs-' + tone });
    row.appendChild(el('div', { class: 'dn-instr-finding-head' }, [
      chip('instr-sev-' + tone, String(f.severity || 'info')),
      el('span', { class: 'dn-instr-finding-title', text: f.title || f.finding_id }),
      el('span', { class: 'dn-instr-finding-count dn-faint', text: (f.evidence || []).length + ' evidence' }),
    ]));
    if (f.detail) row.appendChild(el('p', { class: 'dn-instr-finding-detail', text: String(f.detail) }));
    if (f.proposed_op && f.proposed_op.op) {
      row.appendChild(el('div', { class: 'dn-instr-finding-op' }, [
        el('span', { class: 'dn-faint', text: 'proposed op · ' }),
        el('span', { class: 'dn-mono', text: f.proposed_op.op + '(' + Object.keys(f.proposed_op.args || {}).join(', ') + ')' }),
      ]));
    }
    // the CLI is the apply path — a copyable mono invocation (no apply button in
    // this recommend-only MVP).
    row.appendChild(el('code', {
      class: 'dn-instr-apply', title: 'copy — apply this finding to a builder draft via the CLI',
      text: `zicato reflect apply ${reflectionId} ${f.finding_id}`,
    }));
    wrap.appendChild(row);
  }
  return wrap;
}

// ---- judge audit (per-judge scorecard cards) ------------------------
function judgeAudit(judges, findings, d, ctx) {
  if (!judges.length) {
    return el('div', { class: 'dn-panel' }, [empty('No judge scorecards — no adjudication ran for this reflection (the zero-LLM tier reads reliability + discrimination only).')]);
  }
  // evidence chips per judge come from the FINDINGS payload (the scorecards
  // carry counts only). Group by judge + tag the verdict from the finding kind.
  const evidenceByJudge = new Map();
  for (const f of findings) {
    const v = /missed/i.test(f.finding_id) || /misses/i.test(f.title || '') ? 'FN'
      : (/false/i.test(f.title || '') || /false_fire/i.test(f.finding_id)) ? 'FP' : null;
    if (!v) continue;
    for (const ev of (f.evidence || [])) {
      const name = ev.judge_name;
      if (!name) continue;
      if (!evidenceByJudge.has(name)) evidenceByJudge.set(name, []);
      evidenceByJudge.get(name).push({ verdict: v, run_ref: ev.run_ref, span: ev.span });
    }
  }
  const wrap = el('div', { class: 'dn-instr-cards' });
  for (const j of judges) {
    wrap.appendChild(scorecard(j, evidenceByJudge.get(j.judge_name) || [], d, ctx));
  }
  return wrap;
}

function scorecard(j, evidence, d, ctx) {
  const untested = j.exercised === false;
  const card = el('div', { class: 'dn-panel dn-instr-card' + (untested ? ' dn-instr-card-untested' : '') });
  card.appendChild(el('div', { class: 'dn-instr-card-head' }, [
    el('span', { class: 'dn-instr-card-name dn-mono', text: j.judge_name }),
    untested ? chip('instr-sev-warn', 'never fired') : null,
  ].filter(Boolean)));

  if (untested) {
    card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:12px;margin:6px 0 0;', text: 'This judge never fired across the corpus — its kind was not exercised, so precision/recall cannot be validated here.' }));
    return card;
  }

  // the 2×2 confusion matrix + ambiguous pile.
  const cm = el('div', { class: 'dn-instr-cm' }, [
    cmCell('TP', j.tp, 'correct fire', 'tp'),
    cmCell('FP', j.fp, 'false fire', 'fp'),
    cmCell('FN', j.fn, 'missed fire', 'fn'),
    cmCell('TN', j.tn, 'correct silence', 'tn'),
  ]);
  card.appendChild(cm);
  card.appendChild(el('p', { class: 'dn-instr-amb dn-faint', text: `+${isNum(j.ambiguous) ? j.ambiguous : 0} ambiguous (excluded from the rates — a large pile is itself a finding: the criterion is underspecified)` }));

  // the derived rates (reader-owned) + severity accuracy.
  const metrics = el('div', { class: 'dn-instr-metrics' }, [
    metric('precision', num(j.precision, 2)),
    metric('recall', num(j.recall, 2)),
    metric('F1', num(j.f1, 2)),
    metric('FPR', num(j.fpr, 2)),
    metric('severity acc', num(j.severity_accuracy, 2)),
  ]);
  card.appendChild(metrics);

  // self-consistency — the pairwise disagreement rate AND the chance-corrected
  // Fleiss κ, HONESTLY LABELLED (never one masquerading as the other).
  card.appendChild(el('div', { class: 'dn-instr-consistency' }, [
    metric('disagreement rate', num(j.disagreement_rate, 2)),
    metric('self-consistency κ', num(j.self_consistency_kappa, 2)),
  ]));

  // redundancy / conflict chips.
  const rw = Array.isArray(j.redundant_with) ? j.redundant_with : [];
  const cw = Array.isArray(j.conflicts_with) ? j.conflicts_with : [];
  if (rw.length || cw.length) {
    const chips = el('div', { class: 'dn-instr-xchips' });
    for (const r of rw) chips.appendChild(chip('instr-redundant', 'redundant · ' + (r.judge || '?') + (isNum(r.corr) ? ' ' + fmt(r.corr, 2) : '')));
    for (const c of cw) chips.appendChild(chip('instr-conflict', 'conflict · ' + (c.judge || '?') + (isNum(c.corr) ? ' ' + fmt(c.corr, 2) : '')));
    card.appendChild(chips);
  }

  // evidence chips on the FP/FN piles → the x-ray route.
  if (evidence.length) {
    const chips = el('div', { class: 'dn-instr-evidence' });
    for (const ev of evidence) {
      const tone = verdictTone(ev.verdict);
      chips.appendChild(el('a', {
        class: 'dn-instr-echip dn-instr-t-' + tone,
        href: ctx.href('instrument', { epochId: d.epochId, reflectionId: d.reflectionId, judge: j.judge_name, runRef: ev.run_ref }),
        title: 'open the adjudication x-ray for ' + ev.run_ref,
      }, [
        el('span', { class: 'dn-instr-echip-v', text: ev.verdict }),
        el('span', { class: 'dn-instr-echip-span', text: ev.span ? String(ev.span) : ev.run_ref }),
      ]));
    }
    card.appendChild(chips);
  }
  return card;
}

function cmCell(label, n, caption, tone) {
  return el('div', { class: 'dn-instr-cmcell dn-instr-t-' + tone }, [
    el('span', { class: 'dn-instr-cm-lab', text: label }),
    el('span', { class: 'dn-instr-cm-n', text: isNum(n) ? String(n) : '0' }),
    el('span', { class: 'dn-instr-cm-cap dn-faint', text: caption }),
  ]);
}
function metric(k, v) {
  return el('div', { class: 'dn-instr-metric' }, [
    el('span', { class: 'dn-instr-metric-k dn-faint', text: k }),
    el('span', { class: 'dn-instr-metric-v', text: v }),
  ]);
}

// ====================================================================
// X-RAY — the annotated transcript (left) + judge vs adjudication (right).
// (Transliterates xray.html's inline-annotated-transcript split.)
// ====================================================================
function buildXray(d, ctx) {
  const x = d.xray || {};
  const nodes = [];
  nodes.push(el('div', { class: 'dn-pagehead' }, [
    el('h1', { class: 'dn-h1' }, ['Adjudication x-ray · ', el('span', { class: 'dn-mono', text: d.judge })]),
    el('p', { class: 'dn-lede' }, ['The independent meta-judge over one decision · ', el('span', { class: 'dn-mono', text: d.runRef })]),
  ]));
  if (!x.found) {
    nodes.push(section('X-ray', el('div', { class: 'dn-panel' }, [empty('No such decision — the reflection, judge, or run reference did not resolve.')])));
    return nodes;
  }

  const transcript = x.transcript || { fidelity: 'unavailable', turns: [] };
  const adj = x.adjudication || null;
  const span = adj && adj.evidence_span ? String(adj.evidence_span) : '';
  const tone = adj ? verdictTone(adj.verdict) : 'amb';

  const split = el('div', { class: 'dn-instr-xray' }, [
    transcriptPane(transcript, span, tone),
    verdictPane(x.judge_verdict, adj),
  ]);
  nodes.push(section('X-ray', split));
  return nodes;
}

function transcriptPane(transcript, span, tone) {
  const pane = el('div', { class: 'dn-instr-xleft dn-panel' });
  pane.appendChild(el('div', { class: 'dn-instr-fidelity' }, [
    el('span', { class: 'dn-faint', text: 'transcript · ' }),
    el('span', { class: 'dn-instr-fidelity-tag', text: fidelityLabel(transcript.fidelity) }),
  ]));
  const turns = Array.isArray(transcript.turns) ? transcript.turns : [];
  if (transcript.fidelity === 'unavailable' || !turns.length) {
    pane.appendChild(el('p', { class: 'dn-empty', text: 'Transcript unavailable — the verbatim judge_io / result.json capture was not retained for this run (the events-preview tier needs the dashboard reconstructor and is not read here).' }));
    return pane;
  }
  const body = el('div', { class: 'dn-instr-transcript' });
  turns.forEach((t, i) => {
    body.appendChild(el('div', { class: 'dn-instr-turn' }, [
      el('div', { class: 'dn-instr-turn-role dn-faint', text: 'turn ' + (i + 1) }),
      el('div', { class: 'dn-instr-turn-body' }, highlightSpan(String(t), span, tone)),
    ]));
  });
  pane.appendChild(body);
  return pane;
}

// Highlight the adjudicator's evidence_span when it is a verbatim substring of
// the turn text (the reader hands a copied substring, not a char offset). No
// match ⇒ the plain text, unchanged.
function highlightSpan(text, span, tone) {
  if (!span) return [text];
  const idx = text.indexOf(span);
  if (idx < 0) return [text];
  const out = [];
  if (idx > 0) out.push(text.slice(0, idx));
  out.push(el('span', { class: 'dn-instr-span dn-instr-t-' + tone, text: span }));
  const rest = idx + span.length;
  if (rest < text.length) out.push(text.slice(rest));
  return out;
}

function verdictPane(jv, adj) {
  const pane = el('div', { class: 'dn-instr-xright dn-panel' });
  // the ORIGINAL judge verdict.
  pane.appendChild(el('div', { class: 'dn-instr-xsub' }, [
    el('div', { class: 'dn-instr-xsub-h', text: 'judge' }),
    jv ? el('div', { class: 'dn-instr-xrow' }, [
      el('span', { class: 'dn-mono', text: jv.judge_name || '' }),
      el('span', { class: 'dn-instr-xsev', text: jv.fired ? 'fired' + (jv.severity ? ' · ' + jv.severity : '') : 'silent' }),
    ]) : el('p', { class: 'dn-faint', text: 'no recorded judge decision for this run.' }),
    jv && jv.claim ? el('p', { class: 'dn-instr-xclaim', text: String(jv.claim) }) : null,
  ].filter(Boolean)));

  // the meta-judge ADJUDICATION.
  if (!adj) {
    pane.appendChild(el('div', { class: 'dn-instr-xsub' }, [
      el('div', { class: 'dn-instr-xsub-h', text: 'adjudication' }),
      el('p', { class: 'dn-faint', text: 'no adjudication record — this decision was not adjudicated (or the record is unavailable).' }),
    ]));
    return pane;
  }
  const tone = verdictTone(adj.verdict);
  pane.appendChild(el('div', { class: 'dn-instr-xsub' }, [
    el('div', { class: 'dn-instr-xsub-h' }, [
      'adjudication ', chip('instr-verdict-' + tone, String(adj.verdict || 'ambiguous')),
    ]),
    adj.meta_judge_rationale ? el('p', { class: 'dn-instr-xwhy', text: String(adj.meta_judge_rationale) }) : null,
    el('div', { class: 'dn-instr-xmeta' }, [
      metaKv('model', adj.meta_judge_model || '—'),
      metaKv('prompt version', isNum(adj.prompt_version) ? String(adj.prompt_version) : '—'),
      metaKv('fidelity', fidelityLabel(adj.fidelity)),
      isNum(adj.adjudicator_self_agreement) ? metaKv('self-agreement', fmt(adj.adjudicator_self_agreement, 2)) : null,
    ].filter(Boolean)),
  ].filter(Boolean)));
  return pane;
}

function metaKv(k, v) {
  return el('div', { class: 'dn-instr-metakv' }, [
    el('span', { class: 'dn-faint', text: k }),
    el('span', { class: 'dn-mono', text: v }),
  ]);
}

function fidelityLabel(fidelity) {
  const f = String(fidelity || 'unavailable');
  if (f === 'verbatim') return 'verbatim (exact judge input)';
  if (f === 'result') return 'result.json (reconstructed)';
  if (f === 'unavailable') return 'unavailable';
  return f;
}

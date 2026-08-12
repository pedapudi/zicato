// js/views/instrument.js — the Instrument lens (board reflection).
//
// Reflection treats `board + scoring + judges + gate` as a MEASUREMENT
// INSTRUMENT and audits it. This view is the console-native surface for the
// bill-of-health, practice-review, judge-audit, and x-ray reads. Three modes,
// keyed by the route depth
// (#/e/<epochId>/instrument[/<reflectionId>[/<judge>[/<runRef>]]]):
//
//   * LANDING  (no reflection)          — a dataTable of the epoch's reflections.
//   * BILL OF HEALTH (reflectionId)     — the practice-review narrative + the
//                                         four-pillar quadrant + ranked findings
//                                         + the per-judge audit cards.
//   * X-RAY    (judge + run_ref)        — the annotated transcript + the judge
//                                         verdict vs the meta-judge adjudication.
//
// DESIGN LANGUAGE: the lens speaks the console's existing grammars — it does
// NOT invent chrome (the operator's critique of the generated-UI idiosyncrasies:
// the left rail and the overly-extensive tags). So: the practice review and the
// findings render as the loop-health findings panel's QUIET verdict-led rows
// (a tone glyph + a headline + a dn-faint rationale — NOT a chip per row); the
// judge scorecards render rates as the dn-stat idiom (NOT labelled tags), the
// redundancy/conflict relations as ONE faint inline sentence, and the evidence
// as inline x-ray links (NOT chip strips); metadata (fidelity tier / adjudicator
// model / prompt version / self-agreement) is a single dn-faint CAPTION under
// the relevant figure (NEVER a per-row tag). Tags/chips are reserved for the ONE
// semantic state the console already pills: the adjudication VERDICT on the x-ray.
// Navigation lives in the shell (the hash routes + the tree) — the lens grows no
// internal nav rail of its own.
//
// SERVER AUTHORITY: the view RENDERS reader payloads and computes NO domain
// conclusions. It never derives a rate / κ / verdict — query/reflection_view.py
// owns every metric (precision/recall/F1/FPR, aggregate_f1, decision-flip P,
// margin_clears_floor, the confusion matrix, the ranked findings, the practice
// verdicts). The only aggregation done here is DISPLAY tallying of the reader's
// own per-item booleans/counts — never a withheld number reconstructed.
//
// A completed reflection is IMMUTABLE ⇒ the view is fetch-once, digest-folded,
// and gatedSwap-painted (renderView): two identical fetches rebuild ZERO DOM.

import { el } from '../core/dom.js';
import * as D from '../data.js';
import { section, empty, chip, dataTable, renderView, stat, isNum, fmt } from '../ui.js';

// ---- small local coercions (display-only) ---------------------------
function num(v, d) { return isNum(v) ? fmt(v, isNum(d) ? d : 3) : '—'; }
function pct(v) { return isNum(v) ? Math.round(v * 100) + '%' : '—'; }
function yn(v) { return v === true ? 'yes' : v === false ? 'no' : '—'; }

// The console's quiet-caption treatment — the ONE home for metadata (fidelity,
// model, prompt version) and figure legends. Never a heavier frame.
function caption(text) { return el('p', { class: 'dn-faint dn-instr-cap', text: String(text) }); }

// A small tone-coloured status mark — the loop-health findings panel's
// verdict-led lead, rendered as a glyph rather than a chip box (the de-tagging).
function toneMark(tone) {
  return el('span', { class: 'dn-instr-mark dn-instr-t-' + tone, 'aria-hidden': 'true', text: '●' });
}

// The adjudication verdict → tone (TP good · FP bad · FN caution · TN/ambiguous
// faint). The vocabulary is the server's (adjudicator.py); the view only colours.
function verdictTone(verdict) {
  const v = String(verdict || '').toUpperCase();
  if (v === 'TP') return 'tp';
  if (v === 'FP') return 'fp';
  if (v === 'FN') return 'fn';
  if (v === 'TN') return 'tn';
  return 'amb';
}
// Finding severity → a tone in the shared dn-instr-t-* set (bad/warn/faint).
function severityTone(sev) {
  const s = String(sev || '').toLowerCase();
  if (s === 'critical') return 'bad';
  if (s === 'warning') return 'warn';
  return 'faint';
}
// Practice verdict → tone. sound affirms (good), unsound is the anti-practice
// (bad), attend is a soft deficiency (warn), unmeasured is honest-absent (faint).
function practiceTone(verdict) {
  const v = String(verdict || '').toLowerCase();
  if (v === 'sound') return 'good';
  if (v === 'unsound') return 'bad';
  if (v === 'attend') return 'warn';
  return 'faint';
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
        const [summary, scorecards, practices] = await Promise.all([
          D.reflectionSummary(reflectionId),
          D.reflectionScorecards(reflectionId),
          D.reflectionPractices(reflectionId),
        ]);
        return { mode: 'bill', epochId, reflectionId, summary, scorecards, practices };
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
      j.recommendation || null,
    ]);
    const pr = d.practices || {};
    const cal = (s.pillars && s.pillars.calibration) || {};
    return JSON.stringify({
      m: 'bill', id: d.reflectionId, found: !!s.found,
      p: s.pillars || {},
      // The whole `pillars` object above already folds every pillar row, so the
      // calibration figures gate through it; delta_std is ALSO named here so a
      // later narrowing of `p` to a field list cannot silently un-gate the row
      // that renders it (the recurring no-repaint bug class).
      dstd: isNum(cal.noise_floor_delta_std) ? cal.noise_floor_delta_std.toFixed(6) : null,
      flip: isNum(s.decision_flip_p) ? s.decision_flip_p.toFixed(4) : null,
      findings: (s.findings || []).map((f) => [f.finding_id, f.severity, f.title, (f.evidence || []).length, f.proposed_op && f.proposed_op.op, f.recommendation || null]),
      cards,
      // the practice-review narrative — folded so a new/changed check repaints
      // while a no-op re-serve stays byte-identical.
      pfound: !!pr.found,
      prac: (Array.isArray(pr.checks) ? pr.checks : []).map((c) => [
        c.check_id, c.verdict, c.headline, c.rationale,
        c.proposed_op && c.proposed_op.op, c.unmeasured_reason || null,
        // the measured numbers behind the headline — folded so a check whose
        // evidence moved repaints even when its verdict and wording hold.
        formatEvidence(c.evidence),
      ]),
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
      adj.fidelity, adj.prompt_version, adj.adjudicator_self_agreement,
      // severity agreement is a SEPARATE axis from the fire/silence verdict —
      // folded so a re-adjudication that flips only the severity repaints.
      adj.severity_match],
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
// BILL OF HEALTH — the practice-review narrative + the four-pillar quadrant
// + findings + judge audit. (The mockup's arc GAUGE + top-line VERDICT are
//  DEFERRED: the reader carries no 0–1 pillar score / verdict, and
//  server-authority forbids synthesising one client-side.)
// ====================================================================
function buildBill(d, ctx) {
  const s = d.summary || {};
  const nodes = [];
  nodes.push(el('div', { class: 'dn-pagehead' }, [
    el('h1', { class: 'dn-h1' }, ['Bill of health · ', el('span', { class: 'dn-mono', text: d.reflectionId })]),
    el('p', { class: 'dn-lede', text: 'The one-screen verdict on the instrument: what you should change about how you evaluate (the practice review), then four pillars — reliability (consistent?), discrimination (tells candidates apart?), validity (correct, per adjudication?), calibration (margin tuned?) — over the ranked findings and the per-judge audit.' }),
  ]));
  if (!s.found) {
    nodes.push(section('Bill of health', el('div', { class: 'dn-panel' }, [empty('No such reflection (it may not be indexed yet).')])));
    return nodes;
  }

  // identity as ONE dn-faint caption line (metadata is a caption, never tags).
  nodes.push(caption(
    `created ${s.created_at || '—'} · mode ${s.mode || '—'} · executed ${yn(s.executed)} · ${(s.fidelity_tiers || []).join(' · ') || 'fidelity —'}`,
  ));

  // ── the PRACTICE REVIEW — the narrative layer above the four pillars. ──
  nodes.push(section('Practice review · how you evaluate', practiceReview(d.practices)));

  const pl = s.pillars || {};
  const quad = el('div', { class: 'dn-instr-quad' }, [
    pillarCard('Reliability', reliabilityRows(pl.reliability || {}, s)),
    pillarCard('Discrimination', discriminationRows(pl.discrimination || {})),
    pillarCard('Validity', validityRows(pl.validity || {}, d.scorecards)),
    pillarCard('Calibration', calibrationRows(pl.calibration || {})),
  ]);
  const quadWrap = el('div', {}, [
    quad,
    caption('four pillars over this contract · reliability = consistent · discrimination = tells candidates apart · validity = correct per adjudication · calibration = margin tuned to the measured noise floor'),
  ]);
  nodes.push(section('Four-pillar quadrant', quadWrap));

  // findings
  const findings = Array.isArray(s.findings) ? s.findings : [];
  nodes.push(section('Findings', findingsList(findings, d.reflectionId, ctx, d.epochId)));

  // judge audit
  const judges = (d.scorecards && Array.isArray(d.scorecards.judges)) ? d.scorecards.judges : [];
  nodes.push(section('Judge audit', judgeAudit(judges, findings, d, ctx)));
  return nodes;
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
    // null p_flip — surface the honest reason (never a fabricated 0.0).
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
  // The draw-count-stable A/A dispersion, next to the RANGE it corrects. max|Δ|
  // grows with the calibration draw count K, so a surface showing only the range
  // shows the one number the recommendation is derived NOT to use. Absent on a
  // record that predates the statistic ⇒ no row (never an "undefined").
  if (isNum(cal.noise_floor_delta_std)) {
    rows.push(['noise floor Δ std', num(cal.noise_floor_delta_std, 4)]);
  }
  const clears = cal.margin_clears_floor;
  rows.push(['margin clears floor', yn(clears), clears === true ? 'good' : clears === false ? 'bad' : null]);
  return rows;
}

// ---- the practice review (the loop-health findings panel's row grammar) ----
//
// A practice check is a loop-health finding in a different domain, so it renders
// as the same quiet verdict-led row — NOT a bespoke card grid. Order follows the
// committed editorial policy: affirmations (`sound`) FIRST (a sound practice
// teaches as much as a deficiency flag), then `unsound` above `attend`
// (worst-first), then `unmeasured` last with the missing input named faint.
const _PRACTICE_BAND = { sound: 0, unsound: 1, attend: 2, unmeasured: 3 };

function practiceReview(review) {
  const checks = (review && Array.isArray(review.checks)) ? review.checks : [];
  if (!review || !review.found || !checks.length) {
    return el('div', { class: 'dn-panel' }, [
      empty('No practice review for this reflection.'),
      el('p', { class: 'dn-faint', style: 'font-size:12px;margin:6px 0 0;' }, [
        'Generate one with ', el('code', { class: 'dn-instr-apply', text: 'zicato reflect run' }),
        ' (or the instant ', el('code', { class: 'dn-instr-apply', text: 'zicato reflect practices' }), ' contract+history tier).',
      ]),
    ]);
  }
  const ranked = checks.map((c, i) => [c, i]).sort((a, b) => {
    const ba = _PRACTICE_BAND[a[0].verdict]; const bb = _PRACTICE_BAND[b[0].verdict];
    return (ba === undefined ? 9 : ba) - (bb === undefined ? 9 : bb) || a[1] - b[1];
  }).map((x) => x[0]);

  const panel = el('div', { class: 'dn-panel dn-instr-list-panel' });
  const vc = review.verdict_counts || {};
  panel.appendChild(caption(
    `${vc.sound || 0} sound · ${vc.attend || 0} attend · ${vc.unsound || 0} unsound · ${vc.unmeasured || 0} unmeasured`,
  ));
  for (const c of ranked) panel.appendChild(practiceRow(c));
  return panel;
}

// Render a PracticeCheck's `evidence` dict as one `key=value` line — the twin
// of the report's `_format_evidence` (cli/commands/reflect.py), including its
// clip, since the odd evidence value is a long list whose tail reads better
// from the JSON. Returns '' for an absent/empty dict so callers can skip.
const EVIDENCE_VALUE_CLIP = 80;
function formatEvidence(evidence) {
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) return '';
  const parts = [];
  for (const key of Object.keys(evidence)) {
    const raw = evidence[key];
    let text = typeof raw === 'string' ? raw : JSON.stringify(raw);
    if (text == null) text = String(raw);
    if (text.length > EVIDENCE_VALUE_CLIP) text = text.slice(0, EVIDENCE_VALUE_CLIP - 1).trimEnd() + '…';
    parts.push(key + '=' + text);
  }
  return parts.join(', ');
}

function practiceRow(c) {
  const tone = practiceTone(c.verdict);
  const row = el('div', { class: 'dn-instr-frow dn-instr-fs-' + tone });
  row.appendChild(el('div', { class: 'dn-instr-frow-head' }, [
    toneMark(tone),
    el('span', { class: 'dn-instr-frow-verdict dn-instr-t-' + tone, text: String(c.verdict || '') }),
    el('span', { class: 'dn-instr-frow-title', text: c.headline || c.check_id }),
  ]));
  if (c.rationale) row.appendChild(el('p', { class: 'dn-faint dn-instr-frow-why', text: String(c.rationale) }));
  // The measured numbers behind the headline. Issue #129's render-conformance
  // rule — the report already prints this dict, and a check whose evidence is
  // dropped states a verdict the operator cannot check.
  const pev = formatEvidence(c.evidence);
  if (pev) row.appendChild(el('p', { class: 'dn-faint dn-instr-frow-ev', text: 'evidence · ' + pev }));
  // unmeasured: name the missing input faint (honesty over coverage).
  if (String(c.verdict).toLowerCase() === 'unmeasured' && c.unmeasured_reason) {
    row.appendChild(el('p', { class: 'dn-faint dn-instr-frow-missing', text: 'missing input · ' + String(c.unmeasured_reason) }));
  }
  // a proposed op — practice checks ride practices.json, NOT findings.json, and
  // `reflect apply` is finding-only (it takes a finding_id and reads
  // findings.json), so there is no CLI apply target: render the op as copyable
  // JSON with a faint "apply via the builder" note.
  const op = c.proposed_op;
  if (op && op.op) {
    row.appendChild(el('code', {
      class: 'dn-instr-apply', title: 'copy the proposed op — apply it via the builder',
      text: JSON.stringify({ op: op.op, args: op.args || {} }),
    }));
    row.appendChild(el('span', { class: 'dn-faint dn-instr-applynote', text: 'apply via the builder (practice checks are not a reflect apply target)' }));
  }
  return row;
}

// ---- findings list (the same loop-health row grammar) ---------------
function findingsList(findings, reflectionId, ctx, epochId) {
  if (!findings.length) {
    return el('div', { class: 'dn-panel' }, [empty('No findings — the instrument reads healthy on every pillar this reflection measured.')]);
  }
  const panel = el('div', { class: 'dn-panel dn-instr-list-panel' });
  for (const f of findings) panel.appendChild(findingRow(f, reflectionId, ctx, epochId));
  return panel;
}

function findingRow(f, reflectionId, ctx, epochId) {
  const tone = severityTone(f.severity);
  const row = el('div', { class: 'dn-instr-frow dn-instr-fs-' + tone });
  row.appendChild(el('div', { class: 'dn-instr-frow-head' }, [
    toneMark(tone),
    el('span', { class: 'dn-instr-frow-verdict dn-instr-t-' + tone, text: String(f.severity || 'info') }),
    el('span', { class: 'dn-instr-frow-title', text: f.title || f.finding_id }),
  ]));
  if (f.detail) row.appendChild(el('p', { class: 'dn-faint dn-instr-frow-why', text: String(f.detail) }));

  // evidence as inline links in the row's prose (NOT a chip strip). Each links
  // into the adjudication x-ray for that (judge, run_ref).
  const ev = Array.isArray(f.evidence) ? f.evidence : [];
  if (ev.length) {
    const kids = ['evidence · '];
    ev.forEach((e, i) => {
      if (i) kids.push(' · ');
      const label = (e.verdict ? e.verdict + ' ' : '') + (e.span ? String(e.span) : String(e.run_ref || ''));
      if (e.judge_name && e.run_ref) {
        kids.push(el('a', {
          class: 'dn-instr-link dn-instr-t-' + verdictTone(e.verdict),
          href: ctx.href('instrument', { epochId, reflectionId, judge: e.judge_name, runRef: e.run_ref }),
          title: 'open the adjudication x-ray for ' + e.run_ref, text: label,
        }));
      } else {
        kids.push(label);
      }
    });
    row.appendChild(el('p', { class: 'dn-faint dn-instr-frow-ev' }, kids));
  }

  // What to DO about it. The report prints this; the lens dropped it, which
  // matters most for the emitters that carry no proposed_op, where the
  // recommendation is the only remedy text the finding has.
  if (f.recommendation) {
    row.appendChild(el('p', { class: 'dn-instr-frow-rec', text: 'recommend · ' + String(f.recommendation) }));
  }
  // a proposed op — an inline faint mono phrase — plus the copyable CLI apply
  // invocation (the CLI IS the apply path for findings; recommend-only MVP).
  if (f.proposed_op && f.proposed_op.op) {
    row.appendChild(el('p', { class: 'dn-faint dn-instr-frow-op' }, [
      'proposed op · ',
      el('span', { class: 'dn-mono', text: f.proposed_op.op + '(' + Object.keys(f.proposed_op.args || {}).join(', ') + ')' }),
    ]));
    row.appendChild(el('code', {
      class: 'dn-instr-apply', title: 'copy — apply this finding to a builder draft via the CLI',
      text: `zicato reflect apply ${reflectionId} ${f.finding_id}`,
    }));
  }
  return row;
}

// ---- judge audit (per-judge scorecard cards) ------------------------
function judgeAudit(judges, findings, d, ctx) {
  if (!judges.length) {
    return el('div', { class: 'dn-panel' }, [empty('No judge scorecards — no adjudication ran for this reflection (the zero-LLM tier reads reliability + discrimination only).')]);
  }
  // evidence per judge comes from the FINDINGS payload (the scorecards carry
  // counts only). Each evidence dict carries its OWN adjudicated `verdict`
  // (findings.py stamps it from the adjudication, e.g. FP / FN) — read it
  // directly. A title/id regex would MISLABEL whenever the wording and the
  // verdict diverge (a "fires falsely"-titled finding can carry an FN span).
  const evidenceByJudge = new Map();
  for (const f of findings) {
    for (const ev of (f.evidence || [])) {
      const name = ev.judge_name;
      const v = ev.verdict;
      if (!name || !v) continue;
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
    // untested: greyed treatment + a faint "never fired" label (NOT a chip).
    untested ? el('span', { class: 'dn-faint dn-instr-card-flag', text: 'never fired' }) : null,
  ].filter(Boolean)));

  if (untested) {
    card.appendChild(el('p', { class: 'dn-faint dn-instr-card-note', text: 'This judge never fired across the corpus — its kind was not exercised, so precision/recall cannot be validated here.' }));
    return card;
  }

  // the 2×2 confusion matrix — the figure carries the information (tone grammar:
  // TP green · FP red · FN caution · TN faint), with the ambiguous pile as a
  // dn-faint caption beneath it.
  card.appendChild(el('div', { class: 'dn-instr-cm' }, [
    cmCell('TP', j.tp, 'correct fire', 'tp'),
    cmCell('FP', j.fp, 'false fire', 'fp'),
    cmCell('FN', j.fn, 'missed fire', 'fn'),
    cmCell('TN', j.tn, 'correct silence', 'tn'),
  ]));
  card.appendChild(caption(`+${isNum(j.ambiguous) ? j.ambiguous : 0} ambiguous · excluded from the rates (a large pile is itself a finding — the criterion is underspecified)`));

  // the reader-owned rates as a quiet stat row (the dn-stat idiom, NOT tags).
  card.appendChild(el('div', { class: 'dn-row dn-instr-stats' }, [
    stat(num(j.precision, 2), 'precision'),
    stat(num(j.recall, 2), 'recall'),
    stat(num(j.f1, 2), 'F1'),
    stat(num(j.fpr, 2), 'FPR'),
    stat(num(j.severity_accuracy, 2), 'severity acc'),
  ]));
  // self-consistency — the pairwise disagreement rate AND the chance-corrected
  // Fleiss κ, HONESTLY LABELLED (never one masquerading as the other).
  card.appendChild(el('div', { class: 'dn-row dn-instr-stats' }, [
    stat(num(j.disagreement_rate, 2), 'disagreement rate'),
    stat(num(j.self_consistency_kappa, 2), 'self-consistency κ'),
  ]));

  // redundancy / conflict → ONE faint inline sentence (NOT chips).
  const rw = Array.isArray(j.redundant_with) ? j.redundant_with : [];
  const cw = Array.isArray(j.conflicts_with) ? j.conflicts_with : [];
  const phrases = [];
  for (const r of rw) phrases.push('fires with ' + (r.judge || '?') + (isNum(r.corr) ? ' (r=' + fmt(r.corr, 2) + ')' : ''));
  for (const c of cw) phrases.push('conflicts with ' + (c.judge || '?') + (isNum(c.corr) ? ' (r=' + fmt(c.corr, 2) + ')' : ''));
  if (phrases.length) card.appendChild(caption(phrases.join(' · ')));

  // the FP/FN evidence pile as inline x-ray links in prose (NOT a chip strip).
  if (evidence.length) {
    const kids = ['spans · '];
    evidence.forEach((ev, i) => {
      if (i) kids.push(' · ');
      const label = (ev.verdict || '') + ' ' + (ev.span ? String(ev.span) : String(ev.run_ref || ''));
      kids.push(el('a', {
        class: 'dn-instr-link dn-instr-t-' + verdictTone(ev.verdict),
        href: ctx.href('instrument', { epochId: d.epochId, reflectionId: d.reflectionId, judge: j.judge_name, runRef: ev.run_ref }),
        title: 'open the adjudication x-ray for ' + ev.run_ref, text: label,
      }));
    });
    card.appendChild(el('p', { class: 'dn-faint dn-instr-card-ev' }, kids));
  }
  // What to do about this judge. First-class in the record's schema and
  // rendered by NEITHER surface until now — a card that reports a judge's
  // precision without its remedy leaves the operator to re-derive one.
  if (j.recommendation) {
    card.appendChild(el('p', { class: 'dn-instr-frow-rec', text: 'recommend · ' + String(j.recommendation) }));
  }
  return card;
}

function cmCell(label, n, capText, tone) {
  return el('div', { class: 'dn-instr-cmcell dn-instr-t-' + tone }, [
    el('span', { class: 'dn-instr-cm-lab', text: label }),
    el('span', { class: 'dn-instr-cm-n', text: isNum(n) ? String(n) : '0' }),
    el('span', { class: 'dn-instr-cm-cap dn-faint', text: capText }),
  ]);
}

// ====================================================================
// X-RAY — the annotated transcript (left) + judge vs adjudication (right).
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
  // the fidelity tier is metadata → a dn-faint caption, not a highlighted tag.
  pane.appendChild(caption('transcript · ' + fidelityLabel(transcript.fidelity)));
  const turns = Array.isArray(transcript.turns) ? transcript.turns : [];
  if (transcript.fidelity === 'unavailable' || !turns.length) {
    pane.appendChild(el('p', { class: 'dn-empty', text: 'Transcript unavailable — the verbatim judge_io / result.json capture was not retained for this run (the events-preview tier needs the dashboard reconstructor and is not read here).' }));
    return pane;
  }
  const body = el('div', { class: 'dn-instr-transcript' });
  // Highlight the evidence span in the FIRST turn it occurs in only — the span
  // is one adjudicated location, so a `found` latch stops a common substring
  // from lighting up in every later turn too.
  let found = false;
  turns.forEach((t, i) => {
    const text = String(t);
    const useSpan = !found && span && text.indexOf(span) >= 0;
    if (useSpan) found = true;
    body.appendChild(el('div', { class: 'dn-instr-turn' }, [
      el('div', { class: 'dn-instr-turn-role dn-faint', text: 'turn ' + (i + 1) }),
      el('div', { class: 'dn-instr-turn-body' }, useSpan ? highlightSpan(text, span, tone) : [text]),
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

  // the meta-judge ADJUDICATION. The VERDICT keeps ONE pill (verdict IS the
  // semantic state the console pills); everything else is a dn-faint caption.
  if (!adj) {
    pane.appendChild(el('div', { class: 'dn-instr-xsub' }, [
      el('div', { class: 'dn-instr-xsub-h', text: 'adjudication' }),
      el('p', { class: 'dn-faint', text: 'no adjudication record — this decision was not adjudicated (or the record is unavailable).' }),
    ]));
    return pane;
  }
  const tone = verdictTone(adj.verdict);
  // fidelity · meta-judge model · prompt version · self-agreement — ONE caption.
  const metaBits = [fidelityLabel(adj.fidelity)];
  if (adj.meta_judge_model) metaBits.push('meta-judge ' + adj.meta_judge_model);
  if (isNum(adj.prompt_version)) metaBits.push('prompt v' + adj.prompt_version);
  if (isNum(adj.adjudicator_self_agreement)) metaBits.push('self-agreement ' + fmt(adj.adjudicator_self_agreement, 2));
  pane.appendChild(el('div', { class: 'dn-instr-xsub' }, [
    el('div', { class: 'dn-instr-xsub-h' }, [
      'adjudication ', chip('instr-verdict-' + tone, String(adj.verdict || 'ambiguous')),
    ]),
    adj.meta_judge_rationale ? el('p', { class: 'dn-instr-xwhy', text: String(adj.meta_judge_rationale) }) : null,
    severityMatchLine(adj, jv),
    caption(metaBits.join(' · ')),
  ].filter(Boolean)));
  return pane;
}

// Did the adjudicator agree with the judge's CLAIMED severity? Severity
// correctness is tracked APART from fire/silence (BOARD-REFLECTION.md, judge
// audit — confusion-matrix definitions):
// a judge that fires on the right span at the wrong severity passes the 2×2
// and still mis-weights the loss, so the verdict pill alone overstates it.
// The adjudicator scores this on a TP only, so
// `severity_match` is null everywhere else — and a null renders NOTHING (the
// aggregate `severity_accuracy` on the scorecard is the only other home).
function severityMatchLine(adj, jv) {
  if (adj.severity_match !== true && adj.severity_match !== false) return null;
  const claimed = jv && jv.severity ? String(jv.severity) : '';
  const text = adj.severity_match
    ? 'severity agrees' + (claimed ? ' · ' + claimed : '')
    : 'severity mismatch' + (claimed ? ' · the judge claimed ' + claimed : '')
      + ' — a correct fire at the wrong severity still mis-weights the loss';
  return el('p', {
    class: 'dn-instr-xsevmatch dn-instr-t-' + (adj.severity_match ? 'good' : 'bad'),
    text,
  });
}

function fidelityLabel(fidelity) {
  const f = String(fidelity || 'unavailable');
  if (f === 'verbatim') return 'verbatim (exact judge input)';
  if (f === 'result') return 'result.json (reconstructed)';
  if (f === 'unavailable') return 'unavailable';
  return f;
}

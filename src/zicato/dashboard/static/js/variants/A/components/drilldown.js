// variants/A/components/drilldown.js — Theme 3 depth 2: the instrument
// panel that slides in when a sortie tile is clicked.
//
// Renders, for one board entry under one candidate:
//   * the entry header (kind, budget, the resolved status lamp),
//   * its expectation outcomes (/api/run/.../expectations),
//   * its per-judge loss bars (/api/run/.../per-judge),
//   * and a deeper link that opens the run transcript
//     (/api/conversation/{run_id}) — depth 3.
//
// Pure builders: (data) -> DOM node. The fetching is the view's job; the
// builders take already-resolved data (or null while loading) and degrade
// gracefully when fields are missing.

import { el } from '../../../core/dom.js';
import { bar, chip, empty, loading } from './instruments.js';

function fmt(v, d = 1) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(d) : '—'; }

// expectation outcomes block.
//   data: { outcomes: [{ kind, passed, detail, judge_name, score }] } | null
export function expectationsBlock(data) {
  if (data == null) return loading('Reading expectation outcomes');
  const outcomes = Array.isArray(data.outcomes) ? data.outcomes : [];
  if (!outcomes.length) return empty('No expectation recorded for this entry.');
  const list = el('div', { class: 'mcA-drill-exps' });
  for (const o of outcomes) {
    const passed = o.passed === true;
    const failed = o.passed === false;
    const light = passed ? 'go' : failed ? 'stop' : 'idle';
    list.appendChild(el('div', { class: 'mcA-drill-exp' }, [
      el('span', { class: 'mcA-drill-exp-mark', 'data-light': light }, [passed ? '✓' : failed ? '✗' : '·']),
      el('div', { class: 'mcA-drill-exp-body' }, [
        el('div', { class: 'mcA-drill-exp-kind mono' }, [
          (o.kind || 'expectation'),
          o.judge_name ? el('span', { class: 'mcA-drill-exp-judge' }, ['  · ' + o.judge_name]) : null,
          typeof o.score === 'number' ? el('span', { class: 'mcA-drill-exp-judge' }, ['  · score ' + fmt(o.score, 2)]) : null,
        ]),
        o.detail ? el('div', { class: 'mcA-drill-exp-detail' }, [o.detail]) : null,
      ]),
    ]));
  }
  return list;
}

// per-judge loss bars.
//   data: { judges: [{ judge_name, weighted_loss, raw_loss, run_count, weight }] } | null
export function perJudgeBars(data) {
  if (data == null) return loading('Reading per-judge loss');
  const judges = Array.isArray(data.judges) ? data.judges : [];
  if (!judges.length) return empty('No process judges scored this entry.');
  let max = 0;
  for (const j of judges) {
    const v = typeof j.weighted_loss === 'number' ? j.weighted_loss : j.raw_loss;
    if (typeof v === 'number' && isFinite(v)) max = Math.max(max, Math.abs(v));
  }
  max = max || 1;
  const wrap = el('div', { class: 'mcA-drill-judges' });
  for (const j of judges) {
    const v = typeof j.weighted_loss === 'number' ? j.weighted_loss
      : (typeof j.raw_loss === 'number' ? j.raw_loss : null);
    wrap.appendChild(el('div', { class: 'mcA-drill-judge' }, [
      el('div', { class: 'mcA-drill-judge-name mono' }, [j.judge_name || 'judge']),
      el('div', { class: 'mcA-drill-judge-bar' }, [bar(v != null ? Math.abs(v) / max : 0, 'stop')]),
      el('span', { class: 'mcA-drill-judge-val mono' }, [fmt(v, 1)]),
      j.weight != null ? el('span', { class: 'mcA-drill-judge-weight mono' }, ['×' + fmt(j.weight, 1)]) : null,
    ]));
  }
  return wrap;
}

// The whole slide-in instrument panel for one entry.
//   ctx: {
//     entry,                 // board entry
//     score,                 // per-entry score (lamp + loss)
//     expectations,          // /expectations payload | null
//     perJudge,              // /per-judge payload | null
//     runId,                 // run_id from per-entry (depth-3 link)
//     onOpenRun(runId),      // navigate to the transcript
//     onClose(),             // dismiss
//   }
export function instrumentPanel(ctx) {
  ctx = ctx || {};
  const entry = ctx.entry || {};
  const score = ctx.score || null;

  const head = el('div', { class: 'mcA-drill-head' }, [
    el('div', null, [
      el('div', { class: 'mcA-drill-eyebrow mono' }, ['instrument · board entry']),
      el('div', { class: 'mcA-drill-title mono' }, [entry.id || '?']),
    ]),
    ctx.onClose
      ? el('button', { class: 'mcA-drill-close', type: 'button', 'aria-label': 'close instrument panel', onclick: ctx.onClose }, ['✕'])
      : null,
  ]);

  // top vitals: kind · budget · loss · pass
  const vitals = el('div', { class: 'mcA-drill-vitals mono' }, [
    el('span', null, [entry.kind || 'single_turn']),
    el('span', null, ['⏱ ' + (entry.budget_s != null ? fmt(entry.budget_s, 0) + 's' : '—')]),
    score && typeof score.drift_loss === 'number'
      ? el('span', { class: 'mcA-drill-vital-loss' }, ['loss ' + fmt(score.drift_loss, 1)]) : null,
    score
      ? chip(score.wall_clock_budget_exceeded ? 'timeout' : score.pass_fail === 1 ? 'pass' : score.pass_fail === 0 ? 'fail' : 'no predicate',
          score.wall_clock_budget_exceeded ? 'warn' : score.pass_fail === 1 ? 'go' : score.pass_fail === 0 ? 'stop' : 'idle')
      : null,
  ]);

  const body = el('div', { class: 'mcA-drill-body' }, [
    el('div', { class: 'mcA-drill-section' }, [
      el('div', { class: 'mcA-drill-section-title mono' }, ['expectation outcomes']),
      expectationsBlock(ctx.expectations),
    ]),
    el('div', { class: 'mcA-drill-section' }, [
      el('div', { class: 'mcA-drill-section-title mono' }, ['per-judge loss']),
      perJudgeBars(ctx.perJudge),
    ]),
  ]);

  const foot = el('div', { class: 'mcA-drill-foot' }, [
    ctx.runId && ctx.onOpenRun
      ? el('button', { class: 'mcA-btn', type: 'button', onclick: () => ctx.onOpenRun(ctx.runId) },
          ['open run transcript →'])
      : el('span', { class: 'mcA-readout-foot' }, ['no run id recorded for this entry']),
  ]);

  return el('div', { class: 'mcA-drill', 'data-light': score ? (score.wall_clock_budget_exceeded ? 'warn' : score.pass_fail === 1 ? 'go' : 'stop') : 'idle' },
    [head, vitals, body, foot]);
}

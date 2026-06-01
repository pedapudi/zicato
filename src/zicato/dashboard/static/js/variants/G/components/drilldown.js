// variants/G/components/drilldown.js — the per-entry instrument panel.
//
// Theme 3 depth 2: when a board entry is selected (a dot in the per-board
// dot-plot), this builds the entry's detail — expectation outcomes,
// per-judge loss bars, and a link to the transcript (depth 3). Pure
// builder; the VIEW owns the selection state in module scope and only
// rebuilds this when the SELECTED entry actually changes (A bug #1 fix:
// no rebuild on a heartbeat tick).

import { el } from '../../../core/dom.js';
import { chip, empty, loading, fmt } from './ui.js';
import { valueBars } from '../svg.js';

function expectationsBlock(data) {
  if (data == null) return loading('Reading expectation outcomes');
  const outcomes = Array.isArray(data.outcomes) ? data.outcomes : [];
  if (!outcomes.length) return empty('No expectation recorded for this entry.');
  const list = el('div', { class: 'g-drill-exps' });
  for (const o of outcomes) {
    const passed = o.passed === true;
    const failed = o.passed === false;
    const light = passed ? 'improve' : failed ? 'regress' : 'idle';
    list.appendChild(el('div', { class: 'g-drill-exp' }, [
      el('span', { class: 'g-drill-exp-mark', dataset: { light } }, [passed ? '✓' : failed ? '✗' : '·']),
      el('div', { class: 'g-drill-exp-body' }, [
        el('div', { class: 'g-drill-exp-kind g-mono' }, [
          (o.kind || 'expectation'),
          o.judge_name ? el('span', { class: 'g-drill-exp-judge' }, ['  · ' + o.judge_name]) : null,
          typeof o.score === 'number' ? el('span', { class: 'g-drill-exp-judge' }, ['  · score ' + fmt(o.score, 2)]) : null,
        ]),
        o.detail ? el('div', { class: 'g-drill-exp-detail' }, [o.detail]) : null,
      ]),
    ]));
  }
  return list;
}

function perJudgeBlock(data) {
  if (data == null) return loading('Reading per-judge loss');
  const judges = Array.isArray(data.judges) ? data.judges : [];
  if (!judges.length) return empty('No process judges scored this entry.');
  return valueBars({
    width: 380, labelWidth: 150,
    items: judges.map((j) => ({
      label: (j.judge_name || 'judge') + (j.weight != null ? ' ×' + fmt(j.weight, 1) : ''),
      value: typeof j.weighted_loss === 'number' ? j.weighted_loss : j.raw_loss,
    })),
  });
}

// The instrument panel for one selected board entry.
//   ctx: { entry, score, expectations, perJudge, runId, onOpenRun, onClose }
export function instrumentPanel(ctx) {
  ctx = ctx || {};
  const entry = ctx.entry || {};
  const score = ctx.score || null;

  const head = el('div', { class: 'g-drill-head' }, [
    el('div', null, [
      el('div', { class: 'g-drill-eyebrow g-mono' }, ['board entry · detail']),
      el('div', { class: 'g-drill-title g-mono' }, [entry.id || '?']),
    ]),
    ctx.onClose
      ? el('button', { class: 'g-drill-close', type: 'button', 'aria-label': 'close detail', onclick: ctx.onClose }, ['✕'])
      : null,
  ]);

  const vitals = el('div', { class: 'g-drill-vitals g-mono' }, [
    el('span', null, [entry.kind || 'single_turn']),
    el('span', null, ['⏱ ' + (entry.budget_s != null ? fmt(entry.budget_s, 0) + 's' : '—')]),
    score && typeof score.driftLoss === 'number'
      ? el('span', { class: 'g-drill-vital-loss' }, ['loss ' + fmt(score.driftLoss, 1)]) : null,
    score
      ? chip(score.timeout ? 'timeout' : score.passFail === 1 ? 'pass' : score.passFail === 0 ? 'fail' : 'no predicate',
          score.timeout ? 'caution' : score.passFail === 1 ? 'improve' : score.passFail === 0 ? 'regress' : 'idle')
      : null,
  ]);

  const body = el('div', { class: 'g-drill-body' }, [
    el('div', { class: 'g-drill-section' }, [
      el('div', { class: 'g-drill-section-title g-mono' }, ['expectation outcomes']),
      expectationsBlock(ctx.expectations),
    ]),
    el('div', { class: 'g-drill-section' }, [
      el('div', { class: 'g-drill-section-title g-mono' }, ['per-judge loss']),
      perJudgeBlock(ctx.perJudge),
    ]),
  ]);

  const foot = el('div', { class: 'g-drill-foot' }, [
    ctx.runId && ctx.onOpenRun
      ? el('button', { class: 'g-btn', type: 'button', onclick: () => ctx.onOpenRun(ctx.runId) }, ['open run transcript →'])
      : el('span', { class: 'g-readout-foot' }, ['no run id recorded for this entry']),
  ]);

  return el('div', { class: 'g-drill' }, [head, vitals, body, foot]);
}

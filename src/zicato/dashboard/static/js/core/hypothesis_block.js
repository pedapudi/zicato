// core/hypothesis_block.js — shared "Proposed (before) / Outcome (after)"
// renderer used by L1 (Recent experiments) and L2 (per-generation
// Hypothesis section).
//
// The block reads the standard experiment shape persisted on disk:
//
//   hypothesis: {
//     core_idea, why, risks, modulating[],
//     expected_pass_rate_delta, expected_drift_movements[],
//     expected_metric_movements[],
//   }
//   outcome: {
//     summary, tournament_decision | decision,
//     scalar_score_delta, pass_rate_delta, drift_loss_delta,
//     rejection_reason, ran_at,
//   }
//
// ``options.compact`` (default ``false``) controls density:
//   * full   (L2 — one experiment per page) — full prose lines for
//     why / predicted / risks / modulating, full outcome metric strip.
//   * compact (L1 — six experiments in a list) — terse one-line prose
//     and only the headline metric numbers, no badges or links.
//
// The two callers MUST share this module so L1 and L2 cannot drift on
// what counts as a "Proposed (before)" block. The JS test harness
// pins both shapes against the same input.

import { el } from './dom.js';

function _isStr(v) { return typeof v === 'string' && v.trim() !== ''; }

function _isNum(v) {
  return typeof v === 'number' && isFinite(v);
}

function _fmtSigned(v, digits) {
  if (!_isNum(v)) return '—';
  const d = digits == null ? 3 : digits;
  return (v > 0 ? '+' : '') + v.toFixed(d);
}

// Normalise the verdict text into one of promoted / rejected /
// deferred / null. Used for badge classes on the compact view.
function _normaliseDecision(outcome) {
  if (!outcome || typeof outcome !== 'object') return null;
  const raw = outcome.tournament_decision || outcome.decision || '';
  const d = String(raw).toLowerCase();
  if (d.includes('promot')) return 'promoted';
  if (d.includes('reject')) return 'rejected';
  if (d.includes('defer')) return 'deferred';
  return raw ? d : null;
}

function _expectedDriftLine(moves) {
  if (!Array.isArray(moves) || moves.length === 0) return null;
  const items = moves
    .map((m) => (m && m.kind)
      ? `${m.kind} ${m.direction || '?'}${m.magnitude ? ' (' + m.magnitude + ')' : ''}`
      : null)
    .filter(Boolean);
  return items.length ? items.join('; ') : null;
}

function _expectedMetricLine(moves) {
  if (!Array.isArray(moves) || moves.length === 0) return null;
  const items = moves
    .map((m) => (m && m.name)
      ? `${m.name} ${m.direction || '?'}${m.magnitude ? ' (' + m.magnitude + ')' : ''}`
      : null)
    .filter(Boolean);
  return items.length ? items.join('; ') : null;
}

// Build a labelled prose row: "<lead>. <body>". The lead label is
// bold so the eye can scan a long Outcome / Proposed block by lead.
function _labeled(lead, body) {
  return el('p', { class: 'hyp-line' }, [
    el('strong', { class: 'hyp-lead' }, [lead]),
    ' ',
    String(body),
  ]);
}

// ---------------------------------------------------------------------------
// Proposed (before) — read the hypothesis fields.
// ---------------------------------------------------------------------------

function _renderProposed(hyp, compact) {
  const wrap = el('div', { class: 'hyp-proposed' });
  wrap.appendChild(el('h4', { class: 'hyp-block-h' }, ['Proposed (before)']));
  let any = false;

  if (_isStr(hyp.core_idea)) {
    wrap.appendChild(el('p', { class: 'hyp-core' }, [hyp.core_idea]));
    any = true;
  }

  if (_isStr(hyp.why)) {
    wrap.appendChild(_labeled('Why.', hyp.why));
    any = true;
  }

  if (_isStr(hyp.expected_pass_rate_delta)) {
    wrap.appendChild(_labeled('Expected pass-rate Δ.', hyp.expected_pass_rate_delta));
    any = true;
  }

  const driftLine = _expectedDriftLine(hyp.expected_drift_movements);
  if (driftLine) {
    wrap.appendChild(_labeled('Predicted drift.', driftLine));
    any = true;
  }

  // The compact view stops here. The full view continues with risks,
  // metric movements and modulating sites — the long-form fields.
  if (!compact) {
    const metricLine = _expectedMetricLine(hyp.expected_metric_movements);
    if (metricLine) {
      wrap.appendChild(_labeled('Predicted metrics.', metricLine));
      any = true;
    }
    if (_isStr(hyp.risks)) {
      wrap.appendChild(_labeled('Risks.', hyp.risks));
      any = true;
    }
    const modulating = Array.isArray(hyp.modulating) ? hyp.modulating : [];
    if (modulating.length > 0) {
      const sites = el('p', { class: 'hyp-line' }, [
        el('strong', { class: 'hyp-lead' }, ['Modulating.']),
        ' ',
      ]);
      for (let i = 0; i < modulating.length; i += 1) {
        if (i > 0) sites.appendChild(el('span', null, [', ']));
        sites.appendChild(el('code', { class: 'mono code-pill' }, [String(modulating[i])]));
      }
      wrap.appendChild(sites);
      any = true;
    }
  }

  if (!any) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No structured rationale recorded.']));
  }
  return wrap;
}

// ---------------------------------------------------------------------------
// Outcome (after) — read the tournament-stamped outcome fields.
// ---------------------------------------------------------------------------

function _renderOutcome(outcome, compact) {
  const wrap = el('div', { class: 'hyp-outcome' });
  wrap.appendChild(el('h4', { class: 'hyp-block-h' }, ['Outcome (after)']));

  if (!outcome || typeof outcome !== 'object') {
    wrap.appendChild(el('p', { class: 'empty' }, [
      'No tournament verdict recorded — the experiment is incomplete.',
    ]));
    return wrap;
  }

  const decision = _normaliseDecision(outcome);
  const verdictBadgeCls = decision === 'promoted' ? 'promoted'
    : decision === 'rejected' ? 'rejected'
    : decision === 'deferred' ? 'deferred' : 'pending';

  // Verdict line — badge + text. The compact view keeps just the badge.
  const verdictLine = el('p', { class: 'hyp-line hyp-verdict' });
  verdictLine.appendChild(el('span', {
    class: 'badge ' + verdictBadgeCls,
  }, [decision || '?']));
  if (!compact) {
    const text = decision === 'promoted'
      ? ' Challenger beat the champion — promoted.'
      : decision === 'rejected'
        ? ' Challenger did not beat the champion — rejected.'
        : decision === 'deferred'
          ? ' No decisive winner — kept for analysis.'
          : ' Verdict recorded.';
    verdictLine.appendChild(el('span', { class: 'hyp-verdict-text' }, [text]));
  }
  wrap.appendChild(verdictLine);

  // Metric numbers. Lower drift / scalar loss is better; higher pass-
  // rate Δ is better. The compact view drops the labels' "(lower is
  // better)" affordance — the test fixtures and tooltips carry that.
  const metric = (label, value, goodIsNeg) => {
    let cls = '';
    if (_isNum(value) && value !== 0) {
      const good = goodIsNeg ? value < 0 : value > 0;
      cls = good ? ' good' : ' bad';
    }
    return el('span', { class: 'hyp-metric' }, [
      el('span', { class: 'hyp-metric-value mono' + cls },
        [_isNum(value) ? _fmtSigned(value) : '—']),
      el('span', { class: 'hyp-metric-label' }, [label]),
    ]);
  };

  const metrics = el('div', { class: 'hyp-metric-strip' });
  metrics.appendChild(metric('Δscalar', outcome.scalar_score_delta, true));
  metrics.appendChild(metric('Δpass', outcome.pass_rate_delta, false));
  metrics.appendChild(metric('Δdrift', outcome.drift_loss_delta, true));
  wrap.appendChild(metrics);

  // Long-form fields — full mode only.
  if (!compact) {
    if (_isStr(outcome.summary)) {
      wrap.appendChild(_labeled('Summary.', outcome.summary));
    }
    if (_isStr(outcome.rejection_reason)) {
      wrap.appendChild(_labeled('Rejection reason.', outcome.rejection_reason));
    }
    if (_isStr(outcome.ran_at)) {
      wrap.appendChild(el('p', { class: 'meta mono hyp-ran-at' }, [
        'evaluated ', outcome.ran_at,
      ]));
    }
  }
  return wrap;
}

// ---------------------------------------------------------------------------
// Public entrypoint.
// ---------------------------------------------------------------------------

// Build the full Proposed/Outcome block. Always returns an element so
// the caller can attach it unconditionally; the renderer handles empty
// inputs internally with "empty" placeholders.
export function renderHypothesisOutcomeCompact(hypothesis, outcome, options) {
  const opts = (options && typeof options === 'object') ? options : {};
  const compact = opts.compact === true;
  const hyp = (hypothesis && typeof hypothesis === 'object') ? hypothesis : {};
  const block = el('div', {
    class: 'hyp-block' + (compact ? ' hyp-block-compact' : ' hyp-block-full'),
  });
  block.appendChild(_renderProposed(hyp, compact));
  block.appendChild(_renderOutcome(outcome, compact));
  return block;
}

// components/gate_ladder.js — the promote-gate as a vertical checklist.
//
// The promote gate is an ORDERED set of rules (see tournament/gate.py):
//   1. regression suite       (the child must still pass its guards)
//   2. scalar margin          (loss must drop by >= promote_margin)
//   3. pass-rate monotonicity (no entry the parent passed may regress)
//   4. namespace monotonicity (no tracked namespace may move worse)
//
// Rules short-circuit: the first one that rejects decides, and the rest
// are never reached. This component makes that legible — each rule is a
// row in evaluation order with a glyph that encodes its status, and the
// ONE rule that actually fired the rejection is emphasized so the reader
// sees at a glance why a child was rejected.
//
//   status ∈ 'pass' | 'fail' | 'skipped' | 'not_reached'
//     pass        → ✓ green
//     fail        → ✗ red
//     skipped     → ◦ grey (rule disabled / not applicable)
//     not_reached → ◦ grey (an earlier rule already decided)

import { el } from '../core/dom.js';

const _STATUS = {
  pass:        { glyph: '✓', cls: 'gate-pass' },
  fail:        { glyph: '✗', cls: 'gate-fail' },
  skipped:     { glyph: '◦', cls: 'gate-muted' },
  not_reached: { glyph: '◦', cls: 'gate-muted' },
};

/**
 * Render the gate ladder.
 *
 * rules — ORDERED array of { id, label, status, detail, fired }.
 *   status — one of the keys above (unknown → muted dot).
 *   detail — optional secondary text (numbers render fine in mono).
 *   fired  — true on the single rule that decided a rejection; that row
 *            is visually emphasized.
 */
export function gateLadder({ rules } = {}) {
  const list = Array.isArray(rules) ? rules : [];
  const wrap = el('ol', { class: 'gate-ladder', role: 'list' });
  if (list.length === 0) {
    wrap.appendChild(el('li', { class: 'gate-row gate-empty' }, [
      el('span', { class: 'gate-label' }, ['No gate rules.']),
    ]));
    return wrap;
  }
  for (const rule of list) {
    const status = (rule && rule.status) || 'not_reached';
    const spec = _STATUS[status] || _STATUS.not_reached;
    const fired = !!(rule && rule.fired);
    const row = el('li', {
      class: 'gate-row ' + spec.cls + (fired ? ' gate-fired' : ''),
      'data-status': status,
    }, [
      el('span', { class: 'gate-glyph', 'aria-hidden': 'true' }, [spec.glyph]),
      el('span', { class: 'gate-label' }, [String((rule && rule.label) != null ? rule.label : (rule && rule.id) || '')]),
      (rule && rule.detail != null)
        ? el('span', { class: 'gate-detail mono' }, [String(rule.detail)])
        : null,
    ]);
    if (fired) row.setAttribute('aria-current', 'true');
    wrap.appendChild(row);
  }
  return wrap;
}

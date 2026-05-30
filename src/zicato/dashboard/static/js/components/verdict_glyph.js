// components/verdict_glyph.js — the unified verdict glyph + label.
//
// This is the SINGLE source of verdict iconography for the whole
// dashboard. Wherever a generation's tournament outcome is shown — the
// spine, a row, a header — it speaks through this factory so the glyph,
// the color, and the wording stay identical everywhere.
//
// Map:
//   promoted          → ✓  (success)
//   rejected          → ✗  (error)
//   deferred | open   → ◦  (neutral / muted)
//   pending           → ·  (muted)
//
// Color is REDUNDANT to the glyph and the label, so the affordance
// survives a grayscale screenshot or a color-blind reader.

import { el } from '../core/dom.js';

// decision → { glyph, label, kind }. `kind` names the semantic color
// class; unknown decisions fall back to the pending dot.
const _VERDICT = {
  promoted: { glyph: '✓', label: 'promoted', kind: 'promoted' },
  rejected: { glyph: '✗', label: 'rejected', kind: 'rejected' },
  deferred: { glyph: '◦', label: 'deferred', kind: 'neutral' },
  open:     { glyph: '◦', label: 'open', kind: 'neutral' },
  pending:  { glyph: '·', label: 'pending', kind: 'muted' },
};

/**
 * Render the verdict glyph + (optional) label.
 *
 * decision — 'promoted' | 'rejected' | 'deferred' | 'open' | 'pending'
 * opts.withLabel — when true (default) the word is shown beside the glyph.
 */
export function verdictGlyph(decision, opts = { withLabel: true }) {
  const key = String(decision == null ? '' : decision).toLowerCase();
  const spec = _VERDICT[key] || _VERDICT.pending;
  const withLabel = opts && opts.withLabel !== false;
  const children = [
    el('span', { class: 'vglyph-mark', 'aria-hidden': 'true' }, [spec.glyph]),
  ];
  if (withLabel) {
    children.push(el('span', { class: 'vglyph-label' }, [spec.label]));
  }
  return el('span', {
    class: `vglyph vglyph-${spec.kind}`,
    role: 'img',
    'aria-label': spec.label,
    title: spec.label,
  }, children);
}

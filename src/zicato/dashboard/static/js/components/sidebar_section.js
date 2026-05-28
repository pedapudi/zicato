// components/sidebar_section.js — section eyebrow for the dashboard sidebar.
//
// Every sidebar block (Live activity, Files, Search) wears the same hat
// so the eye can scan top-to-bottom: a small-caps eyebrow on the left
// (optionally prefixed with an icon) and an optional adornment slot on
// the right (a pulsing live dot, a count, etc.). Kept as a pure factory
// so a view-level test can render it in isolation.
//
// Markup contract (pinned by the sidebar tests):
//
//   <div class="phase0-sidebar-section-header">
//     <span class="phase0-sidebar-section-eyebrow">
//       <svg class="phase0-sidebar-section-icon">…</svg>?
//       <span class="phase0-sidebar-section-label">LABEL</span>
//     </span>
//     <span class="phase0-sidebar-section-adorn">…?</span>
//   </div>

import { el, svgEl } from '../core/dom.js';

/**
 * Render the section header row.
 *
 * opts:
 *   label   — string, the eyebrow text (rendered uppercase via CSS).
 *   icon    — optional sprite id (e.g. "icon-activity"); rendered as a
 *             small <svg><use> ahead of the label.
 *   adorn   — optional DOM node placed on the right side of the row
 *             (typical: a live indicator or a count chip).
 */
export function renderSidebarSection({ label, icon, adorn } = {}) {
  const eyebrowKids = [];
  if (icon) {
    const svg = svgEl('svg', {
      class: 'phase0-sidebar-section-icon',
      'aria-hidden': 'true',
      width: '12',
      height: '12',
      viewBox: '0 0 20 20',
    });
    svg.appendChild(svgEl('use', { href: '/static/icons.svg#' + icon }));
    eyebrowKids.push(svg);
  }
  eyebrowKids.push(el('span', {
    class: 'phase0-sidebar-section-label',
  }, [String(label || '')]));
  const children = [
    el('span', { class: 'phase0-sidebar-section-eyebrow' }, eyebrowKids),
  ];
  if (adorn) {
    children.push(el('span', {
      class: 'phase0-sidebar-section-adorn',
    }, [adorn]));
  }
  return el('div', {
    class: 'phase0-sidebar-section-header',
  }, children);
}

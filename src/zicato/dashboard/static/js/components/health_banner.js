// components/health_banner.js — the loop-health banner.
//
// The evolve loop's health (is the loss surface even moving? does any
// board entry discriminate the candidates?) is computed by
// zicato.health.diagnostics and exposed at GET /api/health-report, but
// it has had nowhere to render. This banner is that surface: a compact,
// full-width strip that answers "is this loop even meaningful right now"
// at a glance.
//
// Colour follows the highest severity present:
//   * green  — healthy, or no findings at all,
//   * amber  — a top `warning` (and no critical),
//   * red    — any `critical`.
//
// The banner shows the highest-severity finding's `summary`. A
// "details" toggle reveals that finding's `detail` text plus every
// remaining finding. When `report` is null / absent it degrades to a
// muted "health not yet evaluated" line — it never throws.
//
// Re-render safe: a pure factory returning a fresh detached node.

import { el } from '../core/dom.js';

// Severity → rank (higher wins) + the banner tone it paints.
const SEVERITY_RANK = { critical: 3, warning: 2, info: 1 };
const SEVERITY_TONE = {
  critical: 'err',
  warning: 'warn',
  info: 'info',
};

function _rankOf(severity) {
  return SEVERITY_RANK[String(severity || '').toLowerCase()] || 0;
}

// The single highest-severity finding (ties broken by input order).
function _topFinding(findings) {
  let top = null;
  let topRank = 0;
  for (const f of findings) {
    const r = _rankOf(f && f.severity);
    if (r > topRank) { topRank = r; top = f; }
  }
  return top;
}

// Render `detail` — a JSON-friendly dict of structured specifics — as a
// small set of key: value rows. A string detail renders verbatim.
function _renderDetail(detail) {
  if (detail == null) return null;
  if (typeof detail === 'string') {
    return el('p', { class: 'health-detail-text' }, [detail]);
  }
  if (typeof detail !== 'object') {
    return el('p', { class: 'health-detail-text' }, [String(detail)]);
  }
  const rows = [];
  for (const [k, v] of Object.entries(detail)) {
    let valueText;
    if (v == null) valueText = '—';
    else if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
      valueText = String(v);
    } else {
      try { valueText = JSON.stringify(v); } catch { valueText = String(v); }
    }
    rows.push(el('div', { class: 'health-detail-row' }, [
      el('span', { class: 'health-detail-key mono' }, [String(k)]),
      el('span', { class: 'health-detail-value mono' }, [valueText]),
    ]));
  }
  if (rows.length === 0) return null;
  return el('div', { class: 'health-detail-grid' }, rows);
}

// One finding rendered inside the expandable detail region.
function _renderFindingBlock(finding, { lead = false } = {}) {
  const severity = String((finding && finding.severity) || 'info').toLowerCase();
  const code = finding && finding.code != null ? String(finding.code) : '';
  const summary = finding && finding.summary != null ? String(finding.summary) : '';
  const children = [
    el('div', { class: 'health-finding-head' }, [
      el('span', { class: `health-sev health-sev-${severity}` }, [severity]),
      code ? el('span', { class: 'health-finding-code mono' }, [code]) : null,
    ]),
  ];
  if (summary) children.push(el('p', { class: 'health-finding-summary' }, [summary]));
  const detailNode = _renderDetail(finding && finding.detail);
  if (detailNode) children.push(detailNode);
  return el('div', {
    class: `health-finding health-finding-${severity}${lead ? ' health-finding-lead' : ''}`,
  }, children);
}

/**
 * Build the loop-health banner.
 *
 * opts:
 *   report — { healthy: bool, findings: [{ code, severity, summary,
 *             detail }] } | null. A null / undefined / non-object report
 *             renders the muted "not yet evaluated" line.
 *   onToggleDetail — optional (open: bool) => void, called when the user
 *             expands / collapses the details region.
 */
export function healthBanner(opts) {
  const o = opts || {};
  const onToggleDetail = typeof o.onToggleDetail === 'function' ? o.onToggleDetail : null;
  const report = o.report;

  // -- absent report ---------------------------------------------------
  if (report == null || typeof report !== 'object') {
    return el('div', {
      class: 'health-banner health-banner-muted',
      role: 'status',
    }, [
      el('span', { class: 'health-banner-icon', 'aria-hidden': 'true' }, ['◦']),
      el('span', { class: 'health-banner-summary' }, ['health not yet evaluated']),
    ]);
  }

  const findings = Array.isArray(report.findings) ? report.findings.filter(Boolean) : [];
  const top = _topFinding(findings);

  // Healthy when the report says so and nothing warning/critical
  // surfaced. The tone is driven off the top finding's severity; absent
  // any actionable finding the banner is green ("ok").
  const actionable = top && _rankOf(top.severity) >= SEVERITY_RANK.warning;
  const tone = actionable ? SEVERITY_TONE[String(top.severity).toLowerCase()] : 'ok';
  const healthy = report.healthy !== false && !actionable;

  const icon = { ok: '✓', warn: '!', err: '✗', info: 'ℹ' }[tone] || '✓';
  const summaryText = top && top.summary
    ? String(top.summary)
    : (healthy ? 'loop healthy — optimization signal is live' : 'loop health degraded');

  const head = el('div', { class: 'health-banner-head' }, [
    el('span', { class: 'health-banner-icon', 'aria-hidden': 'true' }, [icon]),
    el('span', { class: 'health-banner-summary' }, [summaryText]),
  ]);

  // Remaining findings (everything but the lead one) feed the detail
  // region, alongside the lead finding's own detail.
  const rest = top ? findings.filter((f) => f !== top) : findings;
  const hasDetail = !!(top && (top.detail != null || top.summary)) || rest.length > 0;

  const children = [head];

  if (hasDetail) {
    const detailRegion = el('div', {
      class: 'health-banner-detail',
      hidden: 'hidden',
    }, [
      top ? _renderFindingBlock(top, { lead: true }) : null,
      ...rest.map((f) => _renderFindingBlock(f)),
    ].filter(Boolean));

    let open = false;
    const toggle = el('button', {
      type: 'button',
      class: 'health-banner-toggle',
      'aria-expanded': 'false',
    }, ['details']);
    toggle.addEventListener('click', () => {
      open = !open;
      if (open) detailRegion.removeAttribute('hidden');
      else detailRegion.setAttribute('hidden', 'hidden');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (onToggleDetail) onToggleDetail(open);
    });

    head.appendChild(toggle);
    children.push(detailRegion);
  }

  return el('div', {
    class: `health-banner health-banner-${tone}`,
    role: 'status',
    'data-tone': tone,
    'data-healthy': healthy ? 'true' : 'false',
  }, children);
}

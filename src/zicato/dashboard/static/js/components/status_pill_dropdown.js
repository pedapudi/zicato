// components/status_pill_dropdown.js — the panel below the top bar.
//
// Painted into ``#phase0-status-dropdown`` when the status pill is
// clicked. Contents vary by state:
//
//   RUNNING — epoch / gen / round / runs / context lines, plus a
//             RECENT DECISIONS feed (capped at 3) and an
//             "Open current run" CTA.
//   IDLE    — epoch / gen labels (if heartbeat carries them), no run
//             count, "Open current epoch" CTA (when an epoch is loaded).
//   STALE   — header line + a muted "Last seen Xm ago" hint; no CTA.
//   CONNECTING — minimal "Connecting…" line.
//
// The body is rebuilt every open from current state; the panel itself
// is a stateless renderer.

import { el } from '../core/dom.js';
import { fmtDuration, parseIso, nowMs } from '../core/format.js';
import { state } from '../core/state.js';
import { phase0Href } from '../../js/views/phase0_router.js';
import { resolveStatusState } from './status_pill.js';

const RECENT_CAP = 3;

function _experimentList() {
  const def = state.epochDef;
  if (!def || typeof def !== 'object') return [];
  const xs = Array.isArray(def.experiments) ? def.experiments : [];
  return xs.slice().reverse().slice(0, RECENT_CAP);
}

function _experimentRow(exp) {
  const id = exp.generation_id || exp.gen_id || exp.id || '—';
  const out = exp.outcome && typeof exp.outcome === 'object'
    ? exp.outcome : {};
  const verdictRaw = exp.verdict
    || (typeof exp.outcome === 'string' ? exp.outcome : null)
    || out.tournament_decision
    || '';
  const verdict = String(verdictRaw).toLowerCase();
  const scalarRaw = exp.scalar != null ? exp.scalar
    : (out.scalar_score_delta != null ? out.scalar_score_delta : null);
  const scalar = scalarRaw != null ? Number(scalarRaw) : null;
  const scalarFmt = scalar == null || !isFinite(scalar)
    ? '—'
    : (scalar > 0 ? '+' : '') + scalar.toFixed(2);
  let mark = '·';
  let dataVariant = 'open';
  if (verdict.startsWith('prom') || verdict === 'accepted') {
    mark = '✓';
    dataVariant = 'promoted';
  } else if (verdict.startsWith('rej')) {
    mark = '✗';
    dataVariant = 'rejected';
  }
  return el('div', {
    class: 'phase0-status-recent-row',
    'data-variant': dataVariant,
  }, [
    el('span', { class: 'phase0-status-recent-id' }, [id]),
    el('span', { class: 'phase0-status-recent-mark' }, [mark]),
    el('span', { class: 'phase0-status-recent-verdict' },
      [verdict || 'open']),
    el('span', { class: 'phase0-status-recent-scalar' }, [scalarFmt]),
  ]);
}

function _kvRow(label, value) {
  return el('div', { class: 'phase0-status-kv-row' }, [
    el('span', { class: 'phase0-status-kv-key' }, [label]),
    el('span', { class: 'phase0-status-kv-value mono' },
      [value == null || value === '' ? '—' : String(value)]),
  ]);
}

function _elapsedFromStarted() {
  const hb = state.heartbeat || {};
  const startedRaw = hb.round_started_at || hb.started_at
    || hb.epoch_started_at;
  const startedMs = parseIso(startedRaw);
  if (!isFinite(startedMs)) return null;
  const secs = (nowMs() - startedMs) / 1000;
  if (secs <= 0) return null;
  return fmtDuration(secs);
}

function _lastSeenAgo() {
  const hb = state.heartbeat || {};
  const ms = parseIso(hb.last_heartbeat != null ? hb.last_heartbeat : hb.timestamp);
  if (!isFinite(ms)) return null;
  const ago = (nowMs() - ms) / 1000;
  if (ago < 0) return null;
  if (ago < 90) return Math.floor(ago) + 's';
  if (ago < 3600) return Math.floor(ago / 60) + 'm';
  return Math.floor(ago / 3600) + 'h';
}

// Build the panel body for the current status. Exported pure-render so
// tests can drive each state independently.
export function renderStatusDropdown() {
  const stateName = resolveStatusState();
  const hb = state.heartbeat || {};
  const root = el('div', {
    class: 'phase0-status-dropdown-panel',
    'data-state': stateName,
  });

  // Header — the state name as a bold line at the top.
  root.appendChild(el('div', { class: 'phase0-status-dropdown-header' }, [
    el('span', {
      class: 'phase0-status-dropdown-dot phase0-status-dropdown-dot-' + stateName,
      'aria-hidden': 'true',
    }),
    el('span', { class: 'phase0-status-dropdown-state' },
      [stateName.toUpperCase()]),
  ]));

  if (stateName === 'connecting') {
    root.appendChild(el('p', { class: 'phase0-status-dropdown-hint' },
      ['Waiting for the first heartbeat…']));
    return root;
  }

  if (stateName === 'stale') {
    // Show the last-known epoch/gen for context, plus the "last seen"
    // hint. No CTA — the run might not exist any more.
    if (hb.epoch_id) root.appendChild(_kvRow('epoch', hb.epoch_id));
    if (hb.generation_id) root.appendChild(_kvRow('gen', hb.generation_id));
    const ago = _lastSeenAgo();
    root.appendChild(el('p', { class: 'phase0-status-dropdown-hint' },
      ['Last seen ' + (ago || '—') + ' ago']));
    return root;
  }

  // Common KV block for RUNNING and IDLE — what little we know.
  const kvs = el('div', { class: 'phase0-status-dropdown-kvs' });
  if (hb.epoch_id) kvs.appendChild(_kvRow('epoch', hb.epoch_id));
  if (hb.generation_id) kvs.appendChild(_kvRow('gen', hb.generation_id));

  if (stateName === 'running') {
    if (hb.round_index != null) kvs.appendChild(_kvRow('round', hb.round_index));
    const activeCount = Array.isArray(state.activeRuns) ? state.activeRuns.length : 0;
    kvs.appendChild(_kvRow('runs', activeCount + ' in flight'));
    const elapsed = _elapsedFromStarted();
    if (elapsed) {
      const contextParts = [];
      if (hb.round_index != null) contextParts.push('Round ' + hb.round_index);
      contextParts.push(elapsed + ' elapsed');
      kvs.appendChild(_kvRow('context', contextParts.join(' · ')));
    }
  }
  root.appendChild(kvs);

  // Recent decisions feed — same source as the L0 card. Cap at three so
  // the dropdown reads as a glance, not a deep history.
  const recent = _experimentList();
  if (recent.length > 0) {
    root.appendChild(el('div', { class: 'phase0-status-dropdown-eyebrow' },
      ['RECENT DECISIONS']));
    const feed = el('div', { class: 'phase0-status-dropdown-recent' });
    for (const exp of recent) feed.appendChild(_experimentRow(exp));
    root.appendChild(feed);
  }

  // CTA — RUNNING jumps to the current run (or current generation if no
  // entry id is surfaced); IDLE jumps to the current epoch (or workspace).
  let ctaLabel = null;
  let ctaHref = null;
  if (stateName === 'running' && hb.epoch_id && hb.generation_id) {
    const first = (state.activeRuns || [])[0];
    if (first && first.entry_id) {
      ctaLabel = '→ Open current run';
      ctaHref = phase0Href('run', {
        epochId: hb.epoch_id,
        generationId: hb.generation_id,
        entryId: first.entry_id,
      });
    } else {
      ctaLabel = '→ Open current generation';
      ctaHref = phase0Href('generation', {
        epochId: hb.epoch_id, generationId: hb.generation_id,
      });
    }
  } else if (stateName === 'idle' && hb.epoch_id) {
    ctaLabel = '→ Open current epoch';
    ctaHref = phase0Href('epoch', { epochId: hb.epoch_id });
  }
  if (ctaLabel && ctaHref) {
    root.appendChild(el('a', {
      class: 'phase0-status-dropdown-cta',
      href: ctaHref,
    }, [ctaLabel]));
  }

  return root;
}

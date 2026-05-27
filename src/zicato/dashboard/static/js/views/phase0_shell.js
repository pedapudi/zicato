// views/phase0_shell.js — chrome for the phase-0 level-aligned shell.
//
// This module owns the cross-level pieces of the new shell: the
// breadcrumb (the primary navigation), the sidebar's Live Activity card
// (heartbeat-subscribed, digest-gated, mirrors the top-bar pattern from
// render.js's _relevantStateDigest.hbDigest), and the view-container
// visibility switch. It is intentionally tiny so per-level rendering
// can be added incrementally without touching this file.

import { $, el, clearChildren, patchText } from '../core/dom.js';
import { state } from '../core/state.js';
import { phase0Href, PHASE0_LEVELS } from './phase0_router.js';

// Map a parsed phase-0 route into the breadcrumb segments. Each segment
// is either a clickable link (target route + label) or a placeholder
// (label only, rendered greyed out). The L0 segment is always present;
// L1..L4 fill in only when the route carries the required params.
//
// Exported so the JS test harness can assert breadcrumb composition
// without driving the full route → DOM apply.
export function breadcrumbSegments(route) {
  const segs = [];
  segs.push({ label: 'workspace', href: phase0Href('workspace') });
  const p = (route && route.params) || {};
  if (p.epochId) {
    segs.push({
      label: p.epochId,
      href: phase0Href('epoch', { epochId: p.epochId }),
    });
  } else if (route && route.level !== 'workspace') {
    segs.push({ label: 'epoch', placeholder: true });
  }
  if (p.epochId && p.generationId) {
    segs.push({
      label: 'gen ' + p.generationId,
      href: phase0Href('generation', {
        epochId: p.epochId, generationId: p.generationId,
      }),
    });
  } else if (route && (route.level === 'generation' || route.level === 'run' || route.level === 'round')) {
    segs.push({ label: 'gen', placeholder: true });
  }
  if (route && route.level === 'round' && p.championId && p.challengerId) {
    segs.push({
      label: 'round ' + p.championId + '→' + p.challengerId,
      href: phase0Href('round', {
        epochId: p.epochId, championId: p.championId, challengerId: p.challengerId,
      }),
    });
  }
  if (route && route.level === 'run' && p.entryId) {
    segs.push({
      label: 'run ' + p.entryId,
      href: phase0Href('run', {
        epochId: p.epochId, generationId: p.generationId, entryId: p.entryId,
      }),
    });
  }
  return segs;
}

// Render the breadcrumb into ``#phase0-breadcrumb``. Idempotent: the
// container is rebuilt from segments each call. The container itself
// is preserved (so its outer attributes survive across renders).
export function renderBreadcrumb(route) {
  const root = $('phase0-breadcrumb');
  if (!root) return;
  const segs = breadcrumbSegments(route);
  clearChildren(root);
  segs.forEach((seg, i) => {
    if (i > 0) {
      root.appendChild(el('span', {
        class: 'phase0-crumb-sep',
        'aria-hidden': 'true',
      }, ['›']));
    }
    if (seg.placeholder) {
      root.appendChild(el('span', {
        class: 'phase0-crumb phase0-crumb-placeholder',
        'data-crumb': seg.label,
      }, [seg.label]));
    } else {
      root.appendChild(el('a', {
        class: 'phase0-crumb',
        href: seg.href,
        'data-crumb': seg.label,
      }, [seg.label]));
    }
  });
}

// Show only the phase0 view container that matches ``level``; hide all
// others. The container ids follow ``phase0-view-<level>`` exactly so
// callers do not have to map names.
export function showPhase0View(level) {
  for (const v of PHASE0_LEVELS) {
    const node = $('phase0-view-' + v);
    if (node) node.classList.toggle('hidden', v !== level);
  }
}

// Compute a stable digest of the heartbeat fields that drive the
// sidebar's Live Activity card. Mirrors the top-bar header subscription
// pattern from render.js's _relevantStateDigest.hbDigest — a heartbeat
// tick that only re-stamps a timestamp must NOT churn the live card.
// Exported so a test can pin which fields are part of the contract.
export function liveActivityDigest() {
  const hb = state.heartbeat || {};
  return JSON.stringify({
    epoch_id: hb.epoch_id || null,
    generation_id: hb.generation_id || null,
    round_index: hb.round_index != null ? hb.round_index : null,
    started_at: hb.started_at || null,
    parallelism: typeof hb.parallelism === 'number' ? hb.parallelism : null,
    active_run_count: Array.isArray(state.activeRuns) ? state.activeRuns.length : 0,
    has_active_tournament: !!state.activeTournament,
  });
}

let _lastLiveDigest = null;

// Render the sidebar's Live Activity card. Digest-gated: a no-op tick
// (the heartbeat's churning timestamp fields) writes ZERO DOM nodes,
// so text selection, scroll position and focus survive each tick.
export function renderSidebarLive() {
  const body = $('phase0-live-body');
  if (!body) return;
  const digest = liveActivityDigest();
  if (digest === _lastLiveDigest) return;
  _lastLiveDigest = digest;

  const hb = state.heartbeat || {};
  clearChildren(body);

  if (!hb.epoch_id && !hb.generation_id) {
    body.appendChild(el('p', { class: 'empty' }, ['No active run.']));
    return;
  }

  const lines = [];
  if (hb.epoch_id) {
    lines.push(el('div', { class: 'phase0-live-line mono' },
      ['epoch · ', hb.epoch_id]));
  }
  if (hb.generation_id) {
    lines.push(el('div', { class: 'phase0-live-line mono' },
      ['gen · ', hb.generation_id]));
  }
  if (hb.round_index != null) {
    lines.push(el('div', { class: 'phase0-live-line mono' },
      ['round · ', String(hb.round_index)]));
  }
  const activeCount = Array.isArray(state.activeRuns) ? state.activeRuns.length : 0;
  lines.push(el('div', { class: 'phase0-live-line mono' },
    ['active · ', String(activeCount)]));

  // "Jump to current run" link — points at the L4 run view for the
  // first active run, or at the current generation when no run id is
  // available. The label is stable across heartbeats so an operator
  // can muscle-memory it.
  let jumpHref = phase0Href('workspace');
  if (hb.epoch_id && hb.generation_id) {
    jumpHref = phase0Href('generation', {
      epochId: hb.epoch_id, generationId: hb.generation_id,
    });
    const first = (state.activeRuns || [])[0];
    if (first && first.entry_id) {
      jumpHref = phase0Href('run', {
        epochId: hb.epoch_id,
        generationId: hb.generation_id,
        entryId: first.entry_id,
      });
    }
  }
  lines.push(el('a', {
    class: 'phase0-live-jump',
    href: jumpHref,
  }, ['jump to current run →']));

  for (const line of lines) body.appendChild(line);
}

// Reset the digest cache. Used by tests that share module state across
// renders so a fresh test does not see a stale cache hit.
export function resetSidebarDigest() { _lastLiveDigest = null; }

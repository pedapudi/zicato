// views/phase0_shell.js — chrome for the level-aligned shell.
//
// Clean-slate navigation rework: the sidebar is gone. Everything global
// lives in the top bar:
//
//   [zicato]  workspace › presn › v3 › entry_alpha   [⌘K]   ● RUNNING v8 ›   [Files]  [Harmonograf ↗]
//
// The top bar is rendered into ``#phase0-topbar`` (the single live slot
// in index.html) on every relevant heartbeat tick. ``renderTopBar`` is
// digest-gated so a heartbeat tick that only re-stamps a timestamp
// writes zero DOM nodes.
//
// Below the top bar sits the PERSISTENT LIVE RAIL (``#phase0-live-rail``)
// — a slim always-visible strip that surfaces the in-flight tournament
// (champion vs challenger + entries done / in-flight / queued) at EVERY
// level whenever a run is active, with a "jump to decision" link into
// the L3 round view. It is hidden when the supervisor is idle. The rail
// is digest-gated on the same reconcile pattern as the top bar so an
// SSE heartbeat that only re-stamps a timestamp does NOT flash the rail.
//
// Verdict iconography everywhere routes through ``verdictGlyph`` (the
// shared ✓/✗/◦ vocabulary) so a decision reads identically in the
// breadcrumb, the rail, and the deep views.
//
// Legacy contracts the rest of the app still relies on:
//   * ``renderHeader`` — still writes into the hidden compat strip
//     (epoch-id / generation-id / elapsed / health-badge) so existing
//     tests can keep reading those nodes.

import { $, el, svgEl, clearChildren, patchText, patchClass } from '../core/dom.js';
import { parseIso, fmtDuration, nowMs } from '../core/format.js';
import { state } from '../core/state.js';
import { phase0Href, PHASE0_LEVELS } from './phase0_router.js';
import { renderStatusPill } from '../components/status_pill.js';
import { harmonografBase } from '../core/harmonograf.js';
import { open as openPalette } from '../components/command_palette.js';
import { verdictGlyph } from '../components/verdict_glyph.js';
import {
  liveChampionId, liveChallengerId, liveRoundLabel, entryStatus,
} from './shared.js';

// How long since the last heartbeat before the run is considered
// stale. heartbeat.json is rewritten on a short cadence (well under a
// minute); 90s leaves generous slack for a slow tick or a paused
// scheduler without false-flagging a healthy live run.
const STALE_HEARTBEAT_MS = 90_000;

// Render the hidden compat header strip. The visible top bar lives in
// ``#phase0-topbar``; ``renderHeader`` only keeps the chrome nodes that
// the rest of the dashboard already patches into. Idempotent: every
// text write routes through patchText so an unchanged tick writes zero
// DOM nodes.
export function renderHeader() {
  const hb = state.heartbeat || {};
  const epochId = hb.epoch_id || state.epoch.id;
  const genId = hb.generation_id || state.epoch.generation;
  const roundIdx = (hb.round_index != null) ? hb.round_index : state.epoch.round;
  patchText($('epoch-id'),
    'epoch · ' + (epochId != null && epochId !== '' ? epochId : '—'));
  patchText($('generation-id'),
    'gen · ' + (genId != null && genId !== '' ? genId : '—'));
  patchText($('round-id'),
    'round · ' + (roundIdx != null && roundIdx !== '' ? roundIdx : '—'));

  const startedRaw = hb.started_at || state.epoch.startedAt
    || hb.round_started_at || hb.epoch_started_at;
  const startedMs = parseIso(startedRaw);
  if (isFinite(startedMs)) {
    patchText($('elapsed'), fmtDuration((nowMs() - startedMs) / 1000));
  } else {
    patchText($('elapsed'), '—');
  }

  const badge = $('health-badge');
  if (!badge) return;
  let badgeText = '';
  let badgeKind = '';
  if (state.connecting) {
    badgeKind = 'pending';
    badgeText = 'connecting';
  } else if (state.connected) {
    const hbMs = parseIso(hb.last_heartbeat != null ? hb.last_heartbeat : hb.timestamp);
    const stale = isFinite(hbMs) && (nowMs() - hbMs) > STALE_HEARTBEAT_MS;
    if (stale) {
      badgeKind = 'warn';
      badgeText = 'stale heartbeat';
    } else {
      badgeKind = 'ok';
      badgeText = 'healthy';
    }
  } else {
    badgeKind = 'error';
    badgeText = 'disconnected';
  }
  patchClass(badge, 'ok', badgeKind === 'ok');
  patchClass(badge, 'warn', badgeKind === 'warn');
  patchClass(badge, 'error', badgeKind === 'error');
  patchClass(badge, 'pending', badgeKind === 'pending');
  patchText(badge, badgeText);

  const mockBadge = $('mock-badge');
  if (mockBadge) patchClass(mockBadge, 'hidden', !state.mock);
}

// Render the footer bar — version / port / build sourced from
// /api/health (mirrored on ``state.service`` until /api/health lands).
// Idempotent: every text write routes through patchText so a no-op
// refresh writes zero DOM nodes.
export function renderFooter() {
  const h = state.health || {};
  const pick = (a, b) => {
    if (a != null && a !== '') return a;
    if (b != null && b !== '') return b;
    return '—';
  };
  patchText($('dashboard-version'),
    'dashboard · ' + pick(h.version, state.service.version));
  patchText($('dashboard-port'),
    'port · ' + pick(h.port, state.service.port));
  patchText($('dashboard-build'),
    'build · ' + pick(h.build, state.service.build));
}

// Resolve a generation's tournament verdict from the epoch contract.
// The same source the L2 view + status dropdown read; returns one of
// the verdictGlyph vocabulary keys ('promoted' | 'rejected' | 'open')
// or null when the gen has no recorded outcome yet. Exported so tests
// can pin the mapping independent of the breadcrumb DOM.
export function verdictForGeneration(generationId) {
  if (!generationId) return null;
  const def = state.epochDef;
  if (!def || !Array.isArray(def.experiments)) return null;
  for (const exp of def.experiments) {
    if (!exp || exp.generation_id !== generationId) continue;
    const out = (exp.outcome && typeof exp.outcome === 'object') ? exp.outcome : {};
    const raw = exp.verdict
      || (typeof exp.outcome === 'string' ? exp.outcome : null)
      || out.tournament_decision
      || '';
    const v = String(raw).toLowerCase();
    if (v.startsWith('prom') || v === 'accepted') return 'promoted';
    if (v.startsWith('rej')) return 'rejected';
    return 'open';
  }
  return null;
}

// Map a parsed phase-0 route into the breadcrumb segments. Each segment
// is either a clickable link (target route + label) or a placeholder
// (label only, rendered greyed out). The L0 segment is always present;
// L1..L4 fill in only when the route carries the required params. A
// segment may additionally carry a ``verdict`` key (one of the
// verdictGlyph vocabulary) when the focused gen / matchup has a recorded
// outcome — the renderer prepends the shared glyph so the same ✓/✗/◦
// vocabulary appears in the nav as in the deep views.
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
      verdict: verdictForGeneration(p.generationId),
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
      // The matchup's verdict is the challenger's recorded outcome.
      verdict: verdictForGeneration(p.challengerId),
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

// Render the breadcrumb into a fresh node. Used by the top-bar renderer.
// Returns the container element so the top-bar paint can append it.
export function renderBreadcrumbNode(route) {
  const segs = breadcrumbSegments(route);
  const root = el('nav', {
    class: 'phase0-breadcrumb',
    'aria-label': 'Hierarchy breadcrumb',
  });
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
      const kids = [];
      if (seg.verdict) {
        // Shared verdict glyph — glyph-only (no label) so the crumb stays
        // compact; the word is already implicit in the gen/round label.
        const g = verdictGlyph(seg.verdict, { withLabel: false });
        g.classList.add('phase0-crumb-verdict');
        kids.push(g);
      }
      kids.push(seg.label);
      root.appendChild(el('a', {
        class: 'phase0-crumb',
        href: seg.href,
        'data-crumb': seg.label,
        'data-verdict': seg.verdict || '',
      }, kids));
    }
  });
  return root;
}

// Render the breadcrumb. The breadcrumb is part of the top-bar paint;
// ``renderBreadcrumb`` is kept as a shim that re-runs the full top-bar
// paint so app.js can keep its existing render fan-out unchanged.
export function renderBreadcrumb(route) {
  renderTopBar(route);
}

// Top-bar digest — what does the top bar actually depend on?
//   * the route (for the breadcrumb)
//   * heartbeat status fields (epoch_id, generation_id, last_heartbeat,
//     harmonograf_url — for the status pill state + harmonograf link)
//   * connection + activeRuns / activeTournament (status state)
//
// A heartbeat tick that only re-stamps timestamps must NOT churn the
// pill. The dot animation is purely CSS so the steady-state hold is
// jitter-free.
export function topBarDigest(route) {
  const hb = state.heartbeat || {};
  const lastHbMs = parseIso(hb.last_heartbeat != null ? hb.last_heartbeat : hb.timestamp);
  const isStale = isFinite(lastHbMs) && (nowMs() - lastHbMs) > STALE_HEARTBEAT_MS;
  const activeCount = Array.isArray(state.activeRuns) ? state.activeRuns.length : 0;
  // The breadcrumb may carry verdict glyphs sourced from the epoch
  // contract; fold the focused gen / challenger verdict into the digest
  // so an outcome landing after the first paint repaints the crumb.
  const p = (route && route.params) || {};
  const genVerdict = verdictForGeneration(p.generationId) || '';
  const matchupVerdict = verdictForGeneration(p.challengerId) || '';
  return JSON.stringify({
    route: route && route.raw ? route.raw : (route && route.level || null),
    params: route && route.params ? route.params : null,
    heartbeat_loaded: state.heartbeat != null,
    epoch_id: hb.epoch_id || null,
    generation_id: hb.generation_id || null,
    round_index: hb.round_index != null ? hb.round_index : null,
    started_at: hb.started_at || null,
    harmonograf_url: hb.harmonograf_url || null,
    connecting: !!state.connecting,
    is_stale: isStale,
    active_count: activeCount,
    has_tournament: !!state.activeTournament,
    gen_verdict: genVerdict,
    matchup_verdict: matchupVerdict,
  });
}

let _lastTopBarDigest = null;

// Render the top bar into ``#phase0-topbar``. Digest-gated.
export function renderTopBar(route) {
  const host = $('phase0-topbar');
  if (!host) return;
  const digest = topBarDigest(route);
  if (digest === _lastTopBarDigest) return;
  _lastTopBarDigest = digest;

  clearChildren(host);

  // ---- left cluster: branding + breadcrumb ----
  const brand = el('a', {
    class: 'phase0-topbar-brand',
    href: phase0Href('workspace'),
    'aria-label': 'zicato — go to workspace',
  }, ['zicato']);

  const left = el('div', { class: 'phase0-topbar-left' }, [
    brand,
    renderBreadcrumbNode(route),
  ]);

  // ---- right cluster: ⌘K · status pill · Files · Harmonograf ----
  const palBtn = el('button', {
    type: 'button',
    class: 'phase0-topbar-palette-btn',
    'aria-label': 'Open command palette (Cmd+K)',
    onClick: (ev) => {
      if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
      openPalette();
    },
  }, [
    el('span', { class: 'phase0-topbar-palette-kbd' }, ['⌘K']),
  ]);

  const pill = renderStatusPill();

  const filesIcon = svgEl('svg', {
    class: 'phase0-topbar-icon',
    'aria-hidden': 'true',
    width: '16', height: '16', viewBox: '0 0 20 20',
  });
  filesIcon.appendChild(svgEl('use', {
    href: '/static/icons.svg#icon-folder',
  }));
  const filesLink = el('a', {
    class: 'phase0-topbar-iconlink',
    href: phase0Href('files'),
    'aria-label': 'Files',
    title: 'Files',
  }, [filesIcon]);

  const right = el('div', { class: 'phase0-topbar-right' }, [
    palBtn, pill, filesLink,
  ]);

  // Harmonograf — only when the heartbeat surfaces a URL.
  const hgBase = harmonografBase();
  if (hgBase) {
    right.appendChild(el('a', {
      class: 'phase0-topbar-iconlink phase0-topbar-iconlink-external',
      'data-link': 'harmonograf',
      href: hgBase,
      target: '_blank',
      rel: 'noopener',
      'aria-label': 'Open harmonograf',
      title: 'Harmonograf',
    }, ['↗']));
  }

  host.appendChild(left);
  host.appendChild(right);
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

// ---------------------------------------------------------------------------
// Persistent live rail.
//
// A slim strip below the top bar, visible at EVERY level while a run is
// active. It promotes the status-pill dropdown's running content to a
// first-class always-visible affordance: the live champion vs challenger
// matchup + entry progress (done / in-flight / queued) + a "jump to
// decision" link into the L3 round view. Hidden when idle / stale /
// connecting (anything that is not a live tournament).
//
// The rail reads the same three sources the status dropdown does —
// /api/active-tournament (state.activeTournament), /api/active-runs
// (state.activeRuns), /api/heartbeat (state.heartbeat) — through the
// shared.js field accessors so a producer's field-name drift is
// normalised in exactly one place.
// ---------------------------------------------------------------------------

// Bucket the tournament's flat per-side entries into done / running /
// queued / failed counts. The contract is a flat list where each board
// entry appears once per side; we count distinct entry rows so the
// numbers read as "work units", matching the L3 hall grid.
function _liveEntryTally() {
  const t = state.activeTournament;
  const entries = (t && Array.isArray(t.entries)) ? t.entries : [];
  const tally = { done: 0, running: 0, queued: 0, failed: 0, total: entries.length };
  for (const e of entries) {
    const s = entryStatus(e);
    if (s === 'done') tally.done += 1;
    else if (s === 'running') tally.running += 1;
    else if (s === 'failed') tally.failed += 1;
    else tally.queued += 1;
  }
  return tally;
}

// Is there a live tournament to surface? The rail mirrors the status
// pill's RUNNING gate: a non-empty active-tournament record (after the
// heartbeat is fresh enough not to be stale). When the rail is hidden
// the strip carries the ``hidden`` class so it occupies zero layout.
export function liveRailActive() {
  if (state.connecting) return false;
  const hb = state.heartbeat || {};
  const lastHbMs = parseIso(hb.last_heartbeat != null ? hb.last_heartbeat : hb.timestamp);
  if (isFinite(lastHbMs) && (nowMs() - lastHbMs) > STALE_HEARTBEAT_MS) return false;
  return !!state.activeTournament;
}

// Digest of everything the rail paints — the matchup ids, the round
// label, and the entry tally. Excludes raw heartbeat timestamps so an
// SSE tick that only re-stamps a clock writes zero DOM (no flash).
export function liveRailDigest() {
  if (!liveRailActive()) return JSON.stringify({ active: false });
  const t = state.activeTournament;
  const tally = _liveEntryTally();
  return JSON.stringify({
    active: true,
    champion: liveChampionId(t),
    challenger: liveChallengerId(t),
    round: liveRoundLabel(t),
    total_rounds: (t && (t.total_rounds != null)) ? t.total_rounds : null,
    epoch_id: (state.heartbeat && state.heartbeat.epoch_id) || null,
    done: tally.done, running: tally.running,
    queued: tally.queued, failed: tally.failed, total: tally.total,
  });
}

let _lastLiveRailDigest = null;

// Render the persistent live rail into ``#phase0-live-rail``.
// Digest-gated; hidden (class + aria-hidden) when no live tournament.
export function renderLiveRail() {
  const host = $('phase0-live-rail');
  if (!host) return;
  const digest = liveRailDigest();
  if (digest === _lastLiveRailDigest) return;
  _lastLiveRailDigest = digest;

  clearChildren(host);

  if (!liveRailActive()) {
    host.classList.add('hidden');
    host.setAttribute('aria-hidden', 'true');
    return;
  }
  host.classList.remove('hidden');
  host.removeAttribute('aria-hidden');

  const t = state.activeTournament;
  const championId = liveChampionId(t) || '—';
  const challengerId = liveChallengerId(t) || '—';
  const roundLabel = liveRoundLabel(t);
  const totalRounds = (t && t.total_rounds != null) ? t.total_rounds : null;
  const tally = _liveEntryTally();
  const epochId = (state.heartbeat && state.heartbeat.epoch_id) || null;

  // -- live marker + matchup ------------------------------------------
  const marker = el('span', {
    class: 'phase0-live-rail-pulse',
    'aria-hidden': 'true',
  });
  const eyebrow = el('span', { class: 'phase0-live-rail-eyebrow' }, ['LIVE']);

  const matchup = el('span', { class: 'phase0-live-rail-matchup' }, [
    el('span', {
      class: 'phase0-live-rail-side phase0-live-rail-champion',
      'data-side': 'champion',
    }, [championId]),
    el('span', { class: 'phase0-live-rail-vs', 'aria-hidden': 'true' }, ['vs']),
    el('span', {
      class: 'phase0-live-rail-side phase0-live-rail-challenger',
      'data-side': 'challenger',
    }, [challengerId]),
  ]);

  const left = el('div', { class: 'phase0-live-rail-left' }, [marker, eyebrow, matchup]);

  if (roundLabel != null) {
    left.appendChild(el('span', { class: 'phase0-live-rail-round mono' }, [
      'round ' + roundLabel + (totalRounds != null ? '/' + totalRounds : ''),
    ]));
  }

  // -- entry progress: done / in-flight / queued ----------------------
  const progress = el('div', {
    class: 'phase0-live-rail-progress',
    'data-tally': tally.done + '/' + tally.running + '/' + tally.queued,
  }, [
    el('span', { class: 'phase0-live-rail-stat phase0-live-rail-done' }, [
      el('span', { class: 'phase0-live-rail-stat-n mono' }, [String(tally.done)]),
      el('span', { class: 'phase0-live-rail-stat-l' }, ['done']),
    ]),
    el('span', { class: 'phase0-live-rail-stat phase0-live-rail-running' }, [
      el('span', { class: 'phase0-live-rail-stat-n mono' }, [String(tally.running)]),
      el('span', { class: 'phase0-live-rail-stat-l' }, ['in-flight']),
    ]),
    el('span', { class: 'phase0-live-rail-stat phase0-live-rail-queued' }, [
      el('span', { class: 'phase0-live-rail-stat-n mono' }, [String(tally.queued)]),
      el('span', { class: 'phase0-live-rail-stat-l' }, ['queued']),
    ]),
  ]);
  if (tally.failed > 0) {
    progress.appendChild(el('span', {
      class: 'phase0-live-rail-stat phase0-live-rail-failed',
    }, [
      el('span', { class: 'phase0-live-rail-stat-n mono' }, [String(tally.failed)]),
      el('span', { class: 'phase0-live-rail-stat-l' }, ['failed']),
    ]));
  }

  // -- jump-to-decision CTA into L3 -----------------------------------
  // Only buildable when we know the epoch + both matchup ids; otherwise
  // omit the link rather than emit a dead href.
  const right = el('div', { class: 'phase0-live-rail-right' }, [progress]);
  const realChamp = liveChampionId(t);
  const realChal = liveChallengerId(t);
  if (epochId && realChamp && realChal) {
    right.appendChild(el('a', {
      class: 'phase0-live-rail-cta',
      'data-link': 'jump-to-decision',
      href: phase0Href('round', {
        epochId, championId: realChamp, challengerId: realChal,
      }),
    }, ['jump to decision →']));
  }

  host.appendChild(left);
  host.appendChild(right);
}

// ---------------------------------------------------------------------------
// Render fan-out hook + digest resets.
//
// app.js's per-tick fan-out calls ``renderSidebarLive`` (a name held
// over from the dropped sidebar). Rather than carry a dead no-op, that
// hook now drives the persistent live rail — the rail is the modern
// home for the always-visible "what is running" affordance the old
// sidebar card used to provide. The name is kept so app.js's fan-out
// does not need a coordinated edit; ``renderLiveRail`` is the canonical
// name new code should call.
// ---------------------------------------------------------------------------

// Drive the persistent live rail. Held-over name from the dropped
// sidebar Live Activity card — app.js + tests still call it.
export function renderSidebarLive() {
  renderLiveRail();
}

// Digest of the rail's structural inputs. Held-over name; the modern
// ``liveRailDigest`` is identical in spirit (heartbeat-timestamp churn
// excluded so a steady tick writes zero DOM). Kept so phase0.test.mjs's
// null→loaded / generation-flip assertions keep resolving a digest.
export function liveActivityDigest() {
  const hb = state.heartbeat || {};
  const tally = _liveEntryTally();
  return JSON.stringify({
    heartbeat_loaded: state.heartbeat != null,
    epoch_id: hb.epoch_id || null,
    generation_id: hb.generation_id || null,
    round_index: hb.round_index != null ? hb.round_index : null,
    started_at: hb.started_at || null,
    parallelism: typeof hb.parallelism === 'number' ? hb.parallelism : null,
    active_run_count: Array.isArray(state.activeRuns) ? state.activeRuns.length : 0,
    has_active_tournament: !!state.activeTournament,
    live_done: tally.done, live_running: tally.running, live_queued: tally.queued,
  });
}

// Reset the top-bar + live-rail digest caches. Tests share module state
// across renders so a fresh case must not inherit a stale hit.
export function resetShellDigest() {
  _lastTopBarDigest = null;
  _lastLiveRailDigest = null;
}

// Back-compat alias — several test files + app.js still call this name.
export function resetSidebarDigest() {
  resetShellDigest();
}

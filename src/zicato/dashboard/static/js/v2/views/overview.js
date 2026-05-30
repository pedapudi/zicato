// js/v2/views/overview.js — the Overview view (DASHBOARD-V2 §4.2).
//
// The Overview answers ONE question: *is the loop progressing & healthy?*
// It is the Notebook's landing screen, and it is deliberately DENSE —
// not a stack of one-number hero cards (§8). Its anatomy, top to bottom:
//
//   1. The loss TRAJECTORY as the hero — the optimization curve across
//      the lineage (best scalar per epoch, lower = better). This is the
//      "are we climbing?" answer at a glance. Every node is a door:
//      click drills to that epoch's Notebook page (§2 principle 5).
//   2. A HEALTH strip — green/amber/red with the top finding, the "is
//      this loop meaningful" signal (the loop-health report).
//   3. Compact IDENTITY/context — workspace root, current epoch, the
//      epoch/generation/promoted counts — terse, not a giant card.
//   4. A LIVE affordance — when a tournament is in flight, a prominent
//      "● LIVE — go to the Bench →" link (we do NOT duplicate the Bench;
//      we point to it).
//
// Every async section renders its not-yet / running / empty / broken
// condition through `stateBlock` — never a bare "No data" (§2 principle
// 4). The three reads are independent: one failing degrades only its
// own section, never the whole view.
//
// Data: GET /api/workspace (epochs[], sparkline[], current_epoch_id),
//       GET /api/health-report (findings[], healthy, checked_at),
//       GET /api/active-tournament (phase, *_generation_id, round_*).
//
// Pure-ish view: `renderOverview(host, route)` owns the host's body. It
// kicks the three fetches, paints loading states immediately, and
// repaints each section as its read settles. A render generation token
// guards against a stale fetch landing after the view has been swapped
// away (no write into a detached host).

import { $, el, clearChildren } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { fmtScalar, fmtDelta, truncate } from '../../core/format.js';
import { fetchJson } from '../../core/api.js';
import { v2Router, v2Href } from '../router.js';
import { registerView } from '../shell.js';
import { trajectory } from '../components/trajectory.js';
import { stateBlock } from '../components/stateBlock.js';

// ---------------------------------------------------------------------------
// Trajectory nodes from the workspace ribbon.
//
// The hero curve is the cross-EPOCH optimization trajectory: one node per
// epoch, y = that epoch's best (lowest) scalar. Lower loss sits higher —
// the promoted lineage traces the descent. The current epoch is marked
// live so it pulses while a run is in flight, and clicking any node
// drills to that epoch's Notebook page.
//
// An epoch with no finite best scalar yet (mid-first-run, no generation
// scored) contributes a node with a null scalar — the trajectory
// primitive handles that honestly (solo fallback / non-finite point), so
// we never fabricate a number.
// ---------------------------------------------------------------------------
export function overviewNodes(workspace, activeTournament) {
  const ws = workspace || {};
  const epochs = Array.isArray(ws.epochs) ? ws.epochs : [];
  const currentId = ws.current_epoch_id != null ? String(ws.current_epoch_id) : null;
  const liveEpoch = activeTournament && activeTournament.phase === 'running'
    ? (activeTournament.epoch_id != null ? String(activeTournament.epoch_id) : currentId)
    : null;

  return epochs
    .filter((e) => e && e.epoch_id != null)
    .map((e) => {
      const id = String(e.epoch_id);
      const scalarRaw = e.best_scalar;
      const scalar = (typeof scalarRaw === 'number' && isFinite(scalarRaw)) ? scalarRaw : null;
      return {
        id,
        parentId: e.parent_epoch_id != null ? String(e.parent_epoch_id) : null,
        // An epoch that promoted at least one challenger has descended the
        // curve; otherwise it is still open. The trajectory draws the
        // promoted ∪ live set as the spine.
        verdict: (Number(e.promoted_count) || 0) > 0 ? 'promoted' : 'open',
        live: liveEpoch != null && id === liveEpoch,
        scalar,
        label: id,
      };
    });
}

// Net movement across the lineage: first→last finite best-scalar delta.
// Negative = the loop has descended (improved); positive = regressed.
// Returns null when fewer than two epochs carry a finite scalar.
export function trajectoryDelta(workspace) {
  const ws = workspace || {};
  const spark = Array.isArray(ws.sparkline) ? ws.sparkline : [];
  const finite = spark
    .map((s) => (s && typeof s.scalar === 'number' && isFinite(s.scalar)) ? s.scalar : null)
    .filter((v) => v != null);
  if (finite.length < 2) return null;
  return finite[finite.length - 1] - finite[0];
}

// ---------------------------------------------------------------------------
// Health — map the loop-health report onto the three signal colors.
//
//   critical  → red    (regress)
//   warning   → amber   (caution)
//   healthy   → green   (improve)
//
// The "top finding" is the most severe finding's summary; an empty
// findings list on a healthy report reads plainly as "no issues".
// ---------------------------------------------------------------------------
const _SEVERITY_RANK = { critical: 3, warning: 2, info: 1 };

export function healthSignal(report) {
  const r = report || {};
  const findings = Array.isArray(r.findings) ? r.findings : [];
  const ranked = findings
    .filter((f) => f && typeof f === 'object')
    .slice()
    .sort((a, b) => (_SEVERITY_RANK[String(b.severity)] || 0) - (_SEVERITY_RANK[String(a.severity)] || 0));
  const top = ranked[0] || null;
  const healthy = r.healthy !== false;

  let signal;
  if (top && String(top.severity) === 'critical') signal = 'regress';
  else if (top && String(top.severity) === 'warning') signal = 'caution';
  else if (!healthy) signal = 'caution';
  else signal = 'improve';

  return {
    signal,                       // 'improve' | 'caution' | 'regress'
    healthy,
    finding: top ? String(top.summary || top.code || 'unspecified finding') : null,
    code: top ? (top.code != null ? String(top.code) : null) : null,
    count: findings.length,
  };
}

const _SIGNAL_LABEL = { improve: 'Healthy', caution: 'Caution', regress: 'Unhealthy' };
const _SIGNAL_GLYPH = { improve: '✓', caution: '!', regress: '✗' };

// ---------------------------------------------------------------------------
// Render. One host, four sections, three independent reads.
// ---------------------------------------------------------------------------

// A monotonically-increasing token; each render bumps it so an in-flight
// fetch that resolves after the view was swapped away is a no-op (it
// would otherwise write into a host that no longer belongs to it).
let _gen = 0;
let _lastOvDigest = null;

export function renderOverview(host, route) {
  if (!host) return;

  // Digest-gate the whole rebuild: the shell calls this on every SSE
  // heartbeat, but a tick that changes nothing the overview draws must
  // write zero DOM (otherwise the clearChildren + refetch below flashes
  // the page every second). Keyed on the structural facts the view
  // renders — current epoch, epoch count, and the live-tournament phase.
  const at = state.activeTournament;
  const digest = [
    route && route.view,
    state.epoch && state.epoch.id,
    Array.isArray(state.epochs) ? state.epochs.length : 0,
    at ? (at.phase + ':' + (at.child_generation_id || '') + ':' + (at.round_index || 0)) : 'idle',
  ].join('|');
  if (digest === _lastOvDigest && host.firstChild) return;
  _lastOvDigest = digest;

  const myGen = (_gen += 1);

  clearChildren(host);

  const wrap = el('div', { class: 'v2-overview' });
  host.appendChild(wrap);

  // 1 — the hero: the loss trajectory. Loading until /api/workspace lands.
  const heroSec = el('section', {
    class: 'v2-ov-hero', 'aria-label': 'Loss trajectory',
  }, [
    el('div', { class: 'v2-ov-hero-head' }, [
      el('h1', { class: 'v2-ov-title' }, ['Are we climbing?']),
      el('span', { class: 'v2-ov-hero-sub' }, ['loss trajectory · lower is better']),
      el('span', { class: 'v2-ov-delta', id: 'v2-ov-delta' }),
    ]),
    el('div', { class: 'v2-ov-hero-body', id: 'v2-ov-hero-body' }, [
      stateBlock('running', { label: 'Loading trajectory' }),
    ]),
  ]);
  wrap.appendChild(heroSec);

  // The live affordance slot — populated only when a run is in flight; it
  // sits directly under the hero so a live loop is impossible to miss.
  wrap.appendChild(el('div', { id: 'v2-ov-live' }));

  // 2 — the health strip.
  const healthSec = el('section', { class: 'v2-ov-health-sec', 'aria-label': 'Loop health' }, [
    el('div', { class: 'v2-ov-section-label' }, ['Loop health']),
    el('div', { id: 'v2-ov-health' }, [stateBlock('running', { label: 'Checking health' })]),
  ]);
  wrap.appendChild(healthSec);

  // 3 — compact identity / context.
  const idSec = el('section', { class: 'v2-ov-identity', 'aria-label': 'Workspace context' }, [
    el('div', { class: 'v2-ov-section-label' }, ['Workspace']),
    el('div', { id: 'v2-ov-context' }, [stateBlock('running', { label: 'Loading context' })]),
  ]);
  wrap.appendChild(idSec);

  const stale = () => myGen !== _gen || $('v2-ov-hero-body') == null;

  // --- read 1: the workspace ribbon → trajectory + context ----------------
  fetchJson('/api/workspace').then((ws) => {
    if (stale()) return;
    paintHero(ws, state.activeTournament);
    paintContext(ws);
  }).catch((err) => {
    if (stale()) return;
    const body = $('v2-ov-hero-body');
    if (body) {
      clearChildren(body);
      body.appendChild(stateBlock('broken', { reason: String(err && err.message ? err.message : err) }));
    }
    const ctx = $('v2-ov-context');
    if (ctx) {
      clearChildren(ctx);
      ctx.appendChild(stateBlock('broken', { reason: 'workspace read failed' }));
    }
  });

  // --- read 2: the health report → the strip -------------------------------
  fetchJson('/api/health-report').then((report) => {
    if (stale()) return;
    paintHealth(report);
  }).catch((err) => {
    if (stale()) return;
    const h = $('v2-ov-health');
    if (h) {
      clearChildren(h);
      h.appendChild(stateBlock('broken', { reason: String(err && err.message ? err.message : err) }));
    }
  });

  // --- read 3: the active tournament → the live affordance -----------------
  // Tolerant: a 404 / absent file simply means "no live run" — an empty
  // affordance slot, not a broken state.
  fetchJson('/api/active-tournament').then((t) => {
    if (stale()) return;
    state.activeTournament = t || null;
    paintLive(t);
    // The live state also influences which trajectory node pulses; repaint
    // the hero if the workspace already landed.
    if ($('v2-ov-hero-body') && $('v2-ov-hero-body').getAttribute('data-painted') === 'true') {
      fetchJson('/api/workspace').then((ws) => { if (!stale()) paintHero(ws, t); }).catch(() => {});
    }
  }).catch(() => {
    if (stale()) return;
    paintLive(null);
  });
}

// --- section painters --------------------------------------------------------

function paintHero(workspace, activeTournament) {
  const body = $('v2-ov-hero-body');
  if (!body) return;
  const nodes = overviewNodes(workspace, activeTournament);
  clearChildren(body);
  if (nodes.length === 0) {
    body.appendChild(stateBlock('not_yet', {
      label: 'No lineage yet',
      detail: 'The first epoch will anchor the trajectory once a generation is scored.',
    }));
  } else {
    body.appendChild(trajectory({
      nodes,
      zoom: 'epochs',
      onSelect: (id) => v2Router.go('epoch', id),
    }));
  }
  body.setAttribute('data-painted', 'true');

  // Net movement badge — the one-glance "are we descending?" number.
  const deltaEl = $('v2-ov-delta');
  if (deltaEl) {
    clearChildren(deltaEl);
    const d = trajectoryDelta(workspace);
    if (d == null) {
      deltaEl.removeAttribute('data-signal');
    } else {
      // Loss DOWN (negative delta) is an improvement.
      const sig = d < 0 ? 'improve' : (d > 0 ? 'regress' : 'neutral');
      deltaEl.setAttribute('data-signal', sig);
      deltaEl.appendChild(el('span', { class: 'v2-num' }, [`net ${fmtDelta(d)}`]));
      deltaEl.appendChild(el('span', { class: 'v2-ov-delta-word' }, [
        d < 0 ? 'descending' : (d > 0 ? 'regressing' : 'flat'),
      ]));
    }
  }
}

function paintContext(workspace) {
  const host = $('v2-ov-context');
  if (!host) return;
  const ws = workspace || {};
  const epochs = Array.isArray(ws.epochs) ? ws.epochs : [];
  const current = ws.current_epoch_id != null ? String(ws.current_epoch_id) : null;
  const gens = epochs.reduce((n, e) => n + (Number(e && e.generation_count) || 0), 0);
  const promoted = epochs.reduce((n, e) => n + (Number(e && e.promoted_count) || 0), 0);
  const root = (typeof state.workspace === 'string' && state.workspace) ? state.workspace : null;

  clearChildren(host);
  if (epochs.length === 0 && current == null) {
    host.appendChild(stateBlock('empty', {
      label: 'No workspace activity',
      detail: 'No epochs on disk yet.',
    }));
    return;
  }

  const facts = el('dl', { class: 'v2-ov-facts' });
  const fact = (term, value, opts) => {
    const o = opts || {};
    facts.appendChild(el('dt', { class: 'v2-ov-fact-term' }, [term]));
    const dd = el('dd', { class: `v2-ov-fact-val${o.mono ? ' v2-num' : ''}` });
    if (o.href) dd.appendChild(el('a', { class: 'v2-ov-fact-link', href: o.href }, [String(value)]));
    else dd.appendChild(document.createTextNode(String(value)));
    facts.appendChild(dd);
  };

  if (root) fact('root', root, { mono: true });
  fact('current epoch', current || '—', current ? { mono: true, href: v2Href('epoch', current) } : { mono: true });
  fact('epochs', String(epochs.length), { mono: true });
  fact('generations', String(gens), { mono: true });
  fact('promoted', String(promoted), { mono: true });

  host.appendChild(facts);
}

function paintHealth(report) {
  const host = $('v2-ov-health');
  if (!host) return;
  const h = healthSignal(report);
  clearChildren(host);

  const epochId = report && report.epoch_id != null ? String(report.epoch_id) : null;
  const banner = el('div', {
    class: 'v2-ov-health',
    'data-signal': h.signal,
    role: 'status',
  });
  banner.appendChild(el('span', { class: 'v2-ov-health-glyph', 'aria-hidden': 'true' }, [_SIGNAL_GLYPH[h.signal]]));
  banner.appendChild(el('span', { class: 'v2-ov-health-verdict' }, [_SIGNAL_LABEL[h.signal]]));

  const msg = h.finding
    ? truncate(h.finding, 140)
    : 'No issues detected — the loop is producing a meaningful signal.';
  banner.appendChild(el('span', { class: 'v2-ov-health-finding' }, [msg]));

  if (h.count > 1) {
    banner.appendChild(el('span', { class: 'v2-ov-health-more v2-num' }, [`+${h.count - 1} more`]));
  }
  // The finding lives in an epoch — make it a door to that epoch's page.
  if (epochId) {
    banner.appendChild(el('a', {
      class: 'v2-ov-health-link', href: v2Href('epoch', epochId),
      'aria-label': `Open epoch ${epochId}`,
    }, ['open →']));
  }
  host.appendChild(banner);
}

function paintLive(tournament) {
  const host = $('v2-ov-live');
  if (!host) return;
  clearChildren(host);
  const live = tournament && tournament.phase === 'running';
  if (!live) return;

  const champ = tournament.parent_generation_id != null ? String(tournament.parent_generation_id) : null;
  const chal = tournament.child_generation_id != null ? String(tournament.child_generation_id) : null;
  const round = Number(tournament.round_index);
  const total = Number(tournament.total_rounds);
  const roundStr = (isFinite(round) && isFinite(total) && total > 0)
    ? `round ${round + 1}/${total}` : null;

  const matchup = (champ && chal) ? `${champ} → ${chal}` : null;

  const cta = el('a', {
    class: 'v2-ov-live-cta', href: v2Href('bench'),
    'aria-label': 'A run is live — go to the Bench',
  }, [
    el('span', { class: 'v2-ov-live-dot', 'aria-hidden': 'true' }),
    el('span', { class: 'v2-ov-live-label' }, ['LIVE']),
    el('span', { class: 'v2-ov-live-detail v2-num' }, [
      [matchup, roundStr].filter(Boolean).join(' · ') || 'a tournament is in flight',
    ]),
    el('span', { class: 'v2-ov-live-go' }, ['go to the Bench →']),
  ]);
  host.appendChild(cta);
}

// Self-register so the shell's view router can mount this (§ shell.js
// registerView). The integrator wires `import './views/overview.js'`
// into app2.js so this module is evaluated at bootstrap.
registerView('overview', renderOverview);

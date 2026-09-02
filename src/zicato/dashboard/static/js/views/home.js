// js/views/home.js — HOME / ENVIRONMENT: the workspace as a fleet.
//
// Console's home is dense: a cross-epoch overview strip, the fleet of compact
// console cards (each carrying its per-epoch loss TRENDLINE hero), the composed
// meta-loop ledger as the cross-epoch overview, and loop health.
// Data-ink-maximal, tight chrome.
//
// Data: /api/workspace, /api/health-report, + live AppState.

import { el } from '../core/dom.js';
import { state } from '../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, fmt, chip, truncate,
  loopVerdict, promotionRateLabel, costPerPromotionLabel, fmtDurationMs, noiseBandFor, loopStatsDigest } from '../ui.js';
import { attachHovercard } from '../hovercard.js';
import { livenessFor } from '../livestatus.js';

// The loop-communication helpers moved to ui.js (they were shared UPWARD by
// epoch.js — a reverse view→view dependency). Re-exported here so existing
// `home.loopVerdict`-style consumers (tests, shell.js's `* as home`) keep
// resolving without churn.
export { loopVerdict, promotionRateLabel, costPerPromotionLabel, fmtDurationMs, noiseBandFor, loopStatsDigest };

export async function render(host, ctx) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Acquiring fleet telemetry…' }));

  const [ws, health] = await Promise.all([D.workspace(), D.healthReport()]);
  const rows = (ws && Array.isArray(ws.epochs)) ? ws.epochs : [];
  const current = ws ? ws.current_epoch_id : null;
  // The cross-epoch COMPOSED META-LOOP LEDGER matrix (study opt 7): one row per
  // epoch carrying floor / champion / effort / structure / the per-component
  // change set (incl. the proposer column the contract-diff omits). Surfaced as
  // a sibling of `epochs` on the SAME /api/workspace read, so no extra fan-out.
  const ledger = (ws && Array.isArray(ws.ledger)) ? ws.ledger : [];
  // Liveness is the served tri-state, never raw presence of
  // active_tournament.json — a torn-down run leaves that file on disk, and
  // reading it as "LIVE / tournament running" forever is the stale-live bug
  // class (issue #194 §1).
  const live = livenessFor(state).liveness.live;

  // Each fleet card's hero trendline is that epoch's OWN real per-generation
  // best-scalar trajectory — fetched PER epoch (keyed on epoch_id), never the
  // single currently-loaded contract and never a fabricated curve. The backend
  // scopes /api/score-trajectory by `?epoch=<id>`; missing/short series degrade
  // to an honest "no trajectory yet" placeholder downstream.
  //
  // The LOOP-COMMUNICATION reads ride alongside: the per-epoch optimization
  // trajectory (promotion rate + the uncertainty-honest verdict + the measured
  // noise floor) and the tournament cost (cost/promotion). Both null-degrade
  // (absent endpoint on the Rust supervisor) → the stats are simply omitted.
  const [trajs, loops, costs] = await Promise.all([
    Promise.all(rows.map((r) => D.scoreTrajectory(r.epoch_id))),
    Promise.all(rows.map((r) => D.trajectory(r.epoch_id))),
    Promise.all(rows.map((r) => D.tournamentCost(r.epoch_id))),
  ]);
  const trajByEpoch = new Map();
  const loopByEpoch = new Map();
  const costByEpoch = new Map();
  rows.forEach((r, i) => {
    trajByEpoch.set(r.epoch_id, epochTrajectoryValues(trajs[i]));
    loopByEpoch.set(r.epoch_id, loops[i] || null);
    costByEpoch.set(r.epoch_id, costs[i] || null);
  });

  // The CURRENT epoch's proposer CALIBRATION TREND (DIAGNOSTIC) — the
  // prediction-accuracy fraction over its lineage, surfaced beside the meta-loop
  // ledger. Scoped to the current epoch; absent / no scored predictions degrades
  // to an honest placeholder. NEVER feeds the gate. A null read (no current
  // epoch) drops the panel — byte-identical to the pre-feature home.
  const calib = current != null ? await D.calibrationTrend(current) : null;

  // What each card will PRINT for its objective (first line clipped, or a faint
  // back-reference when the goal repeats the card before it) — decided once,
  // read by both the digest and the build.
  const goals = goalModels(rows);

  const digest = JSON.stringify({
    live, cur: current,
    rows: rows.map((r, i) => [r.epoch_id, r.generation_count || 0, r.promoted_count || 0,
      svg.isNum(r.best_scalar) ? r.best_scalar.toFixed(3) : null, !!r.closed,
      // WHICH generation set that floor (events_index `best_generation_id`) —
      // rendered on the fleet card + as the overview tile's deep link, so it
      // has to gate the swap: the same best_scalar can change hands.
      r.best_generation_id == null ? null : String(r.best_generation_id),
      (trajByEpoch.get(r.epoch_id) || []).map((v) => v.toFixed(3)),
      // the rendered objective (first-line clip / "same goal as …") — the goal
      // was absent from the fold entirely, so an edited objective never
      // repainted the card that shows it.
      goalModelDigest(goals[i])]),
    // the loop-communication stats are content-gated on their own rounded fold
    // so a no-op heartbeat (identical rates/verdicts/costs) churns no DOM.
    loop: rows.map((r) => loopStatsDigest(loopByEpoch.get(r.epoch_id), costByEpoch.get(r.epoch_id))),
    // The ledger is content-gated by its own builder digest so a no-op
    // heartbeat (identical matrix) churns no DOM — the cross-epoch overview
    // is the heaviest figure on this page.
    ledger: svg.metaLoopLedgerDigest({ epochs: ledger, currentEpochId: current }),
    // the proposer calibration trend (DIAGNOSTIC) is content-gated on its own
    // builder digest so a no-op heartbeat (identical trend) churns no DOM — a
    // new scored prediction flips it, a steady tick stays byte-identical.
    calib: calib ? svg.calibrationTrendDigest(calib) : null,
    health: health ? (Array.isArray(health.findings) ? health.findings.length : 0) : -1,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: 'Environment' }),
      el('p', { class: 'dn-lede', text: 'The workspace as a fleet — every epoch at a glance. Lower scalar (loss) is better.' }),
    ]));

    nodes.push(overviewStrip(rows, live, ctx));

    // The fleet of per-epoch console cards is the lead view — the workspace at
    // a glance, each card the per-epoch drill-in.
    const fleet = rows.length === 0
      ? empty('No epochs recorded in this workspace yet.')
      : el('div', { class: 'dn-fleet' }, rows.map((r, i) => fleetCard(r, r.epoch_id === current, ctx, trajByEpoch.get(r.epoch_id) || [], live,
        loopByEpoch.get(r.epoch_id), costByEpoch.get(r.epoch_id), goals[i])));
    nodes.push(section('Fleet · ' + rows.length + ' epoch' + (rows.length === 1 ? '' : 's'), fleet));

    // Below the fleet: the composed meta-loop ledger (study opt 7) — the
    // cross-epoch overview that braids the held floor staircase, effort-
    // proportional bands, and the contract-component heatstrip (incl. the
    // proposer column the diff omits): trajectory, attribution, and
    // effort/champion in one scan.
    if (ledger.length >= 1) {
      const lcard = el('div', { class: 'dn-panel dn-figpane dn-metaledger-pane' });
      lcard.appendChild(svg.metaLoopLedger({
        epochs: ledger, currentEpochId: current, responsive: true,
        onEpoch: (id) => ctx.navigate && ctx.navigate('epoch', { epochId: id }),
      }));
      lcard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;',
        text: 'is the meta-loop making net progress across contracts · which lever moved each reset · is effort buying floor' }));
      nodes.push(section('Meta-loop ledger · cross-epoch', lcard));
    }

    // ── the proposer CALIBRATION TREND (DIAGNOSTIC) — a sibling of the ledger ──
    // The prediction-accuracy fraction over the CURRENT epoch's lineage: is the
    // proposer's calibration drifting? Reuses the sparkline/staircase grammar.
    // EXPLICITLY captioned diagnostic — it never feeds the gate. Rendered only
    // when there is a scored point to show (absent → byte-identical to today).
    if (calib && Array.isArray(calib.points) && calib.points.some((p) => p && svg.isNum(p.score_fraction))) {
      const ccard = el('div', { class: 'dn-panel dn-figpane dn-caltrend-pane' });
      ccard.appendChild(svg.calibrationTrend({
        points: calib.points, rolling_mean: calib.rolling_mean, trend_sign: calib.trend_sign,
        // the SERVED readouts (build_calibration_trend): the latest scored
        // fraction and how many generations carried claims. Read, never
        // re-derived from `points` client-side.
        latest_fraction: calib.latest_fraction, n_scored: calib.n_scored,
        onGen: (gid) => ctx.navigate && current != null && ctx.navigate('candidate', { epochId: current, gen: gid }),
      }));
      const tsign = svg.isNum(calib.trend_sign) ? calib.trend_sign : 0;
      const trendWord = tsign > 0 ? 'improving' : tsign < 0 ? 'regressing' : 'flat / too few';
      const rm = svg.fmtPercent(calib.rolling_mean);
      const lf = svg.fmtPercent(calib.latest_fraction);
      const ns = svg.isNum(calib.n_scored) ? calib.n_scored : null;
      ccard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;',
        text: 'diagnostic — does not affect the gate · proposer calibration ' + trendWord
          + ' · epoch mean ' + rm + ' of claims landed · latest ' + lf
          + (ns == null ? '' : ' · ' + ns + ' generation' + (ns === 1 ? '' : 's') + ' scored')
          + ' · higher = better-calibrated' }));
      nodes.push(section('Calibration trend · proposer prediction accuracy (diagnostic)', ccard));
    }

    if (health) nodes.push(healthPanel(health));

    return nodes;
  });
}

function overviewStrip(rows, live, ctx) {
  let gens = 0, promoted = 0, open = 0, best = null, bestRow = null;
  for (const r of rows) {
    gens += r.generation_count || 0;
    promoted += r.promoted_count || 0;
    if (!r.closed) open += 1;
    if (svg.isNum(r.best_scalar) && (best == null || r.best_scalar < best)) { best = r.best_scalar; bestRow = r; }
  }
  // WHICH generation holds the fleet floor. `/api/workspace` names it
  // (`best_generation_id` beside `best_scalar`); this tile is the one place the
  // pairing can be a REAL link — inside a fleet card the value already sits in
  // the card's own <a>, and an anchor cannot nest. So: the tile foot deep-links
  // to that candidate's dossier, and each card names its own holder in text.
  const bestFoot = (bestRow && bestRow.best_generation_id && ctx && ctx.href)
    ? el('a', {
        class: 'dn-tile-foot dn-tile-footlink dn-mono',
        href: ctx.href('candidate', { epochId: bestRow.epoch_id, gen: String(bestRow.best_generation_id) }),
        title: `${bestRow.best_generation_id} set the fleet floor in ${bestRow.epoch_id}`,
      }, [String(bestRow.best_generation_id) + ' →'])
    : null;
  return el('div', { class: 'dn-panel dn-row dn-overview' }, [
    statTile(String(rows.length), 'epochs', open + ' open'),
    statTile(String(gens), 'generations', promoted + ' promoted'),
    statTile(fmt(best), 'best scalar', bestFoot || 'lowest across fleet'),
    statTile(live ? 'LIVE' : 'IDLE', 'phase', live ? 'tournament running' : 'between rounds'),
  ]);
}

// `foot` is either a string or a ready-made node (the best-scalar deep link).
function statTile(value, key, foot) {
  const footNode = (foot && typeof foot === 'object' && foot.nodeType !== undefined)
    ? foot : (foot ? el('span', { class: 'dn-tile-foot', text: foot }) : null);
  return el('div', { class: 'dn-tile' }, [
    el('span', { class: 'dn-tile-value', text: value }),
    el('span', { class: 'dn-tile-key', text: key }),
    footNode,
  ].filter(Boolean));
}

// ---- the fleet's GOAL PROSE (one line per card, never a wall) --------
//
// An epoch inherits its predecessor's objective far more often than it rewrites
// it. Printing the goal in full on every card would repeat the same five-line
// paragraph card after card, pushing the numbers — the part that differs — below
// the fold. Two rules keep the strip readable:
//
//   * a card shows its goal's FIRST LINE, clipped to ~90 chars; the untouched
//     text rides the hovercard (the console's standard "detail on demand");
//   * a goal IDENTICAL to the previous card's collapses to a faint back-
//     reference — the card says which epoch it is repeating, and says it once.
//
// A pure model so the digest and the DOM read the same decision.
export const GOAL_CLIP = 90;

export function goalModels(rows) {
  let prevGoal = null, prevId = null;
  return (Array.isArray(rows) ? rows : []).map((r) => {
    const full = (r && typeof r.goal === 'string') ? r.goal.trim() : '';
    const id = r ? r.epoch_id : null;
    if (!full) { prevGoal = null; prevId = id; return { kind: 'none' }; }
    if (prevGoal != null && full === prevGoal) {
      const m = { kind: 'same', of: prevId == null ? null : String(prevId), full };
      prevId = id;
      return m;
    }
    const first = full.split('\n').map((l) => l.trim()).filter(Boolean)[0] || full;
    const lead = truncate(first, GOAL_CLIP);
    prevGoal = full; prevId = id;
    return { kind: 'text', lead, full, clipped: lead !== full };
  });
}

// The digest fold: what the card will actually PRINT, so a goal edit that
// changes nothing visible is a no-op beat and one that does repaints.
export function goalModelDigest(m) {
  if (!m) return null;
  if (m.kind === 'same') return ['same', m.of];
  if (m.kind === 'text') return ['text', m.lead, m.clipped];
  return ['none'];
}

function goalLine(m) {
  if (!m || m.kind === 'none') {
    return el('div', { class: 'dn-fleet-goal dn-faint', text: '(no goal recorded)' });
  }
  if (m.kind === 'same') {
    const node = el('div', { class: 'dn-fleet-goal dn-fleet-goal-same dn-faint',
      text: m.of ? 'same goal as ' + m.of : 'same goal as the previous epoch' });
    attachHovercard(node, m.full);
    return node;
  }
  const node = el('div', { class: 'dn-fleet-goal', text: m.lead });
  if (m.clipped) attachHovercard(node, m.full);
  return node;
}

function fleetCard(row, isCurrent, ctx, sparkVals, live, loop, cost, goalModel) {
  // "running" requires the GATED live flag (fresh heartbeat) — not just an
  // active_tournament.json whose epoch_id matches. A stale file must not paint
  // the current epoch's chip "running" after the orchestrator has exited.
  const liveHere = isCurrent && !!live && state.activeTournament && state.activeTournament.epoch_id === row.epoch_id;
  const st = isCurrent ? (liveHere ? 'live' : 'open') : (row.closed ? 'closed' : 'open');
  // The UNCERTAINTY-HONEST loop verdict chip: "plateaued" only when the recent
  // movement is resolvable above the measured noise floor; below the floor the
  // honest word is "no detectable signal", never a confident plateau.
  const verdict = loopVerdict(loop);
  const head = el('div', { class: 'dn-fleet-head' }, [
    el('span', { class: 'dn-fleet-id', text: row.epoch_id }),
    verdict ? chip(verdict.cls, verdict.word) : null,
    chip(liveHere ? 'live' : st, liveHere ? 'running' : st),
  ].filter(Boolean));
  const goal = goalLine(goalModel);
  // The measured A/A noise floor renders as a band around the champion floor
  // (the trajectory's last scalar): movement inside it is indistinguishable
  // from a re-roll. Absent floor → no band (byte-identical to today).
  const hero = el('div', { class: 'dn-fleet-spark' }, [
    sparkVals.length >= 2
      ? svg.sparkline({ width: 240, height: 46, values: sparkVals, band: true, goodDirection: 'down',
          noiseBand: noiseBandFor(loop, sparkVals) })
      : el('span', { class: 'dn-faint', text: heroPlaceholderText(loop) }),
  ]);
  const promo = promotionRateLabel(loop);
  const costLabel = costPerPromotionLabel(cost);
  // "best" names its HOLDER: the generation `best_generation_id` that set this
  // epoch's floor. It is plain text: the whole card is already an <a> to the
  // epoch, and nesting an anchor inside it is invalid; the fleet-wide deep link
  // lives on the overview tile above.
  const bestGen = row.best_generation_id == null ? null : String(row.best_generation_id);
  const stats = el('div', { class: 'dn-fleet-stats' }, [
    miniStat('best', fmt(row.best_scalar), 'good', bestGen),
    miniStat('gens', String(row.generation_count || 0)),
    miniStat('promoted', String(row.promoted_count || 0)),
    promo ? miniStat('promo rate', promo) : null,
    costLabel ? miniStat('cost/promo', costLabel) : null,
  ].filter(Boolean));
  return el('a', {
    class: 'dn-fleet-card' + (isCurrent ? ' dn-is-current' : ''),
    href: ctx.href('epoch', { epochId: row.epoch_id }),
  }, [head, goal, hero, stats]);
}

// (loop-communication helpers moved to ui.js — re-exported at the top of this
// file for back-compat.)

// What the fleet card's hero says when the epoch has fewer than two real
// scalar points to draw. "no trajectory yet" is true only before anything has
// run: once challengers have been fielded and rejected, the epoch HAS a
// history — the champion simply never moved — and saying "yet" reads as a
// loop that has not started rather than one that is not getting anywhere.
// Counts come from the trajectory payload, which already carries them.
// The round count is SETTLED challengers, matching the verdict chip beside it:
// a challenger that is still racing has retained nothing yet, and counting it
// would report a round the loop has not finished. `settled_count` is additive,
// so a payload from before it existed (or the Rust supervisor's) falls back to
// challenger_count and reads exactly as it did.
export function heroPlaceholderText(loop) {
  const l = loop && typeof loop === 'object' ? loop : null;
  const rounds = l && svg.isNum(l.settled_count) ? l.settled_count
    : (l && svg.isNum(l.challenger_count) ? l.challenger_count : 0);
  if (rounds < 1) return 'no trajectory yet';
  const promoted = l && svg.isNum(l.promoted_count) ? l.promoted_count : 0;
  const roundWord = rounds + ' round' + (rounds === 1 ? '' : 's');
  return promoted === 0
    ? 'champion retained · ' + roundWord
    : roundWord + ' · ' + promoted + ' promoted';
}

// `by` (optional) names WHO set the value — rendered as a muted suffix, e.g.
// "best 70.9 · v2" for the generation that holds the epoch's floor.
function miniStat(k, v, tone, by) {
  return el('div', { class: 'dn-mini' }, [
    el('span', { class: 'dn-mini-k', text: k }),
    el('span', { class: 'dn-mini-v' + (tone ? ' dn-good-t' : ''), text: v }),
    by ? el('span', { class: 'dn-mini-by dn-faint dn-mono', title: by + ' set this floor', text: '· ' + by }) : null,
  ].filter(Boolean));
}

// This epoch's REAL per-generation best-scalar trajectory, in generation
// order, from the per-epoch /api/score-trajectory payload. NEVER fabricates a
// curve: when the epoch has fewer than 2 real points, the card shows the
// honest "no trajectory yet" placeholder instead (see fleetCard).
function epochTrajectoryValues(traj) {
  const points = (traj && Array.isArray(traj.points)) ? traj.points : [];
  const vals = [];
  for (const p of points) {
    const s = p && p.scalar;
    if (svg.isNum(s)) vals.push(s);
  }
  return vals;
}

function healthPanel(hr) {
  const findings = Array.isArray(hr.findings) ? hr.findings : [];
  const healthy = hr.healthy !== false && findings.length === 0;
  const body = el('div');
  if (healthy) {
    body.appendChild(el('div', { class: 'dn-good-t', text: '✓ loop is healthy — the evaluation distinguishes candidates.' }));
  } else {
    for (const f of findings) {
      const sev = String((f && (f.severity || f.level)) || 'info').toLowerCase();
      body.appendChild(el('div', { class: 'dn-finding' }, [
        chip(sev === 'critical' ? 'closed' : 'open', sev),
        el('span', { class: 'dn-mono', style: 'margin-left:8px', text: f.detector || f.name || 'finding' }),
        el('div', { class: 'dn-faint', style: 'margin-top:4px', text: f.summary || f.message || '' }),
      ]));
    }
  }
  return section('Loop health' + (hr.epoch_id ? ' · ' + hr.epoch_id : ''), el('div', { class: 'dn-panel' }, [body]));
}

// js/views/structure.js — the configured tournament STRUCTURE, rendered.
//
// The per-structure sections (the elimination bracket, the swiss ladder, the
// racing rung ladder, the gauntlet field bars), the standings table and the
// field-diversity ribbon, drawn from the models in ../tournament_model.js as
// fit-to-width SVG (width:100% + viewBox, no pan/zoom, token-themed, page-scale
// aware). The match-ups page (views/gens.js) renders the default gauntlet with
// its own ladder.

import { el, svgEl } from '../core/dom.js';
import * as svg from '../svg.js';
import { section, empty, stat, verdictPill, overrideChip, overrideControlCell, pendingOverride, clearPendingOverride, chip, hovercardBody, dataTable, ratingCellEl, coreIdeaLine } from '../ui.js';
import { structureStatusLabel } from '../livestatus.js';
import { attachHovercard } from '../hovercard.js';
import { state } from '../core/state.js';
import { postFieldOverride } from '../core/api.js';
import * as D from '../data.js';
import { structureLabel, elimModel, splitBand, swissModel, racingModel, championScalarOf, gauntletModel, diversityMembership } from '../tournament_model.js';
const CROWN = svg.CROWN;

// The structure pill (shown in the epoch header + the match-ups header).
export function structurePill(structure, params) {
  return el('span', { class: 'dt-structure-pill', 'data-structure': String(structure || 'gauntlet') }, [
    el('span', { class: 'dt-structure-pill-k', text: 'structure' }),
    el('span', { class: 'dt-structure-pill-v', text: structureLabel(structure, params) }),
  ]);
}

// ── the structure render dispatch — DOM sections per structure ──────
export function renderStructure(st, ctx, epochId) {
  const structure = String((st && st.structure) || 'gauntlet');
  let nodes;
  if (structure === 'swiss') nodes = renderSwiss(st, ctx, epochId);
  else if (structure === 'racing') nodes = renderRacing(st, ctx, epochId);
  else if (structure === 'gauntlet') nodes = renderGauntlet(st, ctx, epochId);
  // single_elim + double_elim share the bracket renderer.
  else nodes = renderBracket(st, ctx, epochId, structure);
  // The PROPOSED FIELD section leads so a completed epoch's proposing
  // outcomes are visible (e.g. "4 proposed · 0 applied — all rejected" with
  // per-challenger reasons). Absent field_status ⇒ no section (back-compat).
  const proposed = proposedFieldSection(st, ctx, epochId);
  // The FIELD-DIVERSITY ribbon rides directly UNDER the proposed-field section:
  // the mean/max pairwise-Jaccard overlap of the minted field + the soft-reject
  // count, with the overlap matrix beneath it. Absent (single-challenger / pre-
  // feature / gauntlet) → renders nothing (byte-identical to today).
  const diversity = diversitySection(st, ctx, epochId);
  const lead = [proposed, diversity].filter(Boolean);
  return lead.length ? [...lead, ...nodes] : nodes;
}

// ── the FIELD-DIVERSITY ribbon — mean/max pairwise-Jaccard + overlap matrix ──
//
// Reads the additive `diversity` block VERBATIM (build_tournament_structure →
// _enrich_diversity / _compute_field_diversity): `{field_size, distinct_ideas,
// mean_overlap, max_overlap, max_overlap_pair, tolerance, soft_rejected_count}`.
// KEY-ABSENT on a gauntlet / single-challenger / pre-feature run → render NOTHING
// (byte-identical to today). Higher overlap is WORSE (a field of N collapses to
// fewer real experiments), so the meter earns its tone BY DIRECTION: at/above the
// tolerance reads caution, below it reads good.
function diversitySection(st, ctx, epochId) {
  const d = (st && st.diversity && typeof st.diversity === 'object') ? st.diversity : null;
  if (!d || !svg.isNum(d.field_size) || d.field_size < 2) return null;
  const tol = svg.isNum(d.tolerance) ? d.tolerance : null;
  const soft = svg.isNum(d.soft_rejected_count) ? d.soft_rejected_count : 0;
  const meanO = svg.isNum(d.mean_overlap) ? d.mean_overlap : 0;
  const maxO = svg.isNum(d.max_overlap) ? d.max_overlap : 0;
  const distinct = svg.isNum(d.distinct_ideas) ? d.distinct_ideas : null;
  const pair = Array.isArray(d.max_overlap_pair) ? d.max_overlap_pair.map(String) : null;

  // the headline stat strip — distinct ideas / field size + soft-rejects.
  const stats = el('div', { class: 'dn-divstats' }, [
    (distinct != null) ? stat(distinct + ' / ' + d.field_size, 'distinct ideas') : null,
    stat(svg.fmt(meanO, 2), 'mean overlap'),
    stat(svg.fmt(maxO, 2), 'max overlap'),
    soft > 0 ? stat(String(soft), 'soft-rejected') : null,
  ].filter(Boolean));

  // the dual mean/max overlap meter against the tolerance marker.
  const meter = overlapMeter(meanO, maxO, tol);
  if (pair && pair.length === 2) {
    attachHovercard(meter, () => hovercardBody([
      el('div', { class: 'dn-hc-title', text: 'most-overlapping pair' }),
      el('div', { class: 'dn-hc-row dn-mono', text: pair[0] + ' ⇄ ' + pair[1] }),
      el('div', { class: 'dn-hc-row dn-faint', text: 'Jaccard ' + svg.fmt(maxO, 2)
        + (tol != null ? ' · tolerance ' + svg.fmt(tol, 2) : ' · enforcement off') }),
    ]));
  }

  // a soft-reject chip reuses the DEFERRED pill vocabulary: held rather than
  // promoted.
  const softChip = soft > 0
    ? (() => { const p = verdictPill('deferred'); p.textContent = soft + ' soft-rejected'; return p; })()
    : null;

  // the overlap matrix — challenger × mutation-site (the dn-mtx grammar). The
  // per-challenger site membership is NOT on the diversity block, so the
  // dashboard derives it from any membership the payload carries; absent →
  // svg.diversityMatrix returns null → no matrix (byte-identical). FOLLOWUP:
  // wire per-challenger mutation_ids onto the structure payload (Python).
  const membership = diversityMembership(st);
  const matrix = svg.diversityMatrix({
    membership, highlightPair: pair,
    onCompetitor: (gen) => { if (gen && ctx && ctx.navigate) ctx.navigate('candidate', { epochId, gen }); },
  });

  const cap = el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
    'pairwise idea overlap (Jaccard) across the minted field — higher overlap means the field is converging on the same idea'
    + (tol != null ? ' · soft-rejected above ' + svg.fmt(tol, 2) : ' · overlap enforcement off (diagnostic only)') });

  const panel = el('div', { class: 'dn-panel dn-divribbon' }, [
    el('div', { class: 'dn-divribbon-head' }, [stats, softChip].filter(Boolean)),
    meter, cap,
    matrix,
  ].filter(Boolean));
  return section('Field diversity', panel);
}

// The dual overlap meter: a single track with the MEAN-overlap fill + a MAX-
// overlap notch, the tolerance drawn as the dashed promote-threshold marker. The
// fill earns its tone BY DIRECTION — at/above the tolerance is caution (the field
// is collapsing), below is good. No tolerance (enforcement off) → a neutral fill
// (the overlap is diagnostic and never gates). Returns a Node.
function overlapMeter(mean, max, tol) {
  const W = 260, H = 30, padX = 4, axW = W - 2 * padX;
  const fig = svgEl('svg', {
    class: 'dn-div-meter', width: '100%', height: H, viewBox: `0 0 ${W} ${H}`,
    preserveAspectRatio: 'none', role: 'img',
    'aria-label': 'mean and max pairwise idea overlap vs the diversity tolerance',
  });
  const top = 6, barH = 12;
  fig.appendChild(svgEl('rect', { x: padX, y: top, width: axW, height: barH, class: 'dn-div-track' }));
  const over = svg.isNum(tol) ? (mean >= tol) : null;
  const mw = Math.max(0, Math.min(1, mean)) * axW;
  fig.appendChild(svgEl('rect', { x: padX, y: top, width: mw, height: barH,
    class: 'dn-div-fill ' + (over === true ? 'dn-caution-fill' : over === false ? 'dn-good-fill' : 'dn-flat-fill') }));
  // the MAX-overlap notch.
  const mx = padX + Math.max(0, Math.min(1, max)) * axW;
  fig.appendChild(svgEl('line', { x1: mx, y1: top - 3, x2: mx, y2: top + barH + 3, class: 'dn-div-max' }));
  if (svg.isNum(tol)) {
    const tx = padX + Math.max(0, Math.min(1, tol)) * axW;
    fig.appendChild(svgEl('line', { x1: tx, y1: top - 4, x2: tx, y2: top + barH + 4, class: 'dn-div-tol' }));
    // The tolerance marker rides at tx, but a near-1.0 tolerance pushes its
    // middle-anchored label past the right viewBox edge (W is fixed, the bar is
    // preserveAspectRatio:'none') and it CLIPS — same mechanism as the BT gate's
    // ratingProbBar. svg.edgeText keeps the FULL label inside [padX, W-padX] by
    // clamping x near an edge; the common mid-bar case stays middle@tx.
    fig.appendChild(svg.edgeText({
      text: 'tol ' + svg.fmt(tol, 2), x: tx, y: top + barH + 14,
      anchor: 'middle', viewW: W, pad: padX, cls: 'dn-div-tollab',
    }));
  }
  return el('div', { class: 'dn-div-meterwrap' }, [
    el('div', { class: 'dn-div-meterhead' }, [
      el('span', { class: 'dn-div-meterlab', text: 'mean overlap' }),
      el('span', { class: 'dn-div-meterval dn-mono', text: svg.fmt(mean, 2)
        + ' · max ' + svg.fmt(max, 2) }),
    ]),
    fig,
  ]);
}

// The "Proposed field" section — the candidate-generation step rendered via the
// shared proposingTracker (applied rows drill in; rejected rows show the reason).
function proposedFieldSection(st, ctx, epochId) {
  const fs = D.fieldStatus(st);
  if (!fs.length) return null;
  const proposing = fs.filter((f) => f.status === 'proposing').length;
  // A slot still proposing means the field is forming RIGHT NOW — treat as
  // live even if the payload's own `live` flag has not flipped yet.
  const live = !!(st && st.live) || proposing > 0;
  const applied = fs.filter((f) => f.status === 'applied').length;
  const rejected = fs.filter((f) => f.status === 'rejected').length;
  // The proposing tracker earns its own section only when it has something to
  // SAY: LIVE (proposals applying/rejecting in real time — the count + per-row
  // states update as the field mints) or a COMPLETED run WITH REJECTIONS to
  // triage (which proposals failed to apply, and why). A completed, all-applied
  // field is already shown by the ladder/standings + the "field of N" pill, so a
  // lone "N proposed · N applied" line just reads as an empty section — omit it.
  if (!live && rejected === 0) return null;
  const onCompetitor = (gen) => { if (gen && ctx && ctx.navigate) ctx.navigate('candidate', { epochId, gen }); };
  const tracker = svg.proposingTracker({ fieldStatus: fs, onCompetitor });
  return section(live ? 'Proposed field · LIVE' : 'Proposed field', tracker);
}

// The shared champion-gate CAPTION fragment: the crowned / stands /
// deciding phrase every figure's caption appends. An undecided gate → ''.
function gateNoteFor(gateState, championId) {
  return gateState === 'crowned' ? ` · champion-gate: ${championId} promoted ${CROWN.current}`
    : gateState === 'stands' ? ' · champion-gate: champion stands'
    : gateState === 'deciding' ? ' · champion-gate: deciding…' : '';
}

// single_elim / double_elim — the RADIAL bracket: concentric rings narrowing
// to a centre champion seat, one spoke per generation. Double elimination puts
// the winners' bracket on the upper arc and the losers' on the lower one, split
// by a dashed equator; a winners'→losers' drop is a rim-hugging transfer arc.
// The figure reads the SERVED model verbatim (rounds + gen_states) — no client
// re-derivation.
function renderBracket(st, ctx, epochId, structure) {
  const model = elimModel(st) || {
    rounds: (st && Array.isArray(st.rounds)) ? st.rounds : [],
    gen_states: (st && Array.isArray(st.gen_states)) ? st.gen_states : [],
    winners: splitBand((st && st.rounds) || [], () => true), losers: null, live: !!(st && st.live),
  };
  const openGen = (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); };
  const hasFigure = model.hasMatches !== false && model.winners.length;
  const isDouble = structure === 'double_elim';
  const nodes = [];

  const card = el('div', { class: 'dn-panel dn-figpane' });
  card.appendChild(hasFigure
    ? svg.elimRadial({
        rounds: model.rounds, gen_states: model.gen_states,
        championId: model.championId, benchmarkId: model.benchmarkId,
        gateState: model.gateState, live: model.live, double: isDouble, onCompetitor: openGen,
      })
    : empty(model.live ? 'The bracket is being seeded — matches fill in as runs land.' : 'No bracket rounds recorded yet.'));
  if (model.winners.length) {
    card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      'rounds are concentric rings narrowing to the champion seat at the center; each spoke is a generation — the rings it survived read green, the ring it was eliminated at turns red ✕, and the survivor dashes into the center gate ' + CROWN.current
      + (model.benchmarkId ? ' · ' + CROWN.former + ' = displaced incumbent' : '')
      + (isDouble ? ' · winners’ bracket on the upper arc, losers’ on the lower; a dashed arc along the rim carries a first loss down into the losers’ bracket' : '')
      + gateNoteFor(model.gateState, model.championId)
      + (model.live ? ' · LIVE — still-racing spokes are dashed' : '') }));
  }
  nodes.push(section(model.live ? 'Bracket · LIVE — rings narrowing to the champion gate' : 'Bracket · rings narrowing to the champion gate', card));

  const standings = standingsTable(st, ctx, epochId, !!(st && st.live));
  if (standings) nodes.push(section('Standings', standings));
  return nodes;
}

function renderSwiss(st, ctx, epochId) {
  const nodes = [];
  const model = swissModel(st) || { rounds: [], standings: [], live: !!(st && st.live), hasRounds: false };
  const open = (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); };
  const lCard = el('div', { class: 'dn-panel dn-figpane' });
  lCard.appendChild(model.hasRounds
    ? svg.swissLadder({
        rounds: model.rounds, standings: model.standings,
        championId: model.championId, benchmarkId: model.benchmarkId,
        live: model.live, gateState: model.gateState, gateDelta: model.gateDelta,
        onCompetitor: open,
      })
    : empty(model.live ? 'The swiss is being seeded — pairings fill in as runs land.' : 'No swiss rounds recorded yet.'));
  if (model.hasRounds) {
    const gateNote = gateNoteFor(model.gateState, model.championId);
    lCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      'each round pairs the field; Copeland points accumulate (win 1 / draw ½) · hover a pairing for its Δ scalar · ' + CROWN.current + ' = champion · ' + CROWN.former + ' = former champion (displaced incumbent) — the swiss leader must beat the incumbent at the champion-gate'
      + gateNote
      + (model.live ? ' · LIVE — the winner is not committed until the final gate' : '') }));
  }
  // ONE view: the ladder lays out every round's pairings (with winners and Δ on
  // hover) alongside the accumulating standings and the champion-gate, so a
  // standalone "Pairings · round by round" table would only repeat the pairings.
  // Everything lives in this single section.
  nodes.push(section(model.live ? 'Swiss · LIVE — rounds, standings & champion-gate' : 'Swiss · rounds, standings & champion-gate', lCard));
  // the Standings table rides BELOW the ladder so the per-challenger override
  // CONTROL plane (force promote/reject + provenance) is consistent across EVERY
  // structure rather than the bracket, racing and gauntlet ones alone. The ladder
  // lays out pairings; this table carries the actionable per-row controls.
  const standings = standingsTable(st, ctx, epochId, !!(st && st.live));
  if (standings) nodes.push(section('Standings', standings));
  return nodes;
}

// ---- racing — a successive-halving rung ladder ---------------------

function renderRacing(st, ctx, epochId) {
  const nodes = [];
  const live = !!(st && st.live);

  // The rung/gate model is the SINGLE source — racingModel builds each rung from
  // the FULL FIELD (the union of every rung matchup + the authoritative full-rung
  // live_progress), so an IN-FLIGHT rung published as N champion-vs-survivor
  // matchups renders ALL lanes (every survivor), rather than matches[0]'s first
  // lane alone. The figures (scalar track + funnel) read straight off these rungs.
  const rm = racingModel(st) || {};
  const rungs = Array.isArray(rm.rungs) ? rm.rungs : [];
  const championId = rm.championId || null;
  const gateState = rm.gateState || (live ? 'deciding' : 'pending');
  const gateDelta = svg.isNum(rm.gateDelta) ? rm.gateDelta : null;
  const benchmarkId = rm.benchmarkId || null;
  const championScalar = svg.isNum(rm.championScalar) ? rm.championScalar : championScalarOf(st, benchmarkId);
  const openGen = (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); };

  // ── THE PRIMARY FIGURE: the SCALAR TRACK (racing.html opt 1) ─────────
  // Every gen on a shared scalar number-line; marker SIZE = inverse loss (bigger
  // = better) so the surviving leader is the fattest dot and the cuts shrink
  // away; the champion v0 is a dashed benchmark. The track honours all four
  // lifecycle states (the rungs carry live_progress per the live producer).
  const trackCard = el('div', { class: 'dn-panel dn-figpane' });
  // RENDER THE IN-FLIGHT RUNG: racingModel builds a rung (pending=true, full
  // field, live_progress) for a still-streaming rung even before any survivor/cut
  // commits — so `rungs.length > 0` and the scalar track renders ALL lanes
  // racing. The "No rungs evaluated yet." empty is reachable ONLY when no source
  // holds a rung at all (no published or streaming rung, no completed record) —
  // never while a multi-survivor rung is in flight.
  trackCard.appendChild(rungs.length
    ? svg.racingScalarTrack({
        rungs, championId, benchmarkId, championScalar, live, gateState,
        responsive: true, onCompetitor: openGen,
      })
    : empty(live ? 'The race is being seeded — the first rung fills in as runs land.' : 'No rungs evaluated yet.'));
  if (rungs.length) {
    const gateNote = gateNoteFor(gateState, championId);
    trackCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      (benchmarkId ? `every gen is plotted on a shared scalar number-line (lower = better); the dashed line is the champion v0 = ${benchmarkId} benchmark · ` : '')
      + 'marker size = inverse loss (bigger = better) — the survivor is the fattest dot, the cuts shrink away past the cut tick · click a competitor → open'
      + gateNote
      + (live ? ' · LIVE — markers grow as boards land; the winner is not committed until the final gate' : '') }));
  }
  nodes.push(section(live ? 'Scalar track · LIVE — the field on one number-line (lower = better)' : 'Scalar track · the field on one number-line (lower = better)', trackCard));

  // ── SECONDARY: the SURVIVAL FUNNEL (the rung-by-rung FLOW view) ──────
  // The scalar track shows WHERE each gen lands on the loss axis; the funnel
  // shows the FLOW of the field narrowing rung-by-rung (who survived each cut,
  // who was eliminated, per-lane "k/N boards" live progress). It adds live value
  // the single-axis track cannot — the structural narrowing — so it rides below.
  if (rungs.length) {
    const flowCard = el('div', { class: 'dn-panel dn-figpane' });
    flowCard.appendChild(svg.survivalFunnel({
      rungs, championId, benchmarkId, live, gateState, gateDelta,
      responsive: true, onCompetitor: openGen,
    }));
    flowCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      'each rung races the field on a fraction of the board, then cuts the worst by η · ✕ = cut · ↑ = survives · ' + CROWN.current + ' = champion-gate winner'
      + (live ? ' · LIVE — in-flight lanes read "k/N boards"' : '') }));
    nodes.push(section(live ? 'Survival funnel · LIVE — field narrowing rung-by-rung' : 'Survival funnel · field narrowing rung-by-rung', flowCard));
  }

  const standings = standingsTable(st, ctx, epochId, live);
  if (standings) nodes.push(section('Standings', standings));
  return nodes;
}

// gauntlet — the structure-LEVEL field-bars figure (gauntlet.html opt 5). One
// wave of challengers vs the champion standard on a shared scalar axis, the gate
// threshold line, outcome colours, survivor marks, the projected ghost. This is
// ADDED alongside (not in place of) the gens.js match ladder.
function renderGauntlet(st, ctx, epochId) {
  const nodes = [];
  const live = !!(st && st.live);
  const model = gauntletModel(st) || { challengers: [], live, hasField: false };
  const openGen = (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); };

  const card = el('div', { class: 'dn-panel dn-figpane' });
  card.appendChild(model.hasField
    ? svg.gauntletFieldBars({
        championId: model.championId, championScalar: model.championScalar,
        promoteMargin: model.promoteMargin, challengers: model.challengers,
        live: model.live, onCompetitor: openGen,
      })
    : empty(live ? 'The gauntlet is being seeded — challengers fill in as runs land.' : 'No challengers recorded for this gauntlet.'));
  if (model.hasField) {
    card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      (model.championId ? `the wave is measured against the champion standard = ${model.championId}` + (svg.isNum(model.championScalar) ? ` (${svg.fmt(model.championScalar, 2)})` : '') + ' · ' : '')
      + 'each bar runs from the standard out to a challenger’s scalar (lower = better); a bar that clears the dashed promote gate reads ↑ survivor · ✕ = failed the gate · click a challenger → open'
      + (live ? ' · LIVE — in-flight challengers ghost in with a "k/N boards" sub-bar; the winner is not committed until the gate' : '') }));
  }
  nodes.push(section(live ? 'Gauntlet field · LIVE — the wave vs the champion standard' : 'Gauntlet field · the wave vs the champion standard', card));

  const standings = standingsTable(st, ctx, epochId, live);
  if (standings) nodes.push(section('Standings', standings));
  return nodes;
}

// ---- shared: the standings leaderboard table -----------------------

// Resolve a standing's override prov: the DURABLE readback wins; else the
// operator's optimistic queued stamp; else — on a SETTLED round whose queued
// promote never landed in the advanced set — the DRAINED state (queued, never
// fired). null when neither exists (back-compat clean).
function resolveOverrideProv(gid, durable, pending, settled, promotedSet) {
  if (durable) return durable;
  if (!pending) return null;
  if (settled && pending.action === 'promote' && promotedSet && !promotedSet.has(String(gid))) {
    return { action: pending.action, reason: pending.reason, state: 'drained' };
  }
  return pending;
}

function standingsTable(st, ctx, epochId, live) {
  const standings = (st && Array.isArray(st.standings)) ? st.standings.slice() : [];
  if (!standings.length) return null;
  const structure = (st && st.structure) || 'gauntlet';
  // track whether any row resolves to the DEFERRED status-pill state (still in
  // contention — no crown / no elimination committed) so the table can carry an
  // explicit field-level "deferred · winner resolves after the duels" caption,
  // making a held-but-not-rejected field read intentionally rather than blank.
  let anyDeferred = false;
  // operator-override readback (durable field record): {gid: {action, ts,
  // reason, state}}. KEY-ABSENT on every gate-decided / single-challenger /
  // pre-feature run → no chip → byte-identical to today.
  const overrides = (st && st.override_status && typeof st.override_status === 'object') ? st.override_status : null;
  // per-slot diversity status (field_status[].diversity_status ∈ applied /
  // penalized / soft_rejected), keyed by generation_id for a per-row badge. Only
  // attached for a real field with the diversity block (≥2 challengers) → no
  // badge on a gauntlet / single-challenger / pre-feature run (byte-identical).
  const divStatus = diversityStatusByGen(st);
  // the per-challenger CORE IDEA (§3): the proposing step already records each
  // applied challenger's one-line hypothesis on its field_status row, and a
  // standing that names only an id makes the operator open the candidate to
  // learn what it even tried. Threaded as a dim second line under the id.
  const ideaByGen = coreIdeaByGen(st);
  // the advanced SET at settle (supports MULTIPLE promoted / ties) — resolves the
  // DRAINED state for an optimistic stamp that never landed.
  const promotedSet = (st && Array.isArray(st.promoted_generation_ids))
    ? new Set(st.promoted_generation_ids.map((g) => String(g))) : null;
  // the CONTROL plane: a live field accepts operator overrides; a read-only
  // workspace shows the control DISABLED (never POST-and-fail). The POST body
  // names the field round so the readback can attribute it.
  const settled = !live;
  // POLARITY: writable requires an EXPLICIT `read_only: false`, matching the
  // topbar controls (shell.js). A truthy read would let a health payload that
  // had not arrived — or a server that omits the field — render the override
  // controls ENABLED against a workspace that may reject the POST. The safe
  // default for a control affordance is off.
  const readOnly = !(state.health && state.health.read_only === false);
  const tournamentId = (st && st.tournament_id != null) ? String(st.tournament_id) : null;
  const bodyBase = {};
  if (epochId != null) bodyBase.epoch = String(epochId);
  if (tournamentId) bodyBase.tournament_id = tournamentId;
  if (structure) bodyBase.structure = String(structure);
  const onPost = (action, gid, reason) =>
    postFieldOverride(action, gid, Object.assign({}, bodyBase, reason ? { reason } : {}));
  const onChange = () => { if (state && typeof state._changed === 'function') state._changed(); };
  // Racing (successive-halving / best-arm) has NO head-to-head winner/loser —
  // each rung ranks survivors by SCALAR and cuts the worst; the promote/reject
  // is the gate rather than a match record. So W/L are structurally always 0 for
  // racing and a permanently-zero column reads as broken. Drop W/L for racing
  // (scalar + status carry the standing); keep them for the bracket structures
  // that actually populate them (single_elim / double_elim / swiss).
  const showWL = structure !== 'racing';
  standings.sort((a, b) => (svg.isNum(a.rank) ? a.rank : 1e9) - (svg.isNum(b.rank) ? b.rank : 1e9));
  // running tally for the field-level caption ('gate said X · operator forced Y')
  const forced = { promote: 0, reject: 0, drained: 0 };
  const standingRow = (s) => {
    let raw = String(s.status || '').toLowerCase();
    // LIVE — the verdicts have not committed; a standing tagged champion /
    // eliminated mid-run is the EVENTUAL outcome read from a half-finished
    // record. Treat everyone as still in contention so nobody is mislabeled —
    // and route through the SHARED structure mapper so the in-contention word
    // is structure-correct (elim → "in bracket", swiss → "playing", racing →
    // "racing"), NEVER a blanket "racing" for a non-racing tournament.
    if (live && (raw === 'champion' || raw === 'eliminated')) raw = 'competing';
    // INTERRUPTED — a row that was still in contention when the loop stopped
    // was never decided, and "racing" / "playing" / "in bracket" all claim it
    // still is. The row says what happened instead. Committed
    // verdicts (champion / eliminated) are real and pass through untouched.
    const undecided = st.interrupted && raw !== 'champion' && raw !== 'eliminated';
    const status = undecided ? 'undecided when the run ended'
      : structureStatusLabel(raw, structure);
    // the statusPill verdict-state mirror: anything that is NOT a committed
    // champion / eliminated reads as the DEFERRED pill (still in contention).
    if (status !== 'champion' && status !== 'eliminated') anyDeferred = true;
    // PROJECTED — an in-flight row (boards still streaming) shows a projected
    // scalar rather than a settled one: dashed/dimmed row + a "proj" badge + the
    // ~prefix on the number + a scored board-progress sub-bar.
    // The PROJ badge and its progress bar are a claim that boards are streaming
    // in RIGHT NOW. They expire with liveness: an interrupted row shows the
    // committed boards_done/total as a settled tally, never an animated bar.
    const proj = !live && st.interrupted
      ? false : !!(s.in_flight && svg.isNum(s.projected_scalar));
    const strandedBoards = (st.interrupted && s.in_flight) ? true : false;
    const rowCls = (status === 'champion' ? 'dn-board-champ' : status === 'eliminated' ? 'dt-standings-out' : '')
      + (proj ? ' dt-proj-row' : '');
    const bd = svg.isNum(s.boards_done) ? s.boards_done : null;
    const bt = svg.isNum(s.boards_total) ? s.boards_total : null;
    const frac = (bd != null && bt != null && bt > 0) ? Math.min(1, bd / bt) : null;
    const scalarCell = proj
      ? { class: 'dn-num dn-mono dt-proj-val', title: 'projected — boards still streaming in', el: [
          el('span', { text: '~' + svg.fmt(s.projected_scalar, 1) }),
          el('span', { class: 'dt-proj-badge', text: 'proj' }),
        ] }
      : strandedBoards
      ? { class: 'dn-num dn-mono dn-faint',
          title: 'boards were still running when the run stopped — this scalar was never committed',
          el: [
            el('span', { text: svg.isNum(s.projected_scalar) ? '~' + svg.fmt(s.projected_scalar, 1)
                               : (svg.isNum(s.scalar) ? svg.fmt(s.scalar, 1) : '—') }),
            el('span', { class: 'dt-interrupted-pill',
                         text: (bd != null && bt != null) ? bd + '/' + bt + ' scored' : 'uncommitted' }),
          ] }
      : { class: 'dn-num dn-mono', text: svg.isNum(s.scalar) ? svg.fmt(s.scalar, 1) : '—' };
    // operator-override provenance rides BESIDE the status pill (overrideChip),
    // never recoloring the verdict — durable readback wins, else the optimistic
    // queued stamp, else (settled never-landed promote) drained. Absent → null.
    const gidStr = String(s.generation_id);
    const durable = overrides ? overrides[gidStr] : null;
    // once the durable readback carries this override, drop the optimistic stamp
    // so they never double up (the readback is now authoritative).
    if (durable) clearPendingOverride(gidStr);
    const ovProv = resolveOverrideProv(gidStr, durable, pendingOverride(gidStr), settled, promotedSet);
    if (ovProv) {
      const a = String(ovProv.action || '');
      if (String(ovProv.state || 'applied') === 'drained') forced.drained += 1;
      else if (a === 'promote') forced.promote += 1;
      else if (a === 'reject') forced.reject += 1;
    }
    const ovChip = overrideChip(ovProv);
    if (ovChip && ovProv) {
      const act = ovProv.action === 'promote' ? 'force-promoted' : 'force-rejected';
      attachHovercard(ovChip, () => hovercardBody([
        el('div', { class: 'dn-hc-title', text: 'operator override · ' + act }),
        (typeof ovProv.reason === 'string' && ovProv.reason)
          ? el('div', { class: 'dn-hc-row', text: ovProv.reason })
          : el('div', { class: 'dn-hc-row dn-faint', text: 'no reason recorded' }),
      ]));
    }
    // the per-challenger override CONTROL cell (confirm-inline arm→reason→POST,
    // optimistic queued stamp, disabled when read_only/settled/overridden).
    // existingOverride = the durable readback only (the cell reads its own stamp).
    const ctlCell = s.generation_id ? overrideControlCell({
      gid: gidStr, epochId, tournamentId, structure,
      readOnly, settled, existingOverride: durable, onPost, onChange,
    }) : null;
    // the per-row diversity badge — soft-rejected reuses the DEFERRED pill
    // (held rather than promoted); penalized reads as a caution chip. Absent → null.
    const divBadge = diversityBadge(divStatus ? divStatus[gidStr] : null);
    return {
      class: rowCls,
      cells: [
        { class: 'dn-mono', text: svg.isNum(s.rank) ? String(s.rank) : '—' },
        { class: 'dn-mono', el: [
          el('span', { text: (s.generation_id || '—') + (status === 'champion' ? ' ' + CROWN.current : '') }),
          coreIdeaLine(ideaByGen ? ideaByGen[gidStr] : null),
        ].filter(Boolean) },
        { el: [statusPill(status), ovChip, divBadge] },
        scalarCell,
        // the visibility rating (server-joined BT triple; never the gate):
        // mono `1512 ±34`, faint `provisional` under MIN_RATING_GAMES, `—`
        // when the fold has not rated this generation. Quiet — NO chips.
        { class: 'dn-num', el: [ratingCellEl(s)] },
        ...(showWL ? [
          { class: 'dn-num dn-mono', text: svg.isNum(s.wins) ? String(s.wins) : '—' },
          { class: 'dn-num dn-mono', text: svg.isNum(s.losses) ? String(s.losses) : '—' },
        ] : []),
        { el: [
          proj && frac != null ? el('span', { class: 'dt-proj-bar', title: bd + '/' + bt + ' boards scored' }, [
            el('span', { class: 'dt-proj-bar-fill', style: 'width:' + Math.round(frac * 100) + '%;' }),
          ]) : null,
          proj && frac != null ? el('span', { class: 'dt-proj-bar-lab', text: bd + '/' + bt }) : null,
          s.generation_id ? el('a', { class: 'dn-linkbtn', href: ctx.href('candidate', { epochId, gen: s.generation_id }), text: 'open →' }) : null,
        ] },
        { class: 'dn-ovr-col', el: ctlCell ? [ctlCell] : [] },
      ],
    };
  };
  const tbl = dataTable({
    class: 'dn-board-table dt-standings',
    columns: [
      { label: 'rank' }, { label: 'generation' }, { label: 'status' }, { label: 'scalar', class: 'dn-num' },
      { label: 'rating', class: 'dn-num' },
      ...(showWL ? [{ label: 'W', class: 'dn-num' }, { label: 'L', class: 'dn-num' }] : []),
      { label: '' }, { label: 'override', class: 'dn-ovr-col' },
    ],
    rows: standings.map(standingRow),
  });
  const caps = [];
  // a field-level DEFERRED caption — when at least one standing is held in
  // contention (the deferred pill state) and nothing has yet been crowned /
  // eliminated, surface WHY the field reads unsettled: the winner resolves once
  // the duels separate the strengths. Only while LIVE (an uncommitted run) and
  // only when no terminal verdict has landed, so a settled board stays quiet.
  const anyTerminal = standings.some((s) => {
    const r = String(s.status || '').toLowerCase();
    return r === 'champion' || r === 'eliminated';
  });
  if (live && anyDeferred && !anyTerminal) {
    caps.push(el('p', { class: 'dn-faint dt-standings-deferred', style: 'font-size:11px;margin:8px 0 0;',
      text: 'deferred — no winner committed yet · the standing resolves once the duels separate the strengths (held, not rejected)' }));
  }
  // the OVERRIDE PROVENANCE caption — 'gate said X · operator forced Y' — reads
  // only when an override is present (durable/queued/drained); a clean gate-
  // decided field stays byte-identical.
  if (forced.promote || forced.reject || forced.drained) {
    const verbs = [];
    if (forced.promote) verbs.push('forced ' + forced.promote + (forced.promote > 1 ? ' promotions' : ' promotion'));
    if (forced.reject) verbs.push('forced ' + forced.reject + (forced.reject > 1 ? ' rejections' : ' rejection'));
    if (forced.drained) verbs.push(forced.drained + ' queued ' + (forced.drained > 1 ? 'overrides' : 'override') + ' drained (never fired)');
    caps.push(el('p', { class: 'dn-faint dt-standings-override', style: 'font-size:11px;margin:6px 0 0;',
      text: 'gate said settle on the standings · operator ' + verbs.join(' · ') }));
  }
  if (caps.length) return el('div', { class: 'dt-standings-wrap' }, [tbl, ...caps]);
  return tbl;
}

function statusPill(status) {
  const s = status || 'alive';
  // map the standings vocabulary onto verdict-pill semantics so the pill
  // reads in every theme: champion→promoted, eliminated→rejected, else→deferred
  // (alive / playing / in bracket / racing — still in contention).
  const verdict = s === 'champion' ? 'promoted' : s === 'eliminated' ? 'rejected' : 'deferred';
  const pill = verdictPill(verdict);
  pill.textContent = s;
  return pill;
}

// {gid: diversity_status} off the field_status records, ONLY when the diversity
// block is attached (a real ≥2-challenger field). Absent / single-challenger /
// pre-feature → null → no per-row badge (byte-identical to today).
// {gid: core_idea} off the field_status records — the applied challenger's
// one-line hypothesis, recorded by the proposing step. null when no slot
// carries one (gauntlet / a pre-feature record), so the standings rows are
// byte-identical to before the thread existed.
function coreIdeaByGen(st) {
  if (!st || !Array.isArray(st.field_status)) return null;
  const by = {};
  let any = false;
  for (const f of st.field_status) {
    if (!f || typeof f !== 'object' || f.generation_id == null) continue;
    const idea = (typeof f.hypothesis === 'string' && f.hypothesis.trim()) ? f.hypothesis.trim() : null;
    if (idea) { by[String(f.generation_id)] = idea; any = true; }
  }
  return any ? by : null;
}

function diversityStatusByGen(st) {
  if (!st || !st.diversity || !Array.isArray(st.field_status)) return null;
  const by = {};
  let any = false;
  for (const f of st.field_status) {
    if (!f || typeof f !== 'object' || f.generation_id == null) continue;
    const ds = f.diversity_status;
    if (ds === 'soft_rejected' || ds === 'penalized') { by[String(f.generation_id)] = ds; any = true; }
  }
  return any ? by : null;
}

// The per-row diversity badge. `soft_rejected` reuses the DEFERRED pill (held,
// not promoted — the field's most legible "this idea was cut for overlap"
// signal); `penalized` is a softer caution chip. `applied` / absent → null (no
// badge), so a clean diverse field is byte-identical to today.
function diversityBadge(ds) {
  if (ds === 'soft_rejected') {
    const p = verdictPill('deferred');
    p.textContent = 'soft-rejected';
    p.setAttribute('class', (p.getAttribute('class') || '') + ' dn-div-softrej');
    attachHovercard(p, () => hovercardBody([
      el('div', { class: 'dn-hc-title', text: 'diversity · soft-rejected' }),
      el('div', { class: 'dn-hc-row dn-faint', text: 'idea overlap exceeded the diversity tolerance — held out of the field (not gate-rejected)' }),
    ]));
    return p;
  }
  if (ds === 'penalized') {
    const c = chip('live', 'div-penalized', 'dn-div-penalized');
    attachHovercard(c, () => hovercardBody([
      el('div', { class: 'dn-hc-title', text: 'diversity · penalized' }),
      el('div', { class: 'dn-hc-row dn-faint', text: 'idea overlap incurred a diversity penalty but the challenger still entered the field' }),
    ]));
    return c;
  }
  return null;
}

function linkGen(gen, ctx, epochId) {
  if (!gen) return el('span', { class: 'dn-faint', text: 'bye' });
  return el('a', { class: 'dn-linkbtn dn-mono', href: ctx.href('candidate', { epochId, gen }), text: String(gen) });
}


// js/builder/model.js — builder metadata + the preview schematic.
//
// Pure, data-only: the five tournament structures (label / glyph / blurb), the
// per-structure tunable param specs (with the info-popover copy: definition +
// default + the cost/contract tradeoff), a small SCHEMATIC model builder that
// feeds the svg.js per-structure figures an illustrative field derived from the
// draft params (so the preview reuses the runtime figures), and the chat-pane
// width / collapse persistence. No DOM, no fetching.

import { svgEl } from '../core/dom.js';

export const CHAT_MIN = 240;
export const CHAT_MAX = 560;
const CHAT_W_KEY = 'zicato.T.builder.chatWidth';
const CHAT_C_KEY = 'zicato.T.builder.chatCollapsed';
const DEFAULT_CHAT_W = 340;

export function readChatWidth() {
  let v = null;
  try { v = window.localStorage.getItem(CHAT_W_KEY); } catch (e) { /* ignore */ }
  return clampWidth(v == null ? DEFAULT_CHAT_W : Number(v));
}
export function persistChatWidth(w) {
  const n = clampWidth(w);
  try { window.localStorage.setItem(CHAT_W_KEY, String(n)); } catch (e) { /* ignore */ }
  return n;
}
export function clampWidth(w) {
  let n = Number(w);
  if (!isFinite(n)) n = DEFAULT_CHAT_W;
  return Math.max(CHAT_MIN, Math.min(CHAT_MAX, Math.round(n)));
}
export function readChatCollapsed() {
  let v = null;
  try { v = window.localStorage.getItem(CHAT_C_KEY); } catch (e) { /* ignore */ }
  return v === '1';
}
export function persistChatCollapsed(c) {
  try { window.localStorage.setItem(CHAT_C_KEY, c ? '1' : '0'); } catch (e) { /* ignore */ }
  return !!c;
}

// ── the five structures ───────────────────────────────────────────────

export const STRUCTURE_GLYPH = {
  gauntlet: '⚔', single_elim: '◣', double_elim: '◳', swiss: '⇄', racing: '⥥',
};

export const STRUCTURES = [
  { id: 'gauntlet', label: 'Gauntlet', blurb: 'Each challenger duels the champion; the best Δ promotes.' },
  { id: 'single_elim', label: 'Single elim', blurb: 'A knockout bracket — one loss eliminates.' },
  { id: 'double_elim', label: 'Double elim', blurb: 'A bracket with a losers’ lane — two losses out.' },
  { id: 'swiss', label: 'Swiss', blurb: 'Fixed rounds, score-paired; Copeland-point standings.' },
  { id: 'racing', label: 'Racing', blurb: 'Successive halving — the field is cut each rung.' },
];

// ── per-structure param specs (label + bounds + info-popover copy) ─────

const FIELD_SIZE = {
  key: 'field_size', label: 'Field size', def: 2, min: 1, step: 1, int: true,
  info: {
    title: 'Field size', def: '2',
    body: 'How many challengers enter the tournament each round. A larger field explores more variants per epoch but multiplies cost (each adds duels/matches across the whole board). field_size=1 degrades a bracket/swiss/racing structure to a single champion-vs-challenger gauntlet.',
  },
};
const REPLICATES = {
  key: 'replicates', label: 'Replicates', def: 1, min: 1, step: 1, int: true,
  info: {
    title: 'Replicates', def: '1',
    body: 'How many times each board unit is re-run to average out model noise. replicates≥2 is recommended for bracket / swiss structures, where a single noisy run can flip a match verdict — at a linear cost multiple.',
  },
};

// The Bradley–Terry EVIDENCE GATE params — generic tournament params (they
// live in the same opaque params map as field_size), so they apply to every
// structure. The threshold is OPT-IN: setting 0 REMOVES the key from the
// contract (removeAtZero) so an unset gate hashes byte-identically to a
// contract that predates it. These are the recommended-scaffold's gate knobs
// — the form could not display them before this spec existed.
const EVIDENCE_THRESHOLD = {
  key: 'promote_confidence_threshold', label: 'Evidence-gate threshold', def: 0,
  min: 0, max: 0.99, step: 0.05, int: false, removeAtZero: true,
  info: {
    title: 'Evidence-gate threshold', def: 'unset (gate off); scaffold 0.8',
    body: 'The Bradley–Terry pre-gate: crown only when P(challenger > champion) reaches this probability AND the rating confidence intervals separate. Soundness, not power — it stops noise-crownings but needs replicated evidence to say yes, so give it an honest replicate budget. 0 removes the param (gate off).',
  },
};
const EVIDENCE_REPLICATES = {
  key: 'promote_confidence_replicates', label: 'Evidence replicate budget', def: 3,
  min: 0, step: 1, int: true,
  info: {
    title: 'Evidence replicate budget', def: '3; scaffold 32',
    body: 'How many extra crowning-pair replicates the defer→replicate loop may spend chasing CI separation before going inconclusive. Each replicate is a FRESH board sweep for BOTH contestants (~budget × 2 × board runs, per confirmed crowning) — after the F2 fix this is typically the largest cost driver, so the cost meter prices it. CI separation on a near-tie needs ~32+ decisive duels.',
  },
};

export function paramSpecsFor(structure) {
  const evidence = [EVIDENCE_THRESHOLD, EVIDENCE_REPLICATES];
  switch (structure) {
    case 'gauntlet':
      return [FIELD_SIZE, REPLICATES, ...evidence];
    case 'single_elim':
    case 'double_elim':
      return [FIELD_SIZE, REPLICATES, ...evidence];
    case 'swiss':
      return [FIELD_SIZE, REPLICATES, {
        key: 'rounds_n', label: 'Rounds', def: 4, min: 1, step: 1, int: true,
        info: { title: 'Rounds (rounds_n)', def: '4', body: 'How many score-paired swiss rounds to play. More rounds sharpen the standings (each pairs nearer-ranked challengers) but cost rounds_n × pairings × board per epoch.' },
      }, ...evidence];
    case 'racing':
      return [FIELD_SIZE, REPLICATES, {
        key: 'eta', label: 'Cut factor (eta)', def: 2, min: 2, step: 1, int: true,
        info: { title: 'Cut factor (eta)', def: '2', body: 'Successive-halving aggressiveness: each rung keeps 1/eta of the surviving field and scores it on an eta-times-larger board slice. A larger eta cuts faster (cheaper, riskier — a good late bloomer can be cut early).' },
      }, {
        key: 'board_fraction', label: 'Rung-0 board fraction', def: 0.25, min: 0.05, max: 1, step: 0.05, int: false,
        info: { title: 'Rung-0 board fraction', def: '0.25', body: 'The fraction of the board the FIRST (cheapest) rung scores on. Smaller is cheaper but a thin first rung can cut a challenger on too little signal; the slice grows by eta each rung up to the full board.' },
      }, {
        // OPT-IN absolute override of the rung-0 slice: 0 REMOVES the key
        // (removeAtZero) so an unset override hashes byte-identically to a
        // contract that predates it, and the rung-0 slice falls back to
        // ceil(board_fraction × board). Both estimators (estimateCost's
        // _racing_cost + validateContract) ALREADY read this key — this spec
        // only surfaces the control (the L3 cost-relevant SPEC-only change).
        key: 'rung0_board_size', label: 'Rung-0 board size (override)', def: 0,
        min: 0, step: 1, int: true, removeAtZero: true,
        info: { title: 'Rung-0 board size', def: 'unset (use the fraction)', body: 'An explicit entry COUNT for the first (cheapest) racing rung, overriding board_fraction. When > 0 the first rung scores exactly this many train entries (capped at the board); 0 removes the override and the rung-0 slice falls back to ceil(board_fraction × board). A larger rung-0 buys more signal on the cheapest rung at more cost.' },
      }, {
        key: 'slice_schedule', label: 'Board-slice schedule', def: 'prefix',
        options: [
          { value: 'prefix', label: 'Prefix (legacy)' },
          { value: 'stratified_random_v1', label: 'Stratified random' },
        ],
        info: { title: 'Board-slice schedule', def: 'prefix (legacy); scaffold: stratified random', body: 'Prefix preserves the board authoring order. Stratified random derives a reproducible permutation from the frozen board and its tags, then uses nested prefixes of that order. It prevents authored order from deciding a cut while keeping every rung reproducible and tag-balanced.' },
      }, ...evidence];
    default:
      return [FIELD_SIZE, REPLICATES, ...evidence];
  }
}

// ── the schematic preview model ───────────────────────────────────────
//
// Build an ILLUSTRATIVE field for the chosen structure + params so the svg.js
// figure can draw the SAME shape the runtime view shows — champion = reference,
// gate = shaded band (the figures already render those). The model is purely a
// schematic of the SHAPE (rungs / rounds / bracket / dot rows), not real
// results, so it never implies a run happened.

function fieldIds(n) {
  const out = [];
  for (let i = 1; i <= Math.max(1, n); i += 1) out.push('c' + i);
  return out;
}

export function schematicModel(structure, params, boardSize) {
  const fieldSize = Math.max(1, intOf(params, 'field_size', 2));
  const ids = fieldIds(fieldSize);
  const board = Math.max(1, boardSize || 6);
  if (structure === 'racing') return racingSchematic(ids, params, board);
  if (structure === 'swiss') return swissSchematic(ids, params);
  if (structure === 'single_elim' || structure === 'double_elim') return elimSchematic(ids);
  return gauntletSchematic(ids);
}

function gauntletSchematic(ids) {
  // the gauntlet Δ dot-plot: one row per challenger, all vs the champion ref.
  return {
    items: ids.map((id, i) => ({ label: id, value: -(i + 1) * 0.5, context: 'vs v0' })),
    reference: { value: 0, label: 'champion v0' },
    labelWidth: 90,
  };
}

function elimSchematic(ids) {
  // collapse the field into a single illustrative winners' round of pairings.
  // The runtime figure reads the SERVED elim model (`rounds` + `gen_states`);
  // this synthetic preview fabricates the same shape for its demo field —
  // every lane pending, nobody advanced/eliminated.
  const matches = [];
  for (let i = 0; i < ids.length; i += 2) {
    const comps = [ids[i], ids[i + 1] || 'tbd'].filter(Boolean);
    matches.push({ competitors: comps, winner: null, loser: null, pending: true, bracket_slot: 'WB' + (i / 2 + 1) });
  }
  if (!matches.length) matches.push({ competitors: ids.slice(0, 1), winner: null, loser: null, pending: true, bracket_slot: 'WB1' });
  const genStates = ids.map((id) => ({
    generation_id: id,
    played_rounds: [0], advanced_rounds: [], lost_rounds: [],
    eliminated_at_round: null, side_by_round: { 0: 'WB' }, lb_entry_round: null, projected: null,
  }));
  return {
    rounds: [{ label: 'Round 1', bracket_side: 'WB', matches }],
    gen_states: genStates, championId: null, benchmarkId: 'v0',
  };
}

function swissSchematic(ids, params) {
  const roundsN = Math.max(1, intOf(params, 'rounds_n', 4));
  const rounds = [];
  for (let r = 0; r < roundsN; r += 1) {
    const pairings = [];
    for (let i = 0; i < ids.length; i += 2) {
      pairings.push({ a: ids[i], b: ids[i + 1] || null, bye: !ids[i + 1], winner: null, pending: true });
    }
    rounds.push({ label: 'Round ' + (r + 1), pairings });
  }
  const standings = ids.map((id, i) => ({ generation_id: id, rank: i + 1, points: 0, wins: 0, draws: 0, losses: 0, status: 'pending' }));
  return { rounds, standings, benchmarkId: 'v0' };
}

function racingSchematic(ids, params, board) {
  const eta = Math.max(2, intOf(params, 'eta', 2));
  const frac = floatOf(params, 'board_fraction', 0.25);
  const rungs = [];
  let alive = ids.slice();
  let rung = 0;
  let slice = Math.max(1, Math.ceil(board * frac));
  while (alive.length > 1 && rung < 6) {
    const keepN = Math.max(1, Math.floor(alive.length / eta));
    const survivors = alive.slice(0, keepN);
    const cut = alive.slice(keepN);
    rungs.push({
      label: 'Rung ' + rung, competitors: alive.slice(), survivors, cut,
      board_fraction: Math.min(1, (slice / board)), pending: false,
    });
    alive = survivors;
    slice = Math.min(board, slice * eta);
    rung += 1;
  }
  rungs.push({ label: 'Final', competitors: alive.slice(), survivors: alive.slice(), cut: [], board_fraction: 1, pending: false });
  return { rungs, championId: 'v0', benchmarkId: 'v0' };
}

function intOf(params, key, def) {
  const v = params && params[key];
  const n = Number(v);
  return isFinite(n) ? Math.round(n) : def;
}
function floatOf(params, key, def) {
  const v = params && params[key];
  const n = Number(v);
  return isFinite(n) ? n : def;
}

// ── pure cost / validation (the SAME arithmetic the backend's
//    operations.estimate_cost / operations.validate run) ────────────────
//
// PURE port of zicato/builder/operations.py's estimate_cost + validate, so a
// READ-ONLY contract preview can show the cost meter + validation diagnostics
// CLIENT-SIDE — no `/builder/op` round-trip, no backend dependency. The builder
// view still drives its preview from the live `/builder/op` envelope (the server
// is the source of truth there); this is for surfacing a FROZEN contract's
// estimate (Settings → Contract) from `/api/epoch` alone.
//
// The train/holdout COUNTS are passed in (the caller reads them from the same
// place the backend would split to: the builder draft's `holdout`, or
// `/api/epoch`'s server-computed `board_split`), so we never re-derive the
// deterministic sha256 hash split here — only the order-of-magnitude arithmetic.

// The per-structure default `replicates` when params.replicates is UNSET — the
// JS twin of zicato.selection.registry.STRUCTURE_DEFAULT_REPLICATES (itself
// derived from each strategy's `_default_replicates`). The base default is 2
// (the noise-aware posture — replication, not bracket shape, is the noise
// lever), inherited by gauntlet / elim / swiss; racing pins 1 (its replication
// is intrinsic to its escalating board slices). The cost meter MUST resolve
// the default by structure, not a flat 1, or it under-reports the schedule a
// structure actually runs — the Python estimator and this twin must agree
// (the py↔js parity test pins it).
export const STRUCTURE_DEFAULT_REPLICATES = {
  gauntlet: 2, single_elim: 2, double_elim: 2, swiss: 2, racing: 1,
};
export function defaultReplicatesFor(structure) {
  const d = STRUCTURE_DEFAULT_REPLICATES[structure];
  return d == null ? 1 : d;
}

// One cost-meter line: { label, runs, detail }.
function costLine(label, runs, detail) { return { label, runs, detail }; }

// Estimate board-runs-per-round for a structure + params over a given train /
// holdout split. Mirrors operations.estimate_cost (+ _racing_cost). Returns the
// SAME { board_runs_per_round, breakdown:[{label,runs,detail}] } shape the
// `/builder/op` cost envelope carries, so the preview renderer reads one shape.
// `proposerQuality` (optional) is the contract's proposer_quality block —
// when it opts into candidate screening (screen_entries > 0, best_of_n > 1)
// the screen's tryout runs are added, mirroring the Python term exactly.
// `overfitting` (optional) supplies random_baseline_every_n for the placebo
// term. The HONEST-METER terms mirror the Python estimator one-for-one:
// the candidate screen, the best-of-N propose calls (AUXILIARY LLM calls —
// listed but excluded from the board-runs headline), the evidence gate's
// crowning-confirm budget (budget × 2 × board when the threshold is set —
// per confirmed crowning, typically the largest term under the scaffold's
// 32-replicate budget), and the amortized placebo baseline.
export function estimateCost(structure, params, trainCount, holdoutCount, proposerQuality, overfitting) {
  const boardSize = Math.max(0, trainCount || 0);
  const holdoutSize = Math.max(0, holdoutCount || 0);
  // Default `replicates` to the STRUCTURE's own default (swiss / elim default
  // to 2), NOT a flat 1 — matching the Python estimator. An explicit
  // `replicates` in params is honored verbatim.
  const replicates = Math.max(1, intOf(params, 'replicates', defaultReplicatesFor(structure)));
  const fieldSize = Math.max(1, intOf(params, 'field_size', 2));
  const lines = [];
  let perRound;

  if (structure === 'gauntlet' || fieldSize <= 1) {
    perRound = fieldSize * replicates * boardSize;
    lines.push(costLine('duel runs', perRound,
      `field_size ${fieldSize} × replicates ${replicates} × board ${boardSize}`));
  } else if (structure === 'single_elim' || structure === 'double_elim') {
    let matches = Math.max(0, fieldSize - 1);
    if (structure === 'double_elim') matches = Math.max(0, 2 * (fieldSize - 1));
    perRound = matches * replicates * boardSize;
    lines.push(costLine('bracket-match runs', perRound,
      `${matches} matches × replicates ${replicates} × board ${boardSize}`));
  } else if (structure === 'swiss') {
    const roundsN = Math.max(1, intOf(params, 'rounds_n', 4));
    const pairings = Math.max(1, Math.floor(fieldSize / 2));
    perRound = roundsN * pairings * replicates * boardSize;
    lines.push(costLine('swiss-pairing runs', perRound,
      `rounds_n ${roundsN} × pairings ${pairings} × replicates ${replicates} × board ${boardSize}`));
  } else if (structure === 'racing') {
    const racing = racingCost(params, fieldSize, replicates, boardSize);
    perRound = racing.total;
    for (const l of racing.lines) lines.push(l);
  } else {
    perRound = fieldSize * replicates * boardSize;
    lines.push(costLine('duel runs', perRound, 'fallback'));
  }

  const holdoutConfirm = holdoutSize * replicates;
  if (holdoutConfirm) {
    lines.push(costLine('holdout-confirm runs', holdoutConfirm,
      `holdout ${holdoutSize} × replicates ${replicates}`));
    perRound += holdoutConfirm;
  }

  // Pre-tournament candidate screening (tryouts) — the JS twin of the
  // Python estimator's candidate-screen term: proposes × best_of_n ×
  // panel, where the panel is capped at the train board. Off (no line)
  // unless the contract opts in with screen_entries > 0 and a slate.
  const pq = proposerQuality || {};
  const screenEntries = Math.max(0, Number(pq.screen_entries) || 0);
  const bestOfN = Math.max(1, Number(pq.best_of_n) || 1);
  const proposes = (structure === 'gauntlet' || fieldSize <= 1) ? 1 : fieldSize;
  if (screenEntries > 0 && bestOfN > 1) {
    const panel = Math.min(screenEntries, boardSize);
    const screenRuns = proposes * bestOfN * panel;
    if (screenRuns) {
      lines.push(costLine('candidate-screen runs', screenRuns,
        `proposes ${proposes} × best_of_n ${bestOfN} × panel ${panel}`));
      perRound += screenRuns;
    }
  }

  // Best-of-N propose multiplier — auxiliary LLM CALLS, not board runs:
  // listed on the meter (real spend) but EXCLUDED from the headline.
  if (bestOfN > 1) {
    lines.push(costLine('best-of-N propose calls', proposes * bestOfN,
      `proposes ${proposes} × best_of_n ${bestOfN} — auxiliary LLM calls on the proposer-breadth role (sampling); critique / revise run on proposer-depth. Not board runs (excluded from the headline)`));
  }

  // The evidence gate's crowning-confirm budget: budget × 2 contestants ×
  // board FRESH sweeps chasing CI separation — per CONFIRMED crowning (an
  // upper bound per round). Only when the threshold is set (the gate is
  // opt-in; a threshold outside (0,1) reads as unset, mirroring
  // read_promote_confidence_threshold).
  const thr = Number(params && params.promote_confidence_threshold);
  if (isFinite(thr) && thr > 0 && thr < 1) {
    const rawBudget = Number(params && params.promote_confidence_replicates);
    const budget = (isFinite(rawBudget) && rawBudget >= 0) ? Math.round(rawBudget) : 3;
    const confirmRuns = budget * 2 * boardSize;
    if (confirmRuns) {
      lines.push(costLine('crowning-confirm runs (evidence gate)', confirmRuns,
        `budget ${budget} × 2 contestants × board ${boardSize} — per confirmed crowning (upper bound)`));
      perRound += confirmRuns;
    }
  }

  // The placebo control arm: one extra no-op challenger every N rounds,
  // amortized to per-round board runs.
  const of = overfitting || {};
  const baselineN = Math.max(0, Math.round(Number(of.random_baseline_every_n) || 0));
  if (baselineN > 0) {
    const placeboRuns = Math.ceil((replicates * boardSize) / baselineN);
    if (placeboRuns) {
      lines.push(costLine('placebo-baseline runs (amortized)', placeboRuns,
        `1 no-op challenger every ${baselineN} rounds × replicates ${replicates} × board ${boardSize}`));
      perRound += placeboRuns;
    }
  }
  return { board_runs_per_round: perRound, breakdown: lines };
}

// Successive-halving rung sum + the final full-board duel — the JS twin of
// operations._racing_cost (rung r scores the surviving field on a slice that
// grows by eta each rung, capped at the full board; the field halves by eta).
function racingCost(params, fieldSize, replicates, boardSize) {
  const eta = Math.max(2, intOf(params, 'eta', 2));
  const boardFraction = floatOf(params, 'board_fraction', 0.25);
  const rung0 = intOf(params, 'rung0_board_size', 0);
  const baseSlice = rung0 > 0 ? rung0 : Math.max(1, Math.ceil(boardSize * boardFraction));
  const lines = [];
  let alive = Math.max(1, fieldSize);
  let rung = 0;
  let total = 0;
  while (alive > 1 && rung < 32) {
    const sliceSize = Math.min(boardSize, baseSlice * (eta ** rung));
    const rungRuns = alive * replicates * sliceSize;
    total += rungRuns;
    lines.push(costLine(`rung ${rung} runs`, rungRuns,
      `alive ${alive} × replicates ${replicates} × slice ${sliceSize}`));
    if (sliceSize >= boardSize) break;
    alive = Math.max(1, Math.floor(alive / eta));
    rung += 1;
  }
  const finalRuns = replicates * boardSize;
  total += finalRuns;
  lines.push(costLine('racing-final runs', finalRuns,
    `full board ${boardSize} × replicates ${replicates}`));
  return { total, lines };
}

// Advisory warnings about a structure + params over a split. Mirrors
// operations.validate (the checks that do NOT need the entry tag set: the
// degenerate-field, racing rung-0, and bracket-replicates warnings — plus the
// STATISTICAL margin-vs-noise-floor rule when the caller supplies the
// measured floor via `opts`). Returns the SAME [{code, message, severity}]
// shape `/builder/op` carries.
//
// SCOPE — deliberately the ENTRY-FREE subset. The board-entry authoring
// codes are Python-only (they need the full entry objects, which this
// read-only preview never has): duplicate_entry_id, entry_id_unsafe,
// dotted_path_malformed, rubric_spec_invalid, json_schema_spec_invalid,
// entry_budget_outlier, judge_only_board — plus the tag-set checks
// holdout_tags_cover_whole_board. Those surface only through the backend
// envelope (`/builder/op` / `/builder/draft`); do NOT twin them here.
//
// `opts` (optional): { promoteMargin, noiseFloor } — the contract's
// promote_margin and the epoch's measured A/A floor (max |Δ|). When both are
// known, the margin sitting at/below the floor WITH the evidence gate off
// (params.promote_confidence_threshold unset / out of (0,1)) yields the
// `refuse`-severity `margin_below_noise_floor` warning — the JS twin of the
// Python rule. Recommend-only, like every warning here.
export function validateContract(structure, params, trainCount, holdoutCount, overfitting, opts) {
  const warnings = [];
  const fieldSize = Math.max(1, intOf(params, 'field_size', 2));
  const replicates = Math.max(1, intOf(params, 'replicates', 1));
  const boardSize = Math.max(0, trainCount || 0) + Math.max(0, holdoutCount || 0);
  const of = overfitting || {};

  if (structure !== 'gauntlet' && fieldSize === 1) {
    warnings.push({
      code: 'field_size_degrades_to_gauntlet', severity: 'warning',
      message: `structure '${structure}' with field_size=1 degrades to a single champion-vs-challenger duel (a gauntlet).`,
    });
  }
  const minSplit = of.min_board_size_for_split != null ? of.min_board_size_for_split : 0;
  if (boardSize && (holdoutCount || 0) === 0 && of.enabled !== false && minSplit && boardSize < minSplit) {
    warnings.push({
      code: 'holdout_disabled_small_board', severity: 'info',
      message: `board has ${boardSize} entries, below min_board_size_for_split=${minSplit}; the hash-derived holdout is disabled (no entry held out).`,
    });
  }
  if (structure === 'racing' && boardSize) {
    const boardFraction = floatOf(params, 'board_fraction', 0.25);
    const rung0 = intOf(params, 'rung0_board_size', 0);
    const train = Math.max(0, trainCount || 0);
    const sliceSize = rung0 > 0 ? rung0 : Math.max(1, Math.ceil(train * boardFraction));
    warnings.push({
      code: 'racing_rung0_slice', severity: 'info',
      message: `racing rung-0 slice = ${sliceSize} entries (ceil(board_fraction ${boardFraction} × board ${train})).`,
    });
  }
  if ((structure === 'single_elim' || structure === 'double_elim' || structure === 'swiss') && replicates < 2) {
    warnings.push({
      code: 'replicates_recommended_for_brackets', severity: 'warning',
      message: `structure '${structure}' with replicates=${replicates}: a single noisy run can flip a match verdict; replicates>=2 is recommended.`,
    });
  }
  const o = opts || {};
  const floor = Number(o.noiseFloor);
  const margin = Number(o.promoteMargin);
  if (isFinite(floor) && floor >= 0 && isFinite(margin)) {
    const thr = Number(params && params.promote_confidence_threshold);
    const gateOn = isFinite(thr) && thr > 0 && thr < 1;
    if (!gateOn && margin <= floor) {
      warnings.push({
        code: 'margin_below_noise_floor', severity: 'refuse',
        message: `promote_margin ${margin} does not clear the measured A/A noise floor ${floor} and the evidence gate (promote_confidence_threshold) is off: a duel decided by the margin alone cannot distinguish a real improvement from a re-roll of the same tree. Raise promote_margin above the floor or enable the evidence gate. Recommend-only — apply is not blocked.`,
      });
    }
  }
  return warnings;
}

// A small inline structure glyph as theme-adaptive SVG for the picker cards.
// PORTED from the approved tournament-builder mockup (its five structure-card
// figures), re-projected onto a crisp 24×24 viewBox at a single 1.6 stroke
// weight. Theme tokens only — strokes/fills use `currentColor` (the card paints
// the glyph in `var(--v2-accent)`), so the mark follows the card's ink in any
// colour scheme. Each structure declares optional stroked `paths` (brackets /
// ranking lines) and optional filled `dots` (the duel / funnel nodes):
//   gauntlet    — two dots joined by a short line (●—●, the 1v1 duel)
//   swiss       — three stacked horizontal lines of varying length (a ladder)
//   single_elim — a small knockout bracket
//   double_elim — a bracket with an extra losers'-lane line
//   racing      — a staggered 3 → 2 → 1 funnel; the cut arms fade
const GLYPH = {
  gauntlet: {
    dots: [{ cx: 7, cy: 12, r: 2.4 }, { cx: 17, cy: 12, r: 2.4 }],
    paths: ['M9.8,12 H14.2'],
  },
  swiss: {
    paths: ['M5,7 H17', 'M5,12 H14', 'M5,17 H19'],
  },
  single_elim: {
    paths: ['M5,7 H11 V12', 'M5,17 H11 V12', 'M11,12 H19'],
  },
  double_elim: {
    paths: ['M5,6 H11 V11', 'M5,11 H11', 'M11,11 H19', 'M5,16 H11 V11', 'M5,20 H17'],
  },
  racing: {
    // a compact 3→2→1 STAGGERED funnel (each row nested in the gaps of the one
    // above), matching the mockup; the cut arms on the right edge fade (`o`) —
    // successive halving. Rows centred on x=12.
    dots: [
      { cx: 7, cy: 7, r: 1.8 }, { cx: 12, cy: 7, r: 1.8 }, { cx: 17, cy: 7, r: 1.8, o: 0.32 },
      { cx: 9.5, cy: 12, r: 1.8 }, { cx: 14.5, cy: 12, r: 1.8, o: 0.32 },
      { cx: 12, cy: 17, r: 1.8 },
    ],
  },
};

export function structureGlyphSvg(structure) {
  const g = GLYPH[structure] || GLYPH.gauntlet;
  const kids = [];
  if (g.paths && g.paths.length) {
    kids.push(svgEl('g', { fill: 'none', stroke: 'currentColor', 'stroke-width': '1.6', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' },
      g.paths.map((d) => svgEl('path', { d }))));
  }
  if (g.dots && g.dots.length) {
    kids.push(svgEl('g', { fill: 'currentColor', stroke: 'none' },
      g.dots.map((c) => {
        const attrs = { cx: String(c.cx), cy: String(c.cy), r: String(c.r) };
        if (c.o != null) attrs['fill-opacity'] = String(c.o);
        return svgEl('circle', attrs);
      })));
  }
  return svgEl('svg', {
    class: 'dn-bld-cardglyph', width: 24, height: 24, viewBox: '0 0 24 24',
    role: 'img', 'aria-hidden': 'true', focusable: 'false',
  }, kids);
}

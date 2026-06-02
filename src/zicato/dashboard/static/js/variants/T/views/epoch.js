// variants/T/views/epoch.js — EPOCH OVERVIEW: the dense substrate of one epoch.
//
// Console IV's epoch overview leads with the OBJECTIVE, the collapsible
// proposer brief, the SLIM REEL (the rounds along the champion spine — adopted
// from V, trimmed to a compact fit-to-width spine), then the COMPACT
// board×generation drift-loss HEATMAP. The slim reel REPLACES the old lineage
// BUMPS (same champion-vs-challenger-over-rounds story — we show one, never
// both). Per the round-5 de-dup decision (fix #6) the heatmap STAYS here at the
// epoch overview; the board TRELLIS (small-multiples) lives in the Boards view
// (views/boards.js) — never both on one page.
//
// Data: /api/epoch, /api/lineage, /api/tournaments, /api/score-trajectory,
// /api/generation/{e}/{g}/per-entry.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { reel, reelDigest } from '../reel.js';
import { deriveLiveStatus } from '../livestatus.js';
import { gatedSwap, section, empty, stat, renderMarkdown, normaliseDecision, densityTokens } from '../ui.js';
import { structurePill, isNonGauntlet, structureLabel, reconstructRacing, normalizeStructure, racingModel, structureDigest } from './structure.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading epoch contract…' }));

  const [ep, lin, traj, bracket] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory(), D.bracket()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Epoch' }), empty('No current epoch.')]);
    return;
  }
  const epochId = ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const board = Array.isArray(ep.board) ? ep.board : [];

  // SCOPE TO THE VIEWED EPOCH: /api/lineage spans the WHOLE workspace (e0+e1+…),
  // so we MUST filter to generations whose epoch_id matches the epoch on screen —
  // otherwise a sibling epoch's generations leak in and the heatmap renders
  // duplicate `v0 v1 …` columns and an inflated "field of N". When the lineage
  // payload has no rows for this epoch (or carries no epoch_id at all) we fall
  // back to the already epoch-scoped `ep.experiments`. Dedupe by id regardless,
  // so a column id can never appear twice.
  const lineageRows = (lin && Array.isArray(lin.generations)) ? lin.generations : [];
  const scopedLineage = lineageRows.filter((g) => g && g.epoch_id === epochId);
  const rawGens = scopedLineage.length
    ? scopedLineage.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' }));
  const gens = [];
  const seenGen = new Set();
  for (const g of rawGens) {
    if (g.id == null || seenGen.has(g.id)) continue;
    seenGen.add(g.id);
    gens.push(g);
  }

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  const perEntries = await Promise.all(gens.map((g) => D.perEntry(epochId, g.id)));
  const lossLookup = new Map();
  const entryIds = new Set();
  gens.forEach((g, i) => {
    const pe = perEntries[i];
    if (pe && Array.isArray(pe.entries)) for (const r of pe.entries) {
      entryIds.add(r.entry_id);
      if (svg.isNum(r.drift_loss)) lossLookup.set(`${r.entry_id}|${g.id}`, r.drift_loss);
    }
  });
  for (const b of board) { const id = b.entry_id || b.id; if (id) entryIds.add(id); }

  // ---- the slim reel's rounds (champion spine + ticks) ----
  // The rounds are the actual tournament match-ups (round-ordered by ran_at);
  // fall back to the rejected/promoted challenger lineage when there is no
  // tournament payload. The champion is the promoted (or seed) generation.
  const champ = gens.find((g) => g.promoted) || gens.find((g) => !g.parent) || null;
  const championId = champ ? champ.id : null;
  const matchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups.slice() : [];
  matchups.sort((a, b) => String(a.ran_at || '').localeCompare(String(b.ran_at || '')));
  const rounds = matchups.length
    ? matchups.map((m) => ({ challenger: m.challenger, decision: m.decision, deltaScalar: m.delta_scalar }))
    : gens.filter((g) => g.parent).map((g) => ({
        challenger: g.id, decision: g.promoted ? 'promoted' : 'rejected',
        deltaScalar: (svg.isNum(scalarByGen.get(g.id)) && svg.isNum(championId ? scalarByGen.get(championId) : NaN))
          ? scalarByGen.get(g.id) - scalarByGen.get(championId) : null,
      }));
  const reelSpec = { championId, rounds };

  // The configured tournament structure (§3.1) — surfaced as a one-line
  // header pill. Absent ⇒ no pill (a gauntlet epoch that predates the
  // feature reads byte-identically — the block is simply omitted upstream).
  const tournament = (ep && ep.tournament && typeof ep.tournament === 'object') ? ep.tournament : null;
  // Is this a NON-gauntlet epoch? Racing/swiss/elim are NOT N sequential
  // champion-vs-challenger rounds, so the gauntlet "champion spine" reel does
  // not describe them — we swap in a structure-appropriate strip instead.
  const structure = (tournament && tournament.structure) || 'gauntlet';
  const nonGauntlet = isNonGauntlet(structure);

  // ---- RACING survival-funnel data (LIVE-FIRST, else reconstructed) ----
  // For a racing epoch the structure strip renders an interactive survival
  // FUNNEL (field → cuts → survivor → champion-gate). It needs the SAME
  // rung/gate model the Match-ups ladder uses: prefer the LIVE
  // /api/active-tournament topology while a run is in flight (the pending rung
  // stays neutral, the gate "deciding…"), else REUSE reconstructRacing() to
  // rebuild the completed ladder from the per-challenger /api/tournaments
  // records. Resolved to null (→ static summary) when there are no rungs yet.
  let racingFunnel = null;
  if (structure === 'racing') {
    const status = deriveLiveStatus({
      heartbeat: state.heartbeat, activeRuns: state.activeRuns, activeTournament: state.activeTournament,
    });
    // The LIVE topology belongs to the ACTIVE epoch — only adopt it when that
    // is the epoch ON SCREEN. Otherwise the active (e.g. e1) tournament would
    // render under a DIFFERENT epoch's (e.g. e0's) header. When it is not for
    // this epoch we fall through to the epoch-scoped reconstruction (which is
    // itself filtered to `epochId`), so each epoch only ever shows its own data.
    const liveRaw = status.running ? await D.activeTournament() : null;
    const liveForThisEpoch = (liveRaw && liveRaw.epoch_id != null)
      ? String(liveRaw.epoch_id) === String(epochId)
      : !!liveRaw; // no epoch tag ⇒ legacy single-epoch payload, trust it.
    const liveSt = liveForThisEpoch ? normalizeStructure(liveRaw, true) : null;
    const racingSt = (liveSt && liveSt.live) ? liveSt : reconstructRacing(bracket, epochId);
    const model = racingModel(racingSt);
    if (model && model.hasRungs) racingFunnel = { st: racingSt, model };
  }

  const digest = JSON.stringify({
    epochId, goal: ep.goal || '', briefLen: (ep.brief || '').length, closed: !!ep.closed,
    structure: tournament ? [tournament.structure, JSON.stringify(tournament.params || {})] : null,
    nonGauntlet,
    racingFunnel: racingFunnel ? structureDigest(racingFunnel.st) : null,
    gens: gens.map((g) => [g.id, g.parent, g.promoted, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
    reel: reelDigest(reelSpec),
    loss: [...lossLookup.entries()].sort(),
    board: board.map((b) => [b.entry_id || b.id, b.kind, b.weight, b.budget_s]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: `Epoch ${epochId}` }),
      el('div', { class: 'dn-objective' }, [
        el('div', { class: 'lab', text: 'objective' }),
        el('div', { class: 'txt', text: ep.goal && ep.goal.trim() ? ep.goal : '(no objective recorded)' }),
      ]),
      tournament ? el('div', { class: 'dt-structure-line' }, [
        structurePill(tournament.structure, tournament.params),
      ]) : null,
    ].filter(Boolean)));

    const promotedCount = gens.filter((g) => g.promoted).length;
    nodes.push(el('div', { class: 'dn-panel dn-row', style: 'margin-top:12px;' }, [
      stat(String(board.length), 'board entries'),
      stat(String(experiments.length), 'experiments'),
      stat(String(promotedCount), 'promoted'),
      stat(ep.closed ? 'closed' : 'open', 'state'),
    ]));

    nodes.push(el('div', { class: 'dn-quicklinks' }, [
      el('a', { class: 'dn-linkbtn', href: ctx.href('gens', { epochId }), text: 'Generations →' }),
      el('a', { class: 'dn-linkbtn', href: ctx.href('boards', { epochId }), text: 'Boards (trellis) →' }),
      el('a', { class: 'dn-linkbtn', href: ctx.href('mutations', { epochId }), text: 'Mutation surface + diff →' }),
      el('a', { class: 'dn-linkbtn', href: ctx.href('publication', { epochId }), text: 'Epoch publication (ACM) →' }),
    ]));

    const briefText = ep.brief || '';
    const briefDetails = el('details', { class: 'dn-brief', open: briefText.length < 1200 ? '' : null }, [
      el('summary', null, [
        el('span', { class: 'chev', text: '▸' }), 'Proposer brief',
        el('span', { class: 'dn-faint', style: 'font-weight:400;font-size:11px;', text: briefText ? `· ${briefText.split(/\n/).length} lines` : '· none' }),
      ]),
      renderMarkdown(briefText),
    ]);
    nodes.push(section('Operator’s brief to the proposer', briefDetails));

    // ---- the structure-aware reel ------------------------------------
    // GAUNTLET keeps the slim reel: rounds along the champion spine (replaces
    // bumps). Fit-to-width, no pan/zoom: the ticks compress as rounds grow.
    // Clicking a station → that round's candidate; the seed → the champion.
    // The per-challenger detail lives in the generations match cards.
    //
    // For a NON-gauntlet epoch the champion-spine reel is the WRONG story
    // (racing is successive halving, not N sequential title defences), so we
    // swap in a compact structure strip ("racing · field of N · M rungs") with
    // a "see Match-ups" affordance that opens the real ladder/bracket.
    if (nonGauntlet) {
      const fieldN = gens.length;
      const params = (tournament && tournament.params) || {};
      const rungs = Array.isArray(params.rungs) ? params.rungs.length : null;
      const facts = [el('span', { class: 'dt-struct-strip-lab', text: structureLabel(structure, params) })];
      facts.push(el('span', { class: 'dt-struct-strip-fact', text: `field of ${fieldN}` }));
      if (structure === 'racing' && rungs) facts.push(el('span', { class: 'dt-struct-strip-fact', text: `${rungs} rung${rungs === 1 ? '' : 's'}` }));
      else if (structure === 'swiss' && svg.isNum(params.rounds)) facts.push(el('span', { class: 'dt-struct-strip-fact', text: `${params.rounds} round${params.rounds === 1 ? '' : 's'}` }));

      if (structure === 'racing' && racingFunnel) {
        // ---- the interactive SURVIVAL FUNNEL (the epoch hero) ----
        // field → cuts → survivor → champion-gate, the flow narrowing at each
        // cut; eliminated competitors peel off as ✕ branches, survivors (↑)
        // ride the thickening flow toward the gate. LIVE: pending rung neutral
        // + gate "deciding…". Click a competitor → its candidate.
        const m = racingFunnel.model;
        // the card keeps the `dt-struct-strip` facts header (so it still reads
        // as the structure strip) and adds the funnel figure below it.
        const card = el('div', { class: 'dn-panel dn-figpane dt-funnel-card dt-struct-strip' });
        const head = el('div', { class: 'dt-struct-strip-row' }, [
          ...facts,
          m.live ? el('span', { class: 'dt-live-pill', text: 'LIVE' }) : null,
        ].filter(Boolean));
        card.appendChild(head);
        card.appendChild(svg.survivalFunnel({
          rungs: m.rungs, championId: m.championId, benchmarkId: m.benchmarkId, live: m.live,
          gateState: m.gateState, gateDelta: m.gateDelta,
          onCompetitor: (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); },
        }));
        const gateNote = m.gateState === 'crowned' ? ` · champion-gate: ${m.championId} promoted ♚`
          : m.gateState === 'stands' ? ' · champion-gate: champion stands'
          : m.gateState === 'deciding' ? ' · champion-gate: deciding…' : '';
        card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
          (m.benchmarkId ? `the field is raced vs the champion v0 = ${m.benchmarkId}; every Δ is Δ-vs-v0 and v0 defends at the gate · ` : '')
          + 'successive halving — each rung races the field on a growing board fraction, then cuts the worst by η · ✕ = cut · ↑ = survives · ♚ = crowned at the full-board gate · click a competitor → open'
          + gateNote
          + (m.live ? ' · LIVE — the eventual winner is not committed until the final gate' : '') }));
        card.appendChild(el('a', { class: 'dn-linkbtn dt-struct-strip-link', href: ctx.href('gens', { epochId }), text: 'See Match-ups →' }));
        nodes.push(section('Tournament structure · ' + structureLabel(structure, params), card));
      } else {
        // DEGRADE: no rung records yet (e.g. a racing epoch that has not run,
        // or a non-racing structure) → a tidy static "field of N" summary.
        const stripCard = el('div', { class: 'dn-panel dt-struct-strip' });
        stripCard.appendChild(el('div', { class: 'dt-struct-strip-row' }, facts));
        stripCard.appendChild(el('a', { class: 'dn-linkbtn dt-struct-strip-link', href: ctx.href('gens', { epochId }), text: 'See Match-ups →' }));
        stripCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
          structure === 'racing'
            ? 'no rungs have raced yet — the survival funnel fills in once the field runs · the full ladder / standings live in Match-ups'
            : 'this epoch is not a gauntlet — the champion-spine reel does not describe it · the full ladder / bracket / standings live in Match-ups' }));
        nodes.push(section('Tournament structure · ' + structureLabel(structure, params), stripCard));
      }
    } else {
      const reelCard = el('div', { class: 'dn-panel' });
      reelCard.appendChild(reel({
        championId, rounds,
        selected: null,
        onSelect: (id) => ctx.navigate('candidate', { epochId, gen: id }),
        onSeed: (id) => { if (id) ctx.navigate('candidate', { epochId, gen: id }); },
      }));
      nodes.push(section('Reel · the rounds along the champion spine', reelCard));
    }

    // ---- COMPACT board entries × generations heatmap (stays here, fix #6) ----
    const rows = [...entryIds].sort().map((id) => ({ id, label: id }));
    const cols = gens.map((g) => ({ id: g.id, label: g.id }));
    // FIT-TO-WIDTH: the heatmap is a responsive SVG (width:100% + viewBox), so
    // it scales to the pane — NO overflow-x wrapper. Density scales cell size.
    const hmt = densityTokens();
    const hmCard = el('div', { class: 'dn-panel dn-figpane' });
    if (rows.length && cols.length) {
      hmCard.appendChild(svg.heatmap({
        rows, cols, cellW: Math.round(hmt.heatCell * 1.6), cellH: hmt.heatCell,
        value: (r, c) => (lossLookup.has(`${r}|${c}`) ? lossLookup.get(`${r}|${c}`) : null),
        // fix #7: a cell routes to the PER-BOARD cross-candidate view, keyed by
        // the entry id (the row) — NOT to an arbitrary candidate.
        onClick: (rId) => ctx.navigate('board', { epochId, entry: rId }),
      }));
      hmCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'cell = drift loss for one board entry in one generation · denser ink = more drift · click a row → that board across every candidate · the small-multiples trellis lives in Boards' }));
    } else {
      hmCard.appendChild(empty('No per-entry loss profiles yet (the index may not be built).'));
    }
    nodes.push(section('Board entries × generations · drift loss (heatmap)', hmCard));
    return nodes;
  });
}

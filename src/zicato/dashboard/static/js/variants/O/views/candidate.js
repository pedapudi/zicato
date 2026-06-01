// variants/O/views/candidate.js — the DETAIL PANE for a selected generation.
//
// Tabs (facets) within the right pane switch what we show for the selected
// candidate:
//   * lifecycle    — the candidate's life: per-board dot-plot + the Tufte
//                    sankey (FIX 5: label ≠ value) + the promote gate
//                    (FIX 1: clean stacked sections).
//   * matchups     — the gauntlet ladder + the paired per-board duel +
//                    the gate for this round.
//   * mutations    — ONE cohesive visual (FIX 2): the site × generation
//                    matrix + a SIDE-BY-SIDE baseline|new diff that fills
//                    on cell-select, using REAL strings (no [object Object]).
//   * publication  — K's ACM paper renderer (FIX 3: GFM tables; combined
//                    aggregate-scores table+chart; per-matchup detail).
//   * run          — handled by views/run.js (deep drill).

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import {
  gatedSwap, section, empty, loading, stat, verdictPill, normaliseDecision,
  gatePanel, sideBySideDiff, figureFrame, heatRamp,
} from '../ui.js';
import { FACETS } from '../router.js';
import { loadRailModel } from '../model.js';
import { parsePaper, renderPaper } from '../paper.js';

const FACET_LABELS = {
  lifecycle: 'Lifecycle', matchups: 'Match-ups', mutations: 'Mutations',
  publication: 'Publication', run: 'Run',
};

export async function render(host, ctx, route) {
  const genId = route.gen;
  const facet = FACETS.includes(route.facet) ? route.facet : 'lifecycle';
  if (!host.firstChild) host.appendChild(loading('Loading candidate…'));
  if (!genId) { gatedSwap(host, 'no-gen', () => [empty('No generation selected.')]); return; }

  const m = await loadRailModel();
  const { epochId, gens, rawGens, ordered, tours } = m;
  const genMeta = gens.find((g) => g.id === genId) || { id: genId, promoted: false };

  // Tab bar is always present; the body is per-facet.
  let bodyData = null;
  if (facet === 'lifecycle') bodyData = await lifecycleData(epochId, genId, genMeta, m, ctx);
  else if (facet === 'matchups') bodyData = await matchupsData(epochId, genId, m, ctx);
  else if (facet === 'mutations') bodyData = await mutationsData(epochId, genId, m, route, ctx);
  else if (facet === 'publication') bodyData = await publicationData(epochId, m, ctx);
  else if (facet === 'run') bodyData = { digest: 'run-redirect', build: () => [empty('Open a board entry’s run from Lifecycle or Match-ups.')] };

  const digest = JSON.stringify({
    genId, facet, epochId,
    promoted: genMeta.promoted,
    facetDigest: bodyData ? bodyData.digest : null,
  });

  gatedSwap(host, digest, () => {
    const out = [];
    out.push(el('div', { class: 'vo-pagehead' }, [
      el('div', { class: 'vo-pagehead-row' }, [
        el('h1', { class: 'vo-h1 vo-mono', text: genId }),
        verdictPill(genMeta.promoted ? 'promoted' : 'rejected'),
        genMeta.parent ? el('span', { class: 'vo-faint', text: 'child of ' + genMeta.parent }) : el('span', { class: 'vo-faint', text: 'seed' }),
      ]),
    ]));
    // facet tab bar.
    const tabs = el('nav', { class: 'vo-tabs', role: 'tablist' });
    for (const f of FACETS) {
      if (f === 'run') continue; // run is a drill, not a primary tab
      const t = el('button', {
        class: 'vo-tab' + (f === facet ? ' vo-tab-active' : ''),
        type: 'button', role: 'tab', 'data-facet': f, text: FACET_LABELS[f],
        'aria-selected': f === facet ? 'true' : 'false',
      });
      t.addEventListener('click', () => ctx.navigate('gen', { gen: genId, facet: f }));
      tabs.appendChild(t);
    }
    out.push(tabs);
    const body = el('div', { class: 'vo-facet vo-facet-' + facet });
    const built = bodyData ? bodyData.build() : [empty('Nothing here yet.')];
    for (const n of built) if (n) body.appendChild(n);
    out.push(body);
    return out;
  });
}

// ---- LIFECYCLE ------------------------------------------------------

async function lifecycleData(epochId, genId, genMeta, m, ctx) {
  const pe = await D.perEntry(epochId, genId);
  const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];
  // The reigning champion's per-entry as a reference line on the dot-plot.
  const champ = m.gens.find((g) => g.promoted);
  const champPe = (champ && champ.id !== genId) ? await D.perEntry(epochId, champ.id) : null;
  const champMap = entryMap(champPe);
  const champTotal = entries.reduce((a, e) => a + (svg.isNum(champMap.get(e.entry_id)) ? champMap.get(e.entry_id) : 0), 0);
  // The gate for this round (if it was a challenger).
  const gate = (genMeta.parent && champ) ? await D.gate(epochId, genMeta.parent, genId) : null;

  const items = entries.map((e) => ({
    id: e.entry_id, label: e.entry_id, value: e.drift_loss,
    pass: e.pass_fail, timeout: !!e.wall_clock_budget_exceeded,
  })).sort((a, b) => (svg.isNum(b.value) ? b.value : -1) - (svg.isNum(a.value) ? a.value : -1));
  const aggLoss = entries.reduce((a, e) => a + (svg.isNum(e.drift_loss) ? e.drift_loss : 0), 0);
  const boards = entries.filter((e) => svg.isNum(e.drift_loss)).map((e) => ({
    id: e.entry_id, label: e.entry_id, value: e.drift_loss, ref: e.entry_id,
    cls: e.wall_clock_budget_exceeded ? 'vo-bad' : (e.pass_fail === 1 ? 'vo-good' : ''),
  }));
  const champRef = champMap.size ? { label: champ.id, value: champTotal / Math.max(1, entries.length) } : null;

  const digest = JSON.stringify({
    genId, entries: entries.map((e) => [e.entry_id, fx(e.drift_loss), e.pass_fail, !!e.wall_clock_budget_exceeded]),
    gate: gate && Array.isArray(gate.rules) ? gate.rules.map((r) => [r.id, r.status, r.fired]) : null,
    champ: champ ? champ.id : null,
  });

  return {
    digest,
    build: () => {
      const out = [];
      // Per-board scoring — open the per-board cross-candidate view (by id).
      out.push(section('Per-board scoring · sorted worst-first',
        figureFrame({
          mark: svg.valueDotPlot({
            width: 560, rowHeight: 22, labelWidth: 200, items,
            reference: champRef ? { label: champRef.label + ' (avg)', value: champRef.value } : null,
            onClick: (it) => ctx.navigate('board', { entry: it.id }),
          }),
          caption: 'Absolute drift loss per board entry (lower = better). Click a row to open that board’s cross-candidate view.',
        })));
      // Causal flow — the Tufte sankey (label ≠ value).
      out.push(section('Causal flow · candidate → per-board loss → scalar',
        figureFrame({
          mark: svg.sankey({
            width: 760,
            candidate: { label: genId, sub: 'patch on mutation sites' },
            boards,
            aggregate: { label: 'scalar', sub: svg.fmt(aggLoss, 1) + ' loss' },
            onBoard: (entryId) => ctx.navigate('board', { entry: entryId }),
          }),
          caption: 'Per-board loss summing to the aggregate scalar. Click a board to open its cross-candidate view.',
        })));
      // The gate — clean stacked sections.
      if (gate) {
        out.push(section('The promote gate', gatePanel(gate, genMeta.parent, genId, { fmt: svg.fmt, fmtSigned: svg.fmtSigned })));
      } else {
        out.push(section('The promote gate', empty('This is the seed (or no gate decision was recorded).')));
      }
      if (!entries.length) out.push(empty('No per-board scores recorded for this candidate.'));
      return out;
    },
  };
}

// ---- MATCH-UPS ------------------------------------------------------

async function matchupsData(epochId, genId, m, ctx) {
  const tours = m.tours;
  const matchups = (tours && Array.isArray(tours.matchups)) ? tours.matchups : [];
  // The round(s) this generation is the challenger in.
  const mine = matchups.filter((mu) => mu.challenger === genId);
  const focus = mine[0] || matchups[0] || null;
  const grid = (focus && focus.champion && focus.challenger)
    ? await D.matchupGrid(epochId, focus.champion, focus.challenger) : null;
  const gate = (focus && focus.champion && focus.challenger)
    ? await D.gate(epochId, focus.champion, focus.challenger) : null;
  const nodes = m.gens.map((g) => ({ id: g.id, x: g.x, promoted: g.promoted, parent: g.parent, scalar: g.scalar }));
  const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];

  const digest = JSON.stringify({
    genId, focus: focus ? [focus.champion, focus.challenger, focus.decision] : null,
    rows: rows.map((r) => [r.entry_id, fx(r.parent_drift_loss), fx(r.child_drift_loss), r.verdict]),
    gate: gate && Array.isArray(gate.rules) ? gate.rules.map((r) => [r.id, r.status]) : null,
    nodes: nodes.map((n) => [n.id, n.x, n.promoted]),
  });

  return {
    digest,
    build: () => {
      const out = [];
      out.push(section('The gauntlet ladder',
        figureFrame({
          mark: svg.bumps({ width: 720, height: 190, nodes, selected: genId,
            onClick: (n) => ctx.navigate('gen', { gen: n.id, facet: 'matchups' }) }),
          caption: 'King-of-the-hill — every challenger paired against the reigning champion.',
        })));
      if (!focus) { out.push(empty('This generation did not run as a challenger.')); return out; }
      const series = rows.map((r) => ({ label: r.entry_id, id: r.entry_id, a: r.parent_drift_loss, b: r.child_drift_loss, verdict: r.verdict }));
      out.push(section(`Paired duel · ${focus.champion} → ${focus.challenger}`,
        el('div', { class: 'vo-panel' }, [
          el('div', { class: 'vo-round-head' }, [
            el('span', { class: 'vo-mono', text: `${focus.champion} → ${focus.challenger}` }),
            verdictPill(normaliseDecision(focus) || focus.decision),
            svg.isNum(focus.delta_scalar) ? el('span', { class: 'vo-faint', text: `Δ ${svg.fmtSigned(focus.delta_scalar, 2)} scalar` }) : null,
          ].filter(Boolean)),
          svg.pairedSlopegraph({
            width: 580, height: 300, series,
            left: { title: focus.champion }, right: { title: focus.challenger },
            onClick: (s) => ctx.navigate('run', { gen: focus.challenger, entry: s.id }),
          }),
          el('p', { class: 'vo-faint vo-fignote', text: 'slope down = the challenger improved · click a line to open its run' }),
        ])));
      if (gate) out.push(section('The promote gate', gatePanel(gate, focus.champion, focus.challenger, { fmt: svg.fmt, fmtSigned: svg.fmtSigned })));
      return out;
    },
  };
}

// ---- MUTATIONS (FIX 2) ----------------------------------------------

async function mutationsData(epochId, genId, m, route, ctx) {
  const muts = await D.mutations(epochId);
  const mutations = (muts && Array.isArray(muts.mutations)) ? muts.mutations : [];
  let genIds = (muts && Array.isArray(muts.generations) && muts.generations.length) ? muts.generations.slice() : [];
  if (!genIds.length) genIds = m.gens.map((g) => g.id);
  const promotedSet = new Set(m.gens.filter((g) => g.promoted).map((g) => g.id));

  // This generation's patches → the challenger `.new_content` per site.
  const pp = await D.patches(epochId, genId);
  const patches = (pp && Array.isArray(pp.patches)) ? pp.patches : [];
  const patchBySite = new Map(patches.map((p) => [p.mutation_id, p]));

  // The selected site within this facet — taken from the route's `entry`
  // slot (reused as the site id) or defaulting to the first patched site.
  const patchedSites = mutations.filter((mt) => (mt.patched_generation_ids || []).includes(genId));
  let selSite = route.entry || (patchedSites[0] && patchedSites[0].mutation_id) || null;
  const selMut = mutations.find((mt) => mt.mutation_id === selSite) || null;

  // Resolve REAL strings for the side-by-side diff. baseline = the mutation
  // detail's `.baseline.content` STRING; challenger = the patch's
  // `.new_content` STRING. (Rendering `.baseline` itself was the bug.)
  let baselineStr = null; let challengerStr = null; let rationale = null; let op = null;
  if (selSite) {
    const detail = await D.mutationDetail(epochId, selSite);
    baselineStr = (detail && detail.baseline && typeof detail.baseline.content === 'string')
      ? detail.baseline.content : (typeof (detail && detail.baseline) === 'string' ? detail.baseline : null);
    const patch = patchBySite.get(selSite);
    if (patch) { challengerStr = typeof patch.new_content === 'string' ? patch.new_content : null; rationale = patch.rationale || null; op = patch.op || null; }
  }

  const sites = mutations.map((mt) => ({
    id: mt.mutation_id,
    label: mt.role ? String(mt.role) : (mt.file || mt.mutation_id),
    sub: siteSub(mt),
    patched: new Set(mt.patched_generation_ids || []),
  }));

  const digest = JSON.stringify({
    genId, selSite,
    sites: sites.map((s) => [s.id, s.label, [...s.patched]]),
    gens: genIds,
    baseLen: baselineStr == null ? null : baselineStr.length,
    newLen: challengerStr == null ? null : challengerStr.length,
  });

  return {
    digest,
    build: () => {
      if (!sites.length) return [section('Mutation surface', empty('No mutation surface recorded for this epoch.'))];
      const out = [];
      const gens = genIds.map((id) => ({ id, label: id, promoted: promotedSet.has(id) }));
      const patchedLookup = new Map();
      for (const s of sites) for (const g of s.patched) patchedLookup.set(s.id + ' ' + g, true);

      // ONE cohesive visual: the matrix + the diff that fills on select.
      const combined = el('div', { class: 'vo-mut-combined' });
      const matrixWrap = el('div', { class: 'vo-mut-matrix' }, [
        svg.mutationMatrix({
          sites, gens,
          selected: selSite ? { site: selSite, gen: genId } : null,
          patched: (siteId, gId) => patchedLookup.has(siteId + ' ' + gId),
          // Selecting a cell selects that site (on the generation that
          // patched it) — stays on the mutations facet; the site rides in
          // the `entry` slot so the side-by-side diff fills below.
          onCell: (gId, siteId) => ctx.navigate('gen', { gen: gId, facet: 'mutations', entry: siteId }),
        }),
        el('p', { class: 'vo-faint vo-fignote', text: 'a filled cell = that generation patched that site · click to diff it' }),
      ]);
      combined.appendChild(matrixWrap);

      const diffWrap = el('div', { class: 'vo-mut-diff' });
      if (!selSite) {
        diffWrap.appendChild(empty('Select a patched site in the matrix to see its side-by-side diff.'));
      } else {
        diffWrap.appendChild(el('div', { class: 'vo-mut-diffhead' }, [
          el('span', { class: 'vo-mono vo-mut-site', text: (selMut && (selMut.role || selMut.file)) || selSite }),
          op ? el('span', { class: 'vo-diff-op vo-op-' + op, text: op }) : null,
        ].filter(Boolean)));
        if (rationale) diffWrap.appendChild(el('p', { class: 'vo-soft', text: rationale }));
        if (baselineStr == null && challengerStr == null) {
          diffWrap.appendChild(empty('No baseline or patch content recorded for this site.'));
        } else {
          diffWrap.appendChild(sideBySideDiff(baselineStr || '', challengerStr || ''));
        }
      }
      combined.appendChild(diffWrap);

      out.push(section('Mutation surface · site × generation', combined));
      return out;
    },
  };
}

// ---- PUBLICATION (FIX 3) -------------------------------------------

async function publicationData(epochId, m, ctx) {
  const [analysis] = await Promise.all([D.analysis(epochId)]);
  const md = analysis && typeof analysis.analysis_md === 'string' ? analysis.analysis_md : '';
  const broken = analysis === null;
  const missing = !broken && !md.trim();
  const paper = parsePaper(md);

  const ordered = m.ordered;
  const tours = m.tours;
  const scalarById = m.scalarById;
  const perEntries = await Promise.all(ordered.map((g) => D.perEntry(epochId, g.generation_id)));
  const peByGen = new Map(ordered.map((g, i) => [g.generation_id, perEntries[i]]));
  const nav = ctx.navigate;
  const root = (typeof document !== 'undefined' && document.getElementById) ? document.getElementById('variant-root') : null;
  const theme = root ? root.getAttribute('data-vo-theme') : null;
  const ramp = heatRamp(root, theme);

  const figures = {
    lineage: (n) => figLineage(n, ordered, scalarById, nav),
    'combined-scores': (n) => figCombinedScores(n, ordered, peByGen, scalarById, nav),
    'aggregate-scores': (n) => figCombinedScores(n, ordered, peByGen, scalarById, nav),
    'per-board-heatmap': (n) => figHeatmap(n, ordered, peByGen, ramp, nav),
    'score-trajectory': (n) => figSankey(n, ordered, peByGen, nav),
    'hypothesis-vs-outcome': (n) => figMatchup(n, epochId, tours, peByGen, nav),
    'matchup-detail': (n) => figMatchup(n, epochId, tours, peByGen, nav),
  };
  const canonical = [
    (n) => figLineage(n, ordered, scalarById, nav),
    (n) => figCombinedScores(n, ordered, peByGen, scalarById, nav),
    (n) => figSankey(n, ordered, peByGen, nav),
    (n) => figMatchup(n, epochId, tours, peByGen, nav),
    (n) => figHeatmap(n, ordered, peByGen, ramp, nav),
  ];

  const digest = JSON.stringify({
    epochId, broken, missing, title: paper.title, eyebrow: paper.eyebrow,
    meta: paper.meta.map((mm) => [mm.label, mm.value]), bodyLen: paper.body.length,
    gens: ordered.map((g) => [g.generation_id, g.promoted]),
  });

  return {
    digest,
    build: () => [renderPaper({ epochId, paper, figures, canonicalFigures: canonical, broken, missing })],
  };
}

// ---- publication figure builders -----------------------------------

function figLineage(n, ordered, scalarById, nav) {
  const nodes = ordered.map((g) => ({
    id: g.generation_id, x: depth(g, ordered), promoted: !!g.promoted,
    parent: g.parent_generation_id || null, scalar: scalarById.get(g.generation_id),
  }));
  return figureFrame({
    number: n, mark: svg.bumps({ width: 720, height: 200, nodes, onClick: (node) => nav('gen', { gen: node.id, facet: 'lifecycle' }) }),
    caption: 'Lineage as ranked lanes — the champion spine, challengers branching off. ',
    openLabel: 'select seed →', onOpen: () => nav('gen', { gen: (ordered[0] && ordered[0].generation_id) || null, facet: 'lifecycle' }),
  });
}

// FIX 3: the aggregate-generation-scores TABLE and its summary BAR CHART
// combined into ONE cohesive visual — a single figure pairing the bars
// (one per generation, ∝ aggregate loss) with the numeric table beneath.
function figCombinedScores(n, ordered, peByGen, scalarById, nav) {
  const rows = ordered.map((g) => {
    const pe = peByGen.get(g.generation_id);
    const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];
    const loss = entries.reduce((a, e) => a + (svg.isNum(e.drift_loss) ? e.drift_loss : 0), 0);
    const passed = entries.filter((e) => e.pass_fail === 1).length;
    const ran = entries.length;
    return { id: g.generation_id, promoted: !!g.promoted, loss, passed, ran, scalar: scalarById.get(g.generation_id) };
  });
  const items = rows.map((r) => ({ id: r.id, label: r.id, value: r.loss, promoted: r.promoted }))
    .sort((a, b) => a.value - b.value);
  const champLoss = rows.find((r) => r.promoted);
  const mark = el('div', { class: 'vo-combined-scores' }, [
    svg.sortedBars({
      width: 520, rowHeight: 24, labelWidth: 80, items,
      reference: champLoss ? { label: 'champion', value: champLoss.loss } : null,
      onClick: (it) => nav('gen', { gen: it.id, facet: 'lifecycle' }),
    }),
    buildScoresTable(rows),
  ]);
  return figureFrame({
    number: n, mark,
    caption: 'Aggregate generation scores — the summary bars and the per-generation table as ONE visual (loss = Σ per-board drift; lower is better). ',
    openLabel: 'select champion →', onOpen: () => nav('gen', { gen: (champLoss && champLoss.id) || (ordered[0] && ordered[0].generation_id), facet: 'lifecycle' }),
  });
}

function buildScoresTable(rows) {
  const t = el('table', { class: 'vo-md-table vo-scores-table' });
  t.appendChild(el('thead', null, [el('tr', null, [
    el('th', { text: 'generation' }), el('th', { class: 'vo-num', text: 'aggregate loss' }),
    el('th', { class: 'vo-num', text: 'passed' }), el('th', { text: 'verdict' }),
  ])]));
  const tbody = el('tbody');
  for (const r of rows) {
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'vo-mono', text: r.id }),
      el('td', { class: 'vo-num', text: svg.fmt(r.loss, 1) }),
      el('td', { class: 'vo-num', text: `${r.passed}/${r.ran}` }),
      el('td', null, [verdictPill(r.promoted ? 'promoted' : 'rejected')]),
    ]));
  }
  t.appendChild(tbody);
  return t;
}

function figSankey(n, ordered, peByGen, nav) {
  const champ = ordered.find((g) => g.promoted) || ordered[0];
  const pe = champ ? peByGen.get(champ.generation_id) : null;
  const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];
  const boards = entries.filter((e) => svg.isNum(e.drift_loss)).map((e) => ({
    id: e.entry_id, label: e.entry_id, value: e.drift_loss, ref: e.entry_id,
    cls: e.wall_clock_budget_exceeded ? 'vo-bad' : (e.pass_fail === 1 ? 'vo-good' : ''),
  }));
  const agg = boards.reduce((a, b) => a + b.value, 0);
  return figureFrame({
    number: n, mark: svg.sankey({
      width: 740, candidate: { label: champ ? champ.generation_id : 'candidate', sub: 'patch on mutation sites' },
      boards, aggregate: { label: 'scalar', sub: svg.fmt(agg, 1) + ' loss' },
      onBoard: (entryId) => nav('board', { entry: entryId }),
    }),
    caption: `Causal flow for ${champ ? champ.generation_id : 'the champion'} — per-board loss summing to the scalar. `,
  });
}

function figMatchup(n, epochId, tours, peByGen, nav) {
  const matchups = (tours && Array.isArray(tours.matchups)) ? tours.matchups : [];
  const mu = matchups[0];
  let series = []; let champion = null; let challenger = null;
  if (mu) {
    champion = mu.champion; challenger = mu.challenger;
    const cMap = entryMap(peByGen.get(mu.champion)); const xMap = entryMap(peByGen.get(mu.challenger));
    const ids = new Set([...cMap.keys(), ...xMap.keys()]);
    series = [...ids].map((id) => ({ label: id, id, a: cMap.get(id), b: xMap.get(id) }))
      .filter((s) => svg.isNum(s.a) || svg.isNum(s.b));
  }
  return figureFrame({
    number: n, mark: svg.pairedSlopegraph({
      width: 580, height: 320, series, left: { title: champion || 'champion' }, right: { title: challenger || 'challenger' },
      onClick: (s) => nav('run', { gen: challenger, entry: s.id }),
    }),
    caption: `Per-matchup detail — ${champion || 'champion'} vs ${challenger || 'challenger'}, paired per board entry. Slope down = improved. `,
    openLabel: 'open match-up →', onOpen: () => nav('gen', { gen: challenger, facet: 'matchups' }),
  });
}

function figHeatmap(n, ordered, peByGen, ramp, nav) {
  const cols = ordered.map((g) => ({ id: g.generation_id, label: g.generation_id }));
  const entryIds = []; const seen = new Set();
  for (const g of ordered) {
    const pe = peByGen.get(g.generation_id);
    if (pe && Array.isArray(pe.entries)) for (const e of pe.entries) if (!seen.has(e.entry_id)) { seen.add(e.entry_id); entryIds.push(e.entry_id); }
  }
  const rows = entryIds.map((id) => ({ id, label: id }));
  const lookup = new Map();
  for (const g of ordered) {
    const pe = peByGen.get(g.generation_id);
    if (pe && Array.isArray(pe.entries)) for (const e of pe.entries) lookup.set(g.generation_id + ' ' + e.entry_id, e.drift_loss);
  }
  return figureFrame({
    number: n, mark: svg.heatmap({
      rows, cols, cellW: 44, cellH: 20, labelWidth: 180, ramp,
      value: (rowId, colId) => { const v = lookup.get(colId + ' ' + rowId); return svg.isNum(v) ? v : null; },
      onClick: (rowId) => nav('board', { entry: rowId }),
    }),
    caption: 'Board entry × generation drift loss — the ramp follows the active theme. Click a cell to open that board. ',
  });
}

// ---- shared helpers ------------------------------------------------

function entryMap(pe) {
  const m = new Map();
  if (pe && Array.isArray(pe.entries)) for (const e of pe.entries) m.set(e.entry_id, svg.isNum(e.drift_loss) ? e.drift_loss : null);
  return m;
}
function depth(g, ordered) {
  const byId = new Map(ordered.map((n) => [n.generation_id, n]));
  let d = 0; let cur = g;
  while (cur && cur.parent_generation_id && byId.has(cur.parent_generation_id)) { d += 1; cur = byId.get(cur.parent_generation_id); }
  return d;
}
function siteSub(mt) {
  const file = mt.file || '';
  const span = svg.isNum(mt.line_start) ? `:${mt.line_start}${svg.isNum(mt.line_end) && mt.line_end !== mt.line_start ? '-' + mt.line_end : ''}` : '';
  const k = mt.kind ? ` (${mt.kind})` : '';
  return (file + span + k).trim();
}
function fx(v) { return svg.isNum(v) ? Number(v).toFixed(3) : null; }

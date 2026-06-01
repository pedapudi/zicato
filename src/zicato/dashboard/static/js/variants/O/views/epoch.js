// variants/O/views/epoch.js — the DETAIL PANE for a selected EPOCH.
//
// Epoch SCOPE. The publication (the ACM paper) and the mutation surface
// (the site × generation matrix) are inherently EPOCH-WIDE — they were
// wrongly shown per-generation in the first cut; they live HERE now. A
// facet tab bar switches:
//   * overview     — the epoch's lineage bumps + a per-generation drift
//                    heatmap + a match-up summary for the epoch.
//   * publication  — the ACM paper (K's renderer, paper.js), parsed from
//                    /api/epoch/{epochId}/analysis, with live figures.
//   * mutations    — ONE cohesive visual: the epoch-wide site × generation
//                    matrix + a SIDE-BY-SIDE baseline|new diff that fills on
//                    cell-select, using REAL strings (no [object Object]).

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import {
  gatedSwap, section, empty, loading, stat, verdictPill, normaliseDecision,
  sideBySideDiff, figureFrame, heatRamp,
} from '../ui.js';
import { EPOCH_FACETS } from '../router.js';
import { loadRailModel } from '../model.js';
import { parsePaper, renderPaper } from '../paper.js';

const FACET_LABELS = { overview: 'Overview', publication: 'Publication', mutations: 'Mutation surface' };

export async function render(host, ctx, route) {
  const epochId = route.epoch;
  const facet = EPOCH_FACETS.includes(route.facet) ? route.facet : 'overview';
  if (!host.firstChild) host.appendChild(loading('Loading epoch…'));
  if (!epochId) { gatedSwap(host, 'no-epoch', () => [empty('No epoch selected.')]); return; }

  const m = await loadRailModel(epochId);

  let bodyData = null;
  if (facet === 'overview') bodyData = await overviewData(epochId, m, ctx);
  else if (facet === 'publication') bodyData = await publicationData(epochId, m, ctx);
  else if (facet === 'mutations') bodyData = await mutationsData(epochId, m, route, ctx);

  const digest = JSON.stringify({
    epochId, facet,
    gens: m.gens.map((g) => [g.id, g.promoted]),
    facetDigest: bodyData ? bodyData.digest : null,
  });

  gatedSwap(host, digest, () => {
    const out = [];
    out.push(el('div', { class: 'vo-pagehead' }, [
      el('div', { class: 'vo-pagehead-row' }, [
        el('span', { class: 'vo-eyebrow', text: 'EPOCH' }),
        el('h1', { class: 'vo-h1 vo-mono', text: epochId }),
        el('span', { class: 'vo-faint', text: `${m.gens.length} generations · ${m.gens.filter((g) => g.promoted).length} promoted` }),
      ]),
    ]));
    const tabs = el('nav', { class: 'vo-tabs', role: 'tablist' });
    for (const f of EPOCH_FACETS) {
      const t = el('button', {
        class: 'vo-tab' + (f === facet ? ' vo-tab-active' : ''),
        type: 'button', role: 'tab', 'data-facet': f, text: FACET_LABELS[f],
        'aria-selected': f === facet ? 'true' : 'false',
      });
      t.addEventListener('click', () => ctx.navigate('epoch', { epoch: epochId, facet: f }));
      tabs.appendChild(t);
    }
    out.push(tabs);
    const body = el('div', { class: 'vo-facet vo-facet-epoch-' + facet });
    const built = bodyData ? bodyData.build() : [empty('Nothing here yet.')];
    for (const n of built) if (n) body.appendChild(n);
    out.push(body);
    return out;
  });
}

// ---- EPOCH OVERVIEW -------------------------------------------------

async function overviewData(epochId, m, ctx) {
  const ordered = m.ordered;
  const tours = m.tours;
  const scalarById = m.scalarById;
  const matchups = (tours && Array.isArray(tours.matchups)) ? tours.matchups : [];
  const perEntries = await Promise.all(ordered.map((g) => D.perEntry(epochId, g.generation_id)));
  const peByGen = new Map(ordered.map((g, i) => [g.generation_id, perEntries[i]]));
  const root = (typeof document !== 'undefined' && document.getElementById) ? document.getElementById('variant-root') : null;
  const ramp = heatRamp(root, root ? root.getAttribute('data-vo-theme') : null);

  const digest = JSON.stringify({
    epochId, gens: ordered.map((g) => [g.generation_id, g.promoted]),
    matchups: matchups.map((mu) => [mu.champion, mu.challenger, mu.decision]),
  });

  return {
    digest,
    build: () => {
      const out = [];
      out.push(section('Lineage', figureFrame({
        mark: svg.bumps({
          width: 720, height: 200,
          nodes: ordered.map((g) => ({ id: g.generation_id, x: depth(g, ordered), promoted: !!g.promoted,
            parent: g.parent_generation_id || null, scalar: scalarById.get(g.generation_id) })),
          onClick: (n) => ctx.navigate('gen', { gen: n.id, facet: 'lifecycle' }),
        }),
        caption: 'The champion spine with challengers branching off. Click a node to select that generation.',
      })));

      out.push(section('Per-board drift loss · board entry × generation',
        figureFrame({ mark: figHeatmapMark(ordered, peByGen, ramp, ctx.navigate),
          caption: 'Board entry × generation drift loss — the ramp follows the active theme. Click a cell to open that board.' })));

      // Match-up summary for the epoch.
      if (matchups.length) {
        const list = el('ul', { class: 'vo-runlist' });
        for (const mu of matchups) {
          const li = el('li', { class: 'vo-runlist-item', tabindex: '0', role: 'button', 'data-gen': mu.challenger }, [
            el('span', { class: 'vo-mono', text: `${mu.champion} → ${mu.challenger}` }),
            verdictPill(normaliseDecision(mu) || mu.decision),
            el('span', { class: 'vo-runlist-loss', text: svg.isNum(mu.delta_scalar) ? svg.fmtSigned(mu.delta_scalar, 2) : '—' }),
            el('span', { class: 'vo-runlist-open', text: 'open match-up →' }),
          ]);
          const go = () => ctx.navigate('gen', { gen: mu.challenger, facet: 'matchups' });
          li.addEventListener('click', go);
          li.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(); } });
          list.appendChild(li);
        }
        out.push(section('Match-ups · the gauntlet this epoch', list));
      } else {
        out.push(section('Match-ups · the gauntlet this epoch', empty('No tournament rounds recorded for this epoch.')));
      }
      if (!ordered.length) out.push(empty('No generations recorded for this epoch yet.'));
      return out;
    },
  };
}

// ---- PUBLICATION (epoch-scoped) -------------------------------------

async function publicationData(epochId, m, ctx) {
  const analysis = await D.analysis(epochId);
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
  const ramp = heatRamp(root, root ? root.getAttribute('data-vo-theme') : null);

  const figures = {
    lineage: (n) => figLineage(n, ordered, scalarById, nav),
    'combined-scores': (n) => figCombinedScores(n, ordered, peByGen, scalarById, nav),
    'aggregate-scores': (n) => figCombinedScores(n, ordered, peByGen, scalarById, nav),
    'per-board-heatmap': (n) => figureFrame({ number: n, mark: figHeatmapMark(ordered, peByGen, ramp, nav),
      caption: 'Board entry × generation drift loss — the ramp follows the active theme. Click a cell to open that board. ' }),
    'score-trajectory': (n) => figSankey(n, ordered, peByGen, nav),
    'hypothesis-vs-outcome': (n) => figMatchup(n, epochId, tours, peByGen, nav),
    'matchup-detail': (n) => figMatchup(n, epochId, tours, peByGen, nav),
  };
  const canonical = [
    (n) => figLineage(n, ordered, scalarById, nav),
    (n) => figCombinedScores(n, ordered, peByGen, scalarById, nav),
    (n) => figSankey(n, ordered, peByGen, nav),
    (n) => figMatchup(n, epochId, tours, peByGen, nav),
    (n) => figureFrame({ number: n, mark: figHeatmapMark(ordered, peByGen, ramp, nav),
      caption: 'Board entry × generation drift loss — the ramp follows the active theme. ' }),
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

// ---- MUTATION SURFACE (epoch-wide) ----------------------------------

async function mutationsData(epochId, m, route, ctx) {
  const muts = await D.mutations(epochId);
  const mutations = (muts && Array.isArray(muts.mutations)) ? muts.mutations : [];
  let genIds = (muts && Array.isArray(muts.generations) && muts.generations.length) ? muts.generations.slice() : [];
  if (!genIds.length) genIds = m.gens.map((g) => g.id);
  const promotedSet = new Set(m.gens.filter((g) => g.promoted).map((g) => g.id));

  // The selected site (route.entry) + the patched-by generation (route.gen,
  // defaulting to the first generation that patched the site).
  let selSite = route.entry || (mutations[0] && mutations[0].mutation_id) || null;
  const selMut = mutations.find((mt) => mt.mutation_id === selSite) || null;
  const patchedBy = selMut ? (selMut.patched_generation_ids || []) : [];
  const selGen = (route.gen && patchedBy.includes(route.gen)) ? route.gen : (patchedBy[0] || null);

  // Resolve REAL strings: baseline = mutation detail's `.baseline.content`;
  // challenger = the patching generation's `.new_content` for the site.
  let baselineStr = null; let challengerStr = null; let rationale = null; let op = null;
  if (selSite) {
    const detail = await D.mutationDetail(epochId, selSite);
    baselineStr = (detail && detail.baseline && typeof detail.baseline.content === 'string')
      ? detail.baseline.content : (typeof (detail && detail.baseline) === 'string' ? detail.baseline : null);
    if (selGen) {
      const pp = await D.patches(epochId, selGen);
      const patches = (pp && Array.isArray(pp.patches)) ? pp.patches : [];
      const patch = patches.find((p) => p.mutation_id === selSite);
      if (patch) { challengerStr = typeof patch.new_content === 'string' ? patch.new_content : null; rationale = patch.rationale || null; op = patch.op || null; }
    }
  }

  const sites = mutations.map((mt) => ({
    id: mt.mutation_id,
    label: mt.role ? String(mt.role) : (mt.file || mt.mutation_id),
    sub: siteSub(mt),
    patched: new Set(mt.patched_generation_ids || []),
  }));

  const digest = JSON.stringify({
    epochId, selSite, selGen,
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

      const combined = el('div', { class: 'vo-mut-combined' });
      const matrixWrap = el('div', { class: 'vo-mut-matrix' }, [
        svg.mutationMatrix({
          sites, gens,
          selected: (selSite && selGen) ? { site: selSite, gen: selGen } : null,
          patched: (siteId, gId) => patchedLookup.has(siteId + ' ' + gId),
          // Selecting a cell selects that site on the patching generation —
          // stays on the epoch mutations facet; the site rides in `entry`,
          // the generation in `gen`, so the diff fills below.
          onCell: (gId, siteId) => ctx.navigate('epoch', { epoch: epochId, facet: 'mutations', entry: siteId, gen: gId }),
        }),
        el('p', { class: 'vo-faint vo-fignote', text: 'a filled cell = that generation patched that site · click to diff it' }),
      ]);
      combined.appendChild(matrixWrap);

      const diffWrap = el('div', { class: 'vo-mut-diff' });
      if (!selSite || !selGen) {
        diffWrap.appendChild(empty('Select a patched site in the matrix to see its side-by-side diff.'));
      } else {
        diffWrap.appendChild(el('div', { class: 'vo-mut-diffhead' }, [
          el('span', { class: 'vo-mono vo-mut-site', text: (selMut && (selMut.role || selMut.file)) || selSite }),
          el('span', { class: 'vo-faint vo-mono', text: 'patched by ' + selGen }),
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

// ---- figure builders (shared with the publication) ------------------

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

function figHeatmapMark(ordered, peByGen, ramp, nav) {
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
  return svg.heatmap({
    rows, cols, cellW: 44, cellH: 20, labelWidth: 180, ramp,
    value: (rowId, colId) => { const v = lookup.get(colId + ' ' + rowId); return svg.isNum(v) ? v : null; },
    onClick: (rowId) => nav('board', { entry: rowId }),
  });
}

// ---- helpers --------------------------------------------------------

function entryMap(pe) {
  const map = new Map();
  if (pe && Array.isArray(pe.entries)) for (const e of pe.entries) map.set(e.entry_id, svg.isNum(e.drift_loss) ? e.drift_loss : null);
  return map;
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

// variants/O/views/candidate.js — the DETAIL PANE for a selected generation.
//
// CANDIDATE-CENTRIC (the operator likes this). The facets here are the
// candidate's own story; the EPOCH-WIDE views (publication, the full
// mutation surface) live at epoch scope (views/epoch.js), NOT here. Tabs:
//   * lifecycle  — the candidate's life: per-board dot-plot + the Tufte
//                  sankey (label ≠ value) + the promote gate (stacked
//                  sections) + WHAT THIS GENERATION PATCHED (its own patch
//                  sites, each linking into the epoch mutation surface so
//                  the candidate story stays complete).
//   * matchups   — the gauntlet ladder + the paired per-board duel + the
//                  gate for this round.
//   * run        — handled by views/run.js (deep drill).

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import {
  gatedSwap, section, empty, loading, verdictPill, normaliseDecision,
  gatePanel, figureFrame,
} from '../ui.js';
import { FACETS } from '../router.js';
import { loadRailModel } from '../model.js';

const FACET_LABELS = { lifecycle: 'Lifecycle', matchups: 'Match-ups', run: 'Run' };

export async function render(host, ctx, route) {
  const genId = route.gen;
  const facet = FACETS.includes(route.facet) ? route.facet : 'lifecycle';
  if (!host.firstChild) host.appendChild(loading('Loading candidate…'));
  if (!genId) { gatedSwap(host, 'no-gen', () => [empty('No generation selected.')]); return; }

  const m = await loadRailModel();
  const { epochId, gens } = m;
  const genMeta = gens.find((g) => g.id === genId) || { id: genId, promoted: false };

  let bodyData = null;
  if (facet === 'lifecycle') bodyData = await lifecycleData(epochId, genId, genMeta, m, ctx);
  else if (facet === 'matchups') bodyData = await matchupsData(epochId, genId, m, ctx);
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
  const champ = m.gens.find((g) => g.promoted);
  const champPe = (champ && champ.id !== genId) ? await D.perEntry(epochId, champ.id) : null;
  const champMap = entryMap(champPe);
  const champTotal = entries.reduce((a, e) => a + (svg.isNum(champMap.get(e.entry_id)) ? champMap.get(e.entry_id) : 0), 0);
  const gate = (genMeta.parent && champ) ? await D.gate(epochId, genMeta.parent, genId) : null;

  // WHAT THIS GENERATION PATCHED — its own patch sites. Keeps the candidate
  // story complete while the full epoch-wide matrix lives at epoch scope;
  // each site links into the epoch mutation surface (focused on this gen).
  const pp = await D.patches(epochId, genId);
  const myPatches = (pp && Array.isArray(pp.patches)) ? pp.patches : [];

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
    patches: myPatches.map((p) => [p.mutation_id, p.op]),
  });

  return {
    digest,
    build: () => {
      const out = [];
      out.push(section('Per-board scoring · sorted worst-first',
        figureFrame({
          mark: svg.valueDotPlot({
            width: 560, rowHeight: 22, labelWidth: 200, items,
            reference: champRef ? { label: champRef.label + ' (avg)', value: champRef.value } : null,
            onClick: (it) => ctx.navigate('board', { entry: it.id }),
          }),
          caption: 'Absolute drift loss per board entry (lower = better). Click a row to open that board’s cross-candidate view.',
        })));
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
      if (gate) {
        out.push(section('The promote gate', gatePanel(gate, genMeta.parent, genId, { fmt: svg.fmt, fmtSigned: svg.fmtSigned })));
      } else {
        out.push(section('The promote gate', empty('This is the seed (or no gate decision was recorded).')));
      }

      // What this generation patched — its own sites, each a link into the
      // EPOCH mutation surface (the full site×generation matrix + diff).
      const patchList = el('ul', { class: 'vo-runlist' });
      if (!myPatches.length) {
        patchList.appendChild(el('li', { class: 'vo-runlist-item vo-disabled' }, [
          el('span', { class: 'vo-faint', text: 'This generation applied no patches (the seed, or a no-op).' }),
        ]));
      } else {
        for (const p of myPatches) {
          const li = el('li', { class: 'vo-runlist-item', tabindex: '0', role: 'button', 'data-site': p.mutation_id }, [
            el('span', { class: 'vo-mono', text: p.mutation_id }),
            p.op ? el('span', { class: 'vo-diff-op vo-op-' + p.op, text: p.op }) : null,
            el('span', { class: 'vo-runlist-open', text: 'diff in mutation surface →' }),
          ].filter(Boolean));
          // Link into the EPOCH-scoped mutation surface, focused on this
          // generation's patch to the site (the candidate story stays
          // complete; the full matrix is not duplicated as a gen facet).
          const go = () => ctx.navigate('epoch', { epoch: epochId, facet: 'mutations', entry: p.mutation_id, gen: genId });
          li.addEventListener('click', go);
          li.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(); } });
          patchList.appendChild(li);
        }
      }
      out.push(section('What this generation patched', patchList));

      if (!entries.length) out.push(empty('No per-board scores recorded for this candidate.'));
      return out;
    },
  };
}

// ---- MATCH-UPS ------------------------------------------------------

async function matchupsData(epochId, genId, m, ctx) {
  const tours = m.tours;
  const matchups = (tours && Array.isArray(tours.matchups)) ? tours.matchups : [];
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

// ---- shared helpers ------------------------------------------------

function entryMap(pe) {
  const map = new Map();
  if (pe && Array.isArray(pe.entries)) for (const e of pe.entries) map.set(e.entry_id, svg.isNum(e.drift_loss) ? e.drift_loss : null);
  return map;
}
function fx(v) { return svg.isNum(v) ? Number(v).toFixed(3) : null; }

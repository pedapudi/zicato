// variants/L/views/mutations.js — MUTATION SURFACE + SIDE-BY-SIDE DIFF.
//
// ONE cohesive visual (fix #2): the mutation-site × generation MATRIX plus a
// detail pane that fills on cell-select with a SIDE-BY-SIDE, line-diffed view
// (two columns: champion baseline | challenger new). Based on K's mutation
// element (judged best of the round).
//
// Data flow (the exact contract — and the "[object Object]" fix):
//   * surface:  GET /api/mutations/{epoch} → { generations, mutations:[…] }
//   * baseline STRING:  GET /api/mutations/{epoch}/{mutation_id} →
//       `.baseline.content` (NOT the `.baseline` OBJECT — rendering the
//       object is what produced "[object Object]"). Falls back to the
//       surface payload's matching mutation `.baseline.content`.
//   * challenger NEW STRING:  GET /api/files/{epoch}/{gen}/patches → the
//       patches[] entry whose `mutation_id` matches → `.new_content`
//       (+ `.op`, `.rationale`).
//   * full-file fallback:  GET /api/files/{epoch}/{gen}/diff →
//       files[].old_content / .new_content.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, loading } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(loading('Reading the mutation surface…'));

  const ep = await D.epoch();
  const epochId = (ep && ep.epoch_id) || (state.epochDef && state.epochDef.epoch_id) || null;
  if (!epochId) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'vl-h1', text: 'Mutations' }), empty('No current epoch.')]);
    return;
  }

  const [muts, lineage] = await Promise.all([D.mutations(epochId), D.lineage(epochId)]);
  const mutations = (muts && Array.isArray(muts.mutations)) ? muts.mutations : [];
  let genIds = (muts && Array.isArray(muts.generations) && muts.generations.length) ? muts.generations.slice() : [];
  const lineGens = (lineage && Array.isArray(lineage.generations)) ? lineage.generations : [];
  if (!genIds.length) genIds = lineGens.map((g) => g.generation_id);
  const promotedSet = new Set(lineGens.filter((g) => g.promoted).map((g) => g.generation_id));

  const sites = mutations.map((m) => ({
    id: m.mutation_id,
    label: m.role || (m.file || m.mutation_id),
    sub: siteSub(m),
    patched: new Set(m.patched_generation_ids || []),
    raw: m,
  }));
  const siteById = new Map(sites.map((s) => [s.id, s]));

  // Selection: a generation (column) and optionally a mutation site (row).
  // A selected site fills the detail pane with the side-by-side diff.
  const selGen = (params && params.gen) || null;
  const selSite = (params && params.mutationId) || null;

  // Resolve the side-by-side strings only when a site is selected.
  let baseline = null; let next = null; let patchMeta = null; let usedFallback = false;
  if (selSite && selGen) {
    // 1) baseline content STRING — from the per-site detail endpoint, then
    //    the surface payload. Read `.baseline.content`, NEVER `.baseline`.
    const detail = await D.mutationDetail(epochId, selSite);
    baseline = baselineContent(detail) || baselineContent(siteById.get(selSite) && siteById.get(selSite).raw);
    // 2) challenger NEW content STRING — the matching patch's new_content.
    const pp = await D.patches(epochId, selGen);
    const patches = (pp && Array.isArray(pp.patches)) ? pp.patches : [];
    const match = patches.find((p) => p.mutation_id === selSite) || null;
    if (match) { next = match.new_content != null ? String(match.new_content) : null; patchMeta = { op: match.op, rationale: match.rationale }; }
    // 3) full-file fallback when either side is missing.
    if (baseline == null || next == null) {
      const df = await D.diff(epochId, selGen);
      const files = (df && Array.isArray(df.files)) ? df.files : [];
      const site = siteById.get(selSite);
      const fileHint = site && site.raw ? site.raw.file : null;
      const f = (fileHint && files.find((x) => x.path === fileHint)) || files[0] || null;
      if (f) {
        if (baseline == null && f.old_content != null) { baseline = String(f.old_content); usedFallback = true; }
        if (next == null && f.new_content != null) { next = String(f.new_content); usedFallback = true; }
      }
    }
  }

  const digest = JSON.stringify({
    epochId, selGen, selSite,
    gens: genIds,
    sites: sites.map((s) => [s.id, s.label, [...s.patched]]),
    baseLen: baseline == null ? -1 : baseline.length,
    nextLen: next == null ? -1 : next.length,
    op: patchMeta ? patchMeta.op : null,
  });

  gatedSwap(host, digest, () => {
    const out = [];
    out.push(el('div', { class: 'vl-pagehead' }, [
      el('h1', { class: 'vl-h1', text: 'Mutation surface' }),
      el('p', { class: 'vl-lede', text: 'Which mutation sites each generation patched — and exactly what changed, baseline against challenger, side by side.' }),
    ]));

    const gens = genIds.map((id) => ({ id, label: id, promoted: promotedSet.has(id) }));
    const matrixCard = el('div', { class: 'vl-panel' });
    if (sites.length && gens.length) {
      const patchedLookup = new Map();
      for (const s of sites) for (const g of s.patched) patchedLookup.set(s.id + ' ' + g, true);
      matrixCard.appendChild(svg.mutationMatrix({
        sites, gens, selectedGen: selGen, selectedSite: selSite,
        patched: (siteId, genId) => patchedLookup.has(siteId + ' ' + genId),
        // a cell selects BOTH its generation and its site → fills the diff.
        onCell: (genId, siteId) => ctx.navigate('mutations', { gen: genId, mutationId: siteId }),
      }));
      matrixCard.appendChild(el('p', { class: 'vl-faint vl-fignote', text: 'a filled cell = that generation patched that site · click → its baseline-vs-challenger diff' }));
    } else {
      matrixCard.appendChild(empty('No mutation surface recorded for this epoch.'));
    }
    out.push(section('Mutation surface · site × generation', matrixCard));

    // The detail pane: fills on cell-select with the side-by-side diff.
    out.push(section('Patch detail · champion baseline | challenger new', diffPane({
      selGen, selSite, site: selSite ? siteById.get(selSite) : null, baseline, next, patchMeta, usedFallback,
    })));
    return out;
  });
}

function diffPane(o) {
  const card = el('div', { class: 'vl-panel vl-diffpane' });
  if (!o.selSite || !o.selGen) {
    card.appendChild(empty('Select a filled cell in the matrix above to see what that generation changed at that site.'));
    return card;
  }
  const site = o.site;
  card.appendChild(el('div', { class: 'vl-diff-meta' }, [
    el('span', { class: 'vl-diff-status vl-op-' + (o.patchMeta && o.patchMeta.op ? o.patchMeta.op : 'edit'), text: (o.patchMeta && o.patchMeta.op) || 'edit' }),
    el('span', { class: 'vl-diff-path vl-mono', text: (site && (site.sub || site.label)) || o.selSite }),
    el('span', { class: 'vl-faint', text: ' · ' + o.selGen }),
    o.usedFallback ? el('span', { class: 'vl-chip vl-chip-open', text: 'full-file fallback' }) : null,
  ].filter(Boolean)));
  if (o.patchMeta && o.patchMeta.rationale) {
    card.appendChild(el('p', { class: 'vl-soft', text: o.patchMeta.rationale }));
  }
  if (o.baseline == null && o.next == null) {
    card.appendChild(empty('No baseline or patch content recorded for this site (it may be the seed, or the index is not built).'));
    return card;
  }
  // The side-by-side diff is built from two STRINGS (the "[object Object]"
  // fix — never the baseline OBJECT).
  card.appendChild(svg.sideBySideDiff({
    baseline: o.baseline != null ? o.baseline : '',
    next: o.next != null ? o.next : '',
    leftLabel: 'champion baseline',
    rightLabel: 'challenger new · ' + o.selGen,
  }));
  return card;
}

// Read the baseline content STRING from a payload. Accepts the per-site
// detail shape ({ baseline:{content} }) and the surface mutation shape; only
// ever returns `.baseline.content` (a string), never the baseline object.
function baselineContent(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const b = payload.baseline;
  if (b && typeof b === 'object' && typeof b.content === 'string') return b.content;
  if (typeof payload.baseline_content === 'string') return payload.baseline_content;
  return null;
}

function siteSub(m) {
  const file = m.file || '';
  const span = (svg.isNum(m.line_start)) ? `:${m.line_start}${svg.isNum(m.line_end) && m.line_end !== m.line_start ? '-' + m.line_end : ''}` : '';
  const k = m.kind ? ` (${m.kind})` : '';
  return (file + span + k).trim();
}

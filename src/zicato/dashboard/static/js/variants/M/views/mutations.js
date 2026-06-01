// variants/M/views/mutations.js — MUTATION SURFACE: ONE cohesive visual.
//
// CONVERGENCE-II FIX #2 — the mutation surface is ONE combined layout: the
// site × generation MATRIX plus a detail pane that fills on cell-select with
// a SIDE-BY-SIDE diff (two columns: champion baseline | challenger new),
// line-diffed. Based on K's mutation element (the best of the round).
//
// The strings are read correctly (this is the bug the operator flagged):
//   * baseline content (STRING): /api/mutations/{epoch}/{mutation_id} →
//     `.baseline.content`. (Rendering the `baseline` OBJECT was the
//     "[object Object]" bug.)
//   * challenger new content (STRING): /api/files/{epoch}/{gen}/patches →
//     the patches[] entry whose `mutation_id` matches → `.new_content`.
//   * full-file fallback: /api/files/{epoch}/{gen}/diff → files[].old/new.
//
// Selecting a cell (site, gen) drills to that exact champion-vs-challenger
// diff. Selecting just a site picks its first patching generation.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, pageHead, sideBySideDiff } from '../ui.js';

function shortFile(f) {
  const s = String(f || '');
  const parts = s.split('/');
  return parts.length > 2 ? '…/' + parts.slice(-2).join('/') : s;
}
function siteSub(m) {
  const file = shortFile(m.file);
  const span = (svg.isNum(m.line_start)) ? `:${m.line_start}${svg.isNum(m.line_end) && m.line_end !== m.line_start ? '-' + m.line_end : ''}` : '';
  return (file + span).trim();
}

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'd-empty', text: 'Enumerating mutation surface…' }));

  const ep = await D.epoch();
  const epochId = ep && ep.epoch_id;
  if (!epochId) {
    gatedSwap(host, 'no-epoch', () => [pageHead('Mutation surface', 'Mutation surface', ''), empty('No current epoch.')]);
    return;
  }

  const [mut, lin] = await Promise.all([D.mutations(epochId), D.lineage()]);
  const sites = (mut && Array.isArray(mut.mutations)) ? mut.mutations : [];
  let genIds = (mut && Array.isArray(mut.generations) && mut.generations.length) ? mut.generations.slice() : [];
  const lineGens = (lin && Array.isArray(lin.generations)) ? lin.generations : [];
  if (!genIds.length) genIds = lineGens.map((g) => g.generation_id);
  const promotedSet = new Set(lineGens.filter((g) => g.promoted).map((g) => g.generation_id));

  // Per-generation patch sets (so a cell maps to a patch + its new_content).
  const patchSets = await Promise.all(genIds.map((g) => D.patches(epochId, g)));
  const patchByMutGen = new Map(); // mutationId -> genId -> patch
  genIds.forEach((g, i) => {
    const ps = patchSets[i];
    const list = ps && Array.isArray(ps.patches) ? ps.patches : [];
    for (const p of list) {
      if (!p || p.mutation_id == null) continue;
      if (!patchByMutGen.has(p.mutation_id)) patchByMutGen.set(p.mutation_id, new Map());
      patchByMutGen.get(p.mutation_id).set(g, p);
    }
  });

  // The selected site + (optionally) the selected challenger generation.
  const selSite = params && params.mutationId;
  const selSiteObj = sites.find((s) => s.mutation_id === selSite) || null;
  const patchGens = selSite && patchByMutGen.has(selSite) ? [...patchByMutGen.get(selSite).keys()] : [];
  const selGen = (params && params.gen && patchGens.includes(params.gen)) ? params.gen : (patchGens[0] || null);

  let detail = null;
  if (selSite) detail = await D.mutationDetail(epochId, selSite);
  const baseline = D.baselineContent(detail); // STRING via .baseline.content

  const selPatch = (selSite && selGen && patchByMutGen.has(selSite)) ? patchByMutGen.get(selSite).get(selGen) : null;
  let challengerNew = selPatch && typeof selPatch.new_content === 'string' ? selPatch.new_content : null;
  // Full-file fallback when the patch carried no new_content string.
  if (challengerNew == null && selGen) {
    const df = await D.diff(epochId, selGen);
    const files = df && Array.isArray(df.files) ? df.files : [];
    const f = files.find((x) => x && (x.mutation_id === selSite));
    if (f && typeof f.new_content === 'string') challengerNew = f.new_content;
  }

  const digest = JSON.stringify({
    epochId, genIds,
    sites: sites.map((s) => [s.mutation_id, s.file, s.role, s.line_start, (s.patched_generation_ids || []).slice().sort()]),
    sel: selSite || null, selGen: selGen || null,
    baseLen: typeof baseline === 'string' ? baseline.length : null,
    newLen: typeof challengerNew === 'string' ? challengerNew.length : null,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(pageHead('Mutation surface · ' + epochId, 'What each generation changed',
      'Every `# zicato:mutable` site in the epoch’s baseline, against the generations that patched it. Select a cell to read its champion-baseline-vs-challenger SIDE-BY-SIDE diff.'));

    if (mut && mut.error) { nodes.push(el('div', { class: 'd-panel' }, [empty(mut.error)])); return nodes; }
    if (!sites.length) { nodes.push(el('div', { class: 'd-panel' }, [empty('No mutation sites enumerated for this epoch (the baseline surface may be empty).')])); return nodes; }

    // ONE cohesive visual: the matrix + the side-by-side detail pane.
    const combined = el('div', { class: 'd-panel m-mut-combined' });

    const siteRows = sites.map((s) => ({
      id: s.mutation_id, label: s.role || shortFile(s.file) || s.mutation_id, sub: siteSub(s),
    }));
    const gens = genIds.map((id) => ({ id, label: id, promoted: promotedSet.has(id) }));
    const patchedLookup = new Map();
    for (const s of sites) for (const g of (s.patched_generation_ids || [])) patchedLookup.set(s.mutation_id + ' ' + g, true);

    const matrixWrap = el('div', { class: 'm-mut-matrix-wrap m-scroll-x' });
    matrixWrap.appendChild(svg.mutationMatrix({
      sites: siteRows, gens,
      patched: (siteId, genId) => patchedLookup.has(siteId + ' ' + genId),
      selSite, selGen,
      onCell: (siteId, genId) => ctx.navigate('mutations', { mutationId: siteId, gen: genId }),
    }));
    combined.appendChild(el('div', { class: 'm-mut-matrix-panel' }, [
      el('div', { class: 'm-mut-sub', text: 'a filled cell = that generation patched that site · click to read its diff' }),
      matrixWrap,
    ]));

    // detail pane
    const pane = el('div', { class: 'm-mut-detail' });
    if (!selSite) {
      pane.appendChild(empty('Select a patched cell in the matrix to read its side-by-side diff.'));
    } else {
      pane.appendChild(el('div', { class: 'm-mut-detail-head' }, [
        el('span', { class: 'd-mono', text: selSiteObj ? (selSiteObj.file + ':' + (selSiteObj.line_start ?? '?')) : selSite }),
        selSiteObj && selSiteObj.role ? el('span', { class: 'm-mut-role', text: selSiteObj.role }) : null,
        selGen ? el('span', { class: 'm-mut-genchip', text: selGen }) : null,
        selPatch && selPatch.op ? el('span', { class: 'm-mut-op', text: selPatch.op }) : null,
      ].filter(Boolean)));
      // generation tabs (which challenger to diff against)
      if (patchGens.length > 1) {
        pane.appendChild(el('div', { class: 'm-mut-gentabs' }, patchGens.map((g) =>
          el('a', { class: 'm-mut-gentab' + (g === selGen ? ' m-on' : ''), href: ctx.href('mutations', { mutationId: selSite, gen: g }), text: g }))));
      }
      if (selPatch && selPatch.rationale) pane.appendChild(el('p', { class: 'm-mut-rationale', text: selPatch.rationale }));

      if (typeof baseline === 'string' || typeof challengerNew === 'string') {
        pane.appendChild(sideBySideDiff({
          baseline: typeof baseline === 'string' ? baseline : '',
          challenger: typeof challengerNew === 'string' ? challengerNew : '',
          leftLabel: 'champion baseline (v0)',
          rightLabel: 'challenger ' + (selGen || '') + ' new',
        }));
      } else {
        pane.appendChild(empty('No patch content available for this site (it may be unpatched, or the content is missing).'));
      }
    }
    combined.appendChild(pane);
    nodes.push(section('Mutation surface · site × generation + side-by-side diff', combined));
    return nodes;
  });
}

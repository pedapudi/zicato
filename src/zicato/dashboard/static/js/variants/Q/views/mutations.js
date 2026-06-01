// variants/Q/views/mutations.js — ONE cohesive visual: the mutation surface
// (site × generation matrix) + a SIDE-BY-SIDE patch diff (fix #2).
//
// Reuses L's / N's mutation-viewer quality. The matrix plus a detail pane:
// select a site (or arrive with ?gen focused — fix #2, the per-candidate diff
// the candidate page links to) and the pane fills with the line-diffed patch,
// SIDE-BY-SIDE — champion baseline (left) | challenger new (right).
//
// Data (exactly per the brief):
//   * matrix       — /api/mutations/{epoch}
//   * baseline STR — /api/mutations/{epoch}/{mutation_id} → .baseline.content
//                    (the STRING — NOT the `baseline` object).
//   * challenger STR — /api/files/{epoch}/{gen}/patches → matching .new_content.
//   * full-file fallback — /api/files/{epoch}/{gen}/diff.

import { el, svgEl } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dq-empty', text: 'Reading mutation surface…' }));
  const pinned = params && params.mutId;
  const focusGen = (params && params.gen) || null;

  const ep = await D.epoch();
  const epochId = (params && params.epochId) || (ep && ep.epoch_id) || (state.epochDef && state.epochDef.epoch_id) || null;
  if (!epochId) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dq-h1', text: 'Mutation surface' }), empty('No current epoch.')]);
    return;
  }

  const [mut, lin] = await Promise.all([D.mutations(epochId), D.lineage()]);
  const gens = (mut && Array.isArray(mut.generations) && mut.generations.length)
    ? mut.generations.slice()
    : ((lin && Array.isArray(lin.generations)) ? lin.generations.map((g) => g.generation_id) : []);
  const sites = (mut && Array.isArray(mut.mutations)) ? mut.mutations : [];

  const patchedBySite = new Map();
  for (const s of sites) patchedBySite.set(s.mutation_id, new Set(Array.isArray(s.patched_generation_ids) ? s.patched_generation_ids : []));

  // When focused on a candidate (fix #2), pin its FIRST patched site if none
  // pinned, so the per-candidate diff fills immediately from the candidate page.
  let effectivePinned = pinned;
  if (!effectivePinned && focusGen) {
    const firstSite = sites.find((s) => (patchedBySite.get(s.mutation_id) || new Set()).has(focusGen));
    if (firstSite) effectivePinned = firstSite.mutation_id;
  }

  const pinnedSite = effectivePinned ? sites.find((s) => s.mutation_id === effectivePinned) : null;
  let detail = null;
  const patchesByGen = new Map();
  if (pinnedSite) {
    detail = await D.mutationDetail(epochId, effectivePinned);
    // when focused, show only the focus gen's patch; otherwise every patcher.
    const touchedAll = [...(patchedBySite.get(effectivePinned) || [])];
    const touched = focusGen ? touchedAll.filter((g) => g === focusGen) : touchedAll;
    const all = await Promise.all(touched.map((g) => D.patches(epochId, g)));
    touched.forEach((g, i) => patchesByGen.set(g, (all[i] && Array.isArray(all[i].patches)) ? all[i].patches : []));
  }

  const baselineStr = (detail && detail.baseline && typeof detail.baseline.content === 'string')
    ? detail.baseline.content : null;

  const digest = JSON.stringify({
    epochId, focusGen, gens,
    sites: sites.map((s) => [s.mutation_id, s.file, s.role, s.line_start, s.line_end, (s.patched_generation_ids || []).join(',')]),
    pinned: effectivePinned || null,
    baselineLen: baselineStr == null ? -1 : baselineStr.length,
    patched: pinnedSite ? [...patchesByGen.keys()] : null,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dq-pagehead' }, [
      el('h1', { class: 'dq-h1', text: focusGen ? `Mutation diff · ${focusGen}` : 'Mutation surface · site × generation' }),
      el('p', { class: 'dq-lede', text: focusGen
        ? `What ${focusGen} changed — each patched site, champion baseline against this challenger’s new content, side by side. Pick any other site in the matrix to compare.`
        : 'Every enumerated mutation point (a `# zicato:mutable` region) and which generation patched it. Select a site for the side-by-side patch diff.' }),
    ]));

    nodes.push(el('div', { class: 'dq-panel dq-row' }, [
      stat(String(sites.length), 'mutation sites'),
      stat(String(gens.length), 'generations'),
      stat(String(sites.filter((s) => (s.patched_generation_ids || []).length).length), 'sites touched'),
    ]));

    if (!sites.length || !gens.length) {
      nodes.push(section('Surface', el('div', { class: 'dq-panel' }, [empty('No mutation surface for this epoch (the enumeration may be missing).')])));
      return nodes;
    }

    const combined = el('div', { class: 'dq-mut-combined' }, [
      el('div', { class: 'dq-panel dq-mut-matrix', style: 'overflow-x:auto;' }, [matrixTable(sites, gens, patchedBySite, effectivePinned, focusGen, ctx, epochId)]),
      el('div', { class: 'dq-panel dq-mut-detail' }, [detailPane(pinnedSite, baselineStr, detail, patchesByGen, ctx, epochId)]),
    ]);
    nodes.push(section('Mutation surface + side-by-side diff', combined));
    return nodes;
  });
}

function matrixTable(sites, gens, patchedBySite, pinned, focusGen, ctx, epochId) {
  const table = el('table', { class: 'dn-mtx' });
  const thead = el('thead');
  const hr = el('tr');
  hr.appendChild(el('th', { class: 'dn-mtx-corner', text: 'site (file:line · role)' }));
  for (const g of gens) hr.appendChild(el('th', { class: 'dn-mtx-gen' + (g === focusGen ? ' dn-mtx-gen-focus' : '') }, [
    el('a', { class: 'dn-mtx-genlink', href: ctx.href('gen', { epochId, gen: g }), text: g }),
  ]));
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = el('tbody');
  for (const s of sites) {
    const touched = patchedBySite.get(s.mutation_id) || new Set();
    const isPinned = pinned === s.mutation_id;
    const tr = el('tr', { class: 'dn-mtx-row' + (isPinned ? ' dn-mtx-pinned' : '') });
    tr.appendChild(el('th', { class: 'dn-mtx-site', scope: 'row' }, [
      el('a', { class: 'dn-mtx-sitelink', href: ctx.href('mutations', { epochId, gen: focusGen, mutId: s.mutation_id }), title: s.mutation_id }, [
        el('span', { class: 'dn-mtx-file', text: fileLine(s) }),
        el('span', { class: 'dn-mtx-role', text: s.role || s.kind || '' }),
      ]),
    ]));
    for (const g of gens) {
      const on = touched.has(g);
      const td = el('td', { class: 'dn-mtx-cell' + (on ? ' dn-mtx-on' : '') });
      if (on) {
        const dot = svgEl('svg', { class: 'dn-mtx-mark', width: 16, height: 16, viewBox: '0 0 16 16', role: 'img' }, [
          svgEl('rect', { x: 3, y: 3, width: 10, height: 10, rx: 2, class: 'dn-mtx-square' }),
        ]);
        td.appendChild(el('a', { class: 'dn-mtx-celllink', href: ctx.href('mutations', { epochId, gen: g, mutId: s.mutation_id }), title: `${g} patched ${s.mutation_id}` }, [dot]));
      } else {
        td.appendChild(el('span', { class: 'dn-mtx-blank', 'aria-hidden': 'true', text: '·' }));
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  const wrap = el('div');
  wrap.appendChild(table);
  wrap.appendChild(el('p', { class: 'dq-faint', style: 'font-size:11px;margin:12px 0 0;', text: 'row = mutation site · column = generation · ▪ = patched here · click a cell → its side-by-side diff' }));
  return wrap;
}

function detailPane(site, baselineStr, detail, patchesByGen, ctx, epochId) {
  const pane = el('div');
  if (!site) {
    pane.appendChild(el('p', { class: 'dq-empty', text: 'Select a mutation site (a row, or a ▪ cell) to see its side-by-side patch diff.' }));
    return pane;
  }
  pane.appendChild(el('div', { class: 'dn-mtx-drillhead' }, [
    el('span', { class: 'dq-mono', text: site.mutation_id }),
    el('span', { class: 'dq-faint dq-mono', text: ' · ' + fileLine(site) + (site.role ? ' · ' + site.role : '') }),
  ]));

  if (baselineStr == null) {
    pane.appendChild(el('p', { class: 'dn-patch-note dq-faint', text: 'No baseline (v0) content recorded for this site — the diff needs both sides.' }));
  }

  const touched = [...(patchesByGen.keys ? patchesByGen.keys() : [])];
  let any = false;
  for (const g of touched) {
    const patches = patchesByGen.get(g) || [];
    const patch = patches.find((p) => p.mutation_id === site.mutation_id || p.id === site.mutation_id);
    let newStr = patch && patch.new_content != null ? String(patch.new_content) : null;
    if (newStr == null && detail && Array.isArray(detail.versions)) {
      const v = detail.versions.find((x) => x.generation_id === g);
      if (v && typeof v.content === 'string') newStr = v.content;
    }
    if (newStr == null) continue;
    any = true;
    pane.appendChild(genDiffBlock(g, patch, baselineStr == null ? '' : baselineStr, newStr, ctx, epochId));
  }
  if (!any) pane.appendChild(el('p', { class: 'dq-empty', text: 'No generation patched this site (or the patch payloads are unavailable).' }));
  return pane;
}

function genDiffBlock(gen, patch, baselineStr, newStr, ctx, epochId) {
  const block = el('div', { class: 'dn-patch-block' });
  const op = String((patch && patch.op) || 'replace');
  block.appendChild(el('div', { class: 'dn-patch-head' }, [
    el('a', { class: 'dn-mtx-genlink', href: ctx.href('gen', { epochId, gen }), text: gen }),
    el('span', { class: 'dn-patch-op dq-mono', text: op }),
  ]));
  const rationale = patch && patch.rationale ? String(patch.rationale).trim() : '';
  if (rationale) {
    block.appendChild(el('p', { class: 'dn-patch-why' }, [el('span', { class: 'dn-patch-why-lead', text: 'Why. ' }), rationale]));
  }
  block.appendChild(svg.sideBySideDiff({
    baseline: baselineStr,
    challenger: newStr,
    leftLabel: 'champion baseline · v0',
    rightLabel: `challenger new · ${gen}`,
  }));
  return block;
}

function fileLine(s) {
  const f = s.file || '?';
  const a = s.line_start;
  const b = s.line_end;
  if (a != null && b != null && a !== b) return `${f}:${a}–${b}`;
  if (a != null) return `${f}:${a}`;
  return f;
}

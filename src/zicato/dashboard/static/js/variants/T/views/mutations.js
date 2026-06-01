// variants/T/views/mutations.js — ONE cohesive visual: the mutation surface
// (site × generation matrix) + a SIDE-BY-SIDE patch diff (fix #2).
//
// Based on K's mutation element (judged best of the round). The matrix plus a
// detail pane: select a site and the pane fills with the line-diffed patch,
// shown SIDE-BY-SIDE — champion baseline (left) | challenger new (right).
//
// Data (exactly per the brief):
//   * matrix       — /api/mutations/{epoch} → { generations, mutations:[{
//                    mutation_id, file, role, line_start, line_end,
//                    patched_generation_ids }] }
//   * baseline STR — /api/mutations/{epoch}/{mutation_id} → .baseline.content
//                    (the STRING — NOT the `baseline` object; that was the
//                    "[object Object]" bug).
//   * challenger STR — /api/files/{epoch}/{gen}/patches → the patches[] entry
//                    whose mutation_id matches → .new_content (+ .op, .rationale).
//   * full-file fallback — /api/files/{epoch}/{gen}/diff → files[].old/new_content.
//
// The pinned site lives in the URL (#/N/mutations/<mutId>) so the diff pane
// rebuilds ONLY on a route change, never on a heartbeat.

import { el, svgEl } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading mutation surface…' }));
  const pinned = params && params.mutId;

  const ep = await D.epoch();
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Mutation surface' }), empty('No current epoch.')]);
    return;
  }
  const epochId = ep.epoch_id;

  const mut = await D.mutations(epochId);
  const gens = (mut && Array.isArray(mut.generations)) ? mut.generations : [];
  const sites = (mut && Array.isArray(mut.mutations)) ? mut.mutations : [];

  const patchedBySite = new Map();
  for (const s of sites) patchedBySite.set(s.mutation_id, new Set(Array.isArray(s.patched_generation_ids) ? s.patched_generation_ids : []));

  // The pinned site → its baseline STRING (one call) + per-generation patches.
  const pinnedSite = pinned ? sites.find((s) => s.mutation_id === pinned) : null;
  let detail = null;
  const patchesByGen = new Map();
  if (pinnedSite) {
    detail = await D.mutationDetail(epochId, pinned);
    const touched = [...(patchedBySite.get(pinned) || [])];
    const all = await Promise.all(touched.map((g) => D.patches(epochId, g)));
    touched.forEach((g, i) => patchesByGen.set(g, (all[i] && Array.isArray(all[i].patches)) ? all[i].patches : []));
  }

  // baseline content (STRING) — never the object.
  const baselineStr = (detail && detail.baseline && typeof detail.baseline.content === 'string')
    ? detail.baseline.content : null;

  const digest = JSON.stringify({
    epochId, gens,
    sites: sites.map((s) => [s.mutation_id, s.file, s.role, s.line_start, s.line_end, (s.patched_generation_ids || []).join(',')]),
    pinned: pinned || null,
    baselineLen: baselineStr == null ? -1 : baselineStr.length,
    patched: pinnedSite ? [...(patchedBySite.get(pinned) || [])] : null,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: 'Mutation surface · site × generation' }),
      el('p', { class: 'dn-lede', text: 'Every enumerated mutation point (a `# zicato:mutable` region) and which generation patched it. Select a site for the side-by-side patch diff — champion baseline against the challenger’s new content.' }),
    ]));

    nodes.push(el('div', { class: 'dn-panel dn-row' }, [
      stat(String(sites.length), 'mutation sites'),
      stat(String(gens.length), 'generations'),
      stat(String(sites.filter((s) => (s.patched_generation_ids || []).length).length), 'sites touched'),
    ]));

    if (!sites.length || !gens.length) {
      nodes.push(section('Surface', el('div', { class: 'dn-panel' }, [empty('No mutation surface for this epoch (the enumeration may be missing).')])));
      return nodes;
    }

    // ONE cohesive layout: the matrix and the detail pane in a single section.
    const combined = el('div', { class: 'dn-mut-combined' }, [
      el('div', { class: 'dn-panel dn-mut-matrix' }, [matrixTable(sites, gens, patchedBySite, pinned, ctx, epochId)]),
      el('div', { class: 'dn-panel dn-mut-detail' }, [detailPane(pinnedSite, baselineStr, detail, patchesByGen, ctx, epochId)]),
    ]);
    nodes.push(section('Mutation surface + side-by-side diff', combined));
    return nodes;
  });
}

function matrixTable(sites, gens, patchedBySite, pinned, ctx, epochId) {
  const table = el('table', { class: 'dn-mtx' });
  const thead = el('thead');
  const hr = el('tr');
  hr.appendChild(el('th', { class: 'dn-mtx-corner', text: 'site (file:line · role)' }));
  for (const g of gens) hr.appendChild(el('th', { class: 'dn-mtx-gen' }, [
    el('a', { class: 'dn-mtx-genlink', href: ctx.href('candidate', { epochId, gen: g }), text: g }),
  ]));
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = el('tbody');
  for (const s of sites) {
    const touched = patchedBySite.get(s.mutation_id) || new Set();
    const isPinned = pinned === s.mutation_id;
    const tr = el('tr', { class: 'dn-mtx-row' + (isPinned ? ' dn-mtx-pinned' : '') });
    tr.appendChild(el('th', { class: 'dn-mtx-site', scope: 'row' }, [
      el('a', { class: 'dn-mtx-sitelink', href: ctx.href('mutations', { epochId, mutId: s.mutation_id }), title: s.mutation_id }, [
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
        td.appendChild(el('a', { class: 'dn-mtx-celllink', href: ctx.href('mutations', { epochId, mutId: s.mutation_id }), title: `${g} patched ${s.mutation_id}` }, [dot]));
      } else {
        td.appendChild(el('span', { class: 'dn-mtx-blank', 'aria-hidden': 'true', text: '·' }));
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  const wrap = el('div');
  // the matrix can be genuinely wide (many generations) — give the TABLE its
  // own contained horizontal scroll so it never forces the panel to overflow.
  wrap.appendChild(el('div', { class: 'dn-table-scroll' }, [table]));
  wrap.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:10px 0 0;', text: 'row = mutation site · column = generation · ▪ = patched here · click a cell or site → its side-by-side diff' }));
  return wrap;
}

// The detail pane: fills on cell-select with the SIDE-BY-SIDE diff(s).
function detailPane(site, baselineStr, detail, patchesByGen, ctx, epochId) {
  const pane = el('div');
  if (!site) {
    pane.appendChild(el('p', { class: 'dn-empty', text: 'Select a mutation site (a row, or a ▪ cell) to see its side-by-side patch diff.' }));
    return pane;
  }
  pane.appendChild(el('div', { class: 'dn-mtx-drillhead' }, [
    el('span', { class: 'dn-mono', text: site.mutation_id }),
    el('span', { class: 'dn-faint dn-mono', text: ' · ' + fileLine(site) + (site.role ? ' · ' + site.role : '') }),
  ]));

  if (baselineStr == null) {
    pane.appendChild(el('p', { class: 'dn-patch-note dn-faint', text: 'No baseline (v0) content recorded for this site — the diff needs both sides.' }));
  }

  const touched = [...(patchesByGen.keys ? patchesByGen.keys() : [])];
  let any = false;
  for (const g of touched) {
    const patches = patchesByGen.get(g) || [];
    const patch = patches.find((p) => p.mutation_id === site.mutation_id || p.id === site.mutation_id);
    // challenger new content (STRING) from /patches; fall back to the detail's
    // version content if the patches payload lacks it.
    let newStr = patch && patch.new_content != null ? String(patch.new_content) : null;
    if (newStr == null && detail && Array.isArray(detail.versions)) {
      const v = detail.versions.find((x) => x.generation_id === g);
      if (v && typeof v.content === 'string') newStr = v.content;
    }
    if (newStr == null) continue;
    any = true;
    pane.appendChild(genDiffBlock(g, patch, baselineStr == null ? '' : baselineStr, newStr, site, ctx, epochId));
  }
  if (!any) pane.appendChild(el('p', { class: 'dn-empty', text: 'No generation patched this site (or the patch payloads are unavailable).' }));
  return pane;
}

function genDiffBlock(gen, patch, baselineStr, newStr, site, ctx, epochId) {
  const block = el('div', { class: 'dn-patch-block' });
  const op = String((patch && patch.op) || 'replace');
  block.appendChild(el('div', { class: 'dn-patch-head' }, [
    el('a', { class: 'dn-mtx-genlink', href: ctx.href('candidate', { epochId, gen }), text: gen }),
    el('span', { class: 'dn-patch-op dn-mono', text: op }),
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

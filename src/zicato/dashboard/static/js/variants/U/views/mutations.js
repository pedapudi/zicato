// variants/U/views/mutations.js — MUTATION SURFACE + SIDE-BY-SIDE diff.
//
// ONE cohesive visual (L's mutation-viewer quality): the mutation-site ×
// generation MATRIX plus a detail pane that fills on select with the
// line-diffed, SIDE-BY-SIDE view — champion baseline (left) | challenger new
// (right). Both sides are STRINGS (the "[object Object]" fix — never the
// baseline OBJECT).
//
// Data: /api/mutations/{epoch} (surface) · /api/mutations/{epoch}/{id}
// (.baseline.content) · /api/files/{epoch}/{gen}/patches (.new_content) ·
// /api/files/{epoch}/{gen}/diff (full-file fallback).

import { el, svgEl } from '../../../core/dom.js';
import * as D from '../data.js';
import { gatedSwap, section, empty } from '../ui.js';
import * as svg from '../svg.js';

export async function render(host, ctx, route) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading mutation surface…' }));
  const p = route.params || {};

  const ep = await D.epoch();
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Mutation surface' }), empty('No current epoch.')]);
    return;
  }
  const epochId = p.epochId || ep.epoch_id;
  const pinned = p.mutId || null;

  const mut = await D.mutations(ep.epoch_id);
  const gens = (mut && Array.isArray(mut.generations)) ? mut.generations : [];
  const sites = (mut && Array.isArray(mut.mutations)) ? mut.mutations : [];
  const patchedBySite = new Map();
  for (const s of sites) patchedBySite.set(s.mutation_id, new Set(Array.isArray(s.patched_generation_ids) ? s.patched_generation_ids : []));

  const pinnedSite = pinned ? sites.find((s) => s.mutation_id === pinned) : null;
  let detail = null;
  const patchesByGen = new Map();
  if (pinnedSite) {
    detail = await D.mutationDetail(ep.epoch_id, pinned);
    const touched = [...(patchedBySite.get(pinned) || [])];
    const all = await Promise.all(touched.map((g) => D.patches(ep.epoch_id, g)));
    touched.forEach((g, i) => patchesByGen.set(g, (all[i] && Array.isArray(all[i].patches)) ? all[i].patches : []));
  }
  const baselineStr = (detail && detail.baseline && typeof detail.baseline.content === 'string') ? detail.baseline.content : null;

  const digest = JSON.stringify({
    epochId, pinned, gens,
    sites: sites.map((s) => [s.mutation_id, s.role, [...(patchedBySite.get(s.mutation_id) || [])]]),
    baseLen: baselineStr == null ? -1 : baselineStr.length,
    patched: [...patchesByGen.entries()].map(([g, ps]) => [g, ps.map((x) => [x.mutation_id, (x.new_content || '').length])]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: 'Mutation surface' }),
      el('p', { class: 'dn-lede', text: 'Which mutation sites each generation patched — and exactly what changed, baseline against challenger, side by side.' }),
    ]));
    if (!sites.length || !gens.length) {
      nodes.push(section('Surface', el('div', { class: 'dn-panel' }, [empty('No mutation surface for this epoch.')])));
      return nodes;
    }
    const combined = el('div', { class: 'dn-mut-combined' }, [
      el('div', { class: 'dn-panel dn-mut-matrix', style: 'overflow-x:auto;' }, [matrixTable(sites, gens, patchedBySite, pinned, ctx, epochId)]),
      el('div', { class: 'dn-panel dn-mut-detail' }, [detailPane(pinnedSite, baselineStr, detail, patchesByGen)]),
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
    el('a', { class: 'dn-mtx-genlink', href: ctx.href('candidate', { epochId, gen: g }), text: g,
      onclick: (ev) => { ev.preventDefault(); ctx.navigate('candidate', { epochId, gen: g }); } }),
  ]));
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = el('tbody');
  for (const s of sites) {
    const touched = patchedBySite.get(s.mutation_id) || new Set();
    const isPinned = pinned === s.mutation_id;
    const tr = el('tr', { class: 'dn-mtx-row' + (isPinned ? ' dn-mtx-pinned' : '') });
    tr.appendChild(el('th', { class: 'dn-mtx-site', scope: 'row' }, [
      el('a', { class: 'dn-mtx-sitelink', href: ctx.href('mutations', { epochId, mutId: s.mutation_id }), title: s.mutation_id,
        onclick: (ev) => { ev.preventDefault(); ctx.navigate('mutations', { epochId, mutId: s.mutation_id }); } }, [
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
        td.appendChild(el('a', { class: 'dn-mtx-celllink', href: ctx.href('mutations', { epochId, mutId: s.mutation_id }), title: `${g} patched ${s.mutation_id}`,
          onclick: (ev) => { ev.preventDefault(); ctx.navigate('mutations', { epochId, mutId: s.mutation_id }); } }, [dot]));
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
  wrap.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:10px 0 0;', text: 'row = mutation site · column = generation · ▪ = patched here · click a cell or site → its side-by-side diff' }));
  return wrap;
}

function detailPane(site, baselineStr, detail, patchesByGen) {
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
    const patch = patches.find((x) => x.mutation_id === site.mutation_id || x.id === site.mutation_id);
    let newStr = patch && patch.new_content != null ? String(patch.new_content) : null;
    if (newStr == null && detail && Array.isArray(detail.versions)) {
      const v = detail.versions.find((x) => x.generation_id === g);
      if (v && typeof v.content === 'string') newStr = v.content;
    }
    if (newStr == null) continue;
    any = true;
    pane.appendChild(genDiffBlock(g, patch, baselineStr == null ? '' : baselineStr, newStr));
  }
  if (!any) pane.appendChild(el('p', { class: 'dn-empty', text: 'No generation patched this site (or the patch payloads are unavailable).' }));
  return pane;
}

function genDiffBlock(gen, patch, baselineStr, newStr) {
  const block = el('div', { class: 'dn-patch-block' });
  const op = String((patch && patch.op) || 'replace');
  block.appendChild(el('div', { class: 'dn-patch-head' }, [
    el('span', { class: 'dn-mono', text: gen }),
    el('span', { class: 'dn-patch-op dn-mono', text: op }),
  ]));
  const rationale = patch && patch.rationale ? String(patch.rationale).trim() : '';
  if (rationale) block.appendChild(el('p', { class: 'dn-patch-why' }, [el('span', { class: 'dn-patch-why-lead', text: 'Why. ' }), rationale]));
  block.appendChild(svg.sideBySideDiff({
    baseline: baselineStr, challenger: newStr,
    leftLabel: 'champion baseline · v0', rightLabel: `challenger new · ${gen}`,
  }));
  return block;
}

function fileLine(s) {
  const f = s.file || '?';
  const a = s.line_start; const b = s.line_end;
  if (a != null && b != null && a !== b) return `${f}:${a}–${b}`;
  if (a != null) return `${f}:${a}`;
  return f;
}

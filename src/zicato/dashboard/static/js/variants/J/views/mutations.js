// variants/J/views/mutations.js — NEW VIEW: mutation sites × generation.
//
// The convergence brief's first new screen (E lacked it). A dense matrix:
// rows = mutation SITES (file:line + role), columns = GENERATIONS; a cell is
// filled when that generation patched that site. Click a filled cell (or a
// row) to drill into the patch diff that generation applied there — rendered
// as a themed line diff (the patch's new_content, the realized change).
//
// Bind:
//   /api/mutations/{epoch_id} → { generations, mutations:[{mutation_id, kind,
//     file, role, line_start, line_end, patched_by, patched_generation_ids}] }
//   /api/files/{epoch}/{gen}/patches → { patches:[{id, mutation_id, op,
//     new_content, new_numeric, new_enum, rationale}] }
//
// The pinned site lives in the URL (#/J/mutations/<mutId>) so the drill-down
// rebuilds ONLY on a route change, never on a heartbeat.

import { el } from '../../../core/dom.js';
import { svgEl } from '../../../core/dom.js';
import * as D from '../data.js';
import { gatedSwap, section, empty, stat } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dj-empty', text: 'Reading mutation surface…' }));
  const pinned = params && params.mutId;

  const ep = await D.epoch();
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dj-h1', text: 'Mutation sites' }), empty('No current epoch.')]);
    return;
  }
  const epochId = ep.epoch_id;

  const mut = await D.mutations(epochId);
  const gens = (mut && Array.isArray(mut.generations)) ? mut.generations : [];
  const sites = (mut && Array.isArray(mut.mutations)) ? mut.mutations : [];

  // Per-generation patches (cached) so a cell can drill to the actual diff.
  const patchesByGen = new Map();
  if (gens.length) {
    const all = await Promise.all(gens.map((g) => D.patches(epochId, g)));
    gens.forEach((g, i) => patchesByGen.set(g, (all[i] && Array.isArray(all[i].patches)) ? all[i].patches : []));
  }

  // index: which generations patched each site.
  const patchedBySite = new Map();
  for (const s of sites) patchedBySite.set(s.mutation_id, new Set(Array.isArray(s.patched_generation_ids) ? s.patched_generation_ids : []));

  // the pinned site's patch per generation (for the drill diff).
  const pinnedSite = pinned ? sites.find((s) => s.mutation_id === pinned) : null;

  const digest = JSON.stringify({
    epochId, gens,
    sites: sites.map((s) => [s.mutation_id, s.file, s.role, s.line_start, s.line_end, (s.patched_generation_ids || []).join(',')]),
    pinned: pinned || null,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dj-pagehead' }, [
      el('h1', { class: 'dj-h1', text: 'Mutation sites × generation' }),
      el('p', { class: 'dj-lede', text: 'Every enumerated mutation point (a `# zicato:mutable` region) and which generation patched it. A filled cell = that generation changed that site; click it for the patch diff.' }),
    ]));

    nodes.push(el('div', { class: 'dj-panel dj-row' }, [
      stat(String(sites.length), 'mutation sites'),
      stat(String(gens.length), 'generations'),
      stat(String(sites.filter((s) => (s.patched_generation_ids || []).length).length), 'sites touched'),
    ]));

    if (!sites.length || !gens.length) {
      nodes.push(section('Surface', el('div', { class: 'dj-panel' }, [empty('No mutation surface for this epoch (the enumeration may be missing).')])));
      return nodes;
    }

    // ---- the matrix ----
    nodes.push(section('Site × generation matrix', matrix(sites, gens, patchedBySite, pinned, ctx)));

    // ---- drill: the pinned site's patch diff(s) ----
    if (pinnedSite) nodes.push(siteDrilldown(pinnedSite, gens, patchesByGen, ctx));
    return nodes;
  });
}

function matrix(sites, gens, patchedBySite, pinned, ctx) {
  const card = el('div', { class: 'dj-panel', style: 'overflow-x:auto;' });
  const table = el('table', { class: 'dj-mtx' });
  const thead = el('thead');
  const hr = el('tr');
  hr.appendChild(el('th', { class: 'dj-mtx-corner', text: 'site (file:line · role)' }));
  for (const g of gens) hr.appendChild(el('th', { class: 'dj-mtx-gen' }, [
    el('a', { class: 'dj-mtx-genlink', href: ctx.href('candidate', { gen: g }), text: g }),
  ]));
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = el('tbody');
  for (const s of sites) {
    const touched = patchedBySite.get(s.mutation_id) || new Set();
    const isPinned = pinned === s.mutation_id;
    const tr = el('tr', { class: 'dj-mtx-row' + (isPinned ? ' dj-mtx-pinned' : '') });
    const rowLabel = el('th', { class: 'dj-mtx-site', scope: 'row' }, [
      el('a', {
        class: 'dj-mtx-sitelink', href: ctx.href('mutations', { mutId: s.mutation_id }),
        title: s.mutation_id,
      }, [
        el('span', { class: 'dj-mtx-file', text: fileLine(s) }),
        el('span', { class: 'dj-mtx-role', text: s.role || s.kind || '' }),
      ]),
    ]);
    tr.appendChild(rowLabel);
    for (const g of gens) {
      const on = touched.has(g);
      const td = el('td', { class: 'dj-mtx-cell' + (on ? ' dj-mtx-on' : '') });
      if (on) {
        const dot = svgEl('svg', { class: 'dj-mtx-mark', width: 16, height: 16, viewBox: '0 0 16 16', role: 'img' }, [
          svgEl('rect', { x: 3, y: 3, width: 10, height: 10, rx: 2, class: 'dj-mtx-square' }),
        ]);
        const btn = el('a', {
          class: 'dj-mtx-celllink', href: ctx.href('mutations', { mutId: s.mutation_id }),
          title: `${g} patched ${s.mutation_id}`,
        }, [dot]);
        td.appendChild(btn);
      } else {
        td.appendChild(el('span', { class: 'dj-mtx-blank', 'aria-hidden': 'true', text: '·' }));
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  card.appendChild(table);
  card.appendChild(el('p', { class: 'dj-faint', style: 'font-size:11px;margin:10px 0 0;', text: 'row = mutation site · column = generation · ▪ = patched here · click a cell or a site → the patch diff' }));
  return card;
}

function siteDrilldown(site, gens, patchesByGen, ctx) {
  const card = el('div', { class: 'dj-panel dj-drill' });
  card.appendChild(el('div', { class: 'dj-mtx-drillhead' }, [
    el('span', { class: 'dj-mono', text: site.mutation_id }),
    el('span', { class: 'dj-faint dj-mono', text: ' · ' + fileLine(site) + (site.role ? ' · ' + site.role : '') }),
  ]));

  const touched = new Set(Array.isArray(site.patched_generation_ids) ? site.patched_generation_ids : []);
  let any = false;
  for (const g of gens) {
    if (!touched.has(g)) continue;
    const patches = patchesByGen.get(g) || [];
    const patch = patches.find((p) => p.mutation_id === site.mutation_id || p.id === site.mutation_id);
    if (!patch) continue;
    any = true;
    card.appendChild(patchBlock(g, patch, ctx));
  }
  if (!any) card.appendChild(empty('No generation patched this site (or the patch payloads are unavailable).'));
  return section('Patch · ' + site.mutation_id, card);
}

function patchBlock(gen, patch, ctx) {
  const op = String(patch.op || 'replace');
  const head = el('div', { class: 'dj-patch-head' }, [
    el('a', { class: 'dj-mtx-genlink', href: ctx.href('candidate', { gen }), text: gen }),
    el('span', { class: 'dj-patch-op dj-mono', text: op }),
  ]);
  const block = el('div', { class: 'dj-patch-block' }, [head]);
  if (patch.rationale && String(patch.rationale).trim()) {
    block.appendChild(el('p', { class: 'dj-patch-why' }, [el('span', { class: 'dj-patch-why-lead', text: 'Why. ' }), String(patch.rationale).trim()]));
  }
  if (op === 'set_numeric' && patch.new_numeric != null) {
    block.appendChild(scalarSet('new value', String(patch.new_numeric)));
  } else if (op === 'set_enum' && patch.new_enum != null) {
    block.appendChild(scalarSet('new value', String(patch.new_enum)));
  } else {
    // a text replace: render the new content as an all-added themed diff
    // (parent unavailable from /patches — shown as added, honestly labelled).
    block.appendChild(el('p', { class: 'dj-patch-note dj-faint', text: 'new instruction text (parent unavailable from this endpoint — shown as added)' }));
    block.appendChild(addedDiff(patch.new_content == null ? '' : String(patch.new_content)));
  }
  return block;
}

function scalarSet(label, value) {
  return el('div', { class: 'dj-patch-scalar' }, [
    el('span', { class: 'dj-patch-scalar-label', text: label }),
    el('span', { class: 'dj-patch-scalar-val dj-mono', text: value }),
  ]);
}

function addedDiff(text) {
  const body = el('div', { class: 'dj-diff-body', role: 'list' });
  const lines = String(text).replace(/\r\n/g, '\n').split('\n');
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
  lines.forEach((ln, i) => {
    body.appendChild(el('div', { class: 'dj-diff-line dj-diff-add', role: 'listitem' }, [
      el('span', { class: 'dj-diff-gutter', 'aria-hidden': 'true', text: String(i + 1) }),
      el('span', { class: 'dj-diff-sign', 'aria-hidden': 'true', text: '+' }),
      el('span', { class: 'dj-diff-text', text: ln === '' ? '​' : ln }),
    ]));
  });
  return body;
}

function fileLine(s) {
  const f = s.file || '?';
  const a = s.line_start;
  const b = s.line_end;
  if (a != null && b != null && a !== b) return `${f}:${a}–${b}`;
  if (a != null) return `${f}:${a}`;
  return f;
}

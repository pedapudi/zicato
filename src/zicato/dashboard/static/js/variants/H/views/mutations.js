// variants/H/views/mutations.js — MUTATION SITES × GENERATIONS (new in H).
//
// The mutation surface E lacked: a matrix of every enumerated mutation site
// (rows = `file:line` + role) against every generation (cols), a filled cell
// where that generation patched that site. Click a patched cell to drill into
// the realized patch diff for that (site, generation) pair.
//
// Data:
//   GET /api/mutations/{epoch_id} → { generations:[…], mutations:[{mutation_id,
//     kind, file, role, line_start, line_end, patched_by:[{generation_id,
//     patch_id, op, rationale}], patched_generation_ids}] }
//   GET /api/files/{epoch}/{gen}/patches → { patches:[{id, mutation_id, op,
//     new_content, new_numeric, new_enum, rationale}] } — what each gen changed.
//
// The drilled site lives in the URL (route param mutationId), so the drill-down
// rebuilds only on a route change, never on a heartbeat — the gatedSwap digest
// carries the selection.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import { mutationPatchCard } from '../patchDiff.js';
import { gatedSwap, section, empty } from '../ui.js';

function shortFile(f) {
  const s = String(f || '');
  const parts = s.split('/');
  return parts.length > 2 ? '…/' + parts.slice(-2).join('/') : s;
}

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'd-empty', text: 'Reading mutation surface…' }));
  const selected = params && params.mutationId;

  const ep = await D.epoch();
  const epochId = (ep && ep.epoch_id) || null;
  if (!epochId) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'd-h1', text: 'Mutation sites' }), empty('No current epoch.')]);
    return;
  }

  const mut = await D.mutations(epochId);
  const sites = (mut && Array.isArray(mut.mutations)) ? mut.mutations : [];
  const genIds = (mut && Array.isArray(mut.generations)) ? mut.generations.slice() : [];
  // The mutation index lists v0 first; v0 is the seed (no patches).
  const cols = genIds.filter((g) => g !== 'v0');

  // For the selected site, fetch every generation's patch set and pull the
  // patches that target this site (so we render WHAT each gen changed).
  let detailRows = null;
  if (selected) {
    const site = sites.find((s) => s.mutation_id === selected) || null;
    const touchedBy = site && Array.isArray(site.patched_generation_ids) ? site.patched_generation_ids : [];
    const patchSets = await Promise.all(touchedBy.map((g) => D.patches(epochId, g)));
    detailRows = [];
    touchedBy.forEach((g, i) => {
      const ps = patchSets[i];
      const list = (ps && Array.isArray(ps.patches)) ? ps.patches : [];
      for (const p of list) {
        if (p && p.mutation_id === selected) detailRows.push({ gen: g, patch: p, site });
      }
    });
  }

  const digest = JSON.stringify({
    epochId, selected: selected || null,
    cols,
    sites: sites.map((s) => [s.mutation_id, s.file, s.role, s.line_start, s.line_end,
      (s.patched_generation_ids || []).slice().sort()]),
    detail: detailRows ? detailRows.map((d) => [d.gen, d.patch.id, d.patch.op,
      (d.patch.new_content || '').length, d.patch.new_numeric ?? null, d.patch.new_enum ?? null]) : null,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'e-pagehead' }, [
      el('h1', { class: 'd-h1', text: 'Mutation sites × generations' }),
      el('p', { class: 'd-lede', text: 'Every enumerated mutation point the proposer was offered (rows) against every challenger generation (columns). A filled cell marks a patch at that site; click it to read the realized patch diff.' }),
    ]));

    if (!sites.length) {
      nodes.push(el('div', { class: 'd-panel' }, [empty(mut && mut.error ? mut.error : 'No mutation surface for this epoch (the baseline enumeration may be missing).')]));
      return nodes;
    }

    nodes.push(section('Mutation surface · ' + sites.length + ' site' + (sites.length === 1 ? '' : 's') + ' × ' + cols.length + ' challenger' + (cols.length === 1 ? '' : 's'),
      matrix(sites, cols, selected, ctx)));

    if (selected) {
      const card = el('div', { class: 'd-panel hm-patch-detail' });
      if (detailRows && detailRows.length) {
        for (const d of detailRows) {
          card.appendChild(mutationPatchCard(d.patch, {
            generationId: d.gen,
            path: d.site ? shortFile(d.site.file) + ':' + d.site.line_start : null,
          }));
        }
      } else {
        card.appendChild(empty('No recorded patch content for this site (no generation patched it, or the patch set is unavailable).'));
      }
      nodes.push(section('Patch diff · ' + selected, card));
    }
    return nodes;
  });
}

function matrix(sites, cols, selected, ctx) {
  const wrap = el('div', { class: 'd-panel' });
  const scroll = el('div', { class: 'hm-matrix-wrap' });
  const table = el('table', { class: 'hm-matrix' });

  const thead = el('thead');
  const hrow = el('tr', null, [el('th', { class: 'hm-site-head', text: 'mutation site' })]);
  for (const c of cols) hrow.appendChild(el('th', { class: 'hm-gen-head', text: c }));
  thead.appendChild(hrow);
  table.appendChild(thead);

  const tbody = el('tbody');
  for (const s of sites) {
    const touched = new Set(s.patched_generation_ids || []);
    const siteCell = el('td', { class: 'hm-site-cell' }, [
      el('div', { class: 'hm-site-id d-mono', text: s.mutation_id }),
      el('div', { class: 'hm-site-file', text: shortFile(s.file) + ':' + s.line_start + '–' + s.line_end }),
      s.role ? el('div', { class: 'hm-site-role', text: s.role }) : null,
    ].filter(Boolean));
    const row = el('tr', null, [siteCell]);
    for (const c of cols) {
      const patched = touched.has(c);
      const isSel = patched && selected === s.mutation_id;
      const cell = el('td', {
        class: 'hm-cell ' + (patched ? 'hm-patched' : 'hm-empty') + (isSel ? ' hm-selected' : ''),
        title: patched ? `${c} patched ${s.mutation_id}` : `${c} did not patch ${s.mutation_id}`,
      }, [el('span', { class: 'hm-dot' })]);
      if (patched) {
        cell.addEventListener('click', () => ctx.navigate('mutations', { mutationId: s.mutation_id }));
      }
      row.appendChild(cell);
    }
    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  scroll.appendChild(table);
  wrap.appendChild(scroll);
  wrap.appendChild(el('p', { class: 'd-faint', style: 'font-size:11px;margin:10px 0 0;', text: 'filled = a generation patched that site · click a filled cell → its patch diff' }));
  return wrap;
}

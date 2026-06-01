// variants/I/views/mutations.js — MUTATION SURFACE (NEW, round-3).
//
// E lacked this. Ledger renders the epoch's mutation surface as a
// mutation-site × generation MATRIX: which sites each generation patched
// (a site = `file:line` + role), with drill-down to the patch diff. The
// matrix is laid out to fit the container (a constrained-scroll x-rail for
// many generations), NO pan/zoom viewport. Selecting a site (a row link)
// drills to that site's per-generation diff via /api/files patches and the
// mutation detail endpoint.
//
// Data:
//   /api/mutations/{epoch}                 → {generations, mutations[{mutation_id,
//                                             file, role, line_start, line_end,
//                                             patched_generation_ids}]}
//   /api/files/{epoch}/{gen}/patches       → {patches:[{id, mutation_id, op,
//                                             new_content}]} (what each gen changed)
//   /api/mutations/{epoch}/{mutation_id}   → one site's baseline + patched content
//   /api/contract-diff/{epoch}             → illustrative contract context

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import { gatedSwap, section, empty, pageHead } from '../ui.js';

function shortFile(f) {
  const s = String(f || '');
  const parts = s.split('/');
  return parts.length > 2 ? '…/' + parts.slice(-2).join('/') : s;
}

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'd-empty', text: 'Enumerating mutation surface…' }));

  const ep = await D.epoch();
  const epochId = ep && ep.epoch_id;
  if (!epochId) {
    gatedSwap(host, 'no-epoch', () => [pageHead('Mutation surface', 'Mutation surface', ''), empty('No current epoch.')]);
    return;
  }

  const mut = await D.mutations(epochId);
  const sites = (mut && Array.isArray(mut.mutations)) ? mut.mutations : [];
  const gens = (mut && Array.isArray(mut.generations)) ? mut.generations.slice() : [];
  // v0 (seed) never patches anything; keep it as a column for completeness
  // but it always reads empty.
  const genIds = gens.length ? gens : [...new Set(sites.flatMap((s) => s.patched_generation_ids || []))];

  // Fetch the per-generation patch sets so a cell knows the patch id +
  // op for the drill-down (the index only carries the generation list).
  const patchSets = await Promise.all(genIds.map((g) => D.patches(epochId, g)));
  // map: mutationId -> generationId -> patch
  const patchByMutGen = new Map();
  genIds.forEach((g, i) => {
    const ps = patchSets[i];
    const list = ps && Array.isArray(ps.patches) ? ps.patches : [];
    for (const p of list) {
      if (!p || p.mutation_id == null) continue;
      if (!patchByMutGen.has(p.mutation_id)) patchByMutGen.set(p.mutation_id, new Map());
      patchByMutGen.get(p.mutation_id).set(g, p);
    }
  });

  const selectedId = params && params.mutationId;
  let detail = null;
  if (selectedId) detail = await D.mutationDetail(epochId, selectedId);

  const digest = JSON.stringify({
    epochId, genIds,
    sites: sites.map((s) => [s.mutation_id, s.file, s.role, s.line_start, (s.patched_generation_ids || []).slice().sort()]),
    sel: selectedId || null,
    detail: detail ? (Array.isArray(detail.generations) ? detail.generations.map((d) => [d.generation_id, (d.content || '').length]) : (detail.error || 'd')) : null,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(pageHead('Mutation surface · ' + epochId, 'What each generation changed',
      'Every `# zicato:mutable` site in the epoch’s baseline, against the generations that patched it. A filled cell means that generation edited that site; select a site to read its patch diff.'));

    if (mut && mut.error) {
      nodes.push(el('div', { class: 'd-panel' }, [empty(mut.error)]));
      return nodes;
    }
    if (!sites.length) {
      nodes.push(el('div', { class: 'd-panel' }, [empty('No mutation sites enumerated for this epoch (the baseline surface may be empty).')]));
      return nodes;
    }

    // ---- the matrix ----
    const card = el('div', { class: 'd-panel' });
    const scroll = el('div', { class: 'i-scroll-x' });
    const table = el('table', { class: 'i-mut-matrix' });
    const thead = el('thead', null, [
      el('tr', null, [
        el('th', { class: 'i-mut-site-head', text: 'mutation site' }),
        ...genIds.map((g) => el('th', { class: 'i-mut-gen-head', text: g })),
      ]),
    ]);
    table.appendChild(thead);
    const tbody = el('tbody');
    for (const s of sites) {
      const patched = new Set(s.patched_generation_ids || []);
      const siteCell = el('td', { class: 'i-mut-site' }, [
        el('a', {
          class: 'i-mut-site-link' + (selectedId === s.mutation_id ? ' i-on' : ''),
          href: ctx.href('mutations', { mutationId: s.mutation_id }),
        }, [
          el('span', { class: 'i-mut-file d-mono', text: shortFile(s.file) + ':' + (s.line_start ?? '?') }),
          s.role ? el('span', { class: 'i-mut-role', text: s.role }) : null,
        ].filter(Boolean)),
      ]);
      const row = el('tr', { class: 'i-mut-row' + (selectedId === s.mutation_id ? ' i-on' : '') }, [
        siteCell,
        ...genIds.map((g) => {
          const did = patched.has(g);
          const p = patchByMutGen.get(s.mutation_id) && patchByMutGen.get(s.mutation_id).get(g);
          const op = p ? (p.op || 'edit') : null;
          const cell = el('td', { class: 'i-mut-cell' + (did ? ' i-patched' : '') }, [
            did ? el('span', { class: 'i-mut-dot', title: g + ' patched ' + s.mutation_id + (op ? ' (' + op + ')' : '') }) : el('span', { class: 'i-mut-empty', text: '·' }),
          ]);
          if (did) {
            cell.style.cursor = 'pointer';
            cell.addEventListener('click', () => ctx.navigate('mutations', { mutationId: s.mutation_id }));
          }
          return cell;
        }),
      ]);
      tbody.appendChild(row);
    }
    table.appendChild(tbody);
    scroll.appendChild(table);
    card.appendChild(scroll);
    card.appendChild(el('p', { class: 'i-figcap', text: 'Rows are mutation sites (file:line · role); columns are generations. A filled cell is a patch; click it (or the site name) to read the diff.' }));
    nodes.push(section('Mutation sites × generations', card));

    // ---- patch-diff drill-down ----
    if (selectedId) {
      nodes.push(patchDrill(selectedId, detail, sites.find((s) => s.mutation_id === selectedId), patchByMutGen.get(selectedId)));
    }
    return nodes;
  });
}

function patchDrill(mutationId, detail, site, patchByGen) {
  const card = el('div', { class: 'd-panel i-mut-drill' });
  if (site) {
    card.appendChild(el('div', { class: 'i-mut-drill-head' }, [
      el('span', { class: 'd-mono', text: site.file + ':' + (site.line_start ?? '?') + (site.line_end != null ? '–' + site.line_end : '') }),
      site.role ? el('span', { class: 'i-mut-role', text: site.role }) : null,
      site.kind ? el('span', { class: 'd-faint', text: ' · ' + site.kind }) : null,
    ].filter(Boolean)));
  }

  // The detail endpoint carries baseline + per-generation patched content.
  const baseline = detail && (detail.baseline_content != null ? detail.baseline_content : detail.baseline) || null;
  const genContents = detail && Array.isArray(detail.generations) ? detail.generations : [];

  if (baseline != null) {
    card.appendChild(el('p', { class: 'i-figcap', text: 'Baseline (v0) content of this site:' }));
    card.appendChild(el('pre', { class: 'i-patch-pre i-patch-base' }, [el('code', { text: String(baseline) })]));
  }
  if (genContents.length) {
    for (const gc of genContents) {
      const gid = gc.generation_id || gc.id;
      const content = gc.content != null ? gc.content : gc.new_content;
      card.appendChild(el('p', { class: 'i-figcap', text: gid + ' patched this site to:' }));
      card.appendChild(el('pre', { class: 'i-patch-pre i-patch-new' }, [el('code', { text: String(content == null ? '(empty)' : content) })]));
    }
  } else if (patchByGen && patchByGen.size) {
    // Fall back to the patches endpoint's new_content when the detail
    // endpoint did not return per-generation content.
    for (const [gid, p] of patchByGen.entries()) {
      card.appendChild(el('p', { class: 'i-figcap', text: gid + ' · ' + (p.op || 'edit') + ':' }));
      card.appendChild(el('pre', { class: 'i-patch-pre i-patch-new' }, [el('code', { text: String(p.new_content == null ? '(no content)' : p.new_content) })]));
    }
  } else if (baseline == null) {
    card.appendChild(empty('No patch content available for this site (it may be unpatched, or the detail is missing).'));
  }
  return section('Patch diff · ' + mutationId, card);
}

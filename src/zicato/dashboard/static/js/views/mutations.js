// js/views/mutations.js — ONE cohesive visual: the mutation surface
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
//
// PROVENANCE (issue #194 §6). A closed epoch's snapshot trees get pruned; the
// records do not. The server then reconstructs this surface from
// epochs/{id}/mutations.json + the patch records and says so on the payload —
// `provenance` ("snapshot" | "records") and `provenance_note`, the caption. The
// note is rendered VERBATIM, never re-worded here: the server knows WHY the
// tree is missing (pruned vs unreachable) and this view does not.

import { el, svgEl } from '../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { section, empty, stat, renderView } from '../ui.js';

export async function render(host, ctx, params) {
  const pinned = params && params.mutId;
  // The two-affordance selection. A bare mutId pins the SITE (the whole row →
  // ALL generations that patched it, stacked). A mutId PLUS a gen pins ONE cell
  // (that single challenger's side-by-side diff for that site).
  const pinnedGen = (params && params.gen) || null;
  // Class A: the mutation surface is keyed by the VIEWED epoch (route param
  // first), so opening a non-current epoch reads ITS surface.
  const routeEpoch = (params && params.epochId) || null;

  await renderView(host, ctx, {
    loading: 'Reading mutation surface…',
    epoch: true, routeEpoch, title: 'Mutation surface',
    load: async ({ epochId }) => {
      const mut = await D.mutations(epochId);
      // Generation columns in CREATION order (v0, v1, … v9, v10, v11), not the
      // lexical string order the raw id list sorts to — the numeric vN suffix
      // IS the mint order.
      const genNum = (g) => { const m = /(\d+)\s*$/.exec(String(g || '')); return m ? parseInt(m[1], 10) : Number.MAX_SAFE_INTEGER; };
      const gens = ((mut && Array.isArray(mut.generations)) ? mut.generations : [])
        .slice().sort((a, b) => genNum(a) - genNum(b) || String(a).localeCompare(String(b)));
      const sites = (mut && Array.isArray(mut.mutations)) ? mut.mutations : [];

      const patchedBySite = new Map();
      for (const s of sites) patchedBySite.set(s.mutation_id, new Set(Array.isArray(s.patched_generation_ids) ? s.patched_generation_ids : []));

      // The pinned site → its baseline STRING (one call) + per-generation
      // patches. A single CELL (pinnedGen) fetches ONLY that generation's
      // patches; the SITE row fetches every generation that patched it.
      const pinnedSite = pinned ? sites.find((s) => s.mutation_id === pinned) : null;
      let detail = null;
      const patchesByGen = new Map();
      if (pinnedSite) {
        detail = await D.mutationDetail(epochId, pinned);
        const allTouched = [...(patchedBySite.get(pinned) || [])];
        const touched = pinnedGen
          ? allTouched.filter((g) => String(g) === String(pinnedGen))
          : allTouched;
        const all = await Promise.all(touched.map((g) => D.patches(epochId, g)));
        touched.forEach((g, i) => patchesByGen.set(g, (all[i] && Array.isArray(all[i].patches)) ? all[i].patches : []));
      }
      // baseline content (STRING) — never the object.
      const baselineStr = (detail && detail.baseline && typeof detail.baseline.content === 'string')
        ? detail.baseline.content : null;
      // The server's own words for where this came from — the site index's
      // caption, or the pinned site's (which also covers a reconstructed
      // VERSION under an intact baseline).
      const note = String((detail && detail.provenance_note) || (mut && mut.provenance_note) || '');
      // The label for the diff's LEFT column. `null` on the records path: the
      // frozen enumeration is the round's champion surface, and calling it v0
      // would be a guess.
      const baselineGen = (detail && detail.baseline && detail.baseline.generation_id) || null;
      const surfaceError = String((mut && mut.error) || '');
      return { epochId, gens, sites, patchedBySite, pinnedSite, detail, patchesByGen, baselineStr, note, baselineGen, surfaceError };
    },
    digest: (d) => JSON.stringify({
      epochId: d.epochId, gens: d.gens,
      sites: d.sites.map((s) => [s.mutation_id, s.file, s.role, s.line_start, s.line_end, (s.patched_generation_ids || []).join(',')]),
      pinned: pinned || null,
      pinnedGen: pinnedGen || null,
      baselineLen: d.baselineStr == null ? -1 : d.baselineStr.length,
      patched: d.pinnedSite ? [...d.patchesByGen.keys()] : null,
      note: d.note, baselineGen: d.baselineGen, surfaceError: d.surfaceError,
      versions: (d.detail && Array.isArray(d.detail.versions))
        ? d.detail.versions.map((v) => [v.generation_id, v.provenance, v.note || v.error || '',
          v.content == null ? -1 : v.content.length])
        : null,
    }),
    build: (d) => {
      const { epochId, gens, sites, patchedBySite, pinnedSite, detail, patchesByGen, baselineStr, note, baselineGen, surfaceError } = d;
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: 'Mutation surface · site × generation' }),
      el('p', { class: 'dn-lede', text: 'Every enumerated mutation point (a `# zicato:mutable` region) and which generation patched it. Click a ▪ CELL for ONE generation’s side-by-side patch diff at that site; click the SITE row label for ALL generations that patched it, stacked — champion baseline against each challenger’s new content.' }),
    ]));
    if (note) nodes.push(el('p', { class: 'dn-faint dn-mut-prov', text: note }));

    nodes.push(el('div', { class: 'dn-panel dn-row' }, [
      stat(String(sites.length), 'mutation sites'),
      stat(String(gens.length), 'generations'),
      stat(String(sites.filter((s) => (s.patched_generation_ids || []).length).length), 'sites touched'),
    ]));

    if (!sites.length || !gens.length) {
      // Say WHICH read came back empty — the server's error names both the
      // absent tree and the absent record.
      nodes.push(section('Surface', el('div', { class: 'dn-panel' }, [empty(surfaceError || 'No mutation surface for this epoch (the enumeration may be missing).')])));
      return nodes;
    }

    // ONE cohesive layout: the matrix and the detail pane in a single section.
    const combined = el('div', { class: 'dn-mut-combined' }, [
      el('div', { class: 'dn-panel dn-mut-matrix' }, [matrixTable(sites, gens, patchedBySite, pinned, pinnedGen, ctx, epochId)]),
      el('div', { class: 'dn-panel dn-mut-detail' }, [detailPane(pinnedSite, pinnedGen, baselineStr, detail, patchesByGen, ctx, epochId, baselineGen)]),
    ]);
    nodes.push(section('Mutation surface + side-by-side diff', combined));
    return nodes;
    },
  });
}

function matrixTable(sites, gens, patchedBySite, pinned, pinnedGen, ctx, epochId) {
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
    // the SITE row is "pinned" (all-gens view) only when the row itself is the
    // selection — i.e. a mutId with NO cell-level gen.
    const sitePinned = isPinned && !pinnedGen;
    const tr = el('tr', { class: 'dn-mtx-row' + (isPinned ? ' dn-mtx-pinned' : '') });
    // The ROW LABEL is the "all mutations for this site" affordance: it links to
    // the bare mutId (no gen) → every generation that patched the site, stacked.
    tr.appendChild(el('th', { class: 'dn-mtx-site', scope: 'row' }, [
      el('a', {
        class: 'dn-mtx-sitelink' + (sitePinned ? ' dn-mtx-sitelink-on' : ''),
        href: ctx.href('mutations', { epochId, mutId: s.mutation_id }),
        title: `all generations that patched ${s.mutation_id}`,
        'aria-current': sitePinned ? 'true' : null,
      }, [
        el('span', { class: 'dn-mtx-file', text: fileLine(s) }),
        el('span', { class: 'dn-mtx-role', text: s.role || s.kind || '' }),
      ]),
    ]));
    for (const g of gens) {
      const on = touched.has(g);
      // a CELL is the "this one generation" affordance: it links to mutId+gen →
      // ONLY that single challenger's side-by-side diff for the site.
      const cellPinned = isPinned && String(pinnedGen) === String(g);
      const td = el('td', { class: 'dn-mtx-cell' + (on ? ' dn-mtx-on' : '') + (cellPinned ? ' dn-mtx-cell-pinned' : ''),
        'data-gen': g, 'data-site': s.mutation_id });
      if (on) {
        const dot = svgEl('svg', { class: 'dn-mtx-mark', width: 16, height: 16, viewBox: '0 0 16 16', role: 'img' }, [
          svgEl('rect', { x: 3, y: 3, width: 10, height: 10, rx: 2, class: 'dn-mtx-square' }),
        ]);
        td.appendChild(el('a', {
          class: 'dn-mtx-celllink', href: ctx.href('mutations', { epochId, mutId: s.mutation_id, gen: g }),
          title: `${g}’s patch at ${s.mutation_id} (this one generation’s diff)`,
          'aria-current': cellPinned ? 'true' : null,
        }, [dot]));
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
  wrap.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:10px 0 0;', text: 'row = mutation site · column = generation · ▪ = patched here · click a ▪ CELL → that ONE generation’s diff · click the SITE label → ALL generations that patched it' }));
  return wrap;
}

// The detail pane: fills on selection with the SIDE-BY-SIDE diff(s).
//   * a CELL (mutId + pinnedGen) → ONLY that one generation's diff;
//   * the SITE row (mutId, no pinnedGen) → ALL generations that patched it.
function detailPane(site, pinnedGen, baselineStr, detail, patchesByGen, ctx, epochId, baselineGen) {
  const pane = el('div');
  if (!site) {
    pane.appendChild(el('p', { class: 'dn-empty', text: 'Click a ▪ cell for ONE generation’s side-by-side diff at that site, or the site row label for ALL generations that patched it.' }));
    return pane;
  }
  const single = !!pinnedGen;
  pane.appendChild(el('div', { class: 'dn-mtx-drillhead' }, [
    el('span', { class: 'dn-mono', text: site.mutation_id }),
    el('span', { class: 'dn-faint dn-mono', text: ' · ' + fileLine(site) + (site.role ? ' · ' + site.role : '') }),
    el('span', { class: 'dn-mtx-scope', text: single ? `one generation · ${pinnedGen}` : 'all generations' }),
  ]));

  if (baselineStr == null) {
    pane.appendChild(el('p', { class: 'dn-patch-note dn-faint', text: 'No baseline content recorded for this site — the diff needs both sides.' }));
  }

  const versions = (detail && Array.isArray(detail.versions)) ? detail.versions : [];
  const touched = [...(patchesByGen.keys ? patchesByGen.keys() : [])];
  let any = false;
  for (const g of touched) {
    const patches = patchesByGen.get(g) || [];
    const patch = patches.find((p) => p.mutation_id === site.mutation_id || p.id === site.mutation_id);
    const version = versions.find((x) => x.generation_id === g) || null;
    // challenger new content (STRING) from /patches; fall back to the detail's
    // version content if the patches payload lacks it (a value op carries no
    // new_content, so the server's reconstruction is the only text there is).
    let newStr = patch && patch.new_content != null ? String(patch.new_content) : null;
    if (newStr == null && version && typeof version.content === 'string') newStr = version.content;
    if (newStr == null) {
      // No content for this generation — but the server said WHY (a value
      // whose constant sits outside the recorded span; a marker the tree no
      // longer enumerates). Print its reason rather than dropping the row.
      const why = version ? String(version.note || version.error || '') : '';
      if (!why) continue;
      any = true;
      pane.appendChild(genNoteBlock(g, patch, version, why, ctx, epochId));
      continue;
    }
    any = true;
    pane.appendChild(genDiffBlock(g, patch, baselineStr == null ? '' : baselineStr, newStr, site, ctx, epochId, baselineGen, version));
  }
  if (!any) {
    pane.appendChild(el('p', { class: 'dn-empty', text: single
      ? `Generation ${pinnedGen} did not patch this site (or its patch payload is unavailable).`
      : 'No generation patched this site (or the patch payloads are unavailable).' }));
  }
  return pane;
}

// The head every per-generation block shares: the generation, its op, and —
// when this ONE generation's content came from the records while another's did
// not — which of the two it is. Per block, because the mixed case is the COMMON
// one: GC never prunes v0, so an exact baseline routinely sits beside a
// reconstructed challenger.
function genBlockHead(gen, patch, version, ctx, epochId) {
  const op = String((patch && patch.op) || (version && version.op) || 'replace');
  const fromRecords = !!(version && version.provenance === 'records');
  return el('div', { class: 'dn-patch-head' }, [
    el('a', { class: 'dn-mtx-genlink', href: ctx.href('candidate', { epochId, gen }), text: gen }),
    el('span', { class: 'dn-patch-op dn-mono', text: op }),
    fromRecords ? el('span', { class: 'dn-faint dn-mono', text: ' · from records' }) : null,
  ].filter(Boolean));
}

// The patch's own reason for existing. Kept beside a block that has no
// content to show as much as one that does — "what was this trying to do"
// survives in the record even when the changed text does not.
function blockWhy(patch, version) {
  const rationale = String((patch && patch.rationale) || (version && version.rationale) || '').trim();
  if (!rationale) return null;
  return el('p', { class: 'dn-patch-why' }, [el('span', { class: 'dn-patch-why-lead', text: 'Why. ' }), rationale]);
}

function genNoteBlock(gen, patch, version, why, ctx, epochId) {
  const block = el('div', { class: 'dn-patch-block' });
  block.appendChild(genBlockHead(gen, patch, version, ctx, epochId));
  const why_ = blockWhy(patch, version);
  if (why_) block.appendChild(why_);
  block.appendChild(el('p', { class: 'dn-patch-note dn-faint', text: why }));
  return block;
}

function genDiffBlock(gen, patch, baselineStr, newStr, site, ctx, epochId, baselineGen, version) {
  const block = el('div', { class: 'dn-patch-block' });
  block.appendChild(genBlockHead(gen, patch, version, ctx, epochId));
  const why = blockWhy(patch, version);
  if (why) block.appendChild(why);
  block.appendChild(svg.sideBySideDiff({
    baseline: baselineStr,
    challenger: newStr,
    // Name the generation the left column actually IS. Without a tree the
    // server declines to name one, and so does the label.
    leftLabel: baselineGen ? `champion baseline · ${baselineGen}` : 'champion baseline · from records',
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

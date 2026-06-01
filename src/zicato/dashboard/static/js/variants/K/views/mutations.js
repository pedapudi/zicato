// variants/K/views/mutations.js — MUTATION SITES × GENERATION (the methods).

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
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'vk-h1', text: 'Mutation sites' }), empty('No current epoch.')]);
    return;
  }

  const [muts, lineage] = await Promise.all([D.mutations(epochId), D.lineage(epochId)]);
  const mutations = (muts && Array.isArray(muts.mutations)) ? muts.mutations : [];
  // Generation order from the surface payload, falling back to lineage.
  let genIds = (muts && Array.isArray(muts.generations) && muts.generations.length) ? muts.generations.slice() : [];
  const lineGens = (lineage && Array.isArray(lineage.generations)) ? lineage.generations : [];
  if (!genIds.length) genIds = lineGens.map((g) => g.generation_id);
  const promotedSet = new Set(lineGens.filter((g) => g.promoted).map((g) => g.generation_id));

  // selected generation → fetch its patches (drill-down).
  const selGen = params && params.gen;
  let patches = null;
  if (selGen) {
    const pp = await D.patches(epochId, selGen);
    patches = (pp && Array.isArray(pp.patches)) ? pp.patches : [];
  }

  const sites = mutations.map((m) => ({
    id: m.mutation_id,
    label: m.role ? `${m.role}` : (m.file || m.mutation_id),
    sub: siteSub(m),
    patched: new Set(m.patched_generation_ids || []),
  }));

  const digest = JSON.stringify({
    epochId, selGen,
    gens: genIds,
    sites: sites.map((s) => [s.id, s.label, [...s.patched]]),
    patches: patches ? patches.map((p) => [p.id, p.mutation_id, p.op]) : null,
  });

  gatedSwap(host, digest, () => {
    const out = [];
    out.push(el('div', { class: 'vk-pagehead' }, [
      el('h1', { class: 'vk-h1', text: 'Mutation sites' }),
      el('p', { class: 'vk-lede', text: 'Which mutation sites each generation patched — the methods appendix of the report.' }),
    ]));

    const gens = genIds.map((id) => ({ id, label: id, promoted: promotedSet.has(id) }));
    const matrixCard = el('div', { class: 'vk-panel' });
    if (sites.length && gens.length) {
      const patchedLookup = new Map();
      for (const s of sites) for (const g of s.patched) patchedLookup.set(s.id + ' ' + g, true);
      matrixCard.appendChild(svg.mutationMatrix({
        sites, gens,
        patched: (siteId, genId) => patchedLookup.has(siteId + ' ' + genId),
        onCell: (genId) => ctx.navigate('mutations', { gen: genId }),
      }));
      matrixCard.appendChild(el('p', { class: 'vk-faint vk-fignote', text: 'a filled cell = that generation patched that site · click to see what it changed' }));
    } else {
      matrixCard.appendChild(empty('No mutation surface recorded for this epoch.'));
    }
    out.push(section('Mutation surface · site × generation', matrixCard));

    // drill-down: the selected generation's patch diffs.
    if (selGen) out.push(section(`Patches · ${selGen}`, patchPanel(patches, sites)));

    return out;
  });
}

function patchPanel(patches, sites) {
  const card = el('div', { class: 'vk-panel' });
  if (!patches || !patches.length) { card.appendChild(empty('This generation recorded no patches (it may be the seed).')); return card; }
  const siteLabel = new Map(sites.map((s) => [s.id, s.label]));
  for (const p of patches) {
    const det = el('details', { class: 'vk-diff-file' });
    det.appendChild(el('summary', null, [
      el('span', { class: 'vk-diff-status vk-op-' + (p.op || 'edit'), text: p.op || 'edit' }),
      el('span', { class: 'vk-diff-path vk-mono', text: siteLabel.get(p.mutation_id) || p.mutation_id || p.id }),
    ]));
    const body = el('div', { class: 'vk-diff-body' });
    if (p.rationale) body.appendChild(el('p', { class: 'vk-soft', text: p.rationale }));
    body.appendChild(el('div', null, [
      el('div', { class: 'vk-diff-colhead', text: 'new content' }),
      el('pre', { class: 'vk-diff-col vk-new' }, [el('code', { text: p.new_content != null ? String(p.new_content) : '(no content recorded)' })]),
    ]));
    det.appendChild(body);
    card.appendChild(det);
  }
  return card;
}

function siteSub(m) {
  const file = m.file || '';
  const span = (svg.isNum(m.line_start)) ? `:${m.line_start}${svg.isNum(m.line_end) && m.line_end !== m.line_start ? '-' + m.line_end : ''}` : '';
  const k = m.kind ? ` (${m.kind})` : '';
  return (file + span + k).trim();
}

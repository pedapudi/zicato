// variants/R/views/mutations.js — the epoch-scoped MUTATION SURFACE + diff.
//
// The Mutation-surface section goes straight to detail (it has no item column).
// ONE cohesive visual: the site × generation MATRIX (reused svg.mutationMatrix)
// plus a detail pane that fills on cell-select with the SIDE-BY-SIDE diff —
// champion baseline (left) | challenger new (right) — built from REAL strings
// (FIX #2: `.baseline.content`, never the baseline OBJECT → no "[object Object]").
//
//   * surface:   /api/mutations/{epoch} → { generations, mutations:[…] }
//   * baseline STRING:  /api/mutations/{epoch}/{id} → .baseline.content
//   * challenger STRING: /api/files/{epoch}/{gen}/patches → .new_content
//   * full-file fallback: /api/files/{epoch}/{gen}/diff

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat } from '../ui.js';

export async function render(host, ctx, path) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dr-empty', text: 'Reading the mutation surface…' }));
  const selSite = path && path.mutationId;
  // when a site is selected we also need its generation; default to the first
  // generation that patched it (so a deep-link to /mutations/<id> still diffs).
  let selGen = path && path.gen;

  const [ep, lin] = await Promise.all([D.epoch(), D.lineage()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dr-h1', text: 'Mutation surface' }), empty('No current epoch.')]);
    return;
  }
  const epochId = ep.epoch_id;
  const muts = await D.mutations(epochId);
  const mutations = (muts && Array.isArray(muts.mutations)) ? muts.mutations : [];
  let genIds = (muts && Array.isArray(muts.generations) && muts.generations.length) ? muts.generations.slice() : [];
  const lineGens = (lin && Array.isArray(lin.generations)) ? lin.generations : [];
  if (!genIds.length) genIds = lineGens.map((g) => g.generation_id);
  const promotedSet = new Set(lineGens.filter((g) => g.promoted).map((g) => g.generation_id));

  const sites = mutations.map((m) => ({
    id: m.mutation_id, label: m.role || m.file || m.mutation_id, sub: siteSub(m),
    patched: new Set(m.patched_generation_ids || []), raw: m,
  }));
  const siteById = new Map(sites.map((s) => [s.id, s]));

  if (selSite && !selGen) {
    const s = siteById.get(selSite);
    if (s && s.patched.size) selGen = [...s.patched][0];
  }

  let baseline = null; let next = null; let op = null; let rationale = null; let usedFallback = false;
  if (selSite && selGen) {
    const detail = await D.mutationDetail(epochId, selSite);
    baseline = baselineContent(detail) || baselineContent(siteById.get(selSite) && siteById.get(selSite).raw);
    const pp = await D.patches(epochId, selGen);
    const patches = (pp && Array.isArray(pp.patches)) ? pp.patches : [];
    const match = patches.find((p) => p.mutation_id === selSite || p.id === selSite) || null;
    if (match) { next = match.new_content != null ? String(match.new_content) : null; op = match.op; rationale = match.rationale; }
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
    epochId, selGen, selSite, gens: genIds,
    sites: sites.map((s) => [s.id, s.label, [...s.patched]]),
    baseLen: baseline == null ? -1 : baseline.length, nextLen: next == null ? -1 : next.length, op,
  });

  gatedSwap(host, digest, () => {
    const out = [];
    out.push(el('div', { class: 'dr-pagehead' }, [
      el('h1', { class: 'dr-h1', text: 'Mutation surface · site × generation' }),
      el('p', { class: 'dr-lede', text: 'Which mutation sites each generation patched — and exactly what changed, baseline against challenger, side by side.' }),
    ]));

    out.push(el('div', { class: 'dr-panel dr-row' }, [
      stat(String(sites.length), 'mutation sites'),
      stat(String(genIds.length), 'generations'),
      stat(String(sites.filter((s) => s.patched.size).length), 'sites touched'),
    ]));

    const gens = genIds.map((id) => ({ id, label: id, promoted: promotedSet.has(id) }));
    const matrixCard = el('div', { class: 'dr-panel', style: 'overflow-x:auto;' });
    if (sites.length && gens.length) {
      const patchedLookup = new Map();
      for (const s of sites) for (const g of s.patched) patchedLookup.set(s.id + ' ' + g, true);
      matrixCard.appendChild(svg.mutationMatrix({
        sites, gens, selectedGen: selGen, selectedSite: selSite,
        patched: (siteId, genId) => patchedLookup.has(siteId + ' ' + genId),
        onCell: (genId, siteId) => ctx.navigate({ section: 'mutations', mutationId: siteId, gen: genId }),
      }));
      matrixCard.appendChild(el('p', { class: 'dr-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'a filled cell = that generation patched that site · click → its baseline-vs-challenger diff' }));
    } else {
      matrixCard.appendChild(empty('No mutation surface recorded for this epoch.'));
    }
    out.push(section('Mutation surface · site × generation', matrixCard));

    out.push(section('Patch detail · champion baseline | challenger new', diffPane({ selGen, selSite, site: selSite ? siteById.get(selSite) : null, baseline, next, op, rationale, usedFallback })));
    return out;
  });
}

function diffPane(o) {
  const card = el('div', { class: 'dr-panel dr-diffpane' });
  if (!o.selSite || !o.selGen) {
    card.appendChild(empty('Select a filled cell in the matrix above to see what that generation changed at that site.'));
    return card;
  }
  const site = o.site;
  card.appendChild(el('div', { class: 'dr-diff-meta' }, [
    el('span', { class: 'dr-diff-status dr-op-' + (o.op || 'edit'), text: o.op || 'edit' }),
    el('span', { class: 'dr-diff-path dr-mono', text: (site && (site.sub || site.label)) || o.selSite }),
    el('span', { class: 'dr-faint', text: ' · ' + o.selGen }),
    o.usedFallback ? el('span', { class: 'dr-chip dr-chip-open', text: 'full-file fallback' }) : null,
  ].filter(Boolean)));
  if (o.rationale) card.appendChild(el('p', { class: 'dr-soft', text: o.rationale }));
  if (o.baseline == null && o.next == null) {
    card.appendChild(empty('No baseline or patch content recorded for this site.'));
    return card;
  }
  card.appendChild(svg.sideBySideDiff({
    baseline: o.baseline != null ? o.baseline : '',
    challenger: o.next != null ? o.next : '',
    leftLabel: 'champion baseline', rightLabel: 'challenger new · ' + o.selGen,
  }));
  return card;
}

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

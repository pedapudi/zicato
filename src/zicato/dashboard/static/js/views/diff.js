// js/views/diff.js — PER-CANDIDATE side-by-side patch diff (fix #2).
//
// The target of the candidate lifecycle's clickable "patch" node: this ONE
// candidate's patches against THE GENERATION IT WAS DERIVED FROM, side by
// side. For each mutation site the candidate touched it shows:
//   * parent baseline (LEFT) — /api/mutations/{epoch}/{mutation_id}. The
//     content the site held in the candidate's recorded parent: the
//     `versions[]` entry of the nearest ancestor that patched the site, and
//     `.baseline.content` (the STRING — never the `baseline` object; that was
//     the "[object Object]" bug) when no ancestor ever touched it.
//   * challenger new content (RIGHT) — /api/files/{epoch}/{gen}/patches → the
//     patches[] entry whose mutation_id matches → `.new_content` (+ `.op`,
//     `.rationale`).
// Reuses L's / N's mutation-viewer diff component (svg.sideBySideDiff). A
// per-site full-file fallback (/api/files/{epoch}/{gen}/diff) covers patches
// that carry no inline new_content.
//
// Optional ?…/diff/<mutId> pins ONE site; otherwise every patched site renders.
// Optional `~base=<gen>` picks WHICH version the left column is; the recorded
// parent is the default. Any generation works as a baseline — the content a
// site held in vN is what the nearest link in vN's own chain wrote into it.
// The whole-file FALLBACK cannot honour the pick (its endpoint diffs against
// the recorded parent only) and says so.
//
// PROVENANCE (issue #194 §6). Snapshot GC prunes generation trees and keeps the
// records, so both sides of this view can outlive the tree they describe: the
// patch payloads always do, and the baseline is reconstructed from the epoch's
// frozen enumeration. The server captions what it had to reconstruct
// (`provenance_note`) and declines to NAME a baseline generation it is not sure
// of (`baseline.generation_id: null`); both are rendered as given.

import { el } from '../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, empty, stat, subhead } from '../ui.js';
import { comparePicker } from '../compare.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading patch diff…' }));
  const genId = (params && params.gen) || null;
  const pinned = (params && params.mutId) || null;
  // `~base=<gen>` picks the LEFT side. Absent, the recorded parent answers.
  const askedBase = (params && params.base) || null;

  const ep = await D.epoch();
  const epochId = (params && params.epochId) || (ep && ep.epoch_id) || null;
  if (!epochId || !genId) {
    gatedSwap(host, 'no-cand', () => [el('h1', { class: 'dn-h1', text: 'Patch diff' }), empty('No candidate selected.')]);
    return;
  }

  const [patchesResp, mut, gens] = await Promise.all([
    D.patches(epochId, genId), D.mutations(epochId), D.generationsForEpoch(epochId),
  ]);
  const patches = (patchesResp && Array.isArray(patchesResp.patches)) ? patchesResp.patches : [];
  const sites = (mut && Array.isArray(mut.mutations)) ? mut.mutations : [];
  const siteById = new Map();
  for (const s of sites) siteById.set(s.mutation_id, s);

  // The left column is a GENERATION, and which one is a choice. The DEFAULT
  // is what the candidate was derived from — its recorded parent — because
  // only a v1 off the seed has v0 for a parent, and a mid-chain candidate
  // (v3 → v5) diffed against v0 answers "what changed since the seed" under a
  // heading that promises this one candidate's patch set (issue #253). The
  // operator can pick any OTHER generation as the baseline instead.
  const parentOf = new Map(gens.map((g) => [g.generation_id, g.parent_generation_id || null]));
  // A generation's own chain, itself first, then back through its parents.
  // Reading the site's content off this chain is what makes ANY generation a
  // usable baseline: the content a site held in vN is whatever the nearest
  // link in vN's chain wrote into it.
  const chainOf = (start) => {
    const out = [];
    for (let a = start; a && !out.includes(a); a = parentOf.get(a) || null) out.push(a);
    return out;
  };
  const knownGens = new Set(gens.map((g) => g.generation_id));
  const recordedParent = parentOf.get(genId) || null;
  // A picked baseline must be a generation that exists and is not the
  // candidate itself; anything else falls back to the parent rather than
  // rendering a diff against nothing.
  const pickedBase = (askedBase && askedBase !== genId && knownGens.has(askedBase)) ? askedBase : null;
  const baseGen = pickedBase || recordedParent;
  const chain = baseGen ? chainOf(baseGen) : [];
  // The parent's chain as well, always. The RIGHT column is fixed — it is the
  // candidate you clicked into — and the rows are this candidate's own patch
  // set. Against the parent, every rendered line is therefore this
  // candidate's own edit. Against an EARLIER pick it is not: the lines also
  // carry what the generations in between wrote at the same site. Holding
  // both chains lets each block say which of the two it is showing.
  const parentChain = recordedParent ? chainOf(recordedParent) : [];

  // The patches THIS candidate applied (optionally narrowed to the pinned site).
  let myPatches = patches.filter((p) => p && p.new_content != null);
  if (pinned) myPatches = myPatches.filter((p) => (p.mutation_id || p.id) === pinned);

  // baseline content (STRING) per touched mutation site — never the object.
  const ids = [...new Set(myPatches.map((p) => p.mutation_id || p.id).filter(Boolean))];
  const details = await Promise.all(ids.map((id) => D.mutationDetail(epochId, id)));
  const baselineById = new Map();
  // The generation the baseline column actually IS — null when the server
  // reconstructed it from records and declines to name one.
  const baselineGenById = new Map();
  // Sites where the picked baseline disagrees with the parent — the blocks
  // whose diff is NOT purely this candidate's own change.
  const mixedById = new Map();
  let detailNote = '';
  ids.forEach((id, i) => {
    const d = details[i];
    // `versions[]` carries the content each patching generation wrote into
    // this site. What the BASELINE held at the site is therefore what the
    // nearest link in its chain wrote — and the v0 baseline when no link in
    // that chain ever touched it.
    const byGen = new Map(((d && d.versions) || []).map((v) => [v.generation_id, v]));
    const nearest = (links) => {
      for (const a of links) { if (byGen.has(a)) return byGen.get(a); }
      return null;
    };
    const src = nearest(chain);
    // Stop at that FIRST touching link even when its content is null (a
    // record the server could not honestly reconstruct). Walking further back
    // would put an older generation's text in the column under the baseline's
    // name; the "no content recorded" note below says so instead.
    const str = src
      ? (typeof src.content === 'string' ? src.content : null)
      : ((d && d.baseline && typeof d.baseline.content === 'string') ? d.baseline.content : null);
    baselineById.set(id, str);
    baselineGenById.set(id, src ? src.generation_id : ((d && d.baseline && d.baseline.generation_id) || null));
    // Same read against the parent. A difference means the block below shows
    // more than this candidate wrote.
    const parentSrc = nearest(parentChain);
    const parentStr = parentSrc
      ? (typeof parentSrc.content === 'string' ? parentSrc.content : null)
      : ((d && d.baseline && typeof d.baseline.content === 'string') ? d.baseline.content : null);
    mixedById.set(id, Boolean(pickedBase && str != null && parentStr != null && str !== parentStr));
    if (!detailNote && d && d.provenance_note) detailNote = String(d.provenance_note);
  });

  // full-file fallback for patches lacking inline content (rare).
  let fileDiff = null;
  if (!myPatches.length) fileDiff = await D.diff(epochId, genId);
  const fileEntries = (fileDiff && Array.isArray(fileDiff.files)) ? fileDiff.files : [];
  // Whole-tree browsing does not survive snapshot GC; the patch-touched spans
  // do, and the server captions which one it handed back (issue #194 §6).
  const fileNote = String((fileDiff && fileDiff.provenance_note) || '');

  const digest = JSON.stringify({
    epochId, genId, pinned, fileNote, detailNote,
    base: baseGen || '',
    picker: gens.map((g) => g.generation_id).join(','),
    fileParent: (fileDiff && fileDiff.parent_generation_id) || '',
    patches: myPatches.map((p) => [p.mutation_id || p.id, p.op, String(p.new_content || '').length, (p.rationale || '').length]),
    baselines: ids.map((id) => [id, baselineById.get(id) == null ? -1 : baselineById.get(id).length, baselineGenById.get(id) || '', mixedById.get(id) ? 1 : 0]),
    files: fileEntries.map((f) => [f.path || f.file, String(f.old_content || '').length, String(f.new_content || '').length, f.reconstructed ? 1 : 0, f.note || '']),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: `Patch diff · ${genId}` }),
      el('p', { class: 'dn-lede', text: baseLede(baseGen, recordedParent, fileDiff) }),
    ]));

    nodes.push(el('div', { class: 'dn-panel dn-row' }, [
      stat(String(myPatches.length || fileEntries.length), pinned ? 'pinned site' : 'patched sites'),
      stat(genId, 'candidate'),
      stat(baseGen || '—', baseGen && baseGen === recordedParent ? 'baseline · parent' : 'baseline · picked'),
      el('div', { class: 'dn-stat' }, [el('a', { class: 'dn-linkbtn', href: ctx.href('candidate', { epochId, gen: genId }), text: '← back to candidate' })]),
    ]));

    if (gens.length > 1) {
      // The SAME select the candidate compare uses — one picker idiom across
      // the console. The parent is the default option, so choosing it clears
      // `~base=` and returns to the canonical URL.
      nodes.push(el('div', { class: 'dn-panel dn-row dn-basepick' }, [
        comparePicker({
          label: 'diff against…',
          current: genId,
          value: pickedBase || '',
          noneLabel: recordedParent ? `${recordedParent} · parent` : '— parent —',
          options: gens
            .map((g) => ({ id: g.generation_id, label: g.generation_id }))
            .filter((o) => o.id !== recordedParent),
          onChange: (v) => ctx.navigate('diff', { epochId, gen: genId, mutId: pinned, base: v || null }),
        }),
      ]));
    }

    const body = el('div', { class: 'dn-panel dn-mut-detail' });
    if (detailNote) nodes.push(el('p', { class: 'dn-faint dn-mut-prov', text: detailNote }));
    if (myPatches.length) {
      for (const p of myPatches) {
        const mid = p.mutation_id || p.id;
        const site = siteById.get(mid) || { mutation_id: mid };
        body.appendChild(patchBlock(genId, p, baselineById.get(mid), site, ctx, epochId, baselineGenById.get(mid), {
          mixed: mixedById.get(mid) === true, parent: recordedParent,
        }));
      }
    } else if (fileEntries.length) {
      // The server's caption when it had to reconstruct (the tree was pruned),
      // the plain fallback line otherwise.
      body.appendChild(el('p', { class: 'dn-patch-note dn-faint', text: fileNote || 'No inline mutation-point patches recorded — showing the full-file diff fallback.' }));
      if (pickedBase && pickedBase !== (fileDiff && fileDiff.parent_generation_id)) {
        // The whole-file fallback is served by an endpoint that only diffs
        // against the recorded parent. Say the pick did not apply rather than
        // labelling the parent's diff with the picked version's name.
        body.appendChild(el('p', { class: 'dn-patch-note dn-faint', text: `The full-file fallback compares against the recorded parent only — your pick of ${pickedBase} does not apply here.` }));
      }
      for (const f of fileEntries) {
        const span = f.span || null;
        const fblock = el('div', { class: 'dn-patch-block' });
        fblock.appendChild(el('div', { class: 'dn-patch-head' }, [
          el('span', { class: 'dn-mono', text: f.path || f.file || 'file' }),
          span && span.mutation_id ? el('span', { class: 'dn-faint dn-mono', text: ' · ' + span.mutation_id }) : null,
          span && span.op ? el('span', { class: 'dn-patch-op dn-mono', text: span.op }) : null,
        ].filter(Boolean)));
        if (typeof f.new_content !== 'string') {
          // Reconstructed, but not honestly: the record carries a value whose
          // target sits outside the span. Say what the record DOES hold.
          fblock.appendChild(el('p', { class: 'dn-patch-note dn-faint', text: f.note || 'No content recorded for this entry.' }));
        } else {
          fblock.appendChild(svg.sideBySideDiff({
            baseline: typeof f.old_content === 'string' ? f.old_content : '',
            challenger: f.new_content,
            leftLabel: f.reconstructed
              ? 'baseline span · from records'
              : `baseline · ${(fileDiff && fileDiff.parent_generation_id) || 'v0'}`,
            rightLabel: `challenger new · ${genId}`,
          }));
        }
        body.appendChild(fblock);
      }
    } else {
      body.appendChild(empty('This candidate recorded no patches (it may be the seed, or its patch payload is unavailable).'));
    }
    nodes.push(body);
    return nodes;
  });
}

function patchBlock(genId, patch, baselineStr, site, ctx, epochId, baselineGen, opts) {
  const o = opts || {};
  const block = el('div', { class: 'dn-patch-block' });
  const op = String((patch && patch.op) || 'replace');
  block.appendChild(el('div', { class: 'dn-patch-head' }, [
    el('a', { class: 'dn-mtx-genlink', href: ctx.href('mutations', { epochId, mutId: site.mutation_id }), text: site.mutation_id }),
    el('span', { class: 'dn-faint dn-mono', text: site.file ? ' · ' + fileLine(site) : '' }),
    el('span', { class: 'dn-patch-op dn-mono', text: op }),
  ]));
  // The rows are always this candidate's own patch set, but against a
  // baseline earlier than the parent the LINES are not. Say so at the block
  // that is actually affected, not as a blanket page warning.
  if (o.mixed) {
    block.appendChild(el('p', { class: 'dn-patch-note dn-faint', text: o.parent
      ? `${baselineGen || 'this baseline'} and ${o.parent} differ at this site, so these lines include what came between them — not ${genId}’s change alone. Diff against ${o.parent} for that.`
      : `These lines include what earlier generations wrote at this site — not ${genId}’s change alone.` }));
  }
  const rationale = patch && patch.rationale ? String(patch.rationale).trim() : '';
  if (rationale) {
    block.appendChild(el('p', { class: 'dn-patch-why' }, [el('span', { class: 'dn-patch-why-lead', text: 'Why. ' }), rationale]));
  }
  if (baselineStr == null) {
    block.appendChild(el('p', { class: 'dn-patch-note dn-faint', text: baselineGen
      ? `No content recorded for this site in ${baselineGen} — showing the challenger side only.`
      : 'No baseline content recorded for this site — showing the challenger side only.' }));
  }
  block.appendChild(svg.sideBySideDiff({
    baseline: baselineStr == null ? '' : baselineStr,
    challenger: String(patch.new_content == null ? '' : patch.new_content),
    leftLabel: baselineGen ? `baseline · ${baselineGen}` : 'baseline · from records',
    rightLabel: `challenger new · ${genId}`,
  }));
  return block;
}

// The lede names the generation the LEFT column actually is, so neither the
// default nor a picked baseline can be misread as a diff against the seed.
function baseLede(baseGen, recordedParent, fileDiff) {
  const left = baseGen || (fileDiff && fileDiff.parent_generation_id) || null;
  if (!left) return 'What this candidate changed — side by side, line-diffed. Left: the baseline these patches were written against. Right: this candidate’s new content.';
  const role = left === recordedParent ? 'the generation it was derived from' : 'the version you picked';
  return `What this candidate changed against ${left} — side by side, line-diffed. Left: ${left}, ${role}. Right: this candidate’s new content.`;
}

function fileLine(s) {
  const f = s.file || '?';
  const a = s.line_start; const b = s.line_end;
  if (a != null && b != null && a !== b) return `${f}:${a}–${b}`;
  if (a != null) return `${f}:${a}`;
  return f;
}

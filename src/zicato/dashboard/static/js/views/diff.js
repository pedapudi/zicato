// js/views/diff.js — PER-CANDIDATE side-by-side patch diff (fix #2).
//
// The target of the candidate lifecycle's clickable "patch" node: this ONE
// candidate's patches against the champion baseline, side by side. For each
// mutation site the candidate touched it shows:
//   * champion baseline (LEFT)  — /api/mutations/{epoch}/{mutation_id} →
//     `.baseline.content` (the STRING — never the `baseline` object; that was
//     the "[object Object]" bug).
//   * challenger new content (RIGHT) — /api/files/{epoch}/{gen}/patches → the
//     patches[] entry whose mutation_id matches → `.new_content` (+ `.op`,
//     `.rationale`).
// Reuses L's / N's mutation-viewer diff component (svg.sideBySideDiff). A
// per-site full-file fallback (/api/files/{epoch}/{gen}/diff) covers patches
// that carry no inline new_content.
//
// Optional ?…/diff/<mutId> pins ONE site; otherwise every patched site renders.
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
import { gatedSwap, section, empty, stat, subhead } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading patch diff…' }));
  const genId = (params && params.gen) || null;
  const pinned = (params && params.mutId) || null;

  const ep = await D.epoch();
  const epochId = (params && params.epochId) || (ep && ep.epoch_id) || null;
  if (!epochId || !genId) {
    gatedSwap(host, 'no-cand', () => [el('h1', { class: 'dn-h1', text: 'Patch diff' }), empty('No candidate selected.')]);
    return;
  }

  const [patchesResp, mut] = await Promise.all([D.patches(epochId, genId), D.mutations(epochId)]);
  const patches = (patchesResp && Array.isArray(patchesResp.patches)) ? patchesResp.patches : [];
  const sites = (mut && Array.isArray(mut.mutations)) ? mut.mutations : [];
  const siteById = new Map();
  for (const s of sites) siteById.set(s.mutation_id, s);

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
  let detailNote = '';
  ids.forEach((id, i) => {
    const d = details[i];
    const str = (d && d.baseline && typeof d.baseline.content === 'string') ? d.baseline.content : null;
    baselineById.set(id, str);
    baselineGenById.set(id, (d && d.baseline && d.baseline.generation_id) || null);
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
    patches: myPatches.map((p) => [p.mutation_id || p.id, p.op, String(p.new_content || '').length, (p.rationale || '').length]),
    baselines: ids.map((id) => [id, baselineById.get(id) == null ? -1 : baselineById.get(id).length, baselineGenById.get(id) || '']),
    files: fileEntries.map((f) => [f.path || f.file, String(f.old_content || '').length, String(f.new_content || '').length, f.reconstructed ? 1 : 0, f.note || '']),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: `Patch diff · ${genId}` }),
      el('p', { class: 'dn-lede', text: 'What this candidate changed against the champion baseline — side by side, line-diffed. Left: the champion baseline. Right: this candidate’s new content.' }),
    ]));

    nodes.push(el('div', { class: 'dn-panel dn-row' }, [
      stat(String(myPatches.length || fileEntries.length), pinned ? 'pinned site' : 'patched sites'),
      stat(genId, 'candidate'),
      el('div', { class: 'dn-stat' }, [el('a', { class: 'dn-linkbtn', href: ctx.href('candidate', { epochId, gen: genId }), text: '← back to candidate' })]),
    ]));

    const body = el('div', { class: 'dn-panel dn-mut-detail' });
    if (detailNote) nodes.push(el('p', { class: 'dn-faint dn-mut-prov', text: detailNote }));
    if (myPatches.length) {
      for (const p of myPatches) {
        const mid = p.mutation_id || p.id;
        const site = siteById.get(mid) || { mutation_id: mid };
        body.appendChild(patchBlock(genId, p, baselineById.get(mid), site, ctx, epochId, baselineGenById.get(mid)));
      }
    } else if (fileEntries.length) {
      // The server's caption when it had to reconstruct (the tree was pruned),
      // the plain fallback line otherwise.
      body.appendChild(el('p', { class: 'dn-patch-note dn-faint', text: fileNote || 'No inline mutation-point patches recorded — showing the full-file diff fallback.' }));
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
            leftLabel: f.reconstructed ? 'baseline span · from records' : 'champion baseline · v0',
            rightLabel: `challenger new · ${genId}`,
          }));
        }
        body.appendChild(fblock);
      }
    } else {
      body.appendChild(empty('This candidate recorded no patches (it may be the seed, or its patch payload is unavailable).'));
    }
    nodes.push(section('Side-by-side diff', body));
    return nodes;
  });
}

function patchBlock(genId, patch, baselineStr, site, ctx, epochId, baselineGen) {
  const block = el('div', { class: 'dn-patch-block' });
  const op = String((patch && patch.op) || 'replace');
  block.appendChild(el('div', { class: 'dn-patch-head' }, [
    el('a', { class: 'dn-mtx-genlink', href: ctx.href('mutations', { epochId, mutId: site.mutation_id }), text: site.mutation_id }),
    el('span', { class: 'dn-faint dn-mono', text: site.file ? ' · ' + fileLine(site) : '' }),
    el('span', { class: 'dn-patch-op dn-mono', text: op }),
  ]));
  const rationale = patch && patch.rationale ? String(patch.rationale).trim() : '';
  if (rationale) {
    block.appendChild(el('p', { class: 'dn-patch-why' }, [el('span', { class: 'dn-patch-why-lead', text: 'Why. ' }), rationale]));
  }
  if (baselineStr == null) {
    block.appendChild(el('p', { class: 'dn-patch-note dn-faint', text: 'No baseline content recorded for this site — showing the challenger side only.' }));
  }
  block.appendChild(svg.sideBySideDiff({
    baseline: baselineStr == null ? '' : baselineStr,
    challenger: String(patch.new_content == null ? '' : patch.new_content),
    leftLabel: baselineGen ? `champion baseline · ${baselineGen}` : 'champion baseline · from records',
    rightLabel: `challenger new · ${genId}`,
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

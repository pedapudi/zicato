// variants/R/router.js — Variant R ("Strata") Miller-columns hash router.
//
// Strata navigates the SAME data model as Variant N, but as cascading
// macOS-Finder-style MILLER COLUMNS rather than a nested accordion tree. The
// whole COLUMN PATH lives in the hash so a cold deep-link reconstructs every
// column AND the detail pane. The path is a flat segment list under `#/R/`:
//
//   #/R/                                   col1 only (environment → epochs)
//   #/R/<epoch>                            col2 (the epoch's sections)
//   #/R/<epoch>/<section>                  col3 (items in the section)
//   #/R/<epoch>/<section>/<item>[/<sub>…]  detail pane (the selected item)
//
// section ∈ { generations, boards, mutations, publication }.
//   generations/<gen>            → candidate detail (lifecycle + gate + matchups)
//   generations/<gen>/entry/<e>  → that candidate's per-entry drill
//   generations/<gen>/patch/<id> → that candidate's per-site side-by-side diff
//   boards/<entry>               → per-board cross-candidate detail + trellis
//   boards/<entry>/run/<gen>     → INLINE side-by-side transcript anchored on <gen>
//   mutations[/<mutationId>]     → mutation surface + diff (item-less → detail)
//   publication                  → the epoch's ACM paper (item-less → detail)
//
// A foreign / empty hash collapses to col1 only. `href(path)` and the parse
// share one signature: a `path` OBJECT with the named fields above.

export const PREFIX = '#/R';
export const SECTIONS = ['generations', 'boards', 'mutations', 'publication'];

export function parsePath(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  if (!raw.startsWith('/R')) return {};
  const parts = raw.replace(/^\/R\/?/, '').split('/').filter(Boolean).map(dec);
  const path = {};
  if (parts[0]) path.epoch = parts[0];
  if (parts[1] && SECTIONS.includes(parts[1])) path.section = parts[1];
  if (path.section === 'generations') {
    if (parts[2]) path.gen = parts[2];
    if (parts[3] === 'entry' && parts[4]) path.entry = parts[4];
    else if (parts[3] === 'patch' && parts[4]) path.mutationId = parts[4];
    else if (parts[3] === 'matchups') path.facet = 'matchups';
  } else if (path.section === 'boards') {
    if (parts[2]) path.entry = parts[2];
    if (parts[3] === 'run' && parts[4]) path.runGen = parts[4];
  } else if (path.section === 'mutations') {
    if (parts[2]) path.mutationId = parts[2];
  } else if (path.section === 'publication') {
    // no item — the publication is the detail itself.
  }
  return path;
}

export function href(path) {
  const p = path || {};
  let s = PREFIX + '/';
  if (!p.epoch) return s;
  s += enc(p.epoch);
  if (!p.section) return s;
  s += '/' + p.section;
  if (p.section === 'generations') {
    if (p.gen) {
      s += '/' + enc(p.gen);
      if (p.entry) s += '/entry/' + enc(p.entry);
      else if (p.mutationId) s += '/patch/' + enc(p.mutationId);
      else if (p.facet === 'matchups') s += '/matchups';
    }
  } else if (p.section === 'boards') {
    if (p.entry) {
      s += '/' + enc(p.entry);
      if (p.runGen) s += '/run/' + enc(p.runGen);
    }
  } else if (p.section === 'mutations') {
    if (p.mutationId) s += '/' + enc(p.mutationId);
  }
  return s;
}

export function navigate(path) {
  const target = href(path);
  if (location.hash === target) {
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  } else {
    location.hash = target;
  }
}

// The depth of the column cascade a path drives — used by the shell to decide
// which columns are "active". col1 always present; col2 needs an epoch; col3 a
// section; the detail pane needs the section's item (or an item-less section).
export function detailKind(path) {
  const p = path || {};
  if (!p.epoch || !p.section) return null;
  if (p.section === 'generations') return p.gen ? 'candidate' : null;
  if (p.section === 'boards') return p.entry ? 'board' : null;
  if (p.section === 'mutations') return 'mutations';
  if (p.section === 'publication') return 'publication';
  return null;
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
function dec(s) { try { return s == null ? null : decodeURIComponent(s); } catch { return s || null; } }

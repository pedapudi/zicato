// variants/O/router.js — Compass hash router under the `#/O/` prefix.
//
// Compass is a master-detail workspace. The URL encodes the EXPLICIT,
// PERSISTENT selection so a cold deep-link hydrates BOTH panes (the left
// selector rail + the right detail pane). The selection is a typed kind:
//
//   #/O/                       → overview (nothing selected yet)
//   #/O/gen/<gen>[/<facet>]    → a generation is selected; the right pane
//                                shows the candidate detail at <facet>:
//                                lifecycle | matchups | mutations |
//                                publication | run
//   #/O/gen/<gen>/run/<entry>  → a generation's run detail for one entry
//   #/O/board/<entryId>        → a BOARD ENTRY is selected — the new
//                                first-class per-board cross-candidate
//                                view (keyed by entry id, NEVER an
//                                arbitrary candidate).
//   #/O/run/<gen>/<entry>      → a run transcript (the deepest drill).
//
// A "selection" is { kind: 'none'|'gen'|'board'|'run', id, gen, entry,
// facet }. Two panes digest-gate independently off this selection.

export const PREFIX = '#/O';
export const FACETS = ['lifecycle', 'matchups', 'mutations', 'publication', 'run'];
const FACET_SET = new Set(FACETS);

export function parseRoute(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  if (!raw.startsWith('/O')) return overview();
  const parts = raw.replace(/^\/O\/?/, '').split('/').filter(Boolean).map(dec);
  const head = parts[0] || '';
  switch (head) {
    case '':
      return overview();
    case 'gen': {
      const gen = parts[1] || null;
      if (!gen) return overview();
      // #/O/gen/<gen>/run/<entry>  → a run drill from a generation.
      if (parts[2] === 'run' && parts[3]) {
        return { view: 'run', kind: 'run', id: gen, gen, entry: parts[3], facet: 'run' };
      }
      const facet = FACET_SET.has(parts[2]) ? parts[2] : 'lifecycle';
      // #/O/gen/<gen>/mutations/<site>  → the selected mutation site rides
      // in the `entry` slot for the mutations facet (the side-by-side diff).
      const entry = (facet === 'mutations' && parts[3]) ? parts[3] : null;
      return { view: 'gen', kind: 'gen', id: gen, gen, entry, facet };
    }
    case 'board': {
      const entry = parts[1] || null;
      if (!entry) return overview();
      return { view: 'board', kind: 'board', id: entry, gen: null, entry, facet: null };
    }
    case 'run': {
      const gen = parts[1] || null;
      const entry = parts[2] || null;
      return { view: 'run', kind: 'run', id: gen, gen, entry, facet: 'run' };
    }
    default:
      return overview();
  }
}

function overview() {
  return { view: 'overview', kind: 'none', id: null, gen: null, entry: null, facet: null };
}

export function href(view, params) {
  const p = params || {};
  switch (view) {
    case 'overview':
      return `${PREFIX}/`;
    case 'gen': {
      if (!p.gen) return `${PREFIX}/`;
      const facet = FACET_SET.has(p.facet) ? p.facet : 'lifecycle';
      if (facet === 'mutations' && p.entry) return `${PREFIX}/gen/${enc(p.gen)}/mutations/${enc(p.entry)}`;
      return `${PREFIX}/gen/${enc(p.gen)}/${facet}`;
    }
    case 'board':
      return p.entry ? `${PREFIX}/board/${enc(p.entry)}` : `${PREFIX}/`;
    case 'run':
      return (p.gen && p.entry)
        ? `${PREFIX}/gen/${enc(p.gen)}/run/${enc(p.entry)}`
        : `${PREFIX}/`;
    default:
      return `${PREFIX}/`;
  }
}

export function navigate(view, params) {
  const target = href(view, params);
  if (location.hash === target) {
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  } else {
    location.hash = target;
  }
}

// A stable string identity for ONE selection — used to digest-gate each
// pane's repaint and to decide when a pane host must be cleared.
export function selectionKey(route) {
  const r = route || {};
  return [r.kind || 'none', r.id || '', r.gen || '', r.entry || '', r.facet || ''].join('|');
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
function dec(s) { try { return s == null ? null : decodeURIComponent(s); } catch { return s || null; } }

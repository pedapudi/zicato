// variants/O/router.js — Compass hash router under the `#/O/` prefix.
//
// Compass is a master-detail workspace, scoped by LEVEL. The URL encodes
// the EXPLICIT, PERSISTENT selection so a cold deep-link hydrates BOTH
// panes (the left selector rail + the right detail pane). The selection is
// a typed kind, ordered by SCOPE — workspace (all epochs) → epoch →
// generation → board entry:
//
//   #/O/                        → WORKSPACE (the all-epochs overview; the
//                                 default/root — NOT a single epoch).
//   #/O/epoch/<epochId>[/<f>]   → an EPOCH is selected; the right pane shows
//                                 epoch-SCOPED facets <f> ∈
//                                 overview | publication | mutations. The
//                                 ACM publication and the epoch-wide mutation
//                                 surface live HERE (epoch scope), never on a
//                                 generation.
//   #/O/gen/<gen>[/<facet>]     → a GENERATION is selected; candidate-centric
//                                 facets <facet> ∈ lifecycle | matchups | run
//                                 (NO publication — that moved to epoch scope).
//   #/O/gen/<gen>/run/<entry>   → a generation's run detail for one entry.
//   #/O/board/<entryId>         → a BOARD ENTRY is selected — the per-board
//                                 cross-candidate view (keyed by entry id,
//                                 NEVER an arbitrary candidate). UNCHANGED.
//   #/O/run/<gen>/<entry>       → a run transcript (the deepest drill).
//
// A "selection" is { kind: 'workspace'|'epoch'|'gen'|'board'|'run', id,
// epoch, gen, entry, facet }. The two panes digest-gate independently off it.

export const PREFIX = '#/O';

// Generation facets — candidate-centric ONLY (publication moved to epoch).
export const FACETS = ['lifecycle', 'matchups', 'run'];
const FACET_SET = new Set(FACETS);

// Epoch-scoped facets.
export const EPOCH_FACETS = ['overview', 'publication', 'mutations'];
const EPOCH_FACET_SET = new Set(EPOCH_FACETS);

export function parseRoute(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  if (!raw.startsWith('/O')) return workspace();
  const parts = raw.replace(/^\/O\/?/, '').split('/').filter(Boolean).map(dec);
  const head = parts[0] || '';
  switch (head) {
    case '':
      return workspace();
    case 'epoch': {
      const epoch = parts[1] || null;
      if (!epoch) return workspace();
      const facet = EPOCH_FACET_SET.has(parts[2]) ? parts[2] : 'overview';
      // #/O/epoch/<e>/mutations/<site>  → the selected mutation site rides
      // in the `entry` slot for the epoch mutations facet (side-by-side diff).
      const entry = (facet === 'mutations' && parts[3]) ? parts[3] : null;
      // a patched-by generation can also ride for the mutations diff.
      const gen = (facet === 'mutations' && parts[4]) ? parts[4] : null;
      return { view: 'epoch', kind: 'epoch', id: epoch, epoch, gen, entry, facet };
    }
    case 'gen': {
      const gen = parts[1] || null;
      if (!gen) return workspace();
      // #/O/gen/<gen>/run/<entry>  → a run drill from a generation.
      if (parts[2] === 'run' && parts[3]) {
        return { view: 'run', kind: 'run', id: gen, epoch: null, gen, entry: parts[3], facet: 'run' };
      }
      const facet = FACET_SET.has(parts[2]) ? parts[2] : 'lifecycle';
      return { view: 'gen', kind: 'gen', id: gen, epoch: null, gen, entry: null, facet };
    }
    case 'board': {
      const entry = parts[1] || null;
      if (!entry) return workspace();
      return { view: 'board', kind: 'board', id: entry, epoch: null, gen: null, entry, facet: null };
    }
    case 'run': {
      const gen = parts[1] || null;
      const entry = parts[2] || null;
      return { view: 'run', kind: 'run', id: gen, epoch: null, gen, entry, facet: 'run' };
    }
    default:
      return workspace();
  }
}

function workspace() {
  return { view: 'workspace', kind: 'workspace', id: null, epoch: null, gen: null, entry: null, facet: null };
}

export function href(view, params) {
  const p = params || {};
  switch (view) {
    case 'workspace':
    case 'overview': // back-compat alias → the workspace root
      return `${PREFIX}/`;
    case 'epoch': {
      if (!p.epoch) return `${PREFIX}/`;
      const facet = EPOCH_FACET_SET.has(p.facet) ? p.facet : 'overview';
      if (facet === 'mutations' && p.entry) {
        return p.gen
          ? `${PREFIX}/epoch/${enc(p.epoch)}/mutations/${enc(p.entry)}/${enc(p.gen)}`
          : `${PREFIX}/epoch/${enc(p.epoch)}/mutations/${enc(p.entry)}`;
      }
      return `${PREFIX}/epoch/${enc(p.epoch)}/${facet}`;
    }
    case 'gen': {
      if (!p.gen) return `${PREFIX}/`;
      const facet = FACET_SET.has(p.facet) ? p.facet : 'lifecycle';
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
  return [r.kind || 'workspace', r.id || '', r.epoch || '', r.gen || '', r.entry || '', r.facet || ''].join('|');
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
function dec(s) { try { return s == null ? null : decodeURIComponent(s); } catch { return s || null; } }

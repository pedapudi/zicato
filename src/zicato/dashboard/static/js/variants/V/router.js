// variants/V/router.js — "Reel" hash router under the `#/V/` prefix.
//
// Variant V ("Reel") is the round-6 CREATIVE-temporal take: the epoch as a
// horizontal TIMELINE / playback of rounds. It keeps Console III's data-model
// TREE for full-fidelity navigation, but adds a REEL hero that doubles as
// navigation. The hash therefore encodes the FULL path through the hierarchy —
// AND the selected round on the reel (a generation id) — so the tree, the reel
// scrubber, and the detail pane all hydrate identically from a cold deep-link.
//
//   #/V/                                          → Environment (the fleet)
//   #/V/e/<epochId>                               → Epoch overview (reel + heatmap)
//   #/V/e/<epochId>/gens                          → Generations group landing
//   #/V/e/<epochId>/gen/<gen>[/<entry>]           → Candidate (lifecycle + gate)
//   #/V/e/<epochId>/gen/<gen>/diff[/<mutId>]      → that candidate's patch diff
//   #/V/e/<epochId>/boards                        → Boards group (trellis)
//   #/V/e/<epochId>/board/<entry>[/<gen>]         → per-board + inline transcript
//   #/V/e/<epochId>/mutations[/<mutId>]           → Mutation surface + diff
//   #/V/e/<epochId>/paper                         → ACM publication
//
// The COMPARISON target (folded from S "Lens") rides as a query-like suffix on
// the hash: `~cmp=<gen>` — so a side-by-side comparison deep-links without
// adding a route. `href(view, params, opts)` takes an optional opts.{cmp} that
// every view, the tree, and the reel share.

export const PREFIX = '#/V';
export const VIEWS = ['home', 'epoch', 'gens', 'candidate', 'diff', 'boards', 'board', 'mutations', 'publication'];

export function parseRoute(hash) {
  let raw = String(hash || '').replace(/^#/, '');
  // split off the `~cmp=…` comparison suffix.
  let cmp = null;
  const tilde = raw.indexOf('~');
  if (tilde >= 0) {
    const extra = raw.slice(tilde + 1);
    raw = raw.slice(0, tilde);
    for (const kv of extra.split('&')) {
      const eq = kv.indexOf('=');
      if (eq < 0) continue;
      const k = kv.slice(0, eq); const v = dec(kv.slice(eq + 1));
      if (k === 'cmp') cmp = v || null;
    }
  }
  if (!raw.startsWith('/V')) return { view: 'home', params: {}, cmp: null };
  const parts = raw.replace(/^\/V\/?/, '').split('/').filter(Boolean).map(dec);
  if (!parts.length || parts[0] === 'home') return { view: 'home', params: {}, cmp };
  if (parts[0] !== 'e') return { view: 'home', params: {}, cmp };

  const epochId = parts[1] || null;
  const group = parts[2] || null;
  if (!epochId) return { view: 'home', params: {}, cmp };
  if (!group) return { view: 'epoch', params: { epochId }, cmp };

  switch (group) {
    case 'gens':
      return { view: 'gens', params: { epochId }, cmp };
    case 'gen': {
      const gen = parts[3] || null;
      if (parts[4] === 'diff') return { view: 'diff', params: { epochId, gen, mutId: parts[5] || null }, cmp };
      return { view: 'candidate', params: { epochId, gen, entry: parts[4] || null }, cmp };
    }
    case 'boards':
      return { view: 'boards', params: { epochId }, cmp };
    case 'board':
      return { view: 'board', params: { epochId, entry: parts[3] || null, gen: parts[4] || null }, cmp };
    case 'mutations':
      return { view: 'mutations', params: { epochId, mutId: parts[3] || null }, cmp };
    case 'paper': case 'publication': case 'report':
      return { view: 'publication', params: { epochId }, cmp };
    default:
      return { view: 'epoch', params: { epochId }, cmp };
  }
}

export function href(view, params, opts) {
  const p = params || {};
  const o = opts || {};
  const e = p.epochId ? `${PREFIX}/e/${enc(p.epochId)}` : null;
  let base;
  switch (view) {
    case 'home': base = PREFIX + '/'; break;
    case 'epoch': base = e || (PREFIX + '/'); break;
    case 'gens': base = e ? `${e}/gens` : PREFIX + '/'; break;
    case 'candidate':
      if (!e || !p.gen) base = e || PREFIX + '/';
      else base = p.entry ? `${e}/gen/${enc(p.gen)}/${enc(p.entry)}` : `${e}/gen/${enc(p.gen)}`;
      break;
    case 'diff':
      if (!e || !p.gen) base = e || PREFIX + '/';
      else base = p.mutId ? `${e}/gen/${enc(p.gen)}/diff/${enc(p.mutId)}` : `${e}/gen/${enc(p.gen)}/diff`;
      break;
    case 'boards': base = e ? `${e}/boards` : PREFIX + '/'; break;
    case 'board':
      if (!e || !p.entry) base = e ? `${e}/boards` : PREFIX + '/';
      else base = p.gen ? `${e}/board/${enc(p.entry)}/${enc(p.gen)}` : `${e}/board/${enc(p.entry)}`;
      break;
    case 'mutations':
      if (!e) base = PREFIX + '/';
      else base = p.mutId ? `${e}/mutations/${enc(p.mutId)}` : `${e}/mutations`;
      break;
    case 'publication': base = e ? `${e}/paper` : PREFIX + '/'; break;
    default: base = PREFIX + '/';
  }
  // append the comparison suffix when a compare target is set.
  const suffix = [];
  if (o.cmp) suffix.push('cmp=' + enc(o.cmp));
  return suffix.length ? base + '~' + suffix.join('&') : base;
}

export function navigate(view, params, opts) {
  const target = href(view, params, opts);
  if (location.hash === target) {
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  } else {
    location.hash = target;
  }
}

// The back/up affordance: the PARENT of the current selection in the data-model
// hierarchy. Returns { view, params } to navigate UP one level — environment ←
// epoch ← group ← item ← drill. The shell renders the result into the MAIN
// detail pane (NEVER the sidebar — the round-6 fix to Q's back-button bug).
export function upTarget(route) {
  const p = (route && route.params) || {};
  switch (route && route.view) {
    case 'epoch':
      return { view: 'home', params: {} };
    case 'gens': case 'boards': case 'mutations': case 'publication':
      // a group → its epoch; a pinned mutation → the surface; else the epoch.
      if (route.view === 'mutations' && p.mutId) return { view: 'mutations', params: { epochId: p.epochId } };
      return { view: 'epoch', params: { epochId: p.epochId } };
    case 'candidate':
      // an entry drill → the candidate; the candidate → the generations group.
      if (p.entry) return { view: 'candidate', params: { epochId: p.epochId, gen: p.gen } };
      return { view: 'gens', params: { epochId: p.epochId } };
    case 'diff':
      // a pinned site → the whole diff; the diff → its candidate.
      if (p.mutId) return { view: 'diff', params: { epochId: p.epochId, gen: p.gen } };
      return { view: 'candidate', params: { epochId: p.epochId, gen: p.gen } };
    case 'board':
      // a selected run → the board; the board → the boards group.
      if (p.gen) return { view: 'board', params: { epochId: p.epochId, entry: p.entry } };
      return { view: 'boards', params: { epochId: p.epochId } };
    default:
      return null; // already at the environment root.
  }
}

// The breadcrumb trail mirrors the tree path. Returns [{label, view, params, current}].
export function crumbTrail(route) {
  const p = route.params || {};
  const home = { label: 'environment', view: 'home', params: {} };
  const epoch = p.epochId ? { label: p.epochId, view: 'epoch', params: { epochId: p.epochId } } : null;
  switch (route.view) {
    case 'epoch':
      return [home, { label: p.epochId || 'epoch', current: true }];
    case 'gens':
      return [home, epoch, { label: 'generations', current: true }].filter(Boolean);
    case 'candidate': {
      const trail = [home, epoch, { label: 'generations', view: 'gens', params: { epochId: p.epochId } }].filter(Boolean);
      if (p.entry) {
        trail.push({ label: p.gen || 'candidate', view: 'candidate', params: { epochId: p.epochId, gen: p.gen } });
        trail.push({ label: p.entry, current: true });
      } else {
        trail.push({ label: p.gen || 'candidate', current: true });
      }
      return trail;
    }
    case 'diff':
      return [home, epoch,
        { label: 'generations', view: 'gens', params: { epochId: p.epochId } },
        { label: p.gen || 'candidate', view: 'candidate', params: { epochId: p.epochId, gen: p.gen } },
        { label: 'patch diff', current: true },
      ].filter(Boolean);
    case 'boards':
      return [home, epoch, { label: 'boards', current: true }].filter(Boolean);
    case 'board':
      return [home, epoch,
        { label: 'boards', view: 'boards', params: { epochId: p.epochId } },
        { label: p.entry || 'board', current: true },
      ].filter(Boolean);
    case 'mutations':
      return [home, epoch,
        p.mutId ? { label: 'mutation surface', view: 'mutations', params: { epochId: p.epochId } } : null,
        { label: p.mutId ? p.mutId : 'mutation surface', current: true },
      ].filter(Boolean);
    case 'publication':
      return [home, epoch, { label: 'publication', current: true }].filter(Boolean);
    default:
      return [{ label: 'environment', current: true }];
  }
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
function dec(s) { try { return s == null ? null : decodeURIComponent(s); } catch { return s || null; } }

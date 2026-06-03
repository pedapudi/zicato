// variants/T/router.js — Console IV hash router under the bare `#/` prefix.
//
// Variant T ("Console IV") is the converged default UI: P's data-model TREE
// sidebar (the hash encodes the FULL path through the hierarchy) folded with
// S's first-class side-by-side COMPARE detail. T is now the ONLY variant UI, so
// the old `#/T/` bake-off namespacing prefix is dropped — routes are bare `#/`.
// The hash also carries an optional COMPARISON target so a split candidate pane
// DEEP-LINKS:
//
//   #/                                          → Environment (the fleet)
//   #/e/<epochId>                               → Epoch overview (heatmap)
//   #/e/<epochId>/gens                          → Generations group landing
//   #/e/<epochId>/gen/<gen>[/<entry>]           → Candidate (lifecycle + gate)
//   #/e/<epochId>/gen/<gen>/diff[/<mutId>]      → that candidate's patch diff
//   #/e/<epochId>/boards                        → Boards group (trellis)
//   #/e/<epochId>/board/<entry>[/<gen>]         → per-board + inline transcript
//   #/e/<epochId>/mutations[/<mutId>]           → Mutation surface + diff
//   #/e/<epochId>/paper                         → ACM publication
//
// The COMPARE target is a `~cmp=<gen>` suffix on the hash (S's convention) —
// kept in the hash, not location.search, so one deep-link captures the whole
// comparison state and a cold load hydrates the split. A missing / foreign
// hash returns Environment so a deep-link never lands blank. `href(view,
// params, opts)` takes a params OBJECT plus an optional `{cmp}` so the tree,
// breadcrumb, back button, and every view share one signature.

export const PREFIX = '#';
export const VIEWS = ['home', 'epoch', 'gens', 'candidate', 'diff', 'boards', 'board', 'mutations', 'publication'];

// Split the hash into its path part and its `~k=v` suffix params (the compare
// target). Everything before the first `~` is the structural path.
function splitHash(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  const tildeIdx = raw.indexOf('~');
  const path = tildeIdx >= 0 ? raw.slice(0, tildeIdx) : raw;
  const tail = tildeIdx >= 0 ? raw.slice(tildeIdx) : '';
  const extra = {};
  if (tail) {
    for (const seg of tail.split('~').filter(Boolean)) {
      const eq = seg.indexOf('=');
      if (eq >= 0) extra[seg.slice(0, eq)] = dec(seg.slice(eq + 1));
    }
  }
  return { path, extra };
}

export function parseRoute(hash) {
  const { path, extra } = splitHash(hash);
  const cmp = extra.cmp || null;
  const raw = path;
  // bare `#/` prefix: the path part is everything after the leading slash.
  const parts = raw.replace(/^\/+/, '').split('/').filter(Boolean).map(dec);
  if (!parts.length || parts[0] === 'home') return { view: 'home', params: {}, cmp };
  if (parts[0] !== 'e') return { view: 'home', params: {}, cmp };

  const epochId = parts[1] || null;
  const group = parts[2] || null;
  if (!epochId) return { view: 'home', params: {}, cmp };
  if (!group) return { view: 'epoch', params: { epochId }, cmp };

  switch (group) {
    case 'gens': {
      // an optional `/r/<round>` drill scopes the Match-ups to ONE evolve round.
      const round = (parts[3] === 'r' && parts[4] != null) ? parts[4] : null;
      return { view: 'gens', params: { epochId, round }, cmp };
    }
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
      // …/mutations[/<mutId>[/<gen>]] — a bare mutId pins the SITE (all gens that
      // patched it, stacked); a trailing gen pins ONE site×generation cell (that
      // single challenger's side-by-side diff).
      return { view: 'mutations', params: { epochId, mutId: parts[3] || null, gen: parts[4] || null }, cmp };
    case 'paper': case 'publication': case 'report':
      return { view: 'publication', params: { epochId }, cmp };
    default:
      return { view: 'epoch', params: { epochId }, cmp };
  }
}

export function href(view, params, opts) {
  const p = params || {};
  const e = p.epochId ? `${PREFIX}/e/${enc(p.epochId)}` : null;
  let base;
  switch (view) {
    case 'home': base = PREFIX + '/'; break;
    case 'epoch': base = e || (PREFIX + '/'); break;
    case 'gens':
      base = e ? (p.round != null ? `${e}/gens/r/${enc(p.round)}` : `${e}/gens`) : PREFIX + '/';
      break;
    case 'candidate':
      if (!e || !p.gen) { base = e || PREFIX + '/'; break; }
      base = p.entry ? `${e}/gen/${enc(p.gen)}/${enc(p.entry)}` : `${e}/gen/${enc(p.gen)}`;
      break;
    case 'diff':
      if (!e || !p.gen) { base = e || PREFIX + '/'; break; }
      base = p.mutId ? `${e}/gen/${enc(p.gen)}/diff/${enc(p.mutId)}` : `${e}/gen/${enc(p.gen)}/diff`;
      break;
    case 'boards': base = e ? `${e}/boards` : PREFIX + '/'; break;
    case 'board':
      if (!e || !p.entry) { base = e ? `${e}/boards` : PREFIX + '/'; break; }
      base = p.gen ? `${e}/board/${enc(p.entry)}/${enc(p.gen)}` : `${e}/board/${enc(p.entry)}`;
      break;
    case 'mutations':
      if (!e) { base = PREFIX + '/'; break; }
      // a bare mutId pins the SITE (all gens); a mutId+gen pins ONE cell.
      base = p.mutId
        ? (p.gen ? `${e}/mutations/${enc(p.mutId)}/${enc(p.gen)}` : `${e}/mutations/${enc(p.mutId)}`)
        : `${e}/mutations`;
      break;
    case 'publication': base = e ? `${e}/paper` : PREFIX + '/'; break;
    default: base = PREFIX + '/';
  }
  const o = opts || {};
  // the compare target rides only on the candidate split.
  return (o.cmp && view === 'candidate') ? base + '~cmp=' + enc(o.cmp) : base;
}

export function navigate(view, params, opts) {
  const target = href(view, params, opts);
  if (location.hash === target) {
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  } else {
    location.hash = target;
  }
}

// THE BACK-BUTTON destination: navigate UP the selection hierarchy. Returns the
// PARENT { view, params } of the current route (or null at the root). The shell
// renders this destination into the MAIN detail pane — never the sidebar (the
// explicit fix over Q's buggy back button). A compare split collapses to the
// single candidate first; then the candidate steps up to the generations group.
export function up(route) {
  const p = (route && route.params) || {};
  // A compare split is a "deeper" state than the bare candidate — step out of
  // the comparison before climbing the hierarchy.
  if (route && route.view === 'candidate' && route.cmp) {
    return { view: 'candidate', params: { epochId: p.epochId, gen: p.gen }, cmp: null };
  }
  switch (route ? route.view : 'home') {
    case 'home': return null;
    case 'epoch': return { view: 'home', params: {} };
    case 'gens':
      // a round drill-down steps up to the full (all-rounds) Match-ups first.
      if (p.round != null) return { view: 'gens', params: { epochId: p.epochId } };
      return { view: 'epoch', params: { epochId: p.epochId } };
    case 'candidate':
      // an entry drill steps up to the bare candidate first.
      if (p.entry) return { view: 'candidate', params: { epochId: p.epochId, gen: p.gen } };
      return { view: 'gens', params: { epochId: p.epochId } };
    case 'diff': return { view: 'candidate', params: { epochId: p.epochId, gen: p.gen } };
    case 'boards': return { view: 'epoch', params: { epochId: p.epochId } };
    case 'board':
      // an inline-transcript selection steps up to the bare board first.
      if (p.gen) return { view: 'board', params: { epochId: p.epochId, entry: p.entry } };
      return { view: 'boards', params: { epochId: p.epochId } };
    case 'mutations':
      // a single-cell selection (mutId+gen) steps up to the SITE view (all gens);
      // the site view steps up to the epoch.
      if (p.gen && p.mutId) return { view: 'mutations', params: { epochId: p.epochId, mutId: p.mutId } };
      if (p.mutId) return { view: 'mutations', params: { epochId: p.epochId } };
      return { view: 'epoch', params: { epochId: p.epochId } };
    case 'publication': return { view: 'epoch', params: { epochId: p.epochId } };
    default: return { view: 'home', params: {} };
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
      return p.round != null
        ? [home, epoch, { label: 'generations', view: 'gens', params: { epochId: p.epochId } }, { label: 'round ' + p.round, current: true }].filter(Boolean)
        : [home, epoch, { label: 'generations', current: true }].filter(Boolean);
    case 'candidate': {
      const trail = [home, epoch, { label: 'generations', view: 'gens', params: { epochId: p.epochId } }].filter(Boolean);
      if (p.entry) {
        trail.push({ label: p.gen || 'candidate', view: 'candidate', params: { epochId: p.epochId, gen: p.gen } });
        trail.push({ label: p.entry, current: true });
      } else if (route.cmp) {
        trail.push({ label: p.gen || 'candidate', view: 'candidate', params: { epochId: p.epochId, gen: p.gen } });
        trail.push({ label: 'vs ' + route.cmp, current: true });
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
        // a single-cell selection (mutId+gen) keeps the SITE crumb clickable and
        // labels the leaf with the generation.
        (p.mutId && p.gen) ? { label: p.mutId, view: 'mutations', params: { epochId: p.epochId, mutId: p.mutId } } : null,
        { label: (p.mutId && p.gen) ? p.gen : (p.mutId ? p.mutId : 'mutation surface'), current: true },
      ].filter(Boolean);
    case 'publication':
      return [home, epoch, { label: 'publication', current: true }].filter(Boolean);
    default:
      return [{ label: 'environment', current: true }];
  }
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
function dec(s) { try { return s == null ? null : decodeURIComponent(s); } catch { return s || null; } }

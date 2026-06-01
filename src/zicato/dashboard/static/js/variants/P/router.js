// variants/P/router.js — Console III hash router under the `#/P/` prefix.
//
// Variant P ("Console III") drops Variant N's top-tab nav for a persistent
// data-model TREE sidebar. The hash therefore encodes the FULL path through
// the hierarchy — Environment → Epoch → {Generations|Boards|Mutation
// surface|Publication} → item — so the tree AND the detail pane hydrate
// identically from a cold deep-link, and BOTH the epoch AND the generation are
// always explicit (N's gap: its tabs could not switch which epoch/gen).
//
//   #/P/                                          → Environment (the fleet)
//   #/P/e/<epochId>                               → Epoch overview (heatmap)
//   #/P/e/<epochId>/gens                          → Generations group landing
//   #/P/e/<epochId>/gen/<gen>[/<entry>]           → Candidate (lifecycle + gate)
//   #/P/e/<epochId>/gen/<gen>/diff[/<mutId>]      → that candidate's patch diff
//   #/P/e/<epochId>/boards                        → Boards group (trellis)
//   #/P/e/<epochId>/board/<entry>[/<gen>]         → per-board + inline transcript
//   #/P/e/<epochId>/mutations[/<mutId>]           → Mutation surface + diff
//   #/P/e/<epochId>/paper                         → ACM publication
//
// A missing / foreign hash returns Environment so a deep-link never lands
// blank. `href(view, params)` takes a params OBJECT so the tree, breadcrumb,
// and every view share one signature.

export const PREFIX = '#/P';
export const VIEWS = ['home', 'epoch', 'gens', 'candidate', 'diff', 'boards', 'board', 'mutations', 'publication'];

export function parseRoute(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  if (!raw.startsWith('/P')) return { view: 'home', params: {} };
  const parts = raw.replace(/^\/P\/?/, '').split('/').filter(Boolean).map(dec);
  if (!parts.length || parts[0] === 'home') return { view: 'home', params: {} };
  if (parts[0] !== 'e') return { view: 'home', params: {} };

  const epochId = parts[1] || null;
  const group = parts[2] || null;
  if (!epochId) return { view: 'home', params: {} };
  if (!group) return { view: 'epoch', params: { epochId } };

  switch (group) {
    case 'gens':
      return { view: 'gens', params: { epochId } };
    case 'gen': {
      const gen = parts[3] || null;
      if (parts[4] === 'diff') return { view: 'diff', params: { epochId, gen, mutId: parts[5] || null } };
      return { view: 'candidate', params: { epochId, gen, entry: parts[4] || null } };
    }
    case 'boards':
      return { view: 'boards', params: { epochId } };
    case 'board':
      return { view: 'board', params: { epochId, entry: parts[3] || null, gen: parts[4] || null } };
    case 'mutations':
      return { view: 'mutations', params: { epochId, mutId: parts[3] || null } };
    case 'paper': case 'publication': case 'report':
      return { view: 'publication', params: { epochId } };
    default:
      return { view: 'epoch', params: { epochId } };
  }
}

export function href(view, params) {
  const p = params || {};
  const e = p.epochId ? `${PREFIX}/e/${enc(p.epochId)}` : null;
  switch (view) {
    case 'home': return PREFIX + '/';
    case 'epoch': return e || (PREFIX + '/');
    case 'gens': return e ? `${e}/gens` : PREFIX + '/';
    case 'candidate':
      if (!e || !p.gen) return e || PREFIX + '/';
      return p.entry ? `${e}/gen/${enc(p.gen)}/${enc(p.entry)}` : `${e}/gen/${enc(p.gen)}`;
    case 'diff':
      if (!e || !p.gen) return e || PREFIX + '/';
      return p.mutId ? `${e}/gen/${enc(p.gen)}/diff/${enc(p.mutId)}` : `${e}/gen/${enc(p.gen)}/diff`;
    case 'boards': return e ? `${e}/boards` : PREFIX + '/';
    case 'board':
      if (!e || !p.entry) return e ? `${e}/boards` : PREFIX + '/';
      return p.gen ? `${e}/board/${enc(p.entry)}/${enc(p.gen)}` : `${e}/board/${enc(p.entry)}`;
    case 'mutations':
      if (!e) return PREFIX + '/';
      return p.mutId ? `${e}/mutations/${enc(p.mutId)}` : `${e}/mutations`;
    case 'publication': return e ? `${e}/paper` : PREFIX + '/';
    default: return PREFIX + '/';
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

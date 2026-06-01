// variants/Q/router.js — Atlas IV hash router under the `#/Q/` prefix.
//
// Variant Q ("Atlas IV") replaces N's top-tab nav with a persistent LEFT TREE
// grounded in the DATA MODEL. The route is the tree SELECTION — explicit and
// URL-encoded, so a cold deep-link hydrates BOTH the tree (expanded ancestors)
// and the detail pane. Crucially the routes carry the EPOCH id (and, where it
// matters, the generation), so the tree can navigate MULTIPLE epochs AND
// MULTIPLE generations (N's gap was that its tabs could not switch which
// epoch/candidate was shown).
//
//   #/Q/                                   → Environment (the fleet)
//   #/Q/epoch/<epochId>                    → Epoch overview (lineage + HEATMAP)
//   #/Q/gen/<epochId>/<gen>[/<entry>]      → Candidate (lifecycle · gate · patch · per-board)
//   #/Q/matchups/<epochId>/<gen>           → ALL match-ups for that candidate (fix #3)
//   #/Q/board/<epochId>[/<entry>]          → Boards (TRELLIS; entry → cross-candidate + INLINE transcript)
//   #/Q/mutations/<epochId>[/<gen>/<mutId>]→ Mutation surface + side-by-side diff
//   #/Q/publication/<epochId>              → ACM publication
//   #/Q/run/<epochId>/<gen>/<entry>        → Run detail (the transcript)
//
// A missing / foreign hash returns Environment so a deep-link never lands
// blank. `href(view, params)` takes a params OBJECT so the tree, the breadcrumb
// and every view share one signature.

export const PREFIX = '#/Q';
export const VIEWS = ['home', 'epoch', 'gen', 'matchups', 'board', 'mutations', 'publication', 'run'];

export function parseRoute(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  if (!raw.startsWith('/Q')) return { view: 'home', params: {} };
  const parts = raw.replace(/^\/Q\/?/, '').split('/').filter(Boolean).map(dec);
  const head = parts[0] || 'home';
  switch (head) {
    case 'epoch':
      return { view: 'epoch', params: { epochId: parts[1] || null } };
    case 'gen': case 'candidate':
      return { view: 'gen', params: { epochId: parts[1] || null, gen: parts[2] || null, entry: parts[3] || null } };
    case 'matchups':
      return { view: 'matchups', params: { epochId: parts[1] || null, gen: parts[2] || null } };
    case 'board':
      return { view: 'board', params: { epochId: parts[1] || null, entry: parts[2] || null, cmp: parts[3] || null } };
    case 'mutations':
      return { view: 'mutations', params: { epochId: parts[1] || null, gen: parts[2] || null, mutId: parts[3] || null } };
    case 'publication': case 'report':
      return { view: 'publication', params: { epochId: parts[1] || null } };
    case 'run':
      return { view: 'run', params: { epochId: parts[1] || null, gen: parts[2] || null, entry: parts[3] || null } };
    case 'home': case '':
      return { view: 'home', params: {} };
    default:
      return { view: 'home', params: {} };
  }
}

export function href(view, params) {
  const p = params || {};
  const e = p.epochId ? enc(p.epochId) : null;
  switch (view) {
    case 'home': return PREFIX + '/';
    case 'epoch': return e ? `${PREFIX}/epoch/${e}` : `${PREFIX}/epoch`;
    case 'gen': case 'candidate':
      if (!e) return `${PREFIX}/gen`;
      if (p.gen && p.entry) return `${PREFIX}/gen/${e}/${enc(p.gen)}/${enc(p.entry)}`;
      if (p.gen) return `${PREFIX}/gen/${e}/${enc(p.gen)}`;
      return `${PREFIX}/gen/${e}`;
    case 'matchups':
      return e && p.gen ? `${PREFIX}/matchups/${e}/${enc(p.gen)}` : (e ? `${PREFIX}/matchups/${e}` : `${PREFIX}/matchups`);
    case 'board':
      if (!e) return `${PREFIX}/board`;
      if (p.entry && p.cmp) return `${PREFIX}/board/${e}/${enc(p.entry)}/${enc(p.cmp)}`;
      return p.entry ? `${PREFIX}/board/${e}/${enc(p.entry)}` : `${PREFIX}/board/${e}`;
    case 'mutations':
      if (!e) return `${PREFIX}/mutations`;
      if (p.gen && p.mutId) return `${PREFIX}/mutations/${e}/${enc(p.gen)}/${enc(p.mutId)}`;
      if (p.gen) return `${PREFIX}/mutations/${e}/${enc(p.gen)}`;
      return `${PREFIX}/mutations/${e}`;
    case 'publication': return e ? `${PREFIX}/publication/${e}` : `${PREFIX}/publication`;
    case 'run':
      return (e && p.gen && p.entry) ? `${PREFIX}/run/${e}/${enc(p.gen)}/${enc(p.entry)}` : `${PREFIX}/run`;
    default: return PREFIX + '/';
  }
}

export function navigate(view, params) {
  const target = href(view, params);
  if (location.hash === target) {
    if (typeof HashChangeEvent === 'function') {
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    } else if (window._listeners && Array.isArray(window._listeners.hashchange)) {
      for (const fn of [...window._listeners.hashchange]) fn({ type: 'hashchange' });
    }
  } else {
    location.hash = target;
  }
}

// The breadcrumb trail. Returns [{label, view, params, current}].
export function crumbTrail(route) {
  const p = route.params || {};
  const home = { label: 'environment', view: 'home', params: {} };
  const epoch = p.epochId ? { label: p.epochId, view: 'epoch', params: { epochId: p.epochId } } : { label: 'epoch', view: 'epoch', params: {} };
  switch (route.view) {
    case 'epoch':
      return [home, { label: p.epochId || 'epoch', current: true }];
    case 'gen': {
      const trail = [home, epoch, { label: 'generations' }];
      if (p.entry) {
        trail.push({ label: p.gen || 'candidate', view: 'gen', params: { epochId: p.epochId, gen: p.gen } });
        trail.push({ label: p.entry, current: true });
      } else {
        trail.push({ label: p.gen || 'candidate', current: true });
      }
      return trail;
    }
    case 'matchups':
      return [home, epoch, { label: 'generations' },
        { label: p.gen || 'candidate', view: 'gen', params: { epochId: p.epochId, gen: p.gen } },
        { label: 'match-ups', current: true }];
    case 'board':
      return [home, epoch,
        { label: 'boards', view: 'board', params: { epochId: p.epochId } },
        p.entry ? { label: p.entry, current: true } : { label: 'boards', current: true }].filter((c, i, a) => !(i === 2 && !p.entry));
    case 'mutations':
      return [home, epoch,
        { label: 'mutation surface', view: 'mutations', params: { epochId: p.epochId } },
        p.mutId ? { label: p.mutId, current: true } : { label: 'mutation surface', current: true }]
        .filter((c, i, a) => !(i === 2 && !p.mutId));
    case 'publication':
      return [home, epoch, { label: 'publication', current: true }];
    case 'run':
      return [home, epoch,
        p.gen ? { label: p.gen, view: 'gen', params: { epochId: p.epochId, gen: p.gen } } : null,
        { label: 'run' + (p.entry ? ' · ' + p.entry : ''), current: true },
      ].filter(Boolean);
    default:
      return [{ label: 'environment', current: true }];
  }
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
function dec(s) { try { return s == null ? null : decodeURIComponent(s); } catch { return s || null; } }

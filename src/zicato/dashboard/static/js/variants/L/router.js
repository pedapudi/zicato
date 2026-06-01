// variants/L/router.js — Atlas III hash router under the `#/L/` prefix.
//
// Dashboard-first (E's IA/flow) over the convergence-II screen set:
//
//   #/L/                                  → Environment (the fleet)
//   #/L/epoch                             → Epoch (bumps + heatmap + trellis)
//   #/L/epoch/<epochId>                   → Epoch (explicit id)
//   #/L/candidate/<gen>                   → Candidate (lifecycle + sankey + scoring)
//   #/L/candidate/<gen>/<entry>           → Candidate drilled into one entry
//   #/L/board/<entryId>                   → Board: ONE entry across ALL candidates (NEW)
//   #/L/matchups                          → Match-ups (gauntlet + paired duels + alts)
//   #/L/mutations                         → Mutation surface + side-by-side diff (NEW)
//   #/L/mutations/<gen>                   → Mutations focused on a generation
//   #/L/mutations/<gen>/<mutationId>      → Mutations focused on a site (side-by-side)
//   #/L/publication                       → ACM-style epoch publication (K's renderer)
//   #/L/run/<gen>/<entry>                 → Run detail (the transcript)
//
// parseRoute() turns the location hash into a `{ view, params }` record; it
// tolerates a missing / foreign hash (returns Environment) so a deep-link or
// a stale hash never lands on a blank screen.

export const PREFIX = '#/L';
export const VIEWS = ['home', 'epoch', 'candidate', 'board', 'matchups', 'mutations', 'publication', 'run'];

export function parseRoute(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  if (!raw.startsWith('/L')) return { view: 'home', params: {} };
  const parts = raw.replace(/^\/L\/?/, '').split('/').filter(Boolean).map(dec);
  const head = parts[0] || 'home';
  switch (head) {
    case 'epoch':
      return { view: 'epoch', params: { epochId: parts[1] || null } };
    case 'candidate':
      return { view: 'candidate', params: { gen: parts[1] || null, entry: parts[2] || null } };
    case 'board':
      return { view: 'board', params: { entry: parts[1] || null } };
    case 'matchups':
      return { view: 'matchups', params: {} };
    case 'mutations':
      return { view: 'mutations', params: { gen: parts[1] || null, mutationId: parts[2] || null } };
    case 'publication': case 'paper':
      return { view: 'publication', params: { epochId: parts[1] || null } };
    case 'run':
      return { view: 'run', params: { gen: parts[1] || null, entry: parts[2] || null } };
    case 'home': case '':
      return { view: 'home', params: {} };
    default:
      return { view: 'home', params: {} };
  }
}

export function href(view, params) {
  const p = params || {};
  switch (view) {
    case 'home': return PREFIX + '/';
    case 'epoch': return p.epochId ? `${PREFIX}/epoch/${enc(p.epochId)}` : `${PREFIX}/epoch`;
    case 'candidate':
      return (p.gen && p.entry)
        ? `${PREFIX}/candidate/${enc(p.gen)}/${enc(p.entry)}`
        : (p.gen ? `${PREFIX}/candidate/${enc(p.gen)}` : `${PREFIX}/candidate`);
    case 'board': return p.entry ? `${PREFIX}/board/${enc(p.entry)}` : `${PREFIX}/board`;
    case 'matchups': return PREFIX + '/matchups';
    case 'mutations':
      return (p.gen && p.mutationId)
        ? `${PREFIX}/mutations/${enc(p.gen)}/${enc(p.mutationId)}`
        : (p.gen ? `${PREFIX}/mutations/${enc(p.gen)}` : `${PREFIX}/mutations`);
    case 'publication': return p.epochId ? `${PREFIX}/publication/${enc(p.epochId)}` : `${PREFIX}/publication`;
    case 'run':
      return (p.gen && p.entry) ? `${PREFIX}/run/${enc(p.gen)}/${enc(p.entry)}` : `${PREFIX}/run`;
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

// The breadcrumb trail (E's hierarchical IA): ancestors are links up the
// tree, the active leaf is plain. Returns [{ label, view, params, current }].
export function crumbTrail(route) {
  const home = { label: 'environment', view: 'home', params: {} };
  switch (route.view) {
    case 'epoch':
      return [home, { label: route.params.epochId || 'epoch', current: true }];
    case 'candidate': {
      const trail = [home, { label: 'epoch', view: 'epoch', params: {} }];
      if (route.params.entry) {
        trail.push({ label: route.params.gen || 'candidate', view: 'candidate', params: { gen: route.params.gen } });
        trail.push({ label: route.params.entry, current: true });
      } else {
        trail.push({ label: route.params.gen || 'candidate', current: true });
      }
      return trail;
    }
    case 'board':
      return [home, { label: 'epoch', view: 'epoch', params: {} },
        { label: 'board · ' + (route.params.entry || ''), current: true }];
    case 'matchups':
      return [home, { label: 'match-ups', current: true }];
    case 'mutations':
      return [home, { label: 'mutations', current: true }];
    case 'publication':
      return [home, { label: 'publication', current: true }];
    case 'run':
      return [home,
        { label: 'epoch', view: 'epoch', params: {} },
        route.params.gen
          ? { label: route.params.gen, view: 'candidate', params: { gen: route.params.gen } }
          : null,
        { label: 'run' + (route.params.entry ? ' · ' + route.params.entry : ''), current: true },
      ].filter(Boolean);
    default:
      return [{ label: 'environment', current: true }];
  }
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
function dec(s) { try { return s == null ? null : decodeURIComponent(s); } catch { return s || null; } }

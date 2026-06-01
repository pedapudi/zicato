// variants/M/router.js — Ledger II hash router under the `#/M/` prefix.
//
// Variant M ("Ledger II") is the editorial, light-first publication skin
// built on Variant E's flow. It keeps E's hierarchical breadcrumb IA and
// adds the convergence-II views — a combined Mutations surface + side-by-side
// diff, a NEW per-board cross-candidate view, and the prominent ACM
// Publication tab.
//
//   #/M/                              → Home / Environment (the fleet)
//   #/M/epoch                         → Epoch (lineage bumps + heatmap + trellis)
//   #/M/epoch/<epochId>               → Epoch (explicit id)
//   #/M/candidate/<gen>               → Candidate (generation)
//   #/M/candidate/<gen>/<entry>       → Candidate drilled into one board entry
//   #/M/matchups                      → Match-ups (gauntlet + slopegraphs + GATE)
//   #/M/mutations                     → Mutation surface × generation matrix
//   #/M/mutations/<mutationId>        → …drilled to its SIDE-BY-SIDE diff
//   #/M/mutations/<mutationId>/<gen>  → …diff against a specific challenger
//   #/M/board                         → Board (pick an entry)
//   #/M/board/<entryId>               → Per-board cross-candidate detail  (NEW)
//   #/M/paper                         → ACM-style epoch publication
//   #/M/paper/<epochId>               → Publication (explicit id)
//   #/M/run/<gen>/<entry>             → Run detail (the transcript)
//
// parseRoute() tolerates a missing / foreign hash (returns Home) so a
// deep-link or a stale hash never lands on a blank screen.

export const PREFIX = '#/M';
export const VIEWS = ['home', 'epoch', 'candidate', 'matchups', 'mutations', 'board', 'paper', 'run'];

export function parseRoute(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  if (!raw.startsWith('/M')) return { view: 'home', params: {} };
  const parts = raw.replace(/^\/M\/?/, '').split('/').filter(Boolean).map(dec);
  const head = parts[0] || 'home';
  switch (head) {
    case 'epoch':
      return { view: 'epoch', params: { epochId: parts[1] || null } };
    case 'candidate':
      return { view: 'candidate', params: { gen: parts[1] || null, entry: parts[2] || null } };
    case 'matchups':
      return { view: 'matchups', params: {} };
    case 'mutations':
      return { view: 'mutations', params: { mutationId: parts[1] || null, gen: parts[2] || null } };
    case 'board':
      return { view: 'board', params: { entry: parts[1] || null } };
    case 'paper':
      return { view: 'paper', params: { epochId: parts[1] || null } };
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
    case 'matchups': return PREFIX + '/matchups';
    case 'mutations':
      return p.mutationId
        ? (p.gen
          ? `${PREFIX}/mutations/${enc(p.mutationId)}/${enc(p.gen)}`
          : `${PREFIX}/mutations/${enc(p.mutationId)}`)
        : `${PREFIX}/mutations`;
    case 'board': return p.entry ? `${PREFIX}/board/${enc(p.entry)}` : `${PREFIX}/board`;
    case 'paper': return p.epochId ? `${PREFIX}/paper/${enc(p.epochId)}` : `${PREFIX}/paper`;
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
    case 'matchups':
      return [home, { label: 'match-ups', current: true }];
    case 'mutations':
      return route.params.mutationId
        ? [home, { label: 'mutation surface', view: 'mutations', params: {} }, { label: route.params.mutationId, current: true }]
        : [home, { label: 'mutation surface', current: true }];
    case 'board':
      return route.params.entry
        ? [home, { label: 'board', view: 'board', params: {} }, { label: route.params.entry, current: true }]
        : [home, { label: 'board', current: true }];
    case 'paper':
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

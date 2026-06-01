// variants/J/router.js — Console hash router under the `#/J/` prefix.
//
// Variant J ("Console") is a dense observatory built on Variant E's IA/flow
// (the operator confirmed E's flow is "likely fine"), with two NEW screens
// the convergence brief mandates: a mutation-site × generation matrix and an
// ACM-style epoch publication. The seven Console screens:
//
//   #/J/                              → Home / Environment (the fleet)
//   #/J/epoch                         → Epoch (lineage + heatmap + trellis)
//   #/J/epoch/<epochId>               → Epoch (explicit id)
//   #/J/candidate/<gen>               → Candidate (generation) — dot-plot + DAG
//   #/J/candidate/<gen>/<entry>       → Candidate drilled into one board entry
//   #/J/matchups                      → Match-ups (gauntlet + slopegraphs + alts)
//   #/J/mutations                     → Mutation sites × generation matrix (NEW)
//   #/J/mutations/<mutId>             → Mutation matrix, one site pinned (drill)
//   #/J/report                        → ACM-style epoch publication (NEW)
//   #/J/report/<epochId>              → Report for an explicit epoch
//   #/J/run/<gen>/<entry>             → Run detail (the transcript)
//
// parseRoute() turns the location hash into a `{ view, params }` record; it
// tolerates a missing / foreign hash (returns Home) so a deep-link or a
// stale hash never lands on a blank screen. `href(view, params)` accepts a
// params OBJECT so the shell breadcrumb and every view share one signature.

export const PREFIX = '#/J';
export const VIEWS = ['home', 'epoch', 'candidate', 'matchups', 'mutations', 'report', 'run'];

export function parseRoute(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  if (!raw.startsWith('/J')) return { view: 'home', params: {} };
  const parts = raw.replace(/^\/J\/?/, '').split('/').filter(Boolean).map(dec);
  const head = parts[0] || 'home';
  switch (head) {
    case 'epoch':
      return { view: 'epoch', params: { epochId: parts[1] || null } };
    case 'candidate':
      return { view: 'candidate', params: { gen: parts[1] || null, entry: parts[2] || null } };
    case 'matchups':
      return { view: 'matchups', params: {} };
    case 'mutations':
      return { view: 'mutations', params: { mutId: parts[1] || null } };
    case 'report':
      return { view: 'report', params: { epochId: parts[1] || null } };
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
    case 'mutations': return p.mutId ? `${PREFIX}/mutations/${enc(p.mutId)}` : `${PREFIX}/mutations`;
    case 'report': return p.epochId ? `${PREFIX}/report/${enc(p.epochId)}` : `${PREFIX}/report`;
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
      return [home, { label: 'epoch', view: 'epoch', params: {} },
        route.params.mutId
          ? { label: 'mutations', view: 'mutations', params: {} }
          : null,
        { label: route.params.mutId ? route.params.mutId : 'mutations', current: true },
      ].filter(Boolean);
    case 'report':
      return [home, { label: 'epoch', view: 'epoch', params: {} },
        { label: 'report', current: true }];
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

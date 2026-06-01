// variants/E/router.js — Atlas hash router under the `#/E/` prefix.
//
// Variant E ("Atlas") keeps A's hierarchical breadcrumb IA — the one that
// tested well, where almost everything clickable went where expected — but
// over the five Atlas screens:
//
//   #/E/                                  → Home / Environment (the fleet)
//   #/E/epoch                             → Epoch (lineage + heatmap + trellis)
//   #/E/epoch/<epochId>                   → Epoch (explicit id)
//   #/E/candidate/<gen>                   → Candidate (generation) — dot-plot + DAG
//   #/E/candidate/<gen>/<entry>           → Candidate drilled into one board entry
//   #/E/matchups                          → Match-ups (gauntlet + slopegraphs + alts)
//   #/E/run/<gen>/<entry>                 → Run detail (the transcript)
//
// parseRoute() turns the location hash into a `{ view, params }` record; it
// tolerates a missing / foreign hash (returns Home) so a deep-link or a
// stale hash never lands on a blank screen. `href(view, params)` accepts a
// params OBJECT (epochId / gen / entry) so ui.crumb(view, params) and the
// shell breadcrumb share one signature.

export const PREFIX = '#/E';
export const VIEWS = ['home', 'epoch', 'candidate', 'matchups', 'run'];

export function parseRoute(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  if (!raw.startsWith('/E')) return { view: 'home', params: {} };
  const parts = raw.replace(/^\/E\/?/, '').split('/').filter(Boolean).map(dec);
  const head = parts[0] || 'home';
  switch (head) {
    case 'epoch':
      return { view: 'epoch', params: { epochId: parts[1] || null } };
    case 'candidate':
      return { view: 'candidate', params: { gen: parts[1] || null, entry: parts[2] || null } };
    case 'matchups':
      return { view: 'matchups', params: {} };
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

// The breadcrumb trail (A's hierarchical IA): ancestors are links up the
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

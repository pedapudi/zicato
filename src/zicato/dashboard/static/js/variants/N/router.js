// variants/N/router.js — Console II hash router under the `#/N/` prefix.
//
// Variant N ("Console II") is a dense observatory built on Variant E's IA/flow,
// refined for convergence II. Eight screens:
//
//   #/N/                              → Environment (the fleet)
//   #/N/epoch[/<epochId>]             → Epoch (lineage + heatmap + trellis)
//   #/N/candidate/<gen>[/<entry>]     → Candidate — lifecycle DAG + dot-plot
//   #/N/matchups                      → Match-ups (sankey + gauntlet + GATE)
//   #/N/mutations[/<mutId>]           → Mutation surface + side-by-side diff
//   #/N/board/<entry>                 → Per-board cross-candidate view (NEW)
//   #/N/publication[/<epochId>]       → ACM publication (K's renderer, a tab)
//   #/N/run/<gen>/<entry>             → Run detail (the transcript)
//
// A missing / foreign hash returns Environment so a deep-link never lands
// blank. `href(view, params)` takes a params OBJECT so the shell breadcrumb
// and every view share one signature.

export const PREFIX = '#/N';
export const VIEWS = ['home', 'epoch', 'candidate', 'matchups', 'mutations', 'board', 'publication', 'run'];

export function parseRoute(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  if (!raw.startsWith('/N')) return { view: 'home', params: {} };
  const parts = raw.replace(/^\/N\/?/, '').split('/').filter(Boolean).map(dec);
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
    case 'board':
      return { view: 'board', params: { entry: parts[1] || null } };
    case 'publication': case 'report':
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
    case 'matchups': return PREFIX + '/matchups';
    case 'mutations': return p.mutId ? `${PREFIX}/mutations/${enc(p.mutId)}` : `${PREFIX}/mutations`;
    case 'board': return p.entry ? `${PREFIX}/board/${enc(p.entry)}` : `${PREFIX}/board`;
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

// The breadcrumb trail (E's hierarchical IA). Returns [{label, view, params, current}].
export function crumbTrail(route) {
  const home = { label: 'environment', view: 'home', params: {} };
  const epoch = { label: 'epoch', view: 'epoch', params: {} };
  switch (route.view) {
    case 'epoch':
      return [home, { label: route.params.epochId || 'epoch', current: true }];
    case 'candidate': {
      const trail = [home, epoch];
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
      return [home, epoch,
        route.params.mutId ? { label: 'mutations', view: 'mutations', params: {} } : null,
        { label: route.params.mutId ? route.params.mutId : 'mutations', current: true },
      ].filter(Boolean);
    case 'board':
      return [home, epoch,
        { label: route.params.entry ? route.params.entry : 'board', current: true }];
    case 'publication':
      return [home, epoch, { label: 'publication', current: true }];
    case 'run':
      return [home, epoch,
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

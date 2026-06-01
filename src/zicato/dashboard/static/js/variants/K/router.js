// variants/K/router.js — Monograph hash router under the `#/K/` prefix.

export const PREFIX = '#/K';
export const VIEWS = ['paper', 'candidate', 'matchups', 'mutations', 'run'];

export function parseRoute(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  if (!raw.startsWith('/K')) return { view: 'paper', params: {} };
  const parts = raw.replace(/^\/K\/?/, '').split('/').filter(Boolean).map(dec);
  const head = parts[0] || 'paper';
  switch (head) {
    case 'paper':
      return { view: 'paper', params: { epochId: parts[1] || null } };
    case 'candidate':
      return { view: 'candidate', params: { gen: parts[1] || null, entry: parts[2] || null } };
    case 'matchups':
      return { view: 'matchups', params: { champion: parts[1] || null, challenger: parts[2] || null } };
    case 'mutations':
      return { view: 'mutations', params: { gen: parts[1] || null } };
    case 'run':
      return { view: 'run', params: { gen: parts[1] || null, entry: parts[2] || null } };
    case '':
      return { view: 'paper', params: {} };
    default:
      return { view: 'paper', params: {} };
  }
}

export function href(view, params) {
  const p = params || {};
  switch (view) {
    case 'paper': return p.epochId ? `${PREFIX}/paper/${enc(p.epochId)}` : `${PREFIX}/`;
    case 'home': return `${PREFIX}/`;
    case 'candidate':
      return (p.gen && p.entry)
        ? `${PREFIX}/candidate/${enc(p.gen)}/${enc(p.entry)}`
        : (p.gen ? `${PREFIX}/candidate/${enc(p.gen)}` : `${PREFIX}/candidate`);
    case 'matchups':
      return (p.champion && p.challenger)
        ? `${PREFIX}/matchups/${enc(p.champion)}/${enc(p.challenger)}`
        : `${PREFIX}/matchups`;
    case 'mutations':
      return p.gen ? `${PREFIX}/mutations/${enc(p.gen)}` : `${PREFIX}/mutations`;
    case 'run':
      return (p.gen && p.entry) ? `${PREFIX}/run/${enc(p.gen)}/${enc(p.entry)}` : `${PREFIX}/run`;
    default: return `${PREFIX}/`;
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

// The breadcrumb trail — the paper is always the root (HOME), with the live
export function crumbTrail(route) {
  const paper = { label: 'paper', view: 'paper', params: {} };
  switch (route.view) {
    case 'candidate': {
      const trail = [paper];
      if (route.params.entry) {
        trail.push({ label: route.params.gen || 'candidate', view: 'candidate', params: { gen: route.params.gen } });
        trail.push({ label: route.params.entry, current: true });
      } else {
        trail.push({ label: route.params.gen || 'candidate', current: true });
      }
      return trail;
    }
    case 'matchups':
      return (route.params.champion && route.params.challenger)
        ? [paper, { label: 'match-ups', view: 'matchups', params: {} },
          { label: `${route.params.champion} → ${route.params.challenger}`, current: true }]
        : [paper, { label: 'match-ups', current: true }];
    case 'mutations':
      return route.params.gen
        ? [paper, { label: 'mutation sites', view: 'mutations', params: {} },
          { label: route.params.gen, current: true }]
        : [paper, { label: 'mutation sites', current: true }];
    case 'run':
      return [paper,
        route.params.gen
          ? { label: route.params.gen, view: 'candidate', params: { gen: route.params.gen } }
          : null,
        { label: 'run' + (route.params.entry ? ' · ' + route.params.entry : ''), current: true },
      ].filter(Boolean);
    case 'paper':
    default:
      return [{ label: 'paper', current: true }];
  }
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
function dec(s) { try { return s == null ? null : decodeURIComponent(s); } catch { return s || null; } }

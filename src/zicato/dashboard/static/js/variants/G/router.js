// variants/G/router.js — the Bridge hash router under the `#/G/` prefix.
//
// Variant G keeps Variant A's exact navigation/IA — the nav that worked
// ("almost everything I wanted to click took me where I wanted to go").
// Same routes, same breadcrumb spine, same prefix shape, just rebound
// under `#/G/`:
//
//   #/G/                              -> environment (Fleet + trendline)
//   #/G/epoch/:epochId                -> epoch (heatmap + bumps + trellis)
//   #/G/experiment/:epochId/:genId    -> candidate (lifecycle/flow + scoring)
//   #/G/tournament/:epochId           -> match-ups (slopegraphs + topology)
//   #/G/run/:runId                    -> run transcript
//   #/G/bench                         -> bench / live event tail
//
// The router NEVER recreates the view host on a switch — it parses the
// route and hands it to the shell, which paints into ONE persistent
// content host. No orphaned listeners, no blanked screen on nav.

const PREFIX = '#/G';

export function parseRoute(hash) {
  const h = String(hash || '').trim();
  if (!h.startsWith(PREFIX)) return { name: 'environment', params: {} };
  const rest = h.slice(PREFIX.length).replace(/^\//, '');
  if (rest === '') return { name: 'environment', params: {} };
  const parts = rest.split('/').filter(Boolean).map(decodeURIComponent);
  const [head, ...args] = parts;
  switch (head) {
    case 'epoch':
      return { name: 'epoch', params: { epochId: args[0] || null } };
    case 'experiment':
      return { name: 'experiment', params: { epochId: args[0] || null, genId: args[1] || null } };
    case 'tournament':
      return { name: 'tournament', params: { epochId: args[0] || null } };
    case 'run':
      return { name: 'run', params: { runId: args[0] || null } };
    case 'bench':
      return { name: 'bench', params: {} };
    default:
      return { name: 'environment', params: {} };
  }
}

export function href(name, params = {}) {
  switch (name) {
    case 'environment': return PREFIX + '/';
    case 'epoch': return PREFIX + '/epoch/' + enc(params.epochId);
    case 'experiment': return PREFIX + '/experiment/' + enc(params.epochId) + '/' + enc(params.genId);
    case 'tournament': return PREFIX + '/tournament/' + enc(params.epochId);
    case 'run': return PREFIX + '/run/' + enc(params.runId);
    case 'bench': return PREFIX + '/bench';
    default: return PREFIX + '/';
  }
}

function enc(v) { return encodeURIComponent(v == null ? '' : String(v)); }

// Breadcrumb trail for the top strip, given a parsed route — same shape
// as Variant A's (the navigation that worked).
export function crumbsFor(route) {
  const home = { label: 'environment', href: href('environment') };
  switch (route.name) {
    case 'epoch':
      return [home, { label: route.params.epochId || 'epoch', current: true }];
    case 'tournament':
      return [home,
        { label: route.params.epochId || 'epoch', href: href('epoch', route.params) },
        { label: 'match-ups', current: true }];
    case 'experiment':
      return [home,
        { label: route.params.epochId || 'epoch', href: href('epoch', { epochId: route.params.epochId }) },
        { label: route.params.genId || 'candidate', current: true }];
    case 'run':
      return [home, { label: 'run · ' + (route.params.runId || ''), current: true }];
    case 'bench':
      return [home, { label: 'bench', current: true }];
    default:
      return [{ label: 'environment', current: true }];
  }
}

// Subscribe to hashchange; calls `cb(route)` immediately and on change.
export function startRouter(cb) {
  const fire = () => cb(parseRoute(window.location.hash));
  window.addEventListener('hashchange', fire);
  fire();
}

export function navigate(name, params) {
  window.location.hash = href(name, params);
}

// variants/A/router.js — a tiny hash router under the `#/A/` prefix.
//
// Routes (all prefixed `#/A/`):
//   #/A/                              -> environment (home / fleet)
//   #/A/epoch/:epochId                -> epoch control panel
//   #/A/experiment/:epochId/:genId    -> experiment telemetry readout
//   #/A/tournament/:epochId           -> lineage / gauntlet viz
//   #/A/run/:runId                    -> run transcript (lighter)
//   #/A/bench                         -> bench / live ops (lighter)
//
// The router NEVER recreates the view host on a switch — it parses the
// route and hands it to one render function that paints into the SAME
// persistent root. That is the no-fresh-host guarantee: nav cannot
// orphan listeners or blank the screen.

const PREFIX = '#/A';

export function parseRoute(hash) {
  let h = String(hash || '').trim();
  if (!h.startsWith(PREFIX)) return { name: 'environment', params: {} };
  let rest = h.slice(PREFIX.length).replace(/^\//, '');
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

// Breadcrumb trail for the top strip, given a parsed route.
export function crumbsFor(route) {
  const home = { label: 'environment', href: href('environment') };
  switch (route.name) {
    case 'epoch':
      return [home, { label: route.params.epochId || 'epoch', current: true }];
    case 'tournament':
      return [home,
        { label: route.params.epochId || 'epoch', href: href('epoch', route.params) },
        { label: 'lineage', current: true }];
    case 'experiment':
      return [home,
        { label: route.params.epochId || 'epoch', href: href('epoch', { epochId: route.params.epochId }) },
        { label: route.params.genId || 'experiment', current: true }];
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

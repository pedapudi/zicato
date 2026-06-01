// variants/H/router.js — Atlas II hash router under the `#/H/` prefix.
//
// Variant H ("Atlas II") keeps Variant E's exact IA / breadcrumb flow — the
// hierarchical navigation the operator confirmed is "likely fine" — and adds
// the two views E lacked: the mutation-site × generation matrix and the
// ACM-style epoch publication. The seven screens:
//
//   #/H/                                  → Home / Environment (the fleet)
//   #/H/epoch                             → Epoch (lineage + heatmap + trellis)
//   #/H/epoch/<epochId>                   → Epoch (explicit id)
//   #/H/candidate/<gen>                   → Candidate (generation) — dot-plot + DAG
//   #/H/candidate/<gen>/<entry>           → Candidate drilled into one board entry
//   #/H/matchups                          → Match-ups (gauntlet + slopegraphs + alts)
//   #/H/run/<gen>/<entry>                 → Run detail (the transcript)
//   #/H/mutations                         → Mutation sites × generations matrix
//   #/H/mutations/<mutationId>            → that site drilled into its patch diffs
//   #/H/report                            → ACM-style epoch publication
//   #/H/report/<epochId>                  → report (explicit id)
//
// parseRoute() tolerates a missing / foreign hash (returns Home) so a
// deep-link or stale hash never lands on a blank screen.

export const PREFIX = '#/H';
export const VIEWS = ['home', 'epoch', 'candidate', 'matchups', 'run', 'mutations', 'report'];

export function parseRoute(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  if (!raw.startsWith('/H')) return { view: 'home', params: {} };
  const parts = raw.replace(/^\/H\/?/, '').split('/').filter(Boolean).map(dec);
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
    case 'mutations':
      return { view: 'mutations', params: { mutationId: parts[1] || null } };
    case 'report':
      return { view: 'report', params: { epochId: parts[1] || null } };
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
    case 'mutations':
      return p.mutationId ? `${PREFIX}/mutations/${enc(p.mutationId)}` : `${PREFIX}/mutations`;
    case 'report':
      return p.epochId ? `${PREFIX}/report/${enc(p.epochId)}` : `${PREFIX}/report`;
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
    case 'mutations': {
      const trail = [home, { label: 'epoch', view: 'epoch', params: {} }];
      if (route.params.mutationId) {
        trail.push({ label: 'mutations', view: 'mutations', params: {} });
        trail.push({ label: route.params.mutationId, current: true });
      } else {
        trail.push({ label: 'mutations', current: true });
      }
      return trail;
    }
    case 'report':
      return [home, { label: 'epoch', view: 'epoch', params: {} }, { label: 'report', current: true }];
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

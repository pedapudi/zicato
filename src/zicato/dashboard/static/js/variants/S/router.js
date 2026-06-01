// variants/S/router.js — Variant S ("Lens") tree-node hash router.
//
// S replaces top-tab navigation with a persistent DATA-MODEL TREE. The hash
// encodes the SELECTED tree node AND the optional COMPARISON target, so a cold
// deep-link rebuilds both the tree (expanded to the selection) and the detail
// pane (incl. a split comparison). The scheme mirrors the real hierarchy:
//
//   #/S/                                       → environment (all epochs first)
//   #/S/e/<epochId>                            → epoch overview (heatmap)
//   #/S/e/<epochId>/gen/<gen>                  → candidate (lifecycle · gate · matchups)
//   #/S/e/<epochId>/gen/<gen>/patch            → candidate, patch diff opened
//   #/S/e/<epochId>/gen/<gen>/entry/<entry>    → candidate, one entry drilled
//   #/S/e/<epochId>/board/<entry>              → per-board cross-candidate view
//   #/S/e/<epochId>/mut[/<mutId>]              → mutation surface (+ side-by-side diff)
//   #/S/e/<epochId>/pub                        → epoch publication (ACM)
//
// The comparison target is a query-like suffix on the hash: `~cmp=<gen>` (the
// SECOND candidate, for the split candidate / matchup comparison) and
// `~runs=<genA>,<genB>` (the two candidates whose transcripts are shown side by
// side INLINE on a board). Kept in the hash (not location.search) so it is
// self-contained and one deep-link captures the whole comparison state.

export const PREFIX = '#/S';
export const VIEWS = ['env', 'epoch', 'candidate', 'board', 'mutations', 'publication'];

// Split the hash into its path part and its `~k=v` suffix params.
function splitHash(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  const tildeIdx = raw.indexOf('~');
  const path = tildeIdx >= 0 ? raw.slice(0, tildeIdx) : raw;
  const tail = tildeIdx >= 0 ? raw.slice(tildeIdx) : '';
  const extra = {};
  if (tail) {
    for (const seg of tail.split('~').filter(Boolean)) {
      const eq = seg.indexOf('=');
      if (eq >= 0) extra[seg.slice(0, eq)] = dec(seg.slice(eq + 1));
    }
  }
  return { path, extra };
}

export function parseRoute(hash) {
  const { path, extra } = splitHash(hash);
  if (!path.startsWith('/S')) return { view: 'env', params: {}, cmp: null, runs: null };
  const parts = path.replace(/^\/S\/?/, '').split('/').filter(Boolean).map(dec);
  const cmp = extra.cmp || null;
  const runs = extra.runs ? String(extra.runs).split(',').filter(Boolean) : null;

  if (!parts.length) return { view: 'env', params: {}, cmp, runs };
  // parts[0] === 'e' → an epoch-scoped node.
  if (parts[0] === 'e') {
    const epochId = parts[1] || null;
    const kind = parts[2] || null;
    if (kind === 'gen') {
      const sub = parts[4] || null; // 'patch' | 'entry'
      return {
        view: 'candidate',
        params: { epochId, gen: parts[3] || null, sub, entry: sub === 'entry' ? (parts[5] || null) : null },
        cmp, runs,
      };
    }
    if (kind === 'board') return { view: 'board', params: { epochId, entry: parts[3] || null }, cmp, runs };
    if (kind === 'mut') return { view: 'mutations', params: { epochId, mutId: parts[3] || null }, cmp, runs };
    if (kind === 'pub') return { view: 'publication', params: { epochId }, cmp, runs };
    return { view: 'epoch', params: { epochId }, cmp, runs };
  }
  return { view: 'env', params: {}, cmp, runs };
}

// Build a hash for a tree node. `opts` may carry { cmp, runs } to preserve /
// set the comparison target.
export function href(view, params, opts) {
  const p = params || {};
  let base;
  switch (view) {
    case 'env': base = PREFIX + '/'; break;
    case 'epoch': base = p.epochId ? `${PREFIX}/e/${enc(p.epochId)}` : `${PREFIX}/`; break;
    case 'candidate': {
      let b = `${PREFIX}/e/${enc(p.epochId)}/gen/${enc(p.gen)}`;
      if (p.sub === 'patch') b += '/patch';
      else if (p.sub === 'entry' && p.entry) b += `/entry/${enc(p.entry)}`;
      base = b; break;
    }
    case 'board': base = `${PREFIX}/e/${enc(p.epochId)}/board/${enc(p.entry)}`; break;
    case 'mutations': base = p.mutId ? `${PREFIX}/e/${enc(p.epochId)}/mut/${enc(p.mutId)}` : `${PREFIX}/e/${enc(p.epochId)}/mut`; break;
    case 'publication': base = `${PREFIX}/e/${enc(p.epochId)}/pub`; break;
    default: base = PREFIX + '/';
  }
  const o = opts || {};
  const suffix = [];
  if (o.cmp) suffix.push('cmp=' + enc(o.cmp));
  if (o.runs && o.runs.length) suffix.push('runs=' + enc(o.runs.join(',')));
  return suffix.length ? base + '~' + suffix.join('~') : base;
}

export function navigate(view, params, opts) {
  const target = href(view, params, opts);
  if (location.hash === target) {
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  } else {
    location.hash = target;
  }
}

// The breadcrumb trail for the detail header — [{label, view, params, current}].
export function crumbTrail(route) {
  const p = route.params || {};
  const env = { label: 'environment', view: 'env', params: {} };
  const epoch = p.epochId
    ? { label: p.epochId, view: 'epoch', params: { epochId: p.epochId } }
    : { label: 'epoch', view: 'epoch', params: {} };
  switch (route.view) {
    case 'epoch':
      return [env, { label: p.epochId || 'epoch', current: true }];
    case 'candidate': {
      const trail = [env, epoch];
      if (p.sub === 'entry' && p.entry) {
        trail.push({ label: p.gen || 'candidate', view: 'candidate', params: { epochId: p.epochId, gen: p.gen } });
        trail.push({ label: p.entry, current: true });
      } else if (p.sub === 'patch') {
        trail.push({ label: p.gen || 'candidate', view: 'candidate', params: { epochId: p.epochId, gen: p.gen } });
        trail.push({ label: 'patch', current: true });
      } else {
        trail.push({ label: p.gen || 'candidate', current: true });
      }
      return trail;
    }
    case 'board':
      return [env, epoch, { label: p.entry ? 'board · ' + p.entry : 'board', current: true }];
    case 'mutations':
      return [env, epoch,
        p.mutId ? { label: 'mutations', view: 'mutations', params: { epochId: p.epochId } } : null,
        { label: p.mutId ? p.mutId : 'mutation surface', current: true }].filter(Boolean);
    case 'publication':
      return [env, epoch, { label: 'publication', current: true }];
    default:
      return [{ label: 'environment', current: true }];
  }
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
function dec(s) { try { return s == null ? null : decodeURIComponent(s); } catch { return s || null; } }

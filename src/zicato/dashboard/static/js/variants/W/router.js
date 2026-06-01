// variants/W/router.js — Arena hash router under the `#/W/` prefix.
//
// Variant W ("Arena") is the convergence-IV CREATIVE broadcast take: the
// tournament as live STANDINGS + MATCH CARDS. It keeps Console III's (Variant
// P) data-model TREE sidebar and detail views, folds in S's side-by-side
// COMPARISON detail, and adds a fixed back/up control. The hash therefore
// encodes the FULL path through the hierarchy — Environment → Epoch →
// {Generations|Boards|Mutation surface|Publication} → item — PLUS the optional
// comparison target as a `~`-suffix (S's scheme), so the tree, the standings,
// the detail pane, AND any split comparison all hydrate from a cold deep-link.
//
//   #/W/                                          → Environment / standings home
//   #/W/e/<epochId>                               → Epoch overview (standings + heatmap)
//   #/W/e/<epochId>/gens                          → Generations group landing
//   #/W/e/<epochId>/gen/<gen>[/<entry>][~cmp=<g2>] → Candidate (lifecycle + gate); ~cmp splits A|B
//   #/W/e/<epochId>/gen/<gen>/diff[/<mutId>]      → that candidate's patch diff
//   #/W/e/<epochId>/boards                        → Boards group (trellis)
//   #/W/e/<epochId>/board/<entry>[~runs=A,B]      → per-board + inline side-by-side transcript
//   #/W/e/<epochId>/mutations[/<mutId>]           → Mutation surface + diff
//   #/W/e/<epochId>/paper                         → ACM publication
//
// `~cmp=<gen>` is the second candidate for the split candidate detail;
// `~runs=<genA>,<genB>` is the two candidates whose transcripts show side by
// side on a board. A missing / foreign hash returns the standings home so a
// deep-link never lands blank.

export const PREFIX = '#/W';
export const VIEWS = ['home', 'epoch', 'gens', 'candidate', 'diff', 'boards', 'board', 'mutations', 'publication'];

// Split the hash into its path part and its `~k=v` comparison suffix.
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
  const cmp = extra.cmp || null;
  const runs = extra.runs ? String(extra.runs).split(',').filter(Boolean) : null;
  const blank = (view, params) => ({ view, params: params || {}, cmp, runs });

  if (!path.startsWith('/W')) return blank('home');
  const parts = path.replace(/^\/W\/?/, '').split('/').filter(Boolean).map(dec);
  if (!parts.length || parts[0] === 'home') return blank('home');
  if (parts[0] !== 'e') return blank('home');

  const epochId = parts[1] || null;
  const group = parts[2] || null;
  if (!epochId) return blank('home');
  if (!group) return blank('epoch', { epochId });

  switch (group) {
    case 'gens':
      return blank('gens', { epochId });
    case 'gen': {
      const gen = parts[3] || null;
      if (parts[4] === 'diff') return blank('diff', { epochId, gen, mutId: parts[5] || null });
      return blank('candidate', { epochId, gen, entry: parts[4] || null });
    }
    case 'boards':
      return blank('boards', { epochId });
    case 'board':
      return blank('board', { epochId, entry: parts[3] || null });
    case 'mutations':
      return blank('mutations', { epochId, mutId: parts[3] || null });
    case 'paper': case 'publication': case 'report':
      return blank('publication', { epochId });
    default:
      return blank('epoch', { epochId });
  }
}

export function href(view, params, opts) {
  const p = params || {};
  const e = p.epochId ? `${PREFIX}/e/${enc(p.epochId)}` : null;
  let base;
  switch (view) {
    case 'home': base = PREFIX + '/'; break;
    case 'epoch': base = e || (PREFIX + '/'); break;
    case 'gens': base = e ? `${e}/gens` : PREFIX + '/'; break;
    case 'candidate':
      if (!e || !p.gen) { base = e || PREFIX + '/'; break; }
      base = p.entry ? `${e}/gen/${enc(p.gen)}/${enc(p.entry)}` : `${e}/gen/${enc(p.gen)}`;
      break;
    case 'diff':
      if (!e || !p.gen) { base = e || PREFIX + '/'; break; }
      base = p.mutId ? `${e}/gen/${enc(p.gen)}/diff/${enc(p.mutId)}` : `${e}/gen/${enc(p.gen)}/diff`;
      break;
    case 'boards': base = e ? `${e}/boards` : PREFIX + '/'; break;
    case 'board':
      if (!e || !p.entry) { base = e ? `${e}/boards` : PREFIX + '/'; break; }
      base = `${e}/board/${enc(p.entry)}`;
      break;
    case 'mutations':
      if (!e) { base = PREFIX + '/'; break; }
      base = p.mutId ? `${e}/mutations/${enc(p.mutId)}` : `${e}/mutations`;
      break;
    case 'publication': base = e ? `${e}/paper` : PREFIX + '/'; break;
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

// The route ONE level UP the selection hierarchy — the destination of the
// back/up control. Returns null at the root. The back control renders this into
// the MAIN detail pane (never the sidebar — the round-6 fix).
export function parentRoute(route) {
  const p = (route && route.params) || {};
  switch (route.view) {
    case 'home':
      return null;
    case 'epoch':
      return { view: 'home', params: {} };
    case 'gens': case 'boards': case 'mutations': case 'publication':
      return { view: 'epoch', params: { epochId: p.epochId } };
    case 'candidate':
      // an entry-drill backs up to the candidate; a candidate backs up to gens.
      if (p.entry) return { view: 'candidate', params: { epochId: p.epochId, gen: p.gen } };
      return { view: 'gens', params: { epochId: p.epochId } };
    case 'diff':
      return { view: 'candidate', params: { epochId: p.epochId, gen: p.gen } };
    case 'board':
      return { view: 'boards', params: { epochId: p.epochId } };
    default:
      return { view: 'home', params: {} };
  }
}

// The breadcrumb trail mirrors the tree path. Returns [{label, view, params, current}].
export function crumbTrail(route) {
  const p = route.params || {};
  const home = { label: 'environment', view: 'home', params: {} };
  const epoch = p.epochId ? { label: p.epochId, view: 'epoch', params: { epochId: p.epochId } } : null;
  switch (route.view) {
    case 'epoch':
      return [home, { label: p.epochId || 'epoch', current: true }];
    case 'gens':
      return [home, epoch, { label: 'generations', current: true }].filter(Boolean);
    case 'candidate': {
      const trail = [home, epoch, { label: 'generations', view: 'gens', params: { epochId: p.epochId } }].filter(Boolean);
      if (p.entry) {
        trail.push({ label: p.gen || 'candidate', view: 'candidate', params: { epochId: p.epochId, gen: p.gen } });
        trail.push({ label: p.entry, current: true });
      } else {
        trail.push({ label: p.gen || 'candidate', current: true });
      }
      return trail;
    }
    case 'diff':
      return [home, epoch,
        { label: 'generations', view: 'gens', params: { epochId: p.epochId } },
        { label: p.gen || 'candidate', view: 'candidate', params: { epochId: p.epochId, gen: p.gen } },
        { label: 'patch diff', current: true },
      ].filter(Boolean);
    case 'boards':
      return [home, epoch, { label: 'boards', current: true }].filter(Boolean);
    case 'board':
      return [home, epoch,
        { label: 'boards', view: 'boards', params: { epochId: p.epochId } },
        { label: p.entry || 'board', current: true },
      ].filter(Boolean);
    case 'mutations':
      return [home, epoch,
        p.mutId ? { label: 'mutation surface', view: 'mutations', params: { epochId: p.epochId } } : null,
        { label: p.mutId ? p.mutId : 'mutation surface', current: true },
      ].filter(Boolean);
    case 'publication':
      return [home, epoch, { label: 'publication', current: true }].filter(Boolean);
    default:
      return [{ label: 'environment', current: true }];
  }
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
function dec(s) { try { return s == null ? null : decodeURIComponent(s); } catch { return s || null; } }

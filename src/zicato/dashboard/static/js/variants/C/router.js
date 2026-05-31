// variants/C/router.js — the variant-C hash router.
//
// All routes are prefixed `#/C/...` so variant C coexists with the
// shipped shell (which owns the un-prefixed `#/...` space). The router
// is a pure parser + href builder; the entry point listens to
// `hashchange` and re-renders.
//
//   #/C/env                                   environment (cross-epoch map)
//   #/C/epoch/:epochId                        epoch (lineage + objective + brief)
//   #/C/experiment/:epochId/:genId            experiment (causal Sankey)
//   #/C/lifecycle/:epochId/:genId             candidate lifecycle DAG (theme 1+2)
//   #/C/scoring/:epochId/:genId               per-board scoring Sankey (theme 3)
//   #/C/styles/:epochId                       tournament-style topology switcher (theme 4)
//   #/C/tournament/:epochId                   tournament (gauntlet bracket graph)
//   #/C/run/:runId                            run detail
//   #/C/bench                                 bench / status
//
// Unknown / bare hashes resolve to the environment map.

export function parseRoute(hash) {
  let h = String(hash || '');
  if (h.startsWith('#')) h = h.slice(1);
  // Strip the C prefix.
  const parts = h.split('/').filter(Boolean); // ['C','epoch','id', ...]
  if (parts[0] !== 'C') return { view: 'env', params: {} };
  const seg = parts.slice(1);
  const view = seg[0] || 'env';
  const dec = (s) => { try { return decodeURIComponent(s); } catch { return s; } };
  switch (view) {
    case 'epoch':
      return { view: 'epoch', params: { epochId: seg[1] ? dec(seg[1]) : null } };
    case 'experiment':
      return {
        view: 'experiment',
        params: { epochId: seg[1] ? dec(seg[1]) : null, genId: seg[2] ? dec(seg[2]) : null },
      };
    case 'lifecycle':
      return {
        view: 'lifecycle',
        params: { epochId: seg[1] ? dec(seg[1]) : null, genId: seg[2] ? dec(seg[2]) : null },
      };
    case 'scoring':
      return {
        view: 'scoring',
        params: { epochId: seg[1] ? dec(seg[1]) : null, genId: seg[2] ? dec(seg[2]) : null },
      };
    case 'styles':
      return { view: 'styles', params: { epochId: seg[1] ? dec(seg[1]) : null } };
    case 'tournament':
      return { view: 'tournament', params: { epochId: seg[1] ? dec(seg[1]) : null } };
    case 'run':
      return { view: 'run', params: { runId: seg[1] ? dec(seg[1]) : null } };
    case 'bench':
      return { view: 'bench', params: {} };
    case 'env':
      return { view: 'env', params: {} };
    default:
      return { view: 'env', params: {} };
  }
}

export function href(view, params = {}) {
  const enc = (s) => encodeURIComponent(String(s));
  switch (view) {
    case 'epoch':
      return `#/C/epoch/${enc(params.epochId || '')}`;
    case 'experiment':
      return `#/C/experiment/${enc(params.epochId || '')}/${enc(params.genId || '')}`;
    case 'lifecycle':
      return `#/C/lifecycle/${enc(params.epochId || '')}/${enc(params.genId || '')}`;
    case 'scoring':
      return `#/C/scoring/${enc(params.epochId || '')}/${enc(params.genId || '')}`;
    case 'styles':
      return `#/C/styles/${enc(params.epochId || '')}`;
    case 'tournament':
      return `#/C/tournament/${enc(params.epochId || '')}`;
    case 'run':
      return `#/C/run/${enc(params.runId || '')}`;
    case 'bench':
      return '#/C/bench';
    case 'env':
    default:
      return '#/C/env';
  }
}

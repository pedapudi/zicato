// variants/F/router.js — the variant-F hash router.
//
// All routes are prefixed `#/F/...` so variant F coexists with the
// shipped shell (which owns the un-prefixed `#/...` space). The router
// is a pure parser + href builder; the entry point listens to
// `hashchange` and re-renders.
//
//   #/F/env                                   environment (cross-epoch map)
//   #/F/epoch/:epochId                        epoch (lineage + objective + brief)
//   #/F/experiment/:epochId/:genId            experiment (causal Sankey)
//   #/F/lifecycle/:epochId/:genId             candidate lifecycle DAG (theme 1+2)
//   #/F/scoring/:epochId/:genId               per-board scoring Sankey (theme 3)
//   #/F/styles/:epochId                       tournament-style topology switcher (theme 4)
//   #/F/tournament/:epochId                   tournament (gauntlet bracket graph)
//   #/F/run/:runId                            run detail
//   #/F/bench                                 bench / status
//
// Unknown / bare hashes resolve to the environment map.

export function parseRoute(hash) {
  let h = String(hash || '');
  if (h.startsWith('#')) h = h.slice(1);
  // Strip the F prefix.
  const parts = h.split('/').filter(Boolean); // ['F','epoch','id', ...]
  if (parts[0] !== 'F') return { view: 'env', params: {} };
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
      return `#/F/epoch/${enc(params.epochId || '')}`;
    case 'experiment':
      return `#/F/experiment/${enc(params.epochId || '')}/${enc(params.genId || '')}`;
    case 'lifecycle':
      return `#/F/lifecycle/${enc(params.epochId || '')}/${enc(params.genId || '')}`;
    case 'scoring':
      return `#/F/scoring/${enc(params.epochId || '')}/${enc(params.genId || '')}`;
    case 'styles':
      return `#/F/styles/${enc(params.epochId || '')}`;
    case 'tournament':
      return `#/F/tournament/${enc(params.epochId || '')}`;
    case 'run':
      return `#/F/run/${enc(params.runId || '')}`;
    case 'bench':
      return '#/F/bench';
    case 'env':
    default:
      return '#/F/env';
  }
}

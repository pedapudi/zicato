// views/phase0_router.js — hash routing for the phase-0 level-aligned shell.
//
// The phase-0 redesign reshapes the dashboard around the
// environment -> epoch -> generation -> round -> run hierarchy. The
// canonical URLs are:
//
//   #/workspace
//   #/epoch/<epoch_id>
//   #/gen/<epoch_id>/<generation_id>
//   #/round/<epoch_id>/<champion_id>-><challenger_id>
//   #/run/<epoch_id>/<generation_id>/<entry_id>
//   #/files                — sidebar tool, breadcrumb-aware
//
// (The search affordance is an always-visible sidebar input — not a
// route. Typing filters inline; no navigation away from the current
// page.)
//
// This module ONLY parses the fragment and exposes a structured route.
// The shell installs its own hashchange listener so a fragment update
// repaints.

export const PHASE0_LEVELS = [
  'workspace', 'epoch', 'generation', 'round', 'run', 'files',
];

export const PHASE0_DEFAULT = 'workspace';

// Parse the fragment into { level, params, raw }.
//
// Every level returns the params it needs and silently ignores anything
// past the last expected segment. A malformed fragment falls back to the
// default level — the breadcrumb still renders as ``workspace`` so the
// shell is never blank.
export function parsePhase0Hash(hash) {
  const segs = (hash || '').replace(/^#\/?/, '').split('/').filter(Boolean);
  if (segs.length === 0) {
    return { level: PHASE0_DEFAULT, params: {}, raw: hash || '' };
  }
  const head = segs[0];
  // Map the canonical short segment names to their L-level identity.
  // ``gen`` is the URL-friendly stand-in for ``generation`` — the
  // breadcrumb and view ids still use the full word so the contract
  // is grep-able.
  const aliases = { gen: 'generation' };
  const level = aliases[head] || head;
  if (!PHASE0_LEVELS.includes(level)) {
    return { level: PHASE0_DEFAULT, params: {}, raw: hash || '' };
  }
  const rest = segs.slice(1).map(decodeURIComponent);
  const params = {};
  switch (level) {
    case 'epoch':
      if (rest[0]) params.epochId = rest[0];
      break;
    case 'generation':
      if (rest[0]) params.epochId = rest[0];
      if (rest[1]) params.generationId = rest[1];
      break;
    case 'round':
      if (rest[0]) params.epochId = rest[0];
      // The matchup is encoded as ``<champion>-><challenger>``.
      // Decoding the arrow keeps the URL human-readable in the address
      // bar while making the two ids trivially recoverable from the
      // params dict.
      if (rest[1]) {
        const arrow = rest[1].indexOf('->');
        if (arrow > 0) {
          params.championId = rest[1].slice(0, arrow);
          params.challengerId = rest[1].slice(arrow + 2);
        } else {
          params.matchup = rest[1];
        }
      }
      break;
    case 'run':
      if (rest[0]) params.epochId = rest[0];
      if (rest[1]) params.generationId = rest[1];
      if (rest[2]) params.entryId = rest[2];
      break;
    default:
      break;
  }
  return { level, params, raw: hash || '' };
}

// Build a phase-0 hash for a given level + ordered params. Mirrors the
// URL grammar above. Unused trailing params are simply dropped so the
// caller can pass a fixed-arity tuple per level.
export function phase0Href(level, params) {
  params = params || {};
  switch (level) {
    case 'workspace':
      return '#/workspace';
    case 'epoch':
      return params.epochId
        ? '#/epoch/' + encodeURIComponent(params.epochId)
        : '#/epoch';
    case 'generation':
      if (params.epochId && params.generationId) {
        return (
          '#/gen/' + encodeURIComponent(params.epochId)
          + '/' + encodeURIComponent(params.generationId)
        );
      }
      return '#/gen';
    case 'round':
      if (params.epochId && params.championId && params.challengerId) {
        return (
          '#/round/' + encodeURIComponent(params.epochId)
          + '/' + encodeURIComponent(params.championId)
          + '->' + encodeURIComponent(params.challengerId)
        );
      }
      return '#/round';
    case 'run':
      if (params.epochId && params.generationId && params.entryId) {
        return (
          '#/run/' + encodeURIComponent(params.epochId)
          + '/' + encodeURIComponent(params.generationId)
          + '/' + encodeURIComponent(params.entryId)
        );
      }
      return '#/run';
    case 'files':
      return '#/files';
    default:
      return '#/workspace';
  }
}

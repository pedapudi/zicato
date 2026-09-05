// test/episode_export_link.test.mjs — the way from a candidate to Foe's own
// reading of the episode that proposed it.
//
// Foe renders a finished episode to one self-contained page, and the round
// writes that page beside the episode's log. The dossier already carries
// zicato's summary of the same episode (the proposal header), so the page
// belongs next to it as a link and nothing more: no embed, no live server, no
// second presentation of the round.
//
// Pins:
//   * episodeRow: an available page is a link to the served route; a page that
//     was not rendered is a caption naming the log and the command that
//     renders one; a candidate with no episode at all says nothing;
//   * episodeDigest: folds exactly the three fields that reach the DOM, so a
//     page appearing when a round settles repaints and a no-op beat does not;
//   * the dossier: the link sits inside the proposal header, the seed shows
//     no row, and an identical beat churns zero DOM.

import { installDom, test, run, assert, assertEqual, assertDeep } from './harness.mjs';

installDom();

const { router, EPOCH_ID, freshState, allByClass, installFixtureMap, dossierUrl, recorded } = await import('./fixtures.mjs');
const cand = await import('../js/views/candidate.js');

const LOG = '/w/.zicato/epochs/' + EPOCH_ID + '/episodes/v2/episode.jsonl';
const COMMAND = '/usr/local/bin/foe view /w/.zicato/epochs/' + EPOCH_ID + '/episodes/v2';
const HREF = `/api/generation/${EPOCH_ID}/v2/episode-export.html`;

function served(overrides) {
  return Object.assign({
    epoch_id: EPOCH_ID, generation_id: 'v2', slot: null,
    episode_log: LOG, export_available: true, command: COMMAND,
  }, overrides);
}

// ====================================================================
// The row (pure)
// ====================================================================

test('episodeRow: an available page is one link to the served route', () => {
  const row = cand.episodeRow(served(), HREF);
  assert(row, 'the row rendered');
  const links = row.querySelectorAll('[href]');
  assertEqual(links.length, 1, 'one way in, and the caption is gone');
  assertEqual(links[0].getAttribute('href'), HREF, 'it points at the route that serves the page');
  assertEqual(links[0].getAttribute('target'), '_blank', 'a whole document opens in its own tab');
  assert(!row.textContent.includes(COMMAND), 'an available page says nothing about running a command');
});

test('episodeRow: a page that was not rendered names the log and the command', () => {
  const row = cand.episodeRow(served({ export_available: false }), HREF);
  assert(row, 'the row still rendered — the episode exists, only its page does not');
  assertEqual(row.querySelectorAll('[href]').length, 0, 'nothing links to a page that is not there');
  assert(row.textContent.includes(LOG), 'it names the log on disk');
  assert(row.textContent.includes(COMMAND), 'and the command that renders a page from it');
});

test('episodeRow: no episode at all contributes nothing', () => {
  // The seed was never proposed; a workspace whose configured binary is still
  // the scaffold placeholder never ran an episode either.
  assertEqual(cand.episodeRow(null, HREF), null, 'no payload, no row');
  assertEqual(cand.episodeRow(served({ episode_log: '', command: '' }), HREF), null,
    'an empty log means there is no episode to say anything about');
});

test('episodeRow: an available page with no route to serve it falls back to the caption', () => {
  const row = cand.episodeRow(served(), null);
  assert(row.textContent.includes(LOG), 'without an href the reader still learns where the episode is');
});

// ====================================================================
// The digest fold (pure)
// ====================================================================

test('episodeDigest: exactly the three fields that reach the DOM', () => {
  assertEqual(cand.episodeDigest(null), null, 'no episode contributes nothing');
  assertEqual(cand.episodeDigest(served({ episode_log: '' })), null);
  assertDeep(cand.episodeDigest(served()), [LOG, true, COMMAND]);
  // A round settling writes the page: the digest must move so the row repaints.
  assert(JSON.stringify(cand.episodeDigest(served({ export_available: false })))
    !== JSON.stringify(cand.episodeDigest(served())),
    'a page appearing changes the digest');
  // An identical payload digests identically, which is what keeps a no-op
  // heartbeat from rebuilding the card.
  assertDeep(cand.episodeDigest(served()), cand.episodeDigest(served()));
});

// ====================================================================
// The dossier
// ====================================================================

function dossierFixture(episodeExport) {
  const gens = [
    { generation_id: 'v0', epoch_id: EPOCH_ID, parent_generation_id: '', promoted: true },
    { generation_id: 'v2', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
  ];
  const F = {
    '/api/epoch': {
      epoch_id: EPOCH_ID, closed: true, goal: 'Read the episode.', current_champion: 'v0',
      experiments: [
        { generation_id: 'v0', parent_generation_id: '', decision: 'baseline', promoted: true },
        {
          generation_id: 'v2', parent_generation_id: 'v0', decision: 'rejected', promoted: false,
          hypothesis: { core_idea: 'Ask for an outline first.', modulating: ['prompt.system'] },
          patches: { 'prompt.system': { mutation_id: 'prompt.system', op: 'replace', new_content: 'a' } },
        },
      ],
      board: [{ entry_id: 'b1', kind: 'single_turn' }],
    },
    '/api/lineage': { generations: gens },
    '/api/tournaments': { epoch_id: EPOCH_ID, champion_lineage: ['v0'], matchups: [], tournaments: [] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 0.72 }] },
  };
  for (const g of gens) F[`/api/generation/${EPOCH_ID}/${g.generation_id}/per-entry`] = { entries: [] };
  // the dossier of v2 with the episode export the caller names; a null
  // export is the unserved read.
  F[dossierUrl(EPOCH_ID, 'v2')] = Object.assign(recorded('episodes/candidate/v2'), { episode_export: episodeExport });
  F[dossierUrl(EPOCH_ID, 'v0')] = recorded('console/candidate/v0');
  return F;
}

async function dossier(F, gen) {
  freshState(); installFixtureMap(F); cand._resetDefenceExpansion();
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await cand.render(host, ctx, { epochId: EPOCH_ID, gen });
  return { host, ctx };
}

test('candidate dossier: the episode link sits inside the proposal header', async () => {
  const { host } = await dossier(dossierFixture(served()), 'v2');
  const card = allByClass(host, 'dn-proposal')[0];
  assert(card, 'the proposal header rendered');
  const row = allByClass(card, 'dn-proposal-episode')[0];
  assert(row, 'the episode row is inside the card that summarises the same episode');
  assertEqual(row.querySelectorAll('[href]')[0].getAttribute('href'), HREF);
});

test('candidate dossier: a candidate with no page reads the command instead', async () => {
  const { host } = await dossier(dossierFixture(served({ export_available: false })), 'v2');
  const row = allByClass(host, 'dn-proposal-episode')[0];
  assert(row, 'the row is there');
  assert(row.textContent.includes(COMMAND), 'and it carries the by-hand command');
});

test('candidate dossier: an unreachable episode read leaves the header as it was', async () => {
  // The route 404s (an older workspace, a candidate whose episode was pruned).
  const { host } = await dossier(dossierFixture(null), 'v2');
  assert(allByClass(host, 'dn-proposal')[0], 'the proposal header still renders');
  assertEqual(allByClass(host, 'dn-proposal-episode').length, 0, 'and claims nothing about a page');
});

test('candidate dossier: the SEED shows no episode row (it was never proposed)', async () => {
  const { host } = await dossier(dossierFixture(served()), 'v0');
  assertEqual(allByClass(host, 'dn-proposal-episode').length, 0);
});

test('candidate dossier: digest guardrail — a no-op beat churns NO DOM', async () => {
  const F = dossierFixture(served());
  const { host, ctx } = await dossier(F, 'v2');
  const first = host.firstChild;
  const writes = host.innerHTMLWriteCount();
  await cand.render(host, ctx, { epochId: EPOCH_ID, gen: 'v2' });
  assert(host.firstChild === first, 'identical payload: no clear-and-rebuild');
  assertEqual(host.innerHTMLWriteCount(), writes, 'identical payload: zero additional innerHTML writes');
});

await run();

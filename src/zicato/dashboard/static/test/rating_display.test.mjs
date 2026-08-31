// test/rating_display.test.mjs — the per-candidate visibility rating on the
// three display surfaces (standings table, gens roster, candidate dossier).
//
// The server joins the Bradley–Terry triple (`elo` / `elo_se` / `elo_games`)
// onto lineage nodes and standings entries. The client only FORMATS it, since
// the server computes and the client renders. The formats are: mono `1512 ±34`
// in the quiet-precision register; a faint `provisional` suffix under
// MIN_RATING_GAMES; and `—` when the fold has not rated a generation, or when
// the payload omits the keys (the Rust lineage view). No chips. The rating is
// VISIBILITY-ONLY and nothing here feeds the gate.
// Digest guardrails per touched view: a no-op beat churns zero DOM; a rating
// moving on reindex repaints.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const {
  router, data, STRUCT, EPOCH_ID, FIXTURE, installFetch,
  freshState, allByClass, SWISS_STRUCT, structFixture, installFixtureMap,
} = await import('./fixtures.mjs');

const ui = await import('../js/ui.js');

// ====================================================================
// The ui.js helpers — the ONE formatter home
// ====================================================================

test('ratingModel: integer register, provisional floor, null degrade', () => {
  // unrated: missing / null elo (the Rust view omits the keys entirely).
  assertEqual(ui.ratingModel(null), null);
  assertEqual(ui.ratingModel({}), null);
  assertEqual(ui.ratingModel({ elo: null, elo_se: null, elo_games: null }), null);
  // rated: ints (a rating is a legibility number, never false precision).
  const m = ui.ratingModel({ elo: 1512.4, elo_se: 33.6, elo_games: 7 });
  assertEqual(m.text, '1512 ±34');
  assertEqual(m.games, 7);
  assertEqual(m.provisional, false);
  // the provisionality floor lives HERE (one home): games < MIN_RATING_GAMES.
  assertEqual(ui.MIN_RATING_GAMES, 5);
  assert(ui.ratingModel({ elo: 1500, elo_games: 2 }).provisional, 'thin sample reads provisional');
  assert(!ui.ratingModel({ elo: 1500, elo_games: 5 }).provisional, 'at the floor the suffix drops');
  // a pre-v12 payload (elo without elo_se) still renders the point estimate.
  assertEqual(ui.ratingModel({ elo: 1490.9, elo_games: 6 }).text, '1491');
});

test('ratingTripleDigest: int tuple when rated, null when unrated (pre-rating shape)', () => {
  assertEqual(ui.ratingTripleDigest({ elo: null }), null);
  assertEqual(ui.ratingTripleDigest(undefined), null);
  const d = ui.ratingTripleDigest({ elo: 1512.4, elo_se: 33.6, elo_games: 2 });
  assertEqual(JSON.stringify(d), JSON.stringify([1512, 34, 2, true]));
});

// ====================================================================
// Standings table (structure.js)
// ====================================================================

function ratedSwiss() {
  const st = JSON.parse(JSON.stringify(SWISS_STRUCT));
  // v1 rated + credible; v0 rated + provisional; add an UNRATED third row.
  st.standings[0] = { ...st.standings[0], elo: 1534.4, elo_se: 33.6, elo_games: 7 };
  st.standings[1] = { ...st.standings[1], elo: 1465.6, elo_se: 52.1, elo_games: 2 };
  st.standings.push({ generation_id: 'v2', rank: 3, scalar: 0.6, wins: 0, losses: 1,
    status: 'eliminated', elo: null, elo_se: null, elo_games: null });
  return st;
}

test('standings: the rating column renders mono value ±se, provisional suffix, and — for unrated', async () => {
  freshState();
  installFixtureMap(structFixture('swiss', ratedSwiss(), 'tourn_e0_sw'));
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  const table = allByClass(host, 'dt-standings')[0];
  assert(table, 'the standings leaderboard rendered');
  assert(table.textContent.includes('rating'), 'the standings carries a rating column header');
  assert(table.textContent.includes('1534 ±34'), 'the credible rating renders mono `value ±se`');
  assert(table.textContent.includes('1466 ±52'), 'the thin-sample rating still shows its estimate in the table');
  const provs = allByClass(table, 'dt-rating-prov');
  assertEqual(provs.length, 1, 'exactly the thin-sample row carries the faint provisional suffix');
  assert(provs[0].textContent.includes('provisional'), 'the suffix reads provisional');
  // the unrated row renders the honest dash — and NO chip anywhere.
  const cells = allByClass(table, 'dt-rating');
  assertEqual(cells.length, 3, 'every standings row carries a rating cell');
  assert(cells.some((c) => c.textContent === '—'), 'an unrated generation reads —');
});

test('standings: digest guardrail — a no-op beat churns NO DOM; a rating move repaints', async () => {
  freshState();
  const st = ratedSwiss();
  installFixtureMap(structFixture('swiss', st, 'tourn_e0_sw'));
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await gens.render(host, ctx, { epochId: EPOCH_ID });
  const first = host.firstChild;
  const writes = host.innerHTMLWriteCount();
  await gens.render(host, ctx, { epochId: EPOCH_ID });
  assert(host.firstChild === first, 'identical payload: no clear-and-rebuild (firstChild identity)');
  assertEqual(host.innerHTMLWriteCount(), writes, 'identical payload: zero additional innerHTML writes');

  // structureDigest folds the rating: a reindex that moves it MUST change
  // the digest (repaint), while an identical payload digests identically.
  const a = STRUCT.structureDigest(st);
  assertEqual(STRUCT.structureDigest(JSON.parse(JSON.stringify(st))), a, 'identical standings digest-equal');
  const moved = JSON.parse(JSON.stringify(st));
  moved.standings[0].elo = 1601.0;
  assert(STRUCT.structureDigest(moved) !== a, 'a rating move changes the structure digest');
});

// ====================================================================
// Gens roster (gens.js, gauntlet path)
// ====================================================================

function ratedGauntletFixture() {
  const F = JSON.parse(JSON.stringify(FIXTURE));
  F['/api/lineage'] = { generations: [
    { generation_id: 'v0', epoch_id: EPOCH_ID, parent_generation_id: '', promoted: true,
      elo: 1512.4, elo_se: 33.6, elo_games: 7 },
    { generation_id: 'v1', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false,
      elo: 1487.2, elo_se: 61.0, elo_games: 1 },
    // unrated: the fold has not seen a settled duel for v2 (null triple).
    { generation_id: 'v2', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false,
      elo: null, elo_se: null, elo_games: null },
  ] };
  return F;
}

test('gens roster: the rating cell renders per row (rated / provisional / —)', async () => {
  freshState();
  installFixtureMap(ratedGauntletFixture());
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  const tables = allByClass(host, 'dn-board-table');
  const roster = tables.find((tb) => tb.textContent.includes('rating'));
  assert(roster, 'the roster table carries a rating column');
  assert(roster.textContent.includes('1512 ±34'), 'the credible rating renders');
  assert(roster.textContent.includes('1487 ±61'), 'the provisional rating still shows its estimate');
  const provs = allByClass(roster, 'dt-rating-prov');
  assertEqual(provs.length, 1, 'exactly the 1-game row reads provisional');
  const cells = allByClass(roster, 'dt-rating');
  assertEqual(cells.length, 3, 'every roster row carries a rating cell');
  assert(cells.some((c) => c.textContent === '—'), 'the unrated row reads —');
});

test('gens roster: digest guardrail — no-op beat churns NO DOM; absent triple degrades (Rust view)', async () => {
  freshState();
  installFixtureMap(ratedGauntletFixture());
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await gens.render(host, ctx, { epochId: EPOCH_ID });
  const first = host.firstChild;
  const writes = host.innerHTMLWriteCount();
  await gens.render(host, ctx, { epochId: EPOCH_ID });
  assert(host.firstChild === first, 'identical payload: no clear-and-rebuild');
  assertEqual(host.innerHTMLWriteCount(), writes, 'identical payload: zero additional writes');

  // KEY-ABSENT degrade: the stock FIXTURE lineage has NO rating keys at all
  // (the Rust lineage view / a pre-rating payload) — renders, all dashes.
  freshState();
  installFetch();
  const host2 = document.createElement('div');
  await gens.render(host2, ctx, { epochId: EPOCH_ID });
  const roster2 = allByClass(host2, 'dn-board-table').find((tb) => tb.textContent.includes('rating'));
  assert(roster2, 'the roster still carries the rating column on a key-absent payload');
  const cells2 = allByClass(roster2, 'dt-rating');
  assert(cells2.length >= 3 && cells2.every((c) => c.textContent === '—'),
    'every key-absent row degrades to —');
});

// ====================================================================
// Candidate dossier (candidate.js stat row)
// ====================================================================

test('candidate dossier: the rating stat renders `value ±se · N games`', async () => {
  freshState();
  installFixtureMap(ratedGauntletFixture());
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v0' });

  const stats = allByClass(host, 'dn-stat');
  const ratingStat = stats.find((s) => s.textContent.includes('rating'));
  assert(ratingStat, 'the dossier stat row carries a rating stat');
  assert(ratingStat.textContent.includes('1512 ±34 · 7 games'), 'the credible stat reads value ±se · N games');
});

test('candidate dossier: a thin sample declines the point estimate (provisional · N games); unrated reads —', async () => {
  freshState();
  installFixtureMap(ratedGauntletFixture());
  const candidate = await import('../js/views/candidate.js');
  const ctx = { navigate() {}, href: router.href };

  // v1: 1 game — below MIN_RATING_GAMES the stat declines the number (the
  // ratingBlock forming-state honesty precedent), naming the sample size.
  const hostProv = document.createElement('div');
  await candidate.render(hostProv, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  const provStat = allByClass(hostProv, 'dn-stat').find((s) => s.textContent.includes('rating'));
  assert(provStat, 'the provisional dossier carries the rating stat');
  assert(provStat.textContent.includes('provisional · 1 game'), 'the thin-sample stat reads provisional · N game(s)');
  assert(!provStat.textContent.includes('±'), 'the thin-sample stat declines the point estimate');

  // v2: unrated — the honest dash.
  const hostNull = document.createElement('div');
  await candidate.render(hostNull, ctx, { epochId: EPOCH_ID, gen: 'v2' });
  const nullStat = allByClass(hostNull, 'dn-stat').find((s) => s.textContent.includes('rating'));
  assert(nullStat, 'the unrated dossier still carries the rating stat');
  assert(nullStat.textContent.includes('—'), 'the unrated stat reads —');
});

test('candidate dossier: digest guardrail — no-op beat churns NO DOM; a rating move repaints', async () => {
  freshState();
  const F = ratedGauntletFixture();
  installFixtureMap(F);
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v0' });
  const digest1 = host.getAttribute('data-t-digest');
  const first = host.firstChild;
  const writes = host.innerHTMLWriteCount();
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v0' });
  assertEqual(host.getAttribute('data-t-digest'), digest1, 'no-op beat: dossier digest unchanged');
  assert(host.firstChild === first, 'no-op beat: no clear-and-rebuild');
  assertEqual(host.innerHTMLWriteCount(), writes, 'no-op beat: zero additional writes');

  // a reindex that moves the rating MUST repaint (the digest folds it).
  freshState();
  const moved = ratedGauntletFixture();
  moved['/api/lineage'].generations[0].elo = 1600.0;
  installFixtureMap(moved);
  const host2 = document.createElement('div');
  await candidate.render(host2, ctx, { epochId: EPOCH_ID, gen: 'v0' });
  assert(host2.getAttribute('data-t-digest') !== digest1, 'a rating move changes the dossier digest');
});

run();

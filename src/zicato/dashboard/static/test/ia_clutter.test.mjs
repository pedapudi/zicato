// test/ia_clutter.test.mjs — the IA + clutter pass (issue #194 §7).
//
// Six small guarantees, one per finding, each pinned where it can regress:
//
//   1. the Instrument lens has a RAIL entry, unconditionally, in rail order;
//   2. the epoch page carries NO in-content nav row restating that rail;
//   3. fleet cards print one line of goal prose, and a repeat says so;
//   4. one surface = one NAME (rail · crumb · title all read ROUNDS);
//   5. a figure caption never stacks — the overflow rides a "?" hovercard;
//   6. the top bar carries no page-scale control (it lives in Settings).
//
// (6) is asserted in shell.test.mjs / candidate_surfaces.test.mjs
// where the rest of the top-bar + scale contract already lives.

import { installDom, test, run, assert, assertEqual, assertDeep } from './harness.mjs';

installDom();

const {
  router, tree, ui, EPOCH_ID, FIXTURE, installFetch, installFixtureMap, freshState, allByClass,
} = await import('./fixtures.mjs');

const CTX = { navigate() {}, href: router.href };

function treeModel(extra) {
  return {
    epochs: [{ id: EPOCH_ID, current: true }],
    byEpoch: {
      [EPOCH_ID]: Object.assign({
        gens: [{ id: 'v0', promoted: true, parent: null }],
        boards: [{ id: 'waffles_single' }],
      }, extra || {}),
    },
  };
}

function railKinds(model) {
  const host = document.createElement('div');
  tree.buildTree(host, model, router.parseRoute(`#/e/${EPOCH_ID}`), new Set(['e:' + EPOCH_ID]), CTX, () => {});
  return {
    host,
    kinds: host.querySelectorAll('[data-kind]')
      .map((n) => n.getAttribute('data-kind'))
      .filter((k) => ['group', 'evals', 'instrument', 'traces', 'mutations', 'paper'].includes(k)),
  };
}

// ---- 1. the Instrument lens is REACHABLE BY CLICK -------------------

test('IA: the epoch rail carries an Instrument entry (route + order), reflections or not', () => {
  // no reflections: the lens is STILL on the rail — it was unreachable by click
  // from exactly the epochs that needed to be told it exists.
  const bare = railKinds(treeModel());
  assertDeep(bare.kinds, ['group', 'group', 'evals', 'instrument', 'mutations', 'paper'],
    'rail order: Rounds · Boards · Evals · Instrument · Mutation surface · Publication');
  const instr = bare.host.querySelectorAll('[data-kind]').filter((n) => n.getAttribute('data-kind') === 'instrument')[0];
  assert(instr.textContent.includes('Instrument'), 'the entry is labelled Instrument');

  // with reflections, Traces joins it (a trace is imported INTO a reflection).
  const withRefl = railKinds(treeModel({ hasReflections: true }));
  assertDeep(withRefl.kinds, ['group', 'group', 'evals', 'instrument', 'traces', 'mutations', 'paper'],
    'Traces sits under Instrument when the epoch has reflections');
});

test('IA: clicking the Instrument rail entry navigates to the epoch-scoped instrument route', () => {
  const seen = [];
  const host = document.createElement('div');
  tree.buildTree(host, treeModel(), router.parseRoute(`#/e/${EPOCH_ID}`), new Set(['e:' + EPOCH_ID]),
    { navigate: (view, params) => seen.push([view, params]), href: router.href }, () => {});
  const instr = host.querySelectorAll('[data-kind]').filter((n) => n.getAttribute('data-kind') === 'instrument')[0];
  const label = allByClass(instr, 'dt-label')[0];
  label.dispatchEvent({ type: 'click' });
  assertDeep(seen, [['instrument', { epochId: EPOCH_ID }]], 'the entry navigates to this epoch’s instrument lens');
  assertEqual(router.href('instrument', { epochId: EPOCH_ID }), `#/e/${EPOCH_ID}/instrument`,
    'and that route is the one the router already served');
});

// ---- 2. the epoch page does not restate the rail --------------------

test('IA: the epoch page carries NO in-content nav row duplicating the rail', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, CTX, { epochId: EPOCH_ID });

  assertEqual(allByClass(host, 'dn-quicklinks').length, 0, 'the quicklinks nav row is gone');
  // and none of its four buttons survived under another wrapper: no in-content
  // link points at a bare rail destination for this epoch.
  const railTargets = ['gens', 'boards', 'mutations', 'publication']
    .map((v) => router.href(v, { epochId: EPOCH_ID }));
  const dupes = host.querySelectorAll('[href]')
    .filter((n) => railTargets.includes(n.getAttribute('href')));
  assertEqual(dupes.length, 0, 'no in-content link restates a rail destination');
});

// ---- 3. fleet-card goal prose: one line, and repeats say so ---------

test('clutter: a fleet card shows its goal’s FIRST LINE clipped, with the full text on hover', async () => {
  const home = await import('../js/views/home.js');
  const long = 'Make the presentation agent reliably find the files it creates — eliminate the write/read slug mismatch so reads succeed on the first attempt.\nSecond paragraph nobody reads.';
  const [m] = home.goalModels([{ epoch_id: 'e0', goal: long }]);
  assertEqual(m.kind, 'text', 'a first goal renders as text');
  assert(m.lead.length <= home.GOAL_CLIP, `the lead is clipped to ${home.GOAL_CLIP} chars`);
  assert(m.lead.endsWith('…'), 'the clip is marked with an ellipsis');
  assert(!m.lead.includes('\n') && !m.lead.includes('Second paragraph'), 'only the first line survives');
  assertEqual(m.full, long.trim(), 'the untouched text is kept for the hovercard');
  assertEqual(m.clipped, true, 'the model records that it clipped');

  // short + single-line: no clip, no hovercard.
  const [s] = home.goalModels([{ epoch_id: 'e0', goal: 'crisper' }]);
  assertDeep([s.kind, s.lead, s.clipped], ['text', 'crisper', false], 'a short goal is printed verbatim');
});

test('clutter: identical CONSECUTIVE goals collapse to "same goal as <prev epoch>"', async () => {
  const home = await import('../js/views/home.js');
  const G = 'Make the presentation agent reliably find the files it creates.';
  const models = home.goalModels([
    { epoch_id: 'e0', goal: G },
    { epoch_id: 'e1', goal: G },
    { epoch_id: 'e2', goal: '  ' + G + '  ' },      // whitespace is not a new goal
    { epoch_id: 'e3', goal: 'Something else entirely.' },
    { epoch_id: 'e4', goal: G },                    // a RETURN is not consecutive
    { epoch_id: 'e5', goal: '' },
  ]);
  assertDeep(models.map((m) => m.kind), ['text', 'same', 'same', 'text', 'text', 'none'],
    'only a goal identical to the card BEFORE it collapses');
  assertEqual(models[1].of, 'e0', 'the collapse names the epoch it repeats');
  assertEqual(models[2].of, 'e1', 'a run of repeats each points one card back');
  assertEqual(models[1].full, G, 'the full goal is still carried for the hovercard');

  // the digest folds what is PRINTED, so a repeat and a first differ.
  assert(JSON.stringify(home.goalModelDigest(models[0])) !== JSON.stringify(home.goalModelDigest(models[1])),
    'the digest distinguishes a printed goal from a collapsed one');
});

test('clutter: the rendered fleet prints one goal line per card and collapses the repeat', async () => {
  freshState();
  const G = 'Make the presentation agent reliably find the files it creates — eliminate the write/read slug mismatch so reads succeed.';
  installFixtureMap({
    ...FIXTURE,
    '/api/workspace': {
      current_epoch_id: 'e1',
      epochs: [
        { epoch_id: 'e0', generation_count: 3, promoted_count: 1, best_scalar: 70.9, closed: true, goal: G },
        { epoch_id: 'e1', generation_count: 4, promoted_count: 0, best_scalar: 69.1, closed: false, goal: G },
      ],
      ledger: [], sparkline: [],
    },
  });
  const home = await import('../js/views/home.js');
  const host = document.createElement('div');
  await home.render(host, CTX);
  const goals = allByClass(host, 'dn-fleet-goal');
  assertEqual(goals.length, 2, 'one goal line per fleet card');
  assertEqual(goals[0].textContent.split('\n').length, 1, 'the first card prints a single line');
  assert(goals[0].textContent.length <= ui.truncate(G, 90).length, 'the first card clipped the prose');
  assertEqual(goals[1].textContent, 'same goal as e0', 'the second card says whose goal it is repeating');
  assert(allByClass(host, 'dn-fleet-goal-same').length === 1, 'the repeat is marked for the faint treatment');
});

// ---- 4. one surface, one name --------------------------------------

test('naming: the ROUNDS surface reads the same on the rail, in the crumb, and in the title', async () => {
  // the RAIL.
  const host = document.createElement('div');
  tree.buildTree(host, treeModel(), router.parseRoute(`#/e/${EPOCH_ID}`), new Set(['e:' + EPOCH_ID]), CTX, () => {});
  const group = host.querySelectorAll('[data-kind]').filter((n) => n.getAttribute('data-kind') === 'group')[0];
  const railLabel = allByClass(group, 'dt-text')[0].textContent;

  // the CRUMB (every route whose trail passes through the surface).
  const crumbOf = (hash) => router.crumbTrail(router.parseRoute(hash))
    .filter(Boolean).map((c) => c.label);
  const trails = [
    crumbOf(`#/e/${EPOCH_ID}/gens`),
    crumbOf(`#/e/${EPOCH_ID}/gens/r/0`),
    crumbOf(`#/e/${EPOCH_ID}/gen/v1`),
    crumbOf(`#/e/${EPOCH_ID}/gen/v1/diff`),
  ];
  for (const t of trails) {
    assert(t.includes('rounds'), 'the crumb names the surface "rounds": ' + t.join(' › '));
    assert(!t.includes('generations'), 'no crumb still says "generations": ' + t.join(' › '));
  }

  // the TITLE.
  freshState(); installFetch();
  const gens = await import('../js/views/gens.js');
  const gHost = document.createElement('div');
  await gens.render(gHost, CTX, { epochId: EPOCH_ID });
  const title = allByClass(gHost, 'dn-h1')[0].textContent;

  assertEqual(railLabel, 'Rounds', 'the rail says Rounds');
  assertEqual(title, `Rounds · ${EPOCH_ID}`, 'the title says Rounds');
  assertEqual(railLabel.toLowerCase(), trails[0][trails[0].length - 1], 'rail and crumb agree');
  assert(title.toLowerCase().startsWith(railLabel.toLowerCase()), 'title and rail agree');
  assert(!title.includes('Match-ups') && !title.includes('Generations'),
    'the two retired names are gone from the title');
});

test('naming: a ROUND-SCOPED view titles itself "Round N · match-ups"; the ROUTE is unchanged', async () => {
  freshState(); installFetch();
  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, CTX, { epochId: EPOCH_ID, round: '0' });
  assertEqual(allByClass(host, 'dn-h1')[0].textContent, `Round 0 · match-ups · ${EPOCH_ID}`,
    'a round drill names the round first, then what it is showing');

  // ADDRESSES ARE API: renaming the label must not have touched a single route.
  assertEqual(router.href('gens', { epochId: EPOCH_ID }), `#/e/${EPOCH_ID}/gens`, 'the all-rounds route is unchanged');
  assertEqual(router.href('gens', { epochId: EPOCH_ID, round: 0 }), `#/e/${EPOCH_ID}/gens/r/0`, 'the round route is unchanged');
  assertEqual(router.parseRoute(`#/e/${EPOCH_ID}/gens`).view, 'gens', 'the view id is unchanged');
});

// ---- 5. figure captions do not stack -------------------------------

test('clutter: figCaption prints ONE line and hangs the rest on a "?" hovercard', async () => {
  const { hasHovercard } = await import('../js/hovercard.js');

  const one = ui.figCaption(['the only thing worth saying']);
  assertEqual(one.textContent, 'the only thing worth saying', 'a single line renders bare');
  assertEqual(allByClass(one, 'dn-figcap-more').length, 0, 'no "?" where there was no crowding');

  const many = ui.figCaption(['lead line', 'detail one', 'detail two']);
  assertEqual(allByClass(many, 'dn-figcap-lead')[0].textContent, 'lead line', 'the first line stays visible');
  assert(!many.textContent.includes('detail one'), 'the overflow is NOT printed under the figure');
  const mark = allByClass(many, 'dn-figcap-more')[0];
  assert(mark, 'a "?" affordance carries the rest');
  assert(hasHovercard(mark), 'the "?" is hovercard-wired (the lifecycle DAG’s idiom)');
  assert((mark.getAttribute('aria-label') || '').length > 0, 'the "?" is labelled for assistive tech');

  // nullish / empty lines are dropped rather than printed as blanks.
  assertEqual(ui.figCaption([null, '', '   ']), null, 'an all-empty caption renders nothing');
  assertEqual(allByClass(ui.figCaption(['solo', null, '']), 'dn-figcap-more').length, 0,
    'a lone surviving line still gets no "?"');
});

test('clutter: the board trellis cell collapses its two dim lines to one', async () => {
  const boards = await import('../js/views/boards.js');
  const { hasHovercard } = await import('../js/hovercard.js');

  const cap = boards.trellisCaption({
    entry_id: 'waffles_single', kind: 'single_turn', budget_s: 450, weight: 1,
    tags: ['smoke', 'topic_waffles'], input_preview: 'Make a presentation about waffles.',
  });
  assertEqual(allByClass(cap, 'dn-figcap-lead')[0].textContent, '450s budget · w 1.0',
    'the cell prints one short key line');
  assert(!cap.textContent.includes('waffles.”') && !cap.textContent.includes('smoke'),
    'the prompt and the tags are NOT stacked under the figure');
  const mark = allByClass(cap, 'dn-figcap-more')[0];
  assert(mark && hasHovercard(mark), 'prompt + tags moved onto the "?"');

  // an entry with neither prompt nor tags has nothing to hide → no "?".
  const bare = boards.trellisCaption({ entry_id: 'x', budget_s: 360, weight: 0.5, tags: [] });
  assertEqual(bare.textContent, '360s budget · w 0.5', 'the key line stands alone');
  assertEqual(allByClass(bare, 'dn-figcap-more').length, 0, 'and it needs no "?"');
});

run();

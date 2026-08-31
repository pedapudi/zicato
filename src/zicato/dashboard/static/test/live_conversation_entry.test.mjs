// test/live_conversation_entry.test.mjs — how an operator REACHES the live
// conversation pane: its entry points and its deep-linkable route.
//
// The pane is only worth building if it is findable from where the operator
// already is when they want it: a row that says a unit is running. Two such
// rows exist — the live hero's "what's running" block and the candidate page's
// in-flight table — and both must lead to the SAME deep-linkable place.
//
// The route EXTENDS the existing transcript route family rather
// than forking one: a followed conversation is the board route it already
// lives on, plus a `~follow=1` suffix on the same `~k=v` mechanism `~cmp=`
// uses. So these tests also pin that dropping the suffix lands you back on
// the same board rather than somewhere else.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

const { parseRoute, href, up } = await import('../js/router.js');
const { followRunButton, liveMatchGroupedBlocks } = await import('../js/live.js');

// ---------------------------------------------------------------------------
// The route
// ---------------------------------------------------------------------------

test('~follow=1 parses off the board route without disturbing it', () => {
  const r = parseRoute('#/e/2026-08_e1/board/waffles/v3~follow=1');
  assertEqual(r.view, 'board');
  assertEqual(r.params.entry, 'waffles');
  assertEqual(r.params.gen, 'v3');
  assertEqual(r.follow, true);
});

test('a board route without the suffix does not follow', () => {
  assertEqual(parseRoute('#/e/e1/board/waffles/v3').follow, false);
  assertEqual(parseRoute('#/e/e1/board/waffles').follow, false);
});

test('href round-trips the follow flag', () => {
  const url = href('board', { epochId: 'e1', entry: 'waffles', gen: 'v3' }, { follow: true });
  assertEqual(url, '#/e/e1/board/waffles/v3~follow=1');
  const back = parseRoute(url);
  assertEqual(back.follow, true);
  assertEqual(back.params.gen, 'v3');
});

test('there is nothing to follow without a selected candidate', () => {
  // A bare board has no conversation — the flag must not attach.
  assertEqual(href('board', { epochId: 'e1', entry: 'waffles' }, { follow: true }), '#/e/e1/board/waffles');
});

test('the follow flag rides only the board route', () => {
  assertEqual(href('candidate', { epochId: 'e1', gen: 'v3' }, { follow: true }), '#/e/e1/gen/v3');
});

test('stepping up from a followed conversation closes the pane, one level', () => {
  const dest = up(parseRoute('#/e/e1/board/waffles/v3~follow=1'));
  // Back to the SAME board and candidate — the side-by-side the operator came
  // through — not two levels up to the bare board.
  assertEqual(dest.view, 'board');
  assertEqual(dest.params.entry, 'waffles');
  assertEqual(dest.params.gen, 'v3');
  assertEqual(href(dest.view, dest.params), '#/e/e1/board/waffles/v3');
});

// ---------------------------------------------------------------------------
// Entry point 1 — the live hero's "what's running" rows
// ---------------------------------------------------------------------------

function blocksWithRun(outcome) {
  return [{
    match_id: 'm0', label: 'round 0', kind: 'pair',
    entries: [{
      id: 'v3', outcome, inflight: outcome !== 'win', ratio: 0.4,
      runs: [{ run_id: 'v3--waffles', entry_id: 'waffles' }],
    }],
  }];
}

test('an in-flight what’s-running row offers FOLLOW', () => {
  const seen = [];
  const node = liveMatchGroupedBlocks(blocksWithRun(null), undefined,
    { canControl: false, onFollow: (...a) => seen.push(a) });
  const btn = node.querySelector('[data-follow-run]');
  assert(btn != null, 'no follow affordance on a running row');

  btn.dispatchEvent(makeEvent('click'));
  assertEqual(seen.length, 1);
  // gen, entry, run_id — everything the pane needs to resolve the transcript.
  assertEqual(seen[0][0], 'v3');
  assertEqual(seen[0][1], 'waffles');
  assertEqual(seen[0][2], 'v3--waffles');
});

test('a SETTLED row offers no follow — there is nothing live to watch', () => {
  const node = liveMatchGroupedBlocks(blocksWithRun('win'), undefined,
    { canControl: false, onFollow: () => {} });
  assertEqual(node.querySelector('[data-follow-run]'), null,
    'offered to follow a finished unit');
});

test('no follow sink means no follow affordance', () => {
  const node = liveMatchGroupedBlocks(blocksWithRun(null), undefined, { canControl: false });
  assertEqual(node.querySelector('[data-follow-run]'), null);
});

test('the follow click does not also fire the row’s competitor navigation', () => {
  const opened = [];
  const node = liveMatchGroupedBlocks(blocksWithRun(null), (gen) => opened.push(gen),
    { canControl: false, onFollow: () => {} });
  const btn = node.querySelector('[data-follow-run]');

  // A real bubbling click, so stopPropagation is actually exercised: without
  // it, following a conversation would ALSO navigate away to the candidate
  // page — the operator would end up somewhere they did not ask for.
  btn.dispatchEvent(makeEvent('click'));

  assertEqual(opened.length, 0, 'the follow click bubbled into the row navigation');
});

test('followRunButton names the unit it opens, for the screen reader too', () => {
  const btn = followRunButton('v3', { entry_id: 'waffles', run_id: 'r1' }, () => {});
  assert(String(btn.getAttribute('aria-label')).indexOf('waffles') >= 0);
  assert(String(btn.getAttribute('aria-label')).indexOf('v3') >= 0);
});

await run();

// test/shared.test.mjs — cross-view helper tests.
//
// predictedGateVerdict is the deterministic gate-projection calculator;
// tournamentVerdict distinguishes a regression from a near-miss; the
// entry-status bucket must never mislabel a finished run as queued.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const {
  predictedGateVerdict, tournamentVerdict, entryStatus, dataQuality,
  liveChampionId, liveChallengerId,
} = await import('../js/views/shared.js');

test('entryStatus maps every terminal spelling to done', () => {
  for (const s of ['done', 'complete', 'completed', 'finished']) {
    assertEqual(entryStatus({ status: s }), 'done', s);
  }
  assertEqual(entryStatus({ status: 'COMPLETED' }), 'done', 'case-insensitive');
  assertEqual(entryStatus({ status: 'in_progress' }), 'running');
  assertEqual(entryStatus({ status: 'aborted' }), 'failed');
  assertEqual(entryStatus({}), 'queued', 'absent status falls back to queued');
});

test('predictedGateVerdict: no entries -> tbd', () => {
  const v = predictedGateVerdict({ entries: [] }, 0.05);
  assertEqual(v.verdict, 'tbd');
});

test('predictedGateVerdict: locked pass regression -> reject', () => {
  const v = predictedGateVerdict({
    entries: [
      { status: 'done', parent: { drift_loss: 0.1, pass: true },
        child: { drift_loss: 0.1, pass: false } },
    ],
  }, 0.05);
  assertEqual(v.verdict, 'reject');
  assert(v.reason.includes('regression'));
});

test('predictedGateVerdict: clear win -> promote', () => {
  // Child far ahead on every finished entry, none remaining.
  const v = predictedGateVerdict({
    entries: [
      { status: 'done', parent: { drift_loss: 0.9, pass: false },
        child: { drift_loss: 0.1, pass: true } },
      { status: 'done', parent: { drift_loss: 0.8, pass: false },
        child: { drift_loss: 0.1, pass: true } },
    ],
  }, 0.05);
  assertEqual(v.verdict, 'promote');
});

test('tournamentVerdict distinguishes regression from near-miss', () => {
  assertEqual(tournamentVerdict('promoted', -0.1, 0.05), 'promoted');
  assertEqual(tournamentVerdict('rejected', 0.30, 0.05), 'regression',
    'a clear loss past the margin is a regression');
  assertEqual(tournamentVerdict('rejected', 0.01, 0.05), 'near_miss',
    'a loss inside the margin band is a near-miss');
  assertEqual(tournamentVerdict('rejected', -0.01, 0.05), 'near_miss',
    'a tiny improvement that still lost is a near-miss');
});

test('dataQuality splits the run population by terminal state', () => {
  const q = dataQuality([
    { status: 'done' }, { status: 'done' }, { status: 'completed' },
    { status: 'failed' }, { status: 'failed' },
    { status: 'running' }, { status: 'queued' },
  ]);
  assertEqual(q.total, 7);
  assertEqual(q.completed, 3);
  assertEqual(q.failed, 2);
  assertEqual(q.running, 1);
  assertEqual(q.queued, 1);
});

test('live*Id accessors normalise drifted field names', () => {
  assertEqual(liveChampionId({ parent_generation_id: 'v4' }), 'v4');
  assertEqual(liveChampionId({ parent_id: 'v4' }), 'v4');
  assertEqual(liveChallengerId({ child_generation_id: 'v5' }), 'v5');
  assertEqual(liveChallengerId({ generation_id: 'v5' }), 'v5');
});

await run();

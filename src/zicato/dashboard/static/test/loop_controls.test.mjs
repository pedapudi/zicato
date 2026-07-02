// test/loop_controls.test.mjs — the OPERATOR LOOP CONTROLS (WS4-A item 3).
//
// The topbar pause/resume toggle + skip-round (shell.buildLoopControls) and
// the per-run kill affordance on the live "what's running" rows
// (live.killRunButton / live.runsByGeneration), all driving the previously-
// dead postControl file-based control channel.
//
// Pins:
//   * the toggle REFLECTS `paused`: unpaused → "⏸ pause" fires onPause;
//     paused → "▶ resume" fires onResume — never both;
//   * skip-round is TWO-STEP: the first click only ARMS ("confirm skip?"),
//     the second fires onSkip and disarms — a single stray click can never
//     abort a round;
//   * killRunButton is two-step the same way, fires onKill with the run_id,
//     and stops propagation so the row's competitor navigation never fires;
//   * runsByGeneration groups active runs by generation and DROPS a run whose
//     epoch is known-and-different from the tournament's (the foreign-epoch
//     guard, verbatim from tournamentHasActiveRuns);
//   * LiveController hides every kill affordance on a read-only workspace
//     (canControl:false) and shows them (digest-flipping) when writable.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

const shell = await import('../js/shell.js');
const live = await import('../js/live.js');

function classOf(node) { return (node && node.getAttribute && node.getAttribute('class')) || ''; }
function hasClass(node, cls) { return classOf(node).split(/\s+/).includes(cls); }
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => hasClass(n, cls));
}
function mountInto(node) { const h = document.createElement('div'); if (node) h.appendChild(node); return h; }
function click(node) { node.dispatchEvent(makeEvent('click')); }

// ── 1. the pause/resume toggle reflects `paused` ────────────────────────────
test('buildLoopControls: unpaused shows pause and fires onPause; paused shows resume and fires onResume', () => {
  let paused = 0; let resumed = 0;
  const unpausedHost = mountInto(shell.buildLoopControls({
    paused: false, onPause: () => { paused += 1; }, onResume: () => { resumed += 1; },
  }));
  const pauseBtn = allByClass(unpausedHost, 'dt-loopctl-pause')[0];
  assert(pauseBtn, 'unpaused renders the pause face');
  assertEqual(allByClass(unpausedHost, 'dt-loopctl-resume').length, 0, 'no resume face while unpaused');
  assert(pauseBtn.textContent.includes('pause'), 'the pause word renders');
  click(pauseBtn);
  assertEqual(paused, 1, 'clicking pause fires onPause immediately (pause is reversible — no confirm)');
  assertEqual(resumed, 0, 'onResume never fires from the pause face');

  const pausedHost = mountInto(shell.buildLoopControls({
    paused: true, onPause: () => { paused += 1; }, onResume: () => { resumed += 1; },
  }));
  const resumeBtn = allByClass(pausedHost, 'dt-loopctl-resume')[0];
  assert(resumeBtn, 'paused renders the resume face');
  assert(resumeBtn.textContent.includes('resume'), 'the resume word renders');
  click(resumeBtn);
  assertEqual(resumed, 1, 'clicking resume fires onResume');
  assertEqual(paused, 1, 'onPause never fires from the resume face');
});

// ── 2. skip-round: two-step confirm ─────────────────────────────────────────
test('buildLoopControls: skip-round arms on the first click and fires only on the second', () => {
  let skipped = 0;
  const host = mountInto(shell.buildLoopControls({ paused: false, onSkip: () => { skipped += 1; } }));
  const skip = allByClass(host, 'dt-loopctl-skip')[0];
  assert(skip, 'the skip control renders');
  assert(skip.textContent.includes('skip round'), 'disarmed face reads "skip round"');

  click(skip);
  assertEqual(skipped, 0, 'the FIRST click only arms — nothing posted');
  assert(hasClass(skip, 'dt-loopctl-armed'), 'the armed state is visually explicit');
  assert(skip.textContent.includes('confirm'), 'the armed face asks for confirmation');

  click(skip);
  assertEqual(skipped, 1, 'the SECOND click fires onSkip');
  assert(!hasClass(skip, 'dt-loopctl-armed'), 'firing disarms');
  assert(skip.textContent.includes('skip round'), 'the face resets after firing');
});

// ── 3. the per-run kill button ───────────────────────────────────────────────
test('killRunButton: two-step confirm, fires onKill with the run_id, stops propagation', () => {
  const killed = [];
  const btn = live.killRunButton({ run_id: 'run-42', entry_id: 'waffles' }, (id) => killed.push(id));
  const row = document.createElement('div');
  let rowClicks = 0;
  row.addEventListener('click', () => { rowClicks += 1; });
  row.appendChild(btn);

  assertEqual(btn.getAttribute('data-run'), 'run-42', 'the button is keyed by run_id');
  assertEqual(btn.textContent, '✕', 'disarmed face is the quiet ✕');

  click(btn);
  assertEqual(killed.length, 0, 'the first click only arms');
  assertEqual(btn.textContent, 'kill?', 'the armed face asks');
  assert(hasClass(btn, 'dt-live-kill-armed'), 'armed class applied');

  click(btn);
  assertEqual(killed.length, 1, 'the second click fires');
  assertEqual(killed[0], 'run-42', 'onKill receives the run_id');
  assertEqual(btn.textContent, '✕', 'the face resets');
  assertEqual(rowClicks, 0, 'clicks never bubble into the row (competitor navigation)');
});

// ── 4. runsByGeneration: grouping + the foreign-epoch guard ─────────────────
test('runsByGeneration: groups by generation and drops known-foreign-epoch runs', () => {
  const at = { epoch_id: 'e2' };
  const runs = [
    { run_id: 'r1', generation_id: 'v1', entry_id: 'a', epoch_id: 'e2' },
    { run_id: 'r2', generation_id: 'v1', entry_id: 'b', epoch_id: 'e2' },
    { run_id: 'r3', generation_id: 'v2', entry_id: 'a' },              // untagged: kept
    { run_id: 'r4', generation_id: 'v9', entry_id: 'a', epoch_id: 'e1' }, // foreign: dropped
    { generation_id: 'v3', entry_id: 'c' },                            // no run_id: dropped
  ];
  const by = live.runsByGeneration(runs, at);
  assertEqual(Object.keys(by).sort().join(','), 'v1,v2', 'v9 (foreign) and the id-less run are dropped');
  assertEqual(by.v1.map((r) => r.run_id).join(','), 'r1,r2', 'multiple in-flight runs per generation survive');
  assertEqual(by.v2[0].entry_id, 'a', 'the entry_id rides along for the button title');
});

// ── 5. read-only hides kills; writable shows them (LiveController end-to-end) ──
test('LiveController: kill buttons render only on a writable workspace', () => {
  const at = {
    epoch_id: 'e0', structure: 'gauntlet', phase: 'running',
    parent_generation_id: 'v0', child_generation_id: 'v1',
    competitors: [
      { generation_id: 'v0', role: 'champion' },
      { generation_id: 'v1', role: 'challenger' },
    ],
    rounds: [{ round_index: 0, matches: [{ match_id: 'm0', competitors: ['v0', 'v1'] }] }],
    entries: [], champion_lineage: ['v0'],
  };
  const heartbeat = { phase: 'tournament:round_0', epoch_id: 'e0', last_heartbeat: new Date().toISOString() };
  const activeRuns = [{ run_id: 'r7', generation_id: 'v1', entry_id: 'waffles', epoch_id: 'e0' }];
  const status = { running: true, alive: true, structure: 'gauntlet', inFlight: 1 };

  const kills = [];
  const ctl = new live.LiveController({ onKill: (id) => kills.push(id) });
  // read-only tick: no kill affordance anywhere.
  ctl.update({ status, heartbeat, activeRuns, activeTournament: at, canControl: false });
  assertEqual(allByClass(ctl.node, 'dt-live-kill').length, 0, 'read-only: zero kill buttons');

  // writable tick: the digest flips and the run's kill button appears.
  ctl.update({ status, heartbeat, activeRuns, activeTournament: at, canControl: true });
  const btns = allByClass(ctl.node, 'dt-live-kill');
  assert(btns.length >= 1, 'writable: the in-flight run earns a kill button');
  assertEqual(btns[0].getAttribute('data-run'), 'r7', 'keyed by the active run id');

  // two-step: arm then fire routes the run id to onKill.
  click(btns[0]);
  click(btns[0]);
  assertEqual(kills.join(','), 'r7', 'the controller-wired sink receives the run id');
});

run();

// test/loop_controls.test.mjs — the operator loop controls.
//
// The topbar pause/resume toggle + skip-round (shell.buildLoopControls), both
// driving the postControl file-based control channel.
//
// Pins:
//   * the toggle REFLECTS `paused`: unpaused → "⏸ pause" fires onPause;
//     paused → "▶ resume" fires onResume — never both;
//   * skip-round is TWO-STEP: the first click only ARMS ("confirm skip?"),
//     the second fires onSkip and disarms — a single stray click can never
//     abort a round.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

const shell = await import('../js/shell.js');

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

await run();

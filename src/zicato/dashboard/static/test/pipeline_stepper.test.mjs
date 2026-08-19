// test/pipeline_stepper.test.mjs — the AUTHORITATIVE round-pipeline stepper
// (WS4-A item 4).
//
// The stepper renders the SERVER's /api/live/pipeline projection verbatim
// (build_round_pipeline owns the propose→apply→run→gate inference; the JS
// never re-derives loop position from phase strings for this display).
//
// Pins:
//   * one pip+label per step wearing its dt-pipe-<state> class; the ACTIVE
//     step's detail renders beside it, a done/pending step's detail does not;
//   * the gate decision word renders once the round settles (promoted earns
//     the good tone);
//   * pipelineStepperDigest: a re-served identical projection is byte-
//     identical; a step advancing flips it;
//   * an epoch_open_step (the A/A noise-floor calibration) leads the strip as
//     the ACTIVE element while all four steps sit pending, and its draw count
//     renders — that stretch is work, not a stalled round;
//   * LiveController.updatePipeline: an idle projection (all pending, no
//     decision) or a null read (Rust supervisor) leaves the host EMPTY; a
//     live one renders; an identical re-serve keeps DOM NODE IDENTITY (zero
//     rebuild — the no-flash contract); an advance repaints.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const live = await import('../js/live.js');

function classOf(node) { return (node && node.getAttribute && node.getAttribute('class')) || ''; }
function hasClass(node, cls) { return classOf(node).split(/\s+/).includes(cls); }
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => hasClass(n, cls));
}
function mountInto(node) { const h = document.createElement('div'); if (node) h.appendChild(node); return h; }

// A realistic build_round_pipeline payload (keys verbatim).
function pipeFixture(overrides) {
  return Object.assign({
    running: true, stale: false, phase: 'tournament:round_0:v1',
    epoch_id: 'e0', round_index: 0,
    steps: [
      { id: 'propose', label: 'propose', state: 'done', detail: '' },
      { id: 'apply', label: 'apply', state: 'done', detail: '3 applied · 1 rejected' },
      { id: 'run', label: 'run', state: 'active', detail: '2 units in flight' },
      { id: 'gate', label: 'gate', state: 'pending', detail: '' },
    ],
    active_step: 'run', decision: null, in_flight: 2,
  }, overrides || {});
}
function idlePipe() {
  return pipeFixture({
    running: false, phase: null, active_step: null,
    steps: [
      { id: 'propose', label: 'propose', state: 'pending', detail: '' },
      { id: 'apply', label: 'apply', state: 'pending', detail: '' },
      { id: 'run', label: 'run', state: 'pending', detail: '' },
      { id: 'gate', label: 'gate', state: 'pending', detail: '' },
    ],
    in_flight: 0,
  });
}

test('pipelineStepper: one step per stage wearing its server-projected state', () => {
  const host = mountInto(live.pipelineStepper(pipeFixture()));
  const steps = allByClass(host, 'dt-pipe-step');
  assertEqual(steps.length, 4, 'propose→apply→run→gate');
  assertEqual(steps.map((s) => s.getAttribute('data-step')).join(','), 'propose,apply,run,gate',
    'server order is rendered verbatim');
  assert(hasClass(steps[0], 'dt-pipe-done'), 'propose is done');
  assert(hasClass(steps[2], 'dt-pipe-active'), 'run is active');
  assert(hasClass(steps[3], 'dt-pipe-pending'), 'gate is pending');
  // detail renders on the ACTIVE step only (done/pending stay compact).
  const details = allByClass(host, 'dt-pipe-detail');
  assertEqual(details.length, 1, 'exactly one detail (the active step)');
  assertEqual(details[0].textContent, '2 units in flight', 'the server detail is verbatim');
});

test('pipelineStepper: the settled decision word renders (promoted = good tone)', () => {
  const settled = pipeFixture({
    steps: pipeFixture().steps.map((s) => Object.assign({}, s, { state: 'done', detail: '' })),
    active_step: null, decision: 'promoted',
  });
  const host = mountInto(live.pipelineStepper(settled));
  const dec = allByClass(host, 'dt-pipe-decision')[0];
  assert(dec, 'the decision renders');
  assert(dec.textContent.includes('promoted'), 'the gate verdict word is verbatim');
  assert(hasClass(dec, 'dn-good-t'), 'a promotion reads in the good tone');
});

// The server's projection while the epoch-open A/A calibration draws: every
// pipeline step pending, nothing active, the calibration reporting itself.
function calibratingPipe(detail) {
  return pipeFixture({
    phase: 'evolve_once:calibrating_noise_floor:2/3',
    steps: idlePipe().steps,
    active_step: null,
    epoch_open_step: {
      id: 'calibrating_noise_floor', label: 'calibrating noise floor',
      detail: detail === undefined ? '2/3 draws' : detail,
    },
    in_flight: 1,
  });
}

test('pipelineStepper: the epoch-open calibration leads the strip with its draw count', () => {
  const host = mountInto(live.pipelineStepper(calibratingPipe()));
  const steps = allByClass(host, 'dt-pipe-step');
  assertEqual(steps.length, 5, 'the calibration leads the four pipeline steps');
  assertEqual(steps[0].getAttribute('data-step'), 'calibrating_noise_floor', 'it comes first');
  assert(hasClass(steps[0], 'dt-pipe-active'), 'the calibration is what is happening now');
  assert(steps.slice(1).every((s) => hasClass(s, 'dt-pipe-pending')),
    'the pipeline itself has not started');
  const details = allByClass(host, 'dt-pipe-detail');
  assertEqual(details.length, 1, 'exactly one detail (the calibration)');
  assertEqual(details[0].textContent, '2/3 draws', 'the server label is rendered verbatim');
  // Stamped before the first draw settles: the label stands alone.
  const bare = allByClass(mountInto(live.pipelineStepper(calibratingPipe(''))), 'dt-pipe-detail');
  assertEqual(bare.length, 0, 'no detail element without served progress');
});

test('LiveController.updatePipeline: a calibrating projection renders, and each draw repaints', () => {
  const ctl = new live.LiveController({});
  const host = ctl._pipeHost;

  ctl.updatePipeline(calibratingPipe());
  assertEqual(allByClass(host, 'dt-pipe-step').length, 5,
    'an all-pending pipeline still renders while the calibration runs');
  const first = host.firstChild;

  ctl.updatePipeline(calibratingPipe());
  assert(host.firstChild === first, 'a steady re-serve writes zero DOM');

  ctl.updatePipeline(calibratingPipe('3/3 draws'));
  assert(host.firstChild !== first, 'a completed draw repaints the count');
  assertEqual(allByClass(host, 'dt-pipe-detail')[0].textContent, '3/3 draws');
});

test('pipelineStepperDigest: identical projections fold identically; an advance flips', () => {
  assertEqual(live.pipelineStepperDigest(pipeFixture()), live.pipelineStepperDigest(pipeFixture()),
    'a re-served identical projection is byte-identical (zero DOM)');
  const advanced = pipeFixture();
  advanced.steps[2].state = 'done';
  advanced.steps[3].state = 'active';
  advanced.active_step = 'gate';
  assert(live.pipelineStepperDigest(advanced) !== live.pipelineStepperDigest(pipeFixture()),
    'a step advance flips the digest');
  assertEqual(live.pipelineStepperDigest(null), 'none', 'a null read folds to the stable none');
  assert(live.pipelineStepperDigest(calibratingPipe('3/3 draws'))
    !== live.pipelineStepperDigest(calibratingPipe()),
    'a settled calibration draw flips the digest');
});

test('LiveController.updatePipeline: idle/null → empty host; live renders; identical re-serve keeps node identity', () => {
  const ctl = new live.LiveController({});
  const host = ctl._pipeHost;
  assert(host, 'the hero head carries the pipeline host');

  ctl.updatePipeline(null);
  assertEqual(host.childNodes.length, 0, 'a null read (Rust supervisor) leaves the head unchanged');
  ctl.updatePipeline(idlePipe());
  assertEqual(host.childNodes.length, 0, 'an idle projection renders no stepper');

  ctl.updatePipeline(pipeFixture());
  assertEqual(allByClass(host, 'dt-pipe-step').length, 4, 'a live projection renders the stepper');
  const first = host.firstChild;

  // a steady heartbeat re-serving the SAME projection must write ZERO DOM.
  ctl.updatePipeline(pipeFixture());
  assert(host.firstChild === first, 'identical re-serve keeps DOM node identity (no rebuild)');

  // an advance repaints (new node, new states).
  const advanced = pipeFixture();
  advanced.steps[2].state = 'done';
  advanced.steps[3].state = 'active';
  ctl.updatePipeline(advanced);
  assert(host.firstChild !== first, 'a genuine advance rebuilds the stepper');
  const gate = allByClass(host, 'dt-pipe-step').find((s) => s.getAttribute('data-step') === 'gate');
  assert(hasClass(gate, 'dt-pipe-active'), 'the gate is now the active pip');

  // going idle (hero hidden) clears the stepper.
  ctl.updatePipeline(null);
  assertEqual(host.childNodes.length, 0, 'a null read clears the stepper');
});

await run();

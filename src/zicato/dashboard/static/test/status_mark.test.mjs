// test/status_mark.test.mjs — the top bar's status area reads as one status.
//
// One drawn mark (js/icons.js `status-*`) carries the browser's socket and the
// run verdict together; the run-state word and the "last seen" note follow it.
// A settled workspace whose heartbeat is an hour old reads
// `<one mark> SETTLED · last seen 1h ago`, never two marks side by side.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const shell = await import('../js/shell.js');
const livestatus = await import('../js/livestatus.js');
const { state } = await import('../js/core/state.js');

function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}

// Every drawn mark inside the status area, and every leftover dot element.
function marksIn(statusEl) {
  const icons = statusEl.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('zi'));
  const dots = statusEl.querySelectorAll('[class]').filter((n) =>
    /(^|\s)dt-[a-z-]*dot(\s|$)/.test(n.getAttribute('class') || ''));
  return { icons, dots };
}

function mountChrome() {
  const document = installDom();
  state.lastSeq = -1;
  state.terminal = false;
  state.lastSeqAdvanceAt = NaN;
  state.heartbeat = null;
  state.activeTournament = null;
  state.activeRuns = [];
  state.liveness = null;
  state.connected = false;
  state.connecting = false;
  const listeners = { hashchange: [] };
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  globalThis.EventSource = function EventSource() { this.readyState = 0; this.addEventListener = () => {}; this.close = () => {}; };
  globalThis.EventSource.CLOSED = 2;
  globalThis.fetch = async () => ({ ok: true, async json() { return {}; } });
  globalThis.window = globalThis.window || {};
  globalThis.window.localStorage = globalThis.window.localStorage || { getItem() { return null; }, setItem() {} };
  globalThis.window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  const loc = { _hash: '#/' };
  Object.defineProperty(loc, 'hash', {
    get() { return this._hash; },
    set(v) { this._hash = v; for (const fn of (listeners.hashchange || [])) fn(); },
    configurable: true,
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => {};
  const root = document.createElement('div');
  document.body.appendChild(root);
  shell.mountShell(root);
  return root;
}

const tick = () => new Promise((r) => setTimeout(r, 0));

// Drive the shell into one status and return the status area.
async function statusFor(setup) {
  const root = mountChrome();
  await tick();
  setup();
  state._changed();
  await tick();
  return allByClass(root, 'dt-status')[0];
}

const SCENARIOS = {
  // the reported case: a clean end, heartbeat an hour old, socket healthy.
  settled: () => {
    state.connected = true;
    state.lastSeq = 12;
    state.terminal = true;
    state.liveness = { state: 'settled' };
    state.setHeartbeat({ phase: 'idle', seq: 12, ts: Date.now() - 3_600_000 });
  },
  live: () => {
    state.connected = true;
    state.activeTournament = { structure: 'racing', phase: 'running', competitors: [{ generation_id: 'v0' }] };
    state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0' }];
    state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', seq: 3, ts: Date.now() });
  },
  interrupted: () => {
    state.connected = true;
    state.lastSeq = 4;
    state.liveness = { state: 'interrupted' };
    state.setHeartbeat({ phase: 'tournament:round_0:final', seq: 4, ts: Date.now() - 120_000 });
  },
  offline: () => {
    state.connected = false;
    state.connecting = false;
    state.lastSeq = 12;
    state.liveness = { state: 'settled' };
  },
  idle: () => { state.connected = true; },
};

test('status area: exactly one drawn mark and no dot element, in every state', async () => {
  for (const [name, setup] of Object.entries(SCENARIOS)) {
    const statusEl = await statusFor(setup);
    const { icons, dots } = marksIn(statusEl);
    assertEqual(icons.length, 1, name + ': the status area draws one mark');
    assertEqual(dots.length, 0, name + ': no separate dot element remains');
    const mark = allByClass(statusEl, 'dt-status-mark')[0];
    assert(mark && mark.querySelectorAll('[class]').includes(icons[0]), name + ': the mark is the status mark');
    assertEqual(mark.getAttribute('role'), 'img', name + ': the mark is an image to assistive technology');
    assert((mark.getAttribute('aria-label') || '').length > 10, name + ': the mark carries an accessible label');
  }
});

test('status area: a settled run reads one mark, then SETTLED, then the freshness note', async () => {
  const statusEl = await statusFor(SCENARIOS.settled);
  const mark = allByClass(statusEl, 'dt-status-mark')[0];
  assertEqual(mark.getAttribute('data-state'), 'settled', 'the mark is in its settled state');
  assertEqual(mark.querySelector('[data-icon]').getAttribute('data-icon'), 'status-settled', 'it draws the settled outline');
  assertEqual(statusEl.childNodes[0], mark, 'the mark leads the status area');
  // the spans sit apart by the flex gap, so the text nodes join without a space.
  const text = statusEl.textContent.replace(/\s+/g, ' ').trim();
  assert(/^SETTLED ?· last seen 1h ago$/.test(text), 'the words read "SETTLED · last seen 1h ago" (got "' + text + '")');
});

test('status area: the mark\'s drawing distinguishes running, a clean end, a cut-short end and no verdict', async () => {
  const drawn = {};
  for (const [name, setup] of Object.entries(SCENARIOS)) {
    const statusEl = await statusFor(setup);
    const mark = allByClass(statusEl, 'dt-status-mark')[0];
    drawn[name] = { state: mark.getAttribute('data-state'), icon: mark.querySelector('[data-icon]').getAttribute('data-icon') };
  }
  assertEqual(drawn.live.icon, 'status-running', 'a live run draws the filled mark');
  assertEqual(drawn.settled.icon, 'status-settled', 'a settled run draws the outline');
  assertEqual(drawn.interrupted.icon, 'status-unsettled', 'an interrupted run draws the struck outline');
  assertEqual(drawn.offline.state, 'offline', 'a dropped socket outranks the last run verdict');
  assertEqual(drawn.offline.icon, 'status-unknown', 'and draws the dashed outline');
  assertEqual(drawn.idle.icon, 'status-unknown', 'a workspace with no run draws the dashed outline');
});

test('statusMark: every key names an icon the icon set draws', async () => {
  const icons = await import('../js/icons.js');
  for (const [key, m] of Object.entries(livestatus.STATUS_MARKS)) {
    assert(icons.ICON_NAMES.includes(m.icon), key + ' draws ' + m.icon);
  }
  assertEqual(livestatus.statusMark(true, 'live').key, 'offline', 'a broken socket outranks a live verdict');
  assertEqual(livestatus.statusMark(false, '').key, 'idle', 'no run recorded reads idle');
  assertEqual(livestatus.statusMark(false, 'stalled').key, 'stalled', 'a run verdict passes through');
});

await run();

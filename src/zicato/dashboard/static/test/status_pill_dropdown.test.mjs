// test/status_pill_dropdown.test.mjs — status pill dropdown contracts.
//
// Clicking the status pill toggles the dropdown panel anchored just
// below the top bar (#phase0-status-dropdown). Contents vary by state:
//   * RUNNING — KV block + RECENT DECISIONS feed + "Open current run" CTA
//   * IDLE    — KV block + "Open current epoch" CTA
//   * STALE   — KV block + "Last seen Xm ago" hint, no CTA
//   * CONNECTING — minimal "Waiting…" line

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { state } = await import('../js/core/state.js');
const pillMod = await import('../js/components/status_pill.js');
const dropMod = await import('../js/components/status_pill_dropdown.js');

function installDropdownSlot() {
  let stale = document.getElementById('phase0-status-dropdown');
  while (stale) {
    if (stale.parentNode) stale.parentNode.removeChild(stale);
    stale = document.getElementById('phase0-status-dropdown');
  }
  const node = document.createElement('div');
  node.id = 'phase0-status-dropdown';
  node.setAttribute('hidden', '');
  document.body.appendChild(node);
  return node;
}

function resetState() {
  state.heartbeat = null;
  state.activeRuns = [];
  state.activeTournament = null;
  state.connecting = false;
  state.connected = true;
  state.epochDef = null;
}

// --- toggle behaviour -----------------------------------------------

test('clicking the status pill toggles the dropdown panel', () => {
  resetState();
  state.heartbeat = { last_heartbeat: '2026-05-27T00:00:00Z' };
  const panel = installDropdownSlot();
  pillMod._resetStatusPill();
  const pill = pillMod.renderStatusPill();
  document.body.appendChild(pill);

  assert(panel.hasAttribute('hidden'),
    'panel must start hidden');

  // Simulate the click — listeners are wired via el(onClick:).
  pill.dispatchEvent({ type: 'click', target: pill });
  assert(!panel.hasAttribute('hidden'),
    'click must reveal the dropdown panel');
  // Click again — closes.
  pill.dispatchEvent({ type: 'click', target: pill });
  assert(panel.hasAttribute('hidden'),
    'second click must hide the dropdown panel');
});

// --- per-state rendering --------------------------------------------

test('IDLE dropdown renders an Open current epoch CTA when epoch is set',
  () => {
    resetState();
    state.heartbeat = {
      last_heartbeat: new Date(Date.now() - 5_000).toISOString(),
      epoch_id: '2026-05-20_presn',
    };
    state.activeRuns = [];
    const dropdown = dropMod.renderStatusDropdown();
    assertEqual(dropdown.getAttribute('data-state'), 'idle',
      'IDLE state must paint with data-state="idle"');
    const ctas = dropdown.querySelectorAll(
      '[class="phase0-status-dropdown-cta"]');
    assertEqual(ctas.length, 1, 'one CTA must render in IDLE');
    assert(ctas[0].textContent.includes('current epoch'),
      `CTA label must mention "current epoch"; got ${ctas[0].textContent}`);
    assertEqual(ctas[0].getAttribute('href'), '#/epoch/2026-05-20_presn',
      'CTA must link at the current epoch route');
  });

test('RUNNING dropdown renders a runs line + Open current run CTA', () => {
  resetState();
  const future = new Date(Date.now() + 1000).toISOString();
  state.heartbeat = {
    last_heartbeat: future,
    epoch_id: 'e0', generation_id: 'v8', round_index: 1,
  };
  state.activeRuns = [
    { entry_id: 'entry_alpha' },
    { entry_id: 'entry_beta' },
  ];
  const dropdown = dropMod.renderStatusDropdown();
  assertEqual(dropdown.getAttribute('data-state'), 'running',
    'RUNNING state must paint with data-state="running"');
  // The KV block must surface the runs count.
  const text = dropdown.textContent;
  assert(text.includes('2 in flight'),
    `runs count must surface as "2 in flight"; got ${text}`);
  const ctas = dropdown.querySelectorAll(
    '[class="phase0-status-dropdown-cta"]');
  assertEqual(ctas.length, 1, 'one CTA must render in RUNNING');
  assert(ctas[0].textContent.includes('current run'),
    'RUNNING CTA must link at the current run');
  assertEqual(ctas[0].getAttribute('href'), '#/run/e0/v8/entry_alpha',
    'RUNNING CTA must link at the first active run');
});

test('STALE dropdown renders a last-seen hint and no CTA', () => {
  resetState();
  const ancient = new Date(Date.now() - 3600 * 1000).toISOString();
  state.heartbeat = { last_heartbeat: ancient, epoch_id: 'e0' };
  const dropdown = dropMod.renderStatusDropdown();
  assertEqual(dropdown.getAttribute('data-state'), 'stale',
    'STALE state must paint with data-state="stale"');
  const ctas = dropdown.querySelectorAll(
    '[class="phase0-status-dropdown-cta"]');
  assertEqual(ctas.length, 0, 'no CTA must render in STALE');
  const text = dropdown.textContent;
  assert(text.toLowerCase().includes('last seen'),
    `last-seen hint must render; got ${text}`);
});

test('RUNNING dropdown surfaces a RECENT DECISIONS feed from epochDef',
  () => {
    resetState();
    const future = new Date(Date.now() + 1000).toISOString();
    state.heartbeat = {
      last_heartbeat: future,
      epoch_id: 'e0', generation_id: 'v8',
    };
    state.activeRuns = [{ entry_id: 'entry_alpha' }];
    state.epochDef = {
      epoch_id: 'e0',
      experiments: [
        { generation_id: 'v3', verdict: 'promoted', scalar: -24.33 },
        { generation_id: 'v4', verdict: 'rejected', scalar: 42.40 },
        { generation_id: 'v5', verdict: 'rejected', scalar: 5.71 },
      ],
    };
    const dropdown = dropMod.renderStatusDropdown();
    const eyebrows = dropdown.querySelectorAll(
      '[class="phase0-status-dropdown-eyebrow"]');
    assert(eyebrows.length >= 1,
      'an eyebrow must label the recent decisions feed');
    // The reversed slice surfaces v5 first.
    const rows = dropdown.querySelectorAll(
      '[class="phase0-status-recent-row"]');
    assert(rows.length >= 1, 'recent rows must render');
    const text = dropdown.textContent;
    assert(text.includes('v5'),
      `most-recent row id must surface; got ${text}`);
  });

await run();

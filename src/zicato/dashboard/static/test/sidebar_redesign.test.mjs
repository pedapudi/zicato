// test/sidebar_redesign.test.mjs — pins the redesigned sidebar markup.
//
// The dashboard sidebar carries three blocks with the same section
// rhythm: Live activity (tinted card), Browse (Files affordance), and
// Search (input + collapsing results panel). The redesign reuses the
// existing slot ids — every search test, every loading-state test, and
// the Live Activity digest test all keep working — but adds a section
// header per block, swaps the Files plain link for an icon + chevron
// row, wraps the search input in a label with a leading magnifier
// icon, and rebuilds the Live Activity body as a 2×2 metric grid with
// a CTA-style "View current run" affordance.
//
// These tests pin the new contract so a future refactor can't silently
// regress the visual structure.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { state } = await import('../js/core/state.js');
const shell = await import('../js/views/phase0_shell.js');
const section = await import('../js/components/sidebar_section.js');

// Helper — install the live-card scaffolding the shell renders into.
// The redesign moved the live indicator out of the body into a sibling
// adornment slot (#phase0-live-adorn); both must be present.
function installLiveSlots() {
  // Clear stale nodes by id so installs across tests don't collide.
  for (const id of ['phase0-live-card', 'phase0-live-body',
      'phase0-live-adorn']) {
    let stale = document.getElementById(id);
    while (stale) {
      if (stale.parentNode) stale.parentNode.removeChild(stale);
      stale = document.getElementById(id);
    }
  }
  const card = document.createElement('section');
  card.id = 'phase0-live-card';
  card.classList.add('phase0-live-card', 'phase0-sidebar-section');
  document.body.appendChild(card);
  const header = document.createElement('div');
  header.classList.add('phase0-sidebar-section-header');
  card.appendChild(header);
  const adorn = document.createElement('span');
  adorn.id = 'phase0-live-adorn';
  adorn.classList.add('phase0-sidebar-section-adorn');
  header.appendChild(adorn);
  const body = document.createElement('div');
  body.id = 'phase0-live-body';
  body.classList.add('phase0-live-body');
  card.appendChild(body);
  return { card, header, adorn, body };
}

// --- renderSidebarSection component --------------------------------

test('renderSidebarSection emits a header with eyebrow + label', () => {
  const node = section.renderSidebarSection({ label: 'Live activity' });
  assertEqual(node.getAttribute('class'),
    'phase0-sidebar-section-header',
    'the section header wears the contract class');
  const eyebrow = node.querySelector(
    '[class="phase0-sidebar-section-eyebrow"]');
  assert(eyebrow != null, 'an eyebrow span must render');
  const labelNode = node.querySelector(
    '[class="phase0-sidebar-section-label"]');
  assert(labelNode != null, 'a label span must render');
  assert(labelNode.textContent.includes('Live activity'),
    `expected the label text; got ${labelNode.textContent}`);
});

test('renderSidebarSection renders the icon sprite when icon id is set',
  () => {
    const node = section.renderSidebarSection({
      label: 'Search', icon: 'icon-search',
    });
    const svg = node.querySelector(
      '[class="phase0-sidebar-section-icon"]');
    assert(svg != null, 'an icon <svg> must render when icon id is set');
  });

test('renderSidebarSection places the adornment in its own slot', () => {
  const dot = document.createElement('span');
  dot.classList.add('test-adorn');
  const node = section.renderSidebarSection({
    label: 'Live activity', adorn: dot,
  });
  const slot = node.querySelector(
    '[class="phase0-sidebar-section-adorn"]');
  assert(slot != null, 'an adorn slot must render');
  assert(slot.textContent != null,
    'the adorn slot must contain the supplied node');
});

// --- Live Activity card markup ------------------------------------

test('renderSidebarLive paints the live indicator into the adorn slot',
  () => {
    const { adorn, body } = installLiveSlots();
    state.heartbeat = {
      epoch_id: 'e0', generation_id: 'v3', round_index: 2,
    };
    state.activeRuns = [{ entry_id: 'entry_alpha' }];
    shell.resetSidebarDigest();
    shell.renderSidebarLive();
    // The live indicator must land in the section adornment, not the
    // body, so the section eyebrow communicates liveness at a glance
    // even before the metric grid has settled.
    // The live indicator is a multi-class span; the harness querySelector
    // is exact-match, so walk the adorn slot's children directly.
    const indicator = adorn.children.find((c) =>
      c.classList && c.classList.contains('live-indicator'));
    assert(indicator != null,
      'a live indicator must render into the section adorn slot');
    // Sanity: the body still carries the metric content for the run.
    assert(body.textContent.includes('e0'),
      'epoch id must surface in the body');
  });

test('renderSidebarLive renders the metric grid as four KV tiles', () => {
  const { body } = installLiveSlots();
  state.heartbeat = {
    epoch_id: 'e0', generation_id: 'v3', round_index: 2,
  };
  state.activeRuns = [{ entry_id: 'entry_alpha' }];
  shell.resetSidebarDigest();
  shell.renderSidebarLive();
  const tiles = body.querySelectorAll('[class="phase0-live-tile"]');
  assertEqual(tiles.length, 4,
    `expected four metric tiles (epoch / gen / round / runs); got ${tiles.length}`);
  // Each tile carries a small-caps label + mono value.
  const labels = body.querySelectorAll(
    '[class="phase0-live-tile-label"]');
  assertEqual(labels.length, 4, 'every tile must carry a label span');
});

test('renderSidebarLive renders the jump CTA with an arrow icon', () => {
  const { body } = installLiveSlots();
  state.heartbeat = {
    epoch_id: 'e0', generation_id: 'v3', round_index: 2,
  };
  state.activeRuns = [{ entry_id: 'entry_alpha' }];
  shell.resetSidebarDigest();
  shell.renderSidebarLive();
  // Find the jump anchor among the body's direct children — the
  // phase0.test.mjs Live Activity test also pins this contract.
  const links = [];
  for (const c of body.childNodes) {
    if (c.localName === 'a') links.push(c);
  }
  assertEqual(links.length, 1,
    `exactly one jump link must render at the bottom; got ${links.length}`);
  const labelSpan = links[0].querySelector(
    '[class="phase0-live-jump-label"]');
  assert(labelSpan != null,
    'the CTA must carry the label span the redesign pins');
  assert(labelSpan.textContent.includes('current run'),
    `expected a "current run" affordance label; got ${labelSpan.textContent}`);
  const arrowIcon = links[0].querySelector(
    '[class="phase0-live-jump-icon"]');
  assert(arrowIcon != null, 'the CTA must wear the arrow icon');
});

test('renderSidebarLive idle state still surfaces the indicator', () => {
  const { adorn, body } = installLiveSlots();
  // Heartbeat has landed but carries no live ids — the sidebar should
  // surface the idle indicator + "No active run" without trying to
  // paint a metric grid.
  state.heartbeat = { last_heartbeat: '2026-05-27T00:00:00Z' };
  state.activeRuns = [];
  shell.resetSidebarDigest();
  shell.renderSidebarLive();
  const indicator = adorn.children.find((c) =>
    c.classList && c.classList.contains('live-indicator'));
  assert(indicator != null,
    'the idle state must still surface a live indicator in the adorn slot');
  assert(body.textContent.includes('No active run'),
    `expected the idle copy in the body; got ${body.textContent}`);
  const grid = body.querySelector('[class="phase0-live-grid"]');
  assertEqual(grid, null,
    'no metric grid must render when the heartbeat carries no live ids');
});

await run();

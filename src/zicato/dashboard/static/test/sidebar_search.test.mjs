// test/sidebar_search.test.mjs — sidebar search bar JS contracts.
//
// The sidebar exposes an always-visible search input + a results panel
// that filters across entries / judges / patches / mutations. These
// tests pin the contracts the frontend module is expected to honour:
//
//   * an empty query hides the results panel completely;
//   * a non-empty query reveals it and lists category headers with
//     ``(no matches)`` for empty categories;
//   * each result row is an anchor with the correct phase-0 hash href;
//   * the input listener debounces fetches and re-runs when state
//     changes (queries are issued through a test-stubbed fetch).

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { state } = await import('../js/core/state.js');
const sb = await import('../js/views/phase0_sidebar_search.js');

// Helper: install the sidebar DOM scaffolding the module reaches for.
function installSidebar() {
  // Clear any prior content (each test creates a fresh DOM via the
  // helper, but `installDom` is module-scoped so we re-add per test).
  document.body.childNodes = [];
  const input = document.createElement('input');
  input.id = 'phase0-sidebar-search-input';
  input.value = '';
  document.body.appendChild(input);
  const panel = document.createElement('div');
  panel.id = 'phase0-sidebar-search-results';
  panel.setAttribute('hidden', '');
  document.body.appendChild(panel);
  return { input, panel };
}

// --- runSearch contracts -------------------------------------------

test('runSearch with empty query hides the results panel', async () => {
  const { panel } = installSidebar();
  sb._resetSidebarSearch();
  // Seed the panel as visible so the hide is observable.
  panel.removeAttribute('hidden');
  panel.appendChild(document.createElement('div'));
  await sb.runSearch('');
  assert(panel.hasAttribute('hidden'),
    'empty query MUST set the hidden attribute on the results panel');
  assertEqual(panel.childNodes.length, 0,
    'empty query MUST clear the panel children');
});

test('runSearch with a query shows the panel and renders category sections',
  async () => {
    const { panel } = installSidebar();
    sb._resetSidebarSearch();
    sb._testHooks.fetch = async () => ({
      entries: [{ id: 'q3_metrics_outline' }],
      judges: [],
      patches: [],
      mutations: [],
    });
    await sb.runSearch('q3');
    assert(!panel.hasAttribute('hidden'),
      'a non-empty query MUST reveal the results panel');
    // Four category blocks must render (ENTRIES / JUDGES / PATCHES /
    // MUTATIONS) even when some are empty — the operator sees that
    // every category was scanned.
    const cats = panel.querySelectorAll('[class="phase0-sidebar-search-cat"]');
    assertEqual(cats.length, 4,
      'all four category blocks must render');
    // The entry hit must surface as a link.
    const text = panel.textContent;
    assert(text.includes('q3_metrics_outline'),
      `expected the entry id in the panel; got ${text}`);
    // And empty categories show "(no matches)".
    assert(text.includes('(no matches)'),
      `expected the (no matches) marker; got ${text}`);
    sb._testHooks.fetch = null;
  });

test('renderSearchResults links each entry to the L4 run href', () => {
  const { panel } = installSidebar();
  state.heartbeat = { epoch_id: 'e0', generation_id: 'v3' };
  sb.renderSearchResults({
    entries: [{ id: 'entry_alpha' }],
    judges: [],
    patches: [],
    mutations: [],
  });
  // Find the anchor link for the entry.
  const links = panel.querySelectorAll('[data-kind="entry"]');
  assert(links.length === 1, `expected one entry anchor, got ${links.length}`);
  const href = links[0].getAttribute('href');
  assertEqual(href, '#/run/e0/v3/entry_alpha',
    `expected the L4 run href, got ${href}`);
});

test('renderSearchResults links a patch to its generation', () => {
  const { panel } = installSidebar();
  sb.renderSearchResults({
    entries: [],
    judges: [],
    patches: [{
      patch_id: 'p1',
      epoch_id: 'e0',
      generation_id: 'v2',
      mutation_id: 'researcher_instruction',
      rationale_snippet: 'why',
    }],
    mutations: [],
  });
  const links = panel.querySelectorAll('[data-kind="patch"]');
  assertEqual(links.length, 1, 'one patch link must render');
  const href = links[0].getAttribute('href');
  assertEqual(href, '#/gen/e0/v2',
    `expected the L2 generation href, got ${href}`);
});

test('renderSearchResults links a mutation to its generation', () => {
  const { panel } = installSidebar();
  sb.renderSearchResults({
    entries: [],
    judges: [],
    patches: [],
    mutations: [{
      mutation_id: 'researcher_instruction',
      epoch_id: 'e0',
      generation_id: 'v2',
      patch_id: 'p1',
    }],
  });
  const links = panel.querySelectorAll('[data-kind="mutation"]');
  assertEqual(links.length, 1, 'one mutation link must render');
  assertEqual(links[0].getAttribute('href'), '#/gen/e0/v2',
    'mutation href must point at the L2 generation');
});

test('renderSearchResults links a judge to the L1 epoch', () => {
  const { panel } = installSidebar();
  state.heartbeat = { epoch_id: 'e0' };
  sb.renderSearchResults({
    entries: [],
    judges: [{ name: 'no_fabricated_numbers' }],
    patches: [],
    mutations: [],
  });
  const links = panel.querySelectorAll('[data-kind="judge"]');
  assertEqual(links.length, 1, 'one judge link must render');
  assertEqual(links[0].getAttribute('href'), '#/epoch/e0',
    'judge href must point at the L1 epoch view');
});

test('initSidebarSearch debounces input and runs after the timer fires',
  async () => {
    const { input, panel } = installSidebar();
    sb._resetSidebarSearch();
    state.heartbeat = { epoch_id: 'e0', generation_id: 'v3' };

    // Capture the scheduled callback so the test drives the timer
    // deterministically.
    let scheduled = null;
    sb._testHooks.scheduleTimer = (fn) => {
      scheduled = fn;
      return 1;
    };
    let fetchedQuery = null;
    sb._testHooks.fetch = async (url) => {
      fetchedQuery = url;
      return {
        entries: [{ id: 'entry_alpha' }],
        judges: [],
        patches: [],
        mutations: [],
      };
    };

    sb.initSidebarSearch();
    // Simulate a typed query — multiple events should coalesce into one
    // pending callback.
    input.value = 'ent';
    input.dispatchEvent({ type: 'input' });
    input.value = 'entry';
    input.dispatchEvent({ type: 'input' });
    assert(scheduled != null, 'a debounce callback must be scheduled');

    // Fire the pending debounce.
    await scheduled();
    // The fetch URL must reflect the final value, not the intermediate
    // keystroke.
    assertEqual(
      fetchedQuery,
      '/api/search?q=entry',
      `expected the final query in the fetch URL, got ${fetchedQuery}`
    );
    // The panel must reveal and render the result.
    assert(!panel.hasAttribute('hidden'), 'panel must reveal after a search');
    assert(panel.textContent.includes('entry_alpha'),
      'the entry id must surface');

    sb._testHooks.fetch = null;
    sb._testHooks.scheduleTimer = null;
  });

await run();

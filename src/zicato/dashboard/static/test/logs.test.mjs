// test/logs.test.mjs — the operator-log pane (views/logs.js, LOGGING.md §5).
//
// Covers: the row/toolbar render off a /api/logs payload, level tone classes,
// the two honest degrade states (transport-down → "unavailable"; empty stream
// → "no logs"), the digest no-op guardrail (two identical fetches ⇒ zero DOM
// rebuild — the render-discipline house rule), and the router round-trip for
// the new #/logs route. The reader's cursor/level filtering is covered by the
// Python tests; this asserts the FRONTEND rendering + gating only.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { router, data, freshState, allByClass, installFixtureMap } = await import('./fixtures.mjs');
const logs = await import('../js/views/logs.js');

const CTX = { navigate() {}, href: router.href };
function fresh() { freshState(); }
function textOf(host) { return host.textContent || ''; }
function hasClass(host, cls) { return allByClass(host, cls).length > 0; }

const LOGS_PAYLOAD = {
  records: [
    { ts: '2026-07-12T08:40:01.100Z', level: 'INFO', component: 'zicato.orchestrator', message: 'loop booted', epoch_id: 'e3', cursor: 0 },
    { ts: '2026-07-12T08:40:02.200Z', level: 'WARNING', component: 'zicato.tournament.runner', message: 'run over budget', epoch_id: 'e3', generation_id: 'g5', run_id: 'g5--faq', cursor: 1 },
    { ts: '2026-07-12T08:40:03.300Z', level: 'ERROR', component: 'zicato.orchestrator', message: 'endpoint outage', epoch_id: 'e3', cursor: 2 },
  ],
  cursor: 2,
  invocation: '20260712T084000Z-4242',
  invocations: [
    { id: '20260712T084000Z-4242', stamp: '20260712T084000Z', pid: 4242, size: 512, mtime: 1.0 },
    { id: '20260712T080000Z-4100', stamp: '20260712T080000Z', pid: 4100, size: 256, mtime: 0.5 },
  ],
  level: null,
};

const EMPTY_PAYLOAD = { records: [], cursor: null, invocation: null, invocations: [], level: null };

// ====================================================================
// ROUTER round-trip — the new workspace-level #/logs route.
// ====================================================================
test('router: logs is a registered VIEW; parseRoute + href round-trip; up() → home', () => {
  assert(router.VIEWS.includes('logs'), 'logs in VIEWS');
  const url = router.href('logs', {});
  assertEqual(url, '#/logs', 'href is the bare workspace-level route');
  const parsed = router.parseRoute(url);
  assertEqual(parsed.view, 'logs', 'parseRoute resolves the logs view');
  const up = router.up({ view: 'logs', params: {} });
  assertEqual(up.view, 'home', 'the log pane steps up to environment');
});

// ====================================================================
// RENDER — rows + toolbar + level tones.
// ====================================================================
test('render: the page head uses the styled wrapper class every other view uses', async () => {
  // Issue #366. `.dn-pagehead` carries the 18px margin under the heading
  // block; `dt-pagehead` matches no rule in either stylesheet, so this view
  // sat tighter against its content than the sixteen views that use the
  // styled class. `dt-` is the controls/token prefix and owns no layout rule.
  fresh();
  installFixtureMap({ '/api/logs': LOGS_PAYLOAD });
  const host = document.createElement('div');
  await logs.render(host, CTX);
  assert(hasClass(host, 'dn-pagehead'), 'the page head carries the styled wrapper class');
  assert(!hasClass(host, 'dt-pagehead'), 'the unstyled wrapper class is gone');
});

test('render: paints one mono row per record, level-toned, with the toolbar', async () => {
  fresh();
  installFixtureMap({ '/api/logs': LOGS_PAYLOAD });
  const host = document.createElement('div');
  await logs.render(host, CTX);
  const rows = allByClass(host, 'dt-logs-row');
  assertEqual(rows.length, 3, 'one row per record');
  // level tones: WARNING → warn, ERROR → bad.
  assert(hasClass(host, 'dt-logs-t-warn'), 'a WARNING row carries the warn tone');
  assert(hasClass(host, 'dt-logs-t-bad'), 'an ERROR row carries the bad tone');
  // the messages + context render.
  assert(textOf(host).includes('run over budget'), 'the warning message is shown');
  assert(textOf(host).includes('g5--faq'), 'the run context is shown');
  // toolbar: an invocation picker + the level-filter chips.
  assert(hasClass(host, 'dt-logs-inv'), 'the invocation picker renders');
  assertEqual(allByClass(host, 'dt-logs-chip').length, 5, 'five level chips (ALL/DEBUG/INFO/WARNING/ERROR)');
});

// ====================================================================
// DEGRADE — honest empty states, never an error.
// ====================================================================
test('degrade: a transport failure shows an honest "unavailable" state', async () => {
  fresh();
  installFixtureMap({}); // no /api/logs → data.logs() catches the 404 → null
  const host = document.createElement('div');
  await logs.render(host, CTX);
  assert(textOf(host).toLowerCase().includes('unavailable'), 'the pane says the log service is unavailable');
  assertEqual(allByClass(host, 'dt-logs-row').length, 0, 'no rows painted on a failure');
});

test('degrade: an empty / no-logs workspace shows an honest empty state', async () => {
  fresh();
  installFixtureMap({ '/api/logs': EMPTY_PAYLOAD });
  const host = document.createElement('div');
  await logs.render(host, CTX);
  assert(textOf(host).toLowerCase().includes('no logs'), 'the pane says there are no logs yet');
  assertEqual(allByClass(host, 'dt-logs-row').length, 0, 'no rows painted for an empty stream');
});

// ====================================================================
// DIGEST NO-OP — two identical fetches ⇒ zero DOM rebuild.
// ====================================================================
test('digest no-op: a second identical render rebuilds ZERO DOM', async () => {
  fresh();
  installFixtureMap({ '/api/logs': LOGS_PAYLOAD });
  const host = document.createElement('div');
  await logs.render(host, CTX);
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  await logs.render(host, CTX);
  assert(host.firstChild === first, 'no clear-and-rebuild on the identical repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

run();

// test/shell_tick_writes.test.mjs — a repeated dispatch writes nothing that
// did not change.
//
// The shell re-dispatches the current route on live ticks. The settings
// drawer (hidden on every non-settings dispatch) and the top bar's up button
// (disabled when the route has no parent) must keep their attributes, classes
// and properties untouched while their state stays the same.

import { installDom, test, run, assertEqual } from './harness.mjs';

installDom();

const { allByClass, freshState, installFetch, mountLiveShell } = await import('./fixtures.mjs');
const { recordWrites, countDisabledWrites } = await import('./write_log.mjs');

const tick = () => new Promise((r) => setTimeout(r, 0));

// Re-dispatch the current route `n` times, as live ticks do.
async function redispatch(n) {
  for (let i = 0; i < n; i++) {
    globalThis.window.dispatchEvent({ type: 'hashchange' });
    await tick();
  }
}

async function mounted(route) {
  freshState(); installFetch();
  const root = mountLiveShell(route);
  await tick();
  await redispatch(1);
  return root;
}

for (const route of ['#/', '#/e/2026-05-30_e0']) {
  test('a repeated dispatch of ' + route + ' writes nothing to the hidden settings drawer', async () => {
    const drawer = allByClass(await mounted(route), 'dt-drawer')[0];
    const writes = await recordWrites(drawer, () => redispatch(5));
    assertEqual(writes.join(', '), '', 'the hidden drawer keeps its data-open and class');
  });

  test('a repeated dispatch of ' + route + ' writes nothing to the up button', async () => {
    const back = allByClass(await mounted(route), 'dt-back')[0];
    const disabledWrites = countDisabledWrites(back);
    const writes = await recordWrites(back, () => redispatch(5));
    assertEqual(writes.join(', '), '', 'the up button keeps its attributes and class');
    assertEqual(disabledWrites(), 0, 'the up button\'s disabled property is not reassigned');
  });
}

test('opening and closing settings still flips the drawer', async () => {
  freshState(); installFetch();
  const root = mountLiveShell('#/');
  await tick();
  const drawer = allByClass(root, 'dt-drawer')[0];
  globalThis.location.hash = '#/settings';
  await tick(); await tick();
  assertEqual(drawer.getAttribute('data-open'), '1', 'the settings route opens the drawer');
  assertEqual(drawer.classList.contains('dt-drawer-open'), true, 'and adds its open class');
  globalThis.location.hash = '#/';
  await tick(); await tick();
  assertEqual(drawer.getAttribute('data-open'), '0', 'a non-settings route closes it');
  assertEqual(drawer.classList.contains('dt-drawer-open'), false, 'and removes its open class');
});

await run();

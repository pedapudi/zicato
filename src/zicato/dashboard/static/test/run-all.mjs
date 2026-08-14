// test/run-all.mjs — run every *.test.mjs file in this directory.
//
// Each file runs in a fresh worker. Dashboard modules keep small render caches;
// sharing one module graph made later files inherit earlier fixtures and fail
// despite passing alone. Workers match the browser's fresh-page boundary.
//
// FOOTGUN THIS GUARDS AGAINST: each file's harness prints its own
// "X passed, Y failed" line, so the FINAL printed line is just the LAST file's
// count — NOT the grand total. A green-looking tail can hide a failing file. The
// real signal is the PROCESS EXIT CODE (0 = all green, 1 = something failed); the
// "TOTAL:" line below makes the aggregate honest and visible too. Verify success
// by EXIT CODE (`echo $?`), never the tail line.
//
// Invoked by `node test/run-all.mjs` and by tests/test_dashboard_js.py.

import { readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { Worker, isMainThread, parentPort, workerData } from 'node:worker_threads';
import { totals, waitForRuns } from './harness.mjs';

const here = dirname(fileURLToPath(import.meta.url));
if (!isMainThread) {
  await import(join(here, workerData));
  await waitForRuns();
  const result = totals();
  process.exitCode = 0;
  parentPort.postMessage({
    ...result,
    failures: result.failures.map(({ name, err }) => ({ name, message: err.message })),
  });
} else {
  const files = readdirSync(here)
    .filter((file) => file.endsWith('.test.mjs'))
    .sort();
  const results = [];
  for (const file of files) {
    process.stdout.write(`\n=== ${file} ===\n`);
    results.push(await new Promise((resolve, reject) => {
      const worker = new Worker(new URL(import.meta.url), { workerData: file });
      let result;
      worker.once('message', (message) => { result = message; });
      worker.once('error', reject);
      worker.once('exit', (code) => code === 0
        ? resolve(result)
        : reject(new Error(`${file} worker exited ${code}`)));
    }));
  }

  const total = results.reduce((sum, result) => ({
    passed: sum.passed + result.passed,
    failed: sum.failed + result.failed,
    files: sum.files + result.files,
    failures: sum.failures.concat(result.failures),
  }), { passed: 0, failed: 0, files: 0, failures: [] });
  process.stdout.write('\n' + '─'.repeat(60) + '\n');
  process.stdout.write(`TOTAL: ${total.passed} passed, ${total.failed} failed across ${total.files} file(s)\n`);
  if (total.failed) {
    process.stdout.write(`\n${total.failed} FAILURE(S):\n`);
    for (const failure of total.failures) {
      process.stdout.write(`  ✗ ${failure.name}\n      ${failure.message}\n`);
    }
  }
  process.stdout.write('─'.repeat(60) + '\n');
  process.exitCode = total.failed ? 1 : 0;
}

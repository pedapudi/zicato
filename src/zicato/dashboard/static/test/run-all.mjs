// test/run-all.mjs — run every *.test.mjs file in this directory.
//
// Each test file installs its own DOM and calls run() itself, so this
// driver just imports them in sequence and aggregates the exit code.
// Invoked by `node test/run-all.mjs` and by tests/test_dashboard_js.py.

import { readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const files = readdirSync(here)
  .filter((f) => f.endsWith('.test.mjs'))
  .sort();

let anyFailed = false;
for (const file of files) {
  process.stdout.write(`\n=== ${file} ===\n`);
  process.exitCode = 0;
  await import(join(here, file));
  if (process.exitCode && process.exitCode !== 0) anyFailed = true;
}

process.exitCode = anyFailed ? 1 : 0;

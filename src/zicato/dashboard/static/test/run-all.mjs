// test/run-all.mjs — run every *.test.mjs file in this directory.
//
// Each test file installs its own DOM and calls run() itself, so this driver
// imports them in sequence and aggregates the result. The test files SHARE the
// harness.mjs module, so its cumulative counters span every file — we read them
// via totals() at the end to print an HONEST grand total.
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
import { totals } from './harness.mjs';

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

// The honest grand total across EVERY file (the harness counters are cumulative
// because all files share the one harness module). This is the line to trust —
// alongside the exit code.
const t = totals();
process.stdout.write('\n' + '─'.repeat(60) + '\n');
process.stdout.write(`TOTAL: ${t.passed} passed, ${t.failed} failed across ${t.files} file(s)\n`);
if (t.failed > 0) {
  process.stdout.write(`\n${t.failed} FAILURE(S):\n`);
  for (const f of t.failures) process.stdout.write(`  ✗ ${f.name}\n      ${f.err.message}\n`);
}
process.stdout.write('─'.repeat(60) + '\n');

// Exit non-zero on ANY failure — from the per-file exit-code signal OR the
// cumulative failure count (belt and suspenders; both must be clean to pass).
process.exitCode = (anyFailed || t.failed > 0) ? 1 : 0;

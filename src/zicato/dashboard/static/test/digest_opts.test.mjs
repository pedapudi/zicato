// test/digest_opts.test.mjs — the generic figure-opts digest (U5).
//
// digestOpts is the SINGLE content digest that replaced the per-figure
// hand-rolled *Digest folds. These pins guard its four load-bearing rules:
// function-drop (the no-op-gate guarantee), key-order independence, 3dp
// rounding (sub-precision jitter never flips), and a real content advance
// flipping the digest.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const svg = await import('../js/svg.js');
const { digestOpts } = svg;

test('digestOpts: identical opts → byte-identical digest', () => {
  const a = { championId: 'v1', scalar: 0.5, rungs: [{ id: 'v2', s: 1 }] };
  const b = { championId: 'v1', scalar: 0.5, rungs: [{ id: 'v2', s: 1 }] };
  assertEqual(digestOpts(a), digestOpts(b), 'same content → same string');
});

test('digestOpts: key-order independence', () => {
  const a = { a: 1, b: 2, c: { x: 1, y: 2 } };
  const b = { c: { y: 2, x: 1 }, b: 2, a: 1 };
  assertEqual(digestOpts(a), digestOpts(b), 'object key order never perturbs the digest');
});

test('digestOpts: FUNCTIONS ARE DROPPED (the no-op-gate rule) — a fresh callback each render does not flip', () => {
  const withFn1 = { id: 'v1', onCompetitor: () => 1, nested: { onClick: function () {} } };
  const withFn2 = { id: 'v1', onCompetitor: () => 2, nested: { onClick: function () {} } };
  const withoutFn = { id: 'v1', nested: {} };
  assertEqual(digestOpts(withFn1), digestOpts(withFn2), 'two different callbacks digest identically');
  assertEqual(digestOpts(withFn1), digestOpts(withoutFn), 'a dropped function is indistinguishable from absent');
});

test('digestOpts: a non-integer finite number rounds to 3dp (sub-precision jitter does not flip)', () => {
  const a = { scalar: 0.40001, nested: { v: 1.9999999 } };
  const b = { scalar: 0.40002, nested: { v: 1.9999998 } };
  assertEqual(digestOpts(a), digestOpts(b), 'sub-3dp jitter folds to the same digest');
  // an integer is preserved exactly (no spurious ".000").
  assert(digestOpts({ n: 7 }).includes('7'), 'an integer stays an integer');
});

test('digestOpts: NaN / undefined / ±Infinity → null (a stable JSON-safe sentinel)', () => {
  const d = digestOpts({ a: NaN, b: undefined, c: Infinity, d: -Infinity });
  assertEqual(d, JSON.stringify({ a: null, b: null, c: null, d: null }), 'all non-finite / undefined fold to null');
});

test('digestOpts: a real content advance FLIPS the digest', () => {
  const before = { rungs: [{ id: 'v2', scalar: 0.5 }] };
  const after = { rungs: [{ id: 'v2', scalar: 0.9 }] };
  assert(digestOpts(before) !== digestOpts(after), 'a scalar advance beyond 3dp flips the digest');
});

test('digestOpts: omit drops named TOP-LEVEL keys (mode flags a fold ignores)', () => {
  const full = { id: 'v1', mini: true, responsive: false };
  const bare = { id: 'v1' };
  assertEqual(digestOpts(full, ['mini', 'responsive']), digestOpts(bare), 'omitted keys do not contribute');
  assert(digestOpts(full) !== digestOpts(full, ['mini']), 'without the omit the flag DOES contribute');
});

test('digestOpts: a non-object arg degrades to a stable empty digest', () => {
  assertEqual(digestOpts(null), '{}', 'null → {}');
  assertEqual(digestOpts(undefined), '{}', 'undefined → {}');
  assertEqual(digestOpts('nope'), '{}', 'a string → {}');
});

await run();

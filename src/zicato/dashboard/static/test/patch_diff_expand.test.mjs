// test/patch_diff_expand.test.mjs — GitHub-style context expansion on the
// per-candidate patch diff.
//
// A patch record holds the SPAN. The lines around it exist only in the
// generations' source trees, so the controls are offered exactly when both
// sides can be read back from a tree, and never when one cannot. These pin:
//   * the bars appear, and one click reveals CONTEXT_STEP more lines;
//   * the gutter reads as the FILE's line numbers, not the span's;
//   * expand-to-edge stops at the file edge and the bar then disappears;
//   * a records-sourced side (no line numbers) gets no controls at all;
//   * a tree that answers for the span but not the file says so.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/router.js');
const data = await import('../js/data.js');
const diff = await import('../js/views/diff.js');

const EPOCH = 'e1';
const CTX = { navigate() {}, href: router.href };
const SITE = 'researcher_instr';
const PATH = 'src/agent/prompts.py';

// A file with the span at lines 21-22 and plenty of room on both sides.
const HEAD = Array.from({ length: 20 }, (_, i) => `head_${i + 1} = ${i + 1}`);
const TAIL = Array.from({ length: 40 }, (_, i) => `tail_${i + 1} = ${i + 1}`);
const V3_SPAN = ['RESEARCHER = """v3"""', 'CITE = True'];
const V5_SPAN = ['RESEARCHER = """v5"""', 'CITE = True'];
const V3_FILE = [...HEAD, ...V3_SPAN, ...TAIL].join('\n');
const V5_FILE = [...HEAD, ...V5_SPAN, ...TAIL].join('\n');
// The same span two lines into the LEFT file: the two columns then have very
// different amounts above them.
const SHORT_HEAD = ['pre_1 = 1', 'pre_2 = 2'];
const V3_SHORT_FILE = [...SHORT_HEAD, ...V3_SPAN, ...TAIL].join('\n');

const LINEAGE = { generations: [
  { generation_id: 'v0', parent_generation_id: null, epoch_id: EPOCH },
  { generation_id: 'v3', parent_generation_id: 'v0', epoch_id: EPOCH },
  { generation_id: 'v5', parent_generation_id: 'v3', epoch_id: EPOCH },
] };

function version(gen, span, opts) {
  return Object.assign({
    generation_id: gen, patch_id: `p${gen}`, op: 'replace', rationale: 'why',
    content: span.join('\n'), provenance: 'snapshot',
    file: PATH, line_start: 21, line_end: 22,
  }, opts || {});
}

function install(opts) {
  const o = opts || {};
  // The LEFT side's file and where its span sits in it; the right side keeps
  // the shared fixture.
  const left = o.left || { file: V3_FILE, start: 21, end: 22 };
  globalThis.fetch = async (path) => {
    const body = (() => {
      if (path.startsWith('/api/epoch')) return { epoch_id: EPOCH };
      if (path.startsWith('/api/lineage')) return LINEAGE;
      if (path === `/api/mutations/${EPOCH}`) return { epoch_id: EPOCH, mutations: [
        { mutation_id: SITE, file: PATH, line_start: 21, line_end: 22 },
      ] };
      if (path.startsWith(`/api/mutations/${EPOCH}/`)) return {
        epoch_id: EPOCH, mutation_id: SITE, file: PATH, line_start: 21, line_end: 22,
        provenance_note: '',
        baseline: { generation_id: 'v0', content: 'seed\n', provenance: 'snapshot', file: PATH, line_start: 21, line_end: 22 },
        versions: o.versions || [
          version('v3', V3_SPAN, { line_start: left.start, line_end: left.end }),
          version('v5', V5_SPAN),
        ],
      };
      if (path.includes('/content')) {
        if (o.contentError) return { path: PATH, error: 'file not found' };
        const content = path.includes('/v5/') ? V5_FILE : left.file;
        return { path: PATH, content, binary: Boolean(o.binary), truncated: Boolean(o.truncated) };
      }
      if (path.includes('/patches')) return { patches: [
        { id: 'p5', mutation_id: SITE, op: 'replace', new_content: V5_SPAN.join('\n'), rationale: 'why' },
      ] };
      return null;
    })();
    if (body == null) return { ok: false, status: 404, json: async () => ({}) };
    return { ok: true, json: async () => body };
  };
}

async function renderDiff(opts) {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  install(opts);
  const host = globalThis.document.createElement('div');
  await diff.render(host, CTX, { epochId: EPOCH, gen: 'v5', mutId: SITE });
  return host;
}

const withClass = (host, cls) => host.querySelectorAll('[class]')
  .filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls));
const buttons = (host) => withClass(host, 'dn-sxs-xbtn');
const rows = (host) => withClass(host, 'dn-sxs-row');
const gutters = (host) => withClass(host, 'dn-sxs-gutter').map((n) => n.textContent);
const textOf = (host) => String(host.textContent || '');

async function click(btn) {
  btn.dispatchEvent({ type: 'click', target: btn });
  // the handler fetches before repainting
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
}

test('expand: the span renders alone, with the bars offered', async () => {
  const host = await renderDiff({});
  assertEqual(rows(host).length, 2, 'only the span is shown at first');
  const labels = buttons(host).map((b) => b.textContent);
  assertEqual(labels.join('|'), '↑ 20 lines|⤒ file start|↓ 20 lines|⤓ file end', 'both bars, both controls');
});

test('expand: one click up reveals 20 more lines above', async () => {
  const host = await renderDiff({});
  await click(buttons(host)[0]);
  assertEqual(rows(host).length, 22, 'the span plus twenty lines');
  assert(textOf(host).includes('head_1 = 1'), 'and it reached the top of the file');
});

test('expand: the gutter reads as the FILE line numbers', async () => {
  const host = await renderDiff({});
  await click(buttons(host)[0]);
  // left gutter of the first row: line 1 of the file, not line 1 of the span.
  assertEqual(gutters(host)[0], '1', 'the first revealed line is file line 1');
  const spanRow = rows(host)[20];
  const g = spanRow.querySelectorAll('[class]')
    .filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-sxs-gutter'))
    .map((n) => n.textContent);
  assertEqual(g[0], '21', 'the span still sits at file line 21');
});

test('expand: the bar disappears once that side is fully expanded', async () => {
  const host = await renderDiff({});
  await click(buttons(host)[1]);  // ⤒ file start
  const labels = buttons(host).map((b) => b.textContent);
  assert(!labels.some((l) => l.includes('↑')), `the up bar is gone: ${labels.join('|')}`);
  assert(labels.some((l) => l.includes('↓')), 'the down bar remains');
});

test('expand: down stops at the end of the file', async () => {
  const host = await renderDiff({});
  const down = buttons(host).filter((b) => b.textContent.includes('⤓'))[0];
  await click(down);
  assertEqual(rows(host).length, 42, 'the span plus every trailing line');
  assert(textOf(host).includes('tail_40 = 40'), 'the last line is shown');
});

test('expand: a records-sourced side gets no controls', async () => {
  // No line numbers means no file to expand into. Offering a control that
  // cannot run is worse than not offering it.
  const host = await renderDiff({ versions: [
    version('v3', V3_SPAN, { provenance: 'records', file: undefined, line_start: undefined, line_end: undefined }),
    version('v5', V5_SPAN),
  ] });
  assertEqual(buttons(host).length, 0, 'no expand controls');
  assertEqual(rows(host).length, 2, 'the span still renders');
});

test('expand: a tree that cannot serve the file says so', async () => {
  const host = await renderDiff({ contentError: true });
  await click(buttons(host)[0]);
  assert(textOf(host).includes('not readable'), `the failure is stated: ${textOf(host)}`);
  assertEqual(buttons(host).length, 0, 'and the dead control is withdrawn');
});

test('expand: a truncated or binary read is not the file', async () => {
  // The read endpoint caps an inline body, so the last line of a truncated
  // read is a CUT. Expanding into one would label that cut "file end".
  for (const flag of ['truncated', 'binary']) {
    const host = await renderDiff({ [flag]: true });
    await click(buttons(host)[0]);
    assert(textOf(host).includes('not readable in full'), `${flag}: the limit is stated`);
    assertEqual(buttons(host).length, 0, `${flag}: and no edge is claimed`);
  }
});

test('expand: the shorter column does not retire the bar for the longer one', async () => {
  // v3's span sits two lines into its file, v5's twenty. One click must take
  // each column as far as ITS file allows, not stop both at two.
  const host = await renderDiff({ left: { file: V3_SHORT_FILE, start: 3, end: 4 } });
  await click(buttons(host)[0]);
  const text = textOf(host);
  assert(text.includes('head_1 = 1'), 'the right column reached its own file start');
  assert(text.includes('pre_1 = 1'), 'and the left column reached its own');
  assert(!buttons(host).some((b) => b.textContent.includes('↑')), 'only now is the up bar retired');
});

test('expand: a tree that disagrees with the record keeps the record', async () => {
  // The file's span text differs from what the patch record holds. Growing
  // into it would swap one source for the other under unchanged labels.
  const drifted = [...HEAD, 'RESEARCHER = """drifted"""', 'CITE = True', ...TAIL].join('\n');
  const host = await renderDiff({ left: { file: drifted, start: 21, end: 22 } });
  await click(buttons(host)[0]);
  const text = textOf(host);
  assert(text.includes('no longer matches the patch record'), `the drift is stated: ${text}`);
  assert(text.includes('"""v3"""'), 'the record’s span is still what is on screen');
  assert(!text.includes('drifted'), 'and the tree’s text never replaces it');
  assertEqual(buttons(host).length, 0, 'the bars withdraw rather than showing the other source');
});

await run();

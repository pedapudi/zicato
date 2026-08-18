// test/patch_diff_parent.test.mjs — the patch diff is taken against the
// candidate's PARENT, not against the seed (issue #253).
//
// Only a v1 off the seed has v0 for a parent. A mid-chain candidate — v3 → v5
// — diffed against v0 answers "what changed since the seed" under a heading
// that promises this one candidate's patch set. These pin the fix:
//   * the left column holds the content the site had in the PARENT, taken
//     from the nearest ancestor that patched it;
//   * the column is LABELLED with the generation it actually is;
//   * a site no ancestor ever touched still falls back to the v0 baseline —
//     which for that site IS what the parent held;
//   * the full-file fallback stops labelling the server's parent as v0;
//   * the baseline is PICKABLE — `~base=<gen>` moves the left column to any
//     other generation, the parent link carries no suffix, and a pick the
//     full-file fallback cannot honour is declared rather than mislabelled;
//   * only the BASE is pickable. The right column is the candidate you
//     clicked into, and the rows are that candidate's own patch set. Against
//     an earlier pick the LINES stop being that candidate's alone, and the
//     affected block says so.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/router.js');
const data = await import('../js/data.js');
const diff = await import('../js/views/diff.js');

const EPOCH = 'e1';
const CTX = { navigate() {}, href: router.href };
const SITE = 'researcher_instr';
const UNTOUCHED = 'planner_instr';

const V0_TEXT = 'RESEARCHER = """seed instruction"""\n';
const V3_TEXT = 'RESEARCHER = """v3 instruction"""\n';
const V5_TEXT = 'RESEARCHER = """v5 instruction"""\n';

function textOf(host) {
  return String(host.textContent || '');
}
function labels(host) {
  return host.querySelectorAll('[class]')
    .filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-sxs-col-h'))
    .map((n) => n.textContent || '');
}

// v0 → v1 → v3 → v5. v5's recorded parent is v3, so the left column of v5's
// patch diff is v3.
const LINEAGE = {
  generations: [
    { generation_id: 'v0', parent_generation_id: null, epoch_id: EPOCH },
    { generation_id: 'v1', parent_generation_id: 'v0', epoch_id: EPOCH },
    { generation_id: 'v3', parent_generation_id: 'v1', epoch_id: EPOCH },
    { generation_id: 'v5', parent_generation_id: 'v3', epoch_id: EPOCH },
  ],
};

function detailFor(mutationId, versions) {
  return {
    epoch_id: EPOCH,
    mutation_id: mutationId,
    file: 'agent/prompts.py',
    line_start: 2,
    line_end: 2,
    provenance_note: '',
    baseline: { generation_id: 'v0', content: V0_TEXT, provenance: 'snapshot' },
    versions,
  };
}

function install(opts) {
  const o = opts || {};
  globalThis.fetch = async (path) => {
    const body = (() => {
      if (path.startsWith('/api/epoch')) return { epoch_id: EPOCH };
      if (path.startsWith('/api/lineage')) return LINEAGE;
      if (path === `/api/mutations/${EPOCH}`) {
        return { epoch_id: EPOCH, mutations: [
          { mutation_id: SITE, file: 'agent/prompts.py', line_start: 2, line_end: 2 },
          { mutation_id: UNTOUCHED, file: 'agent/prompts.py', line_start: 9, line_end: 9 },
        ] };
      }
      if (path === `/api/mutations/${EPOCH}/${UNTOUCHED}`) {
        // No ancestor ever patched this site — only v5 did.
        return detailFor(UNTOUCHED, [
          { generation_id: 'v5', patch_id: 'p9', op: 'replace', rationale: 'widen', content: 'PLANNER = """v5"""\n', provenance: 'snapshot' },
        ]);
      }
      if (path.startsWith(`/api/mutations/${EPOCH}/`)) {
        return detailFor(SITE, o.versions || [
          // v1 and v3 both patched the site; v3 is the nearer ancestor.
          { generation_id: 'v1', patch_id: 'p1', op: 'replace', rationale: 'first pass', content: 'RESEARCHER = """v1 instruction"""\n', provenance: 'snapshot' },
          { generation_id: 'v3', patch_id: 'p3', op: 'replace', rationale: 'second pass', content: V3_TEXT, provenance: 'snapshot' },
          { generation_id: 'v5', patch_id: 'p5', op: 'replace', rationale: 'sharper brief', content: V5_TEXT, provenance: 'snapshot' },
        ]);
      }
      if (path.includes('/patches')) {
        return { patches: o.patches !== undefined ? o.patches : [
          { id: 'p5', mutation_id: SITE, op: 'replace', new_content: V5_TEXT, rationale: 'sharper brief' },
        ] };
      }
      if (path.includes('/diff')) {
        return {
          epoch_id: EPOCH,
          generation_id: 'v5',
          parent_generation_id: 'v3',
          provenance: 'snapshot',
          provenance_note: '',
          files: [{ path: 'agent/prompts.py', status: 'modified', old_content: V3_TEXT, new_content: V5_TEXT }],
        };
      }
      return null;
    })();
    if (body == null) return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
    return { ok: true, json: async () => body };
  };
}

async function renderDiff(opts, params) {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  install(opts);
  const host = globalThis.document.createElement('div');
  await diff.render(host, CTX, Object.assign({ epochId: EPOCH, gen: 'v5' }, params || {}));
  return host;
}

test('patch diff: the left column is the PARENT, not the seed', async () => {
  const host = await renderDiff({}, { mutId: SITE });
  const text = textOf(host);
  assert(text.includes('v3 instruction'), 'the parent content is the left column');
  assert(!text.includes('seed instruction'), 'the seed content is NOT shown as the baseline');
  assert(text.includes('v5 instruction'), 'the challenger side is still the candidate');
});

test('patch diff: the left column is labelled with the generation it is', async () => {
  const host = await renderDiff({}, { mutId: SITE });
  assertEqual(labels(host)[0], 'baseline · v3', 'the label names the parent');
  assertEqual(labels(host)[1], 'challenger new · v5', 'the right side names the candidate');
});

test('patch diff: the lede names the generation it diffed against', async () => {
  const host = await renderDiff({}, { mutId: SITE });
  assert(textOf(host).includes('Left: v3'), 'the lede says what the left column is');
});

test('patch diff: a site no ancestor touched falls back to the v0 baseline', async () => {
  // The parent held the SEED content at that site, so v0 is the right answer
  // there — and the label must say v0, not v3.
  const host = await renderDiff({ patches: [
    { id: 'p9', mutation_id: UNTOUCHED, op: 'replace', new_content: 'PLANNER = """v5"""\n', rationale: 'widen' },
  ] }, { mutId: UNTOUCHED });
  assert(textOf(host).includes('seed instruction'), 'the v0 baseline is the left column');
  assertEqual(labels(host)[0], 'baseline · v0', 'and is labelled v0');
});

test('patch diff: a parent record with no content says so, and does not step back', async () => {
  // v3's record could not be reconstructed. Showing v1's text under the name
  // "v3" would be a quiet lie; the note is the honest answer.
  const host = await renderDiff({ versions: [
    { generation_id: 'v1', patch_id: 'p1', op: 'replace', rationale: 'first pass', content: 'RESEARCHER = """v1 instruction"""\n', provenance: 'snapshot' },
    { generation_id: 'v3', patch_id: 'p3', op: 'set_numeric', rationale: 'second pass', content: null, provenance: 'records' },
  ] }, { mutId: SITE });
  const text = textOf(host);
  assert(!text.includes('v1 instruction'), 'no older generation is shown under the parent name');
  assert(text.includes('No content recorded for this site in v3'), `the note names v3: ${text}`);
});

test('patch diff: the full-file fallback stops labelling the parent as v0', async () => {
  const host = await renderDiff({ patches: [] }, {});
  assertEqual(labels(host)[0], 'baseline · v3', 'the fallback names the server parent');
  assert(textOf(host).includes('agent/prompts.py'), 'and still renders the file entry');
});

test('patch diff: a picked baseline moves the left column', async () => {
  // v1 patched the site before v3 did. Picking v1 must show v1's text, not
  // the parent's — the whole point of the picker.
  const host = await renderDiff({}, { mutId: SITE, base: 'v1' });
  const text = textOf(host);
  assert(text.includes('v1 instruction'), 'the picked version is the left column');
  assert(!text.includes('v3 instruction'), 'the parent is no longer the baseline');
  assertEqual(labels(host)[0], 'baseline · v1', 'and the label names the pick');
  assert(text.includes('Left: v1'), 'the lede names the pick too');
});

test('patch diff: a picked baseline reads THROUGH its own chain', async () => {
  // v0 is a legal pick, and no ancestor of v0 patched anything — so the
  // site's v0 content answers. A pick is not restricted to generations that
  // happened to patch the site.
  const host = await renderDiff({}, { mutId: SITE, base: 'v0' });
  assert(textOf(host).includes('seed instruction'), 'the seed is a legal pick');
  assertEqual(labels(host)[0], 'baseline · v0', 'labelled as picked');
});

test('patch diff: the picker offers every OTHER generation, parent marked', async () => {
  const host = await renderDiff({}, { mutId: SITE });
  const row = host.querySelectorAll('[class]')
    .filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-basepick'))[0];
  assert(row, 'the picker renders');
  const links = row.querySelectorAll('[href]');
  assertEqual(links.map((a) => a.textContent).join('|'), 'v0|v1|v3 · parent', 'every other gen, parent marked');
  // The DEFAULT view keeps one canonical URL — the parent link has no suffix.
  const parentLink = links[2].getAttribute('href');
  assert(!parentLink.includes('~base='), `the parent link carries no suffix: ${parentLink}`);
  assert(links[0].getAttribute('href').includes('~base=v0'), 'a non-parent pick rides ~base=');
  assert(!links.some((a) => a.textContent.includes('v5')), 'the candidate is not its own baseline');
});

test('patch diff: the picked baseline is the one marked on', async () => {
  const host = await renderDiff({}, { mutId: SITE, base: 'v1' });
  const on = host.querySelectorAll('[class]')
    .filter((a) => (a.getAttribute('class') || '').split(/\s+/).includes('dn-linkbtn-on'))
    .map((a) => a.textContent);
  assertEqual(on.join('|'), 'v1', 'the pick is the current chip');
});

test('patch diff: an unknown pick falls back to the parent, not to nothing', async () => {
  const host = await renderDiff({}, { mutId: SITE, base: 'v99' });
  assertEqual(labels(host)[0], 'baseline · v3', 'a foreign pick degrades to the parent');
});

test('patch diff: the picked baseline folds into the digest', async () => {
  // The digest gates the repaint. Changing ONLY the pick must repaint.
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  install({});
  const host = globalThis.document.createElement('div');
  await diff.render(host, CTX, { epochId: EPOCH, gen: 'v5', mutId: SITE });
  assert(textOf(host).includes('v3 instruction'), 'starts on the parent');
  await diff.render(host, CTX, { epochId: EPOCH, gen: 'v5', mutId: SITE, base: 'v1' });
  assert(textOf(host).includes('v1 instruction'), 'repaints when only the pick changed');
});

test('patch diff: the fallback declares a pick it cannot honour', async () => {
  // /api/files/{e}/{g}/diff diffs against the recorded parent only.
  const host = await renderDiff({ patches: [] }, { base: 'v1' });
  const text = textOf(host);
  assert(text.includes('your pick of v1 does not apply here'), `the pick is declared: ${text}`);
  assertEqual(labels(host)[0], 'baseline · v3', 'and the column keeps the parent name');
});

test('patch diff: the picked baseline survives a round trip through the hash', async () => {
  // The pick is deep-linkable — a cold load on the shared URL must hydrate
  // the same baseline, and the parent (default) URL must stay canonical.
  const withPick = router.href('diff', { epochId: EPOCH, gen: 'v5', base: 'v1' });
  assertEqual(router.parseRoute(withPick).params.base, 'v1', 'the pick parses back');
  assertEqual(router.parseRoute(withPick).params.gen, 'v5', 'and the candidate survives it');

  const pinnedPick = router.href('diff', { epochId: EPOCH, gen: 'v5', mutId: SITE, base: 'v1' });
  const pinnedRoute = router.parseRoute(pinnedPick);
  assertEqual(pinnedRoute.params.mutId, SITE, 'a pinned site survives the suffix');
  assertEqual(pinnedRoute.params.base, 'v1', 'alongside the pick');

  const bare = router.href('diff', { epochId: EPOCH, gen: 'v5' });
  assert(!bare.includes('~base='), 'the default view keeps one canonical URL');
  assertEqual(router.parseRoute(bare).params.base, null, 'and parses to no pick');
});

test('patch diff: only the BASE is pickable — the candidate stays fixed', async () => {
  // The right column is the candidate you clicked into. No pick may move it.
  for (const base of [null, 'v0', 'v1']) {
    const host = await renderDiff({}, { mutId: SITE, base });
    assertEqual(labels(host)[1], 'challenger new · v5', `right column fixed under base=${base}`);
    assert(textOf(host).includes('v5 instruction'), 'and still shows the candidate content');
  }
});

test('patch diff: the rows are the CANDIDATE\'s own patch set, whatever the base', async () => {
  // v1 and v3 both patched the site too. Their patches are not rows here —
  // only what v5 itself wrote is.
  const host = await renderDiff({}, { base: 'v0' });
  const text = textOf(host);
  assert(text.includes('sharper brief'), "v5's own rationale is a row");
  assert(!text.includes('first pass'), "v1's patch is not a row");
  assert(!text.includes('second pass'), "v3's patch is not a row");
  assert(text.includes('1'), 'the patched-site count covers only this candidate');
});

test('patch diff: an earlier base flags the block whose lines are not the candidate\'s', async () => {
  const host = await renderDiff({}, { mutId: SITE, base: 'v0' });
  const text = textOf(host);
  assert(text.includes('not v5’s change alone'), `the block is flagged: ${text}`);
  assert(text.includes('Diff against v3 for that'), 'and points back at the parent');
});

test('patch diff: the parent view carries no such flag', async () => {
  // Against the parent every rendered line IS the candidate's own edit.
  const host = await renderDiff({}, { mutId: SITE });
  assert(!textOf(host).includes('change alone'), 'nothing to warn about');
});

test('patch diff: a base that matches the parent at the site is not flagged', async () => {
  // v1 is earlier than the parent, but nothing between v1 and v3 touched
  // THIS site — so the lines are still v5's alone. No note.
  const host = await renderDiff({ versions: [
    { generation_id: 'v1', patch_id: 'p1', op: 'replace', rationale: 'first pass', content: V3_TEXT, provenance: 'snapshot' },
    { generation_id: 'v5', patch_id: 'p5', op: 'replace', rationale: 'sharper brief', content: V5_TEXT, provenance: 'snapshot' },
  ] }, { mutId: SITE, base: 'v1' });
  assert(!textOf(host).includes('change alone'), 'identical content is not a mixed diff');
});

run();

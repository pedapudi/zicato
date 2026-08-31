// test/mutations_provenance.test.mjs — the mutation surface after the snapshot
// tree is gone.
//
// A closed epoch's snapshot trees get pruned; the records do not. The server
// then reconstructs the surface and captions what it did — `provenance_note`
// on the payload. These pin the frontend half of that contract:
//   * the caption RENDERS, verbatim (a payload field nothing reads is a field
//     that stops being true without anyone noticing);
//   * the diff's left column stops claiming `v0` when the server declines to
//     name a baseline generation;
//   * the caption FOLDS INTO THE DIGEST, so a surface that flips from
//     snapshot-backed to records-backed actually repaints;
//   * the snapshot path is byte-identical to before — no caption, `v0` named.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/router.js');
const data = await import('../js/data.js');
const mutations = await import('../js/views/mutations.js');

const EPOCH = 'e1';
const CTX = { navigate() {}, href: router.href };
const CAPTION = 'snapshot pruned · reconstructed from records';

function textOf(host) {
  return String(host.textContent || '');
}
function labels(host) {
  return host.querySelectorAll('[class]')
    .filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-sxs-col-h'))
    .map((n) => n.textContent || '');
}

function index(provenanceNote) {
  return {
    epoch_id: EPOCH,
    generations: ['v0', 'v1'],
    provenance: provenanceNote ? 'records' : 'snapshot',
    provenance_note: provenanceNote || '',
    mutations: [{
      mutation_id: 'researcher_instr',
      kind: 'span',
      file: 'agent/prompts.py',
      role: provenanceNote ? '' : 'system_instruction',
      line_start: 2,
      line_end: 2,
      patched_by: [{ generation_id: 'v1', patch_id: 'p1', op: 'replace', rationale: 'sharper brief' }],
      patched_generation_ids: ['v1'],
    }],
  };
}

function detail(provenanceNote) {
  return {
    epoch_id: EPOCH,
    mutation_id: 'researcher_instr',
    file: 'agent/prompts.py',
    line_start: 2,
    line_end: 2,
    provenance_note: provenanceNote || '',
    baseline: {
      // The records path declines to name a generation; the snapshot path names v0.
      generation_id: provenanceNote ? null : 'v0',
      content: 'RESEARCHER = """original instruction"""\n',
      provenance: provenanceNote ? 'records' : 'snapshot',
    },
    versions: [{
      generation_id: 'v1',
      patch_id: 'p1',
      op: 'replace',
      rationale: 'sharper brief',
      content: 'RESEARCHER = """rewritten instruction"""\n',
      provenance: provenanceNote ? 'records' : 'snapshot',
    }],
  };
}

function install(provenanceNote) {
  globalThis.fetch = async (path) => {
    const body = (() => {
      if (path.startsWith('/api/epoch')) return { epoch_id: EPOCH };
      if (path === `/api/mutations/${EPOCH}`) return index(provenanceNote);
      if (path.startsWith(`/api/mutations/${EPOCH}/`)) return detail(provenanceNote);
      if (path.includes('/patches')) {
        return { patches: [{ id: 'p1', mutation_id: 'researcher_instr', op: 'replace', new_content: 'RESEARCHER = """rewritten instruction"""\n', rationale: 'sharper brief' }] };
      }
      return null;
    })();
    if (body == null) return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
    return { ok: true, json: async () => body };
  };
}

async function renderSurface(provenanceNote, params) {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  install(provenanceNote);
  const host = globalThis.document.createElement('div');
  await mutations.render(host, CTX, Object.assign({ epochId: EPOCH }, params || {}));
  return host;
}

test('mutations: the records caption renders, verbatim', async () => {
  const host = await renderSurface(CAPTION, {});
  assert(textOf(host).includes(CAPTION), 'the server caption is on screen');
});

test('mutations: a snapshot-backed surface says nothing about provenance', async () => {
  const host = await renderSurface('', {});
  const text = textOf(host);
  assert(!text.includes('reconstructed from records'), 'no caption when the tree answered');
  assert(text.includes('agent/prompts.py'), 'the surface still renders');
});

test('mutations: the caption folds into the digest', async () => {
  // The digest gates the repaint. A surface that flips to records-backed while
  // its sites are unchanged must still repaint — otherwise the caption would
  // only ever appear on the NEXT unrelated change.
  const withNote = await renderSurface(CAPTION, {});
  const withoutNote = await renderSurface('', {});
  assert(textOf(withNote) !== textOf(withoutNote), 'the two states render differently');

  data.invalidate();
  install('');
  const host = globalThis.document.createElement('div');
  await mutations.render(host, CTX, { epochId: EPOCH });
  assert(!textOf(host).includes(CAPTION), 'starts snapshot-backed');
  data.invalidate();
  install(CAPTION);
  await mutations.render(host, CTX, { epochId: EPOCH });
  assert(textOf(host).includes(CAPTION), 'repaints when only the provenance changed');
});

test('mutations: a version with no content prints the reason, not nothing', async () => {
  // A set_numeric record whose constant sits outside the enumerated span has
  // no faithful content to show. Dropping the row would tell the operator the
  // generation "did not patch this site", which is false and which the payload
  // contradicts.
  const reason = 'set_numeric 0.42 — the constant sits outside the recorded span';
  globalThis.fetch = async (path) => {
    const body = (() => {
      if (path.startsWith('/api/epoch')) return { epoch_id: EPOCH };
      if (path === `/api/mutations/${EPOCH}`) return index(CAPTION);
      if (path.startsWith(`/api/mutations/${EPOCH}/`)) {
        const d = detail(CAPTION);
        d.versions[0] = Object.assign({}, d.versions[0], {
          op: 'set_numeric', content: null, note: reason, rationale: 'tighten the margin',
        });
        return d;
      }
      if (path.includes('/patches')) {
        return { patches: [{ id: 'p1', mutation_id: 'researcher_instr', op: 'set_numeric', new_numeric: 0.42, new_content: null, rationale: 'tighten the margin' }] };
      }
      return null;
    })();
    if (body == null) return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
    return { ok: true, json: async () => body };
  };
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  const host = globalThis.document.createElement('div');
  await mutations.render(host, CTX, { epochId: EPOCH, mutId: 'researcher_instr' });
  const text = textOf(host);
  assert(text.includes(reason), 'the server reason is on screen');
  assert(text.includes('tighten the margin'), 'and the patch still says what it wanted');
  assert(!text.includes('did not patch this site'), 'and is not called an absent patch');
});

test('mutations: a records-sourced version is marked at its own block', async () => {
  // GC never prunes v0, so an exact baseline beside a reconstructed
  // challenger is the COMMON case — the block says which it is.
  const host = await renderSurface(CAPTION, { mutId: 'researcher_instr' });
  assert(textOf(host).includes('from records'), 'the block carries the provenance');
});

test('mutations: the diff column stops claiming v0 when the server will not', async () => {
  const pinned = { epochId: EPOCH, mutId: 'researcher_instr' };
  const fromRecords = await renderSurface(CAPTION, pinned);
  const left = labels(fromRecords)[0] || '';
  assert(left.includes('from records'), `left label names the records: ${left}`);
  assert(!left.includes('v0'), 'and does not name a generation it cannot prove');

  const fromSnapshot = await renderSurface('', pinned);
  assertEqual(labels(fromSnapshot)[0], 'champion baseline · v0', 'snapshot path names v0');
});

run();

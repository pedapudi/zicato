// test/traces.test.mjs — the Traces surface (views/traces.js + svg.trajectoryStrip).
//
// THE COMPOSITION CHECK (TRAJECTORY-UI.md §4.1, a named deliverable): every
// strip / list / detail render here loads the fixtures produced by the REAL
// readers over a seeded workspace (tools/gen_trace_view_fixtures.py →
// test/fixtures/trace_view/{list,detail,provenance}.json), NEVER hand-authored
// strip shapes — so a drift between a reader field and what the JS reads is a red
// test. Only the honest DEGRADE payloads (a reflection with zero imported traces;
// a trace with zero episodes) reshape a real payload to the reader's documented
// empty contract, and the XSS probe injects markup into a real turn's text.
//
// Covers: the strip figure known-answer (marks / signal cluster / budget /
// episode overlays incl. the unpositioned-cluster honesty), compact mode, the
// focus accent + click wiring, the list + detail renders, the episode-anchor
// builder-inbox hrefs, the digest no-op pin, every empty state, the XSS guard,
// and the #/e/<e>/traces router round-trip.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

const fs = await import('node:fs');
const { router, svg, freshState, allByClass, installFixtureMap, svgsByClass } = await import('./fixtures.mjs');
const traces = await import('../js/views/traces.js');

const CTX = { navigate() {}, href: router.href };
function fresh() { freshState(); }
function textOf(host) { return host.textContent || ''; }
function loadFixture(name) {
  return JSON.parse(fs.readFileSync(new URL('./fixtures/trace_view/' + name + '.json', import.meta.url), 'utf8'));
}
const LIST = loadFixture('list');
const DETAIL = loadFixture('detail');
const EPOCH_ID = LIST.epoch_id;          // '2020-01-01_boot'
const REFL_ID = LIST.reflection_id;      // 'refl-traceviz'
const TRACE_ID = DETAIL.trace_id;        // 'trace-a0be332d' (4 episodes, 4 signals)

// find the one trace row with data attribute equal to a value.
function byData(host, attr, val) {
  return host.querySelectorAll('[' + attr + ']').filter((n) => n.getAttribute(attr) === val);
}
// collect every element localName in a subtree (for the XSS "no <img>" probe —
// the harness querySelector has no tag-selector support).
function localNames(node, out) {
  out = out || [];
  for (const c of node.children) { out.push(c.localName); localNames(c, out); }
  return out;
}

// A base fixture map carrying the epoch + reflections list + the trace endpoints.
function traceMap(over) {
  const o = over || {};
  return {
    '/api/epoch': { epoch_id: EPOCH_ID, closed: false, goal: 'boot' },
    '/api/reflections': o.reflections || { reflections: [
      { reflection_id: REFL_ID, epoch_id: EPOCH_ID, created_at: '2020-01-01T00:00:00Z', mode: 'mint', executed: true },
    ] },
    [`/api/reflection/${REFL_ID}/traces`]: o.list || LIST,
    [`/api/reflection/${REFL_ID}/trace/${TRACE_ID}`]: o.detail || DETAIL,
  };
}

// ====================================================================
// ROUTER round-trip.
// ====================================================================
test('router: traces is a registered VIEW; parse/href/up round-trip', () => {
  assert(router.VIEWS.includes('traces'), 'traces in VIEWS');
  const landing = router.href('traces', { epochId: EPOCH_ID });
  assertEqual(landing, `#/e/${EPOCH_ID}/traces`, 'landing href');
  const list = router.href('traces', { epochId: EPOCH_ID, reflectionId: REFL_ID });
  assertEqual(list, `#/e/${EPOCH_ID}/traces/${REFL_ID}`, 'list href');
  const detail = router.href('traces', { epochId: EPOCH_ID, reflectionId: REFL_ID, traceId: TRACE_ID });
  assertEqual(router.parseRoute(detail).view, 'traces', 'detail parses to traces');
  const p = router.parseRoute(detail).params;
  assertEqual(p.traceId, TRACE_ID, 'traceId parsed');
  // up(): detail → list → landing → epoch.
  assertEqual(router.up({ view: 'traces', params: { epochId: EPOCH_ID, reflectionId: REFL_ID, traceId: TRACE_ID } }).params.reflectionId, REFL_ID, 'detail steps up to list');
  assert(router.up({ view: 'traces', params: { epochId: EPOCH_ID, reflectionId: REFL_ID } }).params.reflectionId == null, 'list steps up to landing');
  assertEqual(router.up({ view: 'traces', params: { epochId: EPOCH_ID } }).view, 'epoch', 'landing steps up to epoch');
});

// ====================================================================
// THE STRIP FIGURE — known-answer over the REAL strip-model.
// ====================================================================
test('strip: draws marks / signal cluster / budget / episode overlays from the real model', () => {
  const model = LIST.traces[0].strip_model;       // trace-a0be332d
  const s = svg.trajectoryStrip(model);
  assertEqual(s.localName, 'svg', 'returns an <svg>');
  // turn lane: 2 marks (1 user + 1 agent), toned neutral (never a verdict).
  const marks = allByClass(s, 'dn-strip-mark');
  assertEqual(marks.length, 2, 'one mark per turn');
  assertEqual(allByClass(s, 'dn-strip-mark-user').length, 1, 'a user mark');
  assertEqual(allByClass(s, 'dn-strip-mark-agent').length, 1, 'an agent mark');
  // budget ground: shaded (fill 0.02), not over.
  const budget = allByClass(s, 'dn-strip-budget');
  assertEqual(budget.length, 1, 'a budget fill rect');
  assertEqual(budget[0].getAttribute('data-over'), '0', 'not over a ceiling');
  assert(parseFloat(budget[0].getAttribute('data-fill')) > 0, 'a positive budget fill');
  // episode overlay: 4 brackets, each carrying its linked suggestion id.
  const eps = allByClass(s, 'dn-strip-ep');
  assertEqual(eps.length, 4, 'one overlay per episode');
  assert(eps.every((e) => e.getAttribute('data-suggestion-ids')), 'each overlay wires its suggestion_ids');
});

test('strip HONESTY: signals render as an unpositioned labelled cluster (§1.1)', () => {
  const model = LIST.traces[0].strip_model;
  const s = svg.trajectoryStrip(model);
  const sigs = allByClass(s, 'dn-strip-signal');
  assertEqual(sigs.length, model.signals.length, 'one tick per adverse signal');
  // the load-bearing rule: NO tick claims a real timeline position.
  assert(sigs.every((g) => g.getAttribute('data-positioned') === 'false'), 'every signal tick is data-positioned=false');
  const cluster = allByClass(s, 'dn-strip-signals');
  assertEqual(cluster.length, 1, 'a dedicated signal-cluster lane');
  assertEqual(cluster[0].getAttribute('data-positioned'), 'false', 'the cluster itself is unpositioned');
  // the count rides the tick (the honesty label), not a fabricated position.
  assert(sigs.some((g) => g.getAttribute('data-count')), 'ticks carry their aggregate count');
});

test('strip: compact mode reduces height + carries the compact class', () => {
  const model = LIST.traces[0].strip_model;
  const full = svg.trajectoryStrip(model, {});
  const compact = svg.trajectoryStrip(model, { compact: true });
  assert(!full.getAttribute('class').includes('dn-strip-compact'), 'full is not compact');
  assert(compact.getAttribute('class').includes('dn-strip-compact'), 'compact carries the class');
  const h = (n) => parseFloat(n.getAttribute('viewBox').split(/\s+/)[3]);
  assert(h(compact) < h(full), 'compact viewBox is shorter');
});

test('strip: focusEpisodeId rides the accent (the ONE structural highlight)', () => {
  const model = LIST.traces[0].strip_model;
  const epId = model.episodes[0].episode_id;
  const s = svg.trajectoryStrip(model, { focusEpisodeId: epId });
  const focused = byData(s, 'data-episode-id', epId)[0];
  assert(focused && focused.getAttribute('class').includes('dn-strip-ep-focus'), 'the focused episode carries the focus class');
  assert(focused.getAttribute('class').includes('dn-strip-t-accent'), 'and rides --v2-accent');
});

test('strip: clicking an episode fires onFocusEpisode(id, suggestionIds)', () => {
  const model = LIST.traces[0].strip_model;
  const ep = model.episodes[0];
  let got = null;
  const s = svg.trajectoryStrip(model, { onFocusEpisode: (id, sugs) => { got = { id, sugs }; } });
  const g = byData(s, 'data-episode-id', ep.episode_id)[0];
  g.dispatchEvent(makeEvent('click'));
  assert(got && got.id === ep.episode_id, 'handler got the episode id');
  assert(Array.isArray(got.sugs) && got.sugs.join(',') === ep.suggestion_ids.join(','), 'handler got the suggestion ids');
});

test('strip: an empty model degrades to a cold state, never a throw', () => {
  const s = svg.trajectoryStrip({});
  assertEqual(s.localName, 'svg', 'still an <svg>');
  assertEqual(allByClass(s, 'dn-strip-mark').length, 0, 'no marks on an empty model');
});

// ====================================================================
// THE LIST view.
// ====================================================================
test('list: one strip per imported trace + source/dialect/counts captions', async () => {
  fresh();
  installFixtureMap(traceMap());
  const host = document.createElement('div');
  await traces.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFL_ID });
  const rows = allByClass(host, 'dn-trace-row');
  assertEqual(rows.length, LIST.trace_count, 'a row per imported trace');
  assertEqual(svgsByClass(host, 'dn-strip').length, LIST.trace_count, 'a strip per row');
  // captions (never chips): the dialect + counts read as faint metadata.
  assert(textOf(host).includes('adk_events'), 'the dialect renders as caption text');
  assert(textOf(host).includes('4 episodes'), 'the episode count renders');
  // the compact list strips.
  assert(svgsByClass(host, 'dn-strip').every((s) => s.getAttribute('class').includes('dn-strip-compact')), 'list strips are compact');
});

// ====================================================================
// THE DETAIL view.
// ====================================================================
test('detail: full strip + reconstructed conversation + episode anchors', async () => {
  fresh();
  installFixtureMap(traceMap());
  const host = document.createElement('div');
  await traces.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFL_ID, traceId: TRACE_ID });
  // the hero strip (not compact).
  const strips = svgsByClass(host, 'dn-strip');
  assert(strips.length >= 1 && !strips[0].getAttribute('class').includes('dn-strip-compact'), 'a hero strip');
  // the reconstructed conversation reuses the transcript turn vocabulary.
  const turns = allByClass(host, 'dn-turn');
  assertEqual(turns.length, DETAIL.turns.length, 'one turn per reconstructed turn');
  assert(textOf(host).includes('Book me a flight to Lisbon'), 'a user turn renders');
  // the honest reconstruction note.
  assert(textOf(host).includes('zipped by index'), 'the reconstruction note is shown');
  // the episode anchors.
  assertEqual(allByClass(host, 'dn-trace-ep').length, DETAIL.episodes.length, 'an anchor per episode');
});

test('detail: episode anchors link their suggestions to the builder inbox', async () => {
  fresh();
  installFixtureMap(traceMap());
  const host = document.createElement('div');
  await traces.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFL_ID, traceId: TRACE_ID });
  const sugLinks = allByClass(host, 'dn-trace-ep-sug');
  assert(sugLinks.length >= 1, 'at least one suggestion link');
  assertEqual(sugLinks[0].getAttribute('href'), router.href('builder', {}), 'links into the builder inbox');
  assert(sugLinks.some((a) => a.getAttribute('text') === 'sug-14ffa7e6' || textOf(a) === 'sug-14ffa7e6'), 'names the drafted suggestion');
});

// ====================================================================
// DIGEST NO-OP — two identical renders ⇒ zero DOM rebuild.
// ====================================================================
test('digest no-op: a second identical render rebuilds ZERO DOM', async () => {
  fresh();
  installFixtureMap(traceMap());
  const host = document.createElement('div');
  await traces.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFL_ID });
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  await traces.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFL_ID });
  assert(host.firstChild === first, 'no clear-and-rebuild on the identical repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

// ====================================================================
// EMPTY / COLD states — honest, never an error.
// ====================================================================
test('empty: a landing with no reflections shows an honest empty state', async () => {
  fresh();
  installFixtureMap(traceMap({ reflections: { reflections: [] } }));
  const host = document.createElement('div');
  await traces.render(host, CTX, { epochId: EPOCH_ID });
  assert(textOf(host).toLowerCase().includes('no reflections'), 'says no reflections');
  assertEqual(allByClass(host, 'dn-trace-row').length, 0, 'no trace rows');
});

test('empty: a reflection with no imported traces degrades honestly', async () => {
  fresh();
  installFixtureMap(traceMap({ list: { reflection_id: REFL_ID, epoch_id: EPOCH_ID, found: true, trace_count: 0, traces: [] } }));
  const host = document.createElement('div');
  await traces.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFL_ID });
  assert(textOf(host).toLowerCase().includes('no imported traces'), 'says no imported traces');
});

test('empty: a trace with no mined episodes shows the honest note', async () => {
  fresh();
  // the reader's honest 0-episode shape: the REAL detail with its episodes
  // emptied (strip lane/signals/budget stay real; the episode list is empty).
  const noEp = { ...DETAIL, episodes: [], strip_model: { ...DETAIL.strip_model, episodes: [] } };
  installFixtureMap(traceMap({ detail: noEp }));
  const host = document.createElement('div');
  await traces.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFL_ID, traceId: TRACE_ID });
  assert(textOf(host).toLowerCase().includes('no mined episodes'), 'says no mined episodes');
  // the strip + conversation still render.
  assert(svgsByClass(host, 'dn-strip').length >= 1, 'the strip still renders');
  assert(allByClass(host, 'dn-turn').length === DETAIL.turns.length, 'the conversation still renders');
});

test('empty: an unknown trace shows the honest not-found state', async () => {
  fresh();
  installFixtureMap(traceMap({ detail: { reflection_id: REFL_ID, epoch_id: EPOCH_ID, found: false, trace_id: TRACE_ID, source_file: '', dialect: '', line_count: 0, malformed_line_count: 0, signal_counts: {}, strip_model: {}, turns: [], reconstruction_note: '', episodes: [] } }));
  const host = document.createElement('div');
  await traces.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFL_ID, traceId: TRACE_ID });
  assert(textOf(host).toLowerCase().includes('no such trace'), 'says no such trace');
});

// ====================================================================
// XSS — trace text into the DOM goes through textContent (never markup).
// ====================================================================
test('xss: a markup-bearing turn renders as inert text, not DOM', async () => {
  fresh();
  const evil = '<img src=x onerror="alert(1)">';
  const poisoned = {
    ...DETAIL,
    turns: [
      { index: 0, role: 'user', text: evil, chars: evil.length, truncated: false },
      { index: 1, role: 'agent', text: 'ok', chars: 2, truncated: false },
    ],
  };
  installFixtureMap(traceMap({ detail: poisoned }));
  const host = document.createElement('div');
  await traces.render(host, CTX, { epochId: EPOCH_ID, reflectionId: REFL_ID, traceId: TRACE_ID });
  // the markup survives as literal text (proving textContent), and NO <img>
  // element was ever created from it.
  assert(textOf(host).includes(evil), 'the markup is present as inert text');
  assert(!localNames(host).includes('img'), 'no <img> element was injected');
});

run();

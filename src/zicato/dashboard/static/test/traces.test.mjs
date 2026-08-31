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

import { Worker } from 'node:worker_threads';
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
  // budget ground: shaded (fill 0.02) and under the ceiling.
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
  // the count rides the tick (the honesty label) rather than a fabricated position.
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

// ====================================================================
// THE LANE GEOMETRY — the "black blob" regression pins.
//
// The shipped strip drew each turn mark as a FULL-LANE-HEIGHT rectangle filled
// with a raw foreground ink token, so a 2-turn trace (the real fixture below)
// rendered as two half-lane slabs that fused into one solid block — near-black
// in a light theme. These assert the geometry that makes that impossible in ANY
// theme: bounded bars straddling a mid-lane baseline, a real gap between
// neighbours, a capped horizontal extent, and a soft area fill.
// ====================================================================

// Read the lane frame back OUT of the rendered figure (never re-derived here):
// the budget ground rect IS the lane box, the baseline line IS the mid-lane
// baseline the marks straddle.
function laneFrame(s) {
  const ground = allByClass(s, 'dn-strip-ground')[0];
  const base = allByClass(s, 'dn-strip-baseline')[0];
  return {
    top: parseFloat(ground.getAttribute('y')),
    height: parseFloat(ground.getAttribute('height')),
    baseY: parseFloat(base.getAttribute('y1')),
  };
}
function markBoxes(s) {
  return allByClass(s, 'dn-strip-mark').map((r) => ({
    role: r.getAttribute('data-role'),
    x: parseFloat(r.getAttribute('x')), w: parseFloat(r.getAttribute('width')),
    y: parseFloat(r.getAttribute('y')), h: parseFloat(r.getAttribute('height')),
    cls: (r.getAttribute('class') || '').split(/\s+/),
  })).sort((a, b) => a.x - b.x);
}
// The FULL lane geometry invariants, asserted for both sizes.
function assertLaneGeometry(s, label) {
  const lane = laneFrame(s);
  const boxes = markBoxes(s);
  assert(boxes.length > 0, `${label}: the lane draws marks`);
  const capH = 0.40 * lane.height;
  for (const b of boxes) {
    // (1) BOUNDED HEIGHT — no mark may fill the lane.
    assert(b.h <= capH + 0.01, `${label}: mark height ${b.h} within the ≤40% lane cap ${capH}`);
    assert(b.h > 0, `${label}: every mark still has a visible height`);
    // (2) THE ALTERNATION — user rides ABOVE the baseline, agent BELOW.
    if (b.role === 'user') assert(b.y + b.h <= lane.baseY + 0.01, `${label}: a user mark sits above the baseline`);
    else assert(b.y >= lane.baseY - 0.01, `${label}: an agent mark sits below the baseline`);
    // (3) inside the lane box.
    assert(b.y >= lane.top - 0.01 && b.y + b.h <= lane.top + lane.height + 0.01, `${label}: the mark stays inside the lane`);
    // (4) SOFT FILL — never the raw-ink class the blob was painted with.
    assert(!b.cls.includes('dn-strip-mark-ink'), `${label}: no raw-ink mark class`);
    assert(b.cls.includes('dn-strip-mark'), `${label}: every mark carries the shared soft-area class`);
  }
  // (5) A REAL GAP between neighbours — adjacent marks never touch or overlap.
  for (let i = 1; i < boxes.length; i++) {
    const gap = boxes[i].x - (boxes[i - 1].x + boxes[i - 1].w);
    assert(gap >= 0.9, `${label}: ≥1px gap between adjacent marks (got ${gap.toFixed(2)})`);
  }
}

test('lane geometry: bounded bars straddling the baseline, gapped, soft-filled (both sizes)', () => {
  const model = LIST.traces[0].strip_model;       // the REAL 2-turn adk trace
  assertLaneGeometry(svg.trajectoryStrip(model, {}), 'hero');
  assertLaneGeometry(svg.trajectoryStrip(model, { compact: true }), 'compact');
  // the multi-turn real trace too (4 marks — the richest lane in the fixtures).
  const multi = LIST.traces.find((t) => t.strip_model.lane.marks.length > 2);
  assert(multi, 'a multi-turn trace exists in the real fixtures');
  assertLaneGeometry(svg.trajectoryStrip(multi.strip_model, {}), 'multi-turn hero');
});

test('lane geometry: THE BLOB PIN — the inked marks cover a small fraction of the lane', () => {
  // The quantitative statement of "not a blob": pre-fix the two full-height
  // half-lane slabs inked ~85% of the lane box. A bounded, gapped, capped lane
  // cannot exceed a fraction of it — whatever the theme paints them.
  //
  // The 30 % threshold is what the SHORT committed traces reach; the geometry's
  // own hard ceiling is LANE_BAR_FRAC (40 %), which a saturated lane of
  // uniformly-long turns legitimately approaches. A recapture that adds a long
  // trace should compare against that ceiling rather than tighten this number.
  const inkFraction = (model) => {
    const s = svg.trajectoryStrip(model, {});
    const lane = laneFrame(s);
    const inked = markBoxes(s).reduce((sum, b) => sum + b.w * b.h, 0);
    return inked / (lane.height * parseFloat(allByClass(s, 'dn-strip-ground')[0].getAttribute('width')));
  };
  for (const t of LIST.traces) {
    const f = inkFraction(t.strip_model);
    assert(f < 0.30, `${t.trace_id}: marks ink ${(100 * f).toFixed(1)}% of the lane (<30%)`);
  }
  // THE DEGENERATE LANE — a text-free trace (a dialect reader that extracted no
  // turn bodies) SATURATES the lane, so if the reader claimed a height for it the
  // figure would tile the lane with maximum bars: the densest field it can paint,
  // for the least informative input. The reader serves `size` 0 (trace_view), so
  // this collapses to hairlines.
  const textless = {
    trace_id: 'textless',
    lane: { turn_count: 4, marks: [0, 1, 2, 3].map((i) => ({
      i, role: i % 2 ? 'agent' : 'user', x0: i / 4, x1: (i + 1) / 4, size: 0, chars: 0,
    })) },
    signals: [], budget: {}, episodes: [],
  };
  const tf = inkFraction(textless);
  assert(tf < 0.05, `a text-free saturated lane stays hairline-thin (inked ${(100 * tf).toFixed(1)}%)`);
});

test('lane geometry: the server CAPS each extent — no single turn walls the lane', () => {
  // The extent cap is the reader's (trace_view.LANE_EXTENT_CAP = 0.25); the JS
  // only draws it. Assert the served fixtures honour it and that a 2-turn lane
  // is UNDER-filled (the lane is a capacity rather than a pie).
  for (const t of LIST.traces) {
    const marks = t.strip_model.lane.marks;
    for (const m of marks) assert(m.x1 - m.x0 <= 0.2501, `${t.trace_id}: extent ${(m.x1 - m.x0).toFixed(4)} within the 0.25 cap`);
    if (marks.length <= 4) assert(marks[marks.length - 1].x1 < 1.0, `${t.trace_id}: a short lane stays under-filled`);
    // laid end-to-end, monotone, non-overlapping.
    for (let i = 1; i < marks.length; i++) assert(marks[i].x0 >= marks[i - 1].x1 - 1e-9, `${t.trace_id}: marks are monotone`);
  }
});

test('lane geometry: a DENSE 500-turn lane still resolves (every mark visible, none overlapping)', () => {
  const dense = {
    trace_id: 'stress',
    lane: { turn_count: 500, marks: Array.from({ length: 500 }, (_, i) => ({
      i, role: i % 2 ? 'agent' : 'user', x0: i / 500, x1: (i + 1) / 500, size: (i % 17) / 16, chars: i,
    })) },
    signals: [], budget: { shaded: true, fill: 0.5, over: false, label: 'stress' }, episodes: [],
  };
  const s = svg.trajectoryStrip(dense, {});
  const boxes = markBoxes(s);
  assertEqual(boxes.length, 500, 'every dense mark is drawn');
  for (const b of boxes) assert(b.w >= 0.5 && b.h > 0, 'a dense mark still has extent');
  for (let i = 1; i < boxes.length; i++) {
    assert(boxes[i].x >= boxes[i - 1].x + boxes[i - 1].w - 1e-6, 'dense marks never overlap');
  }
});

// ====================================================================
// THE STYLE CONTRACT — read straight out of console.css (the theme tokens
// differ per theme, so the RULE is what must hold, in both).
// ====================================================================
test('style: turn marks take the SOFT area treatment — never a raw --v2-ink large-area fill', () => {
  const css = fs.readFileSync(new URL('../css/console.css', import.meta.url), 'utf8');
  const rule = (sel) => {
    const m = new RegExp('\\' + sel.replace(/^\./, '.') + '\\s*\\{([^}]*)\\}').exec(css);
    return m ? m[1] : null;
  };
  const user = rule('.dn-strip-mark-user');
  const agent = rule('.dn-strip-mark-agent');
  assert(user && agent, 'console.css defines both turn-mark classes');
  // the load-bearing rule: NOT the raw foreground ink token (the black blob).
  assert(!/fill:\s*var\(--v2-ink\)/.test(user), 'the user mark does NOT fill with raw --v2-ink');
  assert(!/fill:\s*var\(--v2-ink\)/.test(agent), 'the agent mark does NOT fill with raw --v2-ink');
  // it IS the soft neutral token at a reduced fill-opacity (the house area
  // treatment: .dn-spark-band / .dn-strip-budget speak the same way).
  for (const [name, body] of [['user', user], ['agent', agent]]) {
    assert(/fill:\s*var\(--v2-ink-soft\)/.test(body), `the ${name} mark fills with the SOFT neutral token`);
    const op = /fill-opacity:\s*([0-9.]+)/.exec(body);
    assert(op && parseFloat(op[1]) < 0.7, `the ${name} mark fill is translucent (got ${op && op[1]})`);
  }
  // and the two sides stay distinguishable (a density step rather than a hue).
  const uo = parseFloat(/fill-opacity:\s*([0-9.]+)/.exec(user)[1]);
  const ao = parseFloat(/fill-opacity:\s*([0-9.]+)/.exec(agent)[1]);
  assert(Math.abs(uo - ao) >= 0.1, 'user vs agent marks differ by a readable density step');
});

test('style: svg.dn-strip-hero caps max-WIDTH at the viewBox width — the figure never balloons', () => {
  const css = fs.readFileSync(new URL('../css/console.css', import.meta.url), 'utf8');
  const m = /svg\.dn-strip-hero\s*\{([^}]*)\}/.exec(css);
  assert(m, 'console.css caps the strip hero with the load-bearing `svg.` qualifier');
  const body = m[1];
  assert(/width:\s*100%/.test(body) && /height:\s*auto/.test(body), 'fit-to-width, height follows the aspect');
  const cap = /max-width:\s*(\d+)px/.exec(body);
  assert(cap, 'a real max-WIDTH cap in px (max-width:100% is not a cap)');
  // the cap == the figure's viewBox width, so it renders 1:1 at its designed
  // height with unscaled mono text.
  const vb = svg.trajectoryStrip(LIST.traces[0].strip_model, {}).getAttribute('viewBox').split(/\s+/);
  assertEqual(parseInt(cap[1], 10), parseInt(vb[2], 10), 'the cap matches the viewBox width');
  assert(!/max-height/.test(body), 'no max-height (it would shear the aspect-locked scale)');
});

// ====================================================================
// TERMINATION — the render path provably completes.
//
// A non-terminating render (an unbounded loop in the strip figure, the detail
// builders, or the episode-anchor focus wiring over an UNPOSITIONED episode)
// hangs node exactly as it hangs a browser, so the pin runs the real payloads
// through the real builders in an isolated worker under a hard wall-clock timeout:
// a spin fails BY TIMEOUT here instead of hanging the suite forever.
//
// No such loop exists in this tree; the unresponsive-page report that prompted
// the search traced to the capture tooling rather than to the product. This pin
// is the standing guard that keeps it that way.
// ====================================================================
test('termination: the real list + detail + dense-lane renders complete under a hard timeout', async () => {
  const budgetMs = 20_000;                       // ~60× the observed ~0.3 s run
  const t0 = Date.now();
  const worker = new Worker(new URL('./_trace_render_probe.mjs', import.meta.url));
  const status = await new Promise((resolve) => {
    const timeout = setTimeout(() => resolve('timeout'), budgetMs);
    worker.once('error', (error) => { clearTimeout(timeout); resolve(error); });
    worker.once('exit', (code) => { clearTimeout(timeout); resolve(code); });
  });
  if (status === 'timeout') await worker.terminate();
  const ms = Date.now() - t0;
  assert(status !== 'timeout', `the render probe did not terminate within ${budgetMs} ms (a spin in the Traces render path)`);
  assertEqual(status, 0, `the render probe exited clean (${status instanceof Error ? status.message : status})`);
  assert(ms < budgetMs, `completed in ${ms} ms`);
});

run();

// test/icons.test.mjs — the console's one drawn icon set (js/icons.js).
//
// Pins:
//   * every icon is built the same way: an <svg> on the 16-unit grid, a
//     1.5-unit round-capped stroke in currentColor, hidden from assistive
//     technology, named by `data-icon`;
//   * patchIconLabel writes nothing when neither the icon nor the words changed
//     (the no-op-heartbeat rule for chrome patched in place);
//   * no chrome symbol typed as a Unicode character survives in the console's
//     modules — the bundled faces lack them, so the browser substitutes a
//     system font or a colour emoji. Only the typographic and mathematical
//     characters that sit inside words and values stay (ALLOWED below);
//   * each control, mark and verdict that once carried a typed symbol draws its
//     icon instead.

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative } from 'node:path';
import { installDom, test, run, assert, assertEqual, assertDeep, makeEvent, iconNames } from './harness.mjs';

installDom();

const icons = await import('../js/icons.js');
const shell = await import('../js/shell.js');
const tree = await import('../js/tree.js');
const live = await import('../js/live.js');
const turns = await import('../js/turns.js');
const ui = await import('../js/ui.js');
const dag = await import('../js/dag.js');
const router = await import('../js/router.js');
const { buildSwatchDropdown } = await import('../js/swatchdropdown.js');
const { buildTypefaceDropdown } = await import('../js/typefacedropdown.js');

const STATIC = join(dirname(fileURLToPath(import.meta.url)), '..');

function classOf(node) { return (node && node.getAttribute && node.getAttribute('class')) || ''; }
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => classOf(n).split(/\s+/).includes(cls));
}
function mountInto(node) { const h = document.createElement('div'); if (node) h.appendChild(node); return h; }

// ── 1. one construction for every icon ─────────────────────────────────────
test('icon: every icon is a 16-unit, 1.5-stroke, currentColor svg hidden from assistive technology', () => {
  assert(icons.ICON_NAMES.length >= 40, 'the set covers the console chrome');
  for (const name of icons.ICON_NAMES) {
    const node = icons.icon(name);
    assertEqual(node.localName, 'svg', name + ' is an svg');
    assertEqual(node.getAttribute('data-icon'), name, name + ' names itself');
    assertEqual(node.getAttribute('viewBox'), '0 0 16 16', name + ' sits on the 16-unit grid');
    assertEqual(node.getAttribute('stroke-width'), '1.5', name + ' has the one stroke weight');
    assertEqual(node.getAttribute('stroke'), 'currentColor', name + ' strokes in the text colour');
    assertEqual(node.getAttribute('stroke-linecap'), 'round', name + ' has round caps (the brand mark line)');
    assertEqual(node.getAttribute('fill'), 'none', name + ' is open unless a part fills');
    assertEqual(node.getAttribute('aria-hidden'), 'true', name + ' is hidden from assistive technology');
    assert(node.children.length >= 1, name + ' draws something');
    for (const part of node.children) {
      const fill = part.getAttribute('fill');
      assert(fill == null || fill === 'currentColor', name + ': a filled part fills with the text colour');
    }
  }
  let threw = false;
  try { icons.icon('no-such-icon'); } catch (e) { threw = true; }
  assert(threw, 'an unknown icon name is an error, never a blank mark');
});

test('icon: a figure icon carries its own box; iconLabel orders the icon and the words', () => {
  const placed = icons.icon('crown', { x: 3, y: 4, size: 11 });
  assertDeep(['x', 'y', 'width', 'height'].map((k) => placed.getAttribute(k)), ['3', '4', '11', '11'], 'x/y/size place it in a figure');
  const lead = mountInto(null);
  for (const k of icons.iconLabel('pause', 'pause')) lead.appendChild(typeof k === 'string' ? document.createTextNode(k) : k);
  assertEqual(lead.children[0].getAttribute('data-icon'), 'pause', 'the icon leads by default');
  assertEqual(lead.textContent, ' pause', 'the words follow one space after it');
  const trail = icons.iconLabel('external', 'execution', { after: true });
  assertEqual(trail[0], 'execution', 'with after:true the words come first');
  assertEqual(trail[2].getAttribute('data-icon'), 'external', 'and the icon last');
  assertDeep(icons.CROWN, { current: 'crown', former: 'crown-former' }, 'the champion crowns are icon names');
});

// ── 2. the no-op rule for chrome patched in place ─────────────────────────
test('patchIconLabel: an unchanged icon and words write nothing; a change rebuilds', () => {
  const node = document.createElement('button');
  icons.patchIconLabel(node, 'skip', 'skip round');
  const first = node.firstChild;
  assertDeep(iconNames(node), ['skip'], 'the icon is drawn');
  icons.patchIconLabel(node, 'skip', 'skip round');
  assert(node.firstChild === first, 'a repeat with the same icon and words keeps the same DOM');
  icons.patchIconLabel(node, null, 'confirm skip?');
  assertDeep(iconNames(node), [], 'words alone drop the icon');
  assertEqual(node.textContent, 'confirm skip?', 'and show the words');
  icons.patchIconLabel(node, null, '');
  assertEqual(node.childNodes.length, 0, 'empty words and no icon clear the node');
});

// ── 3. no typed chrome symbol survives in the console modules ─────────────
//
// Kept characters, each text inside words or values: the separator dot, the
// dashes, the ellipsis that marks truncation, typographic quotes, arrows that
// read as words inside prose and axis captions ("cause → effect", "rounds →",
// "scalar ↓", a prediction's direction), the multiplication, plus-minus, minus
// and comparison signs of values, the fraction and division signs, the
// proportional and square-root signs of a formula, the box-drawing bar that
// names a reign in a legend, the combining circumflex of θ̂, and the zero-width
// space that lets a long id break. Letters (Greek symbols, the brand's dotless
// ı) are always allowed.
const ALLOWED = new Set([...'·—–…’“”→←↑↓×±−≥≤≈∝√½÷│', '̂', '​']);
// The symbols the console once typed as chrome. Each must stay out of the
// allowed set, so the scan below catches its return.
const CHROME = [...'⏸⏭▶✕✓✗⟳⟲↻⋯♛♔♚◆◇◌↳▦▤⌾⌇⌗¶▸▾◂›⏱⚙☰□●○▪✂＋◷✦◑⇄⤒⤓▲▼↗∅•', '💬'];

// The string, template and regular-expression text of a module, with every
// comment removed: a comment may name a symbol; the rendered text may not.
function codeText(src) {
  let out = '';
  let i = 0;
  let prev = '';
  while (i < src.length) {
    const c = src[i];
    if (src.startsWith('//', i)) { const j = src.indexOf('\n', i); i = j < 0 ? src.length : j; continue; }
    if (src.startsWith('/*', i)) { const j = src.indexOf('*/', i + 2); i = j < 0 ? src.length : j + 2; continue; }
    if (c === '"' || c === "'" || c === '`' || (c === '/' && prev && '(,=:[!&|?{};+-*%<>~^'.includes(prev))) {
      const close = c;
      let j = i + 1;
      while (j < src.length && src[j] !== close) {
        if (src[j] === '\\') j += 1;
        else if (close === '/' && src[j] === '\n') break;
        j += 1;
      }
      out += src.slice(i, j + 1) + '\n';
      i = j + 1;
      prev = 'x';
      continue;
    }
    if (!/\s/.test(c)) prev = c;
    i += 1;
  }
  return out;
}

function moduleFiles() {
  const out = [join(STATIC, 'console.js')];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name);
      if (statSync(p).isDirectory()) walk(p);
      else if (name.endsWith('.js')) out.push(p);
    }
  };
  walk(join(STATIC, 'js'));
  return out;
}

test('no typed chrome symbol: every non-letter, non-ASCII character in the modules is text in words or values', () => {
  for (const ch of CHROME) assert(!ALLOWED.has(ch), `the chrome symbol ${ch} is not in the allowed set`);
  const found = [];
  for (const file of moduleFiles()) {
    const code = codeText(readFileSync(file, 'utf8'));
    for (const ch of new Set(code)) {
      if (ch.charCodeAt(0) < 128 || /\p{L}/u.test(ch) || ALLOWED.has(ch)) continue;
      found.push(`${relative(STATIC, file)}: ${ch} U+${ch.codePointAt(0).toString(16).toUpperCase()}`);
    }
  }
  assertDeep(found, [], 'draw these with js/icons.js instead');
});

test('no typed chrome symbol: the stylesheet generates no symbol through `content`', () => {
  const css = readFileSync(join(STATIC, 'css', 'console.css'), 'utf8');
  const generated = [...css.matchAll(/content:\s*(['"])(.*?)\1/g)].map((m) => m[2]).filter((v) => /[^\x00-\x7F]/.test(v));
  assertDeep(generated, [], 'a generated symbol is a typed glyph too');
});

// ── 4. each control and mark draws its icon ───────────────────────────────
test('loop controls: pause, resume and skip round draw their icons; the armed skip reads words alone', () => {
  const unpaused = mountInto(shell.buildLoopControls({ paused: false, onSkip() {} }));
  assertDeep(iconNames(allByClass(unpaused, 'dt-loopctl-pause')[0]), ['pause'], 'the pause face draws pause');
  const skip = allByClass(unpaused, 'dt-loopctl-skip')[0];
  assertDeep(iconNames(skip), ['skip'], 'skip round draws skip');
  skip.dispatchEvent(makeEvent('click'));
  assertDeep(iconNames(skip), [], 'armed, the confirm prompt is words alone');
  skip.dispatchEvent(makeEvent('click'));
  assertDeep(iconNames(skip), ['skip'], 'fired, the skip icon returns');
  assertEqual(skip.textContent.trim(), 'skip round', 'with its words');
  const paused = mountInto(shell.buildLoopControls({ paused: true }));
  assertDeep(iconNames(allByClass(paused, 'dt-loopctl-resume')[0]), ['resume'], 'the resume face draws resume');
});

test('top bar: up, log, settings, the breadcrumb separator and the settings close button draw their icons', async () => {
  const { freshState, mountLiveShell, installFetch } = await import('./fixtures.mjs');
  freshState(); installFetch();
  const root = mountLiveShell('#/e/2026-05-30_e0/gen/v1');
  await new Promise((r) => setTimeout(r, 0));
  assertDeep(iconNames(allByClass(root, 'dt-back')[0]), ['up'], 'the up control');
  assertDeep(iconNames(allByClass(root, 'dt-nav-logs')[0]), ['log'], 'the operator log entry');
  assertDeep(iconNames(allByClass(root, 'dt-nav-build')[0]), ['settings'], 'the settings entry');
  const close = allByClass(root, 'dt-drawer-x')[0];
  assertDeep(iconNames(close), ['close'], 'the settings drawer close button');
  assertEqual(close.getAttribute('aria-label'), 'Close settings', 'which names itself in words');
  const seps = allByClass(root, 'dt-crumb-sep');
  assert(seps.length >= 1 && seps.every((s) => iconNames(s)[0] === 'separator'), 'breadcrumb steps are joined by the drawn separator');
});

test('navigation tree: generation, child, crowns, unscored, disclosure and section marks are drawn', () => {
  const EP = '2026-09-27_e0';
  const host = document.createElement('div');
  tree.buildTree(host, {
    epochs: [{ id: EP, current: true }],
    byEpoch: { [EP]: { gens: [
      { id: 'v0', promoted: true, parent: null, formerChampion: true },
      { id: 'v1', promoted: true, parent: 'v0', currentChampion: true },
      { id: 'v2', promoted: false, parent: 'v1' },
      { id: 'v3', promoted: null, parent: 'v1', orphan: true },
    ], boards: [{ id: 'task_a' }], hasReflections: true } },
  }, { view: 'gens', params: { epochId: EP } }, new Set(['e:' + EP, 'e:' + EP + '/gens', 'e:' + EP + '/boards']),
  { navigate() {}, href: router.href }, () => {});
  const glyphs = allByClass(host, 'dt-glyph').map((g) => iconNames(g)[0]);
  for (const name of ['crown', 'crown-former', 'child', 'unscored', 'board', 'evals', 'instrument', 'traces', 'mutations', 'publication']) {
    assert(glyphs.includes(name), `the tree draws the ${name} mark (got ${glyphs.join(',')})`);
  }
  const twisties = allByClass(host, 'dt-twisty').map((t) => iconNames(t)[0]).filter(Boolean);
  assert(twisties.includes('collapse'), 'an open branch draws the collapse chevron');
});

test('live feed: ticker rows lead with the mark of their kind; follow, outcomes and the pipeline arrow are drawn', () => {
  const t = new live.ActivityTicker({ cap: 10 });
  t.push([
    { id: 'a', kind: 'cut', text: 'rung cut · v3 eliminated', tone: 'bad' },
    { id: 'b', kind: 'survive', text: 'rung · v1 survive', tone: 'good' },
    { id: 'c', kind: 'gate', text: 'champion-gate · v1 promoted', tone: 'good' },
    { id: 'd', kind: 'run', text: 'run done' },
    { id: 'e', kind: 'matchup', text: 'matchup running · v2' },
    { id: 'f', kind: 'phase', text: 'phase · racing' },
  ]);
  const marks = allByClass(t.node, 'dt-ticker-glyph').map((g) => iconNames(g)[0]);
  assertDeep(marks, ['fail', 'up', 'crown', 'pass', 'expand', 'dot'], 'one drawn mark per row, by kind');
  const follow = live.followRunButton('v1', { entry_id: 'task_a', run_id: 'r1' }, () => {});
  assertDeep(iconNames(follow), ['message'], 'follow draws the message mark');
  assert(follow.getAttribute('aria-label').includes('follow live conversation'), 'and names itself in words');
  const blocks = live.liveMatchGroupedBlocks([{ match_id: 'm', label: 'v1 vs v0', entries: [
    { entry_id: 'a', gen: 'v1', outcome: 'win', settled: true, progress: 1 },
    { entry_id: 'b', gen: 'v1', outcome: 'loss', settled: true, progress: 1 },
    { entry_id: 'c', gen: 'v1', outcome: 'timeout', settled: true, progress: 1 },
  ] }], () => {});
  const outcomes = allByClass(blocks, 'dt-live-match-state').map((s) => iconNames(s)[0]);
  assertDeep(outcomes, ['pass', 'fail', 'timeout'], 'settled outcomes draw pass, fail and timeout');
  const stepper = live.pipelineStepper({ steps: [{ id: 'propose', state: 'done' }, { id: 'race', state: 'active' }] });
  assert(allByClass(stepper, 'dt-pipe-sep').every((s) => iconNames(s)[0] === 'forward'), 'the pipeline steps are joined by the drawn forward arrow');
});

test('verdicts and overrides: the override chip, the confirm row and the lifecycle terminal draw their marks', () => {
  assertDeep(iconNames(ui.overrideLabel({ present: true, action: 'promote' })), ['refresh', 'up'], 'a forced promote');
  assertDeep(iconNames(ui.overrideLabel({ action: 'reject', state: 'queued' })), ['refresh', 'more'], 'a queued override');
  assertDeep(iconNames(ui.overrideLabel({ action: 'reject', state: 'drained' })), ['refresh', 'empty'], 'a drained override');
  const cell = ui.overrideControlCell({ gid: 'v2', readOnly: false, onFire() {} });
  const host = mountInto(cell);
  allByClass(host, 'dn-ovr-arm')[0].dispatchEvent(makeEvent('click'));
  assertDeep(iconNames(allByClass(host, 'dn-ovr-promote')[0]), ['up'], 'force-promote draws the up mark');
  assertDeep(iconNames(allByClass(host, 'dn-ovr-reject')[0]), ['fail'], 'force-reject draws the fail mark');
  const cancel = allByClass(host, 'dn-ovr-cancel')[0];
  assertDeep(iconNames(cancel), ['close'], 'cancel draws the close mark');
  assertEqual(cancel.getAttribute('aria-label'), 'cancel', 'and names itself');
  const rejected = dag.lifecycleDag({ parentId: 'v0', decision: 'rejected', promoted: false, entries: [] });
  assert(iconNames(rejected).includes('fail'), 'a dead branch leads with the fail mark');
  const promoted = dag.lifecycleDag({ parentId: 'v0', decision: 'promoted', promoted: true, entries: [] });
  assert(iconNames(promoted).includes('crown'), 'a promotion leads with the crown');
  const seed = dag.lifecycleDag({ baseline: true, entries: [] });
  assert(iconNames(seed).includes('empty'), 'a seed has no parent: the empty mark');
});

test('execution and transcript marks: tool, agent and artifact kinds, the disclosure caret and annotations are drawn', () => {
  const execution = { nodes: [
    { node_id: 'root', kind: 'agent', name: 'planner', status: 'completed' },
    { node_id: 't1', kind: 'tool', name: 'search', status: 'completed', parent_id: 'root' },
    { node_id: 'a1', kind: 'artifact', name: 'deck.md', status: 'completed', parent_id: 'root' },
  ], edges: [{ parent: 'root', child: 't1' }, { parent: 'root', child: 'a1' }] };
  const outline = turns.buildExecutionOutline(execution, ['root']);
  assert(outline, 'the execution outline renders');
  const kinds = allByClass(outline, 'dn-exec-glyph').map((g) => iconNames(g)[0]);
  for (const k of ['agent', 'tool', 'artifact']) assert(kinds.includes(k), `the ${k} kind draws its mark (got ${kinds.join(',')})`);
  assert(allByClass(outline, 'dn-exec-caret').length >= 1, 'a node with children draws the disclosure caret');
  const ann = new Map([[1, [{ kind: 'drift', summary: 'went off topic' }]]]);
  const turn = turns.buildTurnNode({ seq: 1, role: 'agent', text: 'hi', tool_calls: [{ name: 'search', args: { q: 'x' } }] }, ann, null);
  assert(iconNames(turn).includes('tool'), 'a tool call draws the tool mark');
  assert(iconNames(allByClass(turn, 'dn-annot')[0]).includes('note'), 'an annotation draws the note mark');
});

test('dropdown carets draw the chevron', () => {
  const swatch = buildSwatchDropdown('monokai', () => {});
  assertDeep(iconNames(allByClass(mountInto(swatch.node), 'dt-cd-caret')[0]), ['collapse'], 'the colour picker caret');
  const face = buildTypefaceDropdown(null, () => {});
  assertDeep(iconNames(allByClass(mountInto(face.node), 'dt-cd-caret')[0]), ['collapse'], 'the typeface picker caret');
});

// ── 5. a mark that replaced text keeps the text's reach ───────────────────
test('elim radial seat: the gate group carries the hovercard in every state; no empty focus stop', async () => {
  const { svg, elimCase } = await import('./fixtures.mjs');
  const hovercard = await import('../js/hovercard.js');
  const served = elimCase('duplicate_pending_match').served;
  const tips = { crowned: 'v5 · crowned champion', stands: 'champion stands', deciding: 'gate deciding…', pending: 'champion gate' };
  for (const [gateState, tip] of Object.entries(tips)) {
    const node = svg.elimRadial({ rounds: served.rounds, gen_states: served.gen_states,
      championId: gateState === 'crowned' ? 'v5' : null, benchmarkId: 'v6', gateState, onCompetitor() {} });
    const gate = allByClass(node, 'dn-elimradial-gate')[0];
    assert(hovercard.hasHovercard(gate), `${gateState}: the gate group is hovercard-wired`);
    gate.dispatchEvent(makeEvent('mouseenter'));
    assertEqual(hovercard.cardText(), tip, `${gateState}: hovering the seat shows its tip`);
    gate.dispatchEvent(makeEvent('mouseleave'));
    const stops = node.querySelectorAll('[tabindex]');
    for (const stop of stops) {
      assert(String(stop.textContent).trim() || iconNames(stop).length, `${gateState}: a focus stop holds text or an icon`);
    }
    assertEqual(allByClass(gate, 'dn-elimradial-seatlab').filter((t) => !String(t.textContent).trim()).length, 0,
      `${gateState}: no empty seat label`);
  }
});

test('override chip: the direction is in words for assistive technology and in the tooltip', () => {
  const cases = [
    [{ present: true, action: 'promote', reason: 'operator call' }, 'forced promote · operator', 'operator override · forced promote · operator call'],
    [{ present: true, action: 'reject' }, 'forced reject · operator', 'operator override · forced reject'],
    [{ action: 'promote', state: 'queued' }, 'queued promote · operator', 'operator override · queued promote'],
    [{ action: 'reject', state: 'drained' }, 'drained reject · operator', 'operator override · drained reject'],
  ];
  for (const [prov, name, tip] of cases) {
    const chip = ui.overrideLabel(prov);
    assertEqual(chip.getAttribute('role'), 'img', 'the chip is one named image');
    assertEqual(chip.getAttribute('aria-label'), name, 'its accessible name states the direction');
    assertEqual(chip.getAttribute('title'), tip, 'its tooltip states the direction');
  }
});

test('trace detail: changing an episode\'s signal kind redraws its icon', async () => {
  const { freshState, installFixtureMap } = await import('./fixtures.mjs');
  const traces = await import('../js/views/traces.js');
  const load = (name) => JSON.parse(readFileSync(join(STATIC, 'test', 'fixtures', 'trace_view', name + '.json'), 'utf8'));
  const list = load('list');
  const detail = load('detail');
  const map = (d) => ({
    '/api/epoch': { epoch_id: list.epoch_id, closed: false, goal: 'boot' },
    '/api/reflections': { reflections: [{ reflection_id: list.reflection_id, epoch_id: list.epoch_id, created_at: '2020-01-01T00:00:00Z', mode: 'mint', executed: true }] },
    [`/api/reflection/${list.reflection_id}/traces`]: list,
    [`/api/reflection/${list.reflection_id}/trace/${d.trace_id}`]: d,
  });
  const route = { epochId: list.epoch_id, reflectionId: list.reflection_id, traceId: detail.trace_id };
  const ctx = { navigate() {}, href: router.href };
  const epIcons = (host) => allByClass(host, 'dn-trace-ep-glyph').map((g) => iconNames(g)[0]);
  const host = document.createElement('div');
  freshState(); installFixtureMap(map(detail));
  await traces.render(host, ctx, route);
  assertEqual(epIcons(host)[0], 'fail', 'an error cascade draws the fail mark');
  const changed = JSON.parse(JSON.stringify(detail));
  changed.episodes[0].signal_kind = 'retry_loop';
  changed.strip_model.episodes[0].signal_kind = 'retry_loop';
  freshState(); installFixtureMap(map(changed));
  await traces.render(host, ctx, route);
  assertEqual(epIcons(host)[0], 'refresh', 'the new kind redraws the episode icon');
});

await run();

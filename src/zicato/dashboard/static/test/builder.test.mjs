// test/builder.test.mjs — the tournament-builder view (B2).
//
// Exercises the slice of frontend behaviour the structural / parsing tests
// can't reach: the view renders each contract section; a form edit calls
// POST /builder/op and applies the returned {draft,patch,cost,warnings,diff}
// to the SHARED draft so the form + preview re-render; an SSE `patch` frame
// applied through the chat path updates the same shared draft; the chat pane
// resize persists + clamps and the layout reflows without overlap; the
// graceful-degrade path disables the chat input. Same harness style as
// variant_t.test.mjs.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

// ── a route-keyed fetch mock for the builder endpoints ───────────────
//
// The view loads /builder/config + /builder/draft once, then POSTs ops. We
// record the op calls and return a mutated draft so the apply-to-shared-draft
// path is observable. A chat-disabled config is the default so the degrade
// path is the baseline; a test flips `CONFIG.chat_enabled` before mount.

const CONFIG = { chat_enabled: false, agent: { model: '' }, skills: ['zicato-build-tournament'] };

function freshDraft() {
  return {
    scoring: {
      // The backend serializes the tournament block under the `tournament`
      // key (zicato.epoch.lifecycle._scoring_to_dict), NOT `tournament_structure`
      // (that is the Python ScoringWeights attribute name). The view must read
      // this exact key — the stuck-on-Gauntlet bug was reading the wrong one.
      tournament: { structure: 'gauntlet', params: { field_size: 2, replicates: 1 } },
      overfitting: { enabled: true, holdout_fraction: 0.2, min_board_size_for_split: 8 },
      promote_margin: 0, pass_rate_monotonicity: false,
    },
    board: [
      { id: 'waffles', kind: 'single_turn' },
      { id: 'picky', kind: 'multi_turn_emulated' },
    ],
    brief: 'be crisper',
    proposer_path: null,
    proposer: { agent_id: 'default', tools: ['edit'], skills: [{ name: 'compose', description: 'compose a proposer' }], has_custom_agent: false },
    holdout: { train_ids: ['waffles', 'picky'], holdout_ids: [] },
  };
}

let DRAFT = freshDraft();
const OP_CALLS = [];

function envelope(patch) {
  return {
    draft: DRAFT,
    patch: patch || { op: 'noop', changed: {} },
    cost: { structure: DRAFT.scoring.tournament.structure, board_size: 2, holdout_size: 0,
      board_runs_per_round: 4, breakdown: [{ label: 'duel runs', runs: 4, detail: 'field 2 × board 2' }] },
    warnings: [],
    diff: { components: [], changed_components: [], rolls_epoch: false },
  };
}

function installBuilderFetch() {
  OP_CALLS.length = 0;
  DRAFT = freshDraft();
  globalThis.fetch = async (path, init) => {
    const body = init && init.body ? JSON.parse(init.body) : {};
    if (path === '/builder/config') return jsonRes(CONFIG);
    if (path.startsWith('/builder/draft')) {
      return jsonRes({ session: 'dashboard', draft: DRAFT, cost: envelope().cost, warnings: [], diff: envelope().diff });
    }
    if (path === '/builder/op') {
      OP_CALLS.push(body);
      // mutate the shared draft so the applied envelope is observably different.
      if (body.op === 'set_structure') DRAFT.scoring.tournament.structure = body.args.structure;
      if (body.op === 'set_param') DRAFT.scoring.tournament.params[body.args.key] = body.args.value;
      if (body.op === 'set_gate' && body.args.promote_margin != null) DRAFT.scoring.promote_margin = body.args.promote_margin;
      if (body.op === 'set_holdout' && Array.isArray(body.args.tags)) {
        const held = new Set(body.args.tags);
        DRAFT.holdout.holdout_ids = DRAFT.board.map((b) => b.id).filter((id) => held.has(id));
        DRAFT.holdout.train_ids = DRAFT.board.map((b) => b.id).filter((id) => !held.has(id));
      }
      const env = envelope({ op: body.op, changed: body.args });
      if (body.op === 'set_structure') env.diff = { changed_components: ['structure', 'scoring'], rolls_epoch: true };
      return jsonRes(env);
    }
    if (path === '/builder/apply') {
      return jsonRes({ confirmed: !!body.confirm, rolled: !!body.confirm, components_changed: ['structure'],
        new_contract_hash: 'abc123def456ghi789', cost: envelope().cost, diff: envelope().diff, warnings: [] });
    }
    return jsonRes({});
  };
}

function jsonRes(obj) {
  return {
    ok: true, status: 200,
    headers: { get: () => 'application/json' },
    json: async () => obj,
    text: async () => JSON.stringify(obj),
  };
}

// install the minimal globals the view + chat touch (mirrors variant_t setup).
function installEnv() {
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  globalThis.window = globalThis.window || {};
  globalThis.window.addEventListener = globalThis.window.addEventListener || (() => {});
  globalThis.window.removeEventListener = globalThis.window.removeEventListener || (() => {});
  if (!globalThis.window.localStorage) {
    const kv = new Map();
    globalThis.window.localStorage = {
      getItem: (k) => (kv.has(k) ? kv.get(k) : null),
      setItem: (k, v) => kv.set(k, String(v)), removeItem: (k) => kv.delete(k), clear: () => kv.clear(),
    };
  }
}

installEnv();

const builder = await import('../js/variants/T/builder/model.js');
const popover = await import('../js/variants/T/builder/popover.js');
const stream = await import('../js/variants/T/builder/stream.js');
const { BuilderChat } = await import('../js/variants/T/builder/chat.js');

// the view module holds shared module-level draft state; re-import is cached
// across tests, so each test re-mounts and drives through the public render().
const view = await import('../js/variants/T/views/builder.js');

// helper: find descendants by class via the harness querySelectorAll.
function byClass(root, cls) { return root.querySelectorAll('[class]').filter((n) => n.classList.contains(cls)); }
function firstClass(root, cls) { return byClass(root, cls)[0] || null; }

// ── tests ─────────────────────────────────────────────────────────────

test('builder view: renders the left-rail sections + the structure section', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const railItems = byClass(host, 'dn-bld-railitem');
  assertEqual(railItems.length, 6, 'six contract sections in the left rail');
  // the structure section leads with the five structure cards.
  const cards = byClass(host, 'dn-bld-card');
  assertEqual(cards.length, 5, 'five structure picker cards');
  // the live preview painted a cost meter from the op envelope's cost.
  assert(firstClass(host, 'dn-bld-cost'), 'the live preview shows a cost meter');
});

test('builder view: a structure pick calls /builder/op and applies the returned draft + diff', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const cards = byClass(host, 'dn-bld-card');
  // pick "swiss" (4th card) — click drives set_structure.
  const swiss = cards.find((c) => c.textContent.toLowerCase().includes('swiss'));
  assert(swiss, 'the swiss card is present');
  swiss.dispatchEvent(makeEvent('click'));
  await tick();
  assertEqual(OP_CALLS.length, 1, 'exactly one op call');
  assertEqual(OP_CALLS[0].op, 'set_structure', 'the op was set_structure');
  assertEqual(OP_CALLS[0].args.structure, 'swiss', 'the structure arg was swiss');
  // the applied diff rolls the epoch — the preview impact pill reflects it.
  const impact = firstClass(host, 'dn-bld-impact');
  assert(impact && impact.textContent.includes('rolls epoch'), 'the contract-impact pill shows the epoch roll');
  // ── the VISUAL selected-state must follow the new structure (the bug) ──
  // After the set_structure result is applied, the highlighted card must be
  // the picked one — derived from the draft's `tournament.structure`, never
  // stuck on the first (Gauntlet) card. Assert on the DOM class, not the data.
  const onCards = byClass(host, 'dn-bld-card').filter((c) => c.classList.contains('dn-bld-card-on'));
  assertEqual(onCards.length, 1, 'exactly one structure card carries the selected class');
  assert(onCards[0].textContent.toLowerCase().includes('swiss'), 'the SELECTED card is Swiss, not Gauntlet');
  assertEqual(onCards[0].getAttribute('aria-pressed'), 'true', 'the selected card reports aria-pressed=true');
});

test('builder view: a draft whose structure is racing highlights the Racing card (not Gauntlet)', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  // Drive the draft to racing through the real op/apply path, then RE-RENDER
  // the mounted view. The data layer ends up racing (envelope applied); the
  // assertion is purely on the selected-state render — it must follow the
  // draft's `tournament.structure`, never stay pinned to the first card.
  const racing = byClass(host, 'dn-bld-card').find((c) => c.textContent.toLowerCase().includes('racing'));
  assert(racing, 'the racing card is present');
  racing.dispatchEvent(makeEvent('click'));
  await tick();
  // a second render() re-mounts the view against the now-racing shared draft,
  // mirroring a persisted racing draft re-opened — the highlight must persist.
  await view.render(host);
  const onCards = byClass(host, 'dn-bld-card').filter((c) => c.classList.contains('dn-bld-card-on'));
  assertEqual(onCards.length, 1, 'exactly one structure card is selected for the racing draft');
  assert(onCards[0].textContent.toLowerCase().includes('racing'), 'the racing draft highlights Racing, not Gauntlet');
});

test('builder view: a field param edit posts set_param with the numeric value', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  // go to the Field & noise section.
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Field'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  const num = byClass(host, 'dn-bld-num')[0];
  assert(num, 'a numeric field-param control is present');
  num.value = '3';
  num.dispatchEvent(makeEvent('change', { target: num }));
  await tick();
  const setParam = OP_CALLS.find((c) => c.op === 'set_param');
  assert(setParam, 'set_param was posted');
  assertEqual(setParam.args.value, 3, 'the value was sent as a number');
});

test('builder view: a holdout toggle posts set_holdout tags and re-renders the split strip', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Board'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  const toggle = byClass(host, 'dn-bld-holdtoggle')[0];
  assert(toggle, 'a holdout toggle is present');
  toggle.dispatchEvent(makeEvent('click'));
  await tick();
  const setHoldout = OP_CALLS.find((c) => c.op === 'set_holdout');
  assert(setHoldout && Array.isArray(setHoldout.args.tags), 'set_holdout posted with tags');
  assertEqual(setHoldout.args.tags.length, 1, 'one entry held out');
});

test('builder view: the Review section dry-run + apply call /builder/apply with the right confirm', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Review'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  let applyBody = null;
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    if (path === '/builder/apply') applyBody = JSON.parse(init.body);
    return realFetch(path, init);
  };
  // dry-run → confirm:false
  const dry = byClass(host, 'dn-bld-btn-dry')[0];
  dry.dispatchEvent(makeEvent('click'));
  await tick();
  assertEqual(applyBody.confirm, false, 'dry-run sends confirm:false');
  // apply needs an explicit second click (the confirm gesture) → confirm:true
  const apply = byClass(host, 'dn-bld-btn-apply')[0];
  apply.dispatchEvent(makeEvent('click'));   // arms the confirm
  await tick();
  apply.dispatchEvent(makeEvent('click'));   // confirms
  await tick();
  assertEqual(applyBody.confirm, true, 'the confirmed apply sends confirm:true');
});

test('info popover: opens on focus, exposes aria, closes on Escape', () => {
  const pop = popover.infoPopover({ title: 'Field size', def: '2', body: 'how many challengers' });
  const trigger = pop.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-bld-info'))[0];
  const panel = pop.querySelectorAll('[role]').filter((n) => n.getAttribute('role') === 'note')[0];
  assert(trigger && panel, 'the popover has a trigger + a note panel');
  assertEqual(trigger.getAttribute('aria-expanded'), 'false', 'starts collapsed');
  assertEqual(panel.getAttribute('aria-hidden'), 'true', 'panel hidden initially');
  trigger.dispatchEvent(makeEvent('focus'));
  assertEqual(trigger.getAttribute('aria-expanded'), 'true', 'focus opens it');
  assertEqual(panel.getAttribute('aria-hidden'), 'false', 'panel shown on open');
  trigger.dispatchEvent(makeEvent('keydown', { key: 'Escape' }));
  assertEqual(panel.getAttribute('aria-hidden'), 'true', 'Escape closes it');
});

test('chat pane: graceful-degrade disables the input when chat_enabled is false', () => {
  const chat = new BuilderChat({ config: { chat_enabled: false, agent: { model: '' } } });
  const input = chat.node.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-bld-chat-input'))[0];
  const send = chat.node.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-bld-chat-send'))[0];
  assert(input.hasAttribute('disabled'), 'the input is disabled');
  assert(send.hasAttribute('disabled'), 'the send button is disabled');
  // the degrade hint names the config file, never a vendor.
  assert(chat.node.textContent.includes('builder.json'), 'the degrade hint points at builder.json');
});

test('chat pane: header shows the configured model NAME only', () => {
  const chat = new BuilderChat({ config: { chat_enabled: true, agent: { model: 'house-model-x' } } });
  const m = chat.node.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-bld-chat-model'))[0];
  assertEqual(m.textContent, 'house-model-x', 'the model name is shown');
});

test('chat pane: resize clamps to [min,max] + persists + reflows the host via onWidthChange', () => {
  globalThis.window.localStorage.clear();
  let lastWidth = null;
  const chat = new BuilderChat({
    config: { chat_enabled: true, agent: { model: 'm' } },
    min: builder.CHAT_MIN, max: builder.CHAT_MAX, initialWidth: 340,
    onWidthChange: (w) => { lastWidth = w; builder.persistChatWidth(w); },
  });
  const handle = chat.node.querySelectorAll('[role]').filter((n) => n.getAttribute('role') === 'separator')[0];
  // a keyboard nudge widens (ArrowLeft) — the host gets the reflow callback.
  handle.dispatchEvent(makeEvent('keydown', { key: 'ArrowLeft' }));
  assert(lastWidth > 340, 'ArrowLeft widened the pane');
  // Home jumps to max; a beyond-max value clamps.
  handle.dispatchEvent(makeEvent('keydown', { key: 'Home' }));
  assertEqual(lastWidth, builder.CHAT_MAX, 'Home clamps to max');
  assertEqual(chat.width(), builder.CHAT_MAX, 'the pane width is the clamped max');
  // persisted + re-readable through the model helpers (clamped on read too).
  assertEqual(builder.readChatWidth(), builder.CHAT_MAX, 'the width persisted to localStorage');
  // a synthetic drag of the LEFT edge: moving left widens, capped at max.
  handle.dispatchEvent(makeEvent('pointerdown', { clientX: 500, pointerId: 1 }));
  handle.dispatchEvent(makeEvent('pointermove', { clientX: 200 }));
  handle.dispatchEvent(makeEvent('pointerup', { clientX: 200 }));
  assertEqual(chat.width(), builder.CHAT_MAX, 'a wide drag stays clamped at max (no overflow)');
});

test('chat pane: collapse toggles the strip + persists', () => {
  globalThis.window.localStorage.clear();
  let collapsedSeen = null;
  const chat = new BuilderChat({
    config: { chat_enabled: true, agent: { model: 'm' } },
    onCollapse: (c) => { collapsedSeen = c; builder.persistChatCollapsed(c); },
  });
  assertEqual(chat.collapsed(), false, 'starts expanded');
  const btn = chat.node.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-bld-chat-collapse'))[0];
  btn.dispatchEvent(makeEvent('click'));
  assertEqual(chat.collapsed(), true, 'collapses on click');
  assertEqual(collapsedSeen, true, 'the host was told to reflow');
  assert(chat.node.classList.contains('dn-bld-chat-collapsed'), 'the collapsed class is applied (thin strip)');
  assertEqual(builder.readChatCollapsed(), true, 'collapse state persisted');
});

test('chat stream: a patch frame is dispatched to onPatch (the shared-draft update)', async () => {
  const frames = [
    { type: 'token', text: 'I will ' },
    { type: 'token', text: 'set racing.' },
    { type: 'tool', name: 'set_structure', args: { structure: 'racing' } },
    { type: 'patch', patch: { op: 'set_structure', changed: { structure: { from: 'gauntlet', to: 'racing' } } },
      cost: { board_runs_per_round: 9, breakdown: [] }, warnings: [], diff: { changed_components: ['structure'], rolls_epoch: true } },
    { type: 'done' },
  ];
  let tokens = '';
  let tool = null;
  let patch = null;
  await stream.streamChat('/builder/chat', { message: 'use racing' }, {
    frames,
    onToken: (t) => { tokens += t; },
    onTool: (n) => { tool = n; },
    onPatch: (p) => { patch = p; },
  });
  assertEqual(tokens, 'I will set racing.', 'token deltas streamed in order');
  assertEqual(tool, 'set_structure', 'the tool step was surfaced');
  assert(patch && patch.diff.rolls_epoch, 'the patch frame carried the contract diff');
  assertEqual(patch.patch.op, 'set_structure', 'the patch op is the structure change');
});

test('chat stream: an error frame stops the stream and calls onError', async () => {
  const frames = [
    { type: 'token', text: 'hmm' },
    { type: 'error', message: 'configure builder.json to enable chat' },
    { type: 'token', text: 'should-not-arrive' },
  ];
  let tokens = '';
  let err = null;
  await stream.streamChat('/builder/chat', { message: 'x' }, {
    frames, onToken: (t) => { tokens += t; }, onError: (m) => { err = m; },
  });
  assertEqual(tokens, 'hmm', 'streaming halts at the error frame');
  assert(err && err.includes('builder.json'), 'the graceful-degrade error message surfaced');
});

test('builder view: the work column reflows the chat width as a CSS var (no overlap)', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const root = host.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-builder'))[0];
  assert(root, 'the builder root is present');
  // the chat-width CSS var is stamped so the grid template reflows the work
  // column to the remaining width (real layout, no overlap/clipping).
  const varVal = root.style._props['--dn-bld-chat'];
  assert(varVal && /px$/.test(varVal), 'the chat-width CSS var is stamped in px on the root');
});

// ── structure-card glyphs (ported from the approved mockup) ───────────

test('structure glyphs: each of the five structures renders a crisp theme-token SVG (currentColor only)', () => {
  for (const id of ['gauntlet', 'swiss', 'single_elim', 'double_elim', 'racing']) {
    const svg = builder.structureGlyphSvg(id);
    assert(svg && svg.localName === 'svg', `${id} → an svg glyph`);
    assertEqual(svg.getAttribute('viewBox'), '0 0 24 24', `${id} glyph is a crisp 24×24 viewBox`);
    assertEqual(svg.getAttribute('class'), 'dn-bld-cardglyph', `${id} glyph carries the card-glyph class`);
    // theme tokens ONLY — every stroke/fill is currentColor, never a hex colour.
    for (const n of allDesc(svg)) {
      const stroke = n.getAttribute('stroke');
      const fill = n.getAttribute('fill');
      if (stroke && stroke !== 'none') assertEqual(stroke, 'currentColor', `${id}: stroke is the currentColor token`);
      if (fill && fill !== 'none') assertEqual(fill, 'currentColor', `${id}: fill is the currentColor token`);
      assert(!/#[0-9a-fA-F]{3,6}/.test(`${stroke} ${fill}`), `${id}: no hardcoded hex colour`);
    }
  }
});

test('structure glyphs: gauntlet is ●—● (two dots + a join), racing is a 4→2→1 funnel of dots', () => {
  // gauntlet: two duel dots joined by a short line.
  const g = builder.structureGlyphSvg('gauntlet');
  const gDots = allDesc(g).filter((n) => n.localName === 'circle');
  const gPaths = allDesc(g).filter((n) => n.localName === 'path');
  assertEqual(gDots.length, 2, 'gauntlet draws the two duel dots');
  assertEqual(gPaths.length, 1, 'gauntlet draws the single joining line');

  // racing: a narrowing funnel of dots — 4 + 2 + 1 = 7 dots.
  const r = builder.structureGlyphSvg('racing');
  const rDots = allDesc(r).filter((n) => n.localName === 'circle');
  assertEqual(rDots.length, 7, 'racing draws a 4→2→1 funnel of dots');

  // swiss: three stacked ranking lines (no dots).
  const s = builder.structureGlyphSvg('swiss');
  const sPaths = allDesc(s).filter((n) => n.localName === 'path');
  assertEqual(sPaths.length, 3, 'swiss draws three stacked ranking lines');

  // double_elim carries the extra losers'-lane line over single_elim.
  const se = allDesc(builder.structureGlyphSvg('single_elim')).filter((n) => n.localName === 'path').length;
  const de = allDesc(builder.structureGlyphSvg('double_elim')).filter((n) => n.localName === 'path').length;
  assert(de > se, 'double-elim adds the extra losers-lane line over single-elim');
});

function allDesc(node) {
  const out = [];
  const walk = (n) => { for (const c of n.children) { out.push(c); walk(c); } };
  walk(node);
  return out;
}

function tick() { return new Promise((r) => setTimeout(r, 0)); }

await run();

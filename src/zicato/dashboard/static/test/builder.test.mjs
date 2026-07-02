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
      // the preflight READ op: the normal envelope plus the `preflight` result
      // (here the REFUSE case) and the just-measured-floor refuse warning.
      if (body.op === 'preflight') {
        const env = envelope({ op: 'preflight', changed: {} });
        env.preflight = {
          available: true, verdict: 'refuse', reason: '',
          report: {
            verdict: 'refuse', signal: 0.02, noise_floor_max_abs_delta: 0.14,
            noise_floor_runs: 5, degraded_mutation_id: 'style_rules',
          },
          noise_floor: { max_abs_delta: 0.14, runs: 5 },
        };
        env.warnings = [{
          code: 'margin_below_noise_floor', severity: 'refuse',
          message: 'promote_margin 0 does not clear the measured A/A noise floor 0.14…',
        }];
        return jsonRes(env);
      }
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

const builder = await import('../js/builder/model.js');
const popover = await import('../js/builder/popover.js');
const stream = await import('../js/builder/stream.js');
const { BuilderChat } = await import('../js/builder/chat.js');

// the view module holds shared module-level draft state; re-import is cached
// across tests, so each test re-mounts and drives through the public render().
const view = await import('../js/views/builder.js');

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

test('chat pane: is a 3-row flex column with the message log flex:1 and the composer pinned at the BOTTOM', () => {
  // The pane must be a docked full-height column: header pinned top, message
  // log the single growable (flex:1) middle child, composer pinned bottom — so
  // even an empty conversation puts the input at the bottom edge, never a short
  // content-sized stub. Assert the structural layout (DOM order + the classes
  // the CSS flex rules key on), since the mock DOM does not compute styles.
  const chat = new BuilderChat({ config: { chat_enabled: true, agent: { model: 'm' } } });
  // the pane → [handle, body, strip]
  const body = chat.node.children.find((n) => n.classList.contains('dn-bld-chat-body'));
  assert(body, 'the pane has a flex body column');
  // the body's three rows, in order: head · log · composer.
  const rows = body.children;
  assertEqual(rows.length, 3, 'the body is a 3-row flex column (header · log · composer)');
  assert(rows[0].classList.contains('dn-bld-chat-head'), 'row 1 is the header (pinned top)');
  assert(rows[1].classList.contains('dn-bld-chat-log'), 'row 2 is the scrollable message log');
  assert(rows[2].classList.contains('dn-bld-chat-composer'), 'row 3 is the composer, LAST → pinned at the bottom');
  // the message log is the growable child the CSS gives flex:1 + overflow-y:auto
  // (so long conversations scroll INSIDE the pane, not the page).
  assertEqual(chat._log, rows[1], 'the streamed message log is the flex:1 middle row');
  // the composer holds the input + send (the bottom-pinned 3rd row).
  assert(rows[2].children.some((n) => n.classList.contains('dn-bld-chat-input')), 'the composer carries the input');
  assert(rows[2].children.some((n) => n.classList.contains('dn-bld-chat-send')), 'the composer carries the send button');
});

test('chat pane: the chat-disabled (degrade) state keeps the full-height frame — composer still pinned at the bottom', () => {
  // The "configure builder.json" degrade state must NOT collapse the pane: the
  // degrade notice sits INSIDE the flex:1 log row, so the composer stays pinned
  // at the bottom of the full-height frame just like the enabled state.
  const chat = new BuilderChat({ config: { chat_enabled: false, agent: { model: '' } } });
  const body = chat.node.children.find((n) => n.classList.contains('dn-bld-chat-body'));
  const rows = body.children;
  assertEqual(rows.length, 3, 'the disabled pane keeps the 3-row frame');
  assert(rows[2].classList.contains('dn-bld-chat-composer'), 'the composer is still the LAST row (bottom-pinned)');
  // the degrade notice rides inside the (flex:1) log, not in place of the frame.
  const log = rows[1];
  assert(log.classList.contains('dn-bld-chat-log'), 'the middle row is still the flex:1 log');
  assert(log.children.some((n) => n.classList.contains('dn-bld-chat-degrade')), 'the degrade notice sits inside the growable log');
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

test('structure glyphs: gauntlet is ●—● (two dots + a join), racing is a 3→2→1 staggered funnel', () => {
  // gauntlet: two duel dots joined by a short line.
  const g = builder.structureGlyphSvg('gauntlet');
  const gDots = allDesc(g).filter((n) => n.localName === 'circle');
  const gPaths = allDesc(g).filter((n) => n.localName === 'path');
  assertEqual(gDots.length, 2, 'gauntlet draws the two duel dots');
  assertEqual(gPaths.length, 1, 'gauntlet draws the single joining line');

  // racing: a staggered funnel of dots — 3 + 2 + 1 = 6 dots, two cut arms faded.
  const r = builder.structureGlyphSvg('racing');
  const rDots = allDesc(r).filter((n) => n.localName === 'circle');
  assertEqual(rDots.length, 6, 'racing draws a 3→2→1 funnel of dots');
  const faded = rDots.filter((n) => n.getAttribute('fill-opacity') != null);
  assertEqual(faded.length, 2, 'racing fades the two cut arms');

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

// ── full-height docked-workspace layout (CSS-level guard) ─────────────
//
// The mock DOM does not compute styles, so the structural tests above assert
// the DOM (3-row flex, composer last). These read the CSS source to lock the
// rules that make the workspace full-height: the builder root gets a DEFINITE
// height from the viewport (so the chat pane stretches to a docked column, not
// a content-sized stub) and the message log is the flex:1 growable child.

const _builderCss = (await import('node:fs'))
  .readFileSync(new URL('../css/console.css', import.meta.url), 'utf8')
  .replace(/\n/g, ' ');
function readBuilderCss() { return _builderCss; }

test('builder CSS: the builder root is a DEFINITE full-height frame (viewport − top bar − viewhost padding)', () => {
  const css = readBuilderCss();
  const block = /\.dn-builder\s*\{([^}]*)\}/.exec(css);
  assert(block, 'the .dn-builder rule exists');
  const decl = block[1];
  // a definite height computed from the viewport (NOT content-driven) so the
  // grid children stretch to a docked full-height column.
  assert(/height:\s*calc\(100vh/.test(decl), 'the builder root takes a definite viewport-derived height');
  assert(/align-items:\s*stretch/.test(decl), 'the grid stretches its children (work column + chat) to that height');
});

test('builder CSS: the chat log is the flex:1 scrollable middle row + the composer is a bottom border-top row', () => {
  const css = readBuilderCss();
  const log = /\.dn-bld-chat-log\s*\{([^}]*)\}/.exec(css);
  assert(log && /flex:\s*1/.test(log[1]), 'the message log is the flex:1 growable child');
  assert(log && /overflow-y:\s*auto/.test(log[1]), 'the message log scrolls internally (long chats scroll in-pane)');
  const composer = /\.dn-bld-chat-composer\s*\{([^}]*)\}/.exec(css);
  assert(composer && /border-top/.test(composer[1]), 'the composer is the bottom-pinned row (border-top divider)');
  // the work column children scroll internally so the full-height frame never clips.
  const cols = /\.dn-bld-center,\s*\.dn-bld-preview\s*\{([^}]*)\}/.exec(css);
  assert(cols && /overflow-y:\s*auto/.test(cols[1]), 'the center + preview columns scroll internally');
});

// ── estimateCost: the per-structure default-replicates twin ───────────
//
// The JS estimateCost is the exact twin of operations.py's estimate_cost. The
// under-reporting bug class: when `replicates` is UNSET the meter must default
// to the STRUCTURE's own default (swiss / elim default to 2), not a flat 1, so
// the dashboard preview matches the Python estimator. These mirror the Python
// cost tests (test_cost_swiss_unset_replicates_uses_strategy_default_two, the
// explicit-override test, and the per-structure-default pin) number-for-number.

test('estimateCost: swiss with UNSET replicates uses the strategy default of 2 (not a flat 1)', () => {
  // field_size 4, board 8, holdout 0, replicates UNSET. The Python estimator
  // reports rounds_n 4 × pairings 2 × replicates 2 × board 8 = 128 — the SAME
  // number this JS twin must produce. A flat-1 default would report 64 (half).
  const est = builder.estimateCost('swiss', { field_size: 4 }, 8, 0);
  assertEqual(est.board_runs_per_round, 128, 'swiss unset replicates → 128 (the py estimator value, not 64)');
  const swissLine = est.breakdown.find((l) => l.label === 'swiss-pairing runs');
  assert(swissLine && /replicates 2/.test(swissLine.detail), 'the breakdown detail shows replicates 2');
});

test('estimateCost: an explicit replicates is honored verbatim over the structure default', () => {
  const est1 = builder.estimateCost('swiss', { field_size: 4, replicates: 1 }, 8, 0);
  assertEqual(est1.board_runs_per_round, 64, 'explicit replicates=1 → 64 (4 × 2 × 1 × 8)');
  const est3 = builder.estimateCost('swiss', { field_size: 4, replicates: 3 }, 8, 0);
  assertEqual(est3.board_runs_per_round, 192, 'explicit replicates=3 → 192 (4 × 2 × 3 × 8)');
});

// ── the build-time statistical pre-flight (Review pane) ────────────────

test('builder view: the Review pane runs the preflight op and renders the verdict chip + refuse warnings', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Review'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  // the preflight control renders BEFORE apply, with no verdict yet.
  const pfBtn = byClass(host, 'dn-bld-btn-preflight')[0];
  assert(pfBtn, 'the Review pane offers a Run-preflight control');
  assertEqual(byClass(host, 'dn-bld-pf-chip').length, 0, 'no verdict chip before a measurement');
  pfBtn.dispatchEvent(makeEvent('click'));
  await tick();
  const pfCall = OP_CALLS.find((c) => c.op === 'preflight');
  assert(pfCall, 'the preflight op was posted through the same /builder/op dispatch');
  // the REFUSE verdict chip + reasons render from the returned envelope.
  const chip = byClass(host, 'dn-bld-pf-chip')[0];
  assert(chip, 'a verdict chip rendered');
  assert(chip.classList.contains('dn-bld-pf-refuse'), 'the chip carries the refuse class');
  assert(chip.textContent.includes('REFUSE'), 'the chip names the verdict');
  const reasons = firstClass(host, 'dn-bld-pf-reasons');
  assert(reasons && reasons.textContent.includes('noise floor'), 'the reasons name the measured floor');
  assert(reasons.textContent.includes('signal'), 'the reasons name the achievable signal');
  // the REFUSE-severity validate warning surfaces in the Review pane itself.
  const refuse = byClass(host, 'dn-bld-warn-refuse')[0];
  assert(refuse && refuse.textContent.includes('noise floor'), 'the margin-vs-floor refuse warning renders beside apply');
});

test('validateContract twin: margin at/below a measured floor with the evidence gate off → refuse', () => {
  // Gate off, margin 0.01 <= floor 0.05 → the refuse-severity rule fires.
  const warns = builder.validateContract('gauntlet', {}, 6, 0, {}, { promoteMargin: 0.01, noiseFloor: 0.05 });
  const hit = warns.find((w) => w.code === 'margin_below_noise_floor');
  assert(hit, 'the margin_below_noise_floor rule fired');
  assertEqual(hit.severity, 'refuse', 'the rule is refuse-severity');
  // Evidence gate ON (threshold in (0,1)) silences it.
  const gated = builder.validateContract('gauntlet', { promote_confidence_threshold: 0.8 }, 6, 0, {}, { promoteMargin: 0.01, noiseFloor: 0.05 });
  assert(!gated.find((w) => w.code === 'margin_below_noise_floor'), 'the evidence gate silences the rule');
  // Margin clearing the floor silences it.
  const clear = builder.validateContract('gauntlet', {}, 6, 0, {}, { promoteMargin: 0.06, noiseFloor: 0.05 });
  assert(!clear.find((w) => w.code === 'margin_below_noise_floor'), 'a clearing margin is silent');
  // No floor known → silent (never guess).
  const unknown = builder.validateContract('gauntlet', {}, 6, 0, {}, { promoteMargin: 0.01 });
  assert(!unknown.find((w) => w.code === 'margin_below_noise_floor'), 'no measured floor, no rule');
});

test('estimateCost: the per-structure default-replicates twin matches the Python map for every structure', () => {
  // The JS default-replicates map is the twin of the Python
  // STRUCTURE_DEFAULT_REPLICATES (derived from each strategy's
  // _default_replicates). Pin every structure so the two can never drift.
  // Base default 2 (noise-aware); racing pins 1 (intrinsic replication).
  const expected = { gauntlet: 2, single_elim: 2, double_elim: 2, swiss: 2, racing: 1 };
  for (const [structure, def] of Object.entries(expected)) {
    assertEqual(builder.defaultReplicatesFor(structure), def, `${structure} default replicates`);
    // The UNSET-default estimate equals the explicit-default estimate.
    const unset = builder.estimateCost(structure, { field_size: 4 }, 8, 0);
    const explicit = builder.estimateCost(structure, { field_size: 4, replicates: def }, 8, 0);
    assertEqual(unset.board_runs_per_round, explicit.board_runs_per_round, `${structure}: unset == explicit-default`);
  }
});

await run();

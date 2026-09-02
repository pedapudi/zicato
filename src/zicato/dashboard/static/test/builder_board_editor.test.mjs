// test/builder_board_editor.test.mjs — the B2 board editor.
//
// Two layers, the builder.test.mjs idiom: (1) PURE buffer round-trip + form
// serialization exercised against entry_form.js directly; (2) INTEGRATION —
// the Board section drives the EXISTING edit_board_entry / remove_board_entry /
// remove_judge / set_board_meta ops through the real render() + POST /builder/op
// dispatch, with a route-keyed fetch mock. The editor is a pure function of a
// module-pinned buffer, so a digest re-render keeps it open (module-state pin).

import { installDom, test, run, assert, assertEqual, assertDeep, makeEvent } from './harness.mjs';

installDom();

// ── the server-derived vocab (mirrors _builder_vocab) ─────────────────
const VOCAB = {
  kinds: ['single_turn', 'multi_turn_scripted', 'multi_turn_emulated', 'synthetic_adversarial', 'synthetic_clean'],
  expectation_kinds: ['expected_text', 'regex', 'json_schema', 'predicate', 'rubric'],
  reads: ['final_output', 'conversation_end'],
  judge_modes: ['inline', 'python'],
  severities: ['info', 'warning', 'critical'],
  drift_kinds: ['off_topic', 'goal_drift', 'hallucination_suspected'],
};

const CONFIG = { chat_enabled: false, agent: { model: '' }, skills: ['zicato-build-board'], vocab: VOCAB };

// ── the shared draft the fetch mock mutates ────────────────────────────
function freshDraft() {
  return {
    scoring: {
      tournament: { structure: 'gauntlet', params: { field_size: 2, replicates: 1 } },
      overfitting: { enabled: true, holdout_fraction: 0.2, min_board_size_for_split: 8, ladder: { enabled: true, budget: 16, noise_scale: 0 } },
      promote_margin: 0, pass_weight: 1,
      namespace_weights: {}, proposer_quality: { best_of_n: 3, critique_enabled: true }, experiment_memory: {},
    },
    board: [
      { id: 'waffles', kind: 'single_turn', budget_s: 180, input: 'make waffles',
        expectation: { kind: 'expected_text', spec: 'waffle' },
        judges: [{ name: 'tone', mode: 'inline', body: 'is it polite?', severity: 'warning' }] },
      { id: 'picky', kind: 'multi_turn_emulated', budget_s: 360,
        user_persona: { goal: 'g', constraints: 'c', stop_when: 's' }, max_turns: 6, tags: ['holdout'] },
    ],
    board_meta: { disable_drift: [], judge_only: false },
    brief: 'be crisper',
    proposer_path: null,
    proposer: { agent_id: 'default', tools: [], skills: [] },
    holdout: { train_ids: ['waffles'], holdout_ids: ['picky'] },
  };
}

let DRAFT = freshDraft();
const OP_CALLS = [];

function envelope(patch) {
  return {
    draft: DRAFT,
    patch: patch || { op: 'noop', changed: {} },
    cost: { board_runs_per_round: 4, breakdown: [{ label: 'duel runs', runs: 4, detail: 'x' }] },
    warnings: [],
    diff: { components: [], changed_components: [], rolls_epoch: false },
  };
}

function jsonRes(obj, ok, status) {
  return {
    ok: ok !== false, status: status || 200,
    headers: { get: () => 'application/json' },
    json: async () => obj, text: async () => JSON.stringify(obj),
  };
}

// entries whose id starts with 'bad' 400 (the inline-error path); everything
// else applies to the shared DRAFT board.
function installFetch() {
  OP_CALLS.length = 0;
  DRAFT = freshDraft();
  globalThis.fetch = async (path, init) => {
    const body = init && init.body ? JSON.parse(init.body) : {};
    if (path === '/builder/config') return jsonRes(CONFIG);
    if (path.startsWith('/builder/draft')) {
      return jsonRes({ session: 'dashboard', draft: DRAFT, cost: envelope().cost, warnings: [], diff: envelope().diff, drafts: [], proposer_dirs: [] });
    }
    if (path === '/builder/op') {
      OP_CALLS.push(body);
      const a = body.args || {};
      let changed = a;
      if (body.op === 'edit_board_entry') {
        const e = a.entry || {};
        if (String(e.id || '').startsWith('bad')) {
          return jsonRes({ error: `BoardEntry ${JSON.stringify(e.id)}: single_turn requires 'input'` }, false, 400);
        }
        const row = entryToRow(e);
        const idx = DRAFT.board.findIndex((x) => x.id === row.id);
        // mirror operations.edit_board_entry's DraftPatch.changed shape.
        changed = { entry_id: row.id, action: idx >= 0 ? 'replaced' : 'added' };
        if (idx >= 0) DRAFT.board[idx] = row; else DRAFT.board.push(row);
      } else if (body.op === 'remove_board_entry') {
        DRAFT.board = DRAFT.board.filter((x) => x.id !== a.entry_id);
      } else if (body.op === 'remove_judge') {
        const e = DRAFT.board.find((x) => x.id === a.entry_id);
        if (e && Array.isArray(e.judges)) e.judges = e.judges.filter((j) => j.name !== a.name);
      } else if (body.op === 'set_board_meta') {
        if (Array.isArray(a.disable_drift)) DRAFT.board_meta.disable_drift = a.disable_drift.slice();
        if (a.judge_only != null) DRAFT.board_meta.judge_only = !!a.judge_only;
      } else if (body.op === 'set_brief') {
        DRAFT.brief = String(a.text || '');
      }
      return jsonRes(envelope({ op: body.op, changed }));
    }
    return jsonRes({});
  };
}

// Mirror entry_to_dict's short-form (budget_s) so a re-read board row looks
// like the server's serialized shape.
function entryToRow(e) {
  const row = { id: e.id, kind: e.kind, budget_s: e.wall_clock_budget_seconds };
  if (e.weight != null && e.weight !== 1) row.weight = e.weight;
  if (Array.isArray(e.tags) && e.tags.length) row.tags = e.tags.slice();
  if (e.expectation) row.expectation = e.expectation;
  if (Array.isArray(e.judges) && e.judges.length) row.judges = e.judges;
  for (const k of ['input', 'turns', 'user_persona', 'max_turns', 'adversarial_agent_spec', 'required_drift_kinds']) {
    if (e[k] != null) row[k] = e[k];
  }
  return row;
}

function installEnv() {
  globalThis.window = globalThis.window || {};
  globalThis.window.addEventListener = globalThis.window.addEventListener || (() => {});
  if (!globalThis.window.localStorage) {
    const kv = new Map();
    globalThis.window.localStorage = { getItem: (k) => (kv.has(k) ? kv.get(k) : null), setItem: (k, v) => kv.set(k, String(v)), removeItem: (k) => kv.delete(k), clear: () => kv.clear() };
  }
  globalThis.HashChangeEvent = globalThis.HashChangeEvent || function HashChangeEvent() {};
}
installEnv();

const ef = await import('../js/builder/entry_form.js');
const view = await import('../js/views/builder.js');

function byClass(root, cls) { return root.querySelectorAll('[class]').filter((n) => n.classList.contains(cls)); }
function firstClass(root, cls) { return byClass(root, cls)[0] || null; }
function byAria(root, aria) { return root.querySelectorAll('[aria-label]').filter((n) => n.getAttribute('aria-label') === aria)[0] || null; }
function tick() { return new Promise((r) => setTimeout(r, 0)); }

// mount the view fresh + navigate to the Board section.
async function mountBoard() {
  view._resetBuilderForTest();
  installFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Board'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  return host;
}

// ── PURE: buffer round-trip per kind (all five) ───────────────────────

const CANON = {
  single_turn: { id: 's1', kind: 'single_turn', wall_clock_budget_seconds: 60, input: 'hi' },
  multi_turn_scripted: { id: 'sc', kind: 'multi_turn_scripted', wall_clock_budget_seconds: 120, turns: [{ user: 'a' }, { user: 'b' }], max_turns: 4 },
  multi_turn_emulated: { id: 'em', kind: 'multi_turn_emulated', wall_clock_budget_seconds: 360, user_persona: { goal: 'g', constraints: 'c', stop_when: 's' }, max_turns: 6 },
  synthetic_adversarial: { id: 'ad', kind: 'synthetic_adversarial', wall_clock_budget_seconds: 90, input: 'x', adversarial_agent_spec: 'pkg.mod:agent', required_drift_kinds: ['off_topic'] },
  synthetic_clean: { id: 'cl', kind: 'synthetic_clean', wall_clock_budget_seconds: 60, input: 'y' },
};

test('entry_form: entryToBuffer→bufferToEntryJson round-trips every kind byte-stably', () => {
  for (const [kind, json] of Object.entries(CANON)) {
    // start from an entry_to_dict-shaped row (short-form budget_s).
    const row = { ...json, budget_s: json.wall_clock_budget_seconds };
    delete row.wall_clock_budget_seconds;
    const buf = ef.entryToBuffer(row);
    const back = ef.bufferToEntryJson(buf);
    assertDeep(back, json, kind + ' round-trip');
  }
});

test('entry_form: a weight≠1 and operator tags survive the round-trip; weight 1 is omitted', () => {
  const buf = ef.entryToBuffer({ id: 'w', kind: 'single_turn', budget_s: 30, input: 'z', weight: 2.5, tags: ['smoke', 'router'] });
  const back = ef.bufferToEntryJson(buf);
  assertEqual(back.weight, 2.5, 'weight preserved');
  assertDeep(back.tags, ['smoke', 'router'], 'tags preserved');
  const plain = ef.bufferToEntryJson(ef.entryToBuffer({ id: 'w2', kind: 'single_turn', budget_s: 30, input: 'z' }));
  assert(!('weight' in plain), 'weight 1 is omitted (matches entry_to_dict)');
});

test('entry_form: newEntryBuffer(kind) seeds the kind discriminants', () => {
  assert(ef.newEntryBuffer('single_turn').input != null, 'single_turn seeds input');
  const sc = ef.newEntryBuffer('multi_turn_scripted');
  assert(Array.isArray(sc.turns) && sc.turns.length === 1, 'scripted seeds one empty turn');
  assert(sc.max_turns > 0, 'scripted seeds max_turns');
  assert(ef.newEntryBuffer('multi_turn_emulated').user_persona != null, 'emulated seeds a persona');
  const ad = ef.newEntryBuffer('synthetic_adversarial');
  assert(ad.input != null && ad.adversarial_agent_spec != null && Array.isArray(ad.required_drift_kinds), 'adversarial seeds its trio');
});

// ── PURE: the editor form (kind switch, rubric, holdout tag) ───────────

function editor(buffer, extra) {
  return ef.entryEditor(buffer, VOCAB, Object.assign({ editing: true, onChange: () => {} }, extra || {}));
}

test('entry_form: switching kind CLEARS inapplicable discriminants, keeps common', () => {
  const buf = ef.newEntryBuffer('multi_turn_scripted');
  buf.id = 'x'; buf.tags = ['keepme']; buf.weight = 2;
  let changed = 0;
  const node = editor(buf, { onChange: () => { changed += 1; } });
  const kindSel = byAria(node, 'Entry kind');
  kindSel.value = 'single_turn';
  kindSel.dispatchEvent(makeEvent('change'));
  assert(changed === 1, 'kind switch requested a re-render');
  assert(buf.turns === undefined, 'the scripted turns discriminant was cleared');
  assert(buf.max_turns === undefined, 'max_turns cleared');
  assert(buf.input != null, 'the single_turn input discriminant was installed');
  assertEqual(buf.id, 'x', 'the id (common) is kept');
  assertDeep(buf.tags, ['keepme'], 'tags (common) kept');
  assertEqual(buf.weight, 2, 'weight (common) kept');
});

test('entry_form: the rubric sub-form serializes to the JSON spec string', () => {
  const buf = ef.newEntryBuffer('single_turn');
  buf.id = 'r'; buf.expectation = { kind: 'rubric', spec: '', reads: 'final_output' };
  const node = editor(buf);
  byAria(node, 'Rubric text').value = 'grade the tone';
  byAria(node, 'Rubric text').dispatchEvent(makeEvent('change'));
  byAria(node, 'Rubric threshold').value = '0.7';
  byAria(node, 'Rubric threshold').dispatchEvent(makeEvent('change'));
  byAria(node, 'Rubric scale low').value = '0';
  byAria(node, 'Rubric scale low').dispatchEvent(makeEvent('change'));
  byAria(node, 'Rubric scale high').value = '5';
  byAria(node, 'Rubric scale high').dispatchEvent(makeEvent('change'));
  const doc = JSON.parse(buf.expectation.spec);
  assertEqual(doc.rubric, 'grade the tone', 'rubric text serialized');
  assertEqual(doc.threshold, 0.7, 'threshold serialized as a number');
  assertDeep(doc.scale, [0, 5], 'scale [lo,hi] serialized');
});

test('entry_form: the JSON-schema spec shows a client parse hint (non-blocking)', () => {
  const buf = ef.newEntryBuffer('single_turn');
  buf.id = 'j'; buf.expectation = { kind: 'json_schema', spec: '{bad', reads: 'final_output' };
  const node = editor(buf);
  const hint = firstClass(node, 'dn-bld-ef-jsonhint');
  assert(hint && /not valid JSON/.test(hint.textContent), 'a bad schema shows an invalid-JSON hint');
  const spec = byAria(node, 'Expectation spec');
  spec.value = '{"type":"object"}';
  spec.dispatchEvent(makeEvent('input'));
  assert(/parses as JSON/.test(hint.textContent), 'a valid schema flips the hint to OK');
});

// `context` round-trips through the buffer. Without a control for it an operator
// could neither read an authored context nor write one: invisible, though never
// dropped.
test('entry_form: an existing context is VISIBLE and round-trips through the form', () => {
  const buf = ef.entryToBuffer({ id: 'c', kind: 'single_turn', budget_s: 30, input: 'z',
    context: { locale: 'en-GB', tier: 2 } });
  const node = editor(buf);
  const area = byAria(node, 'Entry context');
  assert(area != null, 'the Context control exists');
  assert(/"locale": "en-GB"/.test(area.textContent), 'the authored context is shown, not hidden');
  const hint = firstClass(node, 'dn-bld-ef-ctxhint');
  assert(hint && /2 keys/.test(hint.textContent), 'the hint counts the keys');
  assertDeep(ef.bufferToEntryJson(buf).context, { locale: 'en-GB', tier: 2 }, 'and serializes back unchanged');
});

test('entry_form: editing the context commits the parsed object; blank clears it', () => {
  const buf = ef.newEntryBuffer('single_turn');
  buf.id = 'c2';
  const node = editor(buf);
  const area = byAria(node, 'Entry context');
  area.value = '{"seed": 7}';
  area.dispatchEvent(makeEvent('input'));
  assertDeep(buf.context, { seed: 7 }, 'a parsed object is committed to the buffer');
  assertDeep(ef.bufferToEntryJson(buf).context, { seed: 7 }, 'and reaches the op payload');
  area.value = '   ';
  area.dispatchEvent(makeEvent('input'));
  assertDeep(buf.context, {}, 'blank is a REAL value — it clears the context');
  assert(!('context' in ef.bufferToEntryJson(buf)), 'an empty context is omitted from the op payload');
});

test('entry_form: unparseable context keeps the last good value and SAYS SO (no silent loss)', () => {
  const buf = ef.entryToBuffer({ id: 'c3', kind: 'single_turn', budget_s: 30, input: 'z',
    context: { locale: 'en-GB' } });
  const node = editor(buf);
  const area = byAria(node, 'Entry context');
  const hint = firstClass(node, 'dn-bld-ef-ctxhint');
  area.value = '{"locale": ';   // mid-keystroke
  area.dispatchEvent(makeEvent('input'));
  assertDeep(buf.context, { locale: 'en-GB' }, 'a half-typed value never destroys the live one');
  assert(/not valid JSON/.test(hint.textContent), 'the hint says the box does not parse');
  assert(/last valid value is kept/.test(hint.textContent), 'and what is being kept instead');
  area.value = '["not", "an", "object"]';
  area.dispatchEvent(makeEvent('input'));
  assert(/must be a JSON object/.test(hint.textContent), 'a non-object is named as such');
  assertDeep(buf.context, { locale: 'en-GB' }, 'and still commits nothing');
});

test('entry_form: conversation_end reads is DISABLED for single_turn', () => {
  const buf = ef.newEntryBuffer('single_turn');
  buf.id = 'k'; buf.expectation = { kind: 'expected_text', spec: 'x', reads: 'final_output' };
  const node = editor(buf);
  const readsSel = byAria(node, 'Expectation reads');
  const ce = readsSel.querySelectorAll('[value]').find((o) => o.getAttribute('value') === 'conversation_end');
  assert(ce && ce.hasAttribute('disabled'), 'conversation_end is disabled for a single_turn entry');
});

test('entry_form: the holdout tag is stripped on load and re-applied on save from the toggle', () => {
  // a held-out entry: its tags carry HOLDOUT_TAG on disk.
  const buf = ef.entryToBuffer({ id: 'h', kind: 'single_turn', budget_s: 30, input: 'z', tags: ['holdout', 'smoke'] }, { heldOut: true });
  assert(!buf.tags.includes(ef.HOLDOUT_TAG), 'the holdout tag is stripped from the tags input');
  assertDeep(buf.tags, ['smoke'], 'the operator tags remain');
  const back = ef.bufferToEntryJson(buf);
  assert(back.tags.includes('holdout'), 'save re-applies the holdout tag from the toggle state');
  // an entry moved to train drops the tag on save.
  buf.heldOut = false;
  assert(!ef.bufferToEntryJson(buf).tags.includes('holdout'), 'clearing the toggle drops the holdout tag');
});

// ── INTEGRATION: the Board section drives the ops ─────────────────────

test('board editor: a row click opens the inline accordion; Save posts edit_board_entry with the whole entry', async () => {
  const host = await mountBoard();
  const row = byClass(host, 'dn-bld-boardrow-main').find((r) => r.textContent.includes('waffles'));
  assert(row, 'the waffles row renders');
  row.dispatchEvent(makeEvent('click'));
  await tick();
  const form = firstClass(host, 'dn-bld-entryform');
  assert(form, 'the inline editor opened');
  // the id is LOCKED when editing.
  const idInput = byAria(host, 'Entry id');
  assert(idInput.hasAttribute('readonly'), 'the id is locked while editing an existing entry');
  // edit the budget, then Save → one edit_board_entry with the whole buffer.
  const budget = byAria(host, 'Entry budget seconds');
  budget.value = '240';
  budget.dispatchEvent(makeEvent('change'));
  await tick();
  byAria(host, 'Save entry').dispatchEvent(makeEvent('click'));
  await tick();
  const call = OP_CALLS.find((c) => c.op === 'edit_board_entry');
  assert(call, 'Save posted edit_board_entry');
  assertEqual(call.args.entry.id, 'waffles', 'the whole-entry payload carries the id');
  assertEqual(call.args.entry.wall_clock_budget_seconds, 240, 'the edited budget rides the whole-entry replace');
  assertEqual(call.args.entry.kind, 'single_turn', 'the kind rides along (whole-entry round-trip)');
  // the editor closed on success.
  assert(!firstClass(host, 'dn-bld-entryform'), 'the editor closes after a successful save');
});

test('board editor: Add-entry seeds a create buffer; the id is editable and Save creates it', async () => {
  const host = await mountBoard();
  const kindSel = byAria(host, 'New entry kind');
  kindSel.value = 'single_turn';
  byAria(host, 'Add entry').dispatchEvent(makeEvent('click'));
  await tick();
  const idInput = byAria(host, 'Entry id');
  assert(idInput && !idInput.hasAttribute('readonly'), 'the id is editable in create mode');
  idInput.value = 'newbie';
  idInput.dispatchEvent(makeEvent('input'));
  byAria(host, 'Entry input').value = 'do a thing';
  byAria(host, 'Entry input').dispatchEvent(makeEvent('change'));
  await tick();
  byAria(host, 'Save entry').dispatchEvent(makeEvent('click'));
  await tick();
  const call = OP_CALLS.find((c) => c.op === 'edit_board_entry' && c.args.entry.id === 'newbie');
  assert(call, 'the create posted edit_board_entry for the new id');
  assertEqual(call.args.entry.input, 'do a thing', 'the input rode the create');
});

test('board editor: Delete is a two-click confirm that posts remove_board_entry', async () => {
  const host = await mountBoard();
  byClass(host, 'dn-bld-boardrow-main').find((r) => r.textContent.includes('waffles')).dispatchEvent(makeEvent('click'));
  await tick();
  const del = byAria(host, 'Delete entry');
  del.dispatchEvent(makeEvent('click')); // arms
  await tick();
  assert(!OP_CALLS.find((c) => c.op === 'remove_board_entry'), 'the first click only arms — no delete yet');
  const armed = byAria(host, 'Delete entry');
  assert(/Confirm/.test(armed.textContent), 'the button shows the confirm gesture');
  armed.dispatchEvent(makeEvent('click')); // confirms
  await tick();
  const call = OP_CALLS.find((c) => c.op === 'remove_board_entry');
  assert(call && call.args.entry_id === 'waffles', 'the confirmed delete posts remove_board_entry');
});

test('board editor: a per-judge badge × posts remove_judge directly', async () => {
  const host = await mountBoard();
  const x = byAria(host, 'Remove judge tone from waffles');
  assert(x, 'the judge badge renders a × control');
  x.dispatchEvent(makeEvent('click', { target: x }));
  await tick();
  const call = OP_CALLS.find((c) => c.op === 'remove_judge');
  assert(call && call.args.entry_id === 'waffles' && call.args.name === 'tone', 'the × posted remove_judge for that judge');
  // the click did NOT also open the row editor (stopPropagation).
  assert(!firstClass(host, 'dn-bld-entryform'), 'the badge × did not open the accordion');
});

test('board editor: a server 400 renders verbatim in the inline strip and the editor stays open', async () => {
  const host = await mountBoard();
  byAria(host, 'Add entry').dispatchEvent(makeEvent('click'));
  await tick();
  const idInput = byAria(host, 'Entry id');
  idInput.value = 'badentry';
  idInput.dispatchEvent(makeEvent('input'));
  await tick();
  byAria(host, 'Save entry').dispatchEvent(makeEvent('click'));
  await tick();
  const strip = firstClass(host, 'dn-bld-ef-error');
  assert(strip, 'the inline error strip rendered');
  assert(/single_turn requires/.test(strip.textContent), 'the server ValueError text renders verbatim');
  assert(firstClass(host, 'dn-bld-entryform'), 'the editor stays open after a server error');
  // it did NOT route to the global flash.
  assert(!firstClass(host, 'dn-bld-flash'), 'the failure did not hit the global flash');
});

test('board editor: the board_meta panel drives set_board_meta (drift + judge_only)', async () => {
  const host = await mountBoard();
  const drift = byAria(host, 'Disable drift off_topic');
  assert(drift, 'a drift-kind checkbox renders from the vocab');
  drift.checked = true;
  drift.dispatchEvent(makeEvent('change'));
  await tick();
  let call = OP_CALLS.find((c) => c.op === 'set_board_meta' && Array.isArray(c.args.disable_drift));
  assert(call && call.args.disable_drift.includes('off_topic'), 'toggling a drift kind posts the whole disable_drift set');
  const jo = byAria(host, 'Board judge-only');
  jo.checked = true;
  jo.dispatchEvent(makeEvent('change'));
  await tick();
  call = OP_CALLS.find((c) => c.op === 'set_board_meta' && c.args.judge_only === true);
  assert(call, 'the judge-only toggle posts set_board_meta {judge_only:true}');
});

test('board editor: paste-JSONL import posts one op per line and routes a board_meta header to set_board_meta', async () => {
  const host = await mountBoard();
  const area = byAria(host, 'Paste board JSONL');
  const lines = [
    '{"board_meta": true, "disable_drift": ["off_topic"], "judge_only": true}',
    '{"id": "imp1", "kind": "single_turn", "wall_clock_budget_seconds": 30, "input": "a"}',
    '{"id": "imp2", "kind": "synthetic_clean", "wall_clock_budget_seconds": 30, "input": "b"}',
    'not json',
  ].join('\n');
  area.value = lines;
  area.dispatchEvent(makeEvent('input'));
  byAria(host, 'Import board JSONL').dispatchEvent(makeEvent('click'));
  await tick();
  const meta = OP_CALLS.find((c) => c.op === 'set_board_meta' && Array.isArray(c.args.disable_drift));
  assert(meta && meta.args.disable_drift.includes('off_topic') && meta.args.judge_only === true, 'the header line routed to set_board_meta');
  const edits = OP_CALLS.filter((c) => c.op === 'edit_board_entry' && ['imp1', 'imp2'].includes(c.args.entry.id));
  assertEqual(edits.length, 2, 'each entry line posted one edit_board_entry');
  // the per-line report surfaces the bad line inline, and the good lines are ok.
  const report = firstClass(host, 'dn-bld-import-report');
  assert(report && /invalid JSON/.test(report.textContent), 'the malformed line reports inline');
  assert(report.textContent.includes('line 2: ok'), 'the good lines report ok');
});

test('board editor: the open editor survives a digest re-render (module-state pin)', async () => {
  const host = await mountBoard();
  byClass(host, 'dn-bld-boardrow-main').find((r) => r.textContent.includes('waffles')).dispatchEvent(makeEvent('click'));
  await tick();
  // type a NEW budget into the buffer (value edit — no re-render).
  const budget = byAria(host, 'Entry budget seconds');
  budget.value = '300';
  budget.dispatchEvent(makeEvent('change'));
  // force a full re-render by re-mounting against the same pinned state.
  await view.render(host);
  const form = firstClass(host, 'dn-bld-entryform');
  assert(form, 'the editor is STILL open after a re-render (module-state pin)');
  const budget2 = byAria(host, 'Entry budget seconds');
  assert(budget2, 'the budget control re-rendered');
  // the pinned buffer kept the typed value: Save posts 300 rather than the 180 it
  // opened with.
  byAria(host, 'Save entry').dispatchEvent(makeEvent('click'));
  await tick();
  const call = OP_CALLS.find((c) => c.op === 'edit_board_entry');
  assertEqual(call.args.entry.wall_clock_budget_seconds, 300, 'the buffer kept the pre-re-render edit');
});

test('board editor: the empty-state no longer references the phantom "board builder"', async () => {
  const host = await mountBoard();
  DRAFT.board = [];
  DRAFT.holdout = { train_ids: [], holdout_ids: [] };
  await view.render(host);
  const empty = firstClass(host, 'dn-empty');
  assert(empty, 'an empty board shows the empty-state');
  assert(!/board builder/.test(empty.textContent), 'the phantom "board builder" copy is gone');
  assert(/Add entry/.test(empty.textContent), 'the empty-state points at the real Add-entry control');
});

test('board editor: no-op identity guardrail — re-selecting the active section does NOT rebuild it', async () => {
  const host = await mountBoard();
  const center = firstClass(host, 'dn-bld-center');
  const before = center.firstChild;
  const writes = center.innerHTMLWriteCount();
  // re-click the ALREADY-active Board rail item → renderCenter runs on the SAME
  // host with an identical digest → gatedSwap must no-op (no DOM rebuild).
  byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Board')).dispatchEvent(makeEvent('click'));
  await tick();
  assert(center.firstChild === before, 'the center section was not rebuilt (firstChild identity held)');
  assertEqual(center.innerHTMLWriteCount(), writes, 'no innerHTML writes on a no-op re-render');
});

test('board editor: the proposer brief editor posts set_brief with the char count live', async () => {
  view._resetBuilderForTest();
  installFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Proposer')).dispatchEvent(makeEvent('click'));
  await tick();
  const area = byAria(host, 'Proposer brief');
  assert(area, 'the brief editor renders in the proposer section');
  area.value = 'be MUCH crisper';
  area.dispatchEvent(makeEvent('input'));
  const count = firstClass(host, 'dn-bld-brief-count');
  assert(/15 chars/.test(count.textContent), 'the char count updates live without a re-render');
  byAria(host, 'Save brief').dispatchEvent(makeEvent('click'));
  await tick();
  const call = OP_CALLS.find((c) => c.op === 'set_brief');
  assert(call && call.args.text === 'be MUCH crisper', 'Save brief posts set_brief with the text');
});

// ── F7: switching to single_turn clamps a stale conversation_end reads ──

test('entry_form F7: switching to single_turn clamps conversation_end reads to final_output', () => {
  const buf = ef.newEntryBuffer('multi_turn_emulated');
  buf.id = 'c'; buf.expectation = { kind: 'expected_text', spec: 'x', reads: 'conversation_end' };
  const node = editor(buf, {});
  const kindSel = byAria(node, 'Entry kind');
  kindSel.value = 'single_turn';
  kindSel.dispatchEvent(makeEvent('change'));
  assertEqual(buf.kind, 'single_turn', 'the kind switched to single_turn');
  assertEqual(buf.expectation.reads, 'final_output',
    'the conversation_end reads is clamped to final_output (no guaranteed-reject Save)');
});

// ── F1: a hash-held entry saved with NO tag must not gain a holdout tag ──

test('board editor F1: saving a hash-held entry with no tag never stamps a holdout tag', async () => {
  const host = await mountBoard();
  // a HASH-derived holdout entry: it sits in holdout_ids but carries NO explicit
  // holdout tag (the rotating, fraction-based slice — not a per-entry decision).
  DRAFT.board.push({ id: 'hashheld', kind: 'single_turn', budget_s: 60, input: 'q' });
  DRAFT.holdout = { train_ids: ['waffles'], holdout_ids: ['picky', 'hashheld'] };
  await view.render(host);
  const row = byClass(host, 'dn-bld-boardrow-main').find((r) => r.textContent.includes('hashheld'));
  assert(row, 'the hash-held row renders');
  row.dispatchEvent(makeEvent('click'));
  await tick();
  assert(firstClass(host, 'dn-bld-entryform'), 'the editor opened for the hash-held entry');
  // save with NO edits — the toggle was never touched.
  byAria(host, 'Save entry').dispatchEvent(makeEvent('click'));
  await tick();
  const call = OP_CALLS.find((c) => c.op === 'edit_board_entry' && c.args.entry.id === 'hashheld');
  assert(call, 'Save posted edit_board_entry for the hash-held entry');
  const tags = call.args.entry.tags || [];
  assert(!tags.includes('holdout'),
    'the hash-held entry saves untagged — the rotating holdout is not collapsed onto it');
});

// ── F2: a draft-identity op (switch) closes the open editor ──

test('board editor F2: a slot switch closes the open editor (no stale buffer into a new draft)', async () => {
  view._resetBuilderForTest();
  OP_CALLS.length = 0;
  DRAFT = freshDraft();
  globalThis.window.localStorage.clear();
  globalThis.fetch = async (path, init) => {
    const body = init && init.body ? JSON.parse(init.body) : {};
    if (path === '/builder/config') return jsonRes(CONFIG);
    if (path.startsWith('/builder/draft')) {
      return jsonRes({ session: 'dashboard', draft: DRAFT, cost: envelope().cost, warnings: [], diff: envelope().diff, drafts: ['variant-a'], proposer_dirs: [] });
    }
    if (path === '/builder/op') {
      OP_CALLS.push(body);
      return jsonRes(envelope({ op: body.op, changed: body.args || {} }));
    }
    return jsonRes({});
  };
  const host = globalThis.document.createElement('div');
  await view.render(host);
  byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Board')).dispatchEvent(makeEvent('click'));
  await tick();
  byClass(host, 'dn-bld-boardrow-main').find((r) => r.textContent.includes('waffles')).dispatchEvent(makeEvent('click'));
  await tick();
  assert(firstClass(host, 'dn-bld-entryform'), 'the editor opened');
  const sel = byAria(host, 'Draft slot');
  assert(sel, 'the slot picker renders');
  sel.value = 'variant-a';
  sel.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'switch'), 'the slot pick posted the switch op');
  assert(!firstClass(host, 'dn-bld-entryform'), 'the editor CLOSED on the draft-identity switch');
});

// ── F6: a create-mode Save that replaces an existing id surfaces a notice ──

test('board editor F6: a create Save onto an existing id shows a visible replaced notice', async () => {
  const host = await mountBoard();
  const kindSel = byAria(host, 'New entry kind');
  kindSel.value = 'single_turn';
  byAria(host, 'Add entry').dispatchEvent(makeEvent('click'));
  await tick();
  const idInput = byAria(host, 'Entry id');
  idInput.value = 'waffles'; // an id ALREADY on the board
  idInput.dispatchEvent(makeEvent('input'));
  byAria(host, 'Entry input').value = 'clobber';
  byAria(host, 'Entry input').dispatchEvent(makeEvent('change'));
  await tick();
  byAria(host, 'Save entry').dispatchEvent(makeEvent('click'));
  await tick();
  const notice = firstClass(host, 'dn-bld-replaced-note');
  assert(notice, 'a replaced notice rendered');
  assert(/replaced existing entry waffles/.test(notice.textContent), 'the notice names the replaced id');
});

await run();

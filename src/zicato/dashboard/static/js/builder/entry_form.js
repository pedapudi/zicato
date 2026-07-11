// js/builder/entry_form.js — the board-entry editor (B2), a pure DOM-builder.
//
// The flagship board-authoring surface: a per-kind form that mirrors
// zicato.core.board.BoardEntry.validate field-for-field, rendered inline as an
// accordion in the Board section. It owns NO fetching and NO module state — it
// is a pure function of a `buffer` (a plain JSON object validate_board_entry
// accepts) + the server-derived `vocab` + a handler bag. The caller
// (views/builder.js) pins the buffer in module state so the editor survives a
// digest re-render, and posts the whole buffer through the EXISTING
// `edit_board_entry` op on Save (invariant L1 — no new mutation path).
//
// There is NO client validation twin (invariant L4): the form only gates Save
// on the PRESENCE of an id; every structural objection is the server's
// field-precise ValueError, rendered verbatim in the inline error strip.

import { el } from '../core/dom.js';

// The one explicit holdout tag — owned by the train/holdout toggle, NEVER the
// tags input. Stripped when a buffer loads, re-applied on serialize, so the two
// controls can never fight over it. Mirrors zicato.board.split.HOLDOUT_TAG.
export const HOLDOUT_TAG = 'holdout';

const DEFAULT_VOCAB = {
  kinds: ['single_turn', 'multi_turn_scripted', 'multi_turn_emulated', 'synthetic_adversarial', 'synthetic_clean'],
  expectation_kinds: ['expected_text', 'regex', 'json_schema', 'predicate', 'rubric'],
  reads: ['final_output', 'conversation_end'],
  judge_modes: ['inline', 'python'],
  severities: ['info', 'warning', 'critical'],
  drift_kinds: [],
};

// ── buffer ⇄ JSON ─────────────────────────────────────────────────────
//
// The buffer IS the JSON validate_board_entry accepts, plus a private
// `heldOut` boolean the serializer folds back into the tags. `entryToBuffer`
// reads a `board` row (entry_to_dict shape — note it writes `budget_s`, the
// short form); `bufferToEntryJson` emits the canonical
// `wall_clock_budget_seconds` the op parses.

function numOr(v, def) {
  const n = Number(v);
  return isFinite(n) ? n : def;
}

// Install the discriminant fields for `kind`, PRESERVING any still-applicable
// value (single↔adversarial keep `input`) and dropping the inapplicable ones —
// the kind-switch "clears inapplicable discriminants, keeps common" rule.
function setKind(buf, kind) {
  const keep = {
    input: buf.input, turns: buf.turns, user_persona: buf.user_persona,
    max_turns: buf.max_turns, adversarial_agent_spec: buf.adversarial_agent_spec,
    required_drift_kinds: buf.required_drift_kinds,
  };
  delete buf.input; delete buf.turns; delete buf.user_persona;
  delete buf.max_turns; delete buf.adversarial_agent_spec; delete buf.required_drift_kinds;
  buf.kind = kind;
  if (kind === 'single_turn' || kind === 'synthetic_clean') {
    buf.input = keep.input != null ? keep.input : '';
  } else if (kind === 'multi_turn_scripted') {
    buf.turns = (Array.isArray(keep.turns) && keep.turns.length) ? keep.turns : [{ user: '' }];
    buf.max_turns = keep.max_turns != null ? keep.max_turns : 4;
  } else if (kind === 'multi_turn_emulated') {
    buf.user_persona = keep.user_persona || { goal: '', constraints: '', stop_when: '' };
    buf.max_turns = keep.max_turns != null ? keep.max_turns : 6;
  } else if (kind === 'synthetic_adversarial') {
    buf.input = keep.input != null ? keep.input : '';
    buf.adversarial_agent_spec = keep.adversarial_agent_spec != null ? keep.adversarial_agent_spec : '';
    buf.required_drift_kinds = Array.isArray(keep.required_drift_kinds) ? keep.required_drift_kinds : [];
  }
  // A single_turn entry has no conversation to read at its end — validate rejects
  // `reads:'conversation_end'` for it. Clamp a stale reads carried over from a
  // multi-turn buffer so the kind switch never strands the operator at a
  // guaranteed-reject Save (the reads select disables the option, but the buffer
  // value must be corrected too).
  if (kind === 'single_turn' && buf.expectation && buf.expectation.reads === 'conversation_end') {
    buf.expectation.reads = 'final_output';
  }
}

export function newEntryBuffer(kind) {
  const buf = {
    id: '', kind: kind || 'single_turn', wall_clock_budget_seconds: 180, weight: 1,
    tags: [], context: {}, heldOut: false, expectation: null, judges: [],
  };
  setKind(buf, buf.kind);
  return buf;
}

export function entryToBuffer(entry, opts) {
  const e = entry || {};
  const o = opts || {};
  const rawTags = Array.isArray(e.tags) ? e.tags : [];
  const heldOut = o.heldOut != null ? !!o.heldOut : rawTags.includes(HOLDOUT_TAG);
  const buf = {
    id: e.id != null ? e.id : (e.entry_id || ''),
    kind: e.kind || 'single_turn',
    wall_clock_budget_seconds: e.wall_clock_budget_seconds != null
      ? e.wall_clock_budget_seconds : (e.budget_s != null ? e.budget_s : 180),
    weight: e.weight != null ? e.weight : 1,
    tags: rawTags.filter((t) => t !== HOLDOUT_TAG),
    context: e.context && typeof e.context === 'object' ? { ...e.context } : {},
    heldOut,
    expectation: e.expectation && e.expectation.kind ? {
      kind: e.expectation.kind,
      spec: e.expectation.spec != null ? e.expectation.spec : '',
      reads: e.expectation.reads || 'final_output',
    } : null,
    judges: Array.isArray(e.judges) ? e.judges.map((j) => ({
      name: j.name || '', mode: j.mode || 'inline',
      body: j.body != null ? j.body : '', severity: j.severity || 'warning',
    })) : [],
  };
  if (e.input != null) buf.input = e.input;
  if (Array.isArray(e.turns)) buf.turns = e.turns.map((t) => ({ user: t.user != null ? t.user : '' }));
  if (e.user_persona) {
    buf.user_persona = {
      goal: e.user_persona.goal || '', constraints: e.user_persona.constraints || '',
      stop_when: e.user_persona.stop_when || '',
    };
  }
  if (e.max_turns != null) buf.max_turns = e.max_turns;
  if (e.adversarial_agent_spec != null) buf.adversarial_agent_spec = e.adversarial_agent_spec;
  if (Array.isArray(e.required_drift_kinds)) buf.required_drift_kinds = e.required_drift_kinds.slice();
  setKind(buf, buf.kind); // fill any missing discriminant defaults for the kind
  return buf;
}

export function bufferToEntryJson(buffer) {
  const b = buffer || {};
  const out = {
    id: b.id != null ? String(b.id) : '',
    kind: b.kind,
    wall_clock_budget_seconds: numOr(b.wall_clock_budget_seconds, 0),
  };
  if (b.weight != null && Number(b.weight) !== 1) out.weight = Number(b.weight);
  // Re-apply the holdout tag from the toggle state — the tags input never
  // carries it, so the two controls stay disjoint.
  const tags = (Array.isArray(b.tags) ? b.tags : []).map((t) => String(t).trim())
    .filter((t) => t && t !== HOLDOUT_TAG);
  if (b.heldOut) tags.push(HOLDOUT_TAG);
  if (tags.length) out.tags = tags;
  if (b.context && Object.keys(b.context).length) out.context = { ...b.context };
  if (b.expectation && b.expectation.kind) {
    const exp = { kind: b.expectation.kind, spec: b.expectation.spec != null ? b.expectation.spec : '' };
    if (b.expectation.reads && b.expectation.reads !== 'final_output') exp.reads = b.expectation.reads;
    out.expectation = exp;
  }
  if (Array.isArray(b.judges) && b.judges.length) {
    out.judges = b.judges.map((j) => ({
      name: j.name || '', mode: j.mode || 'inline',
      body: j.body != null ? j.body : '', severity: j.severity || 'warning',
    }));
  }
  const k = b.kind;
  if (k === 'single_turn' || k === 'synthetic_clean') {
    out.input = b.input != null ? b.input : '';
  } else if (k === 'multi_turn_scripted') {
    out.turns = (Array.isArray(b.turns) ? b.turns : []).map((t) => ({ user: t.user != null ? t.user : '' }));
    out.max_turns = numOr(b.max_turns, 0);
  } else if (k === 'multi_turn_emulated') {
    const p = b.user_persona || {};
    out.user_persona = { goal: p.goal || '', constraints: p.constraints || '', stop_when: p.stop_when || '' };
    out.max_turns = numOr(b.max_turns, 0);
  } else if (k === 'synthetic_adversarial') {
    out.input = b.input != null ? b.input : '';
    out.adversarial_agent_spec = b.adversarial_agent_spec != null ? b.adversarial_agent_spec : '';
    out.required_drift_kinds = Array.isArray(b.required_drift_kinds) ? b.required_drift_kinds.slice() : [];
  }
  return out;
}

// ── control primitives ─────────────────────────────────────────────────

// Read a control's committed value (browser sets `.value`; the mock DOM leaves
// it undefined until a test assigns it, falling back to the initial attribute /
// textContent). One reader for inputs, selects, and textareas.
function readVal(node) {
  if (node.value != null) return node.value;
  const attr = node.getAttribute ? node.getAttribute('value') : null;
  if (attr != null) return attr;
  return node.textContent != null ? node.textContent : '';
}

function textInput(value, aria, onCommit, extraClass) {
  const input = el('input', {
    class: 'dn-bld-text' + (extraClass ? ' ' + extraClass : ''), type: 'text',
    value: value != null ? String(value) : '', 'aria-label': aria,
  });
  input.addEventListener('input', () => onCommit(readVal(input)));
  input.addEventListener('change', () => onCommit(readVal(input)));
  return input;
}

function numberInput(value, aria, attrs, onCommit) {
  const input = el('input', Object.assign({
    class: 'dn-bld-num', type: 'number', value: value != null ? String(value) : '', 'aria-label': aria,
  }, attrs || {}));
  const commit = () => {
    const n = Number(readVal(input));
    if (isFinite(n)) onCommit(n);
  };
  input.addEventListener('input', commit);
  input.addEventListener('change', commit);
  return input;
}

function areaInput(value, aria, onCommit, extraClass) {
  const area = el('textarea', {
    class: 'dn-bld-area' + (extraClass ? ' ' + extraClass : ''), 'aria-label': aria,
    rows: '3', text: value != null ? String(value) : '',
  });
  area.addEventListener('input', () => onCommit(readVal(area)));
  area.addEventListener('change', () => onCommit(readVal(area)));
  return area;
}

// A closed select over `options`; `disabledValues` renders (but disables) a
// member — the conversation_end-for-single_turn case.
function selectInput(value, aria, options, onCommit, disabledValues) {
  const disabled = new Set(disabledValues || []);
  const sel = el('select', { class: 'dn-bld-select', 'aria-label': aria },
    options.map((opt) => {
      const o = typeof opt === 'string' ? { value: opt, label: opt } : opt;
      const attrs = { value: o.value, text: o.label };
      if (disabled.has(o.value)) attrs.disabled = 'disabled';
      return el('option', attrs);
    }));
  const cur = value != null ? String(value) : '';
  sel.value = cur;
  sel.setAttribute('value', cur);
  sel.addEventListener('change', () => onCommit(readVal(sel)));
  return sel;
}

function checkbox(checked, aria, caption, onToggle) {
  const box = el('input', { class: 'dn-bld-check', type: 'checkbox', 'aria-label': aria });
  if (checked) box.setAttribute('checked', 'checked');
  box.addEventListener('change', () => {
    const on = box.checked != null ? box.checked : (box.getAttribute('checked') != null);
    onToggle(!!on);
  });
  return el('label', { class: 'dn-bld-checkwrap' }, [box, el('span', { text: caption })]);
}

function fieldRow(label, control, hint) {
  return el('div', { class: 'dn-bld-ef-row' }, [
    el('label', { class: 'dn-bld-ef-label', text: label }),
    el('div', { class: 'dn-bld-ef-control' }, [control, hint].filter(Boolean)),
  ]);
}

// ── the editor ─────────────────────────────────────────────────────────
//
// entryEditor(buffer, vocab, handlers) → the accordion body node. VALUE edits
// mutate `buffer` in place WITHOUT a re-render (focus is preserved); STRUCTURAL
// edits (kind switch, add/remove a row, toggle a sub-form) mutate + call
// `handlers.onChange` so the caller re-renders from the pinned buffer.
//
// handlers: { onChange, onSave, onCancel, onDelete, onDuplicate,
//             editing, deleteArmed, error }
export function entryEditor(buffer, vocab, handlers) {
  const b = buffer;
  const V = vocab && vocab.kinds ? vocab : DEFAULT_VOCAB;
  const h = handlers || {};
  const editing = !!h.editing;
  const onChange = typeof h.onChange === 'function' ? h.onChange : () => {};

  const wrap = el('div', { class: 'dn-bld-entryform', role: 'group', 'aria-label': 'Board entry editor' });
  // Forward reference so the id input can live-toggle the presence-gated Save
  // (no re-render → focus is never yanked mid-type).
  let saveBtn = null;
  const syncSaveDisabled = () => {
    if (!saveBtn) return;
    if (String(b.id || '').trim()) saveBtn.removeAttribute('disabled');
    else saveBtn.setAttribute('disabled', 'disabled');
  };

  // — the verbatim server-error strip (L4: render, never block) —
  if (h.error) {
    wrap.appendChild(el('div', { class: 'dn-bld-ef-error', role: 'alert' }, [
      el('span', { class: 'dn-bld-ef-error-glyph', 'aria-hidden': 'true', text: '⛔' }),
      el('span', { class: 'dn-bld-ef-error-msg', text: String(h.error) }),
    ]));
  }

  // — COMMON fields —
  const idInput = el('input', {
    class: 'dn-bld-text dn-bld-ef-id', type: 'text', 'aria-label': 'Entry id',
    value: b.id != null ? String(b.id) : '',
  });
  if (editing) {
    idInput.setAttribute('readonly', 'readonly');
    idInput.setAttribute('aria-readonly', 'true');
  } else {
    // VALUE edit (no re-render, focus kept) — live-toggle the presence-gated
    // Save button so it enables the moment an id is typed.
    const commitId = () => { b.id = readVal(idInput); syncSaveDisabled(); };
    idInput.addEventListener('input', commitId);
    idInput.addEventListener('change', commitId);
  }
  const idHint = editing
    ? el('span', { class: 'dn-bld-ef-hint', text: 'locked — edits replace by id; use Duplicate to fork under a new id' })
    : null;
  wrap.appendChild(fieldRow('Entry id', idInput, idHint));

  wrap.appendChild(fieldRow('Kind', selectInput(b.kind, 'Entry kind', V.kinds, (v) => {
    setKind(b, v);
    onChange();
  })));

  wrap.appendChild(fieldRow('Budget (s)', numberInput(b.wall_clock_budget_seconds, 'Entry budget seconds',
    { min: '1', step: '1' }, (n) => { b.wall_clock_budget_seconds = Math.round(n); })));

  wrap.appendChild(fieldRow('Weight', numberInput(b.weight, 'Entry weight',
    { min: '0', step: '0.1' }, (n) => { b.weight = n; })));

  wrap.appendChild(fieldRow('Tags', textInput((b.tags || []).join(', '), 'Entry tags', (v) => {
    b.tags = String(v).split(',').map((t) => t.trim()).filter(Boolean);
  }, 'dn-bld-ef-tags'), el('span', { class: 'dn-bld-ef-hint', text: 'comma-separated; the holdout tag is owned by the train/holdout toggle' })));

  // — PER-KIND discriminant fields —
  wrap.appendChild(kindFields(b, V, onChange));

  // — EXPECTATION sub-form —
  wrap.appendChild(expectationForm(b, V, onChange));

  // — JUDGES list editor —
  wrap.appendChild(judgesForm(b, V, onChange));

  // — actions —
  const actions = el('div', { class: 'dn-bld-ef-actions' });
  saveBtn = el('button', { class: 'dn-bld-btn dn-bld-btn-save', type: 'button', text: editing ? 'Save entry' : 'Add entry', 'aria-label': 'Save entry' });
  if (!String(b.id || '').trim()) saveBtn.setAttribute('disabled', 'disabled'); // presence-only gate
  saveBtn.addEventListener('click', () => { if (typeof h.onSave === 'function') h.onSave(); });
  actions.appendChild(saveBtn);

  const cancelBtn = el('button', { class: 'dn-bld-btn dn-bld-btn-cancel', type: 'button', text: 'Cancel', 'aria-label': 'Cancel entry edit' });
  cancelBtn.addEventListener('click', () => { if (typeof h.onCancel === 'function') h.onCancel(); });
  actions.appendChild(cancelBtn);

  if (editing) {
    const dupBtn = el('button', { class: 'dn-bld-btn dn-bld-btn-dup', type: 'button', text: 'Duplicate', 'aria-label': 'Duplicate entry' });
    dupBtn.addEventListener('click', () => { if (typeof h.onDuplicate === 'function') h.onDuplicate(); });
    actions.appendChild(dupBtn);

    const delBtn = el('button', {
      class: 'dn-bld-btn dn-bld-btn-del' + (h.deleteArmed ? ' dn-bld-btn-confirm' : ''),
      type: 'button', 'aria-label': 'Delete entry',
      text: h.deleteArmed ? 'Confirm delete' : 'Delete',
    });
    delBtn.addEventListener('click', () => { if (typeof h.onDelete === 'function') h.onDelete(); });
    actions.appendChild(delBtn);
  }
  wrap.appendChild(actions);
  return wrap;
}

function kindFields(b, V, onChange) {
  const box = el('div', { class: 'dn-bld-ef-kindfields' });
  const k = b.kind;
  if (k === 'single_turn' || k === 'synthetic_clean' || k === 'synthetic_adversarial') {
    box.appendChild(fieldRow('Input', areaInput(b.input, 'Entry input', (v) => { b.input = v; })));
  }
  if (k === 'multi_turn_scripted') {
    box.appendChild(turnsEditor(b, onChange));
    box.appendChild(fieldRow('Max turns', numberInput(b.max_turns, 'Max turns',
      { min: '1', step: '1' }, (n) => { b.max_turns = Math.round(n); })));
  }
  if (k === 'multi_turn_emulated') {
    const p = b.user_persona || (b.user_persona = { goal: '', constraints: '', stop_when: '' });
    box.appendChild(fieldRow('Persona goal', areaInput(p.goal, 'Persona goal', (v) => { p.goal = v; })));
    box.appendChild(fieldRow('Persona constraints', areaInput(p.constraints, 'Persona constraints', (v) => { p.constraints = v; })));
    box.appendChild(fieldRow('Stop when', areaInput(p.stop_when, 'Persona stop when', (v) => { p.stop_when = v; })));
    box.appendChild(fieldRow('Max turns', numberInput(b.max_turns, 'Max turns',
      { min: '1', step: '1' }, (n) => { b.max_turns = Math.round(n); })));
  }
  if (k === 'synthetic_adversarial') {
    box.appendChild(fieldRow('Adversarial agent spec',
      textInput(b.adversarial_agent_spec, 'Adversarial agent spec', (v) => { b.adversarial_agent_spec = v; }, 'dn-bld-ef-adv'),
      el('span', { class: 'dn-bld-ef-hint', text: 'dotted path to a known-bad agent — validated by `zicato board audit`, never imported here' })));
    box.appendChild(driftKindsPicker(b, V, onChange));
  }
  return box;
}

function turnsEditor(b, onChange) {
  const turns = Array.isArray(b.turns) ? b.turns : (b.turns = [{ user: '' }]);
  const rows = turns.map((t, i) => {
    const area = areaInput(t.user, 'Scripted turn ' + (i + 1), (v) => { t.user = v; });
    const up = el('button', { class: 'dn-bld-btn dn-bld-ef-turnup', type: 'button', 'aria-label': 'Move scripted turn ' + (i + 1) + ' up', text: '↑' });
    up.addEventListener('click', (ev) => { if (ev.stopPropagation) ev.stopPropagation(); if (i > 0) { const [m] = turns.splice(i, 1); turns.splice(i - 1, 0, m); onChange(); } });
    const down = el('button', { class: 'dn-bld-btn dn-bld-ef-turndown', type: 'button', 'aria-label': 'Move scripted turn ' + (i + 1) + ' down', text: '↓' });
    down.addEventListener('click', (ev) => { if (ev.stopPropagation) ev.stopPropagation(); if (i < turns.length - 1) { const [m] = turns.splice(i, 1); turns.splice(i + 1, 0, m); onChange(); } });
    const rm = el('button', { class: 'dn-bld-btn dn-bld-ef-turnrm', type: 'button', 'aria-label': 'Remove scripted turn ' + (i + 1), text: '×' });
    rm.addEventListener('click', (ev) => { if (ev.stopPropagation) ev.stopPropagation(); turns.splice(i, 1); if (!turns.length) turns.push({ user: '' }); onChange(); });
    return el('div', { class: 'dn-bld-ef-turn' }, [area, el('div', { class: 'dn-bld-ef-turnbtns' }, [up, down, rm])]);
  });
  const add = el('button', { class: 'dn-bld-btn dn-bld-ef-turnadd', type: 'button', 'aria-label': 'Add scripted turn', text: '+ turn' });
  add.addEventListener('click', () => { turns.push({ user: '' }); onChange(); });
  return el('div', { class: 'dn-bld-ef-turns' }, [
    el('div', { class: 'dn-bld-ef-sublabel', text: 'Scripted turns' }),
    ...rows, add,
  ]);
}

function driftKindsPicker(b, V, onChange) {
  const kinds = (V && Array.isArray(V.drift_kinds) && V.drift_kinds.length) ? V.drift_kinds : DEFAULT_VOCAB.drift_kinds;
  const cur = new Set(Array.isArray(b.required_drift_kinds) ? b.required_drift_kinds : []);
  const boxes = kinds.map((dk) => {
    const chk = checkbox(cur.has(dk), 'Required drift kind ' + dk, dk, (on) => {
      const next = new Set(Array.isArray(b.required_drift_kinds) ? b.required_drift_kinds : []);
      if (on) next.add(dk); else next.delete(dk);
      b.required_drift_kinds = [...next];
      onChange();
    });
    return chk;
  });
  if (!boxes.length) boxes.push(el('p', { class: 'dn-faint', text: 'no drift-kind vocabulary available' }));
  return el('div', { class: 'dn-bld-ef-drift' }, [
    el('div', { class: 'dn-bld-ef-sublabel', text: 'Required drift kinds' }),
    el('div', { class: 'dn-bld-ef-driftgrid' }, boxes),
  ]);
}

function expectationForm(b, V, onChange) {
  const box = el('div', { class: 'dn-bld-ef-expect' });
  const on = !!(b.expectation && b.expectation.kind);
  box.appendChild(checkbox(on, 'Expectation enabled', 'Attach a pass/fail expectation', (want) => {
    if (want && !b.expectation) b.expectation = { kind: (V.expectation_kinds || DEFAULT_VOCAB.expectation_kinds)[0], spec: '', reads: 'final_output' };
    else if (!want) b.expectation = null;
    onChange();
  }));
  if (!on) return box;

  const exp = b.expectation;
  box.appendChild(fieldRow('Expectation kind', selectInput(exp.kind, 'Expectation kind',
    V.expectation_kinds || DEFAULT_VOCAB.expectation_kinds, (v) => { exp.kind = v; onChange(); })));

  // spec control per kind.
  if (exp.kind === 'predicate') {
    box.appendChild(fieldRow('Predicate path',
      textInput(exp.spec, 'Expectation predicate path', (v) => { exp.spec = v; }, 'dn-bld-ef-pred'),
      el('span', { class: 'dn-bld-ef-hint', text: 'dotted path to a (run_result) → bool callable — shape only, imported by the runtime not the builder' })));
  } else if (exp.kind === 'json_schema') {
    const hint = el('span', { class: 'dn-bld-ef-hint dn-bld-ef-jsonhint', text: jsonHint(exp.spec) });
    const area = areaInput(exp.spec, 'Expectation spec', (v) => { exp.spec = v; hint.textContent = jsonHint(v); }, 'dn-bld-ef-json');
    box.appendChild(fieldRow('JSON Schema', area, hint));
  } else if (exp.kind === 'rubric') {
    box.appendChild(rubricForm(exp));
  } else {
    // expected_text / regex → a plain textarea.
    box.appendChild(fieldRow(exp.kind === 'regex' ? 'Regex pattern' : 'Expected text',
      areaInput(exp.spec, 'Expectation spec', (v) => { exp.spec = v; })));
  }

  // reads scope — conversation_end DISABLED for single_turn (validate cross-check).
  const disabledReads = b.kind === 'single_turn' ? ['conversation_end'] : [];
  box.appendChild(fieldRow('Reads', selectInput(exp.reads || 'final_output', 'Expectation reads',
    V.reads || DEFAULT_VOCAB.reads, (v) => { exp.reads = v; }, disabledReads)));
  return box;
}

function jsonHint(spec) {
  const s = String(spec == null ? '' : spec).trim();
  if (!s) return 'empty — a JSON Schema document is expected';
  try { JSON.parse(s); return '✓ parses as JSON'; } catch (e) { return '⚠ not valid JSON: ' + (e && e.message ? e.message : 'parse error'); }
}

// The rubric structured sub-form serializes to the JSON `spec` STRING the
// matcher reads: {"rubric": <text>, "threshold": <float|null>, "scale":[lo,hi]}.
function rubricForm(exp) {
  let parsed = {};
  try { parsed = exp.spec ? JSON.parse(exp.spec) : {}; } catch (e) { parsed = {}; }
  const state = {
    rubric: typeof parsed.rubric === 'string' ? parsed.rubric : '',
    threshold: (typeof parsed.threshold === 'number') ? parsed.threshold : null,
    lo: Array.isArray(parsed.scale) && parsed.scale.length === 2 ? Number(parsed.scale[0]) : 0,
    hi: Array.isArray(parsed.scale) && parsed.scale.length === 2 ? Number(parsed.scale[1]) : 1,
  };
  const reserialize = () => {
    const doc = { rubric: state.rubric, threshold: state.threshold, scale: [state.lo, state.hi] };
    exp.spec = JSON.stringify(doc);
  };
  reserialize(); // normalize the on-load spec so a no-op open is a clean doc
  const box = el('div', { class: 'dn-bld-ef-rubric' });
  box.appendChild(fieldRow('Rubric', areaInput(state.rubric, 'Rubric text', (v) => { state.rubric = v; reserialize(); })));
  const thr = el('input', {
    class: 'dn-bld-num', type: 'number', step: '0.05', 'aria-label': 'Rubric threshold',
    value: state.threshold != null ? String(state.threshold) : '',
  });
  const commitThr = () => {
    const raw = thr.value != null ? thr.value : thr.getAttribute('value');
    const s = String(raw == null ? '' : raw).trim();
    state.threshold = s === '' ? null : (isFinite(Number(s)) ? Number(s) : null);
    reserialize();
  };
  thr.addEventListener('input', commitThr);
  thr.addEventListener('change', commitThr);
  box.appendChild(fieldRow('Threshold (optional)', thr,
    el('span', { class: 'dn-bld-ef-hint', text: 'blank = no threshold; the raw scaled score is used' })));
  box.appendChild(fieldRow('Scale low', numberInput(state.lo, 'Rubric scale low', { step: '1' }, (n) => { state.lo = n; reserialize(); })));
  box.appendChild(fieldRow('Scale high', numberInput(state.hi, 'Rubric scale high', { step: '1' }, (n) => { state.hi = n; reserialize(); })));
  return box;
}

function judgesForm(b, V, onChange) {
  const judges = Array.isArray(b.judges) ? b.judges : (b.judges = []);
  const box = el('div', { class: 'dn-bld-ef-judges' });
  box.appendChild(el('div', { class: 'dn-bld-ef-sublabel', text: 'Process judges' }));
  judges.forEach((j, i) => {
    const row = el('div', { class: 'dn-bld-ef-judge' });
    row.appendChild(fieldRow('Name', textInput(j.name, 'Judge name ' + (i + 1), (v) => { j.name = v; }, 'dn-bld-ef-jname')));
    row.appendChild(fieldRow('Mode', selectInput(j.mode, 'Judge mode ' + (i + 1),
      V.judge_modes || DEFAULT_VOCAB.judge_modes, (v) => { j.mode = v; onChange(); })));
    const bodyCtl = j.mode === 'python'
      ? textInput(j.body, 'Judge body ' + (i + 1), (v) => { j.body = v; }, 'dn-bld-ef-jbody')
      : areaInput(j.body, 'Judge body ' + (i + 1), (v) => { j.body = v; }, 'dn-bld-ef-jbody');
    row.appendChild(fieldRow(j.mode === 'python' ? 'Dotted path' : 'Criterion', bodyCtl));
    row.appendChild(fieldRow('Severity', selectInput(j.severity, 'Judge severity ' + (i + 1),
      V.severities || DEFAULT_VOCAB.severities, (v) => { j.severity = v; })));
    const rm = el('button', { class: 'dn-bld-btn dn-bld-ef-judgerm', type: 'button', 'aria-label': 'Remove judge ' + (i + 1), text: 'Remove judge' });
    rm.addEventListener('click', () => { judges.splice(i, 1); onChange(); });
    row.appendChild(rm);
    box.appendChild(row);
  });
  const add = el('button', { class: 'dn-bld-btn dn-bld-ef-judgeadd', type: 'button', 'aria-label': 'Add judge', text: '+ judge' });
  add.addEventListener('click', () => { judges.push({ name: '', mode: 'inline', body: '', severity: 'warning' }); onChange(); });
  box.appendChild(add);
  return box;
}

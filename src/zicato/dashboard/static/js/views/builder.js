// js/views/builder.js — the tournament-builder view (B2).
//
// A self-contained four-pane Console-IV view that drives the B1a/B1b builder
// backend: a LEFT RAIL of contract sections (Structure · Field & noise · Board
// & holdout · Proposer · Gate · Review), a CENTER pane of the active section's
// controls, a RIGHT live PREVIEW (the per-structure svg.js schematic + a cost
// meter + a board/holdout strip + a contract-impact pill + validation
// warnings), and a FAR-RIGHT CHAT copilot pane (collapsible + drag-resizable).
//
// One shared DRAFT is the single source of truth. Every form control posts to
// POST /builder/op and applies the returned {draft,patch,cost,warnings,diff}
// to that local draft, then re-renders the center + preview. The chat pane
// posts to /builder/chat (SSE) and applies each `patch` frame to the SAME
// draft, so the form and chat are two views of one contract. Apply hits POST
// /builder/apply behind an explicit confirm (dry-run = confirm:false).
//
// Re-home discipline: the whole view lives behind `render(host, ctx)` with no
// dependency on the tree/route params, so B3 can move it under a Settings
// panel or a standalone entry by changing only the route wiring.

import { el, clearChildren, patchText } from '../core/dom.js';
import { gatedSwap, section, empty, chip } from '../ui.js';
import { admissionVisuals, vizFromFeedAdmission } from '../core/admission_viz.js';
import * as D from '../data.js';
import { infoPopover } from '../builder/popover.js';
import { BuilderChat } from '../builder/chat.js';
import { previewNodes } from '../builder/preview.js';
import {
  entryEditor, entryToBuffer, bufferToEntryJson, newEntryBuffer, HOLDOUT_TAG,
} from '../builder/entry_form.js';
import {
  STRUCTURES, STRUCTURE_GLYPH, paramSpecsFor, structureGlyphSvg,
  readChatWidth, persistChatWidth, readChatCollapsed, persistChatCollapsed,
  CHAT_MIN, CHAT_MAX,
} from '../builder/model.js';
import {
  getConfig, getDraft, postOp, postApply, getSuggestions,
} from '../builder/api.js';

// The shared session draft + the section selection live across re-renders so a
// state-tick re-dispatch never resets the operator's place. `render` rebuilds
// the chrome ONCE per mount and patches the panes thereafter (digest-gated).
const SECTIONS = [
  { id: 'structure', label: 'Structure' },
  { id: 'field', label: 'Field & noise' },
  { id: 'board', label: 'Board & holdout' },
  { id: 'overfitting', label: 'Overfitting' },
  { id: 'weights', label: 'Weights' },
  { id: 'proposer', label: 'Proposer' },
  { id: 'gate', label: 'Gate' },
  { id: 'review', label: 'Review' },
];

let _draft = null;       // the live draft.to_dict()
let _cost = null;        // last cost.to_dict()
let _warnings = [];      // last warnings[]
let _diff = null;        // last diff.to_dict()
let _preflight = null;   // last preflight result (the op's `preflight` key)
let _drafts = [];        // named fork slots (the fork/compare lifecycle)
let _activeSlot = null;  // the slot the session is bound to ('' = unnamed)
let _compare = null;     // last compare result (the op's `compare` key)
let _cmpA = 'session';   // compare operand selections (persist across renders)
let _cmpB = 'live';
let _config = null;      // /builder/config public dict (+ `vocab`)
let _active = 'structure';
let _busy = false;
let _chat = null;
let _suggestions = null;  // /builder/suggestions feed {epoch_id, reflection_id, suggestions[]}
// The provenance-payload cache for FOREIGN-source suggestion cards (keyed by
// suggestion_id): { state:'loading'|'done', payload }. The fetch is digest-gated
// (kicked once per foreign suggestion, cache-guarded), folded into the center
// digest so its arrival re-renders the card with the mini-strip + admission viz.
const _provenance = {};

// A test seam for the guarded trajectory-strip figure. When set, the injected
// factory renders the mini-strip SYNCHRONOUSLY (so the "figure present" branch is
// deterministic in the node suite); `undefined` FORCES the absent branch (the
// textual fallback) even though the real figure ships in the merged tree; `null`
// (the default) lets the real guarded dynamic import resolve `svg.trajectoryStrip`.
let _stripFigureForTest = null;
export function _setStripFigureForTest(fn) { _stripFigureForTest = fn; }

// ── board-editor module state (B2) ────────────────────────────────────
// Pinned across renders so the inline accordion survives a digest re-render:
// the center digest folds these in, and renderCenter rebuilds the open editor
// from the pinned buffer (module-state pin). VALUE edits mutate `_editBuffer`
// in place (no re-render, focus kept); STRUCTURAL edits re-render off it.
let _editId = null;            // id of the entry whose inline editor is open
let _editBuffer = null;        // the working buffer (entry_form's plain-JSON shape)
let _editCreate = false;       // true → a create-mode buffer (new entry, id unlocked)
let _editError = '';           // verbatim server ValueError for the open editor
let _confirmDeleteEntry = false; // two-click delete arm
let _importText = '';          // paste-JSONL import textarea content
let _importReport = [];        // per-line import results [{line, ok, error?}]
let _briefDraft = null;        // uncommitted proposer-brief text (null → use draft.brief)
let _proposerDirs = [];        // discovered proposer dirs [{name, path}] (from /builder/draft)
let _confirmReset = false;     // two-click revert-to-live arm (slot strip)
let _undoNote = '';            // the last undo's note (e.g. "nothing to undo")
let _replacedNote = '';        // create-mode Save that replaced an existing id (F6 notice)

// Re-render hooks set up per mount.
let _renderCenter = () => {};
let _renderPreview = () => {};
let _renderRail = () => {};

export async function render(host) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Loading the tournament builder…' }));

  // Load config + draft + suggestions once; subsequent renders reuse the state.
  if (_config == null) {
    const [cfg, snap, sug] = await Promise.all([getConfig(), getDraft(), getSuggestions()]);
    _config = cfg || { chat_enabled: false, agent: {}, skills: [] };
    if (snap) applySnapshot(snap);
    _suggestions = sug || { epoch_id: null, reflection_id: null, suggestions: [] };
  }

  clearChildren(host);

  const root = el('div', { class: 'dn-builder' });

  // ── the four panes ────────────────────────────────────────────────
  const railHost = el('nav', { class: 'dn-bld-rail', 'aria-label': 'Contract sections' });
  const centerHost = el('div', { class: 'dn-bld-center', role: 'region', 'aria-label': 'Section controls' });
  const previewHost = el('aside', { class: 'dn-bld-preview', 'aria-label': 'Live preview' });

  // the form + preview share the "work" column; the chat pane rides to its
  // right with a drag handle so the work column reflows to the remaining width.
  const work = el('div', { class: 'dn-bld-work' }, [railHost, centerHost, previewHost]);

  _chat = new BuilderChat({
    config: _config,
    onPatch: (frame) => applyOpResult(frame, { fromChat: true }),
    initialWidth: readChatWidth(),
    collapsed: readChatCollapsed(),
    onWidthChange: (w) => { persistChatWidth(w); reflow(root, w, _chat.collapsed()); },
    onCollapse: (c) => { persistChatCollapsed(c); reflow(root, _chat.width(), c); },
    min: CHAT_MIN, max: CHAT_MAX,
  });

  root.appendChild(work);
  root.appendChild(_chat.node);
  host.appendChild(root);
  reflow(root, _chat.width(), _chat.collapsed());

  _renderRail = () => renderRail(railHost);
  _renderCenter = () => renderCenter(centerHost);
  _renderPreview = () => renderPreview(previewHost);

  _renderRail();
  _renderCenter();
  _renderPreview();
}

// REFLOW the work column to the width the chat pane leaves. Real layout: the
// work column is `1fr` and the chat pane a fixed px column, so the form +
// preview never overlap or clip at any chat width. A collapsed chat shrinks to
// a thin strip. We stamp the width as a CSS var so the grid template tracks it.
function reflow(root, width, collapsed) {
  if (!root) return;
  const w = collapsed ? 0 : Math.max(CHAT_MIN, Math.min(CHAT_MAX, width));
  root.style.setProperty('--dn-bld-chat', (collapsed ? 30 : w) + 'px');
  if (root.setAttribute) root.setAttribute('data-chat-collapsed', collapsed ? '1' : '0');
}

// ── shared-draft plumbing ────────────────────────────────────────────

function applySnapshot(snap) {
  _draft = snap.draft || _draft;
  _cost = snap.cost || _cost;
  _warnings = Array.isArray(snap.warnings) ? snap.warnings : [];
  _diff = snap.diff || _diff;
  if (Array.isArray(snap.drafts)) _drafts = snap.drafts;
  if (Array.isArray(snap.proposer_dirs)) _proposerDirs = snap.proposer_dirs;
}

// Apply the {draft,patch,cost,warnings,diff} envelope the op / a chat patch
// frame returns to the shared draft, then re-render the form + preview so the
// two views stay in sync. `fromChat` tags the chat bubble with the edit.
function applyOpResult(env, opts) {
  if (!env) return;
  if (env.draft) _draft = env.draft;
  if (env.cost) _cost = env.cost;
  if (Array.isArray(env.warnings)) _warnings = env.warnings;
  if (env.diff) _diff = env.diff;
  if (env.preflight) _preflight = env.preflight;
  if (Array.isArray(env.drafts)) _drafts = env.drafts;
  if (env.compare) _compare = env.compare;
  if (env.patch && (env.patch.op === 'fork' || env.patch.op === 'switch')
      && env.patch.changed && env.patch.changed.name) {
    _activeSlot = env.patch.changed.name;
  }
  // The undo lifecycle op reports "nothing to undo" via the patch note when the
  // session's snapshot history is empty — surface it beside the Undo button.
  if (env.patch && env.patch.op === 'undo') _undoNote = env.patch.note || '';
  else if (env.patch) _undoNote = '';
  // A draft-IDENTITY change (switch to another slot, revert-to-live, or an undo
  // step) swaps the board out from under any open inline editor: a buffer typed
  // against the old draft would Save into the NEW one. Close the editor so the
  // stale buffer can never post. (Fork is deliberately excluded — it COPIES the
  // working draft, so the open editor still targets the same entries.)
  if (env.patch && (env.patch.op === 'switch' || env.patch.op === 'revert_to_live' || env.patch.op === 'undo')) {
    closeEditState();
  }
  _renderRail();
  _renderCenter();
  _renderPreview();
  if (_chat && (opts && opts.fromChat) && env.patch) _chat.tagLastEdit(summarizePatch(env.patch));
}

function summarizePatch(patch) {
  if (!patch) return '';
  const changed = patch.changed || {};
  const keys = Object.keys(changed);
  if (patch.op === 'edit_board_entry') return 'board · ' + (changed.entry_id || 'entry');
  if (!keys.length) return patch.op || 'edit';
  return keys.join(', ');
}

// Run one builder op against the shared session and apply the result.
async function runOp(op, args) {
  if (_busy) return;
  _busy = true;
  _renderCenter();
  try {
    const env = await postOp(op, args);
    if (env && !env.error) applyOpResult(env);
    else if (env && env.error) flashError(env.error);
  } catch (err) {
    flashError((err && err.message) || String(err));
  } finally {
    _busy = false;
    _renderCenter();
  }
}

let _flash = '';
function flashError(msg) { _flash = String(msg || ''); _renderCenter(); }

// ── left rail ─────────────────────────────────────────────────────────

function renderRail(host) {
  const digest = JSON.stringify({
    active: _active, done: SECTIONS.map((s) => sectionDone(s.id)),
    drafts: _drafts, slot: _activeSlot,
    resetArm: _confirmReset, undoNote: _undoNote,
  });
  if (gatedSwap(host, 'rail|' + digest, () => {
    const items = SECTIONS.map((s) => {
      const done = sectionDone(s.id);
      const btn = el('button', {
        class: 'dn-bld-railitem' + (s.id === _active ? ' dn-bld-railitem-active' : '') + (done ? ' dn-bld-railitem-done' : ''),
        type: 'button', 'aria-current': s.id === _active ? 'step' : null,
      }, [
        el('span', { class: 'dn-bld-railglyph', 'aria-hidden': 'true', text: done ? '✓' : (s.id === _active ? '◆' : '◇') }),
        el('span', { class: 'dn-bld-raillabel', text: s.label }),
      ]);
      btn.addEventListener('click', () => { _active = s.id; _renderRail(); _renderCenter(); _renderPreview(); });
      return btn;
    });
    items.push(slotStrip());
    return items;
  })) { /* swapped */ }
}

// ── the draft-slot picker (fork/compare lifecycle) ─────────────────────
//
// Compact strip at the bottom of the rail: switch between named fork slots,
// or fork the working draft into a new one. Slots are how operators iterate
// on contract variants WITHOUT rolling the epoch — apply still writes only
// the draft the session is on.
function slotStrip() {
  const wrap = el('div', { class: 'dn-bld-slots' });
  wrap.appendChild(el('div', { class: 'dn-bld-slots-head', text: 'Drafts' }));
  const cur = _activeSlot || '';
  const sel = el('select', { class: 'dn-bld-select dn-bld-slots-pick', 'aria-label': 'Draft slot' }, [
    el('option', { value: '', text: '(working draft)' }),
    ..._drafts.map((n) => el('option', { value: n, text: n })),
  ]);
  sel.value = cur;
  sel.setAttribute('value', cur);
  sel.addEventListener('change', () => {
    const v = sel.value != null ? sel.value : sel.getAttribute('value');
    if (v && v !== _activeSlot) runOp('switch', { name: v });
  });
  wrap.appendChild(sel);
  const nameIn = el('input', {
    class: 'dn-bld-text dn-bld-slots-name', type: 'text',
    placeholder: 'variant-name', 'aria-label': 'Fork name',
  });
  const forkBtn = el('button', { class: 'dn-bld-btn dn-bld-btn-fork', type: 'button', text: 'Fork' });
  forkBtn.addEventListener('click', () => {
    const v = String(nameIn.value != null ? nameIn.value : (nameIn.getAttribute('value') || '')).trim();
    if (v) runOp('fork', { name: v });
  });
  wrap.appendChild(el('div', { class: 'dn-bld-slots-forkrow' }, [nameIn, forkBtn]));
  wrap.appendChild(lifecycleRow());
  return wrap;
}

// The revert/undo lifecycle controls under the fork row. Reset-to-live discards
// every uncommitted edit by restoring the workspace's live contract IN PLACE
// (two-click confirm — it throws away work); Undo steps back one write op. Both
// drive an existing lifecycle op (L1). A `undo` with an empty history returns a
// "nothing to undo" note, rendered here so the click is never silent.
function lifecycleRow() {
  const reset = el('button', {
    class: 'dn-bld-btn dn-bld-btn-reset' + (_confirmReset ? ' dn-bld-btn-confirm' : ''),
    type: 'button', 'aria-label': 'Reset to live',
    text: _confirmReset ? 'Confirm — discard all edits' : 'Reset to live',
  });
  reset.addEventListener('click', () => {
    if (!_confirmReset) { _confirmReset = true; _renderRail(); return; }
    _confirmReset = false;
    runOp('revert_to_live', {});
  });
  const undo = el('button', {
    class: 'dn-bld-btn dn-bld-btn-undo', type: 'button', 'aria-label': 'Undo', text: 'Undo',
  });
  undo.addEventListener('click', () => { _confirmReset = false; runOp('undo', {}); });
  const kids = [el('div', { class: 'dn-bld-slots-lifecyclerow' }, [reset, undo])];
  if (_undoNote) kids.push(el('div', { class: 'dn-bld-slots-undonote dn-faint', role: 'status', text: _undoNote }));
  return el('div', { class: 'dn-bld-slots-lifecycle' }, kids);
}

// A section is "done" once the operator has visibly engaged its core contract
// surface (a coarse heuristic, not a gate): structure is always set; field has
// params; board has entries; gate has a margin; review is done once nothing is
// left to roll. Drives the rail's done check.
function sectionDone(id) {
  const d = _draft || {};
  const sc = d.scoring || {};
  const ts = sc.tournament || {};
  switch (id) {
    case 'structure': return !!ts.structure;
    case 'field': return !!(ts.params && Object.keys(ts.params).length);
    case 'board': return Array.isArray(d.board) && d.board.length > 0;
    case 'overfitting': return !!sc.overfitting;
    case 'weights': return sc.drift_weight != null;
    case 'proposer': return !!d.proposer;
    case 'gate': return sc.promote_margin != null;
    case 'review': return !!(_diff && _diff.rolls_epoch);
    default: return false;
  }
}

// ── center: the active section's controls ─────────────────────────────

function renderCenter(host) {
  const d = _draft || {};
  const digest = JSON.stringify({
    active: _active, draft: d, busy: _busy, flash: _flash, pf: _preflight,
    cmp: _compare, cmpSel: [_cmpA, _cmpB], drafts: _drafts,
    // the board-editor accordion state — pinned so the open editor survives a
    // digest re-render and reflects structural edits to the buffer.
    editId: _editId, editBuf: _editBuffer, editCreate: _editCreate,
    editErr: _editError, delArm: _confirmDeleteEntry, replaced: _replacedNote,
    imp: _importText, impRep: _importReport, brief: _briefDraft,
    // the suggestions inbox feed — static per mount, folded in so an SSE no-op
    // re-render is a digest no-op (never rebuilds the inbox DOM).
    sug: _suggestions,
    // the foreign-source provenance payloads — folded so a card rebuilds ONCE
    // when its provenance arrives (never on a no-op beat).
    prov: _provenance,
  });
  gatedSwap(host, 'center|' + digest, () => {
    const nodes = [];
    if (_flash) {
      const err = el('div', { class: 'dn-bld-flash', role: 'alert' }, [
        el('span', { text: _flash }),
        el('button', { class: 'dn-bld-flash-x', type: 'button', 'aria-label': 'Dismiss', text: '×' }),
      ]);
      err.lastChild.addEventListener('click', () => { _flash = ''; _renderCenter(); });
      nodes.push(err);
    }
    if (_busy) nodes.push(el('div', { class: 'dn-bld-busy', 'aria-live': 'polite', text: 'applying…' }));
    if (!_draft) { nodes.push(empty('The draft is unavailable — the builder backend may be read-only.')); return nodes; }
    let body;
    switch (_active) {
      case 'structure': body = structureSection(d); break;
      case 'field': body = fieldSection(d); break;
      case 'board': body = boardSection(d); break;
      case 'overfitting': body = overfittingSection(d); break;
      case 'weights': body = weightsSection(d); break;
      case 'proposer': body = proposerSection(d); break;
      case 'gate': body = gateSection(d); break;
      case 'review': body = reviewSection(d); break;
      default: body = empty('Unknown section.');
    }
    nodes.push(body);
    return nodes;
  });
}

// A labelled control row with an info popover (ⓘ) beside the label.
function controlRow(labelText, info, control) {
  return el('div', { class: 'dn-bld-row' }, [
    el('div', { class: 'dn-bld-rowhead' }, [
      el('label', { class: 'dn-bld-label', text: labelText }),
      infoPopover(info),
    ]),
    el('div', { class: 'dn-bld-control' }, [control]),
  ]);
}

// A numeric input that commits through onCommit(number) on change. `attrs`
// carries min/max/step/aria-label; non-finite input is ignored (never posts).
function numInput(value, attrs, onCommit, opts) {
  const o = opts || {};
  const input = el('input', Object.assign({
    class: 'dn-bld-num', type: 'number', value: String(value),
  }, attrs || {}));
  input.addEventListener('change', () => {
    const raw = input.value != null ? input.value : input.getAttribute('value');
    let n = Number(raw);
    if (!isFinite(n)) return;
    if (o.int) n = Math.round(n);
    onCommit(n);
  });
  return input;
}

// A checkbox that commits through onToggle(boolean) on change, wrapped with
// its caption. Follows the existing screen-toggle pattern (attribute-checked
// so the mock DOM and the browser agree).
function checkInput(checked, aria, caption, onToggle) {
  const box = el('input', { class: 'dn-bld-check', type: 'checkbox', 'aria-label': aria });
  if (checked) box.setAttribute('checked', 'checked');
  box.addEventListener('change', () => {
    const on = box.checked != null ? box.checked : (box.getAttribute('checked') != null);
    onToggle(!!on);
  });
  return el('label', { class: 'dn-bld-checkwrap' }, [box, el('span', { text: caption })]);
}

// A WHOLESALE-mapping numeric editor: one row per fixed `key`, each committing
// the WHOLE mapping (the current mapping with that one key updated) through
// `post(fullMapping)`. This is the op semantics — a mapping edit replaces the
// whole mapping, never a per-key delta. `opts.def` is the value shown for a key
// absent from `current` (a neutral display; an untouched absent key never
// enters the posted mapping — only the edited key is added).
function mappingNumRows(keys, current, opts, post) {
  const o = opts || {};
  return keys.map((key) => {
    const has = current[key] != null;
    const val = has ? current[key] : (o.def != null ? o.def : 0);
    return controlRow(o.labelFor(key), o.infoFor(key), numInput(val,
      { step: o.step || '0.1', 'aria-label': o.ariaFor(key) }, (n) => {
        const next = Object.assign({}, current);
        next[key] = n;
        post(next);
      }));
  });
}

// A free-text ADD-KEY row: a key text field + a signed number + an Add button.
// Fixes the iterate-existing-keys-only gap — an operator can introduce a NEW
// mapping key the draft never carried. `onAdd(key, number)` assembles + posts
// the whole mapping. Blank key or non-finite number is inert (never posts).
function addKeyNumRow(opts, onAdd) {
  const o = opts || {};
  const keyIn = el('input', {
    class: 'dn-bld-text dn-bld-addkey-k', type: 'text',
    placeholder: o.placeholder || 'new key', 'aria-label': o.ariaKey,
  });
  const valIn = el('input', {
    class: 'dn-bld-num dn-bld-addkey-v', type: 'number', step: o.step || '0.1',
    value: o.defVal != null ? String(o.defVal) : '0', 'aria-label': o.ariaVal,
  });
  const btn = el('button', {
    class: 'dn-bld-btn dn-bld-addkey-btn', type: 'button', text: 'Add', 'aria-label': o.ariaBtn || 'Add key',
  });
  btn.addEventListener('click', () => {
    const k = String(keyIn.value != null ? keyIn.value : (keyIn.getAttribute('value') || '')).trim();
    const raw = valIn.value != null ? valIn.value : valIn.getAttribute('value');
    const n = Number(raw);
    if (!k || !isFinite(n)) return;
    onAdd(k, n);
  });
  return el('div', { class: 'dn-bld-addkeyrow' }, [keyIn, valIn, btn]);
}

function structureSection(d) {
  const cur = ((d.scoring || {}).tournament || {}).structure || 'gauntlet';
  const cards = STRUCTURES.map((s) => {
    const card = el('button', {
      class: 'dn-bld-card' + (s.id === cur ? ' dn-bld-card-on' : ''),
      type: 'button', 'aria-pressed': String(s.id === cur), title: s.blurb,
    }, [
      structureGlyphSvg(s.id),
      el('span', { class: 'dn-bld-cardname', text: s.label }),
      el('span', { class: 'dn-bld-cardblurb', text: s.blurb }),
    ]);
    card.addEventListener('click', () => { if (s.id !== cur) runOp('set_structure', { structure: s.id }); });
    return card;
  });
  return section('Tournament structure',
    el('p', { class: 'dn-lede', text: 'How challengers are raced against the reigning champion each epoch. The picker drives a contract change — applying it rolls the epoch.' }),
    el('div', { class: 'dn-bld-cards' }, cards));
}

function fieldSection(d) {
  const ts = (d.scoring || {}).tournament || {};
  const specs = paramSpecsFor(ts.structure || 'gauntlet');
  const params = ts.params || {};
  const rows = specs.map((spec) => {
    const val = params[spec.key] != null ? params[spec.key] : spec.def;
    const input = el('input', {
      class: 'dn-bld-num', type: 'number', value: String(val),
      min: spec.min != null ? String(spec.min) : null,
      max: spec.max != null ? String(spec.max) : null,
      step: spec.step != null ? String(spec.step) : '1',
      'aria-label': spec.label,
    });
    const commit = () => {
      const raw = input.value != null ? input.value : input.getAttribute('value');
      let num = Number(raw);
      if (!isFinite(num)) return;
      if (spec.int) num = Math.round(num);
      // Opt-in params (the evidence-gate threshold): 0 REMOVES the key so an
      // unset gate hashes byte-identically to a contract that predates it.
      runOp('set_param', { key: spec.key, value: (spec.removeAtZero && num === 0) ? null : num });
    };
    input.addEventListener('change', commit);
    return controlRow(spec.label, spec.info, input);
  });
  if (!rows.length) rows.push(empty('This structure has no tunable field params.'));
  // Pre-tournament candidate screening (tryouts) — a proposer_quality
  // contract knob, so it drives the dedicated set_screening op rather
  // than set_param. Rendered next to the replicates control: both are
  // noise-vs-cost levers over the same board-unit runner.
  const pq = ((d.scoring || {}).proposer_quality) || {};
  const screen = el('input', {
    class: 'dn-bld-num', type: 'number',
    value: String(pq.screen_entries != null ? pq.screen_entries : 0),
    min: '0', step: '1', 'aria-label': 'Candidate screen entries',
  });
  screen.addEventListener('change', () => {
    const n = Number(screen.value != null ? screen.value : screen.getAttribute('value'));
    if (!isFinite(n) || n < 0) return;
    runOp('set_screening', { entries: Math.round(n) });
  });
  rows.push(controlRow('Candidate screen entries', {
    title: 'Candidate screen entries', def: '0 (off); scaffold 2',
    body: 'Pre-tournament tryout: each best-of-N slate candidate runs this many rotating champion-passing train entries BEFORE selection, and a confirmed catastrophic regression (pass-flip or budget blow-out) is vetoed. Veto-first — the screen disqualifies, it never ranks; the critic chooses among survivors. Costs proposes × best_of_n × entries extra runs per round; inert when best_of_n is 1.',
  }, screen));
  const vetoOnly = el('input', { class: 'dn-bld-check', type: 'checkbox', 'aria-label': 'Screen veto-only' });
  if (pq.screen_veto_only) vetoOnly.setAttribute('checked', 'checked');
  vetoOnly.addEventListener('change', () => {
    const on = vetoOnly.checked != null ? vetoOnly.checked : (vetoOnly.getAttribute('checked') != null);
    runOp('set_screening', { veto_only: !!on });
  });
  rows.push(controlRow('Screen veto-only', {
    title: 'Screen veto-only', def: 'off',
    body: 'When on, the screen may only disqualify: its panel counts feed neither the critic prompt nor the heuristic tiebreak. Keeps selection blind to the (selection-biased) tryout measurements while still catching catastrophic regressions.',
  }, el('label', { class: 'dn-bld-checkwrap' }, [vetoOnly, el('span', { text: 'veto only — no selection tiebreak' })])));
  return section('Field & noise', ...rows);
}

function boardSection(d) {
  const board = Array.isArray(d.board) ? d.board : [];
  const holdout = d.holdout || { train_ids: [], holdout_ids: [] };
  const of = ((d.scoring || {}).overfitting) || {};
  const vocab = (_config && _config.vocab) || {};
  const trainSet = new Set(holdout.train_ids || []);
  const holdSet = new Set(holdout.holdout_ids || []);

  const toggleHoldout = (id, held) => {
    const next = held ? [...holdSet].filter((x) => x !== id) : [...holdSet, id];
    runOp('set_holdout', { tags: next });
  };

  // one board row: id + kind + badges (clickable → open the inline accordion),
  // a holdout toggle, and per-judge badges whose × posts remove_judge directly.
  const rowNodes = [];
  board.forEach((b) => {
    const id = b.id || b.entry_id;
    const held = holdSet.has(id);
    const badges = entryBadges(b, id);
    const main = el('div', {
      class: 'dn-bld-boardrow-main', role: 'button', tabindex: '0',
      'aria-label': 'Edit board entry ' + id,
    }, [
      el('span', { class: 'dn-bld-boardid', title: id, text: id }),
      el('span', { class: 'dn-bld-boardkind', text: b.kind || '' }),
      el('div', { class: 'dn-bld-boardbadges' }, badges),
    ]);
    main.addEventListener('click', () => openEdit(b));
    const toggle = el('button', {
      class: 'dn-bld-holdtoggle' + (held ? ' dn-bld-held' : ''), type: 'button',
      'aria-pressed': String(held), title: held ? 'held out (click to train on it)' : 'in train set (click to hold out)',
      text: held ? 'holdout' : 'train',
    });
    toggle.addEventListener('click', (ev) => { if (ev.stopPropagation) ev.stopPropagation(); toggleHoldout(id, held); });
    rowNodes.push(el('div', { class: 'dn-bld-boardrow' + ((_editId === id && !_editCreate) ? ' dn-bld-boardrow-open' : '') }, [main, toggle]));
    // the inline accordion editor rides directly under its row.
    if (_editId === id && !_editCreate && _editBuffer) {
      rowNodes.push(el('div', { class: 'dn-bld-boardrow-editor' }, [entryAccordion(vocab, /* editing */ true)]));
    }
  });

  const fracVal = of.holdout_fraction != null ? of.holdout_fraction : 0.2;
  const frac = el('input', {
    class: 'dn-bld-num', type: 'number', value: String(fracVal), min: '0', max: '0.9', step: '0.05',
    'aria-label': 'Holdout fraction',
  });
  frac.addEventListener('change', () => {
    const n = Number(frac.value != null ? frac.value : frac.getAttribute('value'));
    if (isFinite(n)) runOp('set_holdout', { fraction: n });
  });

  return section('Board & holdout',
    el('p', { class: 'dn-lede', text: 'The evaluation board and its train / holdout split. The winning challenger is re-scored on the holdout slice before it can promote.' }),
    replacedNotice(),
    controlRow('Holdout fraction', {
      title: 'Holdout fraction', def: '0.2',
      body: 'Fraction of the board hash-partitioned into the holdout slice (when no explicit per-entry holdout tags are set). A larger holdout guards harder against overfitting but costs more confirm runs and shrinks the train field.',
    }, frac),
    el('div', { class: 'dn-bld-splitstrip', role: 'img', 'aria-label': `train ${trainSet.size} · holdout ${holdSet.size}` }, [
      el('span', { class: 'dn-bld-split-train', style: `flex:${Math.max(1, trainSet.size)}`, text: `train ${trainSet.size}` }),
      el('span', { class: 'dn-bld-split-hold', style: `flex:${Math.max(0.001, holdSet.size)}`, text: `holdout ${holdSet.size}` }),
    ]),
    boardMetaPanel(d, vocab),
    el('h3', { class: 'dn-bld-subhead', text: 'Entries' }),
    rowNodes.length ? el('div', { class: 'dn-bld-boardlist' }, rowNodes)
      : empty('The board is empty — use “Add entry” below to author the first entry.'),
    addEntryControl(vocab),
    importBox(),
    suggestionsInbox());
}

// ── the eval-suggestions inbox (EVAL-SYNTHESIS.md §6) ──────────────────
// A verdict-led list of the persisted `reflect suggest` output: each row shows
// its rationale, provenance (source episodes + lineage ids + target slice), the
// admission stats rendered HONESTLY (§5 — measured numbers with n, `unmeasured`
// states, the recommended bands as quiet advice, never an auto-verdict), and a
// "stage to draft" affordance driving add_board_entry / add_judge. One
// Instrument-lens link points back at the reflection that motivated them.
// Recommend-only: staging forks the draft the operator reviews; it never seals.
function suggestionsInbox() {
  const feed = _suggestions || { suggestions: [] };
  const items = Array.isArray(feed.suggestions) ? feed.suggestions : [];
  const kids = [
    el('h3', { class: 'dn-bld-subhead', text: 'Suggestions inbox' }),
    el('p', { class: 'dn-faint', text: 'Synthesised eval suggestions (generative reflection). Each carries its admission stats as measured; staging forks a draft you review — nothing here seals the contract. Run `zicato reflect suggest` to refresh.' }),
  ];
  if (feed.epoch_id && feed.reflection_id) {
    kids.push(el('a', {
      class: 'dn-instr-link dn-mono',
      href: '#/e/' + encodeURIComponent(feed.epoch_id) + '/instrument/' + encodeURIComponent(feed.reflection_id),
      text: 'Instrument lens: reflection ' + feed.reflection_id + ' →',
    }));
  }
  if (!items.length) {
    kids.push(empty('No suggestions — run `zicato reflect suggest` to synthesise instrument improvements from the mined episodes.'));
    return el('div', { class: 'dn-bld-suggestions' }, kids);
  }
  kids.push(el('ul', { class: 'dn-bld-suglist' }, items.map((s) => suggestionRow(s))));
  return el('div', { class: 'dn-bld-suggestions' }, kids);
}

function suggestionRow(s) {
  const prov = s.provenance || {};
  const lineage = Array.isArray(prov.source_lineage_ids) ? prov.source_lineage_ids : [];
  const episodes = Array.isArray(prov.source_episodes) ? prov.source_episodes : [];
  const foreign = prov.foreign_source;
  const isForeign = foreign && typeof foreign === 'object';
  // A foreign-source card's provenance payload (mini-strip + render-ready
  // admission marks) is fetched lazily + cached; kick it once (cache-guarded).
  const provPayload = isForeign ? ensureProvenance(s) : null;

  const head = el('div', { class: 'dn-bld-sughead' }, [
    chip('sug', s.suggestion_type || 'suggestion'),
    el('span', { class: 'dn-bld-sugsubject dn-mono', text: s.subject || '' }),
    chip('slice', s.target_slice || 'slice'),
  ]);
  const meta = [
    el('div', { class: 'dn-bld-sugsummary', text: s.summary || '' }),
  ];
  // the longer rationale (the motivating episodes + any self-trace caveat) — a
  // quiet caption under the summary. textContent-set (never innerHTML) so a
  // synthesised rationale can never inject markup.
  if (s.rationale) {
    meta.push(el('div', { class: 'dn-faint dn-bld-sugrationale', text: s.rationale }));
  }
  // ── the ADMISSION VISUALS (TRAJECTORY-UI.md §2.2a) — the flip-rate whisker +
  // discrimination pips + evidence tier, replacing the bare admission numbers.
  // Render from the provenance reader's render-ready `admission_viz` when it has
  // landed (foreign cards), else adapt the feed's engine admission shape. The
  // honest TEXT line stays below as the accessible readout (numbers ride marks).
  const viz = (provPayload && provPayload.admission_viz)
    ? provPayload.admission_viz
    : vizFromFeedAdmission(s.admission);
  meta.push(el('div', { class: 'dn-bld-sugadmviz' }, [admissionVisuals(viz)]));
  meta.push(el('div', { class: 'dn-stat dn-bld-sugadmission', text: 'admission: ' + admissionText(s.admission) }));
  meta.push(el('div', { class: 'dn-faint dn-bld-sugprov', text:
    'provenance: ' + (episodes.length ? episodes.length + ' episode(s)' : 'no episodes')
    + (lineage.length ? ' · lineage ' + lineage.join(', ') : '')
    + ' · target ' + (prov.target_slice || s.target_slice || '?') }));
  // Foreign-source provenance (TRAJECTORY-BOOTSTRAP.md §6): a bootstrap
  // suggestion came from a foreign agent trace, not a reign — name the trace
  // file + sniffed dialect so the operator sees the on-ramp, plus the PROVENANCE
  // MINI-STRIP (trace region → episode → this suggestion) and a link into the
  // Traces detail.
  if (isForeign) {
    meta.push(el('div', { class: 'dn-faint dn-bld-sugforeign', text:
      'foreign source: ' + (foreign.source_file || '?') + ' (' + (foreign.dialect || '?') + ')' }));
    meta.push(provenanceStripBlock(s, foreign, provPayload));
  }
  const kids = [head, ...meta];
  const op = s.proposed_op;
  if (op && op.op) {
    const btn = el('button', {
      class: 'dn-bld-btn dn-bld-sugstage', type: 'button',
      text: 'Stage to draft', 'aria-label': 'Stage suggestion ' + (s.suggestion_id || '') + ' to draft',
    });
    // An entry suggestion stages through the new add_board_entry op — the inbox
    // IS that op's GUI control (the L2 machine-pin). A judge suggestion reuses
    // the granular add_judge op dynamically (its authoring GUI is the entry
    // editor's judges list; the inbox only stages it).
    btn.addEventListener('click', () => {
      if (op.op === 'add_board_entry') runOp('add_board_entry', op.args || {});
      else runOp(op.op, op.args || {});
    });
    kids.push(btn);
  } else {
    kids.push(el('span', { class: 'dn-faint', text: 'recommendation only — no mechanical op (an authoring decision)' }));
  }
  // The roll-honesty note (TRAJECTORY-UI.md §2.2a): a bootstrap entry defaults to
  // `train` (a regression suite) — keep it there unless the trace is genuinely
  // foreign. Recommend-only: staging forks a draft the operator seals.
  if (isForeign && (s.target_slice || '') === 'train') {
    kids.push(el('div', { class: 'dn-faint dn-bld-sugroll', text:
      'drafts default to train (a regression suite) — promote out of train only when the trace is genuinely foreign. Staging forks a draft you seal.' }));
  }
  return el('li', { class: 'dn-bld-sugrow dn-bld-sugcard' }, kids);
}

// Kick the FOREIGN-source provenance fetch once per suggestion (cache-guarded),
// returning the payload if it has landed. The fetch is digest-gated: on arrival
// it re-renders the center (folding `_provenance`), so the card rebuilds ONCE
// with the mini-strip + render-ready admission marks. A transport failure /
// found:false payload leaves the card on its textual fallback (never a raise).
function ensureProvenance(s) {
  const feed = _suggestions || {};
  const rid = feed.reflection_id;
  const sid = s && s.suggestion_id;
  if (!rid || !sid) return null;
  const cached = _provenance[sid];
  if (cached) return cached.state === 'done' ? cached.payload : null;
  _provenance[sid] = { state: 'loading', payload: null };
  D.suggestionProvenance(rid, sid).then((payload) => {
    _provenance[sid] = { state: 'done', payload: (payload && typeof payload === 'object') ? payload : null };
    _renderCenter();
  }).catch(() => {
    _provenance[sid] = { state: 'done', payload: null };
    _renderCenter();
  });
  return null;
}

// The provenance mini-strip block: a link into the Traces detail + a host that
// carries the trajectory-strip figure (compact mode) once BOTH the payload has
// landed AND the shared figure resolves. Absent figure → the textual fallback
// stays (the guarded-import seam, evals_health precedent).
function provenanceStripBlock(s, foreign, provPayload) {
  const feed = _suggestions || {};
  const traceId = foreign.trace_id || (provPayload && provPayload.subject) || '';
  const wrap = el('div', { class: 'dn-bld-sugstripwrap' });
  // the link into the Traces DETAIL route: #/e/<epoch>/traces/<reflection>/<trace>
  // (the review caught the earlier singular `/trace/` segment falling through
  // parseRoute's default to the epoch view).
  if (feed.epoch_id && feed.reflection_id && traceId) {
    wrap.appendChild(el('a', {
      class: 'dn-bld-sugtracelink dn-mono',
      href: '#/e/' + encodeURIComponent(feed.epoch_id) + '/traces/'
        + encodeURIComponent(feed.reflection_id) + '/' + encodeURIComponent(traceId),
      text: 'trace ' + traceId + ' →',
      'aria-label': 'open the imported trace ' + traceId + ' in the Traces view',
    }));
  }
  const host = el('div', { class: 'dn-bld-sugstrip' });
  // the textual fallback — the provenance chain in words. Rendered first; the
  // figure replaces it if the shared trajectoryStrip resolves.
  const seg = provStripModel(provPayload);
  host.appendChild(stripTextFallback(provPayload, seg));
  if (seg) mountProvenanceStrip(host, seg);
  wrap.appendChild(host);
  return wrap;
}

// The motivating episode's segment strip-model from the provenance payload (the
// focus episode's `segment_strip_model`, focus_episode_id already server-set).
function provStripModel(provPayload) {
  if (!provPayload || !Array.isArray(provPayload.episodes)) return null;
  for (const ep of provPayload.episodes) {
    if (ep && ep.segment_strip_model && typeof ep.segment_strip_model === 'object') {
      return ep.segment_strip_model;
    }
  }
  return null;
}

// The textual fallback for the mini-strip — the trace → episode chain in words,
// honest while the payload loads / when the figure is absent (never a fake bar).
function stripTextFallback(provPayload, seg) {
  if (!seg) {
    const loading = provPayload === null;
    return el('div', { class: 'dn-faint dn-bld-sugstrip-fallback', text:
      loading ? 'loading provenance…' : 'provenance strip unavailable' });
  }
  const turns = (seg.lane && seg.lane.turn_count) || 0;
  const sigs = Array.isArray(seg.signals) ? seg.signals.length : 0;
  const eps = Array.isArray(seg.episodes) ? seg.episodes.length : 0;
  const budget = (seg.budget && seg.budget.label) ? seg.budget.label : '';
  return el('div', { class: 'dn-faint dn-bld-sugstrip-fallback', text:
    'provenance: ' + turns + ' turn(s) · ' + sigs + ' signal(s) · ' + eps + ' episode(s)'
    + (budget ? ' · ' + budget : '') });
}

// Mount the shared trajectory-strip figure (compact mode) into a host that
// already carries the textual fallback. GUARDED: a test-injected factory renders
// synchronously; otherwise a dynamic import of `svg.trajectoryStrip` — absent in
// this branch → the fallback stays (the seam composes at integration, WS-TRACES).
function mountProvenanceStrip(hostEl, stripModel) {
  const place = (fn) => {
    if (typeof fn !== 'function') return false;
    try {
      const fig = fn(stripModel, { compact: true });
      if (fig) { clearChildren(hostEl); hostEl.appendChild(fig); return true; }
    } catch (e) { /* additive — keep the textual fallback */ }
    return false;
  };
  if (_stripFigureForTest !== null) { place(_stripFigureForTest); return; }
  import('../svg.js').then((mod) => { place(mod && mod.trajectoryStrip); })
    .catch(() => { /* figure absent → the textual fallback stays */ });
}

// Honest one-line admission summary — mirrors suggestions.format_admission:
// measured numbers WITH their n, `unmeasured` where a probe did not run, and
// the recommended bands only as quiet advice (never an auto-verdict).
function admissionText(adm) {
  if (!adm || typeof adm !== 'object') return 'unmeasured (plan mode — no probe spent)';
  const parts = [];
  const noise = adm.noise;
  if (noise && noise.measured) parts.push('flip ' + noise.flip_rate + ' (n=' + noise.runs + (noise.base != null ? ' @base ' + noise.base : '') + ')');
  else parts.push('flip unmeasured');
  const disc = adm.discrimination;
  if (disc && disc.measured) parts.push('sep ' + disc.separated + '/' + disc.pairs);
  else parts.push('sep unmeasured');
  const leak = adm.leakage;
  if (leak && leak.target_slice_ok === false) parts.push('LEAK: motivating proposer saw the target slice');
  if (leak && leak.self_preference_flag) parts.push('self-preference flag');
  const advisory = admissionAdvisory(adm);
  if (advisory) parts.push('advisory: ' + advisory);
  return parts.join('; ');
}

// The recommended bands as QUIET advice — mirrors suggestions._admission_advisory
// (RECOMMENDED_FLIP_CEILING = 0.25, RECOMMENDED_MIN_DISCRIMINATION = 1). Advice
// text only; never a verdict that drops the suggestion.
function admissionAdvisory(adm) {
  const notes = [];
  const noise = adm.noise;
  if (noise && noise.measured && typeof noise.flip_rate === 'number' && noise.flip_rate > 0.25) {
    notes.push('flip above the 0.25 advisory ceiling (noisy eval)');
  }
  const disc = adm.discrimination;
  if (disc && disc.measured && typeof disc.separated === 'number' && disc.separated < 1) {
    notes.push('separated nothing (a dead channel before it ships)');
  }
  return notes.join('; ');
}

// The create-mode "replaced an existing id" notice (F6). A dismissable status
// banner — NOT a confirm gate — so a create Save that landed on an existing id
// (an id-matched replace, never a rename) is surfaced rather than silent.
function replacedNotice() {
  if (!_replacedNote) return null;
  const note = el('div', { class: 'dn-bld-replaced-note', role: 'status' }, [
    el('span', { class: 'dn-bld-replaced-msg', text: _replacedNote }),
    el('button', { class: 'dn-bld-replaced-x', type: 'button', 'aria-label': 'Dismiss', text: '×' }),
  ]);
  note.lastChild.addEventListener('click', () => { _replacedNote = ''; _renderCenter(); });
  return note;
}

// The row's at-a-glance badges: expectation kind, per-judge (removable), a
// non-unit weight, the budget, and operator tags (never the holdout tag — the
// toggle owns that). Uses ui.js's shared chip builder (U6).
function entryBadges(entry, id) {
  const out = [];
  if (entry.expectation && entry.expectation.kind) out.push(chip('exp', 'exp:' + entry.expectation.kind));
  const judges = Array.isArray(entry.judges) ? entry.judges : [];
  for (const j of judges) {
    const badge = el('span', { class: 'dn-chip dn-chip-judge dn-bld-judgebadge' }, [
      el('span', { class: 'dn-bld-judgebadge-name', text: j.name || 'judge' }),
      el('button', {
        class: 'dn-bld-judgebadge-x', type: 'button', 'aria-label': 'Remove judge ' + (j.name || '') + ' from ' + id, text: '×',
      }),
    ]);
    badge.lastChild.addEventListener('click', (ev) => {
      if (ev.stopPropagation) ev.stopPropagation();
      runOp('remove_judge', { entry_id: id, name: j.name });
    });
    out.push(badge);
  }
  if (entry.weight != null && Number(entry.weight) !== 1) out.push(chip('weight', 'w=' + entry.weight));
  const budget = entry.budget_s != null ? entry.budget_s : entry.wall_clock_budget_seconds;
  if (budget != null) out.push(chip('budget', budget + 's'));
  const tags = (Array.isArray(entry.tags) ? entry.tags : []).filter((t) => t !== HOLDOUT_TAG);
  for (const t of tags) out.push(chip('tag', t));
  return out;
}

// Wire the pure entry_form editor to the module-state handlers. `editing`
// distinguishes an existing-entry accordion (id locked, Delete + Duplicate)
// from a create-mode buffer (id editable).
function entryAccordion(vocab, editing) {
  return entryEditor(_editBuffer, vocab, {
    editing,
    error: _editError,
    deleteArmed: _confirmDeleteEntry,
    onChange: () => { _confirmDeleteEntry = false; _renderCenter(); },
    onSave: () => saveEntry(),
    onCancel: () => { closeEditState(); _renderCenter(); },
    onDelete: () => onDeleteEntry(),
    onDuplicate: () => duplicateEntry(),
  });
}

// The board-level board_meta header controls (drift suppression + judge-only)
// — closes B0's documented GUI exception; both drive the set_board_meta op.
function boardMetaPanel(d, vocab) {
  const meta = d.board_meta || { disable_drift: [], judge_only: false };
  const driftKinds = Array.isArray(vocab.drift_kinds) ? vocab.drift_kinds : [];
  const cur = new Set(meta.disable_drift || []);
  const boxes = driftKinds.map((dk) => checkInput(cur.has(dk), 'Disable drift ' + dk, dk, (on) => {
    const next = new Set(cur);
    if (on) next.add(dk); else next.delete(dk);
    runOp('set_board_meta', { disable_drift: [...next] });
  }));
  const kids = [
    el('h3', { class: 'dn-bld-subhead', text: 'Board metadata' }),
    el('p', { class: 'dn-faint', text: 'Board-level header: drift kinds suppressed for every entry, and the judge-only flag (goldfive judges without steering). A change here rolls the epoch like any board edit.' }),
    checkInput(!!meta.judge_only, 'Board judge-only', 'judge-only board — score on judges alone, no steering', (on) => runOp('set_board_meta', { judge_only: on })),
  ];
  if (boxes.length) {
    kids.push(el('div', { class: 'dn-bld-subhead-min', text: 'Disable drift kinds' }));
    kids.push(el('div', { class: 'dn-bld-boardmeta-drift' }, boxes));
  }
  return el('div', { class: 'dn-bld-boardmeta' }, kids);
}

// The Add-entry control: a kind picker + button seeds a create-mode buffer.
// When a create-mode buffer is open, its editor renders here below the button.
function addEntryControl(vocab) {
  const kinds = Array.isArray(vocab.kinds) && vocab.kinds.length ? vocab.kinds
    : ['single_turn', 'multi_turn_scripted', 'multi_turn_emulated', 'synthetic_adversarial', 'synthetic_clean'];
  const sel = el('select', { class: 'dn-bld-select dn-bld-addkind', 'aria-label': 'New entry kind' },
    kinds.map((k) => el('option', { value: k, text: k })));
  const cur = kinds[0];
  sel.value = cur;
  sel.setAttribute('value', cur);
  const btn = el('button', { class: 'dn-bld-btn dn-bld-btn-addentry', type: 'button', text: '+ Add entry', 'aria-label': 'Add entry' });
  btn.addEventListener('click', () => {
    const kind = sel.value != null ? sel.value : sel.getAttribute('value');
    startCreate(kind);
  });
  const kids = [
    el('div', { class: 'dn-bld-addrow' }, [sel, btn]),
  ];
  if (_editCreate && _editBuffer) {
    kids.push(el('div', { class: 'dn-bld-boardrow-editor dn-bld-createeditor' }, [entryAccordion(vocab, /* editing */ false)]));
  }
  return el('div', { class: 'dn-bld-addentry' }, kids);
}

// The paste-JSONL import box: split lines client-side, route a board_meta
// header line to set_board_meta, post one edit_board_entry per entry line,
// report per-line results inline.
function importBox() {
  const area = el('textarea', {
    class: 'dn-bld-import-area', 'aria-label': 'Paste board JSONL', rows: '4',
    text: _importText,
  });
  const commit = () => { _importText = area.value != null ? area.value : (area.getAttribute('value') || ''); };
  area.addEventListener('input', commit);
  area.addEventListener('change', commit);
  const btn = el('button', { class: 'dn-bld-btn dn-bld-import-btn', type: 'button', text: 'Import JSONL', 'aria-label': 'Import board JSONL' });
  btn.addEventListener('click', () => runImport());
  const kids = [
    el('h3', { class: 'dn-bld-subhead', text: 'Import JSONL' }),
    el('p', { class: 'dn-faint', text: 'One entry per line. A leading {"board_meta": true, …} header routes to the board-meta panel; each other line posts through edit_board_entry. Per-line errors report below — a bad line never blocks the good ones.' }),
    area, btn,
  ];
  if (_importReport.length) {
    kids.push(el('ul', { class: 'dn-bld-import-report' }, _importReport.map((r) => el('li', {
      class: 'dn-bld-import-line ' + (r.ok ? 'dn-bld-import-ok' : 'dn-bld-import-err'),
      text: `line ${r.line}: ${r.ok ? 'ok' : r.error}`,
    }))));
  }
  return el('div', { class: 'dn-bld-import' }, kids);
}

// ── board-editor handlers ─────────────────────────────────────────────

function closeEditState() {
  _editId = null; _editBuffer = null; _editCreate = false;
  _editError = ''; _confirmDeleteEntry = false;
}

// Test-only: reset the shared module state between mounts (mirrors
// ui.js::_resetPendingOverrides). Forces render() to reload config + draft from
// the current fetch mock so a test's own vocab/draft take effect. Not used by
// the app.
export function _resetBuilderForTest() {
  closeEditState();
  _importText = ''; _importReport = []; _briefDraft = null;
  _proposerDirs = []; _confirmReset = false; _undoNote = ''; _replacedNote = '';
  _config = null; _draft = null; _flash = '';
  _active = 'structure'; _busy = false; _suggestions = null;
  for (const k of Object.keys(_provenance)) delete _provenance[k];
  _stripFigureForTest = null;
}

function openEdit(entry) {
  const id = entry.id || entry.entry_id;
  if (_editId === id && !_editCreate) { closeEditState(); _renderCenter(); return; } // toggle closed
  // Seed the editor's heldOut from the entry's OWN tags only — NOT the holdSet,
  // which folds in the hash-derived (rotating, fraction-based) holdout. Passing
  // a hash-held flag here made the serializer stamp a literal `holdout` tag on
  // Save (split.py rule 1: an explicit tag wins), collapsing the whole rotating
  // holdout fraction onto this one entry. The train/holdout toggle stays the
  // ONLY writer of the tag; entryToBuffer reads it from rawTags.
  _editBuffer = entryToBuffer(entry);
  _editId = id; _editCreate = false; _editError = ''; _confirmDeleteEntry = false;
  _renderCenter();
}

function startCreate(kind) {
  _editBuffer = newEntryBuffer(kind);
  _editCreate = true; _editId = null; _editError = ''; _confirmDeleteEntry = false;
  _replacedNote = '';
  _renderCenter();
}

function duplicateEntry() {
  if (!_editBuffer) return;
  const clone = JSON.parse(JSON.stringify(_editBuffer));
  clone.id = ''; // seed a create-mode buffer under a fresh id (replace-by-id never renames)
  _editBuffer = clone; _editCreate = true; _editId = null; _editError = ''; _confirmDeleteEntry = false;
  _renderCenter();
}

// Save posts the WHOLE buffer through the existing edit_board_entry op; a
// server ValueError renders verbatim in the editor's inline strip (never the
// global flash), and the editor stays open so the operator can fix it.
async function saveEntry() {
  if (_busy || !_editBuffer) return;
  const wasCreate = _editCreate;
  _busy = true; _editError = ''; _renderCenter();
  try {
    const env = await postOp('edit_board_entry', { entry: bufferToEntryJson(_editBuffer) });
    if (env && env.error) { _editError = env.error; }
    else if (env) {
      // A create-mode Save whose id already existed REPLACED it in place (no
      // rename on the id-matched op). Surface that verbatim so a fat-fingered
      // duplicate-id create is never a silent clobber (F6 — no confirm gate).
      const changed = (env.patch && env.patch.changed) || {};
      _replacedNote = (wasCreate && changed.action === 'replaced')
        ? 'replaced existing entry ' + (changed.entry_id || '') : '';
      closeEditState(); applyOpResult(env);
    }
  } catch (err) {
    _editError = (err && err.message) || String(err);
  } finally {
    _busy = false;
    _renderCenter();
  }
}

function onDeleteEntry() {
  if (!_confirmDeleteEntry) { _confirmDeleteEntry = true; _renderCenter(); return; }
  _confirmDeleteEntry = false;
  if (_editId != null) deleteEntry(_editId);
}

async function deleteEntry(id) {
  if (_busy) return;
  _busy = true; _editError = ''; _renderCenter();
  try {
    const env = await postOp('remove_board_entry', { entry_id: id });
    if (env && env.error) { _editError = env.error; }
    else if (env) { closeEditState(); applyOpResult(env); }
  } catch (err) {
    _editError = (err && err.message) || String(err);
  } finally {
    _busy = false;
    _renderCenter();
  }
}

async function runImport() {
  if (_busy) return;
  const lines = String(_importText || '').split('\n').map((l) => l.trim());
  const report = [];
  _busy = true; _renderCenter();
  let lineNo = 0;
  for (const raw of lines) {
    lineNo += 1;
    if (!raw) continue;
    let obj;
    try { obj = JSON.parse(raw); } catch (e) {
      report.push({ line: lineNo, ok: false, error: 'invalid JSON: ' + ((e && e.message) || 'parse error') });
      continue;
    }
    try {
      let env;
      if (obj && obj.board_meta === true) {
        env = await postOp('set_board_meta', {
          disable_drift: Array.isArray(obj.disable_drift) ? obj.disable_drift : [],
          judge_only: !!obj.judge_only,
        });
      } else {
        env = await postOp('edit_board_entry', { entry: obj });
      }
      if (env && env.error) report.push({ line: lineNo, ok: false, error: env.error });
      else { report.push({ line: lineNo, ok: true }); if (env) applyOpResult(env); }
    } catch (err) {
      report.push({ line: lineNo, ok: false, error: (err && err.message) || String(err) });
    }
  }
  _importReport = report;
  _busy = false;
  _renderCenter();
}

// ── Overfitting — the full anti-board-memorization contract ───────────
//
// The extended holdout knobs (all through the one set_holdout op): the
// Ladder/Thresholdout governor, holdout rotation, the split floor, the
// proposer-visibility restriction, and the placebo cadence.
function overfittingSection(d) {
  const of = ((d.scoring || {}).overfitting) || {};
  const ladder = of.ladder || {};
  const rows = [
    controlRow('Overfitting guard', {
      title: 'overfitting.enabled', def: 'on',
      body: 'Master switch for the train/holdout machinery. Off, no holdout is ever derived (an explicit per-entry holdout tag still wins) and the loop behaves as if the guard never existed.',
    }, checkInput(of.enabled !== false, 'Overfitting guard enabled', 'train/holdout split on',
      (on) => runOp('set_holdout', { enabled: on }))),
    controlRow('Split floor (min board size)', {
      title: 'min_board_size_for_split', def: '6',
      body: 'The smallest board at which the hash-derived holdout is attempted. Below it the holdout is empty (small boards are never starved of train entries); an explicit per-entry holdout tag overrides the floor.',
    }, numInput(of.min_board_size_for_split != null ? of.min_board_size_for_split : 6,
      { min: '0', step: '1', 'aria-label': 'Min board size for split' },
      (n) => runOp('set_holdout', { min_board_size_for_split: n }), { int: true })),
    controlRow('Rotate holdout', {
      title: 'rotate_holdout', def: 'on',
      body: 'Rotates the hash-derived holdout slice each epoch (the epoch id seeds the split), so no fixed slice is mined forever. Stable within an epoch; an explicit holdout tag is never rotated.',
    }, checkInput(of.rotate_holdout !== false, 'Rotate holdout', 'rotate the holdout slice each epoch',
      (on) => runOp('set_holdout', { rotate_holdout: on }))),
    controlRow('Max generations per contract', {
      title: 'max_generations_per_contract', def: '0 (no ceiling)',
      body: 'The board-refresh ceiling: after this many generations settle under ONE contract hash, the loop stops proposing against the stale board until the operator rolls the contract — a hard cap on how long a board is mined before it must be refreshed. ASYMMETRY: the op reserves None for "leave unchanged", so this form always sends an explicit integer, and 0 CLEARS the ceiling (unlimited generations). Set 0 to remove any cap; a positive value to impose one.',
    }, numInput(of.max_generations_per_contract != null ? of.max_generations_per_contract : 0,
      { min: '0', step: '1', 'aria-label': 'Max generations per contract' },
      (n) => runOp('set_holdout', { max_generations_per_contract: n }), { int: true })),
    controlRow('Restrict proposer visibility', {
      title: 'restrict_proposer_visibility', def: 'on',
      body: 'Sanitises the proposer prompt at the render boundary: per-entry identities aggregate to counts/rates and experiment-memory deltas coarsen to improved/flat/regressed bands, so the proposer cannot memorise individual board entries.',
    }, checkInput(of.restrict_proposer_visibility !== false, 'Restrict proposer visibility',
      'band / aggregate what the proposer sees',
      (on) => runOp('set_holdout', { restrict_proposer_visibility: on }))),
    controlRow('Placebo cadence (rounds)', {
      title: 'random_baseline_every_n', def: '0 (off)',
      body: 'Every Nth round, field ONE extra challenger whose patch is a semantics-preserving no-op. The gate MUST reject it — a promoted placebo is the alarm that gate discrimination is broken and recent wins are suspect. The gate-discrimination control arm; costs one extra challenger per N rounds.',
    }, numInput(of.random_baseline_every_n != null ? of.random_baseline_every_n : 0,
      { min: '0', step: '1', 'aria-label': 'Random baseline every N rounds' },
      (n) => runOp('set_holdout', { random_baseline_every_n: n }), { int: true })),
    controlRow('Ladder governor', {
      title: 'ladder.enabled', def: 'on',
      body: 'The Ladder/Thresholdout governor over the holdout query: a new holdout signal is released only when the train-measured improvement clears the threshold, and each query charges a finite per-epoch budget. What keeps a reused holdout valid under an adaptive proposer.',
    }, checkInput(ladder.enabled !== false, 'Ladder governor enabled', 'Ladder/Thresholdout holdout governor',
      (on) => runOp('set_holdout', { ladder: { enabled: on } }))),
    controlRow('Ladder release threshold', {
      title: 'ladder.threshold', def: 'auto (derive from promote_margin)',
      body: 'The TRAIN-improvement bar the Ladder release rule applies before a holdout signal is released at all. Auto derives it from promote_margin so the Ladder reuses the gate\'s own noise threshold; pin a float to widen the band independently. Distinct from holdout_margin, which bounds the confirmation AFTER release — raising this one WITHHOLDS the query instead, leaving a train promote unconfirmed. The op reads null in the ladder mapping as the real "auto" value, so a NEGATIVE value here posts that reset; -1 is the shown auto state.',
    }, numInput(ladder.threshold != null ? ladder.threshold : -1,
      { min: '-1', step: '0.01', 'aria-label': 'Ladder release threshold' },
      (n) => runOp('set_holdout', { ladder: { threshold: n < 0 ? null : n } }))),
    controlRow('Ladder query budget', {
      title: 'ladder.budget', def: '16',
      body: 'Per-epoch holdout-query budget. Each round that consults the holdout charges one; exhausted, no further holdout signals are released (the loop degrades to champion-stands). The finite budget is what keeps a reused holdout statistically valid under an adaptive proposer.',
    }, numInput(ladder.budget != null ? ladder.budget : 16,
      { min: '0', step: '1', 'aria-label': 'Ladder budget' },
      (n) => runOp('set_holdout', { ladder: { budget: n } }), { int: true })),
    controlRow('Ladder noise scale', {
      title: 'ladder.noise_scale', def: '0 (parameter-free Ladder)',
      body: 'Width of the noise band added to the Ladder release threshold. 0 is the parameter-free Ladder; reserved for DP-grade noise calibration.',
    }, numInput(ladder.noise_scale != null ? ladder.noise_scale : 0,
      { min: '0', step: '0.01', 'aria-label': 'Ladder noise scale' },
      (n) => runOp('set_holdout', { ladder: { noise_scale: n } }))),
  ];
  return section('Overfitting',
    el('p', { class: 'dn-lede', text: 'Anti-board-memorization: the train/holdout machinery, its Ladder query budget, holdout rotation, what the proposer may see, and the placebo control arm. Every knob is contract — a change rolls the epoch.' }),
    ...rows);
}

// ── Weights — the loss-shaping + multi-objective coefficients ─────────
//
// The capability tiers for the telemetry-dialect select's quiet caption —
// the honest-tiers wording sourced verbatim in spirit from
// TELEMETRY-DIALECTS.md §2 / §3.3 / §4 (never invented here).
const DIALECT_TIERS = {
  goldfive: 'goldfive — the full drift-instrument stream: in-process drift instruments, custom process-judge drift, and emulator introspection. The most powerful dialect.',
  adk_events: 'adk_events — no in-process drift instruments and no custom process-judge drift: sees behaviour (tools, transfers, errors), never reasoning; recovers the failure / cost / loop envelope.',
  transcript: 'transcript — the floor: no telemetry at all, the drift term structurally zero; scoring degrades to predicates + optional in-run judges only.',
};

// The telemetry-dialect control: a closed three-token <select> (the PRODUCER
// that reduces raw telemetry into the LossProfile) plus one quiet caption line
// stating the selected dialect's capability tier. Mirrors the monotonicity
// scope select idiom (attribute-mirrored value so the mock DOM + browser agree).
function telemetryDialectControl(sc) {
  const cur = sc.telemetry_dialect || 'goldfive';
  const sel = el('select', { class: 'dn-bld-select', 'aria-label': 'Telemetry dialect' }, [
    el('option', { value: 'goldfive', text: 'goldfive — full drift-instrument stream (default)' }),
    el('option', { value: 'adk_events', text: 'adk_events — agent event-log JSONL' }),
    el('option', { value: 'transcript', text: 'transcript — predicate/judge-only floor' }),
  ]);
  sel.value = cur;
  sel.setAttribute('value', cur);
  sel.addEventListener('change', () => {
    const v = sel.value != null ? sel.value : sel.getAttribute('value');
    if (v === 'goldfive' || v === 'adk_events' || v === 'transcript') runOp('set_telemetry_dialect', { dialect: v });
  });
  const caption = el('p', { class: 'dn-faint dn-bld-dialect-tier', text: DIALECT_TIERS[cur] || DIALECT_TIERS.goldfive });
  return el('div', { class: 'dn-bld-dialect' }, [sel, caption]);
}

// set_weights had NO GUI before this section: the scalar's drift/pass
// coefficients plus the per-namespace multi-objective weights (through the
// dedicated set_namespace_weights op) and the opt-in parsimony term.
function weightsSection(d) {
  const sc = d.scoring || {};
  const ns = sc.namespace_weights || {};
  const vocab = (_config && _config.vocab) || {};
  const rows = [
    controlRow('Telemetry dialect', {
      title: 'telemetry_dialect', def: 'goldfive',
      body: 'The PRODUCER that reduces a run\'s raw telemetry into the LossProfile the scalar scores. goldfive consumes the full drift-instrument stream; adk_events reduces a generic agent event-log JSONL (no in-process drift instruments, no custom process-judge drift); transcript is the predicate/judge-only floor with a structurally zero drift term. A contract field — changing it selects champions under a different measurement rule and rolls the epoch.',
    }, telemetryDialectControl(sc)),
    controlRow('Drift weight', {
      title: 'drift_weight', def: '1.0',
      body: 'Coefficient on the aggregated drift-loss term of the scalar.',
    }, numInput(sc.drift_weight != null ? sc.drift_weight : 1,
      { step: '0.1', 'aria-label': 'Drift weight' },
      (n) => runOp('set_weights', { drift_weight: n }))),
    controlRow('Pass weight', {
      title: 'pass_weight', def: '1.0',
      body: 'Coefficient on the (1 − pass_rate) miss term of the scalar.',
    }, numInput(sc.pass_weight != null ? sc.pass_weight : 1,
      { step: '0.1', 'aria-label': 'Pass weight' },
      (n) => runOp('set_weights', { pass_weight: n }))),
    controlRow('Default judge weight', {
      title: 'default_judge_weight', def: '1.0',
      body: 'The weight a process judge folds into the loss with when it is not named in per_judge_weights. The baseline every judge inherits.',
    }, numInput(sc.default_judge_weight != null ? sc.default_judge_weight : 1,
      { step: '0.1', 'aria-label': 'Default judge weight' },
      (n) => runOp('set_weights', { default_judge_weight: n }))),
    controlRow('Plan-revision weight', {
      title: 'plan_revision_weight', def: '0.5',
      body: 'Coefficient on the plan-revision drift term (how much the agent rewrote its own plan mid-run) in the scalar.',
    }, numInput(sc.plan_revision_weight != null ? sc.plan_revision_weight : 0.5,
      { step: '0.1', 'aria-label': 'Plan revision weight' },
      (n) => runOp('set_weights', { plan_revision_weight: n }))),
    controlRow('Runtime weight', {
      title: 'runtime_weight', def: '0.0',
      body: 'Coefficient on the wall-clock runtime term of the scalar — opt-in pressure toward faster runs (0 keeps runtime untracked in the loss).',
    }, numInput(sc.runtime_weight != null ? sc.runtime_weight : 0,
      { step: '0.1', 'aria-label': 'Runtime weight' },
      (n) => runOp('set_weights', { runtime_weight: n }))),
    controlRow('Diff-complexity weight', {
      title: 'diff_complexity_weight', def: '0 (term absent)',
      body: 'Opt-in MDL/parsimony coefficient: adds weight × (added + removed + patches) to the challenger scalar, biasing selection toward the smaller, more general edit (a shorter-description edit provably overfits the board less). 0 keeps the term exactly absent. Applies on the full gauntlet A/B path only (racing/swiss/elim matchups score without a diff term).',
    }, numInput(sc.diff_complexity_weight != null ? sc.diff_complexity_weight : 0,
      { min: '0', step: '0.001', 'aria-label': 'Diff complexity weight' },
      (n) => runOp('set_namespace_weights', { diff_complexity_weight: n }))),
    controlRow('Diff-complexity ceiling', {
      title: 'diff_complexity_ceiling', def: '0 (off)',
      body: 'Opt-in parsimony CEILING paired with the weight above: a hard gate rule that REJECTS any challenger whose diff complexity (added + removed + patches) exceeds this value, regardless of how much it improved. 0 keeps the ceiling off (never consulted). Applies on the full gauntlet A/B path only, like the weight above.',
    }, numInput(sc.diff_complexity_ceiling != null ? sc.diff_complexity_ceiling : 0,
      { min: '0', step: '1', 'aria-label': 'Diff complexity ceiling' },
      (n) => runOp('set_namespace_weights', { diff_complexity_ceiling: n }))),
  ];

  // ── severity_weights — FIXED rows from vocab.severities (→ set_weights) ──
  const sevKeys = Array.isArray(vocab.severities) ? vocab.severities : [];
  const sev = sc.severity_weights || {};
  const sevRows = mappingNumRows(sevKeys, sev, {
    def: 1, step: '0.1',
    labelFor: (k) => 'Severity ' + k,
    ariaFor: (k) => 'Severity weight ' + k,
    infoFor: (k) => ({
      title: 'severity_weights["' + k + '"]', def: '1.0',
      body: 'Multiplier on every drift observation of ' + k + ' severity before it folds into the loss. Raises or lowers how much a ' + k + '-severity finding costs a challenger. Edits post the whole severity_weights mapping.',
    }),
  }, (mapping) => runOp('set_weights', { severity_weights: mapping }));

  // ── per_kind_weights — FIXED rows from vocab.kinds (→ set_weights) ───────
  const kindKeys = Array.isArray(vocab.kinds) ? vocab.kinds : [];
  const perKind = sc.per_kind_weights || {};
  const kindRows = mappingNumRows(kindKeys, perKind, {
    def: 1, step: '0.1',
    labelFor: (k) => 'Kind ' + k,
    ariaFor: (k) => 'Per-kind weight ' + k,
    infoFor: (k) => ({
      title: 'per_kind_weights["' + k + '"]', def: '1.0',
      body: 'Weight on every ' + k + ' board entry\'s contribution to the aggregate loss (1.0 = neutral). Lets one entry kind pull harder on selection. Edits post the whole per_kind_weights mapping.',
    }),
  }, (mapping) => runOp('set_weights', { per_kind_weights: mapping }));

  // ── per_judge_weights — rows SEEDED from the judges declared on the board
  //    + a free-text add-key row (→ set_weights) ───────────────────────────
  const perJudge = sc.per_judge_weights || {};
  const board = Array.isArray(d.board) ? d.board : [];
  const judgeNames = [];
  const seen = new Set();
  for (const b of board) {
    for (const j of (Array.isArray(b.judges) ? b.judges : [])) {
      const name = j && j.name;
      if (name && !seen.has(name)) { seen.add(name); judgeNames.push(name); }
    }
  }
  for (const key of Object.keys(perJudge)) {
    if (!seen.has(key)) { seen.add(key); judgeNames.push(key); }
  }
  const judgeRows = mappingNumRows(judgeNames, perJudge, {
    def: 1, step: '0.1',
    labelFor: (k) => 'Judge ' + k,
    ariaFor: (k) => 'Per-judge weight ' + k,
    infoFor: (k) => ({
      title: 'per_judge_weights["' + k + '"]', def: 'default_judge_weight',
      body: 'The weight the ' + k + ' process judge folds into the loss with, overriding default_judge_weight for this judge. Seeded from the judges declared on the board. Edits post the whole per_judge_weights mapping.',
    }),
  }, (mapping) => runOp('set_weights', { per_judge_weights: mapping }));
  judgeRows.push(addKeyNumRow({
    placeholder: 'judge name', defVal: 1, step: '0.1',
    ariaKey: 'New per-judge weight name', ariaVal: 'New per-judge weight value',
    ariaBtn: 'Add per-judge weight',
  }, (key, n) => {
    const next = Object.assign({}, perJudge);
    next[key] = n;
    runOp('set_weights', { per_judge_weights: next });
  }));

  // ── namespace_weights — existing rows + the NAMESPACE ADD-KEY row ────────
  const nsKeys = Object.keys(ns);
  const nsRows = nsKeys.map((key) => controlRow('Namespace ' + key, {
    title: 'namespace_weights["' + key + '"]', def: String(ns[key]),
    body: 'Signed coefficient turning this namespace\'s per-run mean into a scalar component. Positive = higher is worse (drift, cost, schema); negative = higher is better (rubric — negation keeps the scalar lower-is-better); zero = tracked but unscored.',
  }, numInput(ns[key], { step: '0.001', 'aria-label': 'Namespace weight ' + key }, (n) => {
    const next = Object.assign({}, ns);
    next[key] = n;
    runOp('set_namespace_weights', { namespace_weights: next });
  })));
  nsRows.push(addKeyNumRow({
    placeholder: 'namespace: (trailing colon)', defVal: 1, step: '0.001',
    ariaKey: 'New namespace weight key', ariaVal: 'New namespace weight value',
    ariaBtn: 'Add namespace weight',
  }, (key, n) => {
    const next = Object.assign({}, ns);
    next[key] = n;
    runOp('set_namespace_weights', { namespace_weights: next });
  }));

  return section('Weights',
    el('p', { class: 'dn-lede', text: 'The loss-shaping coefficients: how drift, misses, and each metric namespace fold into the one scalar a duel compares. Contract fields — a change rolls the epoch.' }),
    ...rows,
    el('h3', { class: 'dn-bld-subhead', text: 'Severity weights' }),
    ...(sevRows.length ? sevRows : [empty('No drift severities in the vocabulary.')]),
    el('h3', { class: 'dn-bld-subhead', text: 'Per-kind weights' }),
    ...(kindRows.length ? kindRows : [empty('No entry kinds in the vocabulary.')]),
    el('h3', { class: 'dn-bld-subhead', text: 'Per-judge weights' }),
    ...judgeRows,
    el('h3', { class: 'dn-bld-subhead', text: 'Namespace weights' }),
    ...nsRows);
}

// The proposer-brief editor: a monospace textarea + an explicit Save (the
// brief is contract, so it commits through set_brief on its own gesture, never
// on every keystroke) with a live char count. The uncommitted text is pinned
// in module state so it survives a digest re-render.
function briefEditor(d) {
  const current = _briefDraft != null ? _briefDraft : (d.brief || '');
  const read = (node) => (node.value != null ? node.value
    : (node.getAttribute('value') != null ? node.getAttribute('value') : (node.textContent || '')));
  const area = el('textarea', { class: 'dn-bld-brief-area dn-mono', 'aria-label': 'Proposer brief', rows: '6', text: current });
  const count = el('span', { class: 'dn-bld-brief-count', text: current.length + ' chars' });
  const commit = () => { _briefDraft = read(area); patchText(count, _briefDraft.length + ' chars'); };
  area.addEventListener('input', commit);
  area.addEventListener('change', commit);
  const save = el('button', { class: 'dn-bld-btn dn-bld-brief-save', type: 'button', text: 'Save brief', 'aria-label': 'Save brief' });
  save.addEventListener('click', () => {
    const text = read(area);
    _briefDraft = null;
    runOp('set_brief', { text });
  });
  return el('div', { class: 'dn-bld-brief' }, [
    el('h3', { class: 'dn-bld-subhead', text: 'Proposer brief' }),
    el('p', { class: 'dn-faint', text: 'The operator’s brief to the proposer for this epoch. Explicit Save writes it (set_brief) — a brief change rolls the epoch.' }),
    area,
    el('div', { class: 'dn-bld-brief-foot' }, [count, save]),
  ]);
}

// The proposer picker: a select over the discovered proposer dirs (from
// /builder/draft's proposer_dirs) + the builtin default + a free-text path row,
// all driving set_proposer. An explicit path outside the scanned set is
// honored verbatim (the scan is a convenience, not a whitelist).
function proposerPicker(d) {
  const cur = d.proposer_path || '';
  const dirs = Array.isArray(_proposerDirs) ? _proposerDirs : [];
  const options = [el('option', { value: '', text: 'builtin default (skill-composed)' })];
  const known = new Set();
  for (const dir of dirs) {
    options.push(el('option', { value: dir.path, text: dir.name + ' — ' + dir.path }));
    known.add(dir.path);
  }
  // list the current path even if the scan didn't find it, so the select never
  // silently drops the live value.
  if (cur && !known.has(cur)) options.push(el('option', { value: cur, text: cur + ' (current)' }));
  const sel = el('select', { class: 'dn-bld-select dn-bld-proposer-pick', 'aria-label': 'Proposer dir' }, options);
  sel.value = cur;
  sel.setAttribute('value', cur);
  sel.addEventListener('change', () => {
    const v = sel.value != null ? sel.value : sel.getAttribute('value');
    runOp('set_proposer', { proposer_path: v ? v : null });
  });
  const pathIn = el('input', {
    class: 'dn-bld-text dn-bld-proposer-path', type: 'text',
    placeholder: '/path/to/proposer', value: cur, 'aria-label': 'Proposer path',
  });
  const setBtn = el('button', { class: 'dn-bld-btn dn-bld-proposer-set', type: 'button', text: 'Set path', 'aria-label': 'Set proposer path' });
  setBtn.addEventListener('click', () => {
    const raw = String(pathIn.value != null ? pathIn.value : (pathIn.getAttribute('value') || '')).trim();
    runOp('set_proposer', { proposer_path: raw ? raw : null });
  });
  return el('div', { class: 'dn-bld-proposer-picker' }, [
    controlRow('Proposer dir', {
      title: 'proposer_path', def: 'builtin default',
      body: 'Point the epoch at a proposer dir (one carrying an agent.py or a skills/ dir), or the builtin skill-composed default. The picker lists dirs discovered under <workspace>/../proposers/; the free-text row below sets any other path. Changing the proposer rolls the epoch.',
    }, sel),
    el('div', { class: 'dn-bld-proposer-freerow' }, [pathIn, setBtn]),
  ]);
}

function proposerSection(d) {
  const p = d.proposer || {};
  const skills = Array.isArray(p.skills) ? p.skills : [];
  const isAgent = !!p.has_custom_agent;
  const pq = ((d.scoring || {}).proposer_quality) || {};
  const em = ((d.scoring || {}).experiment_memory) || {};
  return section('Proposer',
    el('p', { class: 'dn-lede', text: 'Who proposes each challenger: the skill-composed builtin default, a discovered proposer dir, or an explicit path — plus the proposer-quality levers (best-of-N slate, self-critique, cross-epoch memory). Changing the proposer or any lever is a contract edit — it rolls the epoch.' }),
    proposerPicker(d),
    briefEditor(d),
    controlRow('Best-of-N slate', {
      title: 'best_of_n', def: '3',
      body: 'How many candidate experiments each propose-step samples before the critique pass picks one. 1 is the historical single sample (no critique). Each extra sample is an auxiliary propose call, priced on the cost meter; the screen (Field & noise) then tries the slate out.',
    }, numInput(pq.best_of_n != null ? pq.best_of_n : 3,
      { min: '1', step: '1', 'aria-label': 'Best of N' },
      (n) => runOp('set_proposer_quality', { best_of_n: n }), { int: true })),
    controlRow('Self-critique', {
      title: 'critique_enabled', def: 'on',
      body: 'A single cheap auxiliary-LLM pass scores the sampled slate against a quality bar (grounded? targets a real failure mode? minimal diff?) and selects the best. Off, selection falls back to the deterministic smallest-relevant-diff heuristic — no extra LLM call. Inert at best_of_n 1.',
    }, checkInput(pq.critique_enabled !== false, 'Critique enabled', 'auxiliary self-critique selects from the slate',
      (on) => runOp('set_proposer_quality', { critique_enabled: on }))),
    controlRow('Process exemplars', {
      title: 'process_exemplars', def: '0 (off)',
      body: 'Opt-in: show the proposer up to N mechanically-REDACTED event windows per round (how a detected failure unfolds — no entry ids, no task text, no model outputs). Read-side only — free on the cost meter — but it widens the proposer-visibility channel, so enable it only under the harm-detection runbook in PROCESS-EXEMPLARS.md §5 (watch the generalization_gap finding; set back to 0 if it widens while train improves).',
    }, numInput(pq.process_exemplars != null ? pq.process_exemplars : 0,
      { min: '0', step: '1', 'aria-label': 'Process exemplars' },
      (n) => runOp('set_proposer_quality', { process_exemplars: n }), { int: true })),
    controlRow('Recombination slot', {
      title: 'recombine', def: 'off',
      body: 'Opt-in: when best-of-N > 1, the last slate slot mints the patch union of two rejected complementary challengers instead of sampling the LLM — so a single winner can capture two fixes a parsimony-biased selector would each discount. Requires best_of_n > 1 to have any effect; cost-neutral (the mint replaces that slot\'s propose call, never adds one). Inert at best_of_n 1.',
    }, checkInput(!!pq.recombine, 'Recombination slot', 'mint the union of two rejected complementary fixes into the last slate slot',
      (on) => runOp('set_proposer_quality', { recombine: on }))),
    controlRow('LLM-guided merge', {
      title: 'recombine_merge', def: 'mechanical',
      body: 'How the recombination slot composes the union. Off (mechanical): the last slot mints the concatenation of two DISJOINT patch sets with no LLM call — cost-neutral (best_of_n − 1 calls). On (llm): the slot issues ONE merge call instead, and disjointness RELAXES so two rejected fixes that OVERLAP on a mutation point can be merged (the model resolves the overlap a blind concatenation would drop) — the merge substitutes the slot\'s own sample call, so it costs exactly a recombine-off round. Only meaningful with the recombination slot on; on rolls the epoch.',
    }, checkInput(pq.recombine_merge === 'llm', 'LLM-guided merge', 'compose the union with an LLM merge call (relaxes disjointness for overlapping pairs) instead of a mechanical patch concatenation',
      (on) => runOp('set_proposer_quality', { recombine_merge: on ? 'llm' : 'mechanical' }))),
    controlRow('Genealogy channel', {
      title: 'genealogy', def: '0 (off)',
      body: 'Opt-in: show the proposer up to N candidate-lineage items per round — the champion\'s promoted patch history (build on what worked) plus diverse rejected reign candidates (re-frame a different idea), each with a BANDED outcome (improved / flat / regressed) and a capped excerpt of the proposer\'s own diff. Lets the proposer evolve IN CONTEXT (the in-context analogue of the recombination slot). Envelope-safe — candidate genealogy, never board data: no entry ids, no per-entry results, no exact deltas, nothing holdout-derived. Read-side only — free on the cost meter. Non-zero rolls the epoch.',
    }, numInput(pq.genealogy != null ? pq.genealogy : 0,
      { min: '0', step: '1', 'aria-label': 'Genealogy' },
      (n) => runOp('set_proposer_quality', { genealogy: n }), { int: true })),
    controlRow('Calibration feedback', {
      title: 'calibration_feedback', def: '0 (off)',
      body: 'Opt-in: show the proposer up to N of its own RECENT graded hypotheses per round — how its falsifiable movement predictions landed against realized outcomes. Renders per-claim-type hit / miss / unresolved counts, the overall calibration fraction (its own self-accuracy), and each recent claim as its core idea + a BANDED outcome (improved / flat / regressed) + hit/miss. A proposer shown its own miss pattern hypothesizes more honestly. Envelope-safe — the proposer\'s own claim text + aggregate counts, never board data: no entry ids, no per-entry results, no exact deltas, nothing holdout-derived. Read-side only — free on the cost meter. Non-zero rolls the epoch.',
    }, numInput(pq.calibration_feedback != null ? pq.calibration_feedback : 0,
      { min: '0', step: '1', 'aria-label': 'Calibration feedback' },
      (n) => runOp('set_proposer_quality', { calibration_feedback: n }), { int: true })),
    controlRow('Cross-epoch memory', {
      title: 'experiment_memory.cross_epoch', def: 'off',
      body: 'Opts settled experiments from PRIOR epochs that share the current contract hash into the proposer\'s digest — banded, clearly separated, and only in the budget left after same-epoch history. Different-contract experiments are never surfaced.',
    }, checkInput(!!em.cross_epoch, 'Cross-epoch experiment memory', 'surface settled prior-epoch experiments (same contract hash)',
      (on) => runOp('set_experiment_memory', { cross_epoch: on }))),
    el('div', { class: 'dn-bld-panel' }, [
      el('div', { class: 'dn-bld-kv' }, [
        el('span', { class: 'dn-bld-k', text: 'mode' }),
        el('span', { class: 'dn-bld-v', text: isAgent ? 'custom ADK agent' : 'skill-composed default' }),
      ]),
      el('div', { class: 'dn-bld-kv' }, [
        el('span', { class: 'dn-bld-k', text: 'agent id' }),
        el('span', { class: 'dn-bld-v dn-mono', text: p.agent_id || '—' }),
      ]),
      el('div', { class: 'dn-bld-kv' }, [
        el('span', { class: 'dn-bld-k', text: 'tools' }),
        el('span', { class: 'dn-bld-v', text: (Array.isArray(p.tools) && p.tools.length) ? p.tools.join(', ') : '—' }),
      ]),
      skills.length
        ? el('ul', { class: 'dn-bld-skills' }, skills.map((s) => el('li', null, [
            el('span', { class: 'dn-bld-skill-name', text: s.name }),
            s.description ? el('span', { class: 'dn-bld-skill-desc', text: ' · ' + s.description }) : null,
          ].filter(Boolean))))
        : el('p', { class: 'dn-faint', text: 'No composed skills.' }),
    ]));
}

// The per-namespace strict-monotonicity gate mapping editor: a checkbox per
// existing namespace + a text-key/bool add-key row, each committing the WHOLE
// namespace_monotonicity mapping through set_gate (the op replaces it
// wholesale). Closes the namespace_monotonicity GUI gap.
function namespaceMonoPanel(sc) {
  const cur = sc.namespace_monotonicity || {};
  const rows = Object.keys(cur).map((key) => controlRow('Namespace ' + key, {
    title: 'namespace_monotonicity["' + key + '"]', def: String(!!cur[key]),
    body: 'When on, the ' + key + ' namespace\'s per-run mean may not regress on a promotion (a strict-monotonicity gate scoped to this namespace). Edits post the whole namespace_monotonicity mapping.',
  }, checkInput(!!cur[key], 'Namespace monotonicity ' + key, key + ' may not regress', (on) => {
    const next = Object.assign({}, cur);
    next[key] = on;
    runOp('set_gate', { namespace_monotonicity: next });
  })));
  const keyIn = el('input', {
    class: 'dn-bld-text dn-bld-addkey-k', type: 'text',
    placeholder: 'namespace: (trailing colon)', 'aria-label': 'New namespace monotonicity key',
  });
  const box = el('input', { class: 'dn-bld-check dn-bld-addkey-v', type: 'checkbox', 'aria-label': 'New namespace monotonicity value' });
  box.setAttribute('checked', 'checked'); // default strict (may not regress)
  const btn = el('button', { class: 'dn-bld-btn dn-bld-addkey-btn', type: 'button', text: 'Add', 'aria-label': 'Add namespace monotonicity key' });
  btn.addEventListener('click', () => {
    const k = String(keyIn.value != null ? keyIn.value : (keyIn.getAttribute('value') || '')).trim();
    if (!k) return;
    const on = box.checked != null ? box.checked : (box.getAttribute('checked') != null);
    const next = Object.assign({}, cur);
    next[k] = !!on;
    runOp('set_gate', { namespace_monotonicity: next });
  });
  const addRow = el('div', { class: 'dn-bld-addkeyrow' }, [
    keyIn, el('label', { class: 'dn-bld-checkwrap' }, [box, el('span', { text: 'may not regress' })]), btn,
  ]);
  return el('div', { class: 'dn-bld-nsmono' }, [
    el('h3', { class: 'dn-bld-subhead', text: 'Namespace monotonicity' }),
    el('p', { class: 'dn-faint', text: 'Per-namespace strict-monotonicity gates: each listed namespace\'s mean may not regress on a promotion. A change posts the whole mapping and rolls the epoch.' }),
    ...(rows.length ? rows : [empty('No per-namespace monotonicity gates set.')]),
    addRow,
  ]);
}

function gateSection(d) {
  const sc = d.scoring || {};
  const margin = el('input', {
    class: 'dn-bld-num', type: 'number', value: String(sc.promote_margin != null ? sc.promote_margin : 0),
    step: '0.01', 'aria-label': 'Promote margin',
  });
  margin.addEventListener('change', () => {
    const n = Number(margin.value != null ? margin.value : margin.getAttribute('value'));
    if (isFinite(n)) runOp('set_gate', { promote_margin: n });
  });
  const mono = el('input', { class: 'dn-bld-check', type: 'checkbox', 'aria-label': 'Pass-rate monotonicity' });
  if (sc.pass_rate_monotonicity) mono.setAttribute('checked', 'checked');
  mono.addEventListener('change', () => {
    const on = mono.checked != null ? mono.checked : (mono.getAttribute('checked') != null);
    runOp('set_gate', { monotonicity: !!on });
  });
  // the monotonicity SCOPE — a closed two-token select (per_entry / aggregate).
  const curScope = sc.pass_rate_monotonicity_scope || 'per_entry';
  const scope = el('select', { class: 'dn-bld-select', 'aria-label': 'Monotonicity scope' }, [
    el('option', { value: 'per_entry', text: 'per entry — no champion-passed entry may flip' }),
    el('option', { value: 'aggregate', text: 'aggregate — only the overall pass-rate may not drop' }),
  ]);
  scope.value = curScope;
  scope.setAttribute('value', curScope);
  scope.addEventListener('change', () => {
    const v = scope.value != null ? scope.value : scope.getAttribute('value');
    if (v === 'per_entry' || v === 'aggregate') runOp('set_gate', { monotonicity_scope: v });
  });
  // the regression-suite pre-gate: enable + argv + timeout.
  const regCmd = el('input', {
    class: 'dn-bld-text', type: 'text', 'aria-label': 'Regression test command',
    value: Array.isArray(sc.regression_test_command) ? sc.regression_test_command.join(' ') : 'pytest tests/ -q',
  });
  regCmd.addEventListener('change', () => {
    const raw = String(regCmd.value != null ? regCmd.value : regCmd.getAttribute('value') || '');
    const argv = raw.split(/\s+/).filter(Boolean);
    if (argv.length) runOp('set_gate', { regression_test_command: argv });
  });
  return section('Promote gate',
    el('p', { class: 'dn-lede', text: 'What a challenger must clear to dethrone the champion and promote.' }),
    controlRow('Promote margin', {
      title: 'Promote margin', def: '0.0',
      body: 'The minimum scalar improvement (champion loss − challenger loss) a challenger must clear to promote. A larger margin demands a more decisive win and resists noise; 0 promotes on any improvement. Must clear the measured A/A noise floor when the evidence gate is off — run the preflight (Review) to measure it.',
    }, margin),
    controlRow('Holdout margin', {
      title: 'holdout_margin', def: 'auto (reuse promote_margin)',
      body: 'The scalar tolerance the HOLDOUT confirmation applies, separate from the train-side promote margin. The holdout is the smaller slice by construction, so its scalar moves in coarser 1/N steps and needs the WIDER bound: roughly promote_margin x N_train / N_holdout (about twice promote_margin on the default 0.3 split). ASYMMETRY: the op reserves None for "leave unchanged", so a NEGATIVE value here resets the field to auto (reuse promote_margin); -1 is the shown auto state.',
    }, numInput(sc.holdout_margin != null ? sc.holdout_margin : -1,
      { min: '-1', step: '0.01', 'aria-label': 'Holdout margin' },
      (n) => runOp('set_gate', { holdout_margin: n }))),
    controlRow('Holdout entry regression budget', {
      title: 'holdout_entry_regression_budget', def: '0 (zero tolerance)',
      body: 'How many holdout entries may regress before the confirmation rejects. 0 is the historical zero-tolerance rule; on a small noisy holdout ONE entry flipping pass to fail rejects at every margin, because that rejection never came from the scalar bound — this is the knob that rule never had. Holdout-only: the train side keeps zero tolerance.',
    }, numInput(sc.holdout_entry_regression_budget != null ? sc.holdout_entry_regression_budget : 0,
      { min: '0', step: '1', 'aria-label': 'Holdout entry regression budget' },
      (n) => runOp('set_gate', { holdout_entry_regression_budget: n }), { int: true })),
    controlRow('Pass-rate monotonicity', {
      title: 'Pass-rate monotonicity', def: 'off',
      body: 'When on, a challenger may not regress the board pass-rate even if its weighted loss improves — every predicate the champion passed must still pass. Guards against trading a hard-pass away for an average-loss gain.',
    }, el('label', { class: 'dn-bld-checkwrap' }, [mono, el('span', { text: 'require non-regressing pass-rate' })])),
    controlRow('Monotonicity scope', {
      title: 'pass_rate_monotonicity_scope', def: 'per_entry',
      body: 'Granularity of the pass-rate check when it is on. per_entry rejects if ANY champion-passed entry flips to fail (right for invariant / regression-suite boards); aggregate rejects only when the overall pass-rate drops (right for sampled boards where one noisy flip should not veto a strictly-better challenger).',
    }, scope),
    namespaceMonoPanel(sc),
    controlRow('Block on containment violation', {
      title: 'block_on_containment_violation', def: 'off (alarm-only)',
      body: 'Before finalizing a gate-decided promotion, re-check diff containment (files outside the mutable trees must be byte-identical) and REJECT a violating child instead of promoting with an alarm. Fail-open on an unreadable snapshot; an explicit operator force-promote is never blocked.',
    }, checkInput(!!sc.block_on_containment_violation, 'Block on containment violation', 'reject instead of alarm',
      (on) => runOp('set_gate', { block_on_containment_violation: on }))),
    controlRow('Block on gate contradiction', {
      title: 'block_on_gate_contradiction', def: 'off (alarm-only)',
      body: 'Re-derive the gate\'s scalar rule immediately before finalizing a promotion and REFUSE on contradiction (instead of persisting and letting the supervisor\'s out-of-band scan raise the alarm). Fail-open when there is no usable scalar evidence.',
    }, checkInput(!!sc.block_on_gate_contradiction, 'Block on gate contradiction', 'refuse contradictory promotions',
      (on) => runOp('set_gate', { block_on_gate_contradiction: on }))),
    controlRow('Regression-suite gate', {
      title: 'regression_gate_enabled', def: 'off',
      body: 'Run the snapshot\'s own test suite BEFORE the scoring gate; a non-passing (or timed-out) suite hard-rejects the candidate regardless of scalar movement. Needs the snapshot to actually ship a suite.',
    }, checkInput(!!sc.regression_gate_enabled, 'Regression gate enabled', 'run the snapshot test suite as a pre-gate',
      (on) => runOp('set_gate', { regression_gate_enabled: on }))),
    controlRow('Regression command', {
      title: 'regression_test_command', def: 'pytest tests/ -q',
      body: 'The argv used to invoke the regression suite (whitespace-split). Override for non-pytest suites, e.g. "python -m unittest discover".',
    }, regCmd),
    controlRow('Regression timeout (s)', {
      title: 'regression_timeout_s', def: '600',
      body: 'Wall-clock seconds the regression subprocess may take before it is killed; a timeout counts as a regression failure.',
    }, numInput(sc.regression_timeout_s != null ? sc.regression_timeout_s : 600,
      { min: '1', step: '1', 'aria-label': 'Regression timeout seconds' },
      (n) => runOp('set_gate', { regression_timeout_s: n }), { int: true })));
}

function reviewSection(d) {
  const diff = _diff || { changed_components: [], rolls_epoch: false };
  const changed = diff.changed_components || [];
  const dry = el('button', { class: 'dn-bld-btn dn-bld-btn-dry', type: 'button', text: 'Dry-run preview' });
  const apply = el('button', { class: 'dn-bld-btn dn-bld-btn-apply', type: 'button', text: 'Apply (roll epoch)' });
  const out = el('div', { class: 'dn-bld-applyout', 'aria-live': 'polite' });

  dry.addEventListener('click', async () => { await doApply(false, out); });
  apply.addEventListener('click', async () => {
    if (!_confirmApply) {
      _confirmApply = true;
      patchText(apply, 'Confirm — this rolls the epoch');
      apply.classList.add('dn-bld-btn-confirm');
      return;
    }
    _confirmApply = false;
    apply.classList.remove('dn-bld-btn-confirm');
    patchText(apply, 'Apply (roll epoch)');
    await doApply(true, out);
  });

  return section('Review & apply',
    el('p', { class: 'dn-lede', text: 'Applying writes the contract; the auto-epoch machinery then rolls the epoch on the next resolve. Preview first with a dry run.' }),
    el('div', { class: 'dn-bld-panel' }, [
      el('div', { class: 'dn-bld-kv' }, [
        el('span', { class: 'dn-bld-k', text: 'rolls epoch' }),
        el('span', { class: 'dn-bld-v', text: diff.rolls_epoch ? 'yes' : 'no (no contract change)' }),
      ]),
      el('div', { class: 'dn-bld-kv' }, [
        el('span', { class: 'dn-bld-k', text: 'changed' }),
        el('span', { class: 'dn-bld-v', text: changed.length ? changed.join(', ') : 'nothing' }),
      ]),
    ]),
    preflightPanel(),
    refuseWarningsPanel(),
    comparePanel(),
    el('div', { class: 'dn-bld-applyrow' }, [dry, apply]),
    out);
}

// ── draft compare (the fork/compare lifecycle, Review pane) ────────────
//
// Two operand selects (the working draft, the live contract, or any fork
// slot) + a keyed diff: differing contract-canonical scoring keys with both
// values, board ids added/removed/changed, the brief, the proposer.
function comparePanel() {
  const names = ['session', 'live', ..._drafts];
  const mkSel = (aria, cur, onPick) => {
    const sel = el('select', { class: 'dn-bld-select', 'aria-label': aria },
      names.map((n) => el('option', { value: n, text: n })));
    sel.value = cur;
    sel.setAttribute('value', cur);
    sel.addEventListener('change', () => {
      const v = sel.value != null ? sel.value : sel.getAttribute('value');
      if (v) onPick(v);
    });
    return sel;
  };
  const selA = mkSel('Compare draft A', _cmpA, (v) => { _cmpA = v; });
  const selB = mkSel('Compare draft B', _cmpB, (v) => { _cmpB = v; });
  const btn = el('button', { class: 'dn-bld-btn dn-bld-btn-compare', type: 'button', text: 'Compare' });
  btn.addEventListener('click', () => runOp('compare', { name_a: _cmpA, name_b: _cmpB }));
  const kids = [
    el('div', { class: 'dn-bld-cmp-controls' }, [selA, el('span', { class: 'dn-bld-cmp-vs', text: 'vs' }), selB, btn]),
  ];
  if (_compare) kids.push(compareResult(_compare));
  return el('div', { class: 'dn-bld-cmp' }, [
    el('h3', { class: 'dn-bld-subhead', text: 'Compare drafts' }),
    ...kids,
  ]);
}

function compareResult(cmp) {
  const changed = cmp.changed_components || [];
  if (!changed.length) {
    return el('p', { class: 'dn-faint dn-bld-cmp-same', text: `${cmp.a} and ${cmp.b} describe the same contract.` });
  }
  const rows = [];
  const scoring = cmp.scoring || {};
  for (const key of Object.keys(scoring)) {
    rows.push(el('div', { class: 'dn-bld-cmp-row' }, [
      el('span', { class: 'dn-bld-cmp-key dn-mono', text: key }),
      el('span', { class: 'dn-bld-cmp-a dn-mono', text: fmtCmp(scoring[key].a) }),
      el('span', { class: 'dn-bld-cmp-b dn-mono', text: fmtCmp(scoring[key].b) }),
    ]));
  }
  const board = cmp.board || {};
  for (const [label, ids] of [['added', board.added], ['removed', board.removed], ['changed', board.changed]]) {
    if (Array.isArray(ids) && ids.length) {
      rows.push(el('div', { class: 'dn-bld-cmp-row' }, [
        el('span', { class: 'dn-bld-cmp-key dn-mono', text: 'board.' + label }),
        el('span', { class: 'dn-bld-cmp-b', text: ids.join(', ') }),
      ]));
    }
  }
  if (cmp.brief && cmp.brief.changed) {
    rows.push(el('div', { class: 'dn-bld-cmp-row' }, [
      el('span', { class: 'dn-bld-cmp-key dn-mono', text: 'brief' }),
      el('span', { class: 'dn-bld-cmp-a', text: `${cmp.brief.a_chars} chars` }),
      el('span', { class: 'dn-bld-cmp-b', text: `${cmp.brief.b_chars} chars` }),
    ]));
  }
  if (cmp.proposer && cmp.proposer.changed) {
    rows.push(el('div', { class: 'dn-bld-cmp-row' }, [
      el('span', { class: 'dn-bld-cmp-key dn-mono', text: 'proposer' }),
      el('span', { class: 'dn-bld-cmp-a dn-mono', text: cmp.proposer.a || 'builtin' }),
      el('span', { class: 'dn-bld-cmp-b dn-mono', text: cmp.proposer.b || 'builtin' }),
    ]));
  }
  return el('div', { class: 'dn-bld-cmp-result' }, [
    el('div', { class: 'dn-bld-cmp-row dn-bld-cmp-headrow' }, [
      el('span', { class: 'dn-bld-cmp-key', text: `changed: ${changed.join(', ')}` }),
      el('span', { class: 'dn-bld-cmp-a', text: cmp.a }),
      el('span', { class: 'dn-bld-cmp-b', text: cmp.b }),
    ]),
    ...rows,
  ]);
}

function fmtCmp(v) {
  if (v === undefined) return '—';
  try { return JSON.stringify(v); } catch (e) { return String(v); }
}

// ── the build-time statistical pre-flight (Review pane) ────────────────
//
// A READ measurement, surfaced BEFORE apply: can the draft contract
// out-signal its own noise? The op returns the normal envelope plus a
// `preflight` result; the verdict chip + reasons render from module state
// so a re-render (digest includes `pf`) keeps the last measurement
// visible. Recommend-only — apply is never hard-blocked.

function preflightPanel() {
  const btn = el('button', {
    class: 'dn-bld-btn dn-bld-btn-preflight', type: 'button',
    text: _busy ? 'measuring…' : 'Run preflight',
  });
  btn.addEventListener('click', () => runOp('preflight', {}));
  const kids = [
    el('p', { class: 'dn-lede', text: 'Statistical pre-flight: measures the A/A noise floor and the degradation signal of this draft against the registered target, before any round is spent. Recommend-only.' }),
    el('div', { class: 'dn-bld-applyrow' }, [btn]),
  ];
  if (_preflight) kids.push(preflightVerdict(_preflight));
  return el('div', { class: 'dn-bld-preflight' }, kids);
}

function preflightVerdict(pf) {
  const verdict = pf.available ? (pf.verdict || 'ok') : 'unavailable';
  const chip = el('span', {
    class: 'dn-bld-pf-chip dn-bld-pf-' + verdict,
    text: verdict === 'unavailable' ? 'unavailable' : verdict.toUpperCase(),
  });
  const reasons = [];
  if (!pf.available) {
    reasons.push(pf.reason || 'preflight is unavailable for this workspace');
  } else {
    const r = pf.report || {};
    reasons.push(`noise floor ${fmtSig(r.noise_floor_max_abs_delta)} (max |Δ| over ${r.noise_floor_runs != null ? r.noise_floor_runs : '?'} A/A draws)`);
    // Count only the probes that actually spent a draw: `probed_points` also
    // carries the points dropped for free (no_op_patch / verdict_settled), and
    // counting those would claim a broader sample than was measured. A
    // pre-#106 record carries no list at all, so it reads as the one probe it
    // took.
    const probed = Array.isArray(r.probed_points)
      ? r.probed_points.filter((p) => p && !p.skipped).length
      : 0;
    const n = probed || 1;
    reasons.push(`degradation signal ${fmtSig(r.degradation_signal != null ? r.degradation_signal : r.signal)} (best of ${n} probed point${n === 1 ? '' : 's'}: ${r.degraded_mutation_id || '?'})`);
    if (verdict === 'refuse') reasons.push('the signal is at or below the floor — duels under this contract would be decided by noise');
    if (verdict === 'warn') reasons.push('saturated: every probe scored identically — the board cannot discriminate even a deliberate degradation');
    if (verdict === 'inert') reasons.push('every probed point left the scalar exactly at the champion mean while the A/A draws varied — the signal is UNMEASURED, not zero; pin a point the deliverable depends on');
    if (verdict === 'ok') reasons.push('the measured signal clears the measured floor');
    // The promote_margin window is a separate question from signal-vs-noise.
    // Its upper comparison is against DEGRADATION headroom, which does not
    // bound how far a challenger can improve — say so rather than promising a
    // null run (issue #119).
    if (r.window_failure === 'margin_above_achievable') reasons.push(`promote_margin ${fmtSig(r.promote_margin)} is at or above the measured degradation signal — improvement headroom is unmeasured, so check the margin, but this is not proof nothing can promote`);
    else if (r.window_failure === 'margin_below_floor') reasons.push(`promote_margin ${fmtSig(r.promote_margin)} is inside the measured noise — promotions could not be told from re-rolls`);
    else if (r.window_failure === 'empty_window') reasons.push('the measured signal does not clear the noise floor — no promote_margin is defensible on this board');
    if (r.holdout_note) reasons.push(r.holdout_note);
  }
  // The margin window gets its OWN chip, exactly as `zicato board preflight`
  // prints its own `window:` line. The verdict chip reports signal-vs-noise
  // only, so a draft that clears its floor with an unreachable promote_margin
  // would otherwise head a guaranteed-null contract with a green OK.
  const head = [el('span', { class: 'dn-bld-k', text: 'preflight verdict' }), chip];
  const windowFailure = pf.available ? ((pf.report || {}).window_failure || '') : '';
  if (windowFailure) {
    const windowVerdict = (pf.report || {}).window_verdict === 'refuse' ? 'refuse' : 'warn';
    head.push(el('span', {
      class: 'dn-bld-pf-chip dn-bld-pf-' + windowVerdict,
      text: 'WINDOW: ' + windowFailure.replace(/_/g, ' ').toUpperCase(),
    }));
  }
  return el('div', { class: 'dn-bld-pf', role: 'status' }, [
    el('div', { class: 'dn-bld-pf-head' }, head),
    el('ul', { class: 'dn-bld-pf-reasons' }, reasons.map((t) => el('li', { text: t }))),
  ]);
}

// REFUSE-severity validation warnings (e.g. margin_below_noise_floor) get a
// dedicated slot in the Review pane so the statistical objection is in front
// of the operator right where they apply — not only in the side preview.
function refuseWarningsPanel() {
  const refuses = (_warnings || []).filter((w) => w && w.severity === 'refuse');
  if (!refuses.length) return el('div', { class: 'dn-bld-refuses dn-bld-refuses-empty' });
  return el('div', { class: 'dn-bld-refuses', role: 'alert' }, refuses.map((w) => el('div', {
    class: 'dn-bld-warn dn-bld-warn-refuse',
  }, [
    el('span', { class: 'dn-bld-warn-glyph', 'aria-hidden': 'true', text: '⛔' }),
    el('span', { class: 'dn-bld-warn-msg', text: w.message || w.code || '' }),
  ])));
}

function fmtSig(v) {
  const n = Number(v);
  if (!isFinite(n)) return '—';
  return String(Math.round(n * 1e6) / 1e6);
}

let _confirmApply = false;

async function doApply(confirm, out) {
  clearChildren(out);
  out.appendChild(el('p', { class: 'dn-faint', text: confirm ? 'applying…' : 'previewing…' }));
  try {
    const res = await postApply(confirm);
    clearChildren(out);
    if (!res || res.error) { out.appendChild(el('p', { class: 'dn-bld-flash', text: (res && res.error) || 'apply failed' })); return; }
    out.appendChild(el('div', { class: 'dn-bld-panel' }, [
      el('div', { class: 'dn-bld-kv' }, [el('span', { class: 'dn-bld-k', text: res.confirmed ? 'applied' : 'dry-run' }),
        el('span', { class: 'dn-bld-v', text: res.rolled ? 'epoch will roll' : 'no roll' })]),
      el('div', { class: 'dn-bld-kv' }, [el('span', { class: 'dn-bld-k', text: 'contract hash' }),
        el('span', { class: 'dn-bld-v dn-mono', title: res.new_contract_hash || '', text: shorten(res.new_contract_hash, 16) })]),
    ]));
  } catch (err) {
    clearChildren(out);
    out.appendChild(el('p', { class: 'dn-bld-flash', text: (err && err.message) || String(err) }));
  }
}

function shorten(s, n) { const str = String(s || '—'); return str.length > n ? str.slice(0, n) + '…' : str; }

// ── right: the live preview ───────────────────────────────────────────

function renderPreview(host) {
  const d = _draft || {};
  const sc = d.scoring || {};
  const ts = sc.tournament || {};
  const structure = ts.structure || 'gauntlet';
  const cost = _cost || {};
  const diff = _diff || {};
  const board = Array.isArray(d.board) ? d.board : [];
  const holdout = d.holdout || { train_ids: [], holdout_ids: [] };

  const digest = JSON.stringify({
    structure, params: ts.params || {}, cost, warns: _warnings,
    diff: diff.changed_components || [], rolls: diff.rolls_epoch,
    board: board.length, train: (holdout.train_ids || []).length, hold: (holdout.holdout_ids || []).length,
  });

  gatedSwap(host, 'preview|' + digest, () => previewNodes({
    structure,
    params: ts.params || {},
    cost,
    warnings: _warnings,
    diff,
    boardCount: board.length,
    trainCount: (holdout.train_ids || []).length,
    holdoutCount: (holdout.holdout_ids || []).length,
  }));
}

export { STRUCTURE_GLYPH };

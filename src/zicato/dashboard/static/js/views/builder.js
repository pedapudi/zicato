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
import { gatedSwap, section, empty } from '../ui.js';
import { infoPopover } from '../builder/popover.js';
import { BuilderChat } from '../builder/chat.js';
import { previewNodes } from '../builder/preview.js';
import {
  STRUCTURES, STRUCTURE_GLYPH, paramSpecsFor, structureGlyphSvg,
  readChatWidth, persistChatWidth, readChatCollapsed, persistChatCollapsed,
  CHAT_MIN, CHAT_MAX,
} from '../builder/model.js';
import {
  getConfig, getDraft, postOp, postApply,
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
let _config = null;      // /builder/config public dict
let _active = 'structure';
let _busy = false;
let _chat = null;

// Re-render hooks set up per mount.
let _renderCenter = () => {};
let _renderPreview = () => {};
let _renderRail = () => {};

export async function render(host) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Loading the tournament builder…' }));

  // Load config + draft once; subsequent renders reuse the shared state.
  if (_config == null) {
    const [cfg, snap] = await Promise.all([getConfig(), getDraft()]);
    _config = cfg || { chat_enabled: false, agent: {}, skills: [] };
    if (snap) applySnapshot(snap);
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
  const digest = JSON.stringify({ active: _active, done: SECTIONS.map((s) => sectionDone(s.id)) });
  if (gatedSwap(host, 'rail|' + digest, () => SECTIONS.map((s) => {
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
  }))) { /* swapped */ }
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
  const digest = JSON.stringify({ active: _active, draft: d, busy: _busy, flash: _flash, pf: _preflight });
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
  const trainSet = new Set(holdout.train_ids || []);
  const holdSet = new Set(holdout.holdout_ids || []);

  const rows = board.map((b) => {
    const id = b.id || b.entry_id;
    const held = holdSet.has(id);
    const toggle = el('button', {
      class: 'dn-bld-holdtoggle' + (held ? ' dn-bld-held' : ''), type: 'button',
      'aria-pressed': String(held), title: held ? 'held out (click to train on it)' : 'in train set (click to hold out)',
      text: held ? 'holdout' : 'train',
    });
    toggle.addEventListener('click', () => {
      const next = held ? [...holdSet].filter((x) => x !== id) : [...holdSet, id];
      runOp('set_holdout', { tags: next });
    });
    return el('div', { class: 'dn-bld-boardrow' }, [
      el('span', { class: 'dn-bld-boardid', title: id, text: id }),
      el('span', { class: 'dn-bld-boardkind', text: b.kind || '' }),
      toggle,
    ]);
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
    controlRow('Holdout fraction', {
      title: 'Holdout fraction', def: '0.2',
      body: 'Fraction of the board hash-partitioned into the holdout slice (when no explicit per-entry holdout tags are set). A larger holdout guards harder against overfitting but costs more confirm runs and shrinks the train field.',
    }, frac),
    el('div', { class: 'dn-bld-splitstrip', role: 'img', 'aria-label': `train ${trainSet.size} · holdout ${holdSet.size}` }, [
      el('span', { class: 'dn-bld-split-train', style: `flex:${Math.max(1, trainSet.size)}`, text: `train ${trainSet.size}` }),
      el('span', { class: 'dn-bld-split-hold', style: `flex:${Math.max(0.001, holdSet.size)}`, text: `holdout ${holdSet.size}` }),
    ]),
    rows.length ? el('div', { class: 'dn-bld-boardlist' }, rows) : empty('The board is empty — add entries via the board builder.'));
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
// set_weights had NO GUI before this section: the scalar's drift/pass
// coefficients plus the per-namespace multi-objective weights (through the
// dedicated set_namespace_weights op) and the opt-in parsimony term.
function weightsSection(d) {
  const sc = d.scoring || {};
  const ns = sc.namespace_weights || {};
  const rows = [
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
    controlRow('Diff-complexity weight', {
      title: 'diff_complexity_weight', def: '0 (term absent)',
      body: 'Opt-in MDL/parsimony coefficient: adds weight × (added + removed + patches) to the challenger scalar, biasing selection toward the smaller, more general edit (a shorter-description edit provably overfits the board less). 0 keeps the term exactly absent.',
    }, numInput(sc.diff_complexity_weight != null ? sc.diff_complexity_weight : 0,
      { min: '0', step: '0.001', 'aria-label': 'Diff complexity weight' },
      (n) => runOp('set_namespace_weights', { diff_complexity_weight: n }))),
  ];
  const nsKeys = Object.keys(ns);
  const nsRows = nsKeys.map((key) => controlRow('Namespace ' + key, {
    title: 'namespace_weights["' + key + '"]', def: String(ns[key]),
    body: 'Signed coefficient turning this namespace\'s per-run mean into a scalar component. Positive = higher is worse (drift, cost, schema); negative = higher is better (rubric — negation keeps the scalar lower-is-better); zero = tracked but unscored.',
  }, numInput(ns[key], { step: '0.001', 'aria-label': 'Namespace weight ' + key }, (n) => {
    const next = Object.assign({}, ns);
    next[key] = n;
    runOp('set_namespace_weights', { namespace_weights: next });
  })));
  return section('Weights',
    el('p', { class: 'dn-lede', text: 'The loss-shaping coefficients: how drift, misses, and each metric namespace fold into the one scalar a duel compares. Contract fields — a change rolls the epoch.' }),
    ...rows,
    el('h3', { class: 'dn-bld-subhead', text: 'Namespace weights' }),
    ...(nsRows.length ? nsRows : [empty('No namespace weights in this contract.')]));
}

function proposerSection(d) {
  const p = d.proposer || {};
  const skills = Array.isArray(p.skills) ? p.skills : [];
  const isAgent = !!p.has_custom_agent;
  const pq = ((d.scoring || {}).proposer_quality) || {};
  const em = ((d.scoring || {}).experiment_memory) || {};
  return section('Proposer',
    el('p', { class: 'dn-lede', text: 'Who proposes each challenger. A skill-composed default proposer, or a custom ADK agent dir. Read-only summary here — editing the proposer dir is a config change.' }),
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
    controlRow('Pass-rate monotonicity', {
      title: 'Pass-rate monotonicity', def: 'off',
      body: 'When on, a challenger may not regress the board pass-rate even if its weighted loss improves — every predicate the champion passed must still pass. Guards against trading a hard-pass away for an average-loss gain.',
    }, el('label', { class: 'dn-bld-checkwrap' }, [mono, el('span', { text: 'require non-regressing pass-rate' })])),
    controlRow('Monotonicity scope', {
      title: 'pass_rate_monotonicity_scope', def: 'per_entry',
      body: 'Granularity of the pass-rate check when it is on. per_entry rejects if ANY champion-passed entry flips to fail (right for invariant / regression-suite boards); aggregate rejects only when the overall pass-rate drops (right for sampled boards where one noisy flip should not veto a strictly-better challenger).',
    }, scope),
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
    el('div', { class: 'dn-bld-applyrow' }, [dry, apply]),
    out);
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
    el('p', { class: 'dn-lede', text: 'Statistical pre-flight: measures the A/A noise floor and the achievable signal of this draft against the registered target, before any round is spent. Recommend-only.' }),
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
    reasons.push(`achievable signal ${fmtSig(r.signal)} (degraded point ${r.degraded_mutation_id || '?'})`);
    if (verdict === 'refuse') reasons.push('the signal is at or below the floor — duels under this contract would be decided by noise');
    if (verdict === 'warn') reasons.push('saturated: every probe scored identically — the board cannot discriminate even a deliberate degradation');
    if (verdict === 'ok') reasons.push('the achievable signal clears the measured floor');
  }
  return el('div', { class: 'dn-bld-pf', role: 'status' }, [
    el('div', { class: 'dn-bld-pf-head' }, [
      el('span', { class: 'dn-bld-k', text: 'preflight verdict' }),
      chip,
    ]),
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

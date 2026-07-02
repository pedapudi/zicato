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
  { id: 'proposer', label: 'Proposer' },
  { id: 'gate', label: 'Gate' },
  { id: 'review', label: 'Review' },
];

let _draft = null;       // the live draft.to_dict()
let _cost = null;        // last cost.to_dict()
let _warnings = [];      // last warnings[]
let _diff = null;        // last diff.to_dict()
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
    case 'proposer': return !!d.proposer;
    case 'gate': return sc.promote_margin != null;
    case 'review': return !!(_diff && _diff.rolls_epoch);
    default: return false;
  }
}

// ── center: the active section's controls ─────────────────────────────

function renderCenter(host) {
  const d = _draft || {};
  const digest = JSON.stringify({ active: _active, draft: d, busy: _busy, flash: _flash });
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
      runOp('set_param', { key: spec.key, value: num });
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

function proposerSection(d) {
  const p = d.proposer || {};
  const skills = Array.isArray(p.skills) ? p.skills : [];
  const isAgent = !!p.has_custom_agent;
  return section('Proposer',
    el('p', { class: 'dn-lede', text: 'Who proposes each challenger. A skill-composed default proposer, or a custom ADK agent dir. Read-only summary here — editing the proposer dir is a config change.' }),
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
  return section('Promote gate',
    el('p', { class: 'dn-lede', text: 'What a challenger must clear to dethrone the champion and promote.' }),
    controlRow('Promote margin', {
      title: 'Promote margin', def: '0.0',
      body: 'The minimum scalar improvement (champion loss − challenger loss) a challenger must clear to promote. A larger margin demands a more decisive win and resists noise; 0 promotes on any improvement.',
    }, margin),
    controlRow('Pass-rate monotonicity', {
      title: 'Pass-rate monotonicity', def: 'off',
      body: 'When on, a challenger may not regress the board pass-rate even if its weighted loss improves — every predicate the champion passed must still pass. Guards against trading a hard-pass away for an average-loss gain.',
    }, el('label', { class: 'dn-bld-checkwrap' }, [mono, el('span', { text: 'require non-regressing pass-rate' })])));
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
    el('div', { class: 'dn-bld-applyrow' }, [dry, apply]),
    out);
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

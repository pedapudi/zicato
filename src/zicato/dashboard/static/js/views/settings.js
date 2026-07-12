// js/views/settings.js — the Settings surface.
//
// A wide Console-IV view homing the read-mostly contract at-a-glance, the
// models / LLM-endpoint config, and the EDITABLE appearance pickers (colour /
// typeface / page scale / side-panel width — every visual + layout
// preference). A left section rail drives ONE section host on the right.
//
// LAUNCHER, NOT EMBED: the tournament builder is its OWN first-class view now
// (views/builder.js renders full-width at `#/builder`). Embedding it inside
// this section-host nested it behind the settings rail — double rails + a
// cramped centre. Settings therefore keeps only a LAUNCHER rail entry that
// NAVIGATES to `#/builder` (an <a href> via the router) rather than rendering
// the builder inside the host. One route-agnostic builder module still backs
// every entry point: the top-bar nav entry, the `#/builder` deep-link, this
// launcher, and the standalone `zicato builder` CLI.
//
// Render discipline: the chrome (rail + host) is built ONCE per mount and the
// active section is swapped on selection; every section paints through a
// digest gate so a steady `state:changed` heartbeat re-dispatch writes ZERO
// DOM (no flash). Theme tokens only.

import { el, clearChildren } from '../core/dom.js';
import {
  gatedSwap, section, empty, readColor, readType, readFontSize,
} from '../ui.js';
import {
  SCALE_MIN, SCALE_MAX, SCALE_STEP, readScale,
  RAIL_MIN, RAIL_MAX, readRail,
} from '../ui.js';
import { DEFAULT_SETTINGS_SECTION } from '../router.js';
import * as data from '../data.js';
import { getModels, saveModels, getDraft } from '../builder/api.js';
// REUSE the builder's live-PREVIEW renderer (NOT a fork): the frozen current
// contract is the same shape the builder draft has, so the Contract section
// renders the SAME schematic + cost meter + board/holdout strip + validation
// diagnostics READ-ONLY, bound to /api/epoch. The cost / validation are the
// SERVER envelope from the builder draft fetch (C6: no client-side re-estimate);
// absent that envelope the cost panel degrades to an honest "unavailable" line.
import { previewNodes } from '../builder/preview.js';
// REUSE the SAME swatch-dropdown component the top bar renders (NOT a fork): the
// settings theme picker is the very same control, so the two render identically
// and stay in lockstep through the shared store (applyTheme → syncSwatchDropdowns).
import { buildSwatchDropdown } from '../swatchdropdown.js';
// REUSE the SAME typeface grouped-popover the top bar renders (NOT a fork): the
// settings typeface picker is the very same control, so the two render
// identically and stay in lockstep through the shared store (applyTypeface →
// syncTypefaceDropdowns).
import { buildTypefaceDropdown } from '../typefacedropdown.js';
// REUSE the SAME theme/pref mechanism the top-bar controls drive — these apply
// to the app root, persist (the one localStorage store ui.js owns), AND sync the
// top-bar pickers. The Appearance section is editable by calling THESE, so the
// settings panel and the top bar are two views of ONE source of truth (changing
// either updates the other and persists identically). NOT a fork.
import {
  applyTheme, applyTypeface, applyFontSize, applyScale, resetScale, applyRail,
} from '../shell.js';

// The in-host settings sections (each drives the section host). The tournament
// builder is NOT one of them any more — it is a launcher (LAUNCHER below) that
// navigates out to its own full-width `#/builder` view.
const SECTIONS = [
  { id: 'contract', label: 'Contract', glyph: '◷' },
  { id: 'models', label: 'Models / LLM endpoints', glyph: '✦' },
  { id: 'appearance', label: 'Appearance', glyph: '◑' },
];

// The launcher rail entry: a link OUT to the standalone tournament-builder view
// (`#/builder`). It rides at the top of the rail so the builder stays the most
// discoverable affordance, but it navigates rather than swapping a section.
const LAUNCHER = { view: 'builder', label: 'Tournament builder', glyph: '⚒' };

// The LLM roles the unified models section edits, in display order. The two
// proposer-ensemble roles (WS-ENS) are OPTIONAL and fall back to auxiliary when
// unset — a change here, like every models role, does NOT roll the epoch.
const MODEL_ROLES = [
  ['harness', 'Harness', 'The LLM the inner agent under evaluation runs on.'],
  ['auxiliary', 'Auxiliary', 'Every zicato-internal consumer — emulator, proposer, analysis.'],
  ['builder', 'Builder', 'The tournament-builder copilot.'],
  ['judge', 'Judge', 'In-run process judges / rubric matchers (falls back to auxiliary).'],
  ['proposer_breadth', 'Proposer breadth', 'Best-of-N slate sampling — the exploratory ensemble half. A model spec steers the default proposer; a call_llm path applies only to text-shim/custom proposers. Falls back to auxiliary when unset.'],
  ['proposer_depth', 'Proposer depth', 'Best-of-N critique + revise — the refine ensemble half. A model spec steers the default proposer; a call_llm path applies only to text-shim/custom proposers. Falls back to auxiliary when unset.'],
];
const SECTION_IDS = SECTIONS.map((s) => s.id);
// The default section a bare `#/settings` opens — sourced from the router so the
// view and the router's `up()` agree (the builder is no longer the default).
const DEFAULT_SECTION = DEFAULT_SETTINGS_SECTION;

let _active = DEFAULT_SECTION;
let _railHost = null;
let _sectionHost = null;
let _ctx = null;
let _models = null;         // /settings/models secret-safe view (models section)
let _modelsDirty = false;   // an unsaved local edit is pending (digest-gate seam)
let _modelsStatus = '';     // last save outcome message (saved / error)
// The SHARED swatch dropdown for the Appearance theme picker — built ONCE and
// its node REUSED across re-renders (gatedSwap re-appends the same node), so we
// never register a fresh instance per repaint. applyTheme keeps it in sync.
let _themeDropdown = null;
// The SHARED typeface grouped-popover for the Appearance typeface picker — built
// ONCE and its node REUSED across re-renders, mirroring _themeDropdown.
// applyTypeface keeps it in sync via syncTypefaceDropdowns.
let _typeDropdown = null;

function normaliseSection(id) {
  return SECTION_IDS.includes(id) ? id : DEFAULT_SECTION;
}

// NOTE: the product-status "research preview" mark is NOT a Settings card any
// more. It is a quiet pill pinned NEXT TO the wordmark in the top bar (mounted
// once in the shell — see shell.js's researchPreviewPill()), so it persists
// across every view rather than leading the Settings surface.

export async function render(host, ctx, params) {
  _ctx = ctx;
  _active = normaliseSection(params && params.section);

  // Build the chrome ONCE per mount; thereafter swap only the section host.
  if (!host.firstChild) {
    clearChildren(host);
    const root = el('div', { class: 'dn-settings' });
    _railHost = el('nav', { class: 'dn-set-rail', 'aria-label': 'Settings sections' });
    _sectionHost = el('div', { class: 'dn-set-body', role: 'region', 'aria-label': 'Settings section' });
    root.appendChild(_railHost);
    root.appendChild(_sectionHost);
    host.appendChild(root);
  }

  renderRail();
  await renderSection();
}

function renderRail() {
  const digest = 'rail|' + _active;
  gatedSwap(_railHost, digest, () => {
    // The LAUNCHER rides first: a link OUT to the standalone `#/builder` view
    // (it never marks active — it is not an in-host section, it navigates away).
    const launcher = el('a', {
      class: 'dn-set-railitem dn-set-raillauncher',
      href: _ctx.href(LAUNCHER.view, {}),
      title: 'Open the tournament builder (full-width view)',
    }, [
      el('span', { class: 'dn-set-railglyph', 'aria-hidden': 'true', text: LAUNCHER.glyph }),
      el('span', { class: 'dn-set-raillabel', text: LAUNCHER.label }),
      el('span', { class: 'dn-set-raillaunch-glyph', 'aria-hidden': 'true', text: '↗' }),
    ]);
    const items = SECTIONS.map((s) => el('a', {
      class: 'dn-set-railitem' + (s.id === _active ? ' dn-set-railitem-active' : ''),
      // each section rides the settings route so it is itself deep-linkable.
      href: _ctx.href('settings', { section: s.id }),
      'aria-current': s.id === _active ? 'page' : null,
    }, [
      el('span', { class: 'dn-set-railglyph', 'aria-hidden': 'true', text: s.glyph }),
      el('span', { class: 'dn-set-raillabel', text: s.label }),
    ]));
    return [launcher, ...items];
  });
}

async function renderSection() {
  switch (_active) {
    case 'contract': return renderContract();
    case 'models': return renderModels();
    case 'appearance': return renderAppearance();
    default: return renderContract();
  }
}

// ── Contract — read-mostly view of the current epoch ──────────────────
//
// LEADS with the builder's live-PREVIEW visualization (the per-structure
// schematic + cost meter + train/holdout strip + validation diagnostics),
// reused READ-ONLY and bound to the FROZEN contract /api/epoch returns. The
// cost / validation are the SERVER envelope from the builder draft fetch (C6:
// no client-side re-estimate) — the draft initializes from the live workspace,
// so its cost/warnings describe the current contract; when the draft is
// unavailable the cost panel degrades to an honest "unavailable" line. The
// text roll-up follows below; each row links into the builder to edit, so this
// surface stays strictly read-only.

async function renderContract() {
  const ep = await data.epoch();
  const c = ep || {};
  const board = Array.isArray(c.board) ? c.board : [];
  const tournament = (c.tournament && typeof c.tournament === 'object') ? c.tournament : null;
  const structure = (tournament && tournament.structure) || 'gauntlet';
  const params = (tournament && tournament.params && typeof tournament.params === 'object') ? tournament.params : {};
  const brief = c.brief || '';
  const scoring = (c.scoring && typeof c.scoring === 'object') ? c.scoring : {};
  const overfitting = scoring.overfitting || c.overfitting || {};
  const proposer = (c.proposer && typeof c.proposer === 'object') ? c.proposer : null;
  // /api/epoch computes the train/holdout split SERVER-SIDE (the same slices the
  // gate plays) — read its counts so the preview's cost + strip never re-derive
  // the deterministic sha256 hash split client-side.
  const split = (c.board_split && typeof c.board_split === 'object') ? c.board_split : {};
  const trainCount = split.train_count != null ? split.train_count : board.length;
  const holdoutCount = split.holdout_count != null ? split.holdout_count : 0;

  // The SERVER cost envelope + validation warnings — from the builder draft
  // (C6: no client-side re-estimate). The draft initializes from the live
  // workspace, so its cost/warnings describe the current contract; a failed
  // fetch degrades the cost panel to an honest "unavailable" line.
  const draft = await getDraft();
  const cost = (draft && draft.cost && typeof draft.cost === 'object') ? draft.cost : null;
  const warnings = (draft && Array.isArray(draft.warnings)) ? draft.warnings : [];

  const digest = JSON.stringify({
    epoch: c.epoch_id || null, board: board.length, structure, params,
    train: trainCount, hold: holdoutCount,
    briefLen: brief.length, margin: scoring.promote_margin,
    mono: !!scoring.pass_rate_monotonicity,
    holdFrac: overfitting.holdout_fraction, ofEnabled: overfitting.enabled,
    proposer: proposer ? (proposer.has_custom_agent ? 'agent' : 'skills') : null,
    // the server envelope folds in so an unavailable→available transition (or a
    // moved cost) repaints; null cost ⇒ the honest "unavailable" line.
    cost: cost ? [cost.board_runs_per_round, (cost.breakdown || []).length] : null,
    warn: warnings.length,
  });

  gatedSwap(_sectionHost, 'contract|' + digest, () => {
    if (!ep || !c.epoch_id) return [empty('No epoch contract is available yet.')];
    const briefLines = brief ? brief.split(/\n/).length : 0;
    const margin = scoring.promote_margin != null ? scoring.promote_margin : 0;
    const holdFrac = overfitting.holdout_fraction != null ? overfitting.holdout_fraction : null;
    const rows = [
      contractRow('Board', `${board.length} ${board.length === 1 ? 'entry' : 'entries'}`, 'builder'),
      contractRow('Proposer brief', briefLines ? `${briefLines} lines` : 'none', 'builder'),
      contractRow('Tournament structure', structure, 'builder'),
      contractRow('Promote margin', String(margin), 'builder'),
      contractRow('Pass-rate monotonicity', scoring.pass_rate_monotonicity ? 'required' : 'off', 'builder'),
      contractRow('Overfitting guard',
        overfitting.enabled === false ? 'disabled'
          : (holdFrac != null ? `holdout ${holdFrac}` : 'on'), 'builder'),
      contractRow('Proposer',
        proposer ? (proposer.has_custom_agent ? 'custom ADK agent' : 'skill-composed default') : '—', 'builder'),
    ];
    // The read-only preview model: the SAME shape the builder's preview reads,
    // but with no diff (nothing to apply) and the cost / warnings taken from the
    // SERVER envelope (the builder draft fetch above). An absent envelope drives
    // the honest "cost preview unavailable" line via `costUnavailable`.
    const preview = el('aside', { class: 'dn-set-preview dn-bld-preview', 'aria-label': 'Contract visualization' },
      previewNodes({
        structure, params, cost: cost || {}, warnings, costUnavailable: !cost,
        boardCount: board.length, trainCount, holdoutCount,
        readonly: true, heading: 'Contract at a glance',
      }));
    return [
      section('Contract — current epoch',
        el('p', { class: 'dn-lede', text: 'A read-only view of the evaluation contract this epoch runs on — its tournament schematic, the estimated board-runs per round, the train / holdout split, and any validation diagnostics. Open the tournament builder to edit any of it (a change rolls the epoch).' }),
        preview,
        el('div', { class: 'dn-set-kvgrid' }, rows),
        el('a', { class: 'dn-linkbtn', href: _ctx.href('builder', {}), text: 'Edit in the tournament builder →' })),
    ];
  });
}

function contractRow(label, value, linkView) {
  return el('a', {
    class: 'dn-set-kvrow', href: _ctx.href(linkView, {}),
    title: 'edit in the tournament builder',
  }, [
    el('span', { class: 'dn-set-k', text: label }),
    el('span', { class: 'dn-set-v', text: value }),
  ]);
}

// ── Models / LLM endpoints — EDITABLE per-role config (NAMES only) ────
//
// Generalises the former "Builder assistant" read-out into an EDITABLE
// section for EVERY role (harness · auxiliary · builder · judge ·
// proposer_breadth · proposer_depth), backed by the
// secret-safe GET/POST /settings/models. Each role toggles between the
// `call_llm` dotted-path form and the `{model, endpoint, api_key_env}` form;
// only the api_key_env NAME is ever shown/edited (plus a "set / unset"
// indicator from the server's api_key_env_set boolean) — never a secret value.
// A model/endpoint is runtime infra, so a change here does NOT roll the epoch.

// The in-memory editable model of all four roles. Seeded from the server's
// secret-safe view, mutated locally as the operator edits, POSTed on save.
let _modelsEdit = null;

function blankRoleEdit() {
  return { use_call_llm: false, call_llm: '', model: '', endpoint: '', api_key_env: '', api_key_env_set: false };
}

// Fold one server role spec (public, secret-safe) into the editable shape.
function roleEditFromPublic(spec) {
  const s = spec || {};
  const useCallLlm = !!s.call_llm;
  return {
    use_call_llm: useCallLlm,
    call_llm: s.call_llm || '',
    model: s.model || '',
    endpoint: s.endpoint || '',
    api_key_env: s.api_key_env || '',
    api_key_env_set: !!s.api_key_env_set,
  };
}

// Project the editable shape back to the on-disk role spec the POST takes.
// Emits ONLY the active form's keys (so the server stores a clean spec) and
// NEVER a secret value — api_key_env is a NAME.
function roleSpecFromEdit(edit) {
  if (edit.use_call_llm) {
    return edit.call_llm ? { call_llm: edit.call_llm } : {};
  }
  if (!edit.model) return {};
  return { model: edit.model, endpoint: edit.endpoint || null, api_key_env: edit.api_key_env || null };
}

function seedModelsEdit() {
  const view = (_models && _models.models) || {};
  _modelsEdit = {};
  for (const [id] of MODEL_ROLES) _modelsEdit[id] = roleEditFromPublic(view[id]);
}

async function renderModels() {
  if (_models == null) { _models = await getModels(); seedModelsEdit(); }
  if (_modelsEdit == null) seedModelsEdit();

  const digest = JSON.stringify({ edit: _modelsEdit, dirty: _modelsDirty, status: _modelsStatus });
  gatedSwap(_sectionHost, 'models|' + digest, () => {
    if (_models == null) return [empty('Could not load the models settings.')];
    return [
      section('Models / LLM endpoints',
        el('p', { class: 'dn-lede', text: 'How every role reaches an LLM — harness, auxiliary, builder, judge, and the two best-of-N proposer-ensemble roles (breadth / depth). A model / endpoint is runtime INFRASTRUCTURE, not part of the evaluation contract, so a change here does NOT roll the epoch (unlike the Contract section).' }),
        el('p', { class: 'dn-faint', text: 'For each role, either a call_llm dotted path or a model spec. Only the API-key environment-variable NAME is shown or edited — a secret value is never read or surfaced here.' }),
        el('div', { class: 'dn-set-models' }, MODEL_ROLES.map(roleCard)),
        modelsActions()),
    ];
  });
}

function roleCard([id, label, hint]) {
  const edit = _modelsEdit[id];
  return el('div', { class: 'dn-set-modelcard', 'data-role': id }, [
    el('div', { class: 'dn-set-modelhead' }, [
      el('span', { class: 'dn-set-modelname', text: label }),
      el('span', { class: 'dn-faint', text: hint }),
    ]),
    formToggle(id, edit),
    edit.use_call_llm ? callLlmForm(id, edit) : modelSpecForm(id, edit),
  ]);
}

// The call_llm ⟷ model-spec toggle — a two-button group per role.
function formToggle(id, edit) {
  const mk = (useCallLlm, text) => {
    const on = edit.use_call_llm === useCallLlm;
    const b = el('button', {
      class: 'dn-set-typebtn' + (on ? ' dn-set-typebtn-on' : ''),
      type: 'button', 'aria-pressed': String(on),
      'data-form': useCallLlm ? 'call_llm' : 'model', text,
    });
    b.addEventListener('click', () => {
      if (edit.use_call_llm !== useCallLlm) { edit.use_call_llm = useCallLlm; markDirty(); }
    });
    return b;
  };
  return el('div', { class: 'dn-set-typeswitch', role: 'group', 'aria-label': id + ' form' }, [
    mk(false, 'model spec'), mk(true, 'call_llm path'),
  ]);
}

function callLlmForm(id, edit) {
  return el('div', { class: 'dn-set-modelform' }, [
    textField(id + '-call_llm', 'call_llm', edit.call_llm, 'pkg.mod:fn', (v) => { edit.call_llm = v; markDirty(); }),
  ]);
}

function modelSpecForm(id, edit) {
  return el('div', { class: 'dn-set-modelform' }, [
    textField(id + '-model', 'model', edit.model, 'model id', (v) => { edit.model = v; markDirty(); }),
    textField(id + '-endpoint', 'endpoint', edit.endpoint, 'provider default', (v) => { edit.endpoint = v; markDirty(); }),
    apiKeyEnvField(id, edit),
  ]);
}

// The api_key_env field shows + edits ONLY the env-var NAME, plus a read-only
// "set / unset" indicator derived from the server's api_key_env_set boolean.
// There is no secret-value input path anywhere.
function apiKeyEnvField(id, edit) {
  const indicator = el('span', {
    class: 'dn-set-keyflag ' + (edit.api_key_env_set ? 'dn-set-keyflag-set' : 'dn-set-keyflag-unset'),
    text: edit.api_key_env ? (edit.api_key_env_set ? 'set' : 'unset') : '—',
    title: 'whether the named environment variable is currently set (the value is never read)',
  });
  const field = textField(id + '-api_key_env', 'api_key_env (name)', edit.api_key_env, 'API_KEY_ENV_VAR', (v) => { edit.api_key_env = v; markDirty(); });
  field.appendChild(indicator);
  return field;
}

function textField(name, label, value, placeholder, onInput) {
  const input = el('input', {
    class: 'dn-set-input dn-mono', type: 'text', name, value: value || '',
    placeholder: placeholder || '', 'aria-label': label, autocomplete: 'off', spellcheck: 'false',
  });
  input.addEventListener('input', () => {
    const v = input.value != null ? input.value : input.getAttribute('value');
    onInput(String(v || ''));
  });
  return el('label', { class: 'dn-set-field' }, [
    el('span', { class: 'dn-set-fieldlabel', text: label }),
    input,
  ]);
}

function modelsActions() {
  const save = el('button', {
    class: 'dn-linkbtn', type: 'button',
    disabled: _modelsDirty ? null : 'disabled', text: 'Save models config',
  });
  save.addEventListener('click', onSaveModels);
  const status = _modelsStatus
    ? el('span', { class: 'dn-faint', text: _modelsStatus })
    : null;
  return el('div', { class: 'dn-set-modelactions' }, [save, status]);
}

function markDirty() {
  _modelsDirty = true;
  _modelsStatus = '';
  redrawModels();
}

async function onSaveModels() {
  const payload = {};
  for (const [id] of MODEL_ROLES) payload[id] = roleSpecFromEdit(_modelsEdit[id]);
  const res = await saveModels(payload);
  if (res && res.error) {
    _modelsStatus = 'save failed: ' + res.error;
  } else {
    _models = res || _models;
    seedModelsEdit();
    _modelsDirty = false;
    _modelsStatus = 'saved · does not roll the epoch';
  }
  redrawModels();
}

function redrawModels() {
  if (_active === 'models' && _sectionHost) {
    _sectionHost.removeAttribute('data-t-digest');
    renderModels();
  }
}

// ── Appearance — the EDITABLE colour / typeface / scale / rail pickers ─
//
// Every appearance preference is editable INLINE here, wired to the SAME
// mechanism the top-bar controls drive: applyTheme / applyTypeface / applyScale
// / applyRail stamp the app root, persist to the one ui.js localStorage store,
// AND sync the top-bar pickers. So editing a pref here updates the top bar (and
// vice-versa) and persists identically — one source of truth, not a fork. The
// former read-only "Dashboard" roll-up (page scale + side-panel width) is folded
// in here as the editable Layout block, so Appearance is the single home for
// every visual/layout preference and the Dashboard section is retired.

function renderAppearance() {
  const color = readColor();
  const type = readType();
  const fontsize = readFontSize();
  const scale = readScale();
  const rail = readRail();
  gatedSwap(_sectionHost, `appearance|${color}|${type}|${fontsize}|${scale}|${rail}`, () => [
    section('Appearance',
      el('p', { class: 'dn-lede', text: 'Colour theme, typeface (with text size), page scale, and side-panel width — all persistent and shared with the top-bar controls (change either, the other follows).' }),
      el('div', { class: 'dn-set-appgrid' }, [
        appRow('Colour theme', themePicker(color)),
        appRow('Typeface', typefacePicker(type, fontsize)),
        appRow('Page scale', scalePicker(scale)),
        appRow('Side-panel width', railPicker(rail)),
      ])),
  ]);
}

// A labelled appearance row: a label cell + the live control cell.
function appRow(label, control) {
  return el('div', { class: 'dn-set-approw' }, [
    el('span', { class: 'dn-set-k', text: label }),
    el('div', { class: 'dn-set-appctl' }, [control]),
  ]);
}

// COLOUR THEME — the SAME swatch dropdown the top bar renders (the shared
// component, NOT a fork): each option shows its colour swatch strip + name, so
// settings and the top bar look identical and share one store. Built ONCE and
// its node reused across re-renders; choosing applies via applyTheme (which
// stamps the root, persists, AND syncs every live dropdown — top bar + here).
function themePicker(current) {
  if (!_themeDropdown) {
    _themeDropdown = buildSwatchDropdown(current, (id) => { applyTheme(id); });
  } else {
    _themeDropdown.setValue(current);
  }
  return _themeDropdown.node;
}

// TYPEFACE — the SAME grouped-popover the top bar renders (the shared component,
// NOT a fork): a trigger + a grouped listbox of the operator's finalized 12
// faces (4 per mode), each row a micro-preview in its real faces, PLUS the
// compact S/M/L text-size segmented control in the popover footer. Built ONCE
// and its node REUSED across re-renders; choosing a face applies via
// applyTypeface and choosing a size via applyFontSize (each stamps the root,
// persists, AND syncs every live instance — top bar + here).
function typefacePicker(current, currentSize) {
  if (!_typeDropdown) {
    _typeDropdown = buildTypefaceDropdown(current, (id) => { applyTypeface(id); }, {
      size: currentSize, onSizeChoose: (id) => { applyFontSize(id); },
    });
  } else {
    _typeDropdown.setValue(current);
  }
  return _typeDropdown.node;
}

// PAGE SCALE — a native range + a % readout + a reset, wired to applyScale /
// resetScale (whole-page zoom; root + persist + top-bar pill sync).
function scalePicker(current) {
  const range = el('input', {
    class: 'dn-set-range', type: 'range',
    min: String(SCALE_MIN), max: String(SCALE_MAX), step: String(SCALE_STEP),
    value: String(current), 'aria-label': 'Page scale',
    'aria-valuemin': String(SCALE_MIN), 'aria-valuemax': String(SCALE_MAX), 'aria-valuenow': String(current),
  });
  const out = el('span', { class: 'dn-set-readout', text: current + '%' });
  const onScale = (ev) => {
    const raw = (ev && ev.target && ev.target.value != null) ? ev.target.value
      : (range.value != null ? range.value : range.getAttribute('value'));
    const n = applyScale(raw);
    out.textContent = n + '%';
    range.setAttribute('aria-valuenow', String(n));
  };
  range.addEventListener('input', onScale);
  range.addEventListener('change', onScale);
  const reset = el('button', {
    class: 'dn-set-reset', type: 'button',
    title: 'Reset page scale to 100%', 'aria-label': 'Reset page scale to 100%', text: '⟲',
  });
  reset.addEventListener('click', () => {
    const n = resetScale();
    range.value = String(n);
    range.setAttribute('value', String(n));
    range.setAttribute('aria-valuenow', String(n));
    out.textContent = n + '%';
  });
  return el('div', { class: 'dn-set-rangewrap' }, [range, out, reset]);
}

// SIDE-PANEL WIDTH — a native range + a px readout, wired to applyRail (the same
// grid-column + persist the rail-drag handle uses).
function railPicker(current) {
  const range = el('input', {
    class: 'dn-set-range', type: 'range',
    min: String(RAIL_MIN), max: String(RAIL_MAX), step: '4',
    value: String(current), 'aria-label': 'Side-panel width',
    'aria-valuemin': String(RAIL_MIN), 'aria-valuemax': String(RAIL_MAX), 'aria-valuenow': String(current),
  });
  const out = el('span', { class: 'dn-set-readout', text: current + 'px' });
  const onRail = (ev) => {
    const raw = (ev && ev.target && ev.target.value != null) ? ev.target.value
      : (range.value != null ? range.value : range.getAttribute('value'));
    const n = applyRail(raw);
    out.textContent = n + 'px';
    range.setAttribute('aria-valuenow', String(n));
  };
  range.addEventListener('input', onRail);
  range.addEventListener('change', onRail);
  return el('div', { class: 'dn-set-rangewrap' }, [range, out]);
}

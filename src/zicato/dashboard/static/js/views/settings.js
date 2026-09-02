// Settings: digest-gated contract, model-engine, and appearance sections.

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

const SECTIONS = [
  { id: 'contract', label: 'Contract', glyph: '◷' },
  { id: 'models', label: 'Models / LLM endpoints', glyph: '✦' },
  { id: 'appearance', label: 'Appearance', glyph: '◑' },
];

const LAUNCHER = { view: 'builder', label: 'Tournament builder', glyph: '⚒' };

const MODEL_ROLES = [
  ['target', 'Target LLM (optional)', 'Model injected only when the target adapter supports it.'],
  ['evaluation', 'Evaluation', 'Default internal model work.'],
  ['builder', 'Builder', 'The tournament-builder copilot.'],
  ['judge', 'Judge', 'Constrained scoring role; not a proposer session.'],
  ['adjudicator', 'Adjudicator', 'Independently audits judges; must not reuse the judge.'],
  ['user_emulator', 'User emulator', 'Constrained text role for multi-turn tasks.'],
  ['proposer', 'Proposer', 'Default for candidate generation and refinement.'],
  ['proposer_generate', 'Proposer generate', 'Generates candidate alternatives.'],
  ['proposer_review', 'Proposer review', 'Critiques, selects, and revises candidates.'],
];
const SECTION_IDS = SECTIONS.map((s) => s.id);
// The default section a bare `#/settings` opens — sourced from the router so the
// view and the router's `up()` agree on it.
const DEFAULT_SECTION = DEFAULT_SETTINGS_SECTION;

let _active = DEFAULT_SECTION;
let _railHost = null;
let _sectionHost = null;
let _ctx = null;
let _models = null;
let _modelsDirty = false;
let _modelsStatus = '';
let _themeDropdown = null;
let _typeDropdown = null;

function normaliseSection(id) {
  return SECTION_IDS.includes(id) ? id : DEFAULT_SECTION;
}

export async function render(host, ctx, params) {
  _ctx = ctx;
  _active = normaliseSection(params && params.section);

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
  const split = (c.board_split && typeof c.board_split === 'object') ? c.board_split : {};
  const trainCount = split.train_count != null ? split.train_count : board.length;
  const holdoutCount = split.holdout_count != null ? split.holdout_count : 0;

  const draft = await getDraft();
  const cost = (draft && draft.cost && typeof draft.cost === 'object') ? draft.cost : null;
  const warnings = (draft && Array.isArray(draft.warnings)) ? draft.warnings : [];

  const digest = JSON.stringify({
    epoch: c.epoch_id || null, board: board.length, structure, params,
    train: trainCount, hold: holdoutCount,
    briefLen: brief.length, margin: scoring.promote_margin,
    hMargin: scoring.holdout_margin != null ? scoring.holdout_margin : null,
    hBudget: scoring.holdout_entry_regression_budget || 0,
    mono: !!scoring.pass_rate_monotonicity,
    holdFrac: overfitting.holdout_fraction, ofEnabled: overfitting.enabled,
    proposer: proposer ? (proposer.agent_id || '') : null,
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
      // The holdout confirmation's own bounds, shown ONLY once pinned. Both
      // default to "reuse the train-side rule", and a row reading the same
      // number twice would be noise; but left unshown when they ARE pinned,
      // this summary implies the promote margin governs the holdout too —
      // exactly the single-knob confusion the separate bounds exist to end.
      ...(scoring.holdout_margin != null
        ? [contractRow('Holdout margin', String(scoring.holdout_margin), 'builder')] : []),
      ...(scoring.holdout_entry_regression_budget
        ? [contractRow('Holdout regression budget',
            `${scoring.holdout_entry_regression_budget} ${scoring.holdout_entry_regression_budget === 1 ? 'entry' : 'entries'}`,
            'builder')] : []),
      contractRow('Overfitting guard',
        overfitting.enabled === false ? 'disabled'
          : (holdFrac != null ? `holdout ${holdFrac}` : 'on'), 'builder'),
      contractRow('Proposer', (proposer && proposer.agent_id) || '—', 'builder'),
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

let _modelsEdit = null;

function blankRoleEdit() {
  return { use_call_llm: false, call_llm: '', model: '', revision: '', endpoint: '', api_key_env: '', api_key_env_set: false };
}

function roleEditFromPublic(spec) {
  const s = spec || {};
  const useCallLlm = !!s.call_llm;
  return {
    use_call_llm: useCallLlm,
    call_llm: s.call_llm || '',
    model: s.model || '',
    revision: s.revision || '',
    endpoint: s.endpoint || '',
    api_key_env: s.api_key_env || '',
    api_key_env_set: !!s.api_key_env_set,
  };
}

function roleSpecFromEdit(edit) {
  if (edit.use_call_llm) {
    return edit.call_llm ? { call_llm: edit.call_llm, ...(edit.revision ? { revision: edit.revision } : {}) } : {};
  }
  if (!edit.model) return {};
  return { model: edit.model, revision: edit.revision || null, endpoint: edit.endpoint || null, api_key_env: edit.api_key_env || null };
}

function seedModelsEdit() {
  const view = (_models && _models.models) || {};
  _modelsEdit = { engines: {}, roles: { ...(view.roles || {}) }, guide: view._guide || null };
  for (const [name, spec] of Object.entries(view.engines || {})) {
    _modelsEdit.engines[name] = roleEditFromPublic(spec);
  }
}

async function renderModels() {
  if (_models == null) { _models = await getModels(); seedModelsEdit(); }
  if (_modelsEdit == null) seedModelsEdit();

  const digest = JSON.stringify({ edit: _modelsEdit, dirty: _modelsDirty, status: _modelsStatus });
  gatedSwap(_sectionHost, 'models|' + digest, () => {
    if (_models == null) return [empty('Could not load the models settings.')];
    return [
      section('Models / LLM endpoints',
        el('p', { class: 'dn-lede', text: 'Define reusable engines once, then assign roles. The target is adapter-defined and may need no LLM; target config is only its optional model assignment.' }),
        el('p', { class: 'dn-faint', text: 'Only credential-variable names are stored. Model specs support native proposers; call_llm paths steer text/custom consumers only. Role assignment never changes the role protocol.' }),
        el('div', { class: 'dn-set-models' }, Object.entries(_modelsEdit.engines).map(engineCard)),
        addEngineButton(),
        el('div', { class: 'dn-set-models' }, MODEL_ROLES.map(roleAssignment)),
        modelsActions()),
    ];
  });
}

function engineCard([id, edit]) {
  return el('div', { class: 'dn-set-modelcard', 'data-engine': id }, [
    el('div', { class: 'dn-set-modelhead' }, [
      el('span', { class: 'dn-set-modelname', text: id }),
    ]),
    formToggle(id, edit),
    edit.use_call_llm ? callLlmForm(id, edit) : modelSpecForm(id, edit),
  ]);
}

function addEngineButton() {
  const button = el('button', { class: 'dn-linkbtn', type: 'button', text: '+ engine' });
  button.addEventListener('click', () => {
    let name = !_modelsEdit.engines.target ? 'target'
      : (!_modelsEdit.engines.evaluation ? 'evaluation' : 'engine-1');
    let n = 1; while (_modelsEdit.engines[name]) { n += 1; name = 'engine-' + n; }
    _modelsEdit.engines[name] = blankRoleEdit(); markDirty();
  });
  return button;
}

function roleAssignment([id, label, hint]) {
  const select = el('select', { class: 'dn-set-input', 'aria-label': label });
  select.appendChild(el('option', { value: '', text: 'inherit default' }));
  for (const name of Object.keys(_modelsEdit.engines)) {
    select.appendChild(el('option', { value: name, text: name,
      selected: _modelsEdit.roles[id] === name ? 'selected' : null }));
  }
  select.addEventListener('change', () => {
    if (select.value) _modelsEdit.roles[id] = select.value;
    else delete _modelsEdit.roles[id];
    markDirty();
  });
  return el('label', { class: 'dn-set-modelcard' }, [
    el('span', { class: 'dn-set-modelname', text: label }),
    el('span', { class: 'dn-faint', text: hint }), select,
  ]);
}

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
    textField(id + '-revision', 'revision', edit.revision, 'deployment revision', (v) => { edit.revision = v; markDirty(); }),
  ]);
}

function modelSpecForm(id, edit) {
  return el('div', { class: 'dn-set-modelform' }, [
    textField(id + '-model', 'model', edit.model, 'model id', (v) => { edit.model = v; markDirty(); }),
    textField(id + '-revision', 'revision', edit.revision, 'deployment revision', (v) => { edit.revision = v; markDirty(); }),
    textField(id + '-endpoint', 'endpoint', edit.endpoint, 'provider default', (v) => { edit.endpoint = v; markDirty(); }),
    apiKeyEnvField(id, edit),
  ]);
}

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
  const payload = { engines: {}, roles: _modelsEdit.roles };
  for (const [name, edit] of Object.entries(_modelsEdit.engines)) {
    payload.engines[name] = roleSpecFromEdit(edit);
  }
  if (_modelsEdit.guide) payload._guide = _modelsEdit.guide;
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

function appRow(label, control) {
  return el('div', { class: 'dn-set-approw' }, [
    el('span', { class: 'dn-set-k', text: label }),
    el('div', { class: 'dn-set-appctl' }, [control]),
  ]);
}

function themePicker(current) {
  if (!_themeDropdown) {
    _themeDropdown = buildSwatchDropdown(current, (id) => { applyTheme(id); });
  } else {
    _themeDropdown.setValue(current);
  }
  return _themeDropdown.node;
}

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

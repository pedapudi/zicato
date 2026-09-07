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
import { fetchJson } from '../core/api.js';
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


const SECTION_IDS = SECTIONS.map((s) => s.id);
// The default section a bare `#/settings` opens — sourced from the router so the
// view and the router's `up()` agree on it.
const DEFAULT_SECTION = DEFAULT_SETTINGS_SECTION;

let _active = DEFAULT_SECTION;
let _railHost = null;
let _sectionHost = null;
let _ctx = null;
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
    const items = SECTIONS.map((s) => el('a', {
      class: 'dn-set-railitem' + (s.id === _active ? ' dn-set-railitem-active' : ''),
      href: _ctx.href('settings', { section: s.id }),
      'aria-current': s.id === _active ? 'page' : null,
    }, [
      el('span', { class: 'dn-set-railglyph', 'aria-hidden': 'true', text: s.glyph }),
      el('span', { class: 'dn-set-raillabel', text: s.label }),
    ]));
    return items;
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

  const digest = JSON.stringify({
    epoch: c.epoch_id || null, board: board.length, structure, params,
    train: trainCount, hold: holdoutCount,
    briefLen: brief.length, margin: scoring.promote_margin,
    hMargin: scoring.holdout_margin != null ? scoring.holdout_margin : null,
    hBudget: scoring.holdout_entry_regression_budget || 0,
    mono: !!scoring.pass_rate_monotonicity,
    holdFrac: overfitting.holdout_fraction, ofEnabled: overfitting.enabled,
    proposer: proposer ? (proposer.agent_id || '') : null,
  });

  gatedSwap(_sectionHost, 'contract|' + digest, () => {
    if (!ep || !c.epoch_id) return [empty('No epoch contract is available yet.')];
    const briefLines = brief ? brief.split(/\n/).length : 0;
    const margin = scoring.promote_margin != null ? scoring.promote_margin : 0;
    const holdFrac = overfitting.holdout_fraction != null ? overfitting.holdout_fraction : null;
    const rows = [
      contractRow('Board', `${board.length} ${board.length === 1 ? 'entry' : 'entries'}`),
      contractRow('Proposer brief', briefLines ? `${briefLines} lines` : 'none'),
      contractRow('Tournament structure', structure),
      contractRow('Promote margin', String(margin)),
      contractRow('Pass-rate monotonicity', scoring.pass_rate_monotonicity ? 'required' : 'off'),
      // The holdout confirmation's own bounds, shown ONLY once pinned. Both
      // default to "reuse the train-side rule", and a row reading the same
      // number twice would be noise; but left unshown when they ARE pinned,
      // this summary implies the promote margin governs the holdout too —
      // exactly the single-knob confusion the separate bounds exist to end.
      ...(scoring.holdout_margin != null
        ? [contractRow('Holdout margin', String(scoring.holdout_margin))] : []),
      ...(scoring.holdout_entry_regression_budget
        ? [contractRow('Holdout regression budget',
            `${scoring.holdout_entry_regression_budget} ${scoring.holdout_entry_regression_budget === 1 ? 'entry' : 'entries'}`,
)] : []),
      contractRow('Overfitting guard',
        overfitting.enabled === false ? 'disabled'
          : (holdFrac != null ? `holdout ${holdFrac}` : 'on')),
      contractRow('Proposer', (proposer && proposer.agent_id) || '—'),
    ];
    return [
      section('Contract — current epoch',
        el('p', { class: 'dn-lede', text: 'The evaluation settings frozen for this epoch. Edit the workspace files or use the CLI to configure a subsequent evaluation.' }),
        el('div', { class: 'dn-set-kvgrid' }, rows),
      ),
    ];
  });
}

function contractRow(label, value) {
  return el('div', { class: 'dn-set-kvrow' }, [
    el('span', { class: 'dn-set-k', text: label }),
    el('span', { class: 'dn-set-v', text: value }),
  ]);
}

async function renderModels() {
  const env = await fetchJson('/settings/models').catch(() => null);
  gatedSwap(_sectionHost, 'models|' + JSON.stringify(env && env.models), () => {
    if (!env) return [empty('Could not load the models settings.')];
    return [
      section('Models', el('p', { class: 'dn-lede', text: 'Configure model engines and roles in the workspace configuration file.' }),
        el('pre', { text: JSON.stringify(env.models || {}, null, 2) })),
    ];
  });
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

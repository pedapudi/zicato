// variants/T/views/settings.js — the Settings surface (B3).
//
// A wide Console-IV view that HOMES the flagship tournament builder alongside
// the read-mostly contract at-a-glance, the builder-assistant config, and the
// EDITABLE appearance pickers (colour / typeface / page scale / side-panel
// width — every visual + layout preference). A left section rail drives ONE
// section host on the right — the same idiom as the builder's own rail.
//
// RE-HOME DISCIPLINE: the tournament builder is NOT rewritten here. The B2
// builder view module (`./builder.js`) is the single self-contained component;
// this surface simply delegates its `builder` section to that module's
// `render(host)`. `#/builder` still deep-links straight to this section (the
// router resolves it into `settings/builder`), so one component backs every
// entry point — the top-bar ⚙, the in-rail Tournament builder item, and the
// `#/builder` deep-link / the standalone `zicato builder` CLI.
//
// Render discipline: the chrome (rail + host) is built ONCE per mount and the
// active section is swapped on selection; every section paints through a
// digest gate so a steady `state:changed` heartbeat re-dispatch writes ZERO
// DOM (no flash). Theme tokens only.

import { el, clearChildren } from '../../../core/dom.js';
import {
  gatedSwap, section, empty, COLOR_THEMES, TYPE_THEMES, readColor, readType,
} from '../ui.js';
import {
  SCALE_MIN, SCALE_MAX, SCALE_STEP, readScale,
  RAIL_MIN, RAIL_MAX, readRail,
} from '../ui.js';
import * as data from '../data.js';
import { getConfig } from '../builder/api.js';
import * as builder from './builder.js';
// REUSE the SAME theme/pref mechanism the top-bar controls drive — these apply
// to the app root, persist (the one localStorage store ui.js owns), AND sync the
// top-bar pickers. The Appearance section is editable by calling THESE, so the
// settings panel and the top bar are two views of ONE source of truth (changing
// either updates the other and persists identically). NOT a fork.
import {
  applyTheme, applyTypeface, applyScale, resetScale, applyRail,
} from '../shell.js';

const SECTIONS = [
  { id: 'builder', label: 'Tournament builder', glyph: '⚒' },
  { id: 'contract', label: 'Contract', glyph: '◷' },
  { id: 'assistant', label: 'Builder assistant', glyph: '✦' },
  { id: 'appearance', label: 'Appearance', glyph: '◑' },
];
const SECTION_IDS = SECTIONS.map((s) => s.id);
const DEFAULT_SECTION = 'builder';

let _active = DEFAULT_SECTION;
let _railHost = null;
let _sectionHost = null;
let _ctx = null;
let _config = null;         // /builder/config public dict (assistant section)
let _builderMounted = false; // the builder owns its own shared draft + chrome

function normaliseSection(id) {
  return SECTION_IDS.includes(id) ? id : DEFAULT_SECTION;
}

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
    _builderMounted = false;
  }

  renderRail();
  await renderSection();
}

function renderRail() {
  const digest = 'rail|' + _active;
  gatedSwap(_railHost, digest, () => SECTIONS.map((s) => {
    const item = el('a', {
      class: 'dn-set-railitem' + (s.id === _active ? ' dn-set-railitem-active' : ''),
      // the builder section is the canonical `#/builder` deep-link; the rest
      // ride the settings route so each section is itself deep-linkable.
      href: s.id === 'builder' ? _ctx.href('builder', {}) : _ctx.href('settings', { section: s.id }),
      'aria-current': s.id === _active ? 'page' : null,
    }, [
      el('span', { class: 'dn-set-railglyph', 'aria-hidden': 'true', text: s.glyph }),
      el('span', { class: 'dn-set-raillabel', text: s.label }),
    ]);
    return item;
  }));
}

async function renderSection() {
  if (_active === 'builder') {
    // RE-HOME: hand the section host straight to the B2 builder view. It owns
    // its own digest-gated chrome + shared draft, so we mount it once and let
    // its own re-dispatch path keep it fresh. (Clearing+remounting on every
    // state tick would reset the operator's place — so we mount once.)
    if (!_builderMounted) {
      clearChildren(_sectionHost);
      _sectionHost.removeAttribute('data-t-digest');
      _builderMounted = true;
    }
    await builder.render(_sectionHost);
    return;
  }
  // Leaving the builder section: drop its mount flag so a return re-mounts it.
  _builderMounted = false;

  switch (_active) {
    case 'contract': return renderContract();
    case 'assistant': return renderAssistant();
    case 'appearance': return renderAppearance();
    default: return renderContract();
  }
}

// ── Contract — read-mostly at-a-glance of the current epoch ───────────
//
// board · brief · scoring · proposer · overfitting. Reuses /api/epoch (the
// authoritative contract) + the builder draft for scoring/overfitting detail.
// Each row links into the builder to edit, so this stays read-only.

async function renderContract() {
  const ep = await data.epoch();
  const c = ep || {};
  const board = Array.isArray(c.board) ? c.board : [];
  const tournament = (c.tournament && typeof c.tournament === 'object') ? c.tournament : null;
  const structure = (tournament && tournament.structure) || 'gauntlet';
  const brief = c.brief || '';
  const scoring = (c.scoring && typeof c.scoring === 'object') ? c.scoring : {};
  const overfitting = scoring.overfitting || c.overfitting || {};
  const proposer = (c.proposer && typeof c.proposer === 'object') ? c.proposer : null;

  const digest = JSON.stringify({
    epoch: c.epoch_id || null, board: board.length, structure,
    briefLen: brief.length, margin: scoring.promote_margin,
    mono: !!scoring.pass_rate_monotonicity,
    holdFrac: overfitting.holdout_fraction, ofEnabled: overfitting.enabled,
    proposer: proposer ? (proposer.has_custom_agent ? 'agent' : 'skills') : null,
  });

  gatedSwap(_sectionHost, 'contract|' + digest, () => {
    if (!ep) return [empty('No epoch contract is available yet.')];
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
    return [
      section('Contract — current epoch',
        el('p', { class: 'dn-lede', text: 'A read-only at-a-glance of the evaluation contract this epoch runs on. Open the tournament builder to edit any of it (a change rolls the epoch).' }),
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

// ── Builder assistant — builder.json config (model NAME only) ─────────
//
// Surfaces the copilot's configured model name + the api_key_env NAME (never a
// secret value) + chat_enabled, from GET /builder/config.

async function renderAssistant() {
  if (_config == null) _config = await getConfig();
  const cfg = _config || { chat_enabled: false, agent: {}, skills: [] };
  const agent = cfg.agent || {};
  const skills = Array.isArray(cfg.skills) ? cfg.skills : [];
  const digest = JSON.stringify({
    chat: !!cfg.chat_enabled, model: agent.model || '',
    keyEnv: agent.api_key_env || '', endpoint: agent.endpoint || '', skills,
  });
  gatedSwap(_sectionHost, 'assistant|' + digest, () => [
    section('Builder assistant',
      el('p', { class: 'dn-lede', text: 'How the tournament-builder copilot reaches a model — read from builder.json. The model name and the API-key environment-variable NAME are shown; a secret value is never read or surfaced here.' }),
      el('div', { class: 'dn-set-kvgrid' }, [
        kv('chat', cfg.chat_enabled ? 'enabled' : 'disabled (no model configured)'),
        kv('model', agent.model || '—', true),
        kv('endpoint', agent.endpoint || 'provider default', true),
        kv('api key env', agent.api_key_env || '—', true),
      ]),
      skills.length
        ? el('div', { class: 'dn-set-panel' }, [
            el('div', { class: 'dn-subhead', text: 'composed builder skills' }),
            el('ul', { class: 'dn-bld-skills' }, skills.map((s) => el('li', null, [
              el('span', { class: 'dn-bld-skill-name', text: typeof s === 'string' ? s : (s.name || '') }),
            ]))),
          ])
        : el('p', { class: 'dn-faint', text: 'No composed builder skills.' }),
      cfg.chat_enabled
        ? null
        : el('p', { class: 'dn-faint', text: 'Configure the agent block in builder.json to enable the chat copilot; the builder still works form-only without it.' })),
  ]);
}

function kv(k, v, mono) {
  return el('div', { class: 'dn-set-kvrow dn-set-kvrow-static' }, [
    el('span', { class: 'dn-set-k', text: k }),
    el('span', { class: 'dn-set-v' + (mono ? ' dn-mono' : ''), text: v }),
  ]);
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
  const scale = readScale();
  const rail = readRail();
  gatedSwap(_sectionHost, `appearance|${color}|${type}|${scale}|${rail}`, () => [
    section('Appearance',
      el('p', { class: 'dn-lede', text: 'Colour theme, typeface, page scale, and side-panel width — all persistent and shared with the top-bar controls (change either, the other follows).' }),
      el('div', { class: 'dn-set-appgrid' }, [
        appRow('Colour theme', themePicker(color)),
        appRow('Typeface', typefacePicker(type)),
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

// COLOUR THEME — a native <select> over all themes, wired to applyTheme (which
// stamps the root, persists, AND syncs the top-bar swatch dropdown).
function themePicker(current) {
  const sel = el('select', { class: 'dn-set-select', 'aria-label': 'Colour theme' },
    COLOR_THEMES.map(([id, label]) => {
      const opt = el('option', { value: id, text: label });
      if (id === current) opt.setAttribute('selected', 'selected');
      return opt;
    }));
  sel.addEventListener('change', () => {
    const v = sel.value != null ? sel.value : sel.getAttribute('value');
    applyTheme(v);
  });
  return sel;
}

// TYPEFACE — the same three-way button group idiom the top bar uses, wired to
// applyTypeface (root + persist + top-bar sync).
function typefacePicker(current) {
  const btns = TYPE_THEMES.map(([id, label]) => {
    const b = el('button', {
      class: 'dn-set-typebtn' + (id === current ? ' dn-set-typebtn-on' : ''),
      type: 'button', 'data-type': id, 'aria-pressed': String(id === current),
      title: 'typeface: ' + id, text: label,
    });
    b.addEventListener('click', () => { applyTypeface(id); _redraw(); });
    return b;
  });
  return el('div', { class: 'dn-set-typeswitch', role: 'group', 'aria-label': 'Typeface' }, btns);
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

// Re-render the appearance section after a control that changes a selected-state
// class (the typeface buttons) so the active button highlights immediately.
function _redraw() {
  if (_active === 'appearance' && _sectionHost) {
    _sectionHost.removeAttribute('data-t-digest');
    renderAppearance();
  }
}

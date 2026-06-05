// variants/T/views/settings.js — the Settings surface (B3).
//
// A wide Console-IV view that HOMES the flagship tournament builder alongside
// the read-mostly contract at-a-glance, the builder-assistant config, the
// appearance pickers, and dashboard prefs. A left section rail drives ONE
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
import { gatedSwap, section, empty, COLOR_THEMES, TYPE_THEMES, readColor, readType } from '../ui.js';
import * as data from '../data.js';
import { getConfig } from '../builder/api.js';
import * as builder from './builder.js';

const SECTIONS = [
  { id: 'builder', label: 'Tournament builder', glyph: '⚒' },
  { id: 'contract', label: 'Contract', glyph: '◷' },
  { id: 'assistant', label: 'Builder assistant', glyph: '✦' },
  { id: 'appearance', label: 'Appearance', glyph: '◑' },
  { id: 'dashboard', label: 'Dashboard', glyph: '▦' },
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
    case 'dashboard': return renderDashboard();
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

// ── Appearance — the colour + typeface pickers ────────────────────────
//
// The theme mechanism already lives in the top bar (the swatch dropdown +
// typeface buttons drive `data-t-theme` / `data-t-type` on the root). Rather
// than duplicate those controls, this section SURFACES the active selections
// and points at the persistent top-bar pickers — theme carry-over, no second
// source of truth.

function renderAppearance() {
  const color = readColor();
  const type = readType();
  const colorLabel = (COLOR_THEMES.find((t) => t[0] === color) || [color, color])[1];
  const typeLabel = (TYPE_THEMES.find((t) => t[0] === type) || [type, type])[1];
  gatedSwap(_sectionHost, `appearance|${color}|${type}`, () => [
    section('Appearance',
      el('p', { class: 'dn-lede', text: 'The colour theme and typeface are persistent and carry across every view. Change them from the pickers in the top bar; the active selections are shown here.' }),
      el('div', { class: 'dn-set-kvgrid' }, [
        kv('colour theme', colorLabel),
        kv('typeface', typeLabel),
      ])),
  ]);
}

// ── Dashboard — existing prefs (read-only roll-up) ────────────────────
//
// Page scale + rail width are persisted by the shell's pickers; surfaced here
// so Settings is a single home for every preference, with the live controls
// staying in the top bar / rail (no second source of truth).

function renderDashboard() {
  let scale = '100%';
  let rail = '—';
  try {
    const s = window.localStorage.getItem('zicato.T.scale');
    if (s) scale = s + '%';
    const r = window.localStorage.getItem('zicato.T.rail');
    if (r) rail = r + 'px';
  } catch (e) { /* private mode */ }
  gatedSwap(_sectionHost, `dashboard|${scale}|${rail}`, () => [
    section('Dashboard',
      el('p', { class: 'dn-lede', text: 'Layout preferences, persisted across sessions. Adjust the page scale from the top-bar pill and the side-panel width from the rail handle.' }),
      el('div', { class: 'dn-set-kvgrid' }, [
        kv('page scale', scale),
        kv('side-panel width', rail),
      ])),
  ]);
}

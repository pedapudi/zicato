// js/swatchdropdown.js — the SHARED colour SWATCH DROPDOWN component.
//
// ONE source of truth for the swatch picker, used IDENTICALLY by the top bar
// (shell.js) and Settings → Appearance (views/settings.js) — NOT forked. The
// CLOSED control is a button showing the current theme's swatch strip + name;
// opening reveals a listbox where each option is a row with its own swatch
// strip (ground · surface · ink · improve · regress · accent — the legibility
// hint, rendered generically from the tuple so it is swatch-count-agnostic) +
// name. The open/close behaviour, the keyboard map, and the cross-instance
// registry are dropdown.js's — this module builds the swatch specimens and the
// trigger, and owns the theme value.
//
// The factory takes an `onChoose(themeId)` so the caller wires the apply path
// (applyTheme) — keeping this component free of any store import (no circular
// dependency with shell.js). Every live instance registers itself so a single
// `syncSwatchDropdowns(value)` call (made from applyTheme) keeps EVERY trigger +
// checked option in lockstep when the theme changes by ANY path (top bar,
// settings, keyboard, restore). Returns { node, setValue }.

import { el, clearChildren, patchText } from './core/dom.js';
import { COLOR_THEMES, normaliseColor } from './ui.js';
import { createSyncRegistry, wireListboxDropdown } from './dropdown.js';

const _registry = createSyncRegistry(normaliseColor);

// Sync EVERY live swatch dropdown to `value` — called from applyTheme so the
// top-bar and settings pickers always show the same selection.
export function syncSwatchDropdowns(value) { _registry.sync(value); }

function swatchStrip(swatches, cls) {
  return el('span', { class: cls || 'dt-swatch-strip', 'aria-hidden': 'true' },
    (swatches || []).map((c) => el('span', { class: 'dt-swatch', style: `background:${c}` })));
}

// Build a swatch dropdown. `onChoose(id)` is invoked with the chosen theme id
// when the user selects an option (the caller applies + persists it).
export function buildSwatchDropdown(initial, onChoose) {
  let value = normaliseColor(initial);
  const byId = new Map(COLOR_THEMES.map((t) => [t[0], t]));

  const triggerSwatch = swatchStrip((byId.get(value) || COLOR_THEMES[0])[2], 'dt-swatch-strip dt-swatch-strip-sm');
  const triggerName = el('span', { class: 'dt-cd-name', text: (byId.get(value) || COLOR_THEMES[0])[1] });
  const trigger = el('button', {
    class: 'dt-cd-trigger', type: 'button',
    'aria-haspopup': 'listbox', 'aria-expanded': 'false',
    'aria-label': 'Colour theme', title: 'Colour theme',
  }, [triggerSwatch, triggerName, el('span', { class: 'dt-cd-caret', 'aria-hidden': 'true', text: '▾' })]);

  const options = COLOR_THEMES.map(([id, label, swatches]) => el('div', {
    class: 'dt-cd-option', role: 'option', 'data-theme': id,
    'aria-selected': String(id === value), tabindex: '-1', title: 'colour: ' + label,
  }, [swatchStrip(swatches), el('span', { class: 'dt-cd-name', text: label })]));
  const listbox = el('div', { class: 'dt-cd-list', role: 'listbox', 'aria-label': 'Colour theme' }, options);

  const node = el('div', { class: 'dt-cd', role: 'group', 'aria-label': 'Colour theme' }, [trigger, listbox]);

  const wiring = wireListboxDropdown({
    node, trigger, listbox, options, valueAttr: 'data-theme',
    getValue: () => value,
    onChoose: (id) => {
      value = normaliseColor(id);
      if (typeof onChoose === 'function') onChoose(value); // caller applies + persists + syncs
    },
  });

  function setValue(v) {
    value = normaliseColor(v);
    const def = byId.get(value) || COLOR_THEMES[0];
    clearChildren(triggerSwatch);
    for (const c of def[2]) triggerSwatch.appendChild(el('span', { class: 'dt-swatch', style: `background:${c}` }));
    patchText(triggerName, def[1]);
    wiring.markSelected();
  }

  // Register for cross-instance sync (top bar ↔ settings, one source of truth).
  _registry.register(setValue);

  return { node, setValue };
}

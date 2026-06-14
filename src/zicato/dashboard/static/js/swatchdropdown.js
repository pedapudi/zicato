// js/swatchdropdown.js — the SHARED colour SWATCH DROPDOWN component.
//
// ONE source of truth for the swatch picker, used IDENTICALLY by the top bar
// (shell.js) and Settings → Appearance (views/settings.js) — NOT forked. The
// CLOSED control is a button showing the current theme's swatch strip + name;
// opening reveals a listbox where each option is a row with its own swatch
// strip (ground · surface · ink · improve · regress · accent — the legibility
// hint, rendered generically from the tuple so it is swatch-count-agnostic) +
// name. Fully keyboard-accessible: Enter/Space/ArrowDown open; within the open
// list ArrowUp/ArrowDown move the active option, Enter/Space select (and apply
// via the injected onChoose), Esc closes back to the trigger; a click outside
// also closes.
//
// The factory takes an `onChoose(themeId)` so the caller wires the apply path
// (applyTheme) — keeping this component free of any store import (no circular
// dependency with shell.js). Every live instance registers itself so a single
// `syncSwatchDropdowns(value)` call (made from applyTheme) keeps EVERY trigger +
// checked option in lockstep when the theme changes by ANY path (top bar,
// settings, keyboard, restore). Returns { node, setValue }.

import { el, clearChildren, patchText, patchClass } from './core/dom.js';
import { COLOR_THEMES, normaliseColor } from './ui.js';

const COLOR_IDS = COLOR_THEMES.map((t) => t[0]);

// Live instances — every mounted dropdown registers its setValue so applyTheme
// can sync them all from one call (top bar ↔ settings, one source of truth).
const _instances = new Set();

// Sync EVERY live swatch dropdown to `value` — called from applyTheme so the
// top-bar and settings pickers always show the same selection.
export function syncSwatchDropdowns(value) {
  const v = normaliseColor(value);
  for (const setValue of _instances) setValue(v);
}

function swatchStrip(swatches, cls) {
  return el('span', { class: cls || 'dt-swatch-strip', 'aria-hidden': 'true' },
    (swatches || []).map((c) => el('span', { class: 'dt-swatch', style: `background:${c}` })));
}

// Build a swatch dropdown. `onChoose(id)` is invoked with the chosen theme id
// when the user selects an option (the caller applies + persists it).
export function buildSwatchDropdown(initial, onChoose) {
  let value = normaliseColor(initial);
  let open = false;
  const byId = new Map(COLOR_THEMES.map((t) => [t[0], t]));

  const triggerSwatch = swatchStrip((byId.get(value) || COLOR_THEMES[0])[2], 'dt-swatch-strip dt-swatch-strip-sm');
  const triggerName = el('span', { class: 'dt-cd-name', text: (byId.get(value) || COLOR_THEMES[0])[1] });
  const trigger = el('button', {
    class: 'dt-cd-trigger', type: 'button',
    'aria-haspopup': 'listbox', 'aria-expanded': 'false',
    'aria-label': 'Colour theme', title: 'Colour theme',
  }, [triggerSwatch, triggerName, el('span', { class: 'dt-cd-caret', 'aria-hidden': 'true', text: '▾' })]);

  const options = COLOR_THEMES.map(([id, label, swatches]) => {
    const opt = el('div', {
      class: 'dt-cd-option', role: 'option', 'data-theme': id,
      'aria-selected': String(id === value), tabindex: '-1', title: 'colour: ' + label,
    }, [swatchStrip(swatches), el('span', { class: 'dt-cd-name', text: label })]);
    opt.addEventListener('click', () => { choose(id); });
    return opt;
  });
  const listbox = el('div', { class: 'dt-cd-list', role: 'listbox', 'aria-label': 'Colour theme' }, options);

  const node = el('div', { class: 'dt-cd', role: 'group', 'aria-label': 'Colour theme' }, [trigger, listbox]);

  let activeIdx = COLOR_IDS.indexOf(value);
  function setActive(i) {
    activeIdx = (i + options.length) % options.length;
    options.forEach((o, k) => patchClass(o, 'dt-cd-active', k === activeIdx));
  }
  function setOpen(next) {
    open = next;
    patchClass(node, 'dt-cd-open', open);
    trigger.setAttribute('aria-expanded', String(open));
    if (open) setActive(Math.max(0, COLOR_IDS.indexOf(value)));
  }
  function choose(id) {
    value = normaliseColor(id);
    if (typeof onChoose === 'function') onChoose(value); // caller applies + persists + syncs
    setOpen(false);
  }
  function setValue(v) {
    value = normaliseColor(v);
    const def = byId.get(value) || COLOR_THEMES[0];
    clearChildren(triggerSwatch);
    for (const c of def[2]) triggerSwatch.appendChild(el('span', { class: 'dt-swatch', style: `background:${c}` }));
    patchText(triggerName, def[1]);
    options.forEach((o) => o.setAttribute('aria-selected', String(o.getAttribute('data-theme') === value)));
  }

  trigger.addEventListener('click', () => setOpen(!open));
  trigger.addEventListener('keydown', (ev) => {
    const k = ev.key;
    if (k === 'ArrowDown' || k === 'Enter' || k === ' ' || k === 'Spacebar') {
      ev.preventDefault(); setOpen(true);
    }
  });
  listbox.addEventListener('keydown', (ev) => {
    const k = ev.key;
    if (k === 'Escape') { ev.preventDefault(); setOpen(false); }
    else if (k === 'ArrowDown') { ev.preventDefault(); setActive(activeIdx + 1); }
    else if (k === 'ArrowUp') { ev.preventDefault(); setActive(activeIdx - 1); }
    else if (k === 'Enter' || k === ' ' || k === 'Spacebar') {
      ev.preventDefault();
      const id = options[activeIdx] && options[activeIdx].getAttribute('data-theme');
      if (id) choose(id);
    }
  });
  // a click anywhere outside the control closes it.
  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('click', (ev) => {
      if (!open) return;
      let n = ev && ev.target;
      while (n) { if (n === node) return; n = n.parentNode; }
      setOpen(false);
    });
  }

  // Register for cross-instance sync (top bar ↔ settings, one source of truth).
  _instances.add(setValue);

  return { node, setValue };
}

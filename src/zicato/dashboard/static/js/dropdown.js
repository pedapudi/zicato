// js/dropdown.js — the behaviour the console's pickers share.
//
// The colour-swatch picker and the typeface picker are two type specimens over
// ONE interaction: a trigger that opens a listbox of option rows, each row
// carrying its value in a data attribute. The visuals stay with each picker;
// the open/close state, active-row tracking, keyboard map, outside-click close
// and cross-instance sync are that one interaction and live here.

import { patchClass } from './core/dom.js';

// A registry of the mounted copies of one picker. The top bar and Settings →
// Appearance each mount their own, and a choice in either must show in both,
// so each registers a setter and the apply path fans one value out to all.
// Registrations are never removed: a setter left behind by a rebuilt Settings
// panel writes to a detached node harmlessly.
export function createSyncRegistry(normalise) {
  const instances = new Set();
  return {
    register(setValue) { instances.add(setValue); },
    sync(value) {
      const v = normalise(value);
      for (const setValue of instances) setValue(v);
    },
  };
}

// Wire the shared interaction onto an already-built picker. `spec`: the root
// `node` (carries `dt-cd-open`), the `trigger`, the `listbox` that owns the key
// handler, the FLAT ordered `options` (group headers are NOT in it, so arrow
// navigation skips them), the `valueAttr` each row carries its value in, a
// `getValue` reader, and `onChoose(value)`. The value stays in the picker,
// which owns normalisation — this module reads it through `getValue` and never
// holds a second copy that could drift. Returns { markSelected }, which
// repaints the rows' `aria-selected` for the picker's own setValue.
export function wireListboxDropdown(spec) {
  const { node, trigger, listbox, options, valueAttr, getValue, onChoose } = spec;
  let open = false;

  const indexOf = (v) => options.findIndex((o) => o.getAttribute(valueAttr) === v);
  let activeIdx = Math.max(0, indexOf(getValue()));

  function setActive(i) {
    activeIdx = (i + options.length) % options.length;
    options.forEach((o, k) => patchClass(o, 'dt-cd-active', k === activeIdx));
  }
  function setOpen(next) {
    open = next;
    patchClass(node, 'dt-cd-open', open);
    trigger.setAttribute('aria-expanded', String(open));
    if (open) setActive(Math.max(0, indexOf(getValue())));
  }
  function choose(id) {
    onChoose(id);
    setOpen(false);
  }
  function markSelected() {
    const v = getValue();
    options.forEach((o) => o.setAttribute('aria-selected', String(o.getAttribute(valueAttr) === v)));
  }

  for (const opt of options) {
    opt.addEventListener('click', () => { choose(opt.getAttribute(valueAttr)); });
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
      const id = options[activeIdx] && options[activeIdx].getAttribute(valueAttr);
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

  return { markSelected };
}

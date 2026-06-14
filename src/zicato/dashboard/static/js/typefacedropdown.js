// js/typefacedropdown.js — the SHARED typeface GROUPED-POPOVER picker.
//
// ONE source of truth for the typeface picker, used IDENTICALLY by the top bar
// (shell.js) and Settings → Appearance (views/settings.js) — NOT forked. It
// reuses the colour picker's `dt-cd` popover idiom (trigger + listbox) so it
// matches the Console "winning" picker style, but the listbox is GROUPED: three
// mode headers (Technical · Editorial · Display) each followed by their FOUR
// finalized options = TWELVE total. Each option row shows the face name plus a
// MICRO-PREVIEW rendered in that option's REAL faces (head / prose / data), so
// the popover reads as a true type specimen.
//
// The CLOSED control is a button showing the current option's label + a tiny
// preview in its faces; opening reveals the grouped listbox. Fully keyboard-
// accessible like the colour picker: Enter/Space/ArrowDown open; within the open
// list ArrowUp/ArrowDown move the active OPTION (skipping the group headers),
// Enter/Space select (and apply via the injected onChoose), Esc closes; a click
// outside also closes.
//
// The factory takes an `onChoose(id)` so the caller wires the apply path
// (applyTypeface) — keeping this component free of any store import (no circular
// dependency with shell.js). Every live instance registers itself so a single
// `syncTypefaceDropdowns(value)` call (made from applyTypeface) keeps EVERY
// trigger + checked option in lockstep when the typeface changes by ANY path
// (top bar, settings, keyboard, restore). Returns { node, setValue }.

import { el, clearChildren, patchText, patchClass } from './core/dom.js';
import {
  TYPE_OPTIONS, TYPE_MODE_ORDER, TYPE_MODE_LABEL, normaliseType, typeOption,
  FONTSIZE_OPTIONS, normaliseFontSize,
} from './ui.js';

// Live instances — every mounted dropdown registers its setValue so
// applyTypeface can sync them all from one call (top bar ↔ settings, one store).
const _instances = new Set();
// The S/M/L font-size segmented controls registered for cross-instance sync,
// mirroring _instances — applyFontSize fans out to every one so the top-bar and
// Settings pickers always show the same size.
const _sizeInstances = new Set();

// Sync EVERY live typeface dropdown to `value` — called from applyTypeface so
// the top-bar and settings pickers always show the same selection.
export function syncTypefaceDropdowns(value) {
  const v = normaliseType(value);
  for (const setValue of _instances) setValue(v);
}

// Sync EVERY live S/M/L font-size segmented control to `value` — called from
// applyFontSize so the top-bar and settings pickers stay in lockstep.
export function syncFontSizeSegments(value) {
  const v = normaliseFontSize(value);
  for (const setSize of _sizeInstances) setSize(v);
}

// Build the compact S/M/L segmented control that lives in the typeface popover
// footer. Keyboard-accessible like the rest of the picker (each segment is a
// real <button>; ArrowLeft/ArrowRight move between them). `onSizeChoose(id)` is
// invoked with the chosen size id (the caller applies + persists + syncs). The
// component is store-free (mirrors the typeface dropdown idiom); it registers a
// `setSize` for cross-instance sync. Returns { node, setSize }.
function buildFontSizeSegment(initial, onSizeChoose) {
  let size = normaliseFontSize(initial);
  const segs = [];
  for (const o of FONTSIZE_OPTIONS) {
    const b = el('button', {
      class: 'dt-tf-sizeseg', type: 'button', role: 'radio',
      'data-fontsize': o.id, 'aria-checked': String(o.id === size),
      title: o.title, 'aria-label': o.title, text: o.label,
    });
    b.addEventListener('click', () => choose(o.id));
    b.addEventListener('keydown', (ev) => {
      const k = ev.key;
      if (k === 'ArrowRight' || k === 'ArrowDown') { ev.preventDefault(); step(o.id, 1); }
      else if (k === 'ArrowLeft' || k === 'ArrowUp') { ev.preventDefault(); step(o.id, -1); }
    });
    segs.push(b);
  }
  function idxOf(id) { return FONTSIZE_OPTIONS.findIndex((o) => o.id === id); }
  function step(fromId, dir) {
    const n = FONTSIZE_OPTIONS.length;
    const next = FONTSIZE_OPTIONS[(idxOf(fromId) + dir + n) % n].id;
    choose(next);
    const target = segs[idxOf(next)];
    if (target && typeof target.focus === 'function') target.focus();
  }
  function choose(id) {
    size = normaliseFontSize(id);
    if (typeof onSizeChoose === 'function') onSizeChoose(size); // caller applies + persists + syncs
  }
  function setSize(v) {
    size = normaliseFontSize(v);
    segs.forEach((b) => b.setAttribute('aria-checked', String(b.getAttribute('data-fontsize') === size)));
  }
  const node = el('div', {
    class: 'dt-tf-foot', role: 'group', 'aria-label': 'Text size',
  }, [
    el('span', { class: 'dt-tf-foot-lab', 'aria-hidden': 'true', text: 'size' }),
    el('div', { class: 'dt-tf-sizeseg-wrap', role: 'radiogroup', 'aria-label': 'Text size' }, segs),
  ]);
  _sizeInstances.add(setSize);
  return { node, setSize };
}

// A micro type-specimen: "Aa" in the head face, a short prose word in the prose
// face, and a tabular sample in the data face — so each row previews the option
// in its REAL faces. `cls` distinguishes the trigger (small) from the option
// rows. The spans inline their font-family so the preview never depends on the
// root's data-t-type (a true per-option specimen).
function specimen(opt, cls) {
  return el('span', { class: cls || 'dt-tf-spec', 'aria-hidden': 'true' }, [
    el('span', { class: 'dt-tf-spec-head', style: `font-family:${opt.head}`, text: 'Aa' }),
    el('span', { class: 'dt-tf-spec-prose', style: `font-family:${opt.prose}`, text: 'prose' }),
    el('span', { class: 'dt-tf-spec-data', style: `font-family:${opt.data}`, text: '0.418' }),
  ]);
}

// Build a typeface grouped-popover dropdown. `onChoose(id)` is invoked with the
// chosen option id when the user selects a row (the caller applies + persists).
// `opts` (optional) carries the S/M/L font-size control: `{ size, onSizeChoose }`
// — when present, a compact segmented control is mounted in the popover FOOTER
// so the operator can step the text size right where they pick the face. The
// caller wires `onSizeChoose(id)` to applyFontSize (store-free here, like the
// face callback); the segment registers for cross-instance sync.
export function buildTypefaceDropdown(initial, onChoose, opts) {
  const cfg = opts || {};
  let value = normaliseType(initial);
  let open = false;

  const triggerSpec = specimen(typeOption(value), 'dt-tf-spec dt-tf-spec-sm');
  const triggerName = el('span', { class: 'dt-cd-name', text: typeOption(value).label });
  const trigger = el('button', {
    class: 'dt-cd-trigger dt-tf-trigger', type: 'button',
    'aria-haspopup': 'listbox', 'aria-expanded': 'false',
    'aria-label': 'Typeface', title: 'Typeface',
  }, [triggerSpec, triggerName, el('span', { class: 'dt-cd-caret', 'aria-hidden': 'true', text: '▾' })]);

  // Build the GROUPED listbox: a header per mode, then its four option rows.
  // `options` is the FLAT ordered list of selectable option nodes (headers are
  // NOT in it) so keyboard nav + active tracking skip the headers cleanly.
  const options = [];
  const listChildren = [];
  for (const mode of TYPE_MODE_ORDER) {
    listChildren.push(el('div', {
      class: 'dt-cd-group', role: 'presentation', 'aria-hidden': 'true',
      text: TYPE_MODE_LABEL[mode] || mode,
    }));
    for (const o of TYPE_OPTIONS.filter((x) => x.mode === mode)) {
      const opt = el('div', {
        class: 'dt-tf-option', role: 'option', 'data-type': o.id,
        'aria-selected': String(o.id === value), tabindex: '-1',
        title: 'typeface: ' + o.label,
      }, [
        el('span', { class: 'dt-cd-name dt-tf-name', text: o.label }),
        specimen(o, 'dt-tf-spec'),
      ]);
      opt.addEventListener('click', () => { choose(o.id); });
      options.push(opt);
      listChildren.push(opt);
    }
  }
  const listbox = el('div', { class: 'dt-cd-list dt-tf-list', role: 'listbox', 'aria-label': 'Typeface' }, listChildren);

  // The S/M/L text-size segmented control — built only when the caller wires it.
  // It rides in the popover FOOTER (the same `dt-cd-open` popover that holds the
  // listbox), reading the current size + applying on click via onSizeChoose.
  let sizeCtl = null;
  if (typeof cfg.onSizeChoose === 'function') {
    sizeCtl = buildFontSizeSegment(cfg.size, cfg.onSizeChoose);
  }

  const popChildren = [listbox];
  if (sizeCtl) popChildren.push(sizeCtl.node);
  const popover = el('div', { class: 'dt-tf-pop' }, popChildren);
  const node = el('div', { class: 'dt-cd dt-tf', role: 'group', 'aria-label': 'Typeface' }, [trigger, popover]);

  const idxOf = (v) => options.findIndex((o) => o.getAttribute('data-type') === v);
  let activeIdx = Math.max(0, idxOf(value));
  function setActive(i) {
    activeIdx = (i + options.length) % options.length;
    options.forEach((o, k) => patchClass(o, 'dt-cd-active', k === activeIdx));
  }
  function setOpen(next) {
    open = next;
    patchClass(node, 'dt-cd-open', open);
    trigger.setAttribute('aria-expanded', String(open));
    if (open) setActive(Math.max(0, idxOf(value)));
  }
  function choose(id) {
    value = normaliseType(id);
    if (typeof onChoose === 'function') onChoose(value); // caller applies + persists + syncs
    setOpen(false);
  }
  function setValue(v) {
    value = normaliseType(v);
    const def = typeOption(value);
    clearChildren(triggerSpec);
    triggerSpec.appendChild(el('span', { class: 'dt-tf-spec-head', style: `font-family:${def.head}`, text: 'Aa' }));
    triggerSpec.appendChild(el('span', { class: 'dt-tf-spec-prose', style: `font-family:${def.prose}`, text: 'prose' }));
    triggerSpec.appendChild(el('span', { class: 'dt-tf-spec-data', style: `font-family:${def.data}`, text: '0.418' }));
    patchText(triggerName, def.label);
    options.forEach((o) => o.setAttribute('aria-selected', String(o.getAttribute('data-type') === value)));
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
      const id = options[activeIdx] && options[activeIdx].getAttribute('data-type');
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

  // Expose the S/M/L segment's setSize when present so a caller can drive it too
  // (cross-instance sync already fans out via syncFontSizeSegments).
  return { node, setValue, setSize: sizeCtl ? sizeCtl.setSize : undefined };
}

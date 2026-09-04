// test/dropdown.test.mjs — the interaction the colour-swatch and typeface
// pickers share (js/dropdown.js), exercised through both pickers.
//
// The two pickers build different specimens over one behaviour: open/close,
// active-row tracking with wrap-around, the keyboard map, closing on an
// outside click, and the registry that keeps every mounted copy of a picker
// showing the same selection. Because both read the same wiring, an assertion
// here covers both — and a change to the wiring cannot silently break one of
// them.

import { installDom, makeEvent, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { buildSwatchDropdown, syncSwatchDropdowns } = await import('../js/swatchdropdown.js');
const { buildTypefaceDropdown, syncTypefaceDropdowns, syncFontSizeSegments } = await import('../js/typefacedropdown.js');

const cls = (n) => (n._attrs && n._attrs.class) || '';
const fire = (node, type, props) => {
  const ev = makeEvent(type, props);
  ev.target = node;
  (node._listeners && node._listeners[type] || []).forEach((fn) => fn(ev));
};
const docClick = (target) => {
  const ev = makeEvent('click');
  ev.target = target;
  (document._listeners && document._listeners.click || []).forEach((fn) => fn(ev));
};
const parts = (picker) => ({
  trigger: picker.node.querySelector('[aria-haspopup]'),
  listbox: picker.node.querySelectorAll('[role="listbox"]')[0],
  options: picker.node.querySelectorAll('[role="option"]'),
});
const selected = (picker, attr) => picker.node.querySelectorAll('[role="option"]')
  .filter((o) => o.getAttribute('aria-selected') === 'true')
  .map((o) => o.getAttribute(attr));
const activeIndex = (options) => options.findIndex((o) => cls(o).includes('dt-cd-active'));

// Both pickers, so every assertion below runs against each. `other` is a legal
// value distinct from the initial one; `attr` is where a row carries its value.
const PICKERS = [
  { name: 'swatch', attr: 'data-theme', initial: 'monokai', other: 'dracula',
    build: (onChoose) => buildSwatchDropdown('monokai', onChoose) },
  { name: 'typeface', attr: 'data-type', initial: 'google-sans-mono', other: 'fraunces',
    build: (onChoose) => buildTypefaceDropdown('google-sans-mono', onChoose) },
];

for (const p of PICKERS) {
  test(`${p.name} picker: the trigger opens and closes, and mirrors aria-expanded`, () => {
    const picker = p.build(() => {});
    const { trigger, listbox } = parts(picker);
    assertEqual(trigger.getAttribute('aria-expanded'), 'false', 'starts closed');
    assert(!cls(picker.node).includes('dt-cd-open'), 'the root carries no open class');

    fire(trigger, 'click');
    assertEqual(trigger.getAttribute('aria-expanded'), 'true', 'a trigger click opens');
    assert(cls(picker.node).includes('dt-cd-open'), 'the root carries the open class');

    fire(listbox, 'keydown', { key: 'Escape' });
    assertEqual(trigger.getAttribute('aria-expanded'), 'false', 'Escape closes');
    assert(!cls(picker.node).includes('dt-cd-open'), 'the open class is dropped');
  });

  test(`${p.name} picker: arrow keys move the active row and wrap`, () => {
    const picker = p.build(() => {});
    const { trigger, listbox, options } = parts(picker);
    fire(trigger, 'keydown', { key: 'ArrowDown' });
    assertEqual(trigger.getAttribute('aria-expanded'), 'true', 'ArrowDown on the trigger opens');
    assertEqual(activeIndex(options), 0, 'the selected row starts active');

    fire(listbox, 'keydown', { key: 'ArrowDown' });
    assertEqual(activeIndex(options), 1, 'ArrowDown advances');
    fire(listbox, 'keydown', { key: 'ArrowUp' });
    fire(listbox, 'keydown', { key: 'ArrowUp' });
    assertEqual(activeIndex(options), options.length - 1, 'ArrowUp past the top wraps to the last row');
  });

  test(`${p.name} picker: Enter and a row click both choose, then close`, () => {
    let chosen = null;
    const picker = p.build((id) => { chosen = id; });
    const { trigger, listbox, options } = parts(picker);

    fire(trigger, 'click');
    fire(listbox, 'keydown', { key: 'ArrowDown' });
    fire(listbox, 'keydown', { key: 'Enter' });
    assertEqual(chosen, options[1].getAttribute(p.attr), 'Enter chooses the active row');
    assertEqual(trigger.getAttribute('aria-expanded'), 'false', 'choosing closes the list');

    fire(trigger, 'click');
    const last = options[options.length - 1];
    fire(last, 'click');
    assertEqual(chosen, last.getAttribute(p.attr), 'a row click chooses that row');
    assertEqual(trigger.getAttribute('aria-expanded'), 'false', 'a row click closes the list');
  });

  test(`${p.name} picker: a click outside closes, a click inside does not`, () => {
    const picker = p.build(() => {});
    const { trigger, options } = parts(picker);
    fire(trigger, 'click');
    docClick(options[0]);
    assertEqual(trigger.getAttribute('aria-expanded'), 'true', 'a click inside the picker keeps it open');
    docClick(document.createElement('div'));
    assertEqual(trigger.getAttribute('aria-expanded'), 'false', 'a click outside closes it');
  });

  test(`${p.name} picker: setValue repaints which row reads as selected`, () => {
    const picker = p.build(() => {});
    assert(selected(picker, p.attr).join(',') === p.initial, 'the initial value is the selected row');
    picker.setValue(p.other);
    assertEqual(selected(picker, p.attr).join(','), p.other, 'setValue moves aria-selected');
  });
}

test('a colour choice reaches every mounted swatch picker', () => {
  const a = buildSwatchDropdown('monokai', () => {});
  const b = buildSwatchDropdown('monokai', () => {});
  syncSwatchDropdowns('paper');
  assertEqual(selected(a, 'data-theme').join(','), 'paper', 'the first picker follows the fan-out');
  assertEqual(selected(b, 'data-theme').join(','), 'paper', 'so does the second');
  // The fan-out normalises, so an unknown id lands on the default rather than
  // leaving the pickers showing nothing.
  syncSwatchDropdowns('nonesuch');
  assertEqual(selected(b, 'data-theme').join(','), 'monokai', 'an unknown theme normalises to the default');
});

test('a typeface choice reaches every mounted picker, and a mode id migrates', () => {
  const a = buildTypefaceDropdown('google-sans-mono', () => {});
  const b = buildTypefaceDropdown('google-sans-mono', () => {});
  syncTypefaceDropdowns('archivo-narrow');
  assertEqual(selected(a, 'data-type').join(','), 'archivo-narrow', 'the first picker follows the fan-out');
  assertEqual(selected(b, 'data-type').join(','), 'archivo-narrow', 'so does the second');
  // A stored MODE id is not itself an option id; it migrates to that group's
  // first face rather than snapping back to the global default.
  syncTypefaceDropdowns('editorial');
  assertEqual(selected(b, 'data-type').join(','), 'fraunces', 'a mode id resolves to its group\'s first face');
});

test('the text-size segment in the typeface popover chooses and syncs', () => {
  let size = null;
  const picker = buildTypefaceDropdown('google-sans-mono', () => {}, { size: 'small', onSizeChoose: (id) => { size = id; } });
  const segs = picker.node.querySelectorAll('[data-fontsize]');
  assertEqual(segs.length, 3, 'three size segments');
  assertEqual(segs.filter((s) => s.getAttribute('aria-checked') === 'true').map((s) => s.getAttribute('data-fontsize')).join(','),
    'small', 'the initial size is checked');
  fire(segs[2], 'click');
  assertEqual(size, 'large', 'clicking a segment reports that size');
  syncFontSizeSegments('medium');
  assertEqual(segs.filter((s) => s.getAttribute('aria-checked') === 'true').map((s) => s.getAttribute('data-fontsize')).join(','),
    'medium', 'the fan-out moves the checked segment');
});

await run();

// js/builder/popover.js — a small reusable info popover.
//
// The trigger is a SINGLE circular affordance: the CSS draws the ring (a
// faint, theme-token circle) and the glyph inside is a plain lowercase `i`
// (NOT the pre-circled ⓘ — that stacked a circle inside the CSS circle and
// read as two concentric rings). One ring, one mark.
//
// The additional-detail surface every param control carries: the title, the
// default, the help paragraphs (`body`; a blank line separates paragraphs and
// a newline separates the lines of a list), and an optional `note` about the
// control itself, set faint under the help. Accessible — a real <button>
// trigger (keyboard-focusable; Enter/Space/Escape), aria-expanded +
// aria-describedby wiring, and the panel marked role="note". It opens on
// hover OR focus and closes on blur / mouseleave / Escape. Positioned with
// CSS so it never clips: the panel anchors to the trigger and the stylesheet
// flips it to stay on-screen (the panel is `position:absolute` inside a
// `position:relative` wrapper with max-width + overflow guards).

import { el } from '../core/dom.js';

let _idSeq = 0;

// One <p> per paragraph; the lines of a paragraph (a list's items) are
// separated by <br>, since the panel's text does not preserve newlines.
function bodyParagraphs(body) {
  if (!body) return [];
  return String(body).split(/\n\s*\n/).map((para) => {
    const lines = para.split('\n');
    const children = [];
    lines.forEach((line, i) => {
      if (i) children.push(el('br'));
      children.push(line);
    });
    return el('p', { class: 'dn-bld-pop-body' }, children);
  });
}

export function infoPopover(info) {
  const data = info || {};
  const panelId = 'dn-pop-' + (_idSeq += 1);

  const panel = el('div', {
    class: 'dn-bld-pop', role: 'note', id: panelId, 'aria-hidden': 'true',
  }, [
    data.title ? el('div', { class: 'dn-bld-pop-title', text: data.title }) : null,
    data.def != null ? el('div', { class: 'dn-bld-pop-def' }, [
      el('span', { class: 'dn-bld-pop-def-k', text: 'default ' }),
      el('span', { class: 'dn-bld-pop-def-v dn-mono', text: String(data.def) }),
    ]) : null,
    ...bodyParagraphs(data.body),
    data.note ? el('p', { class: 'dn-bld-pop-body dn-faint', text: data.note }) : null,
  ].filter(Boolean));

  const trigger = el('button', {
    class: 'dn-bld-info', type: 'button',
    'aria-label': (data.title ? data.title + ': ' : '') + 'more detail',
    title: (data.title ? data.title + ': ' : '') + 'more detail',
    'aria-expanded': 'false', 'aria-describedby': panelId,
    // a plain lowercase `i`; the CSS ring supplies the circle (no doubled glyph).
    text: 'i',
  });

  let open = false;
  const setOpen = (next) => {
    if (open === next) return;
    open = next;
    trigger.setAttribute('aria-expanded', String(open));
    panel.setAttribute('aria-hidden', String(!open));
    if (open) wrap.classList.add('dn-bld-pop-open');
    else wrap.classList.remove('dn-bld-pop-open');
  };

  trigger.addEventListener('click', () => setOpen(!open));
  trigger.addEventListener('focus', () => setOpen(true));
  trigger.addEventListener('blur', () => setOpen(false));
  trigger.addEventListener('mouseenter', () => setOpen(true));
  trigger.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') { ev.preventDefault && ev.preventDefault(); setOpen(false); }
  });

  const wrap = el('span', { class: 'dn-bld-popwrap' }, [trigger, panel]);
  wrap.addEventListener('mouseleave', () => setOpen(false));

  // exposed for tests: read the open state off the DOM (aria-hidden).
  return wrap;
}

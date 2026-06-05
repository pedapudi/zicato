// variants/T/builder/popover.js — a small reusable info popover (ⓘ).
//
// The additional-detail surface every param control carries: definition,
// default, and the cost/contract tradeoff. Accessible — a real <button>
// trigger (keyboard-focusable; Enter/Space/Escape), aria-expanded +
// aria-describedby wiring, and the panel marked role="note". It opens on
// hover OR focus and closes on blur / mouseleave / Escape. Positioned with
// CSS so it never clips: the panel anchors to the trigger and the stylesheet
// flips it to stay on-screen (the panel is `position:absolute` inside a
// `position:relative` wrapper with max-width + overflow guards).

import { el } from '../../../core/dom.js';

let _idSeq = 0;

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
    data.body ? el('p', { class: 'dn-bld-pop-body', text: data.body }) : null,
  ].filter(Boolean));

  const trigger = el('button', {
    class: 'dn-bld-info', type: 'button',
    'aria-label': (data.title ? data.title + ': ' : '') + 'more detail',
    'aria-expanded': 'false', 'aria-describedby': panelId,
    text: 'ⓘ',
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

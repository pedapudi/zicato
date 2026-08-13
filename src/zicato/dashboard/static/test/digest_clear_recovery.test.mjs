// test/digest_clear_recovery.test.mjs — an emptied host must not claim a digest.
//
// The settings-overlay kill sequence (the "clicking settings blanks the page"
// bug): the shell clears the view host, the view re-appends its loading
// placeholder, the data lands, and gatedSwap compares the incoming digest to
// the attribute STILL on the host from the paint that was just thrown away —
// match, firstChild truthy, paint skipped. The placeholder then stands
// forever; on a settled workspace nothing ever bumps the digest to break the
// deadlock. The invariant pinned here: the digest describes the children, so
// clearing the children clears the digest, and a repeated digest after a
// clear REPAINTS.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { clearChildren, el } = await import('../js/core/dom.js');
const { gatedSwap } = await import('../js/ui.js');

test('clearChildren drops the render digest with the children', () => {
  const host = document.createElement('div');
  host.appendChild(el('p', { text: 'painted' }));
  host.setAttribute('data-t-digest', 'd1');
  clearChildren(host);
  assertEqual(host.children.length, 0);
  assert(!host.hasAttribute('data-t-digest'), 'an emptied host must not claim a digest');
});

test('gatedSwap repaints a cleared host even when the digest is unchanged', () => {
  const host = document.createElement('div');
  const paint = () => gatedSwap(host, 'same', () => el('p', { class: 'content', text: 'real content' }));

  assert(paint(), 'first paint proceeds');
  assertEqual(host.firstChild.className, 'content');
  assert(!paint(), 'no-op repaint with an unchanged digest is skipped (render discipline)');

  // The kill sequence: shell clears the host, the view re-appends its
  // loading placeholder before awaiting data (views/home.js does exactly
  // this), then the gated paint arrives with the same digest.
  clearChildren(host);
  host.appendChild(el('p', { class: 'dn-empty', text: 'Acquiring fleet telemetry…' }));
  assert(paint(), 'a cleared host must repaint — its DOM no longer matches the digest');
  assertEqual(host.firstChild.className, 'content', 'the placeholder is replaced by the real paint');
});

await run();

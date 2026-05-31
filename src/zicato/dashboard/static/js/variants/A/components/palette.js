// variants/A/components/palette.js — ⌘K command palette.
//
// Jump between epochs / generations / pages. Built from the live state
// (epochs list, current epoch's experiments) so it always reflects the
// real workspace. Keyboard-driven: ⌘K / Ctrl-K to open, ↑↓ to move, ↵
// to go, Esc to close.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import { href } from '../router.js';

let overlay = null, input = null, results = null;
let items = [];
let active = 0;
let open = false;

function buildItems() {
  const out = [
    { kind: 'page', label: 'Environment (home)', href: href('environment') },
    { kind: 'page', label: 'Bench (live ops)', href: href('bench') },
  ];
  const epochs = Array.isArray(state.epochs) ? state.epochs : [];
  for (const e of epochs) {
    if (!e || !e.epoch_id) continue;
    out.push({ kind: 'epoch', label: e.epoch_id + (e.goal ? ' — ' + e.goal : ''), href: href('epoch', { epochId: e.epoch_id }) });
    out.push({ kind: 'lineage', label: 'lineage · ' + e.epoch_id, href: href('tournament', { epochId: e.epoch_id }) });
  }
  const def = state.epochDef;
  if (def && Array.isArray(def.experiments)) {
    for (const x of def.experiments) {
      if (!x || !x.generation_id) continue;
      out.push({ kind: 'gen', label: (def.epoch_id || '') + ' · ' + x.generation_id, href: href('experiment', { epochId: def.epoch_id, genId: x.generation_id }) });
    }
  }
  return out;
}

function filter(q) {
  const all = buildItems();
  const s = q.trim().toLowerCase();
  if (!s) return all.slice(0, 40);
  return all.filter((i) => i.label.toLowerCase().includes(s) || i.kind.includes(s)).slice(0, 40);
}

function render() {
  results.textContent = '';
  items.forEach((it, i) => {
    const node = el('div', {
      class: 'mcA-palette-item' + (i === active ? ' is-active' : ''),
      role: 'option',
    }, [
      el('span', { class: 'mcA-palette-item-kind' }, [it.kind]),
      el('span', { class: 'mcA-palette-item-label' }, [it.label]),
    ]);
    node.addEventListener('click', () => go(it));
    node.addEventListener('mouseenter', () => { active = i; highlight(); });
    results.appendChild(node);
  });
}

function highlight() {
  [...results.children].forEach((c, i) => {
    if (i === active) c.classList.add('is-active'); else c.classList.remove('is-active');
  });
}

function go(it) {
  if (it && it.href) window.location.hash = it.href;
  close();
}

export function openPalette() {
  if (!overlay) return;
  open = true;
  overlay.removeAttribute('hidden');
  input.value = '';
  items = filter('');
  active = 0;
  render();
  setTimeout(() => input.focus && input.focus(), 0);
}

export function close() {
  open = false;
  if (overlay) overlay.setAttribute('hidden', 'true');
}

export function mountPalette(rootHost) {
  overlay = el('div', { class: 'mcA-palette-overlay', hidden: 'true', role: 'dialog', 'aria-modal': 'true' });
  input = el('input', { class: 'mcA-palette-input', type: 'search', placeholder: 'Jump to an epoch, generation, or page…', autocomplete: 'off' });
  results = el('div', { class: 'mcA-palette-results', role: 'listbox' });
  const pal = el('div', { class: 'mcA-palette' }, [
    input, results,
    el('div', { class: 'mcA-palette-foot' }, ['↑↓ navigate   ↵ open   esc close']),
  ]);
  overlay.appendChild(pal);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  input.addEventListener('input', () => { items = filter(input.value); active = 0; render(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); active = Math.min(active + 1, items.length - 1); highlight(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); active = Math.max(active - 1, 0); highlight(); }
    else if (e.key === 'Enter') { e.preventDefault(); if (items[active]) go(items[active]); }
    else if (e.key === 'Escape') { e.preventDefault(); close(); }
  });
  rootHost.appendChild(overlay);

  window.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      if (open) close(); else openPalette();
    }
  });
}

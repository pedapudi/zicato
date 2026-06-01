// variants/M/theme.js — Ledger II's three-COLOUR-theme system + switcher.
//
// Ported from Variant B's three-theme token system in the operator's named
// palettes — solarized-light (DEFAULT for the light-first Ledger II skin),
// solarized-dark, monokai — with a visible switcher. Every mark + diagram
// reads with sufficient contrast in ALL THREE; the per-theme tokens (incl.
// the heatmap/dot ramp endpoints svg.js reads at draw time) live in
// css/variants/M/ledger2.css under `[data-m-theme="…"]` selectors scoped to
// the variant root.
//
// The chosen colour theme is written to `data-m-theme` on the variant root
// AND persisted in localStorage so a reload sticks. Solarized-light is the
// default when nothing is stored — Ledger II is light-first.

export const THEMES = [
  { id: 'solarized-light', label: 'Solarized Light' },
  { id: 'solarized-dark', label: 'Solarized Dark' },
  { id: 'monokai', label: 'Monokai' },
];
const STORAGE_KEY = 'zicato.ui.M.theme';
const DEFAULT_THEME = 'solarized-light';

let _root = null;
let _current = DEFAULT_THEME;
let _buttons = [];

export function currentTheme() { return _current; }

function persisted() {
  try {
    const v = window.localStorage && window.localStorage.getItem(STORAGE_KEY);
    if (v && THEMES.some((t) => t.id === v)) return v;
  } catch { /* storage unavailable */ }
  return DEFAULT_THEME;
}

export function applyTheme(id) {
  const theme = THEMES.some((t) => t.id === id) ? id : DEFAULT_THEME;
  _current = theme;
  if (_root) _root.setAttribute('data-m-theme', theme);
  try { window.localStorage && window.localStorage.setItem(STORAGE_KEY, theme); } catch { /* ignore */ }
  for (const b of _buttons) {
    const on = b.getAttribute('data-theme') === theme;
    if (on) b.classList.add('m-seg-on'); else b.classList.remove('m-seg-on');
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  }
}

export function initTheme(root) {
  _root = root || _root;
  applyTheme(persisted());
}

// Build the visible colour switcher — a small segmented control.
export function themeSwitcher(el) {
  _buttons = THEMES.map((t) => el('button', {
    type: 'button', class: 'm-seg-btn', 'data-theme': t.id,
    title: t.label, 'aria-label': 'Switch to ' + t.label,
    onclick: (ev) => { if (ev && ev.preventDefault) ev.preventDefault(); applyTheme(t.id); },
  }, [t.label.replace('Solarized ', 'Sol·')]));
  const wrap = el('div', { class: 'm-seg m-theme-switch', role: 'group', 'aria-label': 'Colour theme' }, _buttons);
  applyTheme(_current);
  return wrap;
}

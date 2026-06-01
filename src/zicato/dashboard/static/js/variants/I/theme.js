// variants/I/theme.js — Ledger's three-theme system + switcher.
//
// Round-3 mandatory: PORT Variant B's three-theme token SYSTEM but in the
// palettes the operator named — solarized-light (DEFAULT for the editorial
// Ledger skin), solarized-dark, monokai — with a visible switcher. Every
// mark + diagram must read with sufficient contrast in ALL THREE; the
// per-theme tokens (including the heatmap/dot ramp endpoints svg.js reads
// at draw time) live in css/variants/I/ledger.css under
// `[data-i-theme="…"]` selectors scoped to the variant root.
//
// The chosen theme is written to `data-i-theme` on the variant root AND
// persisted in localStorage so a reload sticks. Solarized-light is the
// default when nothing is stored — Ledger is light-first.

export const THEMES = [
  { id: 'solarized-light', label: 'Solarized Light' },
  { id: 'solarized-dark', label: 'Solarized Dark' },
  { id: 'monokai', label: 'Monokai' },
];
const STORAGE_KEY = 'zicato.ui.I.theme';
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
  if (_root) _root.setAttribute('data-i-theme', theme);
  try { window.localStorage && window.localStorage.setItem(STORAGE_KEY, theme); } catch { /* ignore */ }
  for (const b of _buttons) {
    const on = b.getAttribute('data-theme') === theme;
    if (on) b.classList.add('i-theme-on'); else b.classList.remove('i-theme-on');
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  }
}

// Initialise the theme on a root element (idempotent). Reads the persisted
// (or default) theme and stamps it on the root.
export function initTheme(root) {
  _root = root || _root;
  applyTheme(persisted());
}

// Build the visible switcher — a small segmented control. Pure builder
// (uses the provided `el`), wires each segment to applyTheme.
export function themeSwitcher(el) {
  _buttons = THEMES.map((t) => el('button', {
    type: 'button', class: 'i-theme-btn', 'data-theme': t.id,
    title: t.label, 'aria-label': 'Switch to ' + t.label,
    onclick: (ev) => { if (ev && ev.preventDefault) ev.preventDefault(); applyTheme(t.id); },
  }, [t.label.replace('Solarized ', 'Sol·')]));
  const wrap = el('div', { class: 'i-theme-switch', role: 'group', 'aria-label': 'Theme' }, _buttons);
  // reflect the live state onto freshly-built buttons
  applyTheme(_current);
  return wrap;
}

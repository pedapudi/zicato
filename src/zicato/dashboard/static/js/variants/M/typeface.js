// variants/M/typeface.js — Ledger II's NEW typeface theme picker.
//
// A second picker in the chrome, beside the colour switcher. Offers four
// Open-Sans-based Google-Fonts pairings (the fonts are loaded once by
// app_M.js via a fonts.googleapis.com stylesheet — the only permitted
// external dependency — with system fallbacks + font-display:swap so the
// dashboard never blocks on the network):
//
//   * Sans      — Open Sans throughout (UI + headings); tabular figures.
//   * Editorial — Open Sans body + Source Serif 4 for headings & the paper.
//                 (Ledger II's DEFAULT — light-first, publication-forward.)
//   * Technical — Open Sans body + JetBrains Mono for data / labels / code.
//   * Display   — Open Sans body + Archivo Narrow (condensed) for headings.
//
// The chosen face is written to `data-m-face` on the variant root AND
// persisted in localStorage; ledger2.css maps each face to its --m-serif /
// --m-display / --m-mono token overrides. Editorial is the default.

export const FACES = [
  { id: 'sans', label: 'Sans' },
  { id: 'editorial', label: 'Editorial' },
  { id: 'technical', label: 'Technical' },
  { id: 'display', label: 'Display' },
];
const STORAGE_KEY = 'zicato.ui.M.face';
const DEFAULT_FACE = 'editorial';

let _root = null;
let _current = DEFAULT_FACE;
let _buttons = [];

export function currentFace() { return _current; }

function persisted() {
  try {
    const v = window.localStorage && window.localStorage.getItem(STORAGE_KEY);
    if (v && FACES.some((f) => f.id === v)) return v;
  } catch { /* storage unavailable */ }
  return DEFAULT_FACE;
}

export function applyFace(id) {
  const face = FACES.some((f) => f.id === id) ? id : DEFAULT_FACE;
  _current = face;
  if (_root) _root.setAttribute('data-m-face', face);
  try { window.localStorage && window.localStorage.setItem(STORAGE_KEY, face); } catch { /* ignore */ }
  for (const b of _buttons) {
    const on = b.getAttribute('data-face') === face;
    if (on) b.classList.add('m-seg-on'); else b.classList.remove('m-seg-on');
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  }
}

export function initFace(root) {
  _root = root || _root;
  applyFace(persisted());
}

// Build the visible typeface switcher — a small segmented control.
export function faceSwitcher(el) {
  _buttons = FACES.map((f) => el('button', {
    type: 'button', class: 'm-seg-btn', 'data-face': f.id,
    title: f.label + ' typeface', 'aria-label': 'Switch to the ' + f.label + ' typeface',
    onclick: (ev) => { if (ev && ev.preventDefault) ev.preventDefault(); applyFace(f.id); },
  }, [f.label]));
  const wrap = el('div', { class: 'm-seg m-face-switch', role: 'group', 'aria-label': 'Typeface' }, _buttons);
  applyFace(_current);
  return wrap;
}

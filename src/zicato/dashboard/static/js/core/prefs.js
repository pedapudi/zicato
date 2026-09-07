// js/core/prefs.js — the console's persisted per-viewer preferences.
//
// Appearance preferences are strings in `localStorage`. This module owns the
// key namespace and guarded access; ui.js owns defaults and normalization.
// Browsers with only retired keys use defaults until a supported preference
// is stored. Unsupported typeface values also resolve to their default.
//
// Every access is wrapped: a browser in private mode throws on `localStorage`,
// and a preference is never important enough to fail a render over.

// The namespace every console preference key sits in: `zicato.console.<name>`,
// alongside the other `zicato.<area>.<name>` keys the frontend stores.
const PREFIX = 'zicato.console.';

// Return the stored value, or `absent` when this browser has no preference.
export function readPrefRaw(name, absent = null) {
  let stored = null;
  try {
    stored = window.localStorage.getItem(PREFIX + name);
  } catch (e) { /* private mode */ }
  return stored == null ? absent : stored;
}

// Store a preference's value as a string under the current key spelling.
export function writePrefRaw(name, value) {
  try { window.localStorage.setItem(PREFIX + name, String(value)); } catch (e) { /* ignore */ }
}

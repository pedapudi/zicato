// js/core/prefs.js — the console's persisted per-viewer preferences.
//
// Every preference the console remembers across page loads — the colour theme,
// the typeface, the page scale, the text size, the rail width, the builder's
// chat-pane geometry — is a single string in `localStorage` under its own key.
// This module owns the key namespace and the two guarded accessors; the modules
// that own each preference's meaning (ui.js, builder/model.js) own its default
// and its normalisation.
//
// Every access is wrapped: a browser in private mode throws on `localStorage`,
// and a preference is never important enough to fail a render over.

// The namespace every console preference key sits in: `zicato.console.<name>`,
// alongside the other `zicato.<area>.<name>` keys the frontend stores.
const PREFIX = 'zicato.console.';

// The namespace those keys used to sit in. The console was once named after its
// entry in the dashboard bake-off it won, and its preference keys carried that
// entry's letter: `zicato.T.theme`, `zicato.T.rail`, and so on. Browsers that
// were used before the rename still hold the viewer's choices under that
// spelling, so a read falls back to it when the current key is absent. Writes
// only ever go to the current key, so a preference moves across the first time
// it is set. THIS FALLBACK EXISTS SOLELY TO RETIRE THE OLD SPELLING: delete it,
// and the constant below with it, once those browsers no longer matter.
//
// It retires a key spelling and nothing else. What a stored VALUE may say is
// each preference's own question — the typeface's retired option ids resolve in
// `ui.js`'s LEGACY_TYPE_MAP, which is unrelated to this fallback and outlives
// it.
const RETIRED_PREFIX = 'zicato.T.';

// The stored string for a preference name, or `absent` when nothing is stored
// under either spelling.
export function readPrefRaw(name, absent = null) {
  let stored = null;
  try {
    stored = window.localStorage.getItem(PREFIX + name);
    if (stored == null) stored = window.localStorage.getItem(RETIRED_PREFIX + name);
  } catch (e) { /* private mode */ }
  return stored == null ? absent : stored;
}

// Store a preference's value as a string under the current key spelling.
export function writePrefRaw(name, value) {
  try { window.localStorage.setItem(PREFIX + name, String(value)); } catch (e) { /* ignore */ }
}

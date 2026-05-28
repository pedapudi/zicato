// js/core/shell.js — persisted shell-pick (phase0 vs legacy).
//
// The dashboard ships two shells; the user can toggle between them
// from a sidebar link. Phase 1 replaced the original ``?legacy=1``
// query-param activation with a localStorage-backed preference so the
// choice survives reloads.
//
// Precedence (resolved on every page load):
//   1. ``?legacy=1`` / ``?phase0=1`` query param — a transient override
//      (NOT persisted). Lets deep links from older docs and bookmarks
//      keep working without flipping the user's persisted choice.
//   2. localStorage ``zicato.dashboard.shell`` — the persisted value
//      written by ``setShellPreference``.
//   3. Default: ``phase0``.
//
// The two functions exported here are intentionally side-effect-free
// at import time — app.js imports them and wires them into init().
// Keeping them out of app.js means tests can import them without
// triggering app.js's init() (which kicks off SSE + a setInterval and
// would otherwise pin the node test process open).

export const SHELL_STORAGE_KEY = 'zicato.dashboard.shell';

function _readStoredShell() {
  try {
    const raw = window.localStorage && window.localStorage.getItem(SHELL_STORAGE_KEY);
    if (raw === 'phase0' || raw === 'legacy') return raw;
  } catch {
    // localStorage may be unavailable (file://, embedded contexts).
  }
  return null;
}

function _writeStoredShell(shell) {
  try {
    if (window.localStorage) window.localStorage.setItem(SHELL_STORAGE_KEY, shell);
  } catch {
    // best-effort persistence
  }
}

// Resolve which shell to mount before anything else runs.
//
// See the file header for the precedence rules.
export function resolveShell() {
  const params = new URLSearchParams(window.location.search);
  if (params.get('legacy') === '1') return 'legacy';
  if (params.get('phase0') === '1') return 'phase0';
  const stored = _readStoredShell();
  if (stored) return stored;
  return 'phase0';
}

// Public toggle helper — wired to the sidebar/footer "Use legacy/new
// UI →" link. Persists the choice and reloads so the chosen shell's
// container is the only one visible. Strips transient ``?legacy=1`` /
// ``?phase0=1`` from the URL so a stale override does not contradict
// the persisted choice on the next paint.
export function setShellPreference(shell) {
  if (shell !== 'phase0' && shell !== 'legacy') return;
  _writeStoredShell(shell);
  try {
    const url = new URL(window.location.href);
    url.searchParams.delete('legacy');
    url.searchParams.delete('phase0');
    window.location.replace(url.toString());
  } catch {
    if (typeof window.location.reload === 'function') {
      window.location.reload();
    }
  }
}

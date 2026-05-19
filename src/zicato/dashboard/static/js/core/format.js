// core/format.js — pure formatting helpers.
//
// No DOM, no state. Every function here is a total function: a bad
// input yields a sentinel ('—' / NaN / '') rather than throwing.

export const SVG_NS = 'http://www.w3.org/2000/svg';

// The canonical palette — mirrors zicato/epoch/html_report.py so the
// live dashboard and analysis.html read as siblings.
export const COLORS = {
  promoted: '#2ea043',
  rejected: '#d73a49',
  baseline: '#6e7681',
  deferred: '#bf8700',
  running: '#1f6feb',
  grid: '#d0d7de',
};

// Tournament promotion margin default (overridden by scoring.json).
export const DEFAULT_MARGIN = 0.05;

export function fmtDelta(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  const s = v >= 0 ? '+' : '';
  return s + v.toFixed(3);
}

export function fmtRate(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  return v.toFixed(2);
}

export function fmtScalar(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  return v.toFixed(3);
}

export function fmtDuration(seconds) {
  if (!isFinite(seconds) || seconds < 0) return '—';
  const s = Math.floor(seconds % 60);
  const m = Math.floor((seconds / 60) % 60);
  const h = Math.floor(seconds / 3600);
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

export function truncate(s, n) {
  if (s == null) return '';
  if (s.length <= n) return s;
  return s.slice(0, n - 1).trimEnd() + '…';
}

// Robust ISO-8601 parser. heartbeat.json mixes zone forms: a trailing
// `Z` on some fields, an explicit `+00:00` offset on others. A bare
// `new Date` parses a zone-less value as *local* time, skewing every
// elapsed clock. parseIso normalises both, pins zone-less values to
// UTC, and returns epoch ms (or NaN when unparseable).
export function parseIso(value) {
  if (value == null) return NaN;
  if (typeof value === 'number') return isFinite(value) ? value : NaN;
  if (value instanceof Date) return value.getTime();
  if (typeof value !== 'string') return NaN;
  let s = value.trim();
  if (s.length === 0) return NaN;
  const hasZone = /([zZ]|[+-]\d{2}:?\d{2})$/.test(s);
  if (!hasZone && (s.indexOf('T') !== -1 || s.indexOf(' ') !== -1)) {
    s = s.replace(' ', 'T') + 'Z';
  }
  const ms = Date.parse(s);
  return isFinite(ms) ? ms : NaN;
}

// `Date.now()` behind one helper so elapsed / stale math stays uniform
// and testable.
export function nowMs() { return Date.now(); }

// A short clock label (HH:MM:SS) from an ISO timestamp, for log rows.
export function fmtClock(ts) {
  const ms = parseIso(ts);
  if (!isFinite(ms)) return typeof ts === 'string' ? ts : '—';
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

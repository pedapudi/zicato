// js/transcript_stream.js — the live conversation pane's read layer.
//
// A SEPARATE module from js/data.js on purpose. data.js is a URL-keyed cache
// for payloads that are immutable once a generation closes; a growing
// transcript is the opposite of that, and bolting it on would mean busting
// cache keys on every beat (which is the flashing bug the digest discipline
// exists to prevent). This module holds no cache at all: it holds a CURSOR,
// and each pull asks the server only for what landed after it.
//
// It is DOM-free by design and takes its fetcher by injection, so the whole
// cursor protocol — including the gap heal — is testable without a server.
//
// The wire contract is js/CONTRACTS.md § `/transcript/delta`. The two rules
// that matter here:
//
//   * the cursor is opaque. It counts parsed events, but the client's only
//     job is to hand back whatever the server last returned;
//   * `truncated` means the delta was longer than the server would send, so
//     the client's rendered prefix has a HOLE in it. The honest repair is a
//     re-read from the top, which is what pull() does — reporting `reset` so
//     the caller replaces its turn list rather than splicing into it.

import { fetchJson } from './core/api.js';

// The stream starts with no cursor at all, which the server reads as "from the
// top". Distinct from a cursor of 0 only in intent; both yield everything.
const FROM_THE_TOP = null;

export function createTranscriptStream(coords, opts) {
  const o = opts || {};
  const get = o.fetchJson || fetchJson;
  const c = coords || {};

  const stream = {
    // Wire state, refreshed by every pull.
    cursor: FROM_THE_TOP,
    eventsPath: null,      // the SSE run_log filter key
    found: false,
    complete: false,
    turnTotal: 0,
    fidelity: null,
    verbatimAvailable: false,
    error: null,

    url(after) {
      let u = '/api/run/' + enc(c.epochId) + '/' + enc(c.gen) + '/' + enc(c.entry) + '/transcript/delta';
      const q = [];
      if (after != null) q.push('after=' + enc(after));
      if (c.runId) q.push('run=' + enc(c.runId));
      if (q.length) u += '?' + q.join('&');
      return u;
    },

    // Ask for everything past the cursor. Resolves to
    // { turns, annotations, reset } — `reset` true means the caller must
    // REPLACE its turn list rather than splice into it. Resolves to null on a
    // transient failure (the next run_log frame retries); never throws.
    async pull() {
      let body = await this._get(this.cursor);
      if (body == null) return null;
      let reset = this.cursor == null;
      if (body.truncated) {
        // We fell too far behind to splice. Re-read from the top; if even
        // THAT comes back truncated the transcript is longer than one
        // response, and the tail we have is the honest best answer.
        const whole = await this._get(FROM_THE_TOP);
        if (whole != null) { body = whole; reset = true; }
      }
      this._absorb(body);
      return {
        turns: Array.isArray(body.turns) ? body.turns : [],
        annotations: Array.isArray(body.annotations) ? body.annotations : [],
        execution: body.execution || null,
        reset,
      };
    },

    async _get(after) {
      try {
        const body = await get(this.url(after));
        return (body && typeof body === 'object') ? body : null;
      } catch (err) {
        return null;
      }
    },

    _absorb(body) {
      // Only ADVANCE the cursor. A server that answered an older read out of
      // order must never rewind us into re-delivering turns we already hold.
      if (typeof body.cursor === 'number' && (this.cursor == null || body.cursor > this.cursor)) {
        this.cursor = body.cursor;
      }
      this.eventsPath = body.events_path != null ? body.events_path : this.eventsPath;
      this.found = !!body.found;
      this.complete = !!body.complete;
      this.turnTotal = typeof body.turn_total === 'number' ? body.turn_total : this.turnTotal;
      this.fidelity = body.fidelity != null ? body.fidelity : this.fidelity;
      this.verbatimAvailable = !!body.verbatim_available;
      this.error = body.error != null ? body.error : null;
    },
  };
  return stream;
}

// Splice a delta's turns into a client-side turn list held at the SERVER's
// indices, so a turn that grew lands back on itself instead of duplicating.
// Returns { turns, gap } — `gap` true when the delta starts past the end of
// what we hold, which means turns went missing and only a re-read can heal it.
export function spliceTurns(existing, deltaTurns) {
  const turns = Array.isArray(existing) ? existing.slice() : [];
  let gap = false;
  for (const t of (Array.isArray(deltaTurns) ? deltaTurns : [])) {
    const i = typeof t.turn_index === 'number' ? t.turn_index : turns.length;
    if (i > turns.length) gap = true;
    turns[i] = t;
  }
  // A gap leaves holes in the array; report it rather than render `undefined`.
  return { turns, gap };
}

// Merge delta annotations into the held list, keyed on the source index that
// minted them so a replayed annotation never doubles.
export function mergeAnnotations(existing, deltaAnns) {
  const out = Array.isArray(existing) ? existing.slice() : [];
  const seen = new Set(out.map(keyOf));
  for (const a of (Array.isArray(deltaAnns) ? deltaAnns : [])) {
    const k = keyOf(a);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(a);
  }
  return out;
}

function keyOf(a) {
  return (a && a.source_index != null) ? 'i' + a.source_index : 'k' + (a && a.kind) + ':' + (a && a.summary);
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }

// js/convo.js — the live conversation pane (issue #194 §2).
//
// ONE component that follows a running board unit's conversation and then
// BECOMES that unit's permanent transcript. Four traps, and how each is
// answered here:
//
//  1. APPEND vs REBUILD. The pane never re-renders per beat. The SSE
//     `run_log:grew` frame (which names the events.jsonl that grew) wakes a
//     cursor pull; the delta carries only new turns; reconcileTurns() appends
//     the tail nodes and leaves every existing turn node untouched by
//     identity. A beat that brings nothing writes ZERO DOM, so scroll — the
//     thing an operator loses first and notices most — simply survives.
//
//  2. LIVE → SETTLED CONTINUITY. There is no second component for a finished
//     run. When the transcript reports `complete`, the SAME pane stops
//     following and repaints only its caption and header pill. The scroller,
//     its turn nodes, and the reader's scroll position are not touched, so a
//     run finishing under the operator's eyes does not yank the page.
//
//  3. FIDELITY HONESTY. The pane renders the events.jsonl reconstruction and
//     says so, in the shared vocabulary (ui.js fidelityLabel) the Instrument
//     x-ray uses. When a verbatim result.json capture exists beside the run it
//     says that too — what the operator is reading is a reconstruction, and a
//     higher-fidelity record existing elsewhere is a fact they should have.
//
//  4. AUTOSCROLL WITH PIN. While the reader is at the tail, new turns scroll
//     into view. The moment they scroll UP they own the viewport: new turns
//     still append, but the pane counts them into a "N new turns ↓" affordance
//     instead of dragging the view. Clicking it (or scrolling back down)
//     returns to tailing.
//
// The pane owns no data cache — js/transcript_stream.js holds the cursor.

import { el, clearChildren } from './core/dom.js';
import { bus } from './core/bus.js';
import { fidelityLabel, pill } from './ui.js';
import { reconcileTurns, nearBottom } from './turns.js';
import { createTranscriptStream, spliceTurns, mergeAnnotations } from './transcript_stream.js';
import { LIVENESS } from './unit_liveness.js';

// mountConversationPane(host, spec) → a handle.
//
// `spec` is { epochId, gen, entry, runId, tri } where `tri` is issue #194
// §1's per-run tri-state ('live' | 'settled' | 'interrupted'). FOLLOW is
// offered only for 'live'; the other two open the same component already
// settled. `opts.fetchJson` / `opts.subscribe` exist for tests.
//
// The handle exposes { node, refresh(), setTriState(), destroy() } plus the
// read-only fields the tests assert on.
export function mountConversationPane(host, spec, opts) {
  const s = spec || {};
  const o = opts || {};
  const stream = createTranscriptStream(s, { fetchJson: o.fetchJson });

  const pane = {
    tri: s.tri || LIVENESS.SETTLED,
    // When the loop stopped, for the interrupted caption's past tense. Null
    // while live; supplied by the caller off §1's liveness payload.
    endedAt: s.endedAt || null,
    turns: [],
    annotations: [],
    unseen: 0,
    following: false,
    stream,
  };

  // ---- frame ----------------------------------------------------------
  // Built ONCE. Everything that changes as the run streams is a text update
  // inside these nodes — never a rebuild of the frame, because rebuilding it
  // would take the scroller (and the reader's position) with it.
  const statusPill = pill(triClass(pane.tri), triWord(pane.tri));
  const caption = el('p', { class: 'dn-faint dn-convo-cap', 'data-convo-caption': '' });
  const scroller = el('div', { class: 'dn-transcript dn-convo-scroll', 'data-convo-scroll': '' });
  const pinBtn = el('button', {
    class: 'dn-linkbtn dn-convo-pin',
    'data-convo-pin': '',
    hidden: 'hidden',
    onclick: () => tailNow(),
  });
  const node = el('div', { class: 'dn-panel dn-convo', 'data-convo-pane': '' }, [
    el('div', { class: 'dn-convo-head' }, [
      el('span', { class: 'dn-mono', text: (s.gen || '?') + ' · ' + (s.entry || '?') }),
      statusPill,
    ]),
    caption,
    scroller,
    pinBtn,
  ]);

  // Scrolling back to the tail re-arms autoscroll and clears the backlog —
  // the affordance must never outlive the condition it describes.
  if (typeof scroller.addEventListener === 'function') {
    scroller.addEventListener('scroll', () => {
      if (nearBottom(scroller)) { pane.unseen = 0; paintPin(); }
    });
  }

  if (host) {
    // The pane replaces whatever placeholder its host held, once.
    clearChildren(host);
    host.appendChild(node);
  }

  // ---- the follow loop ------------------------------------------------
  const subscribe = o.subscribe || ((topic, fn) => bus.on(topic, fn));
  let unsubscribe = null;

  function startFollowing() {
    if (pane.following || pane.tri !== LIVENESS.LIVE) return;
    pane.following = true;
    unsubscribe = subscribe('run_log:grew', (frame) => {
      // FILTER to this run's file. A sibling unit's growth must not cost us a
      // fetch — with many units in flight that is the difference between one
      // pull per beat and one per unit per beat. Before the first pull we do
      // not know our events path yet, so an unmatched frame still wakes us
      // once; after that the filter is exact.
      const grew = frame && frame.events_path;
      if (stream.eventsPath && grew && grew !== stream.eventsPath) return;
      refresh();
    });
  }

  function stopFollowing() {
    pane.following = false;
    if (unsubscribe) { unsubscribe(); unsubscribe = null; }
  }

  // One cursor pull, folded into the rendered thread. Safe to call at any
  // time; concurrent calls are collapsed so a burst of growth frames cannot
  // interleave two deltas into the same turn list.
  let inFlight = null;
  function refresh() {
    if (inFlight) return inFlight;
    inFlight = pullOnce().finally(() => { inFlight = null; });
    return inFlight;
  }

  async function pullOnce() {
    const delta = await stream.pull();
    if (delta == null) return;   // transient; the next frame retries

    if (delta.reset) {
      pane.turns = [];
      pane.annotations = [];
    }
    const spliced = spliceTurns(pane.turns, delta.turns);
    if (spliced.gap) {
      // Turns went missing between our cursor and this delta — splicing would
      // render holes. Drop the cursor and re-read the whole thread.
      stream.cursor = null;
      pane.turns = [];
      pane.annotations = [];
      const whole = await stream.pull();
      if (whole == null) return;
      pane.turns = spliceTurns([], whole.turns).turns;
      pane.annotations = mergeAnnotations([], whole.annotations);
    } else {
      pane.turns = spliced.turns;
      pane.annotations = mergeAnnotations(pane.annotations, delta.annotations);
    }

    render();

    // The run just ended: become the permanent transcript, in place.
    if (stream.complete && pane.tri === LIVENESS.LIVE) setTriState(LIVENESS.SETTLED);
  }

  // ---- rendering ------------------------------------------------------
  function render() {
    const out = reconcileTurns(scroller, pane.turns, pane.annotations);
    // Turns that landed while the reader was scrolled up are a BACKLOG, not a
    // reason to move them. Only a tailing reader has their view advanced (which
    // reconcileTurns already did).
    if (out.appended > 0 && !out.pinned) pane.unseen += out.appended;
    if (out.pinned) pane.unseen = 0;
    paintPin();
    paintCaption(out.rendered);
  }

  function paintCaption(rendered) {
    caption.textContent = captionText(pane.tri, rendered, stream, pane.endedAt);
  }

  function paintPin() {
    const show = pane.unseen > 0;
    pinBtn.textContent = show
      ? pane.unseen + ' new turn' + (pane.unseen === 1 ? '' : 's') + ' ↓'
      : '';
    if (show) pinBtn.removeAttribute('hidden');
    else pinBtn.setAttribute('hidden', 'hidden');
  }

  function tailNow() {
    if (typeof scroller.scrollHeight === 'number') scroller.scrollTop = scroller.scrollHeight;
    pane.unseen = 0;
    paintPin();
  }

  // THE LIVE → SETTLED TRANSITION. Caption and pill only: the scroller and
  // every turn node inside it are left exactly as they are, which is what
  // makes this a state change rather than a remount.
  function setTriState(tri, endedAt) {
    pane.tri = tri;
    if (endedAt !== undefined) pane.endedAt = endedAt;
    if (tri !== LIVENESS.LIVE) stopFollowing();
    statusPill.textContent = triWord(tri);
    statusPill.className = 'dn-pill dn-' + triClass(tri);
    paintCaption(scroller.childNodes.length);
    if (tri === LIVENESS.LIVE) startFollowing();
  }

  // Open in the mode the caller asked for, then take the first read.
  setTriState(pane.tri);
  const ready = refresh();

  return {
    node,
    pane,
    ready,
    refresh,
    setTriState,
    destroy() { stopFollowing(); },
  };
}

// The caption — the fidelity sentence, in the shared vocabulary. It says three
// things and no more: what these bytes are, whether the run is still producing
// them, and whether a higher-fidelity record of the same run exists on disk.
export function captionText(tri, rendered, stream, endedAt) {
  const parts = [fidelityLabel((stream && stream.fidelity) || 'events')];
  if (tri === LIVENESS.LIVE) {
    parts.push('following · ' + rendered + ' turns so far');
  } else if (tri === LIVENESS.INTERRUPTED) {
    // §1's vocabulary for this state, and the part that actually matters to an
    // operator reading it: the unit was mid-run when the loop stopped, so this
    // transcript is a fragment AND its score was never committed. Past tense —
    // nothing here is still happening.
    parts.push('this run was still going when the loop was interrupted'
      + (endedAt ? ' on ' + shortDate(endedAt) : '')
      + ' · ' + rendered + ' turns before it stopped · its score was never committed');
  } else {
    parts.push('settled · ' + rendered + ' turns');
  }
  if (stream && stream.verbatimAvailable) parts.push('a verbatim result.json capture was retained beside this run');
  if (stream && stream.error) parts.push(stream.error);
  return parts.join(' · ');
}

// A short past-tense date for the interrupted caption ("Jun 8"). §1 exports a
// `shortDate(iso)` for exactly this; swap to it when that lands so every
// past-tense surface spells a date the same way.
//
// Rendered in UTC, deliberately. The server stamps these in UTC, and a run
// that stopped at 03:58Z reads as the PREVIOUS day under a western local
// timezone — so a local-time render would have this pane and the candidate
// dossier naming different days for the same interruption, which is worse
// than either choice on its own.
function shortDate(iso) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return String(iso);
  return new Date(t).toLocaleDateString(undefined, {
    month: 'short', day: 'numeric', timeZone: 'UTC',
  });
}

function triWord(tri) {
  if (tri === LIVENESS.LIVE) return 'live';
  if (tri === LIVENESS.INTERRUPTED) return 'interrupted';
  return 'settled';
}

function triClass(tri) {
  if (tri === LIVENESS.LIVE) return 'live';
  if (tri === LIVENESS.INTERRUPTED) return 'warn';
  return 'faint';
}

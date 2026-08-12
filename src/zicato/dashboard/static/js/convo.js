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
import { reconcileTurns } from './turns.js';
import { createTranscriptStream, spliceTurns, mergeAnnotations } from './transcript_stream.js';
import { RUN_TRI } from './livestatus_tristate_stub.js';

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
    tri: s.tri || RUN_TRI.SETTLED,
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
      if (atTail()) { pane.unseen = 0; paintPin(); }
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
    if (pane.following || pane.tri !== RUN_TRI.LIVE) return;
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
    if (stream.complete && pane.tri === RUN_TRI.LIVE) setTriState(RUN_TRI.SETTLED);
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
    caption.textContent = captionText(pane.tri, rendered, stream);
  }

  function paintPin() {
    const show = pane.unseen > 0;
    pinBtn.textContent = show
      ? pane.unseen + ' new turn' + (pane.unseen === 1 ? '' : 's') + ' ↓'
      : '';
    if (show) pinBtn.removeAttribute('hidden');
    else pinBtn.setAttribute('hidden', 'hidden');
  }

  function atTail() {
    const sh = scroller.scrollHeight, st = scroller.scrollTop, ch = scroller.clientHeight;
    if (typeof sh !== 'number' || typeof ch !== 'number' || typeof st !== 'number') return true;
    return (sh - st - ch) <= 8;
  }

  function tailNow() {
    if (typeof scroller.scrollHeight === 'number') scroller.scrollTop = scroller.scrollHeight;
    pane.unseen = 0;
    paintPin();
  }

  // THE LIVE → SETTLED TRANSITION. Caption and pill only: the scroller and
  // every turn node inside it are left exactly as they are, which is what
  // makes this a state change rather than a remount.
  function setTriState(tri) {
    pane.tri = tri;
    if (tri !== RUN_TRI.LIVE) stopFollowing();
    statusPill.textContent = triWord(tri);
    statusPill.className = 'dn-pill dn-' + triClass(tri);
    paintCaption(scroller.childNodes.length);
    if (tri === RUN_TRI.LIVE) startFollowing();
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
export function captionText(tri, rendered, stream) {
  const parts = [fidelityLabel((stream && stream.fidelity) || 'events')];
  if (tri === RUN_TRI.LIVE) parts.push('following · ' + rendered + ' turns so far');
  else if (tri === RUN_TRI.INTERRUPTED) parts.push('interrupted — the run stopped without a terminal event; this is as far as it got');
  else parts.push('settled · ' + rendered + ' turns');
  if (stream && stream.verbatimAvailable) parts.push('a verbatim result.json capture was retained beside this run');
  if (stream && stream.error) parts.push(stream.error);
  return parts.join(' · ');
}

function triWord(tri) {
  if (tri === RUN_TRI.LIVE) return 'live';
  if (tri === RUN_TRI.INTERRUPTED) return 'interrupted';
  return 'settled';
}

function triClass(tri) {
  if (tri === RUN_TRI.LIVE) return 'live';
  if (tri === RUN_TRI.INTERRUPTED) return 'warn';
  return 'faint';
}

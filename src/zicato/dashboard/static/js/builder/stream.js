// js/builder/stream.js — read the copilot SSE stream.
//
// POSTs to the chat endpoint and parses the `text/event-stream` reply frame by
// frame, dispatching each `{type}` to the matching callback as it lands (token
// deltas stream in; they are not buffered). The transport is split out from the
// chat UI so a test can drive the frame dispatch without a real network stream:
// pass `opts.open` to supply a reader (an async iterator / a {read()} reader /
// an array of frame objects), and the parser is bypassed.
//
// Frame schema (one JSON object per SSE `data:` line):
//   {type:"token", text}        → onToken(text)
//   {type:"tool",  name, args}  → onTool(name, args)
//   {type:"patch", patch, cost, warnings, diff}  → onPatch(frame)
//   {type:"done"}               → end
//   {type:"error", message}     → onError(message)

const NOOP = () => {};

export async function streamChat(path, body, opts) {
  const o = opts || {};
  const onToken = o.onToken || NOOP;
  const onTool = o.onTool || NOOP;
  const onPatch = o.onPatch || NOOP;
  const onError = o.onError || NOOP;

  const dispatch = (frame) => {
    if (!frame || typeof frame !== 'object') return false;
    switch (frame.type) {
      case 'token': onToken(frame.text || ''); return false;
      case 'tool': onTool(frame.name || '', frame.args || {}); return false;
      case 'patch': onPatch(frame); return false;
      case 'error': onError(frame.message || 'error'); return true;
      case 'done': return true;
      default: return false;
    }
  };

  // TEST / injection path: a pre-supplied source of frames (array or iterator).
  if (o.frames || o.open) {
    const src = o.frames || (typeof o.open === 'function' ? await o.open() : o.open);
    if (Array.isArray(src)) { for (const f of src) { if (dispatch(f)) break; } return; }
    if (src && typeof src[Symbol.asyncIterator] === 'function') {
      for await (const f of src) { if (dispatch(f)) break; }
      return;
    }
    if (src && typeof src.read === 'function') { await pumpReader(src, dispatch); return; }
    return;
  }

  // LIVE path: fetch + read the response body as an SSE text stream.
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) {
    let msg = 'HTTP ' + res.status;
    try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (e) { /* ignore */ }
    onError(msg);
    return;
  }
  // A non-streaming JSON error body (the graceful-degrade 4xx path returns
  // JSON rather than an event-stream) — surface its message cleanly.
  const ctype = (res.headers && typeof res.headers.get === 'function') ? (res.headers.get('content-type') || '') : '';
  if (ctype.includes('application/json')) {
    try { const j = await res.json(); if (j && j.error) { onError(j.error); return; } } catch (e) { /* fall through */ }
  }
  if (!res.body || typeof res.body.getReader !== 'function') {
    // No streamable body — read the whole text and parse what frames we can.
    let text = '';
    try { text = await res.text(); } catch (e) { text = ''; }
    for (const frame of parseSse(text)) { if (dispatch(frame)) break; }
    return;
  }
  const reader = res.body.getReader();
  const decoder = (typeof TextDecoder !== 'undefined') ? new TextDecoder() : null;
  let buf = '';
  let done = false;
  while (!done) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buf += decoder ? decoder.decode(chunk.value, { stream: true }) : String(chunk.value);
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const frame = frameOf(raw);
      if (frame && dispatch(frame)) { done = true; break; }
    }
  }
}

// Pump a {read()} reader that yields either decoded text or frame objects.
async function pumpReader(reader, dispatch) {
  let buf = '';
  for (;;) {
    const r = await reader.read();
    if (r && r.done) break;
    const v = r ? r.value : undefined;
    if (v == null) continue;
    if (typeof v === 'object' && v.type) { if (dispatch(v)) break; continue; }
    buf += String(v);
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const frame = frameOf(raw);
      if (frame && dispatch(frame)) return;
    }
  }
}

function* parseSse(text) {
  for (const block of String(text || '').split('\n\n')) {
    const frame = frameOf(block);
    if (frame) yield frame;
  }
}

function frameOf(block) {
  const line = String(block || '').split('\n').find((l) => l.startsWith('data:'));
  if (!line) return null;
  const json = line.slice('data:'.length).trim();
  if (!json) return null;
  try { return JSON.parse(json); } catch (e) { return null; }
}

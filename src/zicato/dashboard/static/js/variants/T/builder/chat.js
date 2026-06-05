// variants/T/builder/chat.js — the copilot chat pane (B1b surface).
//
// A collapsible AND drag-resizable side pane. It posts to /builder/chat and
// renders the SSE stream: `token` deltas append into the assistant bubble,
// each `tool` shows as a subtle step, each `patch` is handed to onPatch (which
// applies it to the SHARED draft so the FORM updates live) and tags the bubble
// with an `edited: …` chip. A typing indicator shows while streaming; `error`
// frames (incl. the graceful-degrade "configure builder.json" message) render
// cleanly. When chat_enabled is false the input is disabled with a hint.
//
// RESIZE: a drag handle on the pane's LEFT edge sets the width live
// (pointer-captured, no per-frame persist), clamped to [min,max], and persists
// on release via onWidthChange. COLLAPSE: a header toggle shrinks the pane to
// a thin strip with an expand affordance. Both persist; the host reflows the
// form + preview to the remaining width with no overlap at any width.

import { el, clearChildren, patchText } from '../../../core/dom.js';
import { CHAT_PATH, chatBody } from './api.js';
import { streamChat } from './stream.js';

export class BuilderChat {
  constructor(opts) {
    const o = opts || {};
    this._config = o.config || {};
    this._onPatch = o.onPatch || (() => {});
    this._onWidthChange = o.onWidthChange || (() => {});
    this._onCollapse = o.onCollapse || (() => {});
    this._min = o.min || 240;
    this._max = o.max || 560;
    this._width = clamp(o.initialWidth || 340, this._min, this._max);
    this._collapsed = !!o.collapsed;
    this._lastBubble = null;     // the in-flight assistant bubble being streamed
    this._streaming = false;
    this.node = this._build();
    this._applyCollapsed();
  }

  width() { return this._width; }
  collapsed() { return this._collapsed; }

  _build() {
    const enabled = !!this._config.chat_enabled;
    const model = (this._config.agent && this._config.agent.model) || '';

    // the LEFT-edge drag handle (resizes the pane; the work column reflows).
    this._handle = el('div', {
      class: 'dn-bld-chat-handle', role: 'separator', tabindex: '0',
      'aria-orientation': 'vertical', 'aria-label': 'Resize the copilot pane',
      'aria-valuemin': String(this._min), 'aria-valuemax': String(this._max), 'aria-valuenow': String(this._width),
      title: 'Drag to resize the copilot pane',
    });
    this._wireHandle();

    this._collapseBtn = el('button', {
      class: 'dn-bld-chat-collapse', type: 'button',
      'aria-label': this._collapsed ? 'Expand copilot' : 'Collapse copilot',
      title: this._collapsed ? 'Expand copilot' : 'Collapse copilot',
      text: this._collapsed ? '‹' : '›',
    });
    this._collapseBtn.addEventListener('click', () => this._toggleCollapse());

    const header = el('div', { class: 'dn-bld-chat-head' }, [
      this._collapseBtn,
      el('span', { class: 'dn-bld-chat-title', text: 'copilot' }),
      el('span', { class: 'dn-bld-chat-model dn-mono', title: model || 'no model configured', text: model || 'no model' }),
    ]);

    this._log = el('div', { class: 'dn-bld-chat-log', role: 'log', 'aria-live': 'polite', 'aria-label': 'Copilot conversation' });
    if (!enabled) {
      this._log.appendChild(el('div', { class: 'dn-bld-chat-degrade' }, [
        el('p', { text: 'The copilot is unavailable — no model is configured.' }),
        el('p', { class: 'dn-faint', text: 'Set agent.model in builder.json to enable chat. Form editing works without it.' }),
      ]));
    } else {
      this._log.appendChild(el('p', { class: 'dn-faint dn-bld-chat-hint', text: 'Ask the copilot to shape the contract — it edits the same draft the form does.' }));
    }

    this._input = el('textarea', {
      class: 'dn-bld-chat-input', rows: '2', 'aria-label': 'Message the copilot',
      placeholder: enabled ? 'Message the copilot…' : 'Chat disabled — configure builder.json',
    });
    if (!enabled) this._input.setAttribute('disabled', 'disabled');
    this._send = el('button', { class: 'dn-bld-chat-send', type: 'button', text: 'Send' });
    if (!enabled) this._send.setAttribute('disabled', 'disabled');
    this._send.addEventListener('click', () => this._submit());
    this._input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault && ev.preventDefault(); this._submit(); }
    });

    const composer = el('div', { class: 'dn-bld-chat-composer' }, [this._input, this._send]);

    this._body = el('div', { class: 'dn-bld-chat-body' }, [header, this._log, composer]);
    this._strip = el('button', {
      class: 'dn-bld-chat-strip', type: 'button', 'aria-label': 'Expand copilot', title: 'Expand copilot',
    }, [el('span', { class: 'dn-bld-chat-strip-lab', text: 'copilot' })]);
    this._strip.addEventListener('click', () => this._toggleCollapse());

    return el('aside', { class: 'dn-bld-chat', 'aria-label': 'Copilot' }, [this._handle, this._body, this._strip]);
  }

  _applyCollapsed() {
    if (this._collapsed) this.node.classList.add('dn-bld-chat-collapsed');
    else this.node.classList.remove('dn-bld-chat-collapsed');
    if (this._collapseBtn) {
      patchText(this._collapseBtn, this._collapsed ? '‹' : '›');
      this._collapseBtn.setAttribute('aria-label', this._collapsed ? 'Expand copilot' : 'Collapse copilot');
    }
  }

  _toggleCollapse() {
    this._collapsed = !this._collapsed;
    this._applyCollapsed();
    this._onCollapse(this._collapsed);
  }

  // ── resize ───────────────────────────────────────────────────────
  _wireHandle() {
    const handle = this._handle;
    let startX = 0;
    let startW = this._width;
    let dragging = false;
    const STEP = 24;
    // dragging the LEFT edge: moving left (negative Δx) WIDENS the pane.
    const onMove = (ev) => {
      if (!dragging) return;
      const cx = ev && typeof ev.clientX === 'number' ? ev.clientX : null;
      if (cx == null) return;
      if (ev.preventDefault) ev.preventDefault();
      this._setWidth(startW - (cx - startX), false);
    };
    const onUp = (ev) => {
      if (!dragging) return;
      dragging = false;
      handle.classList && handle.classList.remove('dn-bld-chat-handle-drag');
      if (ev && typeof ev.clientX === 'number') this._setWidth(startW - (ev.clientX - startX), true);
      else this._setWidth(this._width, true);
      if (typeof window !== 'undefined' && window.removeEventListener) {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
      }
    };
    const onDown = (ev) => {
      dragging = true;
      startX = ev && typeof ev.clientX === 'number' ? ev.clientX : 0;
      startW = this._width;
      handle.classList && handle.classList.add('dn-bld-chat-handle-drag');
      if (ev && ev.preventDefault) ev.preventDefault();
      const pid = ev && ev.pointerId != null ? ev.pointerId : 0;
      if (typeof handle.setPointerCapture === 'function') { try { handle.setPointerCapture(pid); } catch (e) { /* ignore */ } }
      if (typeof window !== 'undefined' && window.addEventListener) {
        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', onUp);
        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', onUp);
      }
    };
    handle.addEventListener('pointerdown', onDown);
    handle.addEventListener('mousedown', onDown);
    handle.addEventListener('pointermove', onMove);
    handle.addEventListener('pointerup', onUp);
    handle.addEventListener('keydown', (ev) => {
      const k = ev.key;
      if (k === 'ArrowLeft') { ev.preventDefault && ev.preventDefault(); this._setWidth(this._width + STEP, true); }
      else if (k === 'ArrowRight') { ev.preventDefault && ev.preventDefault(); this._setWidth(this._width - STEP, true); }
      else if (k === 'Home') { ev.preventDefault && ev.preventDefault(); this._setWidth(this._max, true); }
      else if (k === 'End') { ev.preventDefault && ev.preventDefault(); this._setWidth(this._min, true); }
    });
  }

  _setWidth(px, persist) {
    this._width = clamp(px, this._min, this._max);
    if (this._handle) this._handle.setAttribute('aria-valuenow', String(this._width));
    this._onWidthChange(this._width);   // the host reflows; persist on release
  }

  // ── send + stream ────────────────────────────────────────────────
  async _submit() {
    if (!this._config.chat_enabled || this._streaming) return;
    const text = String(this._input.value || this._input.getAttribute('value') || '').trim();
    if (!text) return;
    this._input.value = '';
    this._appendUser(text);
    const bubble = this._appendAssistant();
    this._lastBubble = bubble;
    this._setStreaming(true);
    try {
      await streamChat(CHAT_PATH, chatBody(text), {
        onToken: (t) => this._appendToken(bubble, t),
        onTool: (name) => this._appendTool(bubble, name),
        onPatch: (frame) => { this._onPatch(frame); },
        onError: (msg) => this._appendErr(bubble, msg),
      });
    } catch (err) {
      this._appendErr(bubble, (err && err.message) || String(err));
    } finally {
      this._setStreaming(false);
    }
  }

  _setStreaming(on) {
    this._streaming = !!on;
    if (on) {
      this._typing = el('div', { class: 'dn-bld-chat-typing', 'aria-label': 'copilot is typing' }, [
        el('span', { class: 'dn-bld-chat-dot' }), el('span', { class: 'dn-bld-chat-dot' }), el('span', { class: 'dn-bld-chat-dot' }),
      ]);
      this._log.appendChild(this._typing);
    } else if (this._typing && this._typing.parentNode) {
      this._typing.parentNode.removeChild(this._typing);
      this._typing = null;
    }
    if (this._send) {
      if (on) this._send.setAttribute('disabled', 'disabled');
      else this._send.removeAttribute('disabled');
    }
  }

  _appendUser(text) {
    this._log.appendChild(el('div', { class: 'dn-bld-bubble dn-bld-bubble-user' }, [el('p', { text })]));
  }
  _appendAssistant() {
    const txt = el('p', { class: 'dn-bld-bubble-text' });
    const chips = el('div', { class: 'dn-bld-bubble-chips' });
    const bubble = el('div', { class: 'dn-bld-bubble dn-bld-bubble-asst' }, [txt, chips]);
    bubble._txt = txt; bubble._chips = chips; bubble._buf = '';
    this._log.appendChild(bubble);
    return bubble;
  }
  _appendToken(bubble, t) {
    if (!bubble) return;
    bubble._buf = (bubble._buf || '') + String(t || '');
    patchText(bubble._txt, bubble._buf);
  }
  _appendTool(bubble, name) {
    if (!bubble) return;
    bubble._chips.appendChild(el('span', { class: 'dn-bld-chip dn-bld-chip-tool', text: 'tool: ' + String(name || '') }));
  }
  _appendErr(bubble, msg) {
    const m = String(msg || 'error');
    if (bubble) bubble.appendChild(el('p', { class: 'dn-bld-bubble-err', role: 'alert', text: m }));
    else this._log.appendChild(el('p', { class: 'dn-bld-bubble-err', role: 'alert', text: m }));
  }

  // tag the in-flight (or last) assistant bubble with an `edited: …` chip when
  // a patch frame lands and the host applies it to the shared draft.
  tagLastEdit(summary) {
    const bubble = this._lastBubble;
    if (!bubble || !bubble._chips) return;
    bubble._chips.appendChild(el('span', { class: 'dn-bld-chip dn-bld-chip-edit', text: 'edited: ' + String(summary || 'draft') }));
  }
}

function clamp(v, lo, hi) {
  let n = Number(v);
  if (!isFinite(n)) n = lo;
  return Math.max(lo, Math.min(hi, Math.round(n)));
}

// core/bus.js — a tiny synchronous pub/sub event bus.
//
// The render spine decouples state mutation from view rendering: an
// AppState mutation emits `state:changed`; the active view subscribes
// and re-renders. The router emits `route:changed`. The log stream
// emits `log:appended`. Nothing else couples a producer to a consumer.

const _topics = new Map();

export const bus = {
  on(topic, fn) {
    let set = _topics.get(topic);
    if (!set) { set = new Set(); _topics.set(topic, set); }
    set.add(fn);
    return () => set.delete(fn);
  },
  off(topic, fn) {
    const set = _topics.get(topic);
    if (set) set.delete(fn);
  },
  emit(topic, payload) {
    const set = _topics.get(topic);
    if (!set) return;
    // Iterate a copy so a handler can subscribe/unsubscribe re-entrantly.
    for (const fn of [...set]) {
      try { fn(payload); }
      catch (err) { console.error(`bus handler for ${topic} threw:`, err); }
    }
  },
  // Test-only: drop every subscription.
  _reset() { _topics.clear(); },
};

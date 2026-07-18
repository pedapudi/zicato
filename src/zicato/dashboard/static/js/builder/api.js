// js/builder/api.js — the builder REST client.
//
// Thin wrappers over the B1a/B1b endpoints. A single shared `session` keeps
// the form and the chat copilot editing the SAME server-side draft (the
// backend keys drafts by session id), so a chat patch and a form edit
// accumulate on one contract. GETs are failure-tolerant (null on error);
// POSTs return the parsed JSON envelope (which may itself carry `{error}`).

import { fetchJson } from '../core/api.js';

// One stable session for this browser tab. Shared by op + chat so the
// backend's DraftStore hands them the same mutable draft.
export const SESSION = 'dashboard';

export async function getConfig() {
  try { return await fetchJson('/builder/config'); } catch (e) { return null; }
}

// The unified models / LLM-endpoints settings surface. GET returns the
// secret-safe view (each role's spec carries the api_key_env NAME + an
// api_key_env_set boolean — never the secret value); POST persists the
// `models` block (a model/endpoint is runtime infra, so it never rolls the
// epoch). Failure-tolerant GET (null on error) mirrors getConfig.
export async function getModels() {
  try { return await fetchJson('/settings/models'); } catch (e) { return null; }
}

export async function saveModels(models) {
  return postJson('/settings/models', { models: models || {} });
}

export async function getDraft() {
  try { return await fetchJson('/builder/draft?session=' + encodeURIComponent(SESSION)); } catch (e) { return null; }
}

// The eval-suggestions inbox feed (EVAL-SYNTHESIS.md §6): the current epoch's
// latest reflection's persisted suggestions. Failure-tolerant (null on error);
// an empty / cold workspace returns {epoch_id, reflection_id, suggestions: []}.
export async function getSuggestions() {
  try { return await fetchJson('/builder/suggestions'); } catch (e) { return null; }
}

async function postJson(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  let payload = null;
  try { payload = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok && payload && !payload.error) payload.error = 'HTTP ' + res.status;
  return payload || { error: 'HTTP ' + res.status };
}

export async function postOp(op, args) {
  return postJson('/builder/op', { session: SESSION, op, args: args || {} });
}

export async function postApply(confirm) {
  return postJson('/builder/apply', { session: SESSION, confirm: !!confirm });
}

// The chat endpoint URL + the request body shape (the chat module owns the
// streaming read — fetch with a ReadableStream — so it can render SSE frames
// as they land rather than buffering the whole reply).
export const CHAT_PATH = '/builder/chat';
export function chatBody(message) { return { session: SESSION, message: String(message || '') }; }

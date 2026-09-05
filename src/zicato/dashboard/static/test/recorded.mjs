// test/recorded.mjs — the recorded endpoint responses the browser suite serves.
//
// tests/data/endpoint_route_snapshot.json holds what every probed dashboard
// route answered, captured through the real application by
// tests/test_dashboard_endpoint_table.py over the workspaces that
// tests/_console_scenarios.py writes; tests/data/endpoint_route_probes.json
// names the URL each label was asked for. A fixture map assembled here serves
// those bodies under the URLs the views fetch, so a server-side join a test
// renders (the round timeline, the racing field, a candidate dossier) is one a
// Python endpoint produced. Re-record both files with
// ZICATO_ENDPOINT_SNAPSHOT_UPDATE=1 when a reader's response is meant to change.
//
// tests/data/elim_states_cases.json declares the elimination round lists the
// suite draws, and tests/data/elim_states_served.json is what the server's
// fold (query.tournament_view.derive_elim_states) serves for each of them,
// recorded by tests/test_tournament_view_elim_states.py.

import { readFileSync } from 'node:fs';

const DATA = new URL('../../../../../tests/data/', import.meta.url);
const read = (name) => JSON.parse(readFileSync(new URL(name, DATA), 'utf8'));

const SNAPSHOT = read('endpoint_route_snapshot.json');
const PROBES = read('endpoint_route_probes.json');
let elimCases = null;
let elimServed = null;

// A fresh copy per call: a view may mutate the payload it is handed.
const copy = (v) => JSON.parse(JSON.stringify(v));

// The recorded body of one label (`console/gate/v0/v1`).
export function recorded(label) {
  const entry = SNAPSHOT[label];
  if (!entry) throw new Error(`no recorded response is labelled ${label}`);
  return copy(entry.body);
}

// Every recorded 200 response of one recorded workspace, keyed by the URL the
// route was asked for — the shape `installFixtureMap` takes.
export function recordedRoutes(workspace) {
  const out = {};
  for (const [label, url] of Object.entries(PROBES)) {
    if (!label.startsWith(workspace + '/')) continue;
    const entry = SNAPSHOT[label];
    if (entry && entry.status === 200) out[url] = copy(entry.body);
  }
  if (!Object.keys(out).length) throw new Error(`no recorded workspace is named ${workspace}`);
  return out;
}

// One declared elimination case: the round list a fixture draws (`input`) and
// the rounds + gen_states the server's fold serves for it (`served`).
export function elimCase(name) {
  if (!elimCases) { elimCases = read('elim_states_cases.json'); elimServed = read('elim_states_served.json'); }
  if (!(name in elimCases)) throw new Error(`no elimination case is named ${name}`);
  return { input: copy(elimCases[name]), served: copy(elimServed[name]) };
}

// A structure payload carrying the served elimination model, the way the
// Python `attach_elim_states` enriches every elimination record it serves.
export function elimPayload(name, extra) {
  const { served } = elimCase(name);
  return Object.assign({}, extra || {}, { rounds: served.rounds, gen_states: served.gen_states });
}

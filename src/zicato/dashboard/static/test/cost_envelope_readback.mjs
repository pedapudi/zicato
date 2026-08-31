// test/cost_envelope_readback.mjs — render server cost/validation envelopes
// and print what the browser would show.
//
// Driven by tests/test_builder_cost_envelope_correspondence.py, which computes
// every envelope with zicato.builder.operations (the sole owner of the cost
// arithmetic and the lint rules), writes them to a JSON file, and runs:
//
//     node test/cost_envelope_readback.mjs <fixtures.json>
//
// For each fixture this renders the production preview (builder/preview.js,
// the same module the builder view and Settings mount) into the harness DOM
// and reads the rendered numbers and texts back out of the resulting nodes.
// The readback goes to stdout as JSON, one object per fixture, and the Python
// side asserts it equals the values it computed. A field renamed on either
// side — Python emitting a key the renderer does not read, or the renderer
// reaching for a key Python does not emit — makes the readback disagree.

import { readFileSync } from 'node:fs';
import { installDom } from './harness.mjs';

installDom();

const { previewNodes } = await import('../js/builder/preview.js');

// Depth-first walk yielding every element node under (and including) `node`.
function* walk(node) {
  if (!node || node.nodeType !== 1) return;
  yield node;
  for (const child of node.childNodes || []) yield* walk(child);
}

function firstByClass(nodes, cls) {
  for (const root of nodes) {
    for (const node of walk(root)) {
      if (node.classList && node.classList.contains(cls)) return node;
    }
  }
  return null;
}

function allByClass(nodes, cls) {
  const out = [];
  for (const root of nodes) {
    for (const node of walk(root)) {
      if (node.classList && node.classList.contains(cls)) out.push(node);
    }
  }
  return out;
}

// The cost meter renders each line's label, run count and detail into the
// bar's `title`; read them back from there rather than from the input model,
// so the readback reflects what a reader of the page actually sees.
function readCostBar(bar) {
  const title = bar.getAttribute('title') || '';
  const head = title.indexOf(': ');
  const label = head < 0 ? '' : title.slice(0, head);
  const rest = head < 0 ? '' : title.slice(head + 2);
  const split = rest.indexOf(' · ');
  return {
    label,
    runs: Number(split < 0 ? rest : rest.slice(0, split)),
    detail: split < 0 ? '' : rest.slice(split + 3),
  };
}

function readback(fixture) {
  const nodes = previewNodes({
    structure: fixture.structure,
    params: fixture.params,
    cost: fixture.cost,
    warnings: fixture.warnings,
    boardCount: fixture.board_count,
    trainCount: fixture.train_count,
    holdoutCount: fixture.holdout_count,
    readonly: true,
    heading: 'Contract at a glance',
  });
  const headline = firstByClass(nodes, 'dn-bld-cost-num');
  return {
    name: fixture.name,
    board_runs_per_round: Number(headline ? headline.textContent : NaN),
    breakdown: allByClass(nodes, 'dn-bld-cost-bar').map(readCostBar),
    warnings: allByClass(nodes, 'dn-bld-warn').map((warn) => {
      const msg = firstByClass([warn], 'dn-bld-warn-msg');
      // Severity rides the modifier class the list item carries.
      const severity = ['info', 'warning', 'refuse'].find(
        (s) => warn.classList.contains('dn-bld-warn-' + s),
      );
      return { severity: severity || '', message: msg ? msg.textContent : '' };
    }),
  };
}

const fixtures = JSON.parse(readFileSync(process.argv[2], 'utf8'));
process.stdout.write(JSON.stringify(fixtures.map(readback), null, 2) + '\n');

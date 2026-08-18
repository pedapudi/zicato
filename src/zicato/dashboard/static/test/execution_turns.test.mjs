import { installDom, test, run, assert, assertEqual } from './harness.mjs';
installDom();
const { buildExecutionOutline, buildTurnNode, reconcileTurns } = await import('../js/turns.js');
function allByClass(node, name, out = []) {
  if (node.classList && node.classList.contains(name)) out.push(node);
  for (const child of node.children || []) allByClass(child, name, out);
  return out;
}
const EXECUTION = {
  fidelity: 'partial', nodes: [
    { node_id: 'a1', kind: 'agent', parent_id: null, name: 'researcher', status: 'completed', summary: 'evidence returned', fidelity: 'exact' },
    { node_id: 't1', kind: 'tool', parent_id: 'a1', name: 'search', status: 'observed', summary: '18 results', fidelity: 'turn' },
    { node_id: 'a2', kind: 'agent', parent_id: 'a1', name: 'checker', status: 'failed', summary: 'source unavailable', fidelity: 'unresolved' },
  ],
  root_ids: ['a1'], unresolved_ids: ['a2'],
};
test('execution outline renders exact nested topology with stable accessibility attributes', () => {
  const tree = buildExecutionOutline(EXECUTION, ['a1']);
  assert(tree, 'tree renders');
  assertEqual(tree.getAttribute('role'), 'tree');
  assertEqual(tree.getAttribute('data-execution-fidelity'), 'partial');
  const nodes = allByClass(tree, 'dn-exec-node');
  assertEqual(nodes.length, 3);
  assertEqual(nodes[0].getAttribute('data-node-id'), 'a1');
  assertEqual(nodes[0].getAttribute('data-node-kind'), 'agent');
  assertEqual(nodes[1].getAttribute('data-node-kind'), 'tool');
  assertEqual(nodes[2].getAttribute('data-status'), 'failed');
  assertEqual(nodes[2].getAttribute('data-fidelity'), 'unresolved');
  const toggle = allByClass(tree, 'dn-exec-toggle')[0];
  assertEqual(toggle.getAttribute('aria-expanded'), 'false');
});
test('turn renderer degrades exactly when execution is absent', () => {
  const turn = { seq: 1, role: 'agent', text: 'done', activity_ids: ['a1'] };
  const oldShape = buildTurnNode(turn, new Map());
  const unavailable = buildTurnNode(turn, new Map(), { fidelity: 'unavailable', nodes: [], root_ids: [], unresolved_ids: [] });
  assertEqual(allByClass(oldShape, 'dn-execution').length, 0);
  assertEqual(allByClass(unavailable, 'dn-execution').length, 0);
  assertEqual(oldShape.textContent, unavailable.textContent);
});
test('execution completion patches only its owning turn and no-op preserves identity', () => {
  const host = document.createElement('div');
  const turns = [{ seq: 1, role: 'agent', text: 'working', activity_ids: ['a1'] },
    { seq: 2, role: 'agent', text: 'waiting' }];
  const running = structuredClone(EXECUTION);
  running.nodes[0].status = 'running';
  reconcileTurns(host, turns, [], running);
  const first = host.childNodes[0];
  const second = host.childNodes[1];
  const noop = reconcileTurns(host, turns, [], running);
  assertEqual(noop.patched, 0);
  assert(host.childNodes[0] === first && host.childNodes[1] === second, 'no-op keeps every turn node');
  const done = reconcileTurns(host, turns, [], EXECUTION);
  assertEqual(done.patched, 1);
  assert(host.childNodes[0] !== first, 'owning turn patches');
  assert(host.childNodes[1] === second, 'unrelated turn survives');
});
test('malformed cycles are bounded instead of blanking the pane', () => {
  const cyclic = { fidelity: 'partial', nodes: [
    { node_id: 'one', kind: 'agent', parent_id: 'two', status: 'completed' },
    { node_id: 'two', kind: 'tool', parent_id: 'one', status: 'completed' },
  ] };
  const tree = buildExecutionOutline(cyclic, ['one']);
  assert(tree, 'the known prefix renders');
  assertEqual(allByClass(tree, 'dn-exec-node').length, 2);
});
test('unattached roots and unresolved records render honestly at run scope', () => {
  const host = document.createElement('div');
  reconcileTurns(host, [{ seq: 1, role: 'agent', text: 'done' }], [], EXECUTION);
  const unresolved = allByClass(host, 'dn-exec-unresolved');
  assertEqual(unresolved.length, 1);
  assert(unresolved[0].textContent.includes('Run activity'));
  assert(unresolved[0].textContent.includes('researcher'));
  assert(unresolved[0].textContent.includes('checker'));
});
test('deep agent and tool mixtures render at full stated depth in stream order', () => {
  const deep = { fidelity: 'exact', root_ids: ['a1'], unresolved_ids: [], nodes: [
    { node_id: 'a1', kind: 'agent', parent_id: null, name: 'coordinator', status: 'completed', start_source_index: 0 },
    { node_id: 't1', kind: 'tool', parent_id: 'a1', name: 'researcher', status: 'observed', start_source_index: 1 },
    { node_id: 'a2', kind: 'agent', parent_id: 'a1', name: 'researcher', status: 'completed', start_source_index: 2 },
    { node_id: 't2', kind: 'tool', parent_id: 'a2', name: 'archivist', status: 'observed', start_source_index: 3 },
    { node_id: 'a3', kind: 'agent', parent_id: 'a2', name: 'archivist', status: 'failed', summary: 'error:Boom', start_source_index: 4 },
  ] };
  const tree = buildExecutionOutline(deep, ['a1']);
  assertEqual(allByClass(tree, 'dn-exec-node').length, 5);
  const leaf = tree.querySelector('[data-node-id="a3"]');
  let depth = 0;
  for (let up = leaf.parentNode; up && up !== tree; up = up.parentNode) {
    if (up.classList && up.classList.contains('dn-exec-children')) depth += 1;
  }
  assertEqual(depth, 2, 'grandchild nests two rails deep');
  const siblings = allByClass(tree.querySelector('[data-node-id="a1"]'), 'dn-exec-node')
    .filter((node) => {
      const owner = node.parentNode && node.parentNode.parentNode;
      return owner && owner.getAttribute && owner.getAttribute('data-node-id') === 'a1';
    })
    .map((node) => node.getAttribute('data-node-id'));
  assertEqual(siblings.join(','), 't1,a2', 'a delegation precedes the invocation it observed');
});
test('a running root without a conversation turn remains visible at run scope', () => {
  const scroller = document.createElement('div');
  const execution = { fidelity: 'exact', root_ids: ['worker'], unresolved_ids: [], nodes: [
    { node_id: 'worker', kind: 'agent', parent_id: null, name: 'worker', status: 'running', fidelity: 'exact' },
  ] };
  reconcileTurns(scroller, [], [], execution);
  assertEqual(allByClass(scroller, 'dn-exec-unresolved-title')[0].textContent, 'Run activity');
  assertEqual(scroller.querySelector('[data-node-id="worker"]').dataset.status, 'running');
});
await run();

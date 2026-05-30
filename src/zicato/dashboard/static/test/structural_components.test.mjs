// test/structural_components.test.mjs — the structural redesign pair.
//
// Pins the two new reusable components:
//   * lineageRibbon — the unified spine + epoch-timeline + trajectory
//     ribbon (lineage_ribbon.js). scalar-on-Y placement, rejected
//     branch off its parent, the live node marked, onSelect on click,
//     and re-render safety.
//   * healthBanner — the loop-health banner (health_banner.js). the
//     right severity tone, the null-report fallback, re-render safety.

import { installDom, test, run, assert, makeEvent } from './harness.mjs';

installDom();

const { lineageRibbon, computeRibbonLayout, RIBBON_GEOMETRY } =
  await import('../js/components/lineage_ribbon.js');
const { healthBanner } = await import('../js/components/health_banner.js');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function descendantsWithClass(node, cls) {
  const out = [];
  const walk = (n) => {
    if (!n || n.nodeType !== 1) return;
    if (n.classList && n.classList.contains(cls)) out.push(n);
    for (const c of n.children) walk(c);
  };
  walk(node);
  return out;
}

function descendantsByTag(node, tag) {
  const out = [];
  const lname = tag.toLowerCase();
  const walk = (n) => {
    if (!n || n.nodeType !== 1) return;
    if (n.localName === lname) out.push(n);
    for (const c of n.children) walk(c);
  };
  walk(node);
  return out;
}

function nodeById(root, id) {
  return descendantsWithClass(root, 'ribbon-node')
    .find((n) => n.getAttribute('data-node-id') === id) || null;
}

// ---------------------------------------------------------------------------
// (1) Factory contracts — both return a node.
// ---------------------------------------------------------------------------

test('lineageRibbon returns a detached DOM node', () => {
  const node = lineageRibbon({ nodes: [{ id: 'v0', scalar: 0.5, verdict: 'promoted' }] });
  assert(node && node.nodeType === 1, 'must return an element');
  assert(node.classList.contains('ribbon'), 'root carries the .ribbon class');
  assert(node.parentNode == null, 'must be detached');
});

test('lineageRibbon renders an empty state for no nodes', () => {
  const node = lineageRibbon({ nodes: [] });
  assert(node.classList.contains('ribbon-empty'), 'empty state class');
  assert(node.textContent.includes('No lineage'), 'empty message present');
});

test('healthBanner returns a detached DOM node', () => {
  const node = healthBanner({ report: { healthy: true, findings: [] } });
  assert(node && node.nodeType === 1, 'must return an element');
  assert(node.classList.contains('health-banner'), 'root carries .health-banner');
  assert(node.parentNode == null, 'must be detached');
});

// ---------------------------------------------------------------------------
// (2) Re-render safety — calling each factory twice yields independent,
//     fresh nodes (and never writes innerHTML).
// ---------------------------------------------------------------------------

test('lineageRibbon is re-render-safe: two calls yield distinct fresh nodes', () => {
  const data = { nodes: [{ id: 'v0', scalar: 0.4, verdict: 'promoted' }] };
  const a = lineageRibbon(data);
  const b = lineageRibbon(data);
  assert(a !== b, 'each call returns a new node');
  assert(a.innerHTMLWriteCount() === 0, 'never writes innerHTML (a)');
  assert(b.innerHTMLWriteCount() === 0, 'never writes innerHTML (b)');
});

test('healthBanner is re-render-safe: two calls yield distinct fresh nodes', () => {
  const data = { report: { healthy: false, findings: [
    { code: 'stalled_loop', severity: 'warning', summary: 'stalled', detail: {} },
  ] } };
  const a = healthBanner(data);
  const b = healthBanner(data);
  assert(a !== b, 'each call returns a new node');
  assert(a.innerHTMLWriteCount() === 0, 'never writes innerHTML (a)');
  assert(b.innerHTMLWriteCount() === 0, 'never writes innerHTML (b)');
});

// ---------------------------------------------------------------------------
// (3) lineageRibbon: scalar encodes the Y position — a LOWER scalar
//     sits HIGHER on screen (smaller y).
// ---------------------------------------------------------------------------

test('computeRibbonLayout places a lower scalar higher (smaller y) than a higher scalar', () => {
  const layout = computeRibbonLayout([
    { id: 'v0', parentId: null, scalar: 0.9, verdict: 'promoted' },
    { id: 'v1', parentId: 'v0', scalar: 0.2, verdict: 'promoted' },
  ]);
  const v0 = layout.columns.find((c) => String(c.node.id) === 'v0');
  const v1 = layout.columns.find((c) => String(c.node.id) === 'v1');
  assert(v0 && v1, 'both columns must exist');
  // v1 has the lower loss → it must sit higher → smaller y.
  assert(v1.y < v0.y,
    'lower scalar must map to a smaller y (higher on screen); '
      + `v0.y=${v0.y} v1.y=${v1.y}`);
  // The best (min) scalar pins to PLOT_TOP, the worst (max) to PLOT_BOTTOM.
  assert(Math.abs(v1.y - RIBBON_GEOMETRY.PLOT_TOP) < 0.001,
    'min scalar must pin to PLOT_TOP; got ' + v1.y);
  assert(Math.abs(v0.y - RIBBON_GEOMETRY.PLOT_BOTTOM) < 0.001,
    'max scalar must pin to PLOT_BOTTOM; got ' + v0.y);
  // x increases left → right with lineage order.
  assert(v1.x > v0.x, 'later node sits further right');
});

test('lineageRibbon paints the promoted spine as a single trajectory path', () => {
  const node = lineageRibbon({
    nodes: [
      { id: 'v0', parentId: null, scalar: 0.6, verdict: 'promoted' },
      { id: 'v1', parentId: 'v0', scalar: 0.3, verdict: 'promoted' },
    ],
  });
  const svgs = descendantsByTag(node, 'svg');
  assert(svgs.length === 1, 'exactly one SVG canvas');
  const spinePaths = descendantsWithClass(svgs[0], 'ribbon-spine-path');
  assert(spinePaths.length >= 1, 'a spine trajectory path is painted');
});

// ---------------------------------------------------------------------------
// (4) lineageRibbon: a rejected challenger branches OFF its parent.
// ---------------------------------------------------------------------------

test('lineageRibbon branches a rejected challenger off its parent node', () => {
  const node = lineageRibbon({
    nodes: [
      { id: 'v0', parentId: null, scalar: 0.5, verdict: 'promoted' },
      { id: 'v1', parentId: 'v0', scalar: 0.4, verdict: 'promoted' },
      { id: 'v2', parentId: 'v1', scalar: null, verdict: 'rejected' },
    ],
  });
  // The rejected node renders as a branch node, NOT a spine column.
  const v2 = nodeById(node, 'v2');
  assert(v2, 'rejected challenger must render');
  assert(v2.classList.contains('ribbon-node-branch'),
    'rejected challenger must render as a branch node');
  assert(v2.getAttribute('data-verdict') === 'rejected',
    'verdict marked rejected');
  // The branch connector must run from the branch back to its parent.
  const layout = computeRibbonLayout([
    { id: 'v0', parentId: null, scalar: 0.5, verdict: 'promoted' },
    { id: 'v1', parentId: 'v0', scalar: 0.4, verdict: 'promoted' },
    { id: 'v2', parentId: 'v1', scalar: null, verdict: 'rejected' },
  ]);
  assert(layout.branches.length === 1, 'one branch challenger');
  const br = layout.branches[0];
  assert(br.parent && String(br.parent.node.id) === 'v1',
    'branch must hang off its parent v1');
  // It is nudged off the spine column, not sitting on it.
  assert(br.x !== br.parent.x || br.y !== br.parent.y,
    'branch must be offset from its parent dot');
  // A subordinate dashed branch path is painted.
  const branchPaths = descendantsWithClass(node, 'ribbon-branch-path');
  assert(branchPaths.length === 1, 'one branch path painted');
});

// ---------------------------------------------------------------------------
// (5) lineageRibbon: the live node is the right-most and marked live.
// ---------------------------------------------------------------------------

test('lineageRibbon marks the live node and places it right-most', () => {
  const node = lineageRibbon({
    nodes: [
      { id: 'v0', parentId: null, scalar: 0.5, verdict: 'promoted' },
      { id: 'v3', parentId: 'v0', scalar: 0.3, verdict: 'promoted' },
      { id: 'v8', parentId: 'v3', scalar: null, verdict: 'open', live: true },
    ],
  });
  const live = nodeById(node, 'v8');
  assert(live, 'live node must render');
  assert(live.classList.contains('ribbon-node-live'),
    'live node carries the live class');
  assert(descendantsWithClass(live, 'ribbon-dot-live').length === 1,
    'live dot styled live (dashed + pulse)');
  assert(live.textContent.includes('LIVE'), 'live tag shown');
  // Right-most: its layout x exceeds every other column's x.
  const layout = computeRibbonLayout([
    { id: 'v0', parentId: null, scalar: 0.5, verdict: 'promoted' },
    { id: 'v3', parentId: 'v0', scalar: 0.3, verdict: 'promoted' },
    { id: 'v8', parentId: 'v3', scalar: null, verdict: 'open', live: true },
  ]);
  const liveCol = layout.columns.find((c) => c.isLive);
  const maxX = Math.max(...layout.columns.map((c) => c.x));
  assert(liveCol.x === maxX, 'live node must be right-most');
});

// ---------------------------------------------------------------------------
// (6) lineageRibbon: clicking a node fires onSelect with its id.
// ---------------------------------------------------------------------------

test('lineageRibbon fires onSelect(id) when a node is clicked', () => {
  let picked = null;
  const node = lineageRibbon({
    nodes: [
      { id: 'v0', parentId: null, scalar: 0.5, verdict: 'promoted' },
      { id: 'v1', parentId: 'v0', scalar: 0.3, verdict: 'promoted' },
    ],
    onSelect: (id) => { picked = id; },
  });
  const v1 = nodeById(node, 'v1');
  assert(v1, 'target node must exist');
  v1.dispatchEvent(makeEvent('click'));
  assert(picked === 'v1', 'onSelect must receive the clicked node id; got ' + picked);
});

test('lineageRibbon node uses the shared verdict glyph vocabulary', () => {
  const node = lineageRibbon({
    nodes: [
      { id: 'v0', parentId: null, scalar: 0.5, verdict: 'promoted' },
      { id: 'v2', parentId: 'v0', scalar: 0.6, verdict: 'rejected' },
      { id: 'v9', parentId: 'v0', scalar: null, verdict: 'open', live: true },
    ],
  });
  assert(nodeById(node, 'v0').textContent.includes('✓'), 'promoted uses ✓');
  assert(nodeById(node, 'v2').textContent.includes('✗'), 'rejected uses ✗');
  assert(nodeById(node, 'v9').textContent.includes('◦'), 'open uses ◦');
});

// ---------------------------------------------------------------------------
// (7) lineageRibbon: zoom adjusts label source/density.
// ---------------------------------------------------------------------------

test('lineageRibbon epochs zoom shows the epoch label, generations zoom shows the id', () => {
  const nodes = [
    { id: 'g0', parentId: null, scalar: 0.5, verdict: 'promoted', label: 'epoch-42' },
  ];
  const epochs = lineageRibbon({ nodes, zoom: 'epochs' });
  const gens = lineageRibbon({ nodes, zoom: 'generations' });
  assert(epochs.classList.contains('ribbon-zoom-epochs'), 'epochs zoom class');
  assert(gens.classList.contains('ribbon-zoom-generations'), 'generations zoom class');
  assert(epochs.textContent.includes('epoch-42'), 'epochs zoom leads with the label');
  assert(gens.textContent.includes('g0'), 'generations zoom shows the gen id');
});

// ---------------------------------------------------------------------------
// (8) healthBanner: picks the correct severity tone.
// ---------------------------------------------------------------------------

test('healthBanner is green/ok when healthy with no findings', () => {
  const node = healthBanner({ report: { healthy: true, findings: [] } });
  assert(node.classList.contains('health-banner-ok'),
    'healthy report paints the ok tone; class=' + node.className);
  assert(node.getAttribute('data-tone') === 'ok', 'data-tone=ok');
});

test('healthBanner is amber/warn for a top warning finding', () => {
  const node = healthBanner({
    report: {
      healthy: false,
      findings: [
        { code: 'no_expectations', severity: 'info', summary: 'mostly drift', detail: {} },
        { code: 'stalled_loop', severity: 'warning', summary: '3 rejected in a row', detail: {} },
      ],
    },
  });
  assert(node.classList.contains('health-banner-warn'),
    'a top warning paints the warn tone; class=' + node.className);
  assert(node.textContent.includes('3 rejected in a row'),
    'shows the highest-severity finding summary');
});

test('healthBanner is red/err when any critical finding is present', () => {
  const node = healthBanner({
    report: {
      healthy: false,
      findings: [
        { code: 'stalled_loop', severity: 'warning', summary: 'stalled', detail: {} },
        {
          code: 'degenerate_scoring', severity: 'critical',
          summary: 'flat loss surface', detail: { window: 3 },
        },
      ],
    },
  });
  assert(node.classList.contains('health-banner-err'),
    'a critical finding paints the err tone; class=' + node.className);
  assert(node.textContent.includes('flat loss surface'),
    'leads with the critical summary, not the warning');
});

// ---------------------------------------------------------------------------
// (9) healthBanner: details toggle reveals detail + remaining findings.
// ---------------------------------------------------------------------------

test('healthBanner details toggle expands the detail region', () => {
  let toggled = null;
  const node = healthBanner({
    onToggleDetail: (open) => { toggled = open; },
    report: {
      healthy: false,
      findings: [
        {
          code: 'degenerate_scoring', severity: 'critical',
          summary: 'flat loss surface',
          detail: { window: 3, epsilon: 1e-6 },
        },
        {
          code: 'non_differentiating_entry', severity: 'warning',
          summary: 'dead test', detail: { entry_id: 'e1' },
        },
      ],
    },
  });
  const region = descendantsWithClass(node, 'health-banner-detail')[0];
  assert(region, 'detail region must exist');
  assert(region.hasAttribute('hidden'), 'detail region starts hidden');
  const toggle = descendantsWithClass(node, 'health-banner-toggle')[0];
  assert(toggle, 'toggle button must exist');
  toggle.dispatchEvent(makeEvent('click'));
  assert(!region.hasAttribute('hidden'), 'detail region reveals on toggle');
  assert(toggled === true, 'onToggleDetail receives the open state');
  // Detail text + the remaining (warning) finding both appear.
  assert(region.textContent.includes('window'), 'detail keys render');
  assert(region.textContent.includes('dead test'), 'remaining findings render');
});

// ---------------------------------------------------------------------------
// (10) healthBanner: the null-report fallback is muted and never throws.
// ---------------------------------------------------------------------------

test('healthBanner renders a muted not-yet-evaluated line for a null report', () => {
  const node = healthBanner({ report: null });
  assert(node.classList.contains('health-banner-muted'),
    'null report paints the muted variant');
  assert(node.textContent.includes('not yet evaluated'),
    'muted fallback message present');
});

test('healthBanner tolerates an absent report object without throwing', () => {
  const node = healthBanner({});
  assert(node.classList.contains('health-banner-muted'),
    'absent report also paints muted');
});

await run();

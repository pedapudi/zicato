// variants/F/views/bench.js — Screen 5b: bench / status (basic).
//
// The "is the loop healthy?" board. Service identity, the live
// heartbeat phase, and the latest loop-health report (the detectors that
// catch a running-but-meaningless loop). Diagram-light by design — this
// is the operator's instrument panel, not a causal diagram.
//
// Data: state.health (service), state.heartbeat, state.healthReport.

import { el, clearChildren } from '../../../core/dom.js';
import { verdictClass } from '../diagram/primitives.js';

let _lastDigest = null;

export function renderBench(ctx) {
  const { stage, state } = ctx;

  // Digest gate: connection, heartbeat phase/epoch/gen, service identity,
  // and the loop-health findings — no timestamps. A heartbeat that does
  // not change the phase or findings is a no-op.
  const hb = state.heartbeat || {};
  const svc = state.service || {};
  const report = state.healthReport;
  const digest = JSON.stringify({
    conn: state.connected ? 'live' : (state.connecting ? 'connecting' : 'offline'),
    phase: hb.phase || '', epoch: hb.epoch_id || '', gen: hb.generation_id || '',
    version: svc.version || '', port: svc.port || '',
    healthy: report ? !!report.healthy : null,
    findings: report && Array.isArray(report.findings)
      ? report.findings.map((f) => [f.severity, f.detector || f.name, f.summary]) : null,
  });
  if (digest === _lastDigest && stage.firstChild) return;
  _lastDigest = digest;

  clearChildren(stage);

  stage.appendChild(el('div', { class: 'cz-screen-head' }, [
    el('h1', { class: 'cz-screen-title' }, ['Bench']),
    el('p', { class: 'cz-screen-sub' }, [
      'The instrument panel — service identity, live phase, and loop-health detectors that catch a toothless evaluation.',
    ]),
  ]));

  // Service + heartbeat tiles (svc, hb, report computed in the digest gate).
  const tile = (label, value) => el('div', { class: 'cz-tile' }, [
    el('div', { class: 'cz-tile-label' }, [label]),
    el('div', { class: 'cz-tile-value cz-mono' }, [value == null || value === '' ? '—' : String(value)]),
  ]);
  stage.appendChild(el('div', { class: 'cz-tile-strip' }, [
    tile('connection', state.connected ? 'live' : (state.connecting ? 'connecting' : 'offline')),
    tile('phase', hb.phase),
    tile('epoch', hb.epoch_id),
    tile('generation', hb.generation_id),
    tile('version', svc.version),
    tile('port', svc.port),
  ]));

  // Loop-health report.
  stage.appendChild(el('h2', { class: 'cz-section-title' }, ['Loop health']));
  const findings = report && Array.isArray(report.findings) ? report.findings : [];
  if (!report) {
    stage.appendChild(el('div', { class: 'cz-empty' }, ['No loop-health report yet.']));
  } else if (report.healthy && findings.length === 0) {
    stage.appendChild(el('div', { class: 'cz-health-ok' }, [
      el('span', { class: 'cz-legend-dot cz-v-promoted' }), 'Loop healthy — the evaluation is distinguishing candidates.',
    ]));
  } else {
    const list = el('div', { class: 'cz-health-list' });
    for (const f of findings) {
      const sev = String(f.severity || 'info').toLowerCase();
      const cls = sev === 'critical' ? 'cz-v-rejected' : (sev === 'warn' || sev === 'warning' ? 'cz-v-deferred' : 'cz-v-neutral');
      list.appendChild(el('div', { class: 'cz-health-finding' }, [
        el('div', { class: 'cz-health-finding-head' }, [
          el('span', { class: 'cz-health-sev ' + cls }, [sev]),
          el('span', { class: 'cz-health-name cz-mono' }, [String(f.detector || f.name || '?')]),
        ]),
        f.summary ? el('p', { class: 'cz-health-summary' }, [String(f.summary)]) : null,
        f.remedy ? el('p', { class: 'cz-health-remedy' }, ['→ ', String(f.remedy)]) : null,
      ]));
    }
    stage.appendChild(list);
  }
  void verdictClass;
}

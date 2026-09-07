# Error handling

Zicato distinguishes failures by the operation they protect. Invalid configuration,
a broken execution invariant, or a failed authoritative publication must remain
an explicit failure. An optional report or cleanup failure must remain visible
without invalidating an otherwise valid tournament.

[ROBUSTNESS.md](ROBUSTNESS.md) describes process ownership, cancellation and
recovery. [LOOP-HEALTH.md](LOOP-HEALTH.md) describes diagnostic findings.
[LOGGING.md](LOGGING.md) defines the shared operational stream used here.

## Classify the protected operation

| Operation | Required behavior |
|---|---|
| Authored configuration and execution invariants | Propagate the original validation failure. Do not substitute a default or an empty successful result. |
| Authoritative loss, contract, lineage and decision publication | Preserve failure and the owner's recovery behavior. Logging cannot make an incomplete publication successful. |
| Optional reports, diagnostic captures and derived projections | Continue only where the domain owner declares the operation optional; retain a diagnostic describing what failed. |
| Expected control flow | Catch the specific expected exception. Cancellation and process exit remain under the lifecycle owner's control. |

A broad catch is not evidence that its protected operation is optional. Review
that operation before changing its exception boundary. In particular, target
execution is evaluation work; it must use the runner's failure semantics.

## Optional-operation diagnostics

`zicato.util.best_effort.best_effort` catches `Exception` around explicitly
optional side effects. It writes a warning to the existing operational logger
with `operation` and `exception_type` fields. It does not retain exception text
in those fields. An optional `on_error` callback can preserve a caller's more
detailed diagnostic. `KeyboardInterrupt`, `SystemExit` and cancellation propagate.

```python
from zicato.util import best_effort

with best_effort("epoch analysis report regeneration"):
    regenerate_epoch_report_deterministic(workspace_root, epoch_id)
```

A catch that needs to return a diagnostic fallback can call
`report_optional_failure(operation, exception)` at the same boundary. This does
not change which exceptions the caller catches or which result it returns.

Workers append to their invocation's existing log file with epoch, generation
and run coordinates. Round invocations bind the epoch and `fields.round_index`
until they return. A warning written after worker result publication remains
process evidence in that stream. A failed optional `result.json` capture leaves
the independently published loss and worker transport intact; failed loss
publication still makes the worker fail.

## Health reads persisted observations

`health.inputs.epoch_optional_failures` reads the selected epoch's retained
warnings through the shared log reader. The health assessor produces ordinary
warning findings and includes them in its saved round report. These findings
do not change loss, promotion or the critical-health stop condition.

The health response merges saved findings with more recent warnings. Invocation
and byte cursor identify each persisted event, preventing duplicate display when
the same event appears in both inputs. Separate retry failures remain separate
observations, even when they share a run id. There is no process-local failure
counter, additional counter file, or worker-result field to reconcile.

Health inspects up to 2,000 recent rows per retained invocation through the log
reader's bounded tail. Saved findings remain available after their source row
falls outside that tail. Existing retention and capture-level settings still
apply: an `ERROR` or `CRITICAL` capture floor suppresses warning rows, and health
cannot reconstruct them. An absent or unwritable log stream leaves process
diagnostics only. The displayed observations are not a complete failure count.

Saved health reports use the `LoopHealth` codec. Absence is an empty state;
malformed present records produce an explicit unreadable response. Health never
manufactures a successful report from corrupt saved evidence.

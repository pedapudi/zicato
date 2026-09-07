# Installation profiles

Zicato separates the measurement loop from its optional operator interfaces.
The base wheel can load a board, run the loop, score results, persist canonical
artifacts, and emit JSONL telemetry. It does not install an HTTP server, a
live telemetry service, an adapter ecosystem, or the proposer
protocol server.

## Profiles

| Profile | Install | Adds |
|---|---|---|
| Base | `pip install zicato` | Core loop, storage, scoring, JSONL telemetry, CLI |
| Dashboard | `pip install 'zicato[dashboard]'` | Browser dashboard |
| Observability | `pip install 'zicato[observability]'` | Dashboard and live execution telemetry |
| Complete | `pip install 'zicato[all]'` | Every shipped runtime integration and interface |
| Development | `uv sync --all-extras` | Complete runtime plus tests, lint, typing, and examples |

The `dashboard` profile serves browser views without the live telemetry
service. `observability` includes both. `all` includes every runtime profile.

## Degraded behavior

Optional interfaces are capability boundaries, not core-loop requirements.
Requesting a missing interface reports the extra that supplies it. Missing live
telemetry leaves the loop running with canonical JSONL events; it does not alter
losses, promotion decisions, or workspace formats.

The base-profile invariant is enforced in package metadata tests: neither live
telemetry distribution may return to the hard dependency set. Composition tests
also require `observability` to contain dashboard and live telemetry support and
`all` to contain every runtime profile.

## Public Python API

The package root is deliberately small and lazy. It exports only:

- the one-round and multi-round evolve entry points and round outcome;
- harness protocols and the runtime call protocol;
- board, workspace, and configuration loaders; and
- scoring configuration types and scaffold defaults.

Epoch lifecycle, storage, query, health, tournament, and generation-store APIs
remain available from their owning subpackages. The pre-alpha simplification is
intentionally breaking: removed root names have no forwarding aliases.

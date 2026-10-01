# Installation profiles

Zicato separates the measurement loop from its optional operator interfaces.
The base wheel can load a board, run the loop, score results, persist canonical
artifacts, and emit JSONL telemetry. It includes the Foe host package, the
only proposer runtime. It does not install an HTTP server, a live telemetry
service, Goldfive, or the ADK adapter ecosystem.

## Profiles

| Profile | Install | Adds |
|---|---|---|
| Base | `pip install zicato` | Core loop, storage, scoring, JSONL telemetry, CLI |
| Dashboard | `pip install 'zicato[dashboard]'` | Browser dashboard |
| Observability | `pip install 'zicato[observability]'` | Dashboard and live execution telemetry |
| Goldfive | `pip install 'zicato[goldfive]'` | Goldfive event runtime for adapters that declare the integration |
| Goldfive remote | `pip install 'zicato[goldfive-remote]'` | Goldfive with remote judge and embedding endpoints |
| Goldfive local embedding | `pip install 'zicato[goldfive-local-embedding]'` | Goldfive with the local embedding detector |
| ADK | `pip install 'zicato[adk]'` | The ADK adapter plus its Goldfive composition |
| Complete | `pip install 'zicato[all]'` | Every shipped runtime integration and interface |
| Development | `uv sync --all-extras` | Complete runtime plus tests, lint, typing, and examples |

The `dashboard` profile serves browser views without the live telemetry
service. `observability` includes both. `all` includes every runtime profile.
[GOLDFIVE-CONFIG.md](GOLDFIVE-CONFIG.md) describes the Goldfive profiles.

## Degraded behavior

Optional interfaces are capability boundaries, not core-loop requirements.
Configuring Goldfive without it installed, or requesting live telemetry
without it, reports the extra that supplies it. Running `zicato dashboard`
without the dashboard dependencies reports that the dashboard service is not
available. Missing live
telemetry leaves the loop running with canonical JSONL events; it does not alter
losses, promotion decisions, or workspace formats.

The base-profile invariant is enforced in package metadata tests: neither live
telemetry distribution may return to the hard dependency set. Composition tests
also require `observability` to contain the dashboard dependencies, and `all`
to contain `observability`, Goldfive and the ADK.

## Public Python API

The package root is deliberately small and lazy. It exports only:

- the one-round and multi-round evolve entry points and the round outcome
  (`evolve_once`, `evolve_n_rounds`, `EvolveRoundOutcome`);
- the harness protocols and the runtime call protocol (`HarnessAdapter`,
  `RunnableHarness`, `CallLLM`);
- the board, workspace, and configuration loaders (`load_board`,
  `load_workspace_config`, `load_config`, `ZicatoConfig`); and
- the scoring weights type (`ScoringWeights`).

Epoch lifecycle, storage, query, health, tournament, and generation-store APIs
remain available from their owning subpackages. The package root does not
re-export them.

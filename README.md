# zicato

**A self-improving harness for multi-agent systems.**

zicato wraps a multi-agent system you already have — a coordinator + specialists,
a deep sub-agent tree, a single LlmAgent, whatever shape — and turns it into the
**inner harness** of a learning loop. It runs your system against a board of
tasks, watches what goes wrong via structured runtime telemetry, and rewrites
the inner harness so the next generation goes less wrong.

zicato is the third member of an ecosystem:

- **[goldfive](https://github.com/pedapudi/goldfive)** — orchestration scaffolding:
  goals, plans, per-turn drift analysis, an intervention ladder. Emits a typed
  event stream (`goldfive.v1.Event`) that names *what went wrong* in a run.
- **[harmonograf](https://github.com/pedapudi/harmonograf)** — the observability
  + HCI console: Gantt, graph, trajectory, intervention history. Renders the
  goldfive stream live and lets operators steer.
- **zicato** — the meta-loop: same telemetry stream, but consumed across many
  runs. zicato aggregates drift into **loss patterns**, proposes structured
  edits to the inner harness (agent instructions, tool descriptions, planner
  templates, role scopes), runs tournaments, and promotes the patches that
  reduce loss.

## Where this fits

| Layer | Owner | Cadence |
|---|---|---|
| Single-turn refine (replan in response to drift) | goldfive | within one run |
| Operator-driven steering | harmonograf | within one run |
| **Inner-harness rewrites across runs** | **zicato** | **across generations** |

Goldfive owns plans; zicato owns the prompts and structure that *produce* the
plans. The two are complementary: goldfive handles "this run wandered, replan
this run", zicato handles "this kind of run keeps wandering the same way,
rewrite the harness".

## Status

Alpha. Design and surface are under active iteration — the public API will
break. The first reference adapter targets Google ADK (the framework goldfive
itself wires deepest into). The design is **framework-agnostic at its core**:
any inner harness that fronts a `HarnessAdapter` and emits goldfive telemetry
can participate. LangChain and plain-callable adapters land after ADK.

## Model-agnostic

zicato calls LLMs only through a narrow `call_llm(system, user, model) -> str`
callable supplied by the caller. No vendor SDK is imported by the library
itself; bring whatever model you want.

## License

Apache-2.0. See [LICENSE](LICENSE).

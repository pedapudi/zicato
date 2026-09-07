# Supervisor build cost measurement, 2026-09-05

Python package preparation can dominate a focused test run. On the measured
Linux host, installing the package and its dependencies took 65.07 seconds
with an empty Cargo target directory. Reusing a verified supervisor executable
reduced preparation to 0.71 seconds. The selected tests took about 2.5 seconds
in either case.

The measurements used revision `3bddf6424e5b13287fa9adaee34e73a97cb62457`
with the supervisor build and timing changes for issue #497. Both installations
created separate environments with `uv sync --all-extras --frozen --no-editable
--reinstall-package zicato`. Downloaded Python packages and Cargo registry
sources were already available locally. These results isolate build reuse;
they do not measure an empty dependency download cache or the complete suite.

The host ran Python 3.12.14 and Rust 1.98.1 on Linux x86-64. It exposed
32 CPUs in the process affinity mask. Cargo used two build jobs, and pytest
used two workers. Other verification processes were active on the host.

| Preparation state | Preparation wall seconds | Test wall seconds | Combined wall seconds | Summed process CPU seconds |
|---|---:|---:|---:|---:|
| Empty Cargo target and empty executable cache | 65.07 | 2.41 | 67.49 | 113.67 |
| Verified executable cache, fresh source timestamps | 0.71 | 2.65 | 3.36 | 4.70 |

Both runs selected the same 34 cases from `tools/test_supervisor_build.py`,
`tools/test_verification_cost.py`, and `tools/test_workflows.py`. All passed.
Maximum child resident memory was 522 MiB during cold preparation and 81 MiB
across the cached preparation and test commands. Each value measures the
maximum for an individual child process. Aggregate concurrent memory was unmeasured.

Worker startup and collection completed within 0.35 seconds in each run.
Summed setup, call, and teardown durations were 0.009, 1.021, and 0.002 seconds
for cold preparation, and 0.010, 1.075, and 0.002 seconds for cached preparation.
Parallel phase sums do not equal elapsed time. The timing plugin observed at
most two threads in either pytest worker at report boundaries; short-lived
thread and subprocess peaks between reports are outside that measurement.

Separate Cargo commands explain the preparation savings:

| Cargo state | Wall seconds | Summed process CPU seconds |
|---|---:|---:|
| Empty target directory | 61.61 | 103.47 |
| Same source and target directory | 0.05 | 0.05 |
| Same source bytes with a fresh checkout timestamp | 12.88 | 22.80 |

The fresh timestamp rebuilt the supervisor while reusing dependencies.
That remaining cost justified an executable cache in addition to the Cargo
dependency cache. The executable cache checks content and build inputs, so a
checkout timestamp alone does not invalidate it.

The installed wheel resolved its bundled supervisor from outside the checkout,
executed its version command, and matched the verified build's SHA-256 digest.
The wheel declared native platform metadata. Missing, damaged, or mismatched
cache entries require a successful build; build failures cannot publish a wheel.
The six packaging regression cases also failed against the unmodified build hook.

The JSON reports and command logs are retained in
`/home/sunil/.local/state/zicato/supervisor-build-measurements/`.
The comparable installation reports are `cold-preparation.json` and
`exact-cache-preparation.json`; the corresponding test reports are
`cold-tests.json` and `exact-cache-tests.json`.

## Repeating a measurement

Run commands from the repository root, with dependencies in a virtual environment:

```sh
python tools/verification_cost.py --output preparation.json -- \
  uv sync --all-extras --frozen --reinstall-package zicato
python tools/verification_cost.py --pytest --output tests.json -- \
  python -m pytest tools/test_supervisor_build.py -n 2
```

The report records the command, revision, worktree state, interpreter,
toolchain, resource limits, concurrency controls, exit status, wall time, and
summed process CPU time. Pytest reports add selected test identifiers,
worker startup and collection, individual test phases, and observed process
and thread counts. A failed command remains failed.

For a cold comparison, use an empty Cargo target directory and preserve the
existing `.supervisor-cache` directory under a separate name before running.
For the cached comparison, restore that directory and retain the same source,
compiler, target, profile, and build environment. Create a fresh virtual
environment for each preparation command. Compare the same selected tests.

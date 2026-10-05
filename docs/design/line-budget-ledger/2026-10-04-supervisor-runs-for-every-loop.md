# The supervisor runs for every loop and audits its end

| Measurement | Delta |
|---|---:|
| Total | +841 |
| Production | +249 |
| Production logic | +138 |

`zicato evolve` starts the supervisor with or without the dashboard, reads the
`/statusz` address the supervisor prints, forwards its remaining output in
bounded chunks, and keeps `runtime/supervisor.json` while the supervisor runs.
The supervisor stops after the final index repair through a new invocation exit
stack; evolve signals its whole process group with a bounded SIGTERM-then-SIGKILL
wait, and its integrity loop runs one more scan on shutdown. The workspace layout declares the
supervisor-owned `proctor/` ledger directory. Most of the total is tests: the
binary's address line and final scan, the bounded stop of a process group that
ignores SIGTERM, output lines longer than the read buffer, the record's
lifetime, teardown ordering, and an audit-hook test that no Python code changes
`proctor/` under either generation store.

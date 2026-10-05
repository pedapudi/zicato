# The supervisor runs for every loop and audits its end

| Measurement | Delta |
|---|---:|
| Total | +587 |
| Production | +182 |
| Production logic | +96 |

`zicato evolve` starts the supervisor with or without the dashboard, reads the
`/statusz` address the supervisor prints, forwards its remaining output, and
records the address in `runtime/supervisor.json`. The supervisor stops after
the final index repair through a new invocation exit stack, and its integrity
loop runs one more scan on shutdown. The workspace layout declares the
supervisor-owned `proctor/` ledger directory. Most of the total is tests: the
binary's address line and final scan, the bounded stop, teardown ordering, and
an audit-hook test that no Python code changes `proctor/`.

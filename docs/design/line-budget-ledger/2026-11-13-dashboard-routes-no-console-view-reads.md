# Dashboard routes no console view reads

| Measurement | Delta |
|---|---:|
| Total | -2,560 |
| Production | -1,231 |
| Production logic | -841 |

The dashboard stops serving eighteen `GET` routes that no console view
fetches and no command, skill or tool reads: `/api/state`, `/api/config`,
`/api/search`, the contract diff, the journal as JSON, the file index and
tree, the per-generation matchup detail, the drift movements, the matchup
conversations, and the granular per-judge, expectations, header,
hypothesis-accuracy and episode-export routes whose payloads the candidate
dossier carries. The query-layer readers only those routes reached go with
them, as do their payload contracts, their tests, and their rows in the
recorded route and reader snapshots. The total falls further than
production because the tests and recorded responses of a removed route are
removed with it.

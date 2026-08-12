# toolbox

A three-file repository used as the working tree for target 4's board.

- `ops.py` — the arithmetic helpers. This is the file requests are
  normally about.
- `vendor/legacy_format.py` — third-party rendering code, vendored
  verbatim. Replaced wholesale on upgrade.
- `checks/verify_ops.py` — asserts `ops.py` behaves. Run it with
  `python checks/verify_ops.py`.

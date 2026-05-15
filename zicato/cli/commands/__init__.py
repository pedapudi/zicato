"""zicato CLI subcommand modules.

One module per command group. The top-level ``zicato`` console script
imports each group and registers it. Groups are independent — adding a
new one does not require touching existing ones.
"""

from __future__ import annotations

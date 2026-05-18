"""Allow ``python -m zicato.cli`` to run the CLI in addition to the
installed ``zicato`` console-script entry point.
"""

from __future__ import annotations

from zicato.cli import main

if __name__ == "__main__":
    main()

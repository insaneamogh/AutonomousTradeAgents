"""``python -m trading_agents`` → the council smoke CLI.

Kept as a thin delegator so the package has an obvious default entry point
while the implementation sits with its sibling CLIs in ``trading_agents.cli``.
"""

from __future__ import annotations

import sys

from trading_agents.cli.council import main

if __name__ == "__main__":
    sys.exit(main())

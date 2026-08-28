"""``python -m mcp_server`` — start the MCP server over stdio.

Seeds the demo fixture user (Postgres mode only) before the server starts
accepting tool calls, so the very first ``run_council_pass`` call doesn't
hit ``agent_decisions.user_id``'s foreign-key constraint against a
``users`` row that was never created (see
``mcp_server.context.ensure_demo_user_seeded``).
"""

from __future__ import annotations

import asyncio
import sys

from mcp_server.context import ensure_demo_user_seeded
from mcp_server.server import mcp


def main() -> int:
    asyncio.run(ensure_demo_user_seeded())
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())

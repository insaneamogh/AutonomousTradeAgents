"""Constructs the MCP server and registers the six tools from
``mcp_server.tools`` by reference.

This is the ONLY file in this package that imports the MCP SDK, so every
tool in ``tools.py`` stays directly unit-testable with no MCP
transport/client involved.

SDK note: the task brief for this server assumed
``mcp.server.fastmcp.FastMCP`` (the SDK's v1 decorator API). The version
``uv add "mcp[cli]"`` actually resolved — ``mcp==2.1.1`` — no longer ships
that module at all; importing it raises ``ModuleNotFoundError`` with a
message pointing at ``mcp.server.mcpserver.MCPServer`` as the v2
replacement (see https://py.sdk.modelcontextprotocol.io/v2/migration/
#fastmcp-renamed-to-mcpserver). Confirmed directly (not assumed from
training data) via a throwaway spike server + a real client round trip
over stdio (``mcp.client.Client`` + ``mcp.StdioServerParameters``) before
writing this file — ``MCPServer``'s ``.tool()``/``.add_tool()``/``.run()``
surface is functionally the same shape FastMCP had, just renamed.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from mcp_server import tools

mcp = MCPServer("autonomous-trade-agents")

# Registered by reference, not redefined here — every tool body lives in
# tools.py so it can be awaited directly in tests with no MCP transport
# involved (see apps/mcp_server/tests/test_tools.py).
mcp.add_tool(tools.run_council_pass)
mcp.add_tool(tools.list_positions)
mcp.add_tool(tools.list_recent_decisions)
mcp.add_tool(tools.get_scanner_status)
mcp.add_tool(tools.get_veto_ledger)
mcp.add_tool(tools.list_watchlist)

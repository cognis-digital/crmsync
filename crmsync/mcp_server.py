"""CRMSYNC MCP server — exposes scan() as an MCP tool for Cognis.Studio."""
from __future__ import annotations
from crmsync.core import scan, to_json

def serve() -> int:
    """Start an MCP stdio server. Requires the optional 'mcp' extra:
        pip install "cognis-crmsync[mcp]"
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception:
        print("Install the MCP extra: pip install 'cognis-crmsync[mcp]'")
        return 1
    app = FastMCP("crmsync")

    @app.tool()
    def crmsync_scan(target: str) -> str:
        """Bidirectional, idempotent sync of contacts/deals between a local SQLite source-of-truth and CRM APIs (HubSpot/Pipedrive/Salesforce) via one config.. Returns JSON findings."""
        return to_json(scan(target))

    app.run()
    return 0

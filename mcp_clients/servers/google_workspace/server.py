"""Google Workspace MCP server entrypoint."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

from mcp.server.fastmcp import FastMCP

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp_clients.servers.google_workspace.config.settings import get_google_settings
from mcp_clients.servers.google_workspace.tools.calendar import register_calendar_tools
from mcp_clients.servers.google_workspace.tools.contacts import register_contacts_tools
from mcp_clients.servers.google_workspace.tools.drive import register_drive_tools
from mcp_clients.servers.google_workspace.tools.gmail import register_gmail_tools
from mcp_clients.servers.google_workspace.tools.sheets import TOOLS_META, register_sheets_tools
from mcp_clients.servers.google_workspace.utils.logging import configure_logging


settings = get_google_settings()
configure_logging(settings.log_level)

mcp = FastMCP(settings.mcp_server_name)


def tool(category: str, tier: str = "core") -> Callable:
    """Register an MCP tool and its metadata."""

    def decorator(func):
        TOOLS_META[func.__name__] = {"category": category, "tier": tier}
        return mcp.tool()(func)

    return decorator


register_sheets_tools(mcp, tool)
register_calendar_tools(mcp, tool)
register_gmail_tools(mcp, tool)
register_drive_tools(mcp, tool)
register_contacts_tools(mcp, tool)


@mcp.resource("tools://metadata")
async def get_tools_metadata() -> str:
    """Metadata for all Google Workspace tools."""

    return json.dumps(TOOLS_META)


if __name__ == "__main__":
    mcp.run()

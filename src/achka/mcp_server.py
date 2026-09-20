"""Stdio MCP server exposing TOOL_SPECS to external clients (Codex and friends)."""

from __future__ import annotations

from pathlib import Path

from .db import connect, init_db
from .project import db_path, sync_agents_from_config
from .tools import TOOL_SPECS, call_tool, tool_result_text


def run_mcp(project_dir: Path, agent_name: str, db: Path | None = None) -> None:
    import anyio
    import mcp.types as mt
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    db = connect(db or db_path(project_dir))
    init_db(db)
    sync_agents_from_config(db, project_dir)

    async def on_list_tools(ctx, params):
        return mt.ListToolsResult(
            tools=[
                mt.Tool(name=s["name"], description=s["description"], input_schema=s["schema"])
                for s in TOOL_SPECS
            ]
        )

    async def on_call_tool(ctx, params):
        try:
            value = call_tool(db, agent_name, params.name, dict(params.arguments or {}))
            return mt.CallToolResult(
                content=[mt.TextContent(type="text", text=tool_result_text(value))]
            )
        except Exception as exc:  # surfaced to the model, not the transport
            return mt.CallToolResult(
                content=[mt.TextContent(type="text", text=f"error: {exc}")], is_error=True
            )

    server = Server(
        "achka", version="0.1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)

"""Stdio MCP server exposing TOOL_SPECS to external clients (Codex and friends)."""

from __future__ import annotations

from pathlib import Path

import anyio
import mcp.types as mt
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .db import connect, init_db
from .project import db_path, load_config, sync_agents_from_config
from .tools import call_tool, tool_result_text, toolset


def run_mcp(project_dir: Path, agent_name: str, db: Path | None = None, operator: bool = False) -> None:
    agents = load_config(project_dir).get("agents", {})
    cfg = agents.get(agent_name)
    # operator mode is asked for; an unknown name is a typo, not a promotion
    if operator:
        specs = toolset(None)
    elif cfg is None:
        raise SystemExit(
            f"'{agent_name}' is not an agent in config.toml "
            f"(have: {', '.join(sorted(agents)) or 'none'}). Pass --operator to act as the human."
        )
    else:
        specs = toolset(cfg)
    db = connect(db or db_path(project_dir))
    init_db(db)
    sync_agents_from_config(db, project_dir)

    async def on_list_tools(ctx, params):
        return mt.ListToolsResult(
            tools=[
                mt.Tool(name=s["name"], description=s["description"], input_schema=s["schema"])
                for s in specs
            ]
        )

    async def on_call_tool(ctx, params):
        try:
            value = call_tool(db, agent_name, params.name, dict(params.arguments or {}), specs)
            return mt.CallToolResult(
                content=[mt.TextContent(type="text", text=tool_result_text(value))]
            )
        except Exception as exc:  # surfaced to the model, not the transport
            return mt.CallToolResult(
                content=[mt.TextContent(type="text", text=f"error: {exc}")], is_error=True
            )

    server = Server(
        "kuska", version="0.1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)
